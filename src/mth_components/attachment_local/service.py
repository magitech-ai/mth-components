"""Local durable attachment backend rooted below the harness home.

Ports ``packages/attachment/attachment-local/src/index.ts``: a content-addressed
provider-independent image store, verbatim file storage and deterministic
request images. Mounting creates directories and configuration only; no network
connection or identity is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from magitech_harness.shell_env import resolve_magitech_home

from mth_components.attachment.store import AttachmentStore
from mth_components.attachment.types import (
    FileAttachmentRef,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    ImageMediaType,
    ImageRequestTarget,
    RequestImageAttachment,
    StoredImageAttachment,
)
from mth_components.attachment_local.compression import CompressionLimiter
from mth_components.attachment_local.file_store import (
    read_file_stream_verbatim,
    save_file_stream_verbatim,
    save_file_verbatim,
    stored_file_path,
)
from mth_components.attachment_local.normalization import NormalizationPolicy
from mth_components.attachment_local.request_image import (
    RequestImageResizePolicy,
    read_request_image_file,
    request_image_variant_id,
)
from mth_components.attachment_local.store import (
    PreparedImageFile,
    commit_prepared_image_file,
    normalized_image_path,
    prepare_image_file,
    read_image_file,
    save_image_file,
    validate_image_file,
)

DEFAULT_MAX_IMAGE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_IMAGES_PER_MESSAGE = 20
DEFAULT_MAX_MESSAGE_IMAGE_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 64_000_000
DEFAULT_MAX_IMAGE_DIMENSION = 65_535
DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS = 2048 * 2048
DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION = 8192
DEFAULT_NORMALIZED_IMAGE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_IMAGE_COMPRESSION_CONCURRENCY = 2
MAX_IMAGE_COMPRESSION_CONCURRENCY = 8
DEFAULT_REQUEST_IMAGE_MIN_SHRINK_FACTOR = 1.1
DEFAULT_REQUEST_IMAGE_BYTE_HEADROOM = 0.95

MEDIA_TYPES: tuple[ImageMediaType, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)


def _positive_int(config: Mapping[str, Any], key: str, default: int) -> int:
    """A config number must be a positive integer, never a coerced string."""
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"attachment-local: {key} must be a positive integer")
    return value


class LocalAttachmentStore(AttachmentStore):
    """Persistent content-addressed local attachment store."""

    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        home = str(cfg.get("dshHome") or resolve_magitech_home())
        self.root = str(Path(home, "attachments", "v1"))
        self.cache_root = str(Path(home, "cache", "attachments"))
        self.image_limits: ImageAttachmentLimits = {
            "maxImageBytes": _positive_int(
                cfg, "maxImageBytes", DEFAULT_MAX_IMAGE_BYTES
            ),
            "maxImagesPerMessage": _positive_int(
                cfg, "maxImagesPerMessage", DEFAULT_MAX_IMAGES_PER_MESSAGE
            ),
            "maxMessageImageBytes": _positive_int(
                cfg, "maxMessageImageBytes", DEFAULT_MAX_MESSAGE_IMAGE_BYTES
            ),
            "maxImagePixels": _positive_int(
                cfg, "maxImagePixels", DEFAULT_MAX_IMAGE_PIXELS
            ),
            "maxImageDimension": _positive_int(
                cfg, "maxImageDimension", DEFAULT_MAX_IMAGE_DIMENSION
            ),
            "mediaTypes": MEDIA_TYPES,
        }
        self.normalization_policy: NormalizationPolicy = {
            "maxPixels": _positive_int(
                cfg, "normalizedImageMaxPixels", DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS
            ),
            "maxDimension": _positive_int(
                cfg,
                "normalizedImageMaxDimension",
                DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION,
            ),
            "maxBytes": _positive_int(
                cfg, "normalizedImageMaxBytes", DEFAULT_NORMALIZED_IMAGE_MAX_BYTES
            ),
        }
        min_shrink = float(
            cfg.get(
                "requestImageMinShrinkFactor", DEFAULT_REQUEST_IMAGE_MIN_SHRINK_FACTOR
            )
        )
        headroom = float(
            cfg.get("requestImageByteHeadroom", DEFAULT_REQUEST_IMAGE_BYTE_HEADROOM)
        )
        if not math.isfinite(min_shrink) or min_shrink <= 1:
            raise ValueError(
                "attachment-local: requestImageMinShrinkFactor must be a finite "
                "number greater than one"
            )
        if not math.isfinite(headroom) or headroom <= 0 or headroom > 1:
            raise ValueError(
                "attachment-local: requestImageByteHeadroom must be a finite "
                "number in (0, 1]"
            )
        self.request_image_resize_policy: RequestImageResizePolicy = {
            "minShrinkFactor": min_shrink,
            "byteHeadroom": headroom,
        }
        concurrency = cfg.get(
            "imageCompressionConcurrency", DEFAULT_IMAGE_COMPRESSION_CONCURRENCY
        )
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
            or concurrency > MAX_IMAGE_COMPRESSION_CONCURRENCY
        ):
            raise ValueError(
                f"attachment-local: imageCompressionConcurrency must be an integer "
                f"from 1 through {MAX_IMAGE_COMPRESSION_CONCURRENCY}"
            )
        self.image_compression_concurrency = concurrency
        self.compression = CompressionLimiter(concurrency)
        self._inflight: dict[str, asyncio.Future[RequestImageAttachment]] = {}
        self._lock = asyncio.Lock()

    async def validate_image(self, payload: Mapping[str, Any]) -> None:
        await self.compression.run(
            lambda: asyncio.to_thread(
                validate_image_file,
                payload,
                self.image_limits,
                self.normalization_policy,
            )
        )

    async def save_images(
        self, inputs: Sequence[Mapping[str, Any]]
    ) -> list[ImageAttachmentRef]:
        self.validate_image_batch(inputs)

        async def prepare(item: Mapping[str, Any]) -> PreparedImageFile:
            return await self.compression.run(
                lambda: asyncio.to_thread(
                    prepare_image_file,
                    item,
                    self.image_limits,
                    self.normalization_policy,
                )
            )

        prepared = await asyncio.gather(*(prepare(item) for item in inputs))
        return [
            await self.compression.run(_commit_task(self.root, result))
            for result in prepared
        ]

    async def save_image(self, payload: Mapping[str, Any]) -> ImageAttachmentRef:
        return await self.compression.run(
            lambda: asyncio.to_thread(
                save_image_file,
                self.root,
                payload,
                self.image_limits,
                self.normalization_policy,
            )
        )

    async def read_image(
        self, ref: ImageAttachmentRef, signal: Any = None
    ) -> StoredImageAttachment:
        if signal is not None and signal.aborted:
            signal.throw_if_aborted()
        return await asyncio.to_thread(read_image_file, self.root, ref)

    def image_host_path(self, ref: ImageAttachmentRef) -> str:
        return str(normalized_image_path(self.root, ref))

    async def save_file(self, payload: Mapping[str, Any]) -> FileAttachmentRef:
        return await save_file_verbatim(self.root, payload)

    async def save_file_stream(self, payload: Mapping[str, Any]) -> FileAttachmentRef:
        return await save_file_stream_verbatim(self.root, payload)

    def read_file_stream(
        self, ref: FileAttachmentRef, signal: Any = None
    ) -> AsyncIterator[bytes]:
        return read_file_stream_verbatim(self.root, ref, signal)

    def file_host_path(self, ref: FileAttachmentRef) -> str:
        return stored_file_path(self.root, ref)

    async def read_image_request(
        self, ref: ImageAttachmentRef, target: Mapping[str, Any], signal: Any = None
    ) -> RequestImageAttachment:
        if signal is not None and signal.aborted:
            signal.throw_if_aborted()
        request_target: ImageRequestTarget = {
            "width": int(target["width"]),
            "height": int(target["height"]),
            "maxImageBytes": int(
                target.get("maxBytes", target.get("maxImageBytes", 0))
            ),
            "imageTargetBytes": int(
                target.get(
                    "imageTargetBytes",
                    target.get("maxBytes", target.get("maxImageBytes", 0)),
                )
            ),
        }
        variant = request_image_variant_id(
            ref, request_target, self.request_image_resize_policy
        )
        key = str(variant)
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is None:
                existing = asyncio.get_running_loop().create_future()
                self._inflight[key] = existing
                owner = True
            else:
                owner = False
        if not owner:
            return await asyncio.shield(existing)

        async def work() -> RequestImageAttachment:
            return await self.compression.run(
                lambda: asyncio.to_thread(
                    _read_request_image,
                    self.cache_root,
                    ref,
                    self.root,
                    request_target,
                    self.request_image_resize_policy,
                    signal,
                )
            )

        try:
            result = await work()
            existing.set_result(result)
            return result
        except BaseException as error:
            with contextlib.suppress(asyncio.InvalidStateError):
                existing.set_exception(error)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(key, None)


def _commit_task(
    root: str, result: PreparedImageFile
) -> Callable[[], Awaitable[ImageAttachmentRef]]:
    async def task() -> ImageAttachmentRef:
        return await asyncio.to_thread(commit_prepared_image_file, root, result)

    return task


def _read_request_image(  # noqa: PLR0917
    cache_root: str,
    ref: ImageAttachmentRef,
    root: str,
    target: ImageRequestTarget,
    policy: RequestImageResizePolicy,
    signal: Any,
) -> RequestImageAttachment:
    stored = read_image_file(root, ref)
    cancelled = None if signal is None else (lambda: bool(signal.aborted))
    return read_request_image_file(cache_root, stored, target, policy, cancelled)


__all__ = [
    "DEFAULT_IMAGE_COMPRESSION_CONCURRENCY",
    "DEFAULT_MAX_IMAGES_PER_MESSAGE",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_MAX_IMAGE_DIMENSION",
    "DEFAULT_MAX_IMAGE_PIXELS",
    "DEFAULT_MAX_MESSAGE_IMAGE_BYTES",
    "DEFAULT_NORMALIZED_IMAGE_MAX_BYTES",
    "DEFAULT_NORMALIZED_IMAGE_MAX_DIMENSION",
    "DEFAULT_NORMALIZED_IMAGE_MAX_PIXELS",
    "MAX_IMAGE_COMPRESSION_CONCURRENCY",
    "MEDIA_TYPES",
    "LocalAttachmentStore",
]

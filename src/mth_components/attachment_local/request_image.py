"""Deterministic cached image versions for model requests.

Ports ``packages/attachment/attachment-local/src/request-image.ts``: a stable
variant id over every transform input, a byte-limit shrink loop, and a verified
local cache of request images derived from a normalized attachment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any, TypedDict

from PIL import Image, ImageOps

from mth_components.attachment.types import (
    AttachmentError,
    ImageAttachmentRef,
    ImageMediaType,
    ImageRequestTarget,
    RequestImageAttachment,
    StoredImageAttachment,
    long_edge_dimensions,
    request_image_dimensions,
)
from mth_components.attachment_local.encoding import (
    IMAGE_ENCODING_QUALITIES,
    WEBP_ENCODING_EFFORT,
    chosen,
    encode_first_within_limit,
    encoding_ladder,
)
from mth_components.attachment_local.image import (
    detect_image,
    encoded_alpha_is_compatible,
    probe_image,
)

REQUEST_IMAGE_TRANSFORM_VERSION = "request-image-v7"


class RequestImageResizePolicy(TypedDict):
    minShrinkFactor: float
    byteHeadroom: float


class EncodedRequestImage(TypedDict):
    data: bytes
    mediaType: ImageMediaType
    width: int
    height: int


class VerifiedRequestImage(EncodedRequestImage):
    hasAlpha: bool


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checked_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AttachmentError(
            f"{name} must be a positive integer.", "INVALID_ATTACHMENT_REF"
        )
    return value


def _validate_target(target: ImageRequestTarget) -> None:
    _checked_integer(target["width"], "Image request width")
    _checked_integer(target["height"], "Image request height")
    _checked_integer(target["maxImageBytes"], "Image request maxImageBytes")
    _checked_integer(target["imageTargetBytes"], "Image request imageTargetBytes")
    if target["imageTargetBytes"] > target["maxImageBytes"]:
        raise AttachmentError(
            "Image request imageTargetBytes must not exceed maxImageBytes.",
            "INVALID_ATTACHMENT_REF",
        )


def _descriptor(
    attachment: ImageAttachmentRef,
    target: ImageRequestTarget,
    policy: RequestImageResizePolicy,
) -> str:
    return json.dumps(
        {
            "transformVersion": REQUEST_IMAGE_TRANSFORM_VERSION,
            "attachmentId": attachment["attachmentId"],
            "targetWidth": target["width"],
            "targetHeight": target["height"],
            "maxImageBytes": target["maxImageBytes"],
            "imageTargetBytes": target["imageTargetBytes"],
            "resize": {
                "minShrinkFactor": policy["minShrinkFactor"],
                "byteHeadroom": policy["byteHeadroom"],
            },
            "encoding": {
                "webpQualities": list(IMAGE_ENCODING_QUALITIES),
                "webpEffort": WEBP_ENCODING_EFFORT,
                "jpegQualities": list(IMAGE_ENCODING_QUALITIES),
                "order": ["alpha:webp", "opaque:jpeg"],
                "colourspace": "srgb",
            },
        },
        separators=(",", ":"),
    )


def request_image_variant_id(
    attachment: ImageAttachmentRef,
    target: ImageRequestTarget,
    policy: RequestImageResizePolicy,
) -> str:
    return f"sha256:{_digest(_descriptor(attachment, target, policy))}"


def _prepared(
    attachment: StoredImageAttachment, dimensions: tuple[int, int], has_alpha: bool
) -> Any:
    image = ImageOps.exif_transpose(Image.open(BytesIO(attachment["data"])))
    image = image.convert("RGBA" if has_alpha else "RGB")
    if dimensions != image.size:
        image = image.resize(dimensions, Image.Resampling.LANCZOS)
    return image


def _create_request_image(
    attachment: StoredImageAttachment,
    target: ImageRequestTarget,
    has_alpha: bool,
    policy: RequestImageResizePolicy,
    cancelled: Callable[[], bool] | None,
) -> EncodedRequestImage:
    ref = attachment["ref"]
    if (
        target["width"] >= ref["width"]
        and target["height"] >= ref["height"]
        and len(attachment["data"]) <= target["imageTargetBytes"]
    ):
        return {
            "data": attachment["data"],
            "mediaType": ref["mediaType"],
            "width": ref["width"],
            "height": ref["height"],
        }
    dimensions = long_edge_dimensions(
        ref["width"],
        ref["height"],
        ref["width"] if ref["width"] >= ref["height"] else ref["height"],
    )
    if dimensions[0] > target["width"] or dimensions[1] > target["height"]:
        dimensions = request_image_dimensions(
            ref["width"], ref["height"], float("inf"), target["width"], target["height"]
        )
    while True:
        if cancelled is not None and cancelled():
            raise asyncio.CancelledError()
        result = chosen(
            encode_first_within_limit(
                encoding_ladder(
                    _prepared(attachment, dimensions, has_alpha), has_alpha
                ),
                target["imageTargetBytes"],
            )
        )
        candidate: EncodedRequestImage = {
            "data": result["data"],
            "mediaType": result["mediaType"],  # type: ignore[typeddict-item]
            "width": result["width"],
            "height": result["height"],
        }
        if len(candidate["data"]) <= target["maxImageBytes"]:
            return candidate
        long_edge = max(candidate["width"], candidate["height"])
        if long_edge == 1:
            raise AttachmentError(
                f"Image still exceeds the {target['maxImageBytes']}-byte request limit "
                "at 1x1 pixels. Increase the image byte limit.",
                "IMAGE_REQUEST_TOO_LARGE",
            )
        divisor = max(
            policy["minShrinkFactor"],
            (
                len(candidate["data"])
                / (policy["byteHeadroom"] * target["maxImageBytes"])
            )
            ** 0.5,
        )
        next_edge = max(1, min(long_edge - 1, int(long_edge / divisor)))
        dimensions = long_edge_dimensions(ref["width"], ref["height"], next_edge)


def _cache_path(root: str, hash_value: str) -> Path:
    return Path(root, "request-images", hash_value[:2], hash_value)


def _read_cached(
    path: Path,
    target: ImageRequestTarget,
    expected_alpha: bool,
    cancelled: Callable[[], bool] | None,
) -> VerifiedRequestImage | None:
    try:
        data = path.read_bytes()
        if len(data) > target["maxImageBytes"]:
            return None
        detected = probe_image(data)
        if (
            detected["depth"] != "uchar"
            or detected["space"] != "srgb"
            or detected["width"] > target["width"]
            or detected["height"] > target["height"]
            or not encoded_alpha_is_compatible(expected_alpha, detected)
        ):
            return None
        return {
            "data": data,
            "mediaType": detected["mediaType"],
            "width": detected["width"],
            "height": detected["height"],
            "hasAlpha": detected["hasAlpha"],
        }
    except Exception as error:
        if cancelled is not None and cancelled():
            raise asyncio.CancelledError() from error
        return None


def _verify(image: EncodedRequestImage, expected_alpha: bool) -> VerifiedRequestImage:
    detected = detect_image(image["data"])
    if (
        detected["depth"] != "uchar"
        or detected["space"] != "srgb"
        or detected["width"] != image["width"]
        or detected["height"] != image["height"]
        or detected["mediaType"] != image["mediaType"]
        or not encoded_alpha_is_compatible(expected_alpha, detected)
    ):
        raise AttachmentError(
            "Encoded model-request image does not match its verified 8-bit "
            "sRGB metadata.",
            "ATTACHMENT_WRITE_FAILED",
        )
    return {**image, "hasAlpha": detected["hasAlpha"]}


def _write_cached(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_request_image_file(
    root: str,
    attachment: StoredImageAttachment,
    target: ImageRequestTarget,
    policy: RequestImageResizePolicy,
    cancelled: Callable[[], bool] | None = None,
) -> RequestImageAttachment:
    """Generate or reuse one request image below the local attachment cache."""
    if cancelled is not None and cancelled():
        raise asyncio.CancelledError()
    _validate_target(target)
    source = probe_image(attachment["data"])
    variant_id = request_image_variant_id(attachment["ref"], target, policy)
    path = _cache_path(root, variant_id[len("sha256:") :])
    cached = _read_cached(path, target, source["hasAlpha"], cancelled)
    if cached is not None:
        verified = cached
    else:
        created = _create_request_image(
            attachment, target, source["hasAlpha"], policy, cancelled
        )
        verified = (
            {**created, "hasAlpha": source["hasAlpha"]}
            if created["data"] == attachment["data"]
            else _verify(created, source["hasAlpha"])
        )
    if cached is None and verified["data"] != attachment["data"]:
        _write_cached(path, verified["data"])
    return {
        "variantId": variant_id,
        "attachment": attachment["ref"],
        "data": verified["data"],
        "mediaType": verified["mediaType"],
        "bytes": len(verified["data"]),
        "width": verified["width"],
        "height": verified["height"],
        "depth": "uchar",
        "space": "srgb",
        "hasAlpha": verified["hasAlpha"],
    }


__all__ = [
    "REQUEST_IMAGE_TRANSFORM_VERSION",
    "RequestImageResizePolicy",
    "read_request_image_file",
    "request_image_variant_id",
]

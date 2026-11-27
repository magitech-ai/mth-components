"""Durable attachment storage seam (`ctx.attachments`).

Ports ``packages/attachment/attachment/src/index.ts``: admission of wire
uploads, batch limits, prompt-content admission, and the optional verbatim-file
and request-image capabilities that a backend may implement.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from animas import Context, Service

from mth_components.attachment.types import (
    AttachmentError,
    FileAttachmentRef,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    RequestImageAttachment,
    SaveFileAttachment,
    SaveImageAttachment,
    is_attachment_error,
)


def decode_canonical_base64(data: str, code: str, empty: str = "reject") -> bytes:
    """Decode canonical base64, refusing non-canonical spellings."""
    if data == "" and empty == "reject":
        raise AttachmentError(
            "Image upload is not canonical base64.", "INVALID_IMAGE_BASE64"
        )
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise AttachmentError(
            f"{'Image' if code == 'INVALID_IMAGE_BASE64' else 'File'} upload is "
            "not canonical base64.",
            code,
            error,
        ) from error
    if base64.b64encode(decoded).decode("ascii") != data:
        raise AttachmentError(
            f"{'Image' if code == 'INVALID_IMAGE_BASE64' else 'File'} upload is "
            "not canonical base64.",
            code,
        )
    return decoded


class AttachmentStore(Service):
    """Immutable binary attachment service; implementations validate before publish."""

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "attachments")

    image_limits: ImageAttachmentLimits

    async def validate_image(self, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError

    @staticmethod
    def assert_message_images(
        limits: ImageAttachmentLimits, count: int, total: int
    ) -> None:
        if count > limits["maxImagesPerMessage"]:
            raise AttachmentError(
                "Image batch exceeds the configured image-count limit.",
                "TOO_MANY_IMAGES",
            )
        if total > limits["maxMessageImageBytes"]:
            raise AttachmentError(
                "Image batch exceeds the configured aggregate image-byte limit.",
                "IMAGES_TOO_LARGE",
            )

    def validate_image_batch(self, inputs: Sequence[Mapping[str, Any]]) -> None:
        limits = self.image_limits
        self.assert_message_images(
            limits,
            len(inputs),
            sum(len(cast_bytes(item.get("data"))) for item in inputs),
        )
        for item in inputs:
            if item.get("mediaType") not in limits["mediaTypes"]:
                raise AttachmentError(
                    f"Image type {item.get('mediaType')} is not accepted by "
                    "this deployment.",
                    "UNSUPPORTED_IMAGE_TYPE",
                )

    async def save_images(
        self, inputs: Sequence[Mapping[str, Any]]
    ) -> list[ImageAttachmentRef]:
        self.validate_image_batch(inputs)
        for item in inputs:
            await self.validate_image(item)
        return [await self.save_image(item) for item in inputs]

    async def save_image(self, payload: Mapping[str, Any]) -> ImageAttachmentRef:
        raise NotImplementedError

    async def admit_encoded_images(
        self, images: Sequence[Mapping[str, Any]]
    ) -> list[ImageAttachmentRef]:
        prepared = [
            {
                "data": decode_canonical_base64(item["data"], "INVALID_IMAGE_BASE64"),
                "mediaType": item["mediaType"],
                **({} if item.get("name") is None else {"name": item["name"]}),
            }
            for item in images
        ]
        return await self.save_images(prepared)

    async def admit_encoded_file(self, file: Mapping[str, Any]) -> FileAttachmentRef:
        return await self.save_file(
            {
                "data": decode_canonical_base64(
                    file["data"], "INVALID_FILE_BASE64", empty="accept"
                ),
                **({} if file.get("name") is None else {"name": file["name"]}),
            }
        )

    def is_attachment_error(self, error: Any) -> bool:
        return is_attachment_error(error)

    async def admit_prompt_content(
        self, content: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        encoded = [
            item
            for item in content
            if item.get("type") == "image" and "attachment" not in item
        ]
        stored = [
            item
            for item in content
            if item.get("type") == "image" and "attachment" in item
        ]
        if stored:
            total = sum(
                len(decode_canonical_base64(item["data"], "INVALID_IMAGE_BASE64"))
                for item in encoded
            ) + sum(int(item.get("encodedBytes", 0)) for item in stored)
            self.assert_message_images(
                self.image_limits, len(encoded) + len(stored), total
            )
        refs = await self.admit_encoded_images(encoded) if encoded else []
        result: list[dict[str, Any]] = []
        index = 0
        for part in content:
            if part.get("type") == "text":
                result.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "file":
                result.append({"type": "file", "attachment": part["attachment"]})
            elif "attachment" in part:
                result.append({"type": "image", "attachment": part["attachment"]})
            else:
                result.append({"type": "image", "attachment": refs[index]})
                index += 1
        return result

    def image_host_path(self, ref: Any) -> str | None:
        del ref
        return None

    async def save_file(self, payload: Mapping[str, Any]) -> FileAttachmentRef:
        del payload
        raise AttachmentError(
            "The mounted attachment provider cannot store verbatim files.",
            "ATTACHMENT_FILES_UNSUPPORTED",
        )

    async def save_file_stream(self, payload: Mapping[str, Any]) -> FileAttachmentRef:
        del payload
        raise AttachmentError(
            "The mounted attachment provider cannot stream verbatim files.",
            "ATTACHMENT_FILES_UNSUPPORTED",
        )

    def read_file_stream(self, ref: Any, signal: Any = None) -> AsyncIterator[bytes]:
        del ref, signal
        raise AttachmentError(
            "The mounted attachment provider cannot read verbatim files.",
            "ATTACHMENT_FILES_UNSUPPORTED",
        )

    def file_host_path(self, ref: Any) -> str | None:
        del ref
        return None

    async def read_image_request(
        self, ref: Any, target: Any, signal: Any = None
    ) -> RequestImageAttachment:
        del ref, target, signal
        raise AttachmentError(
            "The mounted attachment provider cannot derive model-request images.",
            "ATTACHMENT_PROJECTION_UNSUPPORTED",
        )


def cast_bytes(value: Any) -> bytes:
    """Bytes already; used by batch byte accounting."""
    return value if isinstance(value, bytes) else b""


__all__ = [
    "AttachmentStore",
    "SaveFileAttachment",
    "SaveImageAttachment",
    "cast_bytes",
    "decode_canonical_base64",
]

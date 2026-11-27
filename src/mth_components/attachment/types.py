"""Durable attachment vocabulary and stable failures.

Ports ``packages/attachment/attachment/src/{types,error,request-projection}.ts``.
Codes and wire shapes are stable: a reference never carries a host path or a
bearer URL, and a failure is routed by code rather than message.
"""

from __future__ import annotations

import math
from typing import Any, Literal, TypedDict

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

IMAGE_ADMISSION_ERROR_CODES = (
    "TOO_MANY_IMAGES",
    "IMAGES_TOO_LARGE",
    "UNSUPPORTED_IMAGE_TYPE",
    "INVALID_IMAGE_BASE64",
    "INVALID_IMAGE",
    "IMAGE_TYPE_MISMATCH",
    "IMAGE_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
    "IMAGE_DIMENSION_TOO_LARGE",
)

ATTACHMENT_ERROR_CODES = (
    *IMAGE_ADMISSION_ERROR_CODES,
    "INVALID_FILE_BASE64",
    "INVALID_ATTACHMENT_REF",
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_WRITE_FAILED",
    "ATTACHMENT_NOT_FOUND",
    "ATTACHMENT_READ_FAILED",
    "ATTACHMENT_PROJECTION_UNSUPPORTED",
    "IMAGE_REQUEST_TOO_LARGE",
    "ATTACHMENT_FILES_UNSUPPORTED",
)


class AttachmentError(Exception):
    """A stable attachment failure with a machine-routable code."""

    name = "AttachmentError"

    def __init__(
        self, message: str, code: str, cause: BaseException | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        if cause is not None:
            self.__cause__ = cause


def is_attachment_error(error: Any) -> bool:
    """Recognize attachment failures by code across package boundaries."""
    return (
        isinstance(error, Exception)
        and getattr(error, "code", None) in ATTACHMENT_ERROR_CODES
    )


def is_image_admission_error(error: Any) -> bool:
    """Whether the caller can correct the submitted image content or batch."""
    return is_attachment_error(error) and error.code in IMAGE_ADMISSION_ERROR_CODES


class ImageAttachmentRef(TypedDict, total=False):
    attachmentId: str
    mediaType: ImageMediaType
    bytes: int
    width: int
    height: int
    name: str
    originalDimensions: dict[str, int]


class FileAttachmentRef(TypedDict):
    attachmentId: str
    name: str
    bytes: int


class ImageRequestTarget(TypedDict):
    width: int
    height: int
    maxImageBytes: int
    imageTargetBytes: int


class RequestImageAttachment(TypedDict):
    variantId: str
    attachment: ImageAttachmentRef
    data: bytes
    mediaType: ImageMediaType
    bytes: int
    width: int
    height: int
    depth: Literal["uchar"]
    space: Literal["srgb"]
    hasAlpha: bool


class StoredImageAttachment(TypedDict):
    ref: ImageAttachmentRef
    data: bytes


class ImageAttachmentLimits(TypedDict):
    maxImageBytes: int
    maxImagesPerMessage: int
    maxMessageImageBytes: int
    maxImagePixels: int
    maxImageDimension: int
    mediaTypes: tuple[ImageMediaType, ...]


class SaveImageAttachment(TypedDict, total=False):
    data: bytes
    mediaType: ImageMediaType
    name: str


class SaveFileAttachment(TypedDict, total=False):
    data: bytes
    name: str


def request_image_dimensions(
    width: int,
    height: int,
    max_pixels: float,
    max_width: float = math.inf,
    max_height: float = math.inf,
) -> tuple[int, int]:
    """Aspect-preserving integer dimensions within pixel and edge limits."""
    scale = min(
        1.0,
        math.sqrt(max_pixels / (width * height)),
        max_width / width,
        max_height / height,
    )
    if scale == 1:
        return width, height
    if width >= height:
        projected_width = max(1, math.floor(width * scale))
        projected_height = max(1, round(projected_width * height / width))
        while projected_width * projected_height > max_pixels and projected_width > 1:
            projected_width -= 1
            projected_height = max(1, round(projected_width * height / width))
        return projected_width, projected_height
    projected_height = max(1, math.floor(height * scale))
    projected_width = max(1, round(projected_height * width / height))
    while projected_width * projected_height > max_pixels and projected_height > 1:
        projected_height -= 1
        projected_width = max(1, round(projected_height * width / height))
    return projected_width, projected_height


def long_edge_dimensions(width: int, height: int, long_edge: int) -> tuple[int, int]:
    """Aspect-preserving dimensions with an exact long edge."""
    if long_edge >= max(width, height):
        return width, height
    if width >= height:
        return long_edge, max(1, round(long_edge * height / width))
    return max(1, round(long_edge * width / height)), long_edge


__all__ = [
    "ATTACHMENT_ERROR_CODES",
    "IMAGE_ADMISSION_ERROR_CODES",
    "AttachmentError",
    "FileAttachmentRef",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageMediaType",
    "ImageRequestTarget",
    "RequestImageAttachment",
    "SaveFileAttachment",
    "SaveImageAttachment",
    "StoredImageAttachment",
    "is_attachment_error",
    "is_image_admission_error",
    "long_edge_dimensions",
    "request_image_dimensions",
]

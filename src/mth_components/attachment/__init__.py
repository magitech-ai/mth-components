"""Durable attachment vocabulary shared by storage backends."""

from __future__ import annotations

from mth_components.attachment.types import (
    AttachmentError,
    FileAttachmentRef,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    ImageRequestTarget,
    RequestImageAttachment,
    StoredImageAttachment,
    is_attachment_error,
    is_image_admission_error,
    long_edge_dimensions,
    request_image_dimensions,
)

__all__ = [
    "AttachmentError",
    "FileAttachmentRef",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageRequestTarget",
    "RequestImageAttachment",
    "StoredImageAttachment",
    "is_attachment_error",
    "is_image_admission_error",
    "long_edge_dimensions",
    "request_image_dimensions",
]

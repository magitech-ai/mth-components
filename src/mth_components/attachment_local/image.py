"""Raster inspection with Pillow: full decode at admission, header probe on read.

Ports ``packages/attachment/attachment-local/src/image.ts``. EXIF orientations
5-8 transpose the stored raster, so limits and references share the perceived
axes rather than the stored ones.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, TypedDict

from PIL import Image, UnidentifiedImageError

from mth_components.attachment.types import AttachmentError, ImageMediaType

_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class DetectedImage(TypedDict):
    mediaType: ImageMediaType
    width: int
    height: int
    animated: bool
    carriesMetadata: bool
    depth: str
    space: str
    hasAlpha: bool


def encoded_alpha_is_compatible(
    source_has_alpha: bool | None,
    output: Any,
) -> bool:
    """Whether an encoded result's alpha metadata matches its source."""
    return (
        source_has_alpha is None
        or output["hasAlpha"] is source_has_alpha
        or (
            source_has_alpha
            and not output["hasAlpha"]
            and output["mediaType"] == "image/webp"
        )
    )


def _metadata(image: Any) -> DetectedImage:
    fmt = image.format
    media_type = _MEDIA_TYPES.get(fmt) if isinstance(fmt, str) else None
    if media_type is None:
        raise AttachmentError("Unsupported or malformed image data.", "INVALID_IMAGE")
    exif = image.getexif() if hasattr(image, "getexif") else {}
    orientation = exif.get(274, 1) if exif else 1
    transposed = isinstance(orientation, int) and orientation >= 5
    width, height = image.size
    info = image.info or {}
    carries = any(
        key in info
        for key in ("exif", "xmp", "iptc", "icc_profile", "photoshop", "comment")
    ) or bool(exif)
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in info
    depth = "ushort" if "16" in image.mode else "uchar"
    mode = image.mode
    if mode in ("1", "L", "LA"):
        space = "b-w"
    elif "16" in mode:
        space = "grey16"
    else:
        space = "srgb"
    return {
        "mediaType": media_type,  # type: ignore[typeddict-item]
        "width": height if transposed else width,
        "height": width if transposed else height,
        "animated": getattr(image, "n_frames", 1) > 1,
        "carriesMetadata": carries,
        "depth": depth,
        "space": space,
        "hasAlpha": has_alpha,
    }


def _open(data: bytes) -> Any:
    try:
        return Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", "INVALID_IMAGE", error
        ) from error


def probe_image(data: bytes) -> DetectedImage:
    """Derive reference fields from a digest-verified image's header only."""
    try:
        return _metadata(_open(data))
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", "INVALID_IMAGE", error
        ) from error


def detect_image(
    data: bytes,
    limits: Any = None,
) -> DetectedImage:
    """Fully decode a supported raster and enforce intrinsic-dimension limits."""
    try:
        image = _open(data)
        detected = _metadata(image)
        if (
            limits is not None
            and detected["width"] * detected["height"] > limits["maxPixels"]
        ):
            raise AttachmentError(
                "Image exceeds the configured decoded-pixel limit.",
                "IMAGE_TOO_MANY_PIXELS",
            )
        if (
            limits is not None
            and max(detected["width"], detected["height"]) > limits["maxDimension"]
        ):
            raise AttachmentError(
                "Image exceeds the configured per-side pixel limit.",
                "IMAGE_DIMENSION_TOO_LARGE",
            )
        image.load()
        return detected
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", "INVALID_IMAGE", error
        ) from error


__all__ = [
    "DetectedImage",
    "detect_image",
    "encoded_alpha_is_compatible",
    "probe_image",
]

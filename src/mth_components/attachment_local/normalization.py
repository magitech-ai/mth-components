"""Deterministic provider-independent image normalization.

Ports ``packages/attachment/attachment-local/src/normalization.ts``: a source
passes through byte-identically only when already clean and within every
budget; otherwise it is oriented, stripped of metadata, reduced to the pixel
budgets and encoded through the quality ladder to an 8-bit sRGB image.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, TypedDict

from PIL import Image, ImageOps

from mth_components.attachment.types import (
    AttachmentError,
    ImageMediaType,
    request_image_dimensions,
)
from mth_components.attachment_local.encoding import (
    chosen,
    encode_first_within_limit,
    encoding_ladder,
)
from mth_components.attachment_local.image import (
    DetectedImage,
    detect_image,
    encoded_alpha_is_compatible,
)


class NormalizationPolicy(TypedDict):
    maxPixels: int
    maxDimension: int
    maxBytes: int


class NormalizedImage(TypedDict):
    data: bytes
    mediaType: ImageMediaType
    width: int
    height: int


def can_pass_through_normalization(
    detected: DetectedImage, size: int, policy: NormalizationPolicy
) -> bool:
    """Whether the source bytes already satisfy every normalization rule."""
    return (
        detected["mediaType"] != "image/gif"
        and not detected["animated"]
        and not detected["carriesMetadata"]
        and detected["depth"] == "uchar"
        and detected["space"] == "srgb"
        and size <= policy["maxBytes"]
        and detected["width"] * detected["height"] <= policy["maxPixels"]
        and max(detected["width"], detected["height"]) <= policy["maxDimension"]
    )


def initial_dimensions(
    detected: DetectedImage, policy: NormalizationPolicy
) -> tuple[int, int]:
    """Apply the total-pixel budget, then the long-edge cap."""
    budgeted = request_image_dimensions(
        detected["width"], detected["height"], policy["maxPixels"]
    )
    long_edge = max(budgeted)
    if long_edge <= policy["maxDimension"]:
        return budgeted
    scale = policy["maxDimension"] / long_edge
    return (
        max(1, int(budgeted[0] * scale)),
        max(1, int(budgeted[1] * scale)),
    )


def _prepared(data: bytes, width: int, height: int, has_alpha: bool) -> Any:
    """One oriented, metadata-free, sRGB image reduced to the given box."""
    image = ImageOps.exif_transpose(Image.open(BytesIO(data)))
    mode = "RGBA" if has_alpha else "RGB"
    flat = image.convert(mode)
    clean = Image.frombytes(mode, flat.size, flat.tobytes())
    clean.thumbnail((width, height))
    return clean


def _verify(image: NormalizedImage, expected_alpha: bool | None) -> NormalizedImage:
    detected = detect_image(image["data"])
    if (
        detected["mediaType"] != image["mediaType"]
        or detected["width"] != image["width"]
        or detected["height"] != image["height"]
        or detected["animated"]
        or detected["carriesMetadata"]
        or detected["depth"] != "uchar"
        or detected["space"] != "srgb"
        or not encoded_alpha_is_compatible(expected_alpha, detected)
    ):
        raise AttachmentError(
            "Image normalization did not produce a single-frame 8-bit sRGB "
            "image with matching metadata.",
            "ATTACHMENT_WRITE_FAILED",
        )
    return image


def normalize_image(
    data: bytes, detected: DetectedImage, policy: NormalizationPolicy
) -> NormalizedImage:
    """Produce the persisted normalized bytes and their reference facts."""
    if can_pass_through_normalization(detected, len(data), policy):
        return {
            "data": data,
            "mediaType": detected["mediaType"],
            "width": detected["width"],
            "height": detected["height"],
        }
    try:
        width, height = initial_dimensions(detected, policy)
        prepared = _prepared(data, width, height, detected["hasAlpha"])
        result = chosen(
            encode_first_within_limit(
                encoding_ladder(prepared, detected["hasAlpha"]), policy["maxBytes"]
            )
        )
        normalized: NormalizedImage = {
            "data": result["data"],
            "mediaType": result["mediaType"],  # type: ignore[typeddict-item]
            "width": result["width"],
            "height": result["height"],
        }
        return _verify(
            normalized,
            None if detected["mediaType"] == "image/gif" else detected["hasAlpha"],
        )
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            f"The {detected['depth']} "
            f"{detected['mediaType'].replace('image/', '').upper()} "
            "could not be converted to the normalized 8-bit sRGB form.",
            "ATTACHMENT_WRITE_FAILED",
            error,
        ) from error


__all__ = [
    "NormalizationPolicy",
    "NormalizedImage",
    "can_pass_through_normalization",
    "initial_dimensions",
    "normalize_image",
]

"""Quality ladder and lazy candidate execution for image encoders.

Ports ``packages/attachment/attachment-local/src/encoding.ts``: WebP keeps a
source alpha channel, everything else is JPEG; the first output within the byte
target wins, otherwise the smallest completed output is retained.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any, TypedDict, cast

IMAGE_ENCODING_QUALITIES = (85, 75, 60)
WEBP_ENCODING_EFFORT = 0


class EncodedImage(TypedDict):
    data: bytes
    mediaType: str
    width: int
    height: int


class ExhaustedEncoding(TypedDict):
    smallest: EncodedImage


def encode(image: Any, media_type: str, quality: int) -> EncodedImage:
    """Encode one prepared image at one ladder quality."""
    buffer = BytesIO()
    if media_type == "image/webp":
        image.save(buffer, format="WEBP", quality=quality, method=WEBP_ENCODING_EFFORT)
    else:
        flat = image if image.mode in ("RGB", "L") else image.convert("RGB")
        flat.save(buffer, format="JPEG", quality=quality)
    return {
        "data": buffer.getvalue(),
        "mediaType": media_type,
        "width": image.width,
        "height": image.height,
    }


def encoding_ladder(image: Any, has_alpha: bool) -> list[Callable[[], EncodedImage]]:
    """Build the lazy highest-to-lowest quality ladder for one prepared image."""
    media_type = "image/webp" if has_alpha else "image/jpeg"

    def attempt(quality: int) -> Callable[[], EncodedImage]:
        return lambda: encode(image.copy(), media_type, quality)

    return [attempt(quality) for quality in IMAGE_ENCODING_QUALITIES]


def encode_first_within_limit(
    attempts: list[Callable[[], EncodedImage]], target_bytes: int
) -> EncodedImage | ExhaustedEncoding:
    """Execute candidates in preference order, stopping at the first fit."""
    if not attempts:
        raise ValueError("image encoding requires at least one candidate")
    smallest = attempts[0]()
    if len(smallest["data"]) <= target_bytes:
        return smallest
    for attempt in attempts[1:]:
        candidate = attempt()
        if len(candidate["data"]) <= target_bytes:
            return candidate
        if len(candidate["data"]) < len(smallest["data"]):
            smallest = candidate
    return {"smallest": smallest}


def is_exhausted_encoding(result: EncodedImage | ExhaustedEncoding) -> bool:
    """Whether every candidate exceeded the byte target."""
    return "smallest" in result


def chosen(result: EncodedImage | ExhaustedEncoding) -> EncodedImage:
    """Unwrap a ladder result into complete encoded bytes."""
    return (
        cast(ExhaustedEncoding, result)["smallest"]
        if is_exhausted_encoding(result)
        else cast(EncodedImage, result)
    )


__all__ = [
    "IMAGE_ENCODING_QUALITIES",
    "WEBP_ENCODING_EFFORT",
    "EncodedImage",
    "ExhaustedEncoding",
    "chosen",
    "encode",
    "encode_first_within_limit",
    "encoding_ladder",
    "is_exhausted_encoding",
]

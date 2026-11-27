"""Unit tests for the local attachment backend."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from PIL import Image

from mth_components.attachment.store import (
    AttachmentStore,
    decode_canonical_base64,
)
from mth_components.attachment.types import (
    AttachmentError,
    is_attachment_error,
    is_image_admission_error,
    long_edge_dimensions,
    request_image_dimensions,
)
from mth_components.attachment_local.component import apply
from mth_components.attachment_local.compression import (
    CompressionLimiter,
    compression_failure,
)
from mth_components.attachment_local.encoding import (
    IMAGE_ENCODING_QUALITIES,
    chosen,
    encode,
    encode_first_within_limit,
    encoding_ladder,
    is_exhausted_encoding,
)
from mth_components.attachment_local.file_store import (
    file_leaf_name,
    read_file_stream_verbatim,
    save_file_stream_verbatim,
    save_file_verbatim,
    stored_file_path,
)
from mth_components.attachment_local.image import (
    detect_image,
    encoded_alpha_is_compatible,
    probe_image,
)
from mth_components.attachment_local.normalization import (
    can_pass_through_normalization,
    initial_dimensions,
    normalize_image,
)
from mth_components.attachment_local.request_image import (
    read_request_image_file,
    request_image_variant_id,
)
from mth_components.attachment_local.service import LocalAttachmentStore
from mth_components.attachment_local.store import (
    commit_prepared_image_file,
    digest,
    display_name,
    ensure_reference,
    normalized_image_path,
    prepare_image_file,
    read_image_file,
    validate_image_file,
)

LIMITS = {
    "maxImageBytes": 50 * 1024 * 1024,
    "maxImagesPerMessage": 20,
    "maxMessageImageBytes": 200 * 1024 * 1024,
    "maxImagePixels": 64_000_000,
    "maxImageDimension": 65_535,
    "mediaTypes": ("image/png", "image/jpeg", "image/webp", "image/gif"),
}
POLICY = {"maxPixels": 4096, "maxDimension": 64, "maxBytes": 1024 * 1024}
RESIZE = {"minShrinkFactor": 1.1, "byteHeadroom": 0.95}


def _image(
    fmt: str = "PNG", size: tuple[int, int] = (8, 6), alpha: bool = False
) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, size, (10, 20, 30, 40) if alpha else (10, 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def _input(
    fmt: str = "PNG", size: tuple[int, int] = (8, 6), alpha: bool = False
) -> dict[str, Any]:
    media = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }[fmt]
    return {
        "data": _image(fmt, size, alpha),
        "mediaType": media,
        "name": "C:\\pics\\a.png",
    }


def _ctx(root: Path) -> Context:
    return Context()


def _store(root: Path, **config: Any) -> LocalAttachmentStore:
    ctx = _ctx(root)
    return LocalAttachmentStore(ctx, {"dshHome": str(root), **config})


def test_types_and_projection() -> None:
    error = AttachmentError("x", "INVALID_IMAGE")
    assert is_attachment_error(error)
    other = type("E", (Exception,), {"code": "INVALID_IMAGE"})()
    assert is_attachment_error(other)
    assert not is_attachment_error(RuntimeError("x"))
    assert is_image_admission_error(error)
    assert not is_image_admission_error(AttachmentError("x", "ATTACHMENT_CORRUPT"))
    assert request_image_dimensions(10, 5, 100) == (10, 5)
    assert request_image_dimensions(10, 5, 25) == (6, 3)
    assert request_image_dimensions(5, 10, 25) == (3, 6)
    assert long_edge_dimensions(10, 5, 20) == (10, 5)
    assert long_edge_dimensions(10, 5, 4) == (4, 2)
    assert long_edge_dimensions(5, 10, 4) == (2, 4)


def test_canonical_base64() -> None:
    data = b"hello"
    encoded = base64.b64encode(data).decode()
    assert decode_canonical_base64(encoded, "INVALID_IMAGE_BASE64") == data
    assert decode_canonical_base64("", "INVALID_FILE_BASE64", "accept") == b""
    for bad in ("!!", encoded + "="):
        with pytest.raises(AttachmentError, match="canonical"):
            decode_canonical_base64(bad, "INVALID_IMAGE_BASE64")
    with pytest.raises(AttachmentError, match="canonical"):
        decode_canonical_base64("", "INVALID_IMAGE_BASE64")


def test_image_detection_and_alpha() -> None:
    detected = detect_image(_image())
    assert detected["mediaType"] == "image/png"
    assert (detected["width"], detected["height"]) == (8, 6)
    assert detected["depth"] == "uchar" and detected["space"] == "srgb"
    assert not detected["animated"] and not detected["hasAlpha"]
    assert probe_image(_image("JPEG"))["mediaType"] == "image/jpeg"
    assert detect_image(_image("JPEG"))["hasAlpha"] is False
    assert detect_image(_image("WEBP", alpha=True))["hasAlpha"] is True
    with pytest.raises(AttachmentError, match="pixel"):
        detect_image(_image(size=(20, 20)), {"maxPixels": 100, "maxDimension": 100})
    with pytest.raises(AttachmentError, match="per-side"):
        detect_image(_image(size=(20, 6)), {"maxPixels": 1000, "maxDimension": 10})
    with pytest.raises(AttachmentError, match="Unsupported"):
        detect_image(b"not an image")
    with pytest.raises(AttachmentError, match="Unsupported"):
        probe_image(b"not an image")
    assert encoded_alpha_is_compatible(
        None, {"hasAlpha": False, "mediaType": "image/jpeg"}
    )
    assert encoded_alpha_is_compatible(
        True, {"hasAlpha": True, "mediaType": "image/jpeg"}
    )
    assert encoded_alpha_is_compatible(
        True, {"hasAlpha": False, "mediaType": "image/webp"}
    )
    assert not encoded_alpha_is_compatible(
        True, {"hasAlpha": False, "mediaType": "image/jpeg"}
    )


def test_encoding_ladder() -> None:
    assert IMAGE_ENCODING_QUALITIES == (85, 75, 60)
    image = Image.open(io.BytesIO(_image()))
    attempts = encoding_ladder(image, False)
    assert len(attempts) == 3
    first = attempts[0]()
    assert first["mediaType"] == "image/jpeg" and first["data"]
    alpha = encoding_ladder(Image.open(io.BytesIO(_image(alpha=True))), True)[0]()
    assert alpha["mediaType"] == "image/webp"
    assert encode_first_within_limit(attempts, len(first["data"])) == first
    exhausted = encode_first_within_limit(attempts, 1)
    assert is_exhausted_encoding(exhausted)
    assert chosen(exhausted)["data"]
    assert not is_exhausted_encoding(first)
    assert chosen(first) == first
    with pytest.raises(ValueError, match="at least one"):
        encode_first_within_limit([], 10)
    smaller = encode(Image.open(io.BytesIO(_image())), "image/jpeg", 10)
    assert len(smaller["data"]) <= len(first["data"])
    result = encode_first_within_limit([attempts[0], lambda: smaller], 1)
    assert chosen(result)["data"] == smaller["data"]


def test_normalization() -> None:
    source = _input()
    detected = detect_image(source["data"])
    assert can_pass_through_normalization(detected, len(source["data"]), POLICY)
    assert not can_pass_through_normalization(detected, POLICY["maxBytes"] + 1, POLICY)
    assert not can_pass_through_normalization(detect_image(_image("GIF")), 1, POLICY)
    prepared = prepare_image_file(source, LIMITS, POLICY)
    assert prepared["ref"]["attachmentId"].startswith("sha256:")
    assert prepared["ref"]["name"] == "a.png"
    assert prepared["ref"]["bytes"] == len(prepared["data"])
    validate_image_file(source, LIMITS, POLICY)

    big = _input(size=(80, 60))
    changed = normalize_image(big["data"], detect_image(big["data"]), POLICY)
    assert changed["width"] <= 64 and changed["height"] <= 64
    assert initial_dimensions(detect_image(big["data"]), POLICY) == (64, 48)
    assert initial_dimensions(detected, {"maxPixels": 10, "maxDimension": 1}) == (1, 1)
    with pytest.raises(AttachmentError, match="byte limit"):
        prepare_image_file(
            {"data": b"x" * 51, "mediaType": "image/png"},
            {**LIMITS, "maxImageBytes": 50},
            POLICY,
        )
    with pytest.raises(AttachmentError, match="empty"):
        prepare_image_file({"data": b"", "mediaType": "image/png"}, LIMITS, POLICY)
    with pytest.raises(AttachmentError, match="does not match"):
        prepare_image_file(
            {"data": _image("PNG"), "mediaType": "image/jpeg"}, LIMITS, POLICY
        )
    assert display_name(None) is None
    assert display_name("a/../b.txt") == "b.txt"
    assert display_name("   ") is None
    assert display_name("x" * 300) == "x" * 255


def test_store_publish_and_read(tmp_path: Path) -> None:
    prepared = prepare_image_file(_input(), LIMITS, POLICY)
    ref = commit_prepared_image_file(str(tmp_path), prepared)
    assert ref["attachmentId"] == prepared["ref"]["attachmentId"]
    # Deduplicating an existing object succeeds.
    commit_prepared_image_file(str(tmp_path), prepared)
    stored = read_image_file(str(tmp_path), ref)
    assert stored["data"] == prepared["data"]
    assert normalized_image_path(str(tmp_path), ref).endswith(ref["attachmentId"][7:])
    assert ensure_reference(ref) == ref["attachmentId"][7:]
    with pytest.raises(AttachmentError, match="invalid"):
        ensure_reference({"attachmentId": "nope"})
    with pytest.raises(AttachmentError, match="missing"):
        read_image_file(str(tmp_path), {**ref, "attachmentId": "sha256:" + "0" * 64})
    with pytest.raises(AttachmentError, match="do not match"):
        commit_prepared_image_file(
            str(tmp_path),
            {"data": prepared["data"], "ref": {**ref, "bytes": 1}},
        )
    corrupt = Path(normalized_image_path(str(tmp_path), ref))
    original = corrupt.read_bytes()
    corrupt.chmod(0o600)
    corrupt.write_bytes(b"corrupt")
    with pytest.raises(AttachmentError, match="integrity"):
        read_image_file(str(tmp_path), ref)
    corrupt.write_bytes(original)
    corrupt.chmod(0o400)
    assert digest(original) == hashlib.sha256(original).hexdigest()


def test_file_store(tmp_path: Path) -> None:
    assert file_leaf_name(None) == "file"
    assert file_leaf_name("C:\\dir\\a:b*.txt") == "a_b_.txt"
    assert file_leaf_name("CON") == "_CON"
    assert file_leaf_name("../../..") == "file"
    assert file_leaf_name("name. ") == "name"
    ref = asyncio.run(
        save_file_verbatim(str(tmp_path), {"data": b"hello", "name": "a.txt"})
    )
    assert ref["bytes"] == 5 and ref["name"] == "a.txt"
    assert Path(stored_file_path(str(tmp_path), ref)).read_bytes() == b"hello"
    chunks: list[bytes] = []
    stream = read_file_stream_verbatim(str(tmp_path), ref)
    while True:
        try:
            chunks.append(asyncio.run(stream.__anext__()))
        except StopAsyncIteration:
            break
    assert b"".join(chunks) == b"hello"

    streamed = asyncio.run(
        save_file_stream_verbatim(
            str(tmp_path), {"data": [b"a", b"bc"], "name": "b.txt"}
        )
    )
    assert streamed["bytes"] == 3
    with pytest.raises(AttachmentError, match="invalid"):
        asyncio.run(
            read_file_stream_verbatim(
                str(tmp_path), {**ref, "name": "a:b.txt"}
            ).__anext__()
        )
    with pytest.raises(AttachmentError, match="missing"):
        asyncio.run(
            read_file_stream_verbatim(
                str(tmp_path), {**ref, "attachmentId": "sha256:" + "1" * 64}
            ).__anext__()
        )


def test_compression_limiter() -> None:
    limiter = CompressionLimiter(1)
    assert asyncio.run(limiter.run(lambda: asyncio.sleep(0, result=5))) == 5
    with pytest.raises(ZeroDivisionError):
        asyncio.run(limiter.run(lambda: (_ for _ in ()).throw(ZeroDivisionError())))
    with pytest.raises(ValueError, match="positive"):
        CompressionLimiter(0)
    assert isinstance(compression_failure("x"), RuntimeError)
    error = RuntimeError("x")
    assert compression_failure(error) is error


def test_request_images(tmp_path: Path) -> None:
    prepared = prepare_image_file(_input(size=(16, 12)), LIMITS, POLICY)
    stored = {"ref": prepared["ref"], "data": prepared["data"]}
    target = {
        "width": 8,
        "height": 6,
        "maxImageBytes": 1_000_000,
        "imageTargetBytes": 900_000,
    }
    variant = request_image_variant_id(prepared["ref"], target, RESIZE)
    assert variant.startswith("sha256:")
    result = read_request_image_file(str(tmp_path), stored, target, RESIZE)
    assert result["variantId"] == variant
    assert result["width"] <= 8 and result["height"] <= 6
    assert result["depth"] == "uchar" and result["space"] == "srgb"
    # Cache hit on the second call.
    assert (
        read_request_image_file(str(tmp_path), stored, target, RESIZE)["data"]
        == result["data"]
    )
    passthrough = {
        "width": 100,
        "height": 100,
        "maxImageBytes": 1_000_000,
        "imageTargetBytes": 1_000_000,
    }
    assert (
        read_request_image_file(str(tmp_path), stored, passthrough, RESIZE)["data"]
        == stored["data"]
    )
    with pytest.raises(AttachmentError, match="positive integer"):
        read_request_image_file(str(tmp_path), stored, {**target, "width": 0}, RESIZE)
    with pytest.raises(AttachmentError, match="must not exceed"):
        read_request_image_file(
            str(tmp_path),
            stored,
            {**target, "imageTargetBytes": 2_000_000},
            RESIZE,
        )


@pytest.mark.asyncio
async def test_service(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        normalizedImageMaxPixels=POLICY["maxPixels"],
        normalizedImageMaxDimension=POLICY["maxDimension"],
        normalizedImageMaxBytes=POLICY["maxBytes"],
    )
    assert store.image_limits["maxImageBytes"] == LIMITS["maxImageBytes"]
    assert store.normalization_policy["maxPixels"] == POLICY["maxPixels"]
    source = _input()
    await store.validate_image(source)
    ref = await store.save_image(source)
    assert ref["attachmentId"].startswith("sha256:")
    stored = await store.read_image(ref)
    assert stored["data"]
    assert store.image_host_path(ref).startswith(str(tmp_path))
    refs = await store.save_images([source, source])
    assert len(refs) == 2
    with pytest.raises(AttachmentError, match="count limit"):
        store.validate_image_batch([source] * 21)
    tiny = {"data": b"x" * 100, "mediaType": "image/png"}
    limited = _store(tmp_path, maxMessageImageBytes=150)
    with pytest.raises(AttachmentError, match="aggregate"):
        limited.validate_image_batch([tiny, tiny])

    file_ref = await store.save_file({"data": b"abc", "name": "a.txt"})
    assert await anext(store.read_file_stream(file_ref)) == b"abc"
    assert store.file_host_path(file_ref).endswith("a.txt")
    request = await store.read_image_request(
        ref,
        {"width": 8, "height": 6, "maxBytes": 1_000_000, "imageTargetBytes": 900_000},
    )
    assert request["bytes"] == len(request["data"])

    with pytest.raises(ValueError, match="Shrink"):
        _store(tmp_path, requestImageMinShrinkFactor=1)
    with pytest.raises(ValueError, match="Headroom"):
        _store(tmp_path, requestImageByteHeadroom=2)
    with pytest.raises(ValueError, match="Concurrency"):
        _store(tmp_path, imageCompressionConcurrency=9)


@pytest.mark.asyncio
async def test_base_store_defaults(tmp_path: Path) -> None:
    class Bare(AttachmentStore):
        @property
        def image_limits(self) -> Any:
            return LIMITS

        async def validate_image(self, payload: Mapping[str, Any]) -> None:
            del payload

    ctx = _ctx(tmp_path)
    store = Bare(ctx)
    with pytest.raises(AttachmentError, match="verbatim files"):
        await store.save_file({"data": b"x"})
    with pytest.raises(AttachmentError, match="stream verbatim"):
        await store.save_file_stream({"data": b"x"})
    with pytest.raises(AttachmentError, match="read verbatim"):
        await anext(
            store.read_file_stream({"attachmentId": "x", "name": "x", "bytes": 1})
        )
    with pytest.raises(AttachmentError, match="request images"):
        await store.read_image_request({}, {})
    assert store.image_host_path({}) is None and store.file_host_path({}) is None
    assert store.is_attachment_error(AttachmentError("x", "INVALID_IMAGE"))


@pytest.mark.asyncio
async def test_component_apply(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    await apply(ctx, dshHome=str(tmp_path))
    assert isinstance(ctx.attachments, LocalAttachmentStore)

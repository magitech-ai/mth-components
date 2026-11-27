"""Edge probes for attachment admission, normalization and request images."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import MappingProxyType
from typing import Any

import animas
import pytest
from PIL import Image

from mth_components.attachment.store import AttachmentStore, decode_canonical_base64
from mth_components.attachment.types import AttachmentError
from mth_components.attachment_local import image as image_module
from mth_components.attachment_local import normalization as norm_module
from mth_components.attachment_local import request_image as request_module
from mth_components.attachment_local import store as store_module
from mth_components.attachment_local.compression import compression_failure
from mth_components.attachment_local.file_store import (
    file_leaf_name,
    read_file_stream_verbatim,
    save_file_stream_verbatim,
    save_file_verbatim,
)
from mth_components.attachment_local.image import detect_image
from mth_components.attachment_local.normalization import (
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
    prepare_image_file,
)

LIMITS = {
    "maxImageBytes": 1000,
    "maxImagesPerMessage": 2,
    "maxMessageImageBytes": 100,
    "maxImagePixels": 1_000_000,
    "maxImageDimension": 1000,
    "mediaTypes": ("image/png", "image/jpeg", "image/webp", "image/gif"),
}
POLICY = {"maxPixels": 4096, "maxDimension": 64, "maxBytes": 100_000}
RESIZE = {"minShrinkFactor": 1.1, "byteHeadroom": 0.95}


def _png(size: tuple[int, int] = (8, 6), **meta: Any) -> bytes:
    image = Image.new("RGB", size, (1, 2, 3))
    image.info.update(meta)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_base_store_unimplemented_and_admission() -> None:
    store = AttachmentStore.__new__(AttachmentStore)
    with pytest.raises(NotImplementedError):
        asyncio.run(store.validate_image({}))
    with pytest.raises(NotImplementedError):
        asyncio.run(store.save_image({}))

    bmp = Image.new("RGB", (2, 2))
    buffer = io.BytesIO()
    bmp.save(buffer, format="BMP")
    with pytest.raises(AttachmentError, match="Unsupported"):
        detect_image(buffer.getvalue())
    with pytest.raises(AttachmentError, match="Unsupported"):
        prepare_image_file(
            {"data": buffer.getvalue(), "mediaType": "image/png"}, LIMITS, POLICY
        )

    animas_ctx = animas.Context()

    class Fake(AttachmentStore):
        def __init__(self) -> None:
            super().__init__(animas_ctx)
            self.image_limits = LIMITS
            self.saved: list[dict[str, Any]] = []
            self.files: list[dict[str, Any]] = []

        async def validate_image(self, payload: MappingProxyType) -> None:  # type: ignore[valid-type]
            del payload

        async def save_image(self, payload: MappingProxyType) -> Any:  # type: ignore[valid-type]
            self.saved.append(dict(payload))
            return {"attachmentId": "sha256:" + "0" * 64}

        async def save_file(self, payload: MappingProxyType) -> Any:  # type: ignore[valid-type]
            self.files.append(dict(payload))
            return {"attachmentId": "sha256:" + "1" * 64, "name": "f", "bytes": 1}

    fake = Fake()
    with pytest.raises(AttachmentError, match="not accepted"):
        fake.validate_image_batch([{"data": b"x", "mediaType": "image/bmp"}])
    asyncio.run(fake.save_images([{"data": b"x", "mediaType": "image/png"}]))
    assert len(fake.saved) == 1
    encoded = base64.b64encode(b"img").decode()
    asyncio.run(
        fake.admit_encoded_images(
            [{"data": encoded, "mediaType": "image/png", "name": "a"}]
        )
    )
    assert fake.saved[-1]["data"] == b"img"
    asyncio.run(fake.admit_encoded_file({"data": "", "name": "a"}))
    assert fake.files[-1]["data"] == b""

    content = [
        {"type": "text", "text": "hi"},
        {"type": "file", "attachment": {"attachmentId": "f"}},
        {"type": "image", "attachment": {"attachmentId": "s"}, "encodedBytes": 5},
        {"type": "image", "data": encoded, "mediaType": "image/png"},
    ]
    result = asyncio.run(fake.admit_prompt_content(content))
    assert [part["type"] for part in result] == ["text", "file", "image", "image"]
    assert result[3]["attachment"]["attachmentId"] == "sha256:" + "0" * 64
    with pytest.raises(AttachmentError, match="count limit"):
        asyncio.run(
            fake.admit_prompt_content(
                [
                    {
                        "type": "image",
                        "attachment": {"attachmentId": "s"},
                        "encodedBytes": 1,
                    }
                ]
                * 3
            )
        )


def test_decode_and_compression_edges() -> None:
    with pytest.raises(AttachmentError, match="canonical"):
        decode_canonical_base64("YQ", "INVALID_IMAGE_BASE64")
    assert compression_failure(42).__class__ is RuntimeError


def test_image_metadata_and_generic_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    exif_image = Image.new("RGB", (30, 10), (5, 6, 7))
    exif = exif_image.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    exif_image.save(buffer, format="JPEG", exif=exif)
    detected = detect_image(buffer.getvalue())
    assert (detected["width"], detected["height"]) == (10, 30)
    assert detected["carriesMetadata"]

    def bad_open(data: bytes) -> Any:
        del data
        raise ValueError("broken decode")

    monkeypatch.setattr(image_module, "_open", bad_open)
    with pytest.raises(AttachmentError, match="Unsupported"):
        image_module.probe_image(_png())
    with pytest.raises(AttachmentError, match="Unsupported"):
        image_module.detect_image(_png())


def test_normalization_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    detected = detect_image(_png())
    assert initial_dimensions(detected, {"maxPixels": 100, "maxDimension": 1}) == (1, 1)
    large_detected = detect_image(_png((16, 12)))
    monkeypatch.setattr(
        norm_module,
        "detect_image",
        lambda data: {**large_detected, "width": 1},
    )
    with pytest.raises(AttachmentError, match="did not produce"):
        normalize_image(_png((16, 12)), large_detected, {**POLICY, "maxBytes": 1})
    monkeypatch.undo()

    def broken_ladder(_image: Any, _alpha: bool) -> list[Any]:
        raise ValueError("encode failed")

    monkeypatch.setattr(norm_module, "encoding_ladder", broken_ladder)
    with pytest.raises(AttachmentError, match="could not be converted"):
        normalize_image(
            _png((16, 12)), detect_image(_png((16, 12))), {**POLICY, "maxBytes": 1}
        )


def test_store_publish_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_image_file(
        {"data": _png(), "mediaType": "image/png"}, LIMITS, POLICY
    )
    commit_prepared_image_file(str(tmp_path), prepared)
    target = Path(store_module.normalized_image_path(str(tmp_path), prepared["ref"]))
    target.chmod(0o600)
    target.write_bytes(b"bad")
    monkeypatch.setattr(
        store_module, "digest", lambda _data: prepared["ref"]["attachmentId"][7:]
    )
    monkeypatch.undo()

    def broken_mkdir(_self: Path, *_args: Any, **_kwargs: Any) -> None:
        raise OSError("read only")

    monkeypatch.setattr(store_module.Path, "mkdir", broken_mkdir)
    with pytest.raises(AttachmentError, match="persist"):
        commit_prepared_image_file(str(tmp_path), prepared)

    with pytest.raises(AttachmentError, match="do not match"):
        commit_prepared_image_file(
            str(tmp_path),
            {**prepared, "ref": {**prepared["ref"], "bytes": 999}},
        )


def test_file_store_edges(tmp_path: Path) -> None:
    assert file_leaf_name("é" * 200).encode("utf-8").__len__() <= 255
    assert file_leaf_name("  ...  ") == "file"
    ref = asyncio.run(save_file_verbatim(str(tmp_path), {"data": b"x", "name": "a"}))
    with pytest.raises(AttachmentError, match="missing"):
        asyncio.run(
            read_file_stream_verbatim(
                str(tmp_path), {**ref, "attachmentId": "sha256:" + "2" * 64}
            ).__anext__()
        )
    signal = type("S", (), {"throw_if_aborted": lambda self: None})()
    chunks: list[bytes] = []
    stream = read_file_stream_verbatim(str(tmp_path), ref, signal)
    while True:
        try:
            chunks.append(asyncio.run(stream.__anext__()))
        except StopAsyncIteration:
            break
    assert b"".join(chunks) == b"x"
    streamed = asyncio.run(
        save_file_stream_verbatim(
            str(tmp_path), {"data": (item for item in [b"a", b"b"]), "name": "s"}
        )
    )
    assert streamed["bytes"] == 2


def test_request_image_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_image_file(
        {"data": _png((16, 12)), "mediaType": "image/png"}, LIMITS, POLICY
    )
    stored = {"ref": prepared["ref"], "data": prepared["data"]}
    target = {
        "width": 8,
        "height": 6,
        "maxImageBytes": 1_000_000,
        "imageTargetBytes": 900_000,
    }
    assert request_image_variant_id(prepared["ref"], target, RESIZE).startswith(
        "sha256:"
    )
    with pytest.raises(AttachmentError, match="positive integer"):
        read_request_image_file(
            str(tmp_path), stored, {**target, "maxImageBytes": 0}, RESIZE
        )
    with pytest.raises(AttachmentError, match="positive integer"):
        read_request_image_file(
            str(tmp_path), stored, {**target, "imageTargetBytes": 0}, RESIZE
        )
    tiny = {"width": 2, "height": 2, "maxImageBytes": 1, "imageTargetBytes": 1}
    with pytest.raises(AttachmentError, match="1x1"):
        read_request_image_file(str(tmp_path), stored, tiny, RESIZE)
    with pytest.raises(asyncio.CancelledError):
        read_request_image_file(str(tmp_path), stored, target, RESIZE, lambda: True)
    # Cache hit with a corrupt file falls back to a fresh encode.
    variant = request_image_variant_id(prepared["ref"], target, RESIZE)
    path = request_module._cache_path(str(tmp_path), variant[len("sha256:") :])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"bad")
    assert read_request_image_file(str(tmp_path), stored, target, RESIZE)["data"]
    # A cached file above the hard byte limit is ignored.
    path.write_bytes(b"x" * (target["maxImageBytes"] + 1))
    assert read_request_image_file(str(tmp_path), stored, target, RESIZE)["data"]


@pytest.mark.asyncio
async def test_service_read_and_inflight(tmp_path: Path) -> None:
    store = LocalAttachmentStore(
        animas.Context(),
        {
            "dshHome": str(tmp_path),
            "normalizedImageMaxPixels": POLICY["maxPixels"],
            "normalizedImageMaxDimension": POLICY["maxDimension"],
            "normalizedImageMaxBytes": POLICY["maxBytes"],
        },
    )
    signal = type(
        "S",
        (),
        {
            "aborted": True,
            "throw_if_aborted": lambda self: (_ for _ in ()).throw(
                asyncio.CancelledError()
            ),
        },
    )()
    with pytest.raises(asyncio.CancelledError):
        await store.read_image({"attachmentId": "sha256:" + "0" * 64}, signal)
    with pytest.raises(asyncio.CancelledError):
        await store.read_image_request(
            {"attachmentId": "sha256:" + "0" * 64},
            {"width": 8, "height": 6, "maxBytes": 1000, "imageTargetBytes": 900},
            signal,
        )
    source = {"data": _png(), "mediaType": "image/png"}
    ref = await store.save_image(source)
    results = await asyncio.gather(
        *[
            store.read_image_request(
                ref,
                {
                    "width": 8,
                    "height": 6,
                    "maxBytes": 100000,
                    "imageTargetBytes": 90000,
                },
            )
            for _ in range(2)
        ]
    )
    assert results[0]["data"] == results[1]["data"]

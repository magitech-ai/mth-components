"""Final branch probes for the attachment backend."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from animas import Context
from PIL import Image

from mth_components.attachment import store as base_store
from mth_components.attachment.types import AttachmentError
from mth_components.attachment_local import encoding as enc_module
from mth_components.attachment_local import file_store as file_store_module
from mth_components.attachment_local import request_image as req_module
from mth_components.attachment_local import store as local_store
from mth_components.attachment_local.compression import CompressionLimiter
from mth_components.attachment_local.file_store import (
    read_file_stream_verbatim,
    save_file_stream_verbatim,
    save_file_verbatim,
)
from mth_components.attachment_local.request_image import read_request_image_file
from mth_components.attachment_local.service import LocalAttachmentStore
from mth_components.attachment_local.store import (
    commit_prepared_image_file,
    prepare_image_file,
    read_image_file,
)

LIMITS = {
    "maxImageBytes": 100000,
    "maxImagesPerMessage": 20,
    "maxMessageImageBytes": 1000000,
    "maxImagePixels": 1000000,
    "maxImageDimension": 1000,
    "mediaTypes": ("image/png", "image/jpeg", "image/webp", "image/gif"),
}
POLICY = {"maxPixels": 4096, "maxDimension": 64, "maxBytes": 100000}
RESIZE = {"minShrinkFactor": 1.1, "byteHeadroom": 0.95}


def _png(size: tuple[int, int] = (8, 6)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (4, 5, 6)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_base_store_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base_store.base64, "b64encode", lambda _data: b"different")
    with pytest.raises(base_store.AttachmentError, match="canonical"):
        base_store.decode_canonical_base64("YQ==", "INVALID_FILE_BASE64")
    monkeypatch.undo()

    class Fake(base_store.AttachmentStore):
        def __init__(self) -> None:
            super().__init__(Context())
            self.image_limits = LIMITS

        async def validate_image(self, payload: Any) -> None:
            del payload

        async def save_image(self, payload: Any) -> Any:
            del payload
            return {"attachmentId": "sha256:" + "0" * 64}

    encoded = "YQ=="
    result = asyncio.run(
        Fake().admit_prompt_content(
            [
                {"type": "image", "data": encoded, "mediaType": "image/png"},
                {"type": "text", "text": "done"},
            ]
        )
    )
    assert result[1]["text"] == "done"


def test_encoding_and_compression_remaining() -> None:
    big = enc_module.encode(Image.new("RGB", (40, 30)), "image/jpeg", 95)
    small = enc_module.encode(Image.new("RGB", (40, 30)), "image/jpeg", 5)
    result = enc_module.encode_first_within_limit(
        [lambda: big, lambda: small], len(small["data"])
    )
    assert result == small
    with pytest.raises(ZeroDivisionError):
        asyncio.run(
            CompressionLimiter(1).run(
                lambda: (_ for _ in ()).throw(ZeroDivisionError())
            )
        )


def test_file_store_remaining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signal = type("S", (), {"throw_if_aborted": lambda self: None})()
    streamed = asyncio.run(
        save_file_stream_verbatim(
            str(tmp_path), {"data": [b"a"], "name": "a", "signal": signal}
        )
    )
    assert streamed["bytes"] == 1

    def broken_open(self: Path, *_args: Any, **_kwargs: Any) -> Any:
        raise OSError("denied")

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(Exception, match="read"):
        asyncio.run(read_file_stream_verbatim(str(tmp_path), streamed).__anext__())
    monkeypatch.undo()

    ref = asyncio.run(save_file_verbatim(str(tmp_path), {"data": b"abc", "name": "a"}))
    wrong = {**ref, "bytes": 99}
    with pytest.raises(AttachmentError, match="integrity"):
        asyncio.run(_consume(read_file_stream_verbatim(str(tmp_path), wrong)))


async def _consume(stream: Any) -> None:
    async for _ in stream:
        pass


def test_request_image_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_image_file(
        {"data": _png((16, 12)), "mediaType": "image/png"}, LIMITS, POLICY
    )
    stored = {"ref": prepared["ref"], "data": prepared["data"]}
    wide = {
        "width": 100,
        "height": 100,
        "maxImageBytes": 100000,
        "imageTargetBytes": 1,
    }
    assert read_request_image_file(str(tmp_path), stored, wide, RESIZE)["data"]

    counter = {"n": 0}

    def delayed_cancel() -> bool:
        counter["n"] += 1
        return counter["n"] >= 4

    with pytest.raises(asyncio.CancelledError):
        read_request_image_file(str(tmp_path), stored, wide, RESIZE, lambda: True)

    target = {
        "width": 8,
        "height": 6,
        "maxImageBytes": 100000,
        "imageTargetBytes": 90000,
    }
    variant = req_module.request_image_variant_id(prepared["ref"], target, RESIZE)
    path = req_module._cache_path(str(tmp_path), variant[len("sha256:") :])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png((200, 200)))
    assert read_request_image_file(str(tmp_path), stored, target, RESIZE)["data"]
    path.write_bytes(b"bad")
    with pytest.raises(asyncio.CancelledError):
        read_request_image_file(str(tmp_path), stored, target, RESIZE, lambda: True)

    path.unlink()
    original_detect = req_module.detect_image
    monkeypatch.setattr(
        req_module,
        "detect_image",
        lambda data: {**original_detect(data), "width": 1},
    )
    with pytest.raises(AttachmentError, match="verified"):
        read_request_image_file(str(tmp_path), stored, target, RESIZE)


@pytest.mark.asyncio
async def test_store_and_service_remaining(tmp_path: Path) -> None:
    big = prepare_image_file(
        {"data": _png((80, 60)), "mediaType": "image/png"}, LIMITS, POLICY
    )
    assert "originalDimensions" in big["ref"]
    commit_prepared_image_file(str(tmp_path), big)

    # EEXIST with mismatched stored bytes is rejected.

    target = Path(local_store.normalized_image_path(str(tmp_path), big["ref"]))
    target.chmod(0o600)
    target.write_bytes(b"bad")

    def exists(_src: Any, _dst: Any) -> None:
        raise FileExistsError()

    original_link = local_store.os.link
    local_store.os.link = exists  # type: ignore[assignment]
    try:
        with pytest.raises(Exception, match="integrity"):
            commit_prepared_image_file(str(tmp_path), big)
    finally:
        local_store.os.link = original_link  # type: ignore[assignment]

    target.write_bytes(big["data"])
    target.chmod(0o400)

    with pytest.raises(Exception, match="metadata"):
        read_image_file(str(tmp_path), {**big["ref"], "width": 1})

    store = LocalAttachmentStore(
        Context(),
        {
            "dshHome": str(tmp_path),
            "normalizedImageMaxPixels": POLICY["maxPixels"],
            "normalizedImageMaxDimension": POLICY["maxDimension"],
            "normalizedImageMaxBytes": POLICY["maxBytes"],
        },
    )
    file_ref = await store.save_file_stream({"data": [b"x"], "name": "a"})
    assert file_ref["bytes"] == 1

    bad_ref = {"attachmentId": "sha256:" + "9" * 64}
    task = store.read_image_request(
        bad_ref,
        {"width": 8, "height": 6, "maxBytes": 100000, "imageTargetBytes": 90000},
    )
    with pytest.raises(AttachmentError):
        await asyncio.gather(task, task)


def test_final_branch_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_image_file(
        {"data": _png((16, 12)), "mediaType": "image/png"}, LIMITS, POLICY
    )
    stored = {"ref": prepared["ref"], "data": prepared["data"]}
    target = {
        "width": 8,
        "height": 6,
        "maxImageBytes": 100000,
        "imageTargetBytes": 90000,
    }

    # Cancellation inside the encode loop and inside the cached-read failure path.
    with pytest.raises(asyncio.CancelledError):
        req_module._create_request_image(stored, target, False, RESIZE, lambda: True)
    calls = {"n": 0}

    def cancel_second() -> bool:
        calls["n"] += 1
        return calls["n"] == 2

    variant = req_module.request_image_variant_id(prepared["ref"], target, RESIZE)
    path = req_module._cache_path(str(tmp_path), variant[len("sha256:") :])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"bad")
    with pytest.raises(asyncio.CancelledError):
        read_request_image_file(str(tmp_path), stored, target, RESIZE, cancel_second)

    # Chmod and read failures are converted to stable storage errors.
    monkeypatch.setattr(
        local_store.Path,
        "chmod",
        lambda _self, _mode: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(AttachmentError, match="persist"):
        commit_prepared_image_file(str(tmp_path), prepared)
    monkeypatch.undo()

    monkeypatch.setattr(
        local_store.Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(AttachmentError, match="read"):
        read_image_file(str(tmp_path), prepared["ref"])
    monkeypatch.undo()

    # A chunk-read failure during a file stream is wrapped.
    ref = asyncio.run(save_file_verbatim(str(tmp_path), {"data": b"abc", "name": "a"}))

    class BrokenHandle:
        def read(self, _size: int) -> bytes:
            raise OSError("denied")

        def close(self) -> None:
            return None

    monkeypatch.setattr(Path, "open", lambda self, *_args, **_kwargs: BrokenHandle())
    with pytest.raises(AttachmentError, match="read"):
        asyncio.run(_consume(read_file_stream_verbatim(str(tmp_path), ref)))


def test_derived_space_and_config_validation(tmp_path: Path) -> None:
    gray = io.BytesIO()
    Image.new("L", (8, 6), 128).save(gray, format="PNG")
    assert req_module.probe_image(gray.getvalue())["space"] == "b-w"

    deep = io.BytesIO()
    Image.new("I;16", (8, 6), 128).save(deep, format="PNG")
    assert req_module.probe_image(deep.getvalue())["space"] == "grey16"

    for key in (
        "maxImageBytes",
        "normalizedImageMaxPixels",
        "imageCompressionConcurrency",
    ):
        with pytest.raises(ValueError, match=key):
            LocalAttachmentStore(Context(), {"dshHome": str(tmp_path), key: 0})
    with pytest.raises(ValueError, match="maxImageBytes"):
        LocalAttachmentStore(
            Context(), {"dshHome": str(tmp_path), "maxImageBytes": "5"}
        )


def test_publish_stream_and_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_image_file(
        {"data": _png(), "mediaType": "image/png"}, LIMITS, POLICY
    )
    sha = prepared["ref"]["attachmentId"][7:]

    def target_for(digest_value: str, _bytes: int) -> Path:
        return Path(str(tmp_path), "file-objects", digest_value[:2], digest_value)

    result_sha, total = local_store.publish_stream(
        str(tmp_path), iter([prepared["data"]]), target_for
    )
    assert result_sha == sha and total == len(prepared["data"])

    # EEXIST with mismatched bytes is rejected, and alias failures are wrapped.
    target = target_for(sha, total)
    target.chmod(0o600)
    target.write_bytes(b"bad")
    with pytest.raises(AttachmentError, match="integrity"):
        local_store.publish_stream(str(tmp_path), iter([prepared["data"]]), target_for)
    target.chmod(0o400)

    def bad_link(_source: Any, _target: Any) -> None:
        raise OSError("denied")

    monkeypatch.setattr(local_store.os, "link", bad_link)
    with pytest.raises(AttachmentError, match="persist"):
        local_store.publish_alias(
            str(tmp_path), target, Path(str(tmp_path), "alias"), sha
        )
    monkeypatch.undo()

    # A non-POSIX host skips directory fsync.
    monkeypatch.setattr(local_store.os, "name", "nt")
    local_store._sync_directory(tmp_path)


def test_stream_cancellation_and_storage_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Signal:
        def __init__(self) -> None:
            self.aborted = False

        def throw_if_aborted(self) -> None:
            if self.aborted:
                raise asyncio.CancelledError()

    # Async stream source plus a cancellation raised while reading a chunk.
    async def source() -> Any:
        yield b"a"
        yield b"b"

    signal = Signal()
    signal.aborted = True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            save_file_stream_verbatim(
                str(tmp_path), {"data": source(), "signal": signal}
            )
        )

    signal.aborted = False
    reference = asyncio.run(
        save_file_stream_verbatim(str(tmp_path), {"data": source()})
    )
    assert reference["bytes"] == 2

    # EEXIST with identical bytes is a valid deduplication.
    def target_for(digest_value: str, _bytes: int) -> Path:
        return Path(str(tmp_path), "objects", digest_value[:2], digest_value)

    data = b"dedup"
    sha = local_store.publish_stream(str(tmp_path), iter([data]), target_for)[0]
    assert local_store.publish_stream(str(tmp_path), iter([data]), target_for)[0] == sha

    # Cancellation inside and after a streamed publish.
    signal.aborted = True
    with pytest.raises(asyncio.CancelledError):
        local_store.publish_stream(str(tmp_path), iter([data]), target_for, signal)
    signal.aborted = False

    def broken_write(self: Any, chunk: bytes) -> None:
        raise OSError("denied")

    monkeypatch.setattr(local_store.Path, "mkdir", lambda *_a, **_k: None)
    monkeypatch.setattr(local_store.Path, "unlink", lambda *_a, **_k: None)
    monkeypatch.setattr(local_store.os, "open", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        local_store.os,
        "fdopen",
        lambda *_a, **_k: type(
            "H",
            (),
            {
                "write": broken_write,
                "flush": lambda self: None,
                "fileno": lambda self: 1,
                "__enter__": lambda self: self,
                "__exit__": lambda self, *_a: None,
            },
        )(),
    )
    with pytest.raises(AttachmentError, match="persist"):
        local_store.publish_stream(str(tmp_path), iter([data]), target_for)


def test_final_cancellation_and_sync_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Signal:
        def __init__(self, trigger: int) -> None:
            self.aborted = False
            self.trigger = trigger
            self.count = 0

        def throw_if_aborted(self) -> None:
            self.count += 1
            if self.count >= self.trigger:
                self.aborted = True
            if self.aborted:
                raise asyncio.CancelledError()

    async def source() -> Any:
        yield b"a"

    late = Signal(99)

    async def late_source() -> Any:
        yield b"a"
        late.aborted = True

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            save_file_stream_verbatim(
                str(tmp_path), {"data": late_source(), "signal": late}
            )
        )

    def alias_then_abort(*_args: Any, **_kwargs: Any) -> None:
        signal.aborted = True

    signal = Signal(99)
    original_alias = file_store_module.publish_alias
    monkeypatch.setattr(file_store_module, "publish_alias", alias_then_abort)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            save_file_stream_verbatim(
                str(tmp_path), {"data": source(), "signal": signal}
            )
        )
    monkeypatch.undo()
    del original_alias

    monkeypatch.setattr(
        local_store.os, "open", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    local_store._sync_directory(tmp_path)
    monkeypatch.undo()

    def fail_finish(*_args: Any, **_kwargs: Any) -> None:
        raise AttachmentError("corrupt", "ATTACHMENT_CORRUPT")

    monkeypatch.setattr(local_store, "_finish_object", fail_finish)
    with pytest.raises(AttachmentError, match="corrupt"):
        local_store.publish_alias(str(tmp_path), tmp_path, tmp_path, "sha")
    monkeypatch.undo()

    def target_for(digest_value: str, _bytes: int) -> Path:
        return Path(str(tmp_path), "objects2", digest_value[:2], digest_value)

    class Alternating:
        def __init__(self) -> None:
            self.count = 0

        @property
        def aborted(self) -> bool:
            self.count += 1
            return self.count > 1

        def throw_if_aborted(self) -> None:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        local_store.publish_stream(
            str(tmp_path), iter([b"x"]), target_for, Alternating()
        )

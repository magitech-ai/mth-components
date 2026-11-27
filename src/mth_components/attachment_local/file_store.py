"""Verbatim content-addressed local file storage.

Ports ``packages/attachment/attachment-local/src/file-store.ts``: a display
name sanitized into a cross-platform leaf, one canonical object per digest and
a digest-verified bounded read stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from mth_components.attachment.types import AttachmentError, FileAttachmentRef
from mth_components.attachment_local.store import (
    _publish,
    digest,
    publish_alias,
)

FILE_ID_PATTERN = re.compile(r"^sha256:([a-f0-9]{64})$")
WINDOWS_DEVICE_NAME = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE
)


def _is_windows_device_name(name: str) -> bool:
    dot = name.find(".")
    stem = (name if dot < 0 else name[:dot]).rstrip(". ")
    return bool(WINDOWS_DEVICE_NAME.fullmatch(stem))


def _utf8_prefix(value: str, max_bytes: int) -> str:
    result = ""
    total = 0
    for character in value:
        size = len(character.encode("utf-8"))
        if total + size > max_bytes:
            break
        result += character
        total += size
    return result


def file_leaf_name(value: str | None) -> str:
    """Sanitize a caller name into a safe stored leaf on every host."""
    if value is None:
        return "file"
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1 :]
    clean = re.sub(r"[\x00-\x1f\x7f]", "", leaf)
    clean = re.sub(r'[<>:"|?*]', "_", clean).strip().rstrip(". ")
    if _is_windows_device_name(clean):
        clean = f"_{clean}"
    clean = _utf8_prefix(clean, 255).rstrip(". ")
    if clean in ("", ".", ".."):
        return "file"
    return clean


def _ensure_file_reference(ref: FileAttachmentRef) -> str:
    match = FILE_ID_PATTERN.fullmatch(str(ref["attachmentId"]))
    if match is None or ref["name"] != file_leaf_name(ref["name"]):
        raise AttachmentError(
            "File attachment reference is invalid.", "INVALID_ATTACHMENT_REF"
        )
    return match.group(1)


def stored_file_path(root: str, ref: FileAttachmentRef) -> str:
    sha = _ensure_file_reference(ref)
    return str(Path(root, "files", sha[:2], sha, ref["name"]))


def _stored_file_object_path(root: str, sha: str) -> Path:
    return Path(root, "file-objects", sha[:2], sha)


async def save_file_verbatim(
    root: str, payload: Mapping[str, Any]
) -> FileAttachmentRef:
    """Commit exact file bytes at their digest-derived object and alias."""
    data = payload["data"]
    sha = digest(data)
    ref: FileAttachmentRef = {
        "attachmentId": f"sha256:{sha}",
        "name": file_leaf_name(payload.get("name")),
        "bytes": len(data),
    }
    object_path = _stored_file_object_path(root, sha)
    _publish(root, object_path, data, sha)
    publish_alias(root, object_path, Path(stored_file_path(root, ref)), sha)
    return ref


async def save_file_stream_verbatim(
    root: str, payload: Mapping[str, Any]
) -> FileAttachmentRef:
    """Commit streamed file bytes without retaining the complete sequence."""
    stream = payload["data"]
    signal = payload.get("signal")
    name = file_leaf_name(payload.get("name"))

    async def chunks() -> Any:
        if hasattr(stream, "__aiter__"):
            async for chunk in stream:
                yield bytes(chunk)
        else:
            for item in stream:
                yield bytes(item)

    await asyncio.to_thread(
        lambda: Path(root, "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
    )
    temporary = Path(root, "tmp", str(uuid.uuid4()))
    hasher = hashlib.sha256()
    total = 0
    try:
        descriptor = await asyncio.to_thread(
            lambda: os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        )
        handle = await asyncio.to_thread(lambda: os.fdopen(descriptor, "wb"))
        try:
            async for chunk in chunks():
                if signal is not None and getattr(signal, "aborted", False):
                    signal.throw_if_aborted()
                await asyncio.to_thread(handle.write, chunk)
                hasher.update(chunk)
                total += len(chunk)
            await asyncio.to_thread(handle.flush)
            await asyncio.to_thread(os.fsync, handle.fileno())
        finally:
            await asyncio.to_thread(handle.close)
        if signal is not None and getattr(signal, "aborted", False):
            signal.throw_if_aborted()
        sha = hasher.hexdigest()
        ref: FileAttachmentRef = {
            "attachmentId": f"sha256:{sha}",
            "name": name,
            "bytes": total,
        }
        object_path = _stored_file_object_path(root, sha)
        await asyncio.to_thread(publish_alias, root, temporary, object_path, sha)
        await asyncio.to_thread(
            publish_alias,
            root,
            object_path,
            Path(stored_file_path(root, ref)),
            sha,
        )
        if signal is not None and getattr(signal, "aborted", False):
            signal.throw_if_aborted()
        return ref
    finally:
        temporary.unlink(missing_ok=True)


async def read_file_stream_verbatim(
    root: str, ref: FileAttachmentRef, signal: Any = None
) -> AsyncIterator[bytes]:
    """Yield stored bytes in bounded chunks, verifying count and digest."""
    if hasattr(signal, "throw_if_aborted"):
        signal.throw_if_aborted()
    sha = _ensure_file_reference(ref)
    path = Path(stored_file_path(root, ref))
    try:
        handle = path.open("rb")
    except FileNotFoundError as error:
        raise AttachmentError(
            "File attachment object is missing.", "ATTACHMENT_NOT_FOUND", error
        ) from error
    except OSError as error:
        raise AttachmentError(
            "Unable to read file attachment.", "ATTACHMENT_READ_FAILED", error
        ) from error
    total = 0
    hasher = hashlib.sha256()
    try:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            if hasattr(signal, "throw_if_aborted"):
                signal.throw_if_aborted()
            hasher.update(chunk)
            total += len(chunk)
            yield chunk
    except OSError as error:
        raise AttachmentError(
            "Unable to read file attachment.", "ATTACHMENT_READ_FAILED", error
        ) from error
    finally:
        handle.close()
    if hasattr(signal, "throw_if_aborted"):
        signal.throw_if_aborted()
    if total != ref["bytes"] or hasher.hexdigest() != sha:
        raise AttachmentError(
            "Stored file attachment failed integrity verification.",
            "ATTACHMENT_CORRUPT",
        )


__all__ = [
    "file_leaf_name",
    "read_file_stream_verbatim",
    "save_file_stream_verbatim",
    "save_file_verbatim",
    "stored_file_path",
]

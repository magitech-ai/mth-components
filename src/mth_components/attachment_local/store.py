"""Content-addressed durable local image objects.

Ports ``packages/attachment/attachment-local/src/store.ts``: staged writes,
digest verification, EEXIST deduplication, read-only immutable objects, and
header-only verification on reads of already-admitted bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypedDict

from mth_components.attachment.types import (
    AttachmentError,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    StoredImageAttachment,
)
from mth_components.attachment_local.image import detect_image, probe_image
from mth_components.attachment_local.normalization import (
    NormalizationPolicy,
    normalize_image,
)

ID_PATTERN = re.compile(r"^sha256:([a-f0-9]{64})$")


class PreparedImageFile(TypedDict):
    data: bytes
    ref: ImageAttachmentRef


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display_name(value: str | None) -> str | None:
    """Strip local path information and unsafe characters from a display name."""
    if value is None:
        return None
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1 :]
    clean = re.sub(r"[\x00-\x1f\x7f]", "", leaf).strip()[:255]
    return clean or None


def ensure_reference(ref: Any) -> str:
    match = ID_PATTERN.fullmatch(str(ref.get("attachmentId", "")))
    if match is None:
        raise AttachmentError(
            "Attachment reference is invalid.", "INVALID_ATTACHMENT_REF"
        )
    return match.group(1)


def normalized_image_path(root: str, ref: ImageAttachmentRef) -> str:
    sha = ensure_reference(ref)
    return str(Path(root, "objects", sha[:2], sha))


def prepare_image_file(
    payload: Any, limits: ImageAttachmentLimits, policy: NormalizationPolicy
) -> PreparedImageFile:
    """Decode, normalize and verify one submitted image without writing it."""
    data = payload["data"]
    if len(data) > limits["maxImageBytes"]:
        raise AttachmentError(
            "Image exceeds the configured byte limit.", "IMAGE_TOO_LARGE"
        )
    if len(data) == 0:
        raise AttachmentError("Image is empty.", "INVALID_IMAGE")
    detected = detect_image(
        data,
        {
            "maxPixels": limits["maxImagePixels"],
            "maxDimension": limits["maxImageDimension"],
        },
    )
    if detected["mediaType"] != payload["mediaType"]:
        raise AttachmentError(
            "Declared image type does not match its bytes.", "IMAGE_TYPE_MISMATCH"
        )
    normalized = normalize_image(data, detected, policy)
    sha = digest(normalized["data"])
    name = display_name(payload.get("name"))
    ref: ImageAttachmentRef = {
        "attachmentId": f"sha256:{sha}",
        "mediaType": normalized["mediaType"],
        "width": normalized["width"],
        "height": normalized["height"],
        "bytes": len(normalized["data"]),
    }
    if name is not None:
        ref["name"] = name
    if (
        detected["width"] != normalized["width"]
        or detected["height"] != normalized["height"]
    ):
        ref["originalDimensions"] = {
            "width": detected["width"],
            "height": detected["height"],
        }
    return {"data": normalized["data"], "ref": ref}


def validate_image_file(
    payload: Any, limits: ImageAttachmentLimits, policy: NormalizationPolicy
) -> None:
    """Run full admission, including normalization, without touching storage."""
    prepare_image_file(payload, limits, policy)


def _publish(root: str, target: Path, data: bytes, sha: str) -> None:
    staging = Path(root, "tmp")
    temporary = staging / str(uuid.uuid4())
    try:
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if digest(target.read_bytes()) != sha:
                raise AttachmentError(
                    "Stored attachment failed integrity verification.",
                    "ATTACHMENT_CORRUPT",
                ) from None
        try:
            target.chmod(0o400)
        except OSError as error:
            raise AttachmentError(
                "Unable to persist attachment.", "ATTACHMENT_WRITE_FAILED", error
            ) from error
        _sync_directory(target.parent)
        _sync_directory(Path(root))
    except AttachmentError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AttachmentError(
            "Unable to persist attachment.", "ATTACHMENT_WRITE_FAILED", error
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    """Persist a directory entry; Windows journaling owns entry durability."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return


def _finish_object(root: str, target: Path, sha: str, temporary: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if digest(target.read_bytes()) != sha:
            raise AttachmentError(
                "Stored attachment failed integrity verification.",
                "ATTACHMENT_CORRUPT",
            ) from None
    target.chmod(0o400)
    _sync_directory(target.parent)
    _sync_directory(Path(root))


def publish_alias(root: str, source: Path, target: Path, sha: str) -> None:
    """Publish another durable hard-link name for an existing object."""
    try:
        _finish_object(root, target, sha, source)
    except AttachmentError:
        raise
    except OSError as error:
        raise AttachmentError(
            "Unable to persist attachment.", "ATTACHMENT_WRITE_FAILED", error
        ) from error


def publish_stream(
    root: str,
    chunks: Iterable[bytes],
    target_for: Callable[[str, int], Path],
    signal: Any = None,
) -> tuple[str, int]:
    """Stream bounded chunks into staging, then publish at the digest target."""
    staging = Path(root, "tmp")
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = staging / str(uuid.uuid4())
    hasher = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in chunks:
                if signal is not None and signal.aborted:
                    signal.throw_if_aborted()
                handle.write(chunk)
                hasher.update(chunk)
                total += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if signal is not None and signal.aborted:
            signal.throw_if_aborted()
        sha = hasher.hexdigest()
        _finish_object(root, target_for(sha, total), sha, temporary)
        return sha, total
    except AttachmentError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AttachmentError(
            "Unable to persist attachment.", "ATTACHMENT_WRITE_FAILED", error
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def commit_prepared_image_file(
    root: str, prepared: PreparedImageFile
) -> ImageAttachmentRef:
    """Publish one verified normalized object at its digest-derived path."""
    sha = ensure_reference(prepared["ref"])
    data = prepared["data"]
    if digest(data) != sha or len(data) != prepared["ref"]["bytes"]:
        raise AttachmentError(
            "Prepared attachment bytes do not match their reference.",
            "ATTACHMENT_CORRUPT",
        )
    _publish(root, Path(normalized_image_path(root, prepared["ref"])), data, sha)
    return prepared["ref"]


def save_image_file(
    root: str, payload: Any, limits: ImageAttachmentLimits, policy: NormalizationPolicy
) -> ImageAttachmentRef:
    return commit_prepared_image_file(root, prepare_image_file(payload, limits, policy))


def read_image_file(root: str, ref: ImageAttachmentRef) -> StoredImageAttachment:
    """Read and verify one content-addressed image with a header-only probe."""
    sha = ensure_reference(ref)
    path = Path(normalized_image_path(root, ref))
    try:
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise AttachmentError(
            "Attachment object is missing.", "ATTACHMENT_NOT_FOUND", error
        ) from error
    except OSError as error:
        raise AttachmentError(
            "Unable to read image attachment.", "ATTACHMENT_READ_FAILED", error
        ) from error
    if digest(data) != sha:
        raise AttachmentError(
            "Stored attachment failed integrity verification.", "ATTACHMENT_CORRUPT"
        )
    metadata = probe_image(data)
    if (
        metadata["mediaType"] != ref["mediaType"]
        or len(data) != ref["bytes"]
        or metadata["width"] != ref["width"]
        or metadata["height"] != ref["height"]
    ):
        raise AttachmentError(
            "Stored attachment metadata does not match its reference.",
            "ATTACHMENT_CORRUPT",
        )
    return {"ref": ref, "data": data}


__all__ = [
    "ID_PATTERN",
    "PreparedImageFile",
    "commit_prepared_image_file",
    "digest",
    "display_name",
    "ensure_reference",
    "normalized_image_path",
    "prepare_image_file",
    "read_image_file",
    "save_image_file",
    "validate_image_file",
]

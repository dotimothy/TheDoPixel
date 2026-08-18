from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO


def is_macos_metadata(path: str | Path) -> bool:
    """Identify AppleDouble sidecar files and directories created by macOS."""
    return any(part.startswith("._") for part in Path(path).parts)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_inside(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    resolved_root = root.expanduser().resolve(strict=True)
    resolved = path.expanduser().resolve(strict=must_exist)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Path is outside the source root: {path}")
    return resolved


def atomic_upload(
    handle: BinaryIO,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            while chunk := handle.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError("Uploaded file exceeds the configured size limit")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()

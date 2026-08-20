from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import BinaryIO


def normalize_local_path_text(path: str | os.PathLike[str], *, windows: bool | None = None) -> str:
    """Normalize browser-style drive paths before passing them to pathlib."""
    value = os.fspath(path)
    use_windows_rules = os.name == "nt" if windows is None else windows
    if use_windows_rules and re.match(r"^/[A-Za-z]:[/\\]", value):
        return value[1:]
    return value


def local_path(path: str | os.PathLike[str]) -> Path:
    return Path(normalize_local_path_text(path))


def source_path_name(path: str | os.PathLike[str]) -> str:
    """Return a basename for paths produced by either supported host OS."""
    parts = [part for part in re.split(r"[/\\]+", os.fspath(path).rstrip("/\\")) if part]
    return parts[-1] if parts else ""


def is_macos_metadata(path: str | Path) -> bool:
    """Identify AppleDouble sidecar files and directories created by macOS."""
    return any(part.startswith("._") for part in re.split(r"[/\\]+", os.fspath(path)))


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

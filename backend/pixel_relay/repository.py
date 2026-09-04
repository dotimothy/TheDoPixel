from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database, utcnow
from .files import is_macos_metadata, local_path, resolve_inside, sha256_file, source_path_name
from .states import ItemState, can_transition

logger = logging.getLogger(__name__)


class DomainError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def relay_filename(source_path: str, item_id: str, used_names: set[str]) -> str:
    """Keep the source basename while remaining safe on both supported transports."""
    original_name = source_path_name(source_path)

    # Browser uploads are stored with an internal collision-prevention prefix.
    # That prefix is an implementation detail and must not reach Google Photos.
    parent_parts = [part for part in re.split(r"[/\\]+", source_path) if part]
    if len(parent_parts) > 1 and parent_parts[-2] == "pixel-relay-imports":
        uploaded_name = re.fullmatch(r"[a-fA-F0-9]{32}-(.+)", original_name)
        if uploaded_name:
            original_name = uploaded_name.group(1)

    safe_name = re.sub(r"[\x00-\x1f\x7f/]+", "_", original_name)
    if safe_name in {"", ".", ".."}:
        safe_name = f"media-{item_id[:8]}"

    # Android shared storage typically limits one filename component to 255 bytes.
    suffix = Path(safe_name).suffix
    stem = safe_name[: -len(suffix)] if suffix else safe_name

    def fit(value: str, byte_limit: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= byte_limit:
            return value
        return encoded[:byte_limit].decode("utf-8", errors="ignore")

    max_stem_bytes = max(1, 255 - len(suffix.encode("utf-8")))
    safe_name = f"{fit(stem, max_stem_bytes)}{suffix}"

    collision_key = safe_name.casefold()
    if collision_key in used_names:
        unique_suffix = f"--{item_id}"
        max_stem_bytes = max(
            1,
            255 - len(suffix.encode("utf-8")) - len(unique_suffix.encode("utf-8")),
        )
        safe_name = f"{fit(stem, max_stem_bytes)}{unique_suffix}{suffix}"
        collision_key = safe_name.casefold()
    used_names.add(collision_key)
    return safe_name


def split_batch_files(
    files: Iterable[dict[str, Any]],
    *,
    max_files: int,
    max_bytes: int,
) -> list[list[dict[str, Any]]]:
    if max_files < 1 or max_bytes < 1:
        raise DomainError(
            "invalid_batch_limit",
            "Batch file and storage limits must be greater than zero",
        )
    ordered = list(files)
    if not ordered:
        return []
    for file in ordered:
        if file["size"] > max_bytes:
            raise DomainError(
                "batch_too_large",
                f"{source_path_name(file['path'])} exceeds the available per-batch storage",
            )

    minimum_batches = max(
        1,
        ceil(len(ordered) / max_files),
        ceil(sum(file["size"] for file in ordered) / max_bytes),
    )
    indexed = list(enumerate(ordered))
    largest_first = sorted(indexed, key=lambda value: (-value[1]["size"], value[0]))
    for batch_count in range(minimum_batches, len(ordered) + 1):
        bins: list[dict[str, Any]] = [{"bytes": 0, "files": []} for _ in range(batch_count)]
        for original_index, file in largest_first:
            candidates = [
                (slot["bytes"], len(slot["files"]), index)
                for index, slot in enumerate(bins)
                if len(slot["files"]) < max_files and slot["bytes"] + file["size"] <= max_bytes
            ]
            if not candidates:
                break
            _, _, target_index = min(candidates)
            bins[target_index]["files"].append((original_index, file))
            bins[target_index]["bytes"] += file["size"]
        else:
            populated = [slot for slot in bins if slot["files"]]
            populated.sort(
                key=lambda slot: min(original_index for original_index, _ in slot["files"])
            )
            return [
                [
                    file
                    for _, file in sorted(
                        slot["files"],
                        key=lambda value: value[0],
                    )
                ]
                for slot in populated
            ]

    raise DomainError(
        "batch_split_failed",
        "The selection could not be divided within the current batch limits",
    )


class Repository:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    @staticmethod
    def _root_access_status(path: Path) -> tuple[bool, str | None, str | None]:
        """Return whether a source can be enumerated, plus a user-facing issue."""
        if not path.is_dir():
            return (
                False,
                "unavailable",
                "This source folder is offline or no longer exists.",
            )
        try:
            # is_dir() can succeed on macOS even when privacy controls prevent the
            # service from reading the directory. Opening the folder is the smallest
            # reliable probe and does not walk the source tree.
            with os.scandir(path) as entries:
                next(entries, None)
        except PermissionError:
            if sys.platform == "darwin":
                guidance = (
                    "On macOS, grant Removable Volumes or Full Disk Access to the terminal "
                    "or tmux process running Pixel Relay, then restart Pixel Relay."
                )
            elif sys.platform == "win32":
                guidance = (
                    "Grant the Windows account running Pixel Relay read access to the "
                    "folder or network share, then scan again."
                )
            else:
                guidance = (
                    "Grant the service account read and directory traversal access to the "
                    "folder or mounted share, then scan again."
                )
            return (
                False,
                "permission_denied",
                f"Pixel Relay does not have permission to read this folder. {guidance}",
            )
        except OSError as exc:
            detail = exc.strerror or str(exc)
            return False, "unavailable", f"Pixel Relay cannot read this folder: {detail}."
        return True, None, None

    def list_roots(self) -> list[dict[str, Any]]:
        roots = self.db.fetchall("SELECT * FROM source_roots WHERE enabled=1 ORDER BY name")
        for root in roots:
            available, issue_code, issue = self._root_access_status(local_path(root["path"]))
            root["available"] = available
            root["issue_code"] = issue_code
            root["issue"] = issue
            root["enabled"] = bool(root["enabled"])
        return roots

    def add_root(self, name: str, path_value: str) -> dict[str, Any]:
        try:
            path = local_path(path_value).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise DomainError(
                "root_not_found",
                "Source root does not exist or is unavailable",
            ) from exc
        if not path.is_dir():
            raise DomainError("not_a_directory", "Source root must be a directory")
        available, issue_code, issue = self._root_access_status(path)
        if not available:
            raise DomainError(
                "root_permission_denied"
                if issue_code == "permission_denied"
                else "root_unavailable",
                issue or "Source root is currently unavailable",
                status_code=403 if issue_code == "permission_denied" else 409,
            )
        existing = self.db.fetchone(
            "SELECT * FROM source_roots WHERE path = ?",
            (str(path),),
        )
        if existing:
            if existing["enabled"]:
                raise DomainError("duplicate_root", "That source root already exists")
            self.db.execute(
                "UPDATE source_roots SET name=?, enabled=1 WHERE id=?",
                (name.strip(), existing["id"]),
            )
            return (
                self.db.fetchone(
                    "SELECT * FROM source_roots WHERE id=?",
                    (existing["id"],),
                )
                or {}
            )
        try:
            root_id = self.db.execute(
                "INSERT INTO source_roots(name, path, created_at) VALUES (?, ?, ?)",
                (name.strip(), str(path), utcnow()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise DomainError("duplicate_root", "That source root already exists") from exc
            raise
        return self.db.fetchone("SELECT * FROM source_roots WHERE id = ?", (root_id,)) or {}

    def ensure_import_root(self) -> dict[str, Any]:
        if not self.settings.import_root:
            raise DomainError(
                "import_root_not_configured",
                "Configure the TheDoPixel import root in Settings before uploading",
                status_code=409,
            )
        path = self.settings.import_root.resolve(strict=True)
        root = self.db.fetchone("SELECT * FROM source_roots WHERE path = ?", (str(path),))
        if root:
            if not root["enabled"]:
                self.db.execute(
                    "UPDATE source_roots SET name='Browser uploads', enabled=1 WHERE id=?",
                    (root["id"],),
                )
                root = self.db.fetchone(
                    "SELECT * FROM source_roots WHERE id=?",
                    (root["id"],),
                )
            return root or {}
        root_id = self.db.execute(
            "INSERT INTO source_roots(name, path, created_at) VALUES (?, ?, ?)",
            ("Browser uploads", str(path), utcnow()),
        )
        return self.db.fetchone("SELECT * FROM source_roots WHERE id = ?", (root_id,)) or {}

    def register_file(
        self,
        path: Path,
        root_id: int,
        known_sha256: str | None = None,
    ) -> dict[str, Any]:
        root = self.db.fetchone("SELECT * FROM source_roots WHERE id = ?", (root_id,))
        if not root or not root["enabled"]:
            raise DomainError("root_not_found", "Source root was not found", status_code=404)
        resolved = resolve_inside(local_path(path), local_path(root["path"]))
        if not resolved.is_file():
            raise DomainError("not_a_file", f"Not a regular file: {resolved}")
        if is_macos_metadata(resolved):
            raise DomainError(
                "macos_metadata",
                f"Ignored macOS AppleDouble metadata file: {resolved.name}",
            )
        extension = resolved.suffix.lower()
        if extension not in self.settings.allowed_extensions:
            raise DomainError(
                "unsupported_media",
                f"Unsupported media extension: {extension or '(none)'}",
            )
        stat = resolved.stat()
        digest = known_sha256 or sha256_file(resolved)
        media_kind = "video" if extension in self.settings.video_extensions else "photo"
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM source_files WHERE path = ?", (str(resolved),)
            ).fetchone()
            if existing:
                file_id = existing["id"]
                connection.execute(
                    """
                    UPDATE source_files
                    SET root_id=?, sha256=?, size=?, mtime_ns=?, extension=?, media_kind=?,
                        discovered_at=?
                    WHERE id=?
                    """,
                    (
                        root_id,
                        digest,
                        stat.st_size,
                        stat.st_mtime_ns,
                        extension,
                        media_kind,
                        utcnow(),
                        file_id,
                    ),
                )
            else:
                file_id = connection.execute(
                    """
                    INSERT INTO source_files(
                      root_id, path, sha256, size, mtime_ns, extension, media_kind,
                      discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        root_id,
                        str(resolved),
                        digest,
                        stat.st_size,
                        stat.st_mtime_ns,
                        extension,
                        media_kind,
                        utcnow(),
                    ),
                ).lastrowid
        return (
            self.db.fetchone(
                """
            SELECT source_files.*, source_roots.name AS root_name
            FROM source_files JOIN source_roots ON source_roots.id=source_files.root_id
            WHERE source_files.id=?
            """,
                (file_id,),
            )
            or {}
        )

    def scan_root(
        self,
        root_id: int,
        selected_paths: list[str] | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        full_verify: bool = False,
    ) -> dict[str, Any]:
        root = self.db.fetchone("SELECT * FROM source_roots WHERE id = ?", (root_id,))
        if not root or not root["enabled"]:
            raise DomainError("root_not_found", "Source root was not found", status_code=404)
        root_path = local_path(root["path"])
        available, issue_code, issue = self._root_access_status(root_path)
        if not available:
            raise DomainError(
                "root_permission_denied"
                if issue_code == "permission_denied"
                else "root_unavailable",
                issue or "Source root is currently unavailable",
                status_code=403 if issue_code == "permission_denied" else 409,
            )
        scan_targets = (
            [resolve_inside(local_path(raw), root_path) for raw in selected_paths]
            if selected_paths
            else [root_path]
        )
        existing_by_path = {
            row["path"]: row
            for row in self.db.fetchall(
                "SELECT * FROM source_files WHERE root_id=?",
                (root_id,),
            )
        }
        ready: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        seen: set[Path] = set()
        visited_directories: set[Path] = set()
        pending: dict[Future[str], dict[str, Any]] = {}
        examined = 0
        eligible = 0
        hashed = 0
        cached = 0
        hash_completed = 0
        hash_workers = min(4, max(2, os.cpu_count() or 2))
        logger.info(
            "Source scan started",
            extra={
                "context": {
                    "root_id": root_id,
                    "root_name": root["name"],
                    "root_path": str(root_path),
                    "full_verify": full_verify,
                    "hash_workers": hash_workers,
                    "selected_scan": bool(selected_paths),
                }
            },
        )

        def report(phase: str, current_name: str | None = None) -> None:
            if progress:
                progress(
                    {
                        "phase": phase,
                        "processed": cached + hash_completed,
                        "total": eligible if phase != "enumerating" else 0,
                        "examined": examined,
                        "discovered": len(ready),
                        "skipped": len(skipped),
                        "cached": cached,
                        "hashed": hashed,
                        "issues": skipped[:3],
                        "current_name": current_name,
                        "full_verify": full_verify,
                    }
                )

        def record_issue(path: Path, exc: Exception) -> None:
            skipped.append({"path": str(path), "reason": str(exc)})
            report("enumerating", path.name)

        def iter_files() -> Iterable[tuple[Path, os.stat_result]]:
            stack = list(reversed(scan_targets))
            while stack:
                current = stack.pop()
                if is_macos_metadata(current):
                    continue
                try:
                    if current.is_file():
                        resolved = resolve_inside(current, root_path)
                        yield resolved, resolved.stat()
                        continue

                    # Windows junctions and directory symlinks are reparse points.
                    # DirEntry.is_dir(follow_symlinks=False) reports those as not
                    # being directories, which used to omit their complete trees.
                    # Resolve each directory before visiting it both to enforce the
                    # source-root boundary and to prevent junction/symlink loops.
                    resolved_directory = resolve_inside(current, root_path)
                    if resolved_directory in visited_directories:
                        continue
                    visited_directories.add(resolved_directory)
                    with os.scandir(resolved_directory) as entries:
                        directories: list[Path] = []
                        for entry in entries:
                            path = Path(entry.path)
                            if is_macos_metadata(path):
                                continue
                            try:
                                if entry.is_dir(follow_symlinks=True):
                                    directories.append(resolve_inside(path, root_path))
                                elif entry.is_file(follow_symlinks=True):
                                    resolved = resolve_inside(path, root_path)
                                    yield resolved, resolved.stat()
                            except (OSError, ValueError) as exc:
                                record_issue(path, exc)
                        stack.extend(reversed(directories))
                except (OSError, ValueError) as exc:
                    record_issue(current, exc)

        def consume(completed: set[Future[str]], phase: str) -> None:
            nonlocal hashed, hash_completed
            for future in completed:
                candidate = pending.pop(future)
                hash_completed += 1
                try:
                    candidate["sha256"] = future.result()
                    ready.append(candidate)
                    hashed += 1
                except OSError as exc:
                    skipped.append({"path": candidate["path"], "reason": str(exc)})
                report(phase, Path(candidate["path"]).name)

        report("enumerating")
        with ThreadPoolExecutor(
            max_workers=hash_workers,
            thread_name_prefix="pixel-relay-scan",
        ) as executor:
            for path, stat in iter_files():
                examined += 1
                if path in seen:
                    report("enumerating", path.name)
                    continue
                seen.add(path)
                extension = path.suffix.lower()
                if extension not in self.settings.allowed_extensions:
                    report("enumerating", path.name)
                    continue
                eligible += 1
                media_kind = "video" if extension in self.settings.video_extensions else "photo"
                candidate = {
                    "root_id": root_id,
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "extension": extension,
                    "media_kind": media_kind,
                }
                existing = existing_by_path.get(str(path))
                unchanged = bool(
                    existing
                    and existing["size"] == stat.st_size
                    and existing["mtime_ns"] == stat.st_mtime_ns
                    and existing["extension"] == extension
                    and existing["media_kind"] == media_kind
                )
                if unchanged and not full_verify:
                    candidate["sha256"] = existing["sha256"]
                    ready.append(candidate)
                    cached += 1
                else:
                    pending[executor.submit(sha256_file, path)] = candidate
                report("enumerating", path.name)
                if len(pending) >= hash_workers * 2:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    consume(completed, "enumerating")

            report("hashing")
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                consume(completed, "hashing")

        report("saving")
        discovered: list[dict[str, Any]] = []
        discovered_at = utcnow()
        with self.db.transaction() as connection:
            for candidate in ready:
                row = connection.execute(
                    """
                    INSERT INTO source_files(
                      root_id, path, sha256, size, mtime_ns, extension, media_kind,
                      discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                      root_id=excluded.root_id,
                      sha256=excluded.sha256,
                      size=excluded.size,
                      mtime_ns=excluded.mtime_ns,
                      extension=excluded.extension,
                      media_kind=excluded.media_kind,
                      discovered_at=excluded.discovered_at
                    RETURNING id
                    """,
                    (
                        candidate["root_id"],
                        candidate["path"],
                        candidate["sha256"],
                        candidate["size"],
                        candidate["mtime_ns"],
                        candidate["extension"],
                        candidate["media_kind"],
                        discovered_at,
                    ),
                ).fetchone()
                discovered.append(
                    {
                        "id": row["id"],
                        **candidate,
                        "discovered_at": discovered_at,
                        "root_name": root["name"],
                    }
                )
        discovered.sort(key=lambda item: item["path"])
        report("complete")
        logger.info(
            "Source scan completed",
            extra={
                "context": {
                    "root_id": root_id,
                    "root_name": root["name"],
                    "root_path": str(root_path),
                    "examined_count": examined,
                    "candidate_count": eligible,
                    "discovered_count": len(discovered),
                    "skipped_count": len(skipped),
                    "cached_count": cached,
                    "hashed_count": hashed,
                    "hash_workers": hash_workers,
                    "full_verify": full_verify,
                    "selected_scan": bool(selected_paths),
                }
            },
        )
        return {
            "files": discovered,
            "skipped": skipped,
            "stats": {
                "examined": examined,
                "candidates": eligible,
                "cached": cached,
                "hashed": hashed,
                "hash_workers": hash_workers,
                "full_verify": full_verify,
            },
        }

    def list_files(self, *, unbatched_only: bool = False) -> list[dict[str, Any]]:
        exclusion = "AND content_history.active_item_count=0" if unbatched_only else ""
        return self.db.fetchall(
            f"""
            WITH content_history AS (
              SELECT historical_files.sha256,
                COUNT(DISTINCT historical_files.id) AS source_path_count,
                COUNT(DISTINCT historical_items.batch_id) AS previous_batch_count,
                MAX(CASE WHEN historical_items.state IN (
                  'confirmed_backed_up', 'purged_from_pixel'
                ) THEN 1 ELSE 0 END) AS previously_confirmed,
                MAX(CASE WHEN historical_items.state='purged_from_pixel'
                  THEN 1 ELSE 0 END) AS previously_purged,
                SUM(CASE WHEN historical_items.state IS NOT NULL
                  AND historical_items.state NOT IN ('cancelled', 'purged_from_pixel')
                  THEN 1 ELSE 0 END) AS active_item_count
              FROM source_files historical_files
              LEFT JOIN batch_items historical_items
                ON historical_items.source_file_id=historical_files.id
              GROUP BY historical_files.sha256
            )
            SELECT source_files.*, source_roots.name AS root_name,
              content_history.source_path_count > 1 AS duplicate_content,
              content_history.previous_batch_count,
              content_history.previously_confirmed,
              content_history.previously_purged,
              content_history.active_item_count
            FROM source_files
            JOIN source_roots ON source_roots.id=source_files.root_id
            JOIN content_history ON content_history.sha256=source_files.sha256
            WHERE source_roots.enabled=1
            AND source_files.path NOT LIKE '%/._%'
            {exclusion}
            ORDER BY source_files.discovered_at DESC
            """
        )

    def plan_batches(
        self,
        name: str | None,
        file_ids: list[int],
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Build the exact batch split without changing queue or database state."""
        unique_ids = list(dict.fromkeys(file_ids))
        if not unique_ids:
            raise DomainError("empty_batch", "Select at least one source file")
        placeholders = ",".join("?" for _ in unique_ids)
        files = self.db.fetchall(
            f"SELECT * FROM source_files WHERE id IN ({placeholders})",
            tuple(unique_ids),
        )
        if len(files) != len(unique_ids):
            raise DomainError("file_not_found", "One or more selected files no longer exist")
        by_id = {file["id"]: file for file in files}
        ordered = [by_id[file_id] for file_id in unique_ids]
        unique_content: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for file in ordered:
            if file["sha256"] not in seen_hashes:
                unique_content.append(file)
                seen_hashes.add(file["sha256"])

        history_by_hash: dict[str, dict[str, Any]] = {}
        hashes = sorted(seen_hashes)
        if hashes:
            hash_placeholders = ",".join("?" for _ in hashes)
            history = self.db.fetchall(
                f"""
                SELECT historical_files.sha256,
                  COUNT(DISTINCT historical_items.batch_id) AS previous_batch_count,
                  MAX(CASE WHEN historical_items.state IN (
                    'confirmed_backed_up', 'purged_from_pixel'
                  ) THEN 1 ELSE 0 END) AS previously_confirmed,
                  MAX(CASE WHEN historical_items.state='purged_from_pixel'
                    THEN 1 ELSE 0 END) AS previously_purged
                FROM batch_items historical_items
                JOIN source_files historical_files
                  ON historical_files.id=historical_items.source_file_id
                WHERE historical_files.sha256 IN ({hash_placeholders})
                GROUP BY historical_files.sha256
                """,
                tuple(hashes),
            )
            history_by_hash = {row["sha256"]: row for row in history}

        folder_files: dict[Path, list[dict[str, Any]]] = {}
        for file in unique_content:
            folder_files.setdefault(local_path(file["path"]).parent, []).append(file)

        batch_byte_limit = min(
            self.settings.max_batch_bytes,
            max_bytes if max_bytes is not None else self.settings.max_batch_bytes,
        )
        grouped_chunks = [
            (
                folder,
                split_batch_files(
                    files_in_folder,
                    max_files=self.settings.max_batch_files,
                    max_bytes=batch_byte_limit,
                ),
            )
            for folder, files_in_folder in folder_files.items()
        ]
        multiple_folders = len(grouped_chunks) > 1
        custom_name = name.strip() if name else ""
        total_batches = sum(len(chunks) for _, chunks in grouped_chunks)
        total_selection_bytes = sum(file["size"] for file in unique_content)
        split_reason = (
            "pixel_storage"
            if total_batches > 1
            and batch_byte_limit < self.settings.max_batch_bytes
            and total_selection_bytes > batch_byte_limit
            else "configured_limits"
            if any(len(chunks) > 1 for _, chunks in grouped_chunks)
            else "source_folders"
            if total_batches > 1
            else None
        )

        parts: list[dict[str, Any]] = []
        for folder, chunks in grouped_chunks:
            for index, chunk in enumerate(chunks, start=1):
                folder_label = (folder.name or "root")[:60]
                folder_suffix = f" · {folder_label}" if custom_name and multiple_folders else ""
                split_suffix = f" · {index} of {len(chunks)}" if len(chunks) > 1 else ""
                suffix = f"{folder_suffix}{split_suffix}"
                base_name = custom_name or folder_label
                batch_name = f"{base_name[: 120 - len(suffix)]}{suffix}"
                parts.append(
                    {
                        "name": batch_name,
                        "folder": str(folder),
                        "file_ids": [int(file["id"]) for file in chunk],
                        "file_count": len(chunk),
                        "total_bytes": sum(int(file["size"]) for file in chunk),
                        "photo_count": sum(
                            file["media_kind"] == "photo"
                            and file["extension"] not in self.settings.raw_extensions
                            for file in chunk
                        ),
                        "raw_count": sum(
                            file["extension"] in self.settings.raw_extensions for file in chunk
                        ),
                        "video_count": sum(file["media_kind"] == "video" for file in chunk),
                    }
                )

        performance = self.recent_performance()
        estimated_seconds: int | None = None
        if performance["transfer_rate_bytes_per_second"]:
            estimated_seconds = round(
                total_selection_bytes / performance["transfer_rate_bytes_per_second"]
                + len(unique_content) * (performance["average_scan_seconds"] or 0)
            )
        return {
            "selected_count": len(ordered),
            "unique_content_count": len(unique_content),
            "duplicate_selection_count": len(ordered) - len(unique_content),
            "total_bytes": total_selection_bytes,
            "photo_count": sum(
                file["media_kind"] == "photo"
                and file["extension"] not in self.settings.raw_extensions
                for file in unique_content
            ),
            "raw_count": sum(
                file["extension"] in self.settings.raw_extensions for file in unique_content
            ),
            "video_count": sum(file["media_kind"] == "video" for file in unique_content),
            "source_count": len({file["root_id"] for file in unique_content}),
            "folder_count": len(folder_files),
            "batch_count": total_batches,
            "batch_byte_limit": batch_byte_limit,
            "split_reason": split_reason,
            "previously_processed_count": sum(
                bool(history_by_hash.get(file["sha256"], {}).get("previous_batch_count"))
                for file in unique_content
            ),
            "previously_confirmed_count": sum(
                bool(history_by_hash.get(file["sha256"], {}).get("previously_confirmed"))
                for file in unique_content
            ),
            "previously_purged_count": sum(
                bool(history_by_hash.get(file["sha256"], {}).get("previously_purged"))
                for file in unique_content
            ),
            "estimated_seconds": estimated_seconds,
            "performance_basis": performance,
            "parts": parts,
        }

    def remove_root(self, root_id: int, user_id: int) -> dict[str, Any]:
        """Hide a source registration without touching its files or history."""
        root = self.db.fetchone(
            "SELECT * FROM source_roots WHERE id=? AND enabled=1",
            (root_id,),
        )
        if not root:
            raise DomainError(
                "root_not_found",
                "Source root was not found",
                status_code=404,
            )
        file_count_row = self.db.fetchone(
            "SELECT COUNT(*) AS count FROM source_files WHERE root_id=?",
            (root_id,),
        )
        self.db.execute(
            "UPDATE source_roots SET enabled=0 WHERE id=?",
            (root_id,),
        )
        file_count = int(file_count_row["count"] if file_count_row else 0)
        self.db.audit(
            "source.remove",
            "source_root",
            str(root_id),
            user_id,
            {
                "name": root["name"],
                "path": root["path"],
                "discovered_records_retained": file_count,
                "originals_deleted": False,
            },
        )
        return {
            "id": root_id,
            "name": root["name"],
            "path": root["path"],
            "discovered_records_retained": file_count,
            "originals_deleted": False,
        }

    def create_batch(
        self,
        name: str,
        file_ids: list[int],
        user_id: int,
        *,
        series_id: str | None = None,
        series_index: int | None = None,
        series_total: int | None = None,
        planned_capacity_bytes: int | None = None,
        split_reason: str | None = None,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(file_ids))
        if len(unique_ids) > self.settings.max_batch_files:
            raise DomainError("batch_too_large", "Batch exceeds the configured file limit")
        placeholders = ",".join("?" for _ in unique_ids)
        files = self.db.fetchall(
            f"SELECT * FROM source_files WHERE id IN ({placeholders})", tuple(unique_ids)
        )
        if len(files) != len(unique_ids):
            raise DomainError("file_not_found", "One or more selected files no longer exist")
        by_id = {file["id"]: file for file in files}
        files = [by_id[file_id] for file_id in unique_ids]
        unique_content: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for file in files:
            if file["sha256"] not in seen_hashes:
                unique_content.append(file)
                seen_hashes.add(file["sha256"])
        files = unique_content
        total_bytes = sum(file["size"] for file in files)
        if total_bytes > self.settings.max_batch_bytes:
            raise DomainError("batch_too_large", "Batch exceeds the configured byte limit")
        batch_id = uuid.uuid4().hex
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO batches(
                  id, name, created_by, created_at, series_id, series_index,
                  series_total, planned_capacity_bytes, split_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    name.strip(),
                    user_id,
                    utcnow(),
                    series_id,
                    series_index,
                    series_total,
                    planned_capacity_bytes,
                    split_reason,
                ),
            )
            used_names: dict[str, set[str]] = {"photos": set(), "videos": set()}
            for file in files:
                item_id = uuid.uuid4().hex
                category_directory = "videos" if file["media_kind"] == "video" else "photos"
                destination_root = (
                    self.settings.ftp_destination_root
                    if self.settings.connection_mode == "ftp"
                    else self.settings.destination_root
                )
                remote_path = (
                    f"{destination_root}/{batch_id}/{category_directory}/"
                    f"{relay_filename(file['path'], item_id, used_names[category_directory])}"
                )
                now = utcnow()
                connection.execute(
                    """
                    INSERT INTO batch_items(
                      id, batch_id, source_file_id, state, remote_path,
                      transfer_total_bytes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        batch_id,
                        file["id"],
                        ItemState.QUEUED,
                        remote_path,
                        file["size"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO state_events(item_id, from_state, to_state, detail, created_at)
                    VALUES (?, NULL, ?, ?, ?)
                    """,
                    (item_id, ItemState.QUEUED, "Batch created", now),
                )
        self.db.audit(
            "batch.create",
            "batch",
            batch_id,
            user_id,
            {
                "file_count": len(files),
                "series_id": series_id,
                "series_index": series_index,
                "series_total": series_total,
                "planned_capacity_bytes": planned_capacity_bytes,
                "split_reason": split_reason,
            },
        )
        logger.info(
            "Batch created and queued",
            extra={
                "context": {
                    "batch_id": batch_id,
                    "batch_name": name.strip(),
                    "file_count": len(files),
                    "photo_count": sum(file["media_kind"] == "photo" for file in files),
                    "video_count": sum(file["media_kind"] == "video" for file in files),
                    "total_bytes": total_bytes,
                    "connection_mode": self.settings.connection_mode,
                    "series_id": series_id,
                    "series_index": series_index,
                    "series_total": series_total,
                    "planned_capacity_bytes": planned_capacity_bytes,
                    "split_reason": split_reason,
                }
            },
        )
        return self.get_batch(batch_id)

    def create_batches(
        self,
        name: str | None,
        file_ids: list[int],
        user_id: int,
        *,
        max_bytes: int | None = None,
    ) -> list[dict[str, Any]]:
        """Group by source folder and default each batch name to that folder."""
        plan = self.plan_batches(name, file_ids, max_bytes=max_bytes)
        total_batches = int(plan["batch_count"])
        series_id = uuid.uuid4().hex if total_batches > 1 else None
        batches: list[dict[str, Any]] = []
        for series_index, part in enumerate(plan["parts"], start=1):
            batches.append(
                self.create_batch(
                    part["name"],
                    part["file_ids"],
                    user_id,
                    series_id=series_id,
                    series_index=series_index if series_id else None,
                    series_total=total_batches if series_id else None,
                    planned_capacity_bytes=plan["batch_byte_limit"] if series_id else None,
                    split_reason=plan["split_reason"],
                )
            )
        return batches

    def list_batches(
        self,
        *,
        unsettled_only: bool = False,
        include_batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        batch_filter = ""
        parameters: tuple[Any, ...] = ()
        if unsettled_only:
            unsettled = """
              batches.cancelled_at IS NULL
              AND batches.confirmed_at IS NULL
              AND batches.purged_at IS NULL
            """
            if include_batch_id:
                batch_filter = f"WHERE (batches.id=? OR ({unsettled}))"
                parameters = (include_batch_id,)
            else:
                batch_filter = f"WHERE {unsettled}"
        rows = self.db.fetchall(
            f"""
            SELECT batches.id, batches.name, batches.created_by, batches.created_at,
              batches.confirmed_at, batches.confirmed_by, batches.purged_at,
              batches.cancelled_at, batches.cancelled_by, batches.series_id,
              batches.paused_at, batches.paused_by, batches.total_paused_seconds,
              batches.series_index, batches.series_total,
              batches.planned_capacity_bytes, batches.split_reason,
              COUNT(batch_items.id) AS file_count,
              COALESCE(SUM(source_files.size), 0) AS total_bytes,
              COALESCE(SUM(batch_items.transfer_bytes), 0) AS transfer_bytes,
              SUM(CASE WHEN source_files.media_kind='photo' THEN 1 ELSE 0 END) AS photo_count,
              SUM(CASE WHEN source_files.media_kind='video' THEN 1 ELSE 0 END) AS video_count
            FROM batches
            LEFT JOIN batch_items ON batch_items.batch_id=batches.id
            LEFT JOIN source_files ON source_files.id=batch_items.source_file_id
            {batch_filter}
            GROUP BY batches.id
            ORDER BY batches.created_at DESC
            """,
            parameters,
        )
        for row in rows:
            row["states"] = self.state_counts(row["id"])
            row["series_blocked"] = self.series_blocked(row["id"])
            raw_count = self._batch_raw_count(row["id"])
            row["raw_count"] = raw_count
            row["photo_count"] = max(0, int(row["photo_count"] or 0) - raw_count)
            self._add_processing_estimate(row)
        return rows

    def list_backed_up_items(self, *, limit: int = 250, offset: int = 0) -> dict[str, Any]:
        """Enumerate unique content covered by an explicit backup confirmation."""
        raw_extensions = sorted(self.settings.raw_extensions)
        raw_placeholders = ",".join("?" for _ in raw_extensions)
        confirmed_items = """
            WITH confirmed_items AS (
              SELECT batch_items.id, batch_items.state, batch_items.remote_path,
                batch_items.updated_at, batches.id AS batch_id,
                batches.name AS batch_name, batches.confirmed_at, batches.purged_at,
                source_files.id AS source_file_id, source_files.path,
                source_files.sha256, source_files.size, source_files.mtime_ns,
                source_files.extension, source_files.media_kind,
                ROW_NUMBER() OVER (
                  PARTITION BY source_files.sha256
                  ORDER BY batches.confirmed_at DESC, batch_items.updated_at DESC,
                    batch_items.id DESC
                ) AS content_row,
                COUNT(*) OVER (
                  PARTITION BY source_files.sha256
                ) AS confirmation_count,
                MIN(batches.confirmed_at) OVER (
                  PARTITION BY source_files.sha256
                ) AS first_confirmed_at,
                MAX(batches.confirmed_at) OVER (
                  PARTITION BY source_files.sha256
                ) AS latest_confirmed_at,
                SUM(CASE WHEN batch_items.state='confirmed_backed_up' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY source_files.sha256) AS retained_copy_count,
                SUM(CASE WHEN batch_items.state='purged_from_pixel' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY source_files.sha256) AS purged_copy_count
              FROM batch_items
              JOIN batches ON batches.id=batch_items.batch_id
              JOIN source_files ON source_files.id=batch_items.source_file_id
              WHERE batches.confirmed_at IS NOT NULL
                AND batch_items.state IN ('confirmed_backed_up', 'purged_from_pixel')
            )
        """
        summary = (
            self.db.fetchone(
                f"""
            {confirmed_items}
            SELECT COUNT(*) AS total,
              COALESCE(SUM(size), 0) AS total_bytes,
              COALESCE(SUM(CASE
                WHEN extension IN ({raw_placeholders}) THEN 1 ELSE 0
              END), 0) AS raw_count,
              COALESCE(SUM(CASE
                WHEN media_kind='photo'
                  AND extension NOT IN ({raw_placeholders})
                THEN 1 ELSE 0
              END), 0) AS photo_count,
              COALESCE(SUM(CASE
                WHEN media_kind='video' THEN 1 ELSE 0
              END), 0) AS video_count,
              COALESCE(SUM(CASE
                WHEN retained_copy_count > 0 THEN 1 ELSE 0
              END), 0) AS retained_on_pixel_count,
              COALESCE(SUM(CASE
                WHEN retained_copy_count = 0 AND purged_copy_count > 0 THEN 1 ELSE 0
              END), 0) AS purged_from_pixel_count
            FROM confirmed_items
            WHERE content_row=1
            """,
                (*raw_extensions, *raw_extensions),
            )
            or {}
        )
        relay_summary = (
            self.db.fetchone(
                f"""
            WITH relay_items AS (
              SELECT source_files.sha256, source_files.size,
                source_files.extension, source_files.media_kind,
                ROW_NUMBER() OVER (
                  PARTITION BY source_files.sha256
                  ORDER BY batch_items.updated_at DESC, batch_items.id DESC
                ) AS content_row,
                MAX(CASE
                  WHEN batches.confirmed_at IS NOT NULL
                    AND batch_items.state IN (
                      'confirmed_backed_up',
                      'purged_from_pixel'
                    )
                  THEN 1 ELSE 0
                END) OVER (PARTITION BY source_files.sha256) AS confirmed,
                MAX(CASE
                  WHEN batch_items.state='awaiting_backup_confirmation'
                  THEN 1 ELSE 0
                END) OVER (PARTITION BY source_files.sha256) AS awaiting_verification
              FROM batch_items
              JOIN batches ON batches.id=batch_items.batch_id
              JOIN source_files ON source_files.id=batch_items.source_file_id
              WHERE batch_items.state='awaiting_backup_confirmation'
                OR (
                  batches.confirmed_at IS NOT NULL
                  AND batch_items.state IN (
                    'confirmed_backed_up',
                    'purged_from_pixel'
                  )
                )
            )
            SELECT COUNT(*) AS uploaded_total,
              COALESCE(SUM(size), 0) AS uploaded_total_bytes,
              COALESCE(SUM(CASE
                WHEN extension IN ({raw_placeholders}) THEN 1 ELSE 0
              END), 0) AS uploaded_raw_count,
              COALESCE(SUM(CASE
                WHEN media_kind='photo'
                  AND extension NOT IN ({raw_placeholders})
                THEN 1 ELSE 0
              END), 0) AS uploaded_photo_count,
              COALESCE(SUM(CASE
                WHEN media_kind='video' THEN 1 ELSE 0
              END), 0) AS uploaded_video_count,
              COALESCE(SUM(CASE
                WHEN confirmed=0 AND awaiting_verification=1 THEN 1 ELSE 0
              END), 0) AS awaiting_verification_count,
              COALESCE(SUM(CASE
                WHEN confirmed=0 AND awaiting_verification=1 THEN size ELSE 0
              END), 0) AS awaiting_verification_bytes
            FROM relay_items
            WHERE content_row=1
            """,
                (*raw_extensions, *raw_extensions),
            )
            or {}
        )
        items = self.db.fetchall(
            f"""
            {confirmed_items}
            SELECT id,
              CASE
                WHEN retained_copy_count > 0 THEN 'confirmed_backed_up'
                ELSE 'purged_from_pixel'
              END AS state,
              remote_path, updated_at, batch_id, batch_name, confirmed_at,
              purged_at, source_file_id, path, sha256, size, mtime_ns,
              extension, media_kind, confirmation_count, first_confirmed_at,
              latest_confirmed_at, retained_copy_count, purged_copy_count
            FROM confirmed_items
            WHERE content_row=1
            ORDER BY latest_confirmed_at DESC, sha256
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return {
            "total": int(summary.get("total") or 0),
            "total_bytes": int(summary.get("total_bytes") or 0),
            "photo_count": int(summary.get("photo_count") or 0),
            "raw_count": int(summary.get("raw_count") or 0),
            "video_count": int(summary.get("video_count") or 0),
            "retained_on_pixel_count": int(summary.get("retained_on_pixel_count") or 0),
            "purged_from_pixel_count": int(summary.get("purged_from_pixel_count") or 0),
            "uploaded_total": int(relay_summary.get("uploaded_total") or 0),
            "uploaded_total_bytes": int(relay_summary.get("uploaded_total_bytes") or 0),
            "uploaded_photo_count": int(relay_summary.get("uploaded_photo_count") or 0),
            "uploaded_raw_count": int(relay_summary.get("uploaded_raw_count") or 0),
            "uploaded_video_count": int(relay_summary.get("uploaded_video_count") or 0),
            "awaiting_verification_count": int(
                relay_summary.get("awaiting_verification_count") or 0
            ),
            "awaiting_verification_bytes": int(
                relay_summary.get("awaiting_verification_bytes") or 0
            ),
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    def _batch_raw_count(self, batch_id: str) -> int:
        extensions = sorted(self.settings.raw_extensions)
        placeholders = ",".join("?" for _ in extensions)
        row = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS count
            FROM batch_items
            JOIN source_files ON source_files.id=batch_items.source_file_id
            WHERE batch_items.batch_id=?
              AND source_files.extension IN ({placeholders})
            """,
            (batch_id, *extensions),
        )
        return int(row["count"] if row else 0)

    def state_counts(self, batch_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT state, COUNT(*) AS count FROM batch_items WHERE batch_id=? GROUP BY state",
            (batch_id,),
        )
        return {row["state"]: row["count"] for row in rows}

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.db.fetchone(
            """
            SELECT id, name, created_by, created_at, confirmed_at, confirmed_by,
              purged_at, cancelled_at, cancelled_by, paused_at, paused_by,
              total_paused_seconds, series_id, series_index,
              series_total, planned_capacity_bytes, split_reason
            FROM batches
            WHERE id=?
            """,
            (batch_id,),
        )
        if not batch:
            raise DomainError("batch_not_found", "Batch was not found", status_code=404)
        batch["items"] = self.db.fetchall(
            """
            SELECT batch_items.*, source_files.path, source_files.sha256, source_files.size,
              source_files.mtime_ns, source_files.extension, source_files.media_kind
            FROM batch_items
            JOIN source_files ON source_files.id=batch_items.source_file_id
            WHERE batch_items.batch_id=?
            ORDER BY source_files.path
            """,
            (batch_id,),
        )
        batch["states"] = dict(Counter(item["state"] for item in batch["items"]))
        batch["series_blocked"] = self.series_blocked(batch_id)
        batch["total_bytes"] = sum(item["size"] for item in batch["items"])
        batch["transfer_bytes"] = sum(
            min(item["size"], item["transfer_bytes"]) for item in batch["items"]
        )
        batch["raw_count"] = sum(
            item["extension"] in self.settings.raw_extensions for item in batch["items"]
        )
        batch["photo_count"] = sum(
            item["media_kind"] == "photo" and item["extension"] not in self.settings.raw_extensions
            for item in batch["items"]
        )
        batch["video_count"] = sum(item["media_kind"] == "video" for item in batch["items"])
        self._add_processing_estimate(batch)
        batch["performance"] = self.batch_performance(batch_id)
        return batch

    def _add_processing_estimate(self, batch: dict[str, Any]) -> None:
        started = self.db.fetchone(
            """
            SELECT MIN(state_events.created_at) AS processing_started_at
            FROM state_events
            JOIN batch_items ON batch_items.id=state_events.item_id
            WHERE batch_items.batch_id=? AND state_events.to_state='transferring'
            """,
            (batch["id"],),
        )
        started_at = started["processing_started_at"] if started else None
        batch["processing_started_at"] = started_at
        batch["transfer_rate_bytes_per_second"] = None
        batch["eta_seconds"] = None
        latest_activity = self.db.fetchone(
            """
            SELECT state, updated_at
            FROM batch_items
            WHERE batch_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (batch["id"],),
        )
        active_activity = self.db.fetchone(
            """
            SELECT state, updated_at
            FROM batch_items
            WHERE batch_id=? AND state IN ('transferring', 'staged_on_pixel')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (batch["id"],),
        )
        batch["last_activity_at"] = (
            latest_activity["updated_at"] if latest_activity else batch.get("created_at")
        )
        batch["stalled"] = False
        batch["stalled_for_seconds"] = None
        batch["stall_reason"] = None
        if active_activity and not batch.get("cancelled_at") and not batch.get("paused_at"):
            idle_seconds = max(
                0,
                round(
                    (
                        datetime.now(UTC) - datetime.fromisoformat(active_activity["updated_at"])
                    ).total_seconds()
                ),
            )
            if idle_seconds >= 15 * 60:
                batch["stalled"] = True
                batch["stalled_for_seconds"] = idle_seconds
                batch["stall_reason"] = (
                    "Transfer has not reported progress"
                    if active_activity["state"] == ItemState.TRANSFERRING
                    else "MediaStore scan has not completed"
                )
        if not started_at or batch.get("cancelled_at") or batch.get("paused_at"):
            return

        transferred = max(0, int(batch.get("transfer_bytes") or 0))
        total = max(0, int(batch.get("total_bytes") or 0))
        remaining = max(0, total - transferred)
        if not transferred or not remaining:
            return
        elapsed = max(
            1.0,
            (datetime.now(UTC) - datetime.fromisoformat(started_at)).total_seconds()
            - float(batch.get("total_paused_seconds") or 0),
        )
        rate = transferred / elapsed
        if rate <= 0:
            return
        batch["transfer_rate_bytes_per_second"] = round(rate, 2)
        batch["eta_seconds"] = max(1, round(remaining / rate))

    @staticmethod
    def _seconds_between(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        return max(
            0.0,
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
        )

    def _performance_rows(
        self,
        *,
        batch_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        batch_filter = ""
        if batch_id:
            batch_filter = "WHERE batch_items.batch_id=?"
            parameters.append(batch_id)
        limit_clause = ""
        if limit:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        return self.db.fetchall(
            f"""
            SELECT batch_items.id, source_files.size,
              MIN(CASE WHEN state_events.to_state='transferring'
                THEN state_events.created_at END) AS transfer_started_at,
              MIN(CASE WHEN state_events.to_state='staged_on_pixel'
                THEN state_events.created_at END) AS staged_at,
              MIN(CASE WHEN state_events.to_state='awaiting_backup_confirmation'
                THEN state_events.created_at END) AS ready_at
            FROM batch_items
            JOIN source_files ON source_files.id=batch_items.source_file_id
            LEFT JOIN state_events ON state_events.item_id=batch_items.id
            {batch_filter}
            GROUP BY batch_items.id
            HAVING staged_at IS NOT NULL
            ORDER BY staged_at DESC
            {limit_clause}
            """,
            tuple(parameters),
        )

    def _performance_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        transfer_seconds = 0.0
        transferred_bytes = 0
        scan_seconds: list[float] = []
        for row in rows:
            transfer_duration = self._seconds_between(
                row.get("transfer_started_at"), row.get("staged_at")
            )
            if transfer_duration is not None and transfer_duration > 0:
                transfer_seconds += transfer_duration
                transferred_bytes += int(row["size"])
            scan_duration = self._seconds_between(row.get("staged_at"), row.get("ready_at"))
            if scan_duration is not None:
                scan_seconds.append(scan_duration)
        return {
            "sample_count": len(rows),
            "transferred_bytes": transferred_bytes,
            "transfer_seconds": round(transfer_seconds, 2),
            "transfer_rate_bytes_per_second": (
                round(transferred_bytes / transfer_seconds, 2)
                if transfer_seconds > 0 and transferred_bytes > 0
                else None
            ),
            "scanned_count": len(scan_seconds),
            "scan_seconds": round(sum(scan_seconds), 2),
            "average_scan_seconds": (
                round(sum(scan_seconds) / len(scan_seconds), 2) if scan_seconds else None
            ),
        }

    def recent_performance(self) -> dict[str, Any]:
        """Return a rolling completed-item baseline for preflight estimates."""
        return self._performance_summary(self._performance_rows(limit=200))

    def batch_performance(self, batch_id: str) -> dict[str, Any]:
        return self._performance_summary(self._performance_rows(batch_id=batch_id))

    def series_blocked(self, batch_id: str) -> bool:
        row = self.db.fetchone(
            """
            SELECT EXISTS (
              SELECT 1
              FROM batches AS current
              JOIN batches AS earlier
                ON earlier.series_id=current.series_id
               AND earlier.series_index < current.series_index
              WHERE current.id=?
                AND current.series_id IS NOT NULL
                AND EXISTS (
                  SELECT 1
                  FROM batch_items AS earlier_items
                  WHERE earlier_items.batch_id=earlier.id
                    AND earlier_items.state NOT IN (
                      'awaiting_backup_confirmation',
                      'confirmed_backed_up',
                      'cancelled',
                      'purged_from_pixel'
                    )
                )
            ) AS blocked
            """,
            (batch_id,),
        )
        return bool(row and row["blocked"])

    def delete_batch(self, batch_id: str, user_id: int) -> dict[str, Any]:
        """Delete inactive local batch records without touching source or Pixel files."""
        batch = self.get_batch(batch_id)
        items = batch["items"]
        never_started = bool(items) and all(
            item["state"] == ItemState.QUEUED and item["attempts"] == 0 for item in items
        )
        fully_purged = bool(items) and all(
            item["state"] == ItemState.PURGED_FROM_PIXEL for item in items
        )
        settled_cancellation = (
            bool(batch["cancelled_at"])
            and bool(items)
            and all(item["state"] != ItemState.TRANSFERRING for item in items)
        )
        if not never_started and not fully_purged and not settled_cancellation:
            raise DomainError(
                "batch_delete_unsafe",
                "A batch can be deleted only before transfer starts, after cancellation "
                "settles, or after every Pixel copy is purged",
                status_code=409,
            )
        with self.db.transaction() as connection:
            connection.execute(
                """
                DELETE FROM state_events
                WHERE item_id IN (SELECT id FROM batch_items WHERE batch_id=?)
                """,
                (batch_id,),
            )
            connection.execute("DELETE FROM batch_items WHERE batch_id=?", (batch_id,))
            connection.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        self.db.audit(
            "batch.delete",
            "batch",
            batch_id,
            user_id,
            {
                "name": batch["name"],
                "file_count": len(items),
                "cancelled": bool(batch["cancelled_at"]),
                "pixel_copies_may_remain": any(
                    item["state"] not in {ItemState.CANCELLED, ItemState.PURGED_FROM_PIXEL}
                    for item in items
                ),
            },
        )
        return {"id": batch_id, "name": batch["name"], "file_count": len(items)}

    def destination_reset_plan(self, destination_root: str) -> dict[str, Any]:
        """Identify complete batches stored beneath one exact destination root."""
        root = destination_root.rstrip("/")
        prefix = f"{root}/"
        rows = self.db.fetchall(
            """
            SELECT DISTINCT batch_items.batch_id
            FROM batch_items
            WHERE substr(remote_path, 1, ?) = ?
            ORDER BY batch_items.batch_id
            """,
            (len(prefix), prefix),
        )
        batch_ids = [str(row["batch_id"]) for row in rows]
        for batch_id in batch_ids:
            outside = self.db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM batch_items
                WHERE batch_id=? AND substr(remote_path, 1, ?) != ?
                """,
                (batch_id, len(prefix), prefix),
            )
            if outside and int(outside["count"] or 0):
                raise DomainError(
                    "mixed_batch_destination",
                    "A batch spans the reset destination and another storage root",
                    status_code=409,
                )
        item_count = 0
        if batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            count = self.db.fetchone(
                f"SELECT COUNT(*) AS count FROM batch_items WHERE batch_id IN ({placeholders})",
                tuple(batch_ids),
            )
            item_count = int(count["count"] or 0) if count else 0
        return {
            "destination_root": root,
            "batch_ids": batch_ids,
            "batch_count": len(batch_ids),
            "item_count": item_count,
        }

    def reconcile_destination_reset(
        self,
        destination_root: str,
        user_id: int,
    ) -> dict[str, Any]:
        """Settle batch state after the configured remote tree was verified empty."""
        plan = self.destination_reset_plan(destination_root)
        now = utcnow()
        purged_items = 0
        cancelled_items = 0
        confirmed_batches = 0
        cancelled_batches = 0
        with self.db.transaction() as connection:
            for batch_id in plan["batch_ids"]:
                batch = connection.execute(
                    "SELECT confirmed_at FROM batches WHERE id=?", (batch_id,)
                ).fetchone()
                if not batch:
                    raise DomainError("batch_not_found", "Batch was not found", status_code=404)
                confirmed = bool(batch["confirmed_at"])
                items = connection.execute(
                    "SELECT id, state FROM batch_items WHERE batch_id=?", (batch_id,)
                ).fetchall()
                for item in items:
                    current = item["state"]
                    target = (
                        ItemState.PURGED_FROM_PIXEL
                        if confirmed or current == ItemState.PURGED_FROM_PIXEL
                        else ItemState.CANCELLED
                    )
                    if target == ItemState.PURGED_FROM_PIXEL:
                        purged_items += 1
                    else:
                        cancelled_items += 1
                    connection.execute(
                        """
                        UPDATE batch_items
                        SET state=?, resume_state=NULL, error_code=NULL, error_detail=NULL,
                            updated_at=?
                        WHERE id=?
                        """,
                        (target, now, item["id"]),
                    )
                    if current != target:
                        connection.execute(
                            """
                            INSERT INTO state_events(
                              item_id, from_state, to_state, detail, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                item["id"],
                                current,
                                target,
                                "Configured Pixel Relay storage tree reset by administrator",
                                now,
                            ),
                        )
                if confirmed:
                    confirmed_batches += 1
                    connection.execute(
                        "UPDATE batches SET purged_at=COALESCE(purged_at, ?) WHERE id=?",
                        (now, batch_id),
                    )
                else:
                    cancelled_batches += 1
                    connection.execute(
                        """
                        UPDATE batches
                        SET cancelled_at=COALESCE(cancelled_at, ?),
                            cancelled_by=COALESCE(cancelled_by, ?),
                            purged_at=COALESCE(purged_at, ?)
                        WHERE id=?
                        """,
                        (now, user_id, now, batch_id),
                    )
        return {
            "destination_root": plan["destination_root"],
            "batch_count": plan["batch_count"],
            "item_count": plan["item_count"],
            "confirmed_batches_purged": confirmed_batches,
            "unconfirmed_batches_cancelled": cancelled_batches,
            "items_purged": purged_items,
            "items_cancelled": cancelled_items,
        }

    def retrigger_batch(self, batch_id: str, user_id: int) -> dict[str, Any]:
        """Create a new queued batch from a safely settled batch's source records."""
        batch = self.get_batch(batch_id)
        items = batch["items"]
        settled_states = {ItemState.CANCELLED, ItemState.PURGED_FROM_PIXEL}
        if not items or any(ItemState(item["state"]) not in settled_states for item in items):
            raise DomainError(
                "batch_retrigger_unsafe",
                "Purge or safely cancel every Pixel copy before running this batch again",
                status_code=409,
            )

        source_ids = [int(item["source_file_id"]) for item in items]
        placeholders = ",".join("?" for _ in source_ids)
        active = self.db.fetchone(
            f"""
            SELECT 1
            FROM batch_items
            WHERE source_file_id IN ({placeholders})
              AND batch_id != ?
              AND state NOT IN ('cancelled', 'purged_from_pixel')
            LIMIT 1
            """,
            (*source_ids, batch_id),
        )
        if active:
            raise DomainError(
                "batch_retrigger_active",
                "One or more files are already queued in another active batch",
                status_code=409,
            )

        suffix = " · rerun"
        name = f"{batch['name'][: 120 - len(suffix)]}{suffix}"
        retriggered = self.create_batch(name, source_ids, user_id)
        self.db.audit(
            "batch.retrigger",
            "batch",
            retriggered["id"],
            user_id,
            {
                "source_batch_id": batch_id,
                "file_count": len(source_ids),
            },
        )
        logger.info(
            "Batch retriggered as a new queued batch",
            extra={
                "context": {
                    "source_batch_id": batch_id,
                    "batch_id": retriggered["id"],
                    "file_count": len(source_ids),
                }
            },
        )
        return retriggered

    def pause_batch(self, batch_id: str, user_id: int) -> dict[str, Any]:
        """Prevent the worker from starting another item in this batch."""
        now = utcnow()
        with self.db.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise DomainError("batch_not_found", "Batch was not found", status_code=404)
            if batch["paused_at"]:
                return self.get_batch(batch_id)
            if batch["cancelled_at"] or batch["confirmed_at"] or batch["purged_at"]:
                raise DomainError(
                    "batch_pause_unavailable",
                    "A cancelled, confirmed, or purged batch cannot be paused",
                    status_code=409,
                )
            connection.execute(
                "UPDATE batches SET paused_at=?, paused_by=? WHERE id=?",
                (now, user_id, batch_id),
            )
        self.db.audit(
            "batch.pause",
            "batch",
            batch_id,
            user_id,
            {"name": batch["name"]},
        )
        logger.info(
            "Batch paused",
            extra={"context": {"batch_id": batch_id}},
        )
        return self.get_batch(batch_id)

    def resume_batch(self, batch_id: str, user_id: int) -> dict[str, Any]:
        """Allow queued work in a manually paused batch to continue."""
        now = utcnow()
        with self.db.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise DomainError("batch_not_found", "Batch was not found", status_code=404)
            if not batch["paused_at"]:
                return self.get_batch(batch_id)
            if batch["cancelled_at"] or batch["confirmed_at"] or batch["purged_at"]:
                raise DomainError(
                    "batch_resume_unavailable",
                    "A cancelled, confirmed, or purged batch cannot be resumed",
                    status_code=409,
                )
            paused_seconds = max(
                0,
                round(
                    (
                        datetime.fromisoformat(now) - datetime.fromisoformat(batch["paused_at"])
                    ).total_seconds()
                ),
            )
            connection.execute(
                """
                UPDATE batches
                SET paused_at=NULL, paused_by=NULL,
                    total_paused_seconds=total_paused_seconds+?
                WHERE id=?
                """,
                (paused_seconds, batch_id),
            )
        self.db.audit(
            "batch.resume",
            "batch",
            batch_id,
            user_id,
            {"name": batch["name"], "paused_seconds": paused_seconds},
        )
        logger.info(
            "Batch resumed",
            extra={"context": {"batch_id": batch_id, "paused_seconds": paused_seconds}},
        )
        return self.get_batch(batch_id)

    def cancel_batch(self, batch_id: str, user_id: int) -> dict[str, Any]:
        """Stop remaining work while retaining records for any Pixel copies."""
        now = utcnow()
        with self.db.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise DomainError("batch_not_found", "Batch was not found", status_code=404)
            if batch["cancelled_at"]:
                return self.get_batch(batch_id)
            if batch["confirmed_at"] or batch["purged_at"]:
                raise DomainError(
                    "batch_cancel_unsafe",
                    "A confirmed or purged batch cannot be cancelled",
                    status_code=409,
                )
            items = connection.execute(
                "SELECT id, state, attempts FROM batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE batches
                SET cancelled_at=?, cancelled_by=?, paused_at=NULL, paused_by=NULL
                WHERE id=?
                """,
                (now, user_id, batch_id),
            )
            for item in items:
                current = ItemState(item["state"])
                if current == ItemState.TRANSFERRING:
                    # The worker owns this item until its active ADB/FTP operation returns.
                    continue
                no_pixel_copy = item["attempts"] == 0 and current in {
                    ItemState.QUEUED,
                    ItemState.DEVICE_OFFLINE,
                    ItemState.STORAGE_MISSING,
                    ItemState.TEMPERATURE_PAUSED,
                    ItemState.TRANSFER_FAILED,
                }
                target = ItemState.CANCELLED if no_pixel_copy else ItemState.CANCELLED_ON_PIXEL
                if current in {ItemState.CANCELLED, ItemState.CANCELLED_ON_PIXEL}:
                    continue
                if current in {
                    ItemState.CONFIRMED_BACKED_UP,
                    ItemState.PURGED_FROM_PIXEL,
                }:
                    raise DomainError(
                        "batch_cancel_unsafe",
                        "A batch containing confirmed or purged items cannot be cancelled",
                        status_code=409,
                    )
                connection.execute(
                    """
                    UPDATE batch_items
                    SET state=?, resume_state=NULL, error_code=NULL, error_detail=NULL,
                        updated_at=?
                    WHERE id=?
                    """,
                    (target, now, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO state_events(item_id, from_state, to_state, detail, created_at)
                    VALUES (?, ?, ?, 'Batch cancelled by administrator', ?)
                    """,
                    (item["id"], current, target, now),
                )
        self.db.audit(
            "batch.cancel",
            "batch",
            batch_id,
            user_id,
            {"name": batch["name"], "file_count": len(items)},
        )
        logger.info(
            "Batch cancelled",
            extra={"context": {"batch_id": batch_id, "item_count": len(items)}},
        )
        return self.get_batch(batch_id)

    def batch_cancelled(self, batch_id: str) -> bool:
        row = self.db.fetchone("SELECT cancelled_at FROM batches WHERE id=?", (batch_id,))
        return bool(row and row["cancelled_at"])

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            """
            SELECT batch_items.*, source_files.path, source_files.sha256, source_files.size,
              source_files.mtime_ns, source_files.extension, source_files.media_kind
            FROM batch_items
            JOIN source_files ON source_files.id=batch_items.source_file_id
            WHERE batch_items.id=?
            """,
            (item_id,),
        )

    def next_work_item(
        self,
        *,
        available_bytes: int | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return queued work, starting a fresh batch only when its full payload fits."""
        row = self.db.fetchone(
            """
            SELECT batch_items.id
            FROM batch_items
            JOIN batches ON batches.id=batch_items.batch_id
            WHERE batch_items.state=? AND batches.cancelled_at IS NULL
              AND batches.paused_at IS NULL
              AND (? IS NULL OR batches.id=?)
              AND (
                batches.series_id IS NULL
                OR NOT EXISTS (
                  SELECT 1
                  FROM batches AS earlier
                  WHERE earlier.series_id=batches.series_id
                    AND earlier.series_index < batches.series_index
                    AND EXISTS (
                      SELECT 1
                      FROM batch_items AS earlier_items
                      WHERE earlier_items.batch_id=earlier.id
                        AND earlier_items.state NOT IN (
                          'awaiting_backup_confirmation',
                          'confirmed_backed_up',
                          'cancelled',
                          'purged_from_pixel'
                        )
                    )
                )
              )
              AND (
                ? IS NULL
                OR EXISTS (
                  SELECT 1
                  FROM batch_items AS started_items
                  WHERE started_items.batch_id=batches.id
                    AND started_items.state <> 'queued'
                )
                OR (
                  SELECT COALESCE(SUM(pending_sources.size), 0)
                  FROM batch_items AS pending_items
                  JOIN source_files AS pending_sources
                    ON pending_sources.id=pending_items.source_file_id
                  WHERE pending_items.batch_id=batches.id
                    AND pending_items.state='queued'
                ) <= ?
              )
            ORDER BY batch_items.updated_at, batch_items.id LIMIT 1
            """,
            (ItemState.QUEUED, batch_id, batch_id, available_bytes, available_bytes),
        )
        return self.get_item(row["id"]) if row else None

    def transition(
        self,
        item_id: str,
        target: ItemState,
        *,
        detail: str | None = None,
        error_code: str | None = None,
        resume_state: ItemState | None = None,
    ) -> None:
        with self.db.transaction() as connection:
            item = connection.execute(
                "SELECT state FROM batch_items WHERE id=?", (item_id,)
            ).fetchone()
            if not item:
                raise DomainError("item_not_found", "Queue item was not found", status_code=404)
            current = item["state"]
            if current != target and not can_transition(current, target):
                raise DomainError(
                    "invalid_transition",
                    f"Cannot transition {current} to {target}",
                    status_code=409,
                )
            connection.execute(
                """
                UPDATE batch_items
                SET state=?, resume_state=?, error_code=?, error_detail=?,
                    attempts=attempts+?, updated_at=?
                WHERE id=?
                """,
                (
                    target,
                    resume_state,
                    error_code,
                    detail if error_code else None,
                    1 if target == ItemState.TRANSFERRING else 0,
                    utcnow(),
                    item_id,
                ),
            )
            if target == ItemState.TRANSFERRING:
                connection.execute(
                    """
                    UPDATE batch_items
                    SET transfer_bytes=0, transfer_updated_at=?
                    WHERE id=?
                    """,
                    (utcnow(), item_id),
                )
            elif target in {
                ItemState.STAGED_ON_PIXEL,
                ItemState.AWAITING_BACKUP_CONFIRMATION,
                ItemState.CONFIRMED_BACKED_UP,
                ItemState.PURGED_FROM_PIXEL,
            }:
                connection.execute(
                    """
                    UPDATE batch_items
                    SET transfer_bytes=transfer_total_bytes, transfer_updated_at=?
                    WHERE id=?
                    """,
                    (utcnow(), item_id),
                )
            connection.execute(
                """
                INSERT INTO state_events(item_id, from_state, to_state, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, current, target, detail, utcnow()),
            )
        logger.info(
            "Queue item state changed",
            extra={
                "context": {
                    "item_id": item_id,
                    "from_state": current,
                    "to_state": target,
                    "error_code": error_code,
                    "resume_state": resume_state,
                    "detail": detail,
                }
            },
        )

    def mark_items_purged(self, item_ids: list[str]) -> None:
        """Record a verified batch-directory removal in one transaction."""
        unique_ids = list(dict.fromkeys(item_ids))
        if not unique_ids:
            return
        now = utcnow()
        transitions: list[tuple[str, str]] = []
        with self.db.transaction() as connection:
            for item_id in unique_ids:
                item = connection.execute(
                    "SELECT state FROM batch_items WHERE id=?", (item_id,)
                ).fetchone()
                if not item:
                    raise DomainError("item_not_found", "Queue item was not found", status_code=404)
                current = item["state"]
                if not can_transition(current, ItemState.PURGED_FROM_PIXEL):
                    raise DomainError(
                        "invalid_transition",
                        f"Cannot transition {current} to {ItemState.PURGED_FROM_PIXEL}",
                        status_code=409,
                    )
                transitions.append((item_id, current))
            connection.executemany(
                """
                UPDATE batch_items
                SET state=?, resume_state=NULL, error_code=NULL, error_detail=NULL,
                    transfer_bytes=transfer_total_bytes, transfer_updated_at=?, updated_at=?
                WHERE id=?
                """,
                [
                    (ItemState.PURGED_FROM_PIXEL, now, now, item_id)
                    for item_id, _current in transitions
                ],
            )
            connection.executemany(
                """
                INSERT INTO state_events(item_id, from_state, to_state, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        item_id,
                        current,
                        ItemState.PURGED_FROM_PIXEL,
                        "Pixel copy removed with verified batch directory",
                        now,
                    )
                    for item_id, current in transitions
                ],
            )
        logger.info(
            "Queue items marked as purged",
            extra={
                "context": {
                    "item_count": len(transitions),
                    "to_state": ItemState.PURGED_FROM_PIXEL,
                }
            },
        )

    def update_transfer_progress(
        self,
        item_id: str,
        transferred_bytes: int,
        total_bytes: int,
    ) -> None:
        total = max(0, total_bytes)
        transferred = min(total, max(0, transferred_bytes))
        self.db.execute(
            """
            UPDATE batch_items
            SET transfer_bytes=?, transfer_total_bytes=?, transfer_updated_at=?
            WHERE id=? AND state='transferring'
            """,
            (transferred, total, utcnow(), item_id),
        )

    def retry_batch(self, batch_id: str, include_purge_failures: bool, user_id: int) -> int:
        batch = self.get_batch(batch_id)
        states = {
            ItemState.TRANSFER_FAILED,
            ItemState.MEDIA_SCAN_FAILED,
            ItemState.DEVICE_OFFLINE,
            ItemState.STORAGE_MISSING,
            ItemState.TEMPERATURE_PAUSED,
        }
        if include_purge_failures:
            states.add(ItemState.PURGE_FAILED)
        count = 0
        for item in batch["items"]:
            state = ItemState(item["state"])
            if state not in states:
                continue
            target = (
                ItemState.CANCELLED_ON_PIXEL
                if item.get("resume_state") == ItemState.CANCELLED_ON_PIXEL
                else ItemState.CONFIRMED_BACKED_UP
                if state == ItemState.PURGE_FAILED
                or item.get("resume_state") == ItemState.CONFIRMED_BACKED_UP
                else ItemState.QUEUED
            )
            self.transition(item["id"], target, detail="Manual retry")
            count += 1
        self.db.audit("batch.retry", "batch", batch_id, user_id, {"items": count})
        return count

    def confirm_batch(
        self,
        batch_id: str,
        user_id: int,
    ) -> dict[str, Any]:
        batch = self.get_batch(batch_id)
        if not batch["items"] or any(
            item["state"] != ItemState.AWAITING_BACKUP_CONFIRMATION for item in batch["items"]
        ):
            raise DomainError(
                "batch_not_ready",
                "Every batch item must await backup confirmation",
                status_code=409,
            )
        now = utcnow()
        detail = "Administrator verified batch in Google Photos"
        with self.db.transaction() as connection:
            for item in batch["items"]:
                current = connection.execute(
                    "SELECT state FROM batch_items WHERE id=?", (item["id"],)
                ).fetchone()
                if not current or current["state"] != ItemState.AWAITING_BACKUP_CONFIRMATION:
                    raise DomainError(
                        "batch_changed",
                        "Batch state changed during confirmation; reload and verify again",
                        status_code=409,
                    )
                connection.execute(
                    """
                    UPDATE batch_items
                    SET state=?, resume_state=NULL, error_code=NULL, error_detail=NULL,
                        updated_at=?
                    WHERE id=?
                    """,
                    (ItemState.CONFIRMED_BACKED_UP, now, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO state_events(
                      item_id, from_state, to_state, detail, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        ItemState.AWAITING_BACKUP_CONFIRMATION,
                        ItemState.CONFIRMED_BACKED_UP,
                        detail,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE batches SET confirmed_at=?, confirmed_by=? WHERE id=?",
                (now, user_id, batch_id),
            )
            connection.execute(
                """
                INSERT INTO audit_log(
                  user_id, action, target_type, target_id, detail_json, created_at
                ) VALUES (?, ?, 'batch', ?, ?, ?)
                """,
                (
                    user_id,
                    "batch.confirm",
                    batch_id,
                    "{}",
                    now,
                ),
            )
        return self.get_batch(batch_id)

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.fetchone("SELECT value FROM app_settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(
        self,
        key: str,
        value: str,
        user_id: int | None = None,
        *,
        sensitive: bool = False,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, utcnow()),
        )
        self.db.audit(
            "setting.update",
            "setting",
            key,
            user_id,
            {"value": "[redacted]" if sensitive else value},
        )

    def expected_uuid(self) -> str:
        return self.setting("expected_primary_uuid", self.settings.expected_primary_uuid)

    def queue_summary(self) -> dict[str, Any]:
        rows = self.db.fetchall("SELECT state, COUNT(*) AS count FROM batch_items GROUP BY state")
        last = self.db.fetchone(
            """
            SELECT id, name, confirmed_at FROM batches
            WHERE confirmed_at IS NOT NULL ORDER BY confirmed_at DESC LIMIT 1
            """
        )
        return {
            "states": {row["state"]: row["count"] for row in rows},
            "last_confirmed_upload": last,
        }

    def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT audit_log.*, users.username
            FROM audit_log LEFT JOIN users ON users.id=audit_log.user_id
            ORDER BY audit_log.id DESC LIMIT ?
            """,
            (min(max(limit, 1), 1000),),
        )
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json"))
        return rows

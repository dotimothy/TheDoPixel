from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

audit_logger = logging.getLogger("pixel_relay.audit")


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_roots (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
  id INTEGER PRIMARY KEY,
  root_id INTEGER REFERENCES source_roots(id),
  path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  extension TEXT NOT NULL,
  media_kind TEXT NOT NULL DEFAULT 'photo' CHECK(media_kind IN ('photo', 'video')),
  discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_files_sha256_idx ON source_files(sha256);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  confirmed_at TEXT,
  confirmed_by INTEGER REFERENCES users(id),
  purged_at TEXT,
  cancelled_at TEXT,
  cancelled_by INTEGER REFERENCES users(id),
  paused_at TEXT,
  paused_by INTEGER REFERENCES users(id),
  total_paused_seconds INTEGER NOT NULL DEFAULT 0,
  series_id TEXT,
  series_index INTEGER,
  series_total INTEGER,
  planned_capacity_bytes INTEGER,
  split_reason TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
  source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE RESTRICT,
  state TEXT NOT NULL,
  resume_state TEXT,
  remote_path TEXT NOT NULL,
  error_code TEXT,
  error_detail TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  transfer_bytes INTEGER NOT NULL DEFAULT 0,
  transfer_total_bytes INTEGER NOT NULL DEFAULT 0,
  transfer_updated_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(batch_id, source_file_id)
);
CREATE INDEX IF NOT EXISTS batch_items_state_idx ON batch_items(state, updated_at);
CREATE TABLE IF NOT EXISTS state_events (
  id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL REFERENCES batch_items(id) ON DELETE RESTRICT,
  from_state TEXT,
  to_state TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_samples (
  id INTEGER PRIMARY KEY,
  status_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, contextlib.closing(self.connect()) as connection:
            connection.executescript(SCHEMA)
            version_row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            previous_version = int(version_row["version"] or 0)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(source_files)").fetchall()
            }
            if "media_kind" not in columns:
                connection.execute(
                    "ALTER TABLE source_files ADD COLUMN media_kind TEXT NOT NULL DEFAULT 'photo'"
                )
            batch_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(batches)").fetchall()
            }
            if "cancelled_at" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN cancelled_at TEXT")
            if "cancelled_by" not in batch_columns:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN cancelled_by INTEGER REFERENCES users(id)"
                )
            if "paused_at" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN paused_at TEXT")
            if "paused_by" not in batch_columns:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN paused_by INTEGER REFERENCES users(id)"
                )
            if "total_paused_seconds" not in batch_columns:
                connection.execute(
                    """
                    ALTER TABLE batches
                    ADD COLUMN total_paused_seconds INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "series_id" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN series_id TEXT")
            if "series_index" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN series_index INTEGER")
            if "series_total" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN series_total INTEGER")
            if "planned_capacity_bytes" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN planned_capacity_bytes INTEGER")
            if "split_reason" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN split_reason TEXT")
            item_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(batch_items)").fetchall()
            }
            if "transfer_bytes" not in item_columns:
                connection.execute(
                    "ALTER TABLE batch_items ADD COLUMN transfer_bytes INTEGER NOT NULL DEFAULT 0"
                )
            if "transfer_total_bytes" not in item_columns:
                connection.execute(
                    """
                    ALTER TABLE batch_items
                    ADD COLUMN transfer_total_bytes INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "transfer_updated_at" not in item_columns:
                connection.execute("ALTER TABLE batch_items ADD COLUMN transfer_updated_at TEXT")
            connection.execute(
                """
                UPDATE batch_items
                SET transfer_total_bytes=COALESCE(
                  (SELECT size FROM source_files WHERE source_files.id=batch_items.source_file_id),
                  transfer_total_bytes
                )
                """
            )
            connection.execute(
                """
                UPDATE batch_items
                SET transfer_bytes=transfer_total_bytes
                WHERE state IN (
                  'staged_on_pixel',
                  'awaiting_backup_confirmation',
                  'confirmed_backed_up',
                  'purged_from_pixel',
                  'cancelled_on_pixel'
                )
                """
            )
            connection.execute(
                """
                UPDATE source_files SET media_kind='video'
                WHERE lower(extension) IN ('.mp4','.mov','.m4v','.avi','.mkv','.3gp')
                """
            )
            if previous_version < 12:
                # Carry installations that merely persisted the old defaults
                # forward without overwriting an operator's custom limits.
                connection.execute(
                    """
                    UPDATE app_settings
                    SET value='6000', updated_at=?
                    WHERE key='max_batch_files' AND value='1000'
                    """,
                    (utcnow(),),
                )
                connection.execute(
                    """
                    UPDATE app_settings
                    SET value='429496729600', updated_at=?
                    WHERE key='max_batch_bytes' AND value='53687091200'
                    """,
                    (utcnow(),),
                )
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version(version) VALUES (12)")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def backup_to(self, destination: Path) -> None:
        """Create a consistent online SQLite snapshot."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            self._lock,
            contextlib.closing(self.connect()) as source,
            contextlib.closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
        destination.chmod(0o600)

    def audit(
        self,
        action: str,
        target_type: str,
        target_id: str | None = None,
        user_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        audit_id = self.execute(
            """
            INSERT INTO audit_log(user_id, action, target_type, target_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, action, target_type, target_id, json.dumps(detail or {}), utcnow()),
        )
        audit_logger.info(
            "Audit event recorded",
            extra={
                "context": {
                    "audit_id": audit_id,
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "user_id": user_id,
                }
            },
        )
        return audit_id

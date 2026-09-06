import sqlite3
from pathlib import Path

from pixel_relay.config import Settings
from pixel_relay.database import Database


def test_v1_database_migrates_and_classifies_existing_videos(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1);
        CREATE TABLE source_roots (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          path TEXT NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE source_files (
          id INTEGER PRIMARY KEY,
          root_id INTEGER REFERENCES source_roots(id),
          path TEXT NOT NULL UNIQUE,
          sha256 TEXT NOT NULL,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          extension TEXT NOT NULL,
          discovered_at TEXT NOT NULL
        );
        INSERT INTO source_roots(id,name,path,created_at)
          VALUES (1,'Archive','/archive','now');
        INSERT INTO source_files(
          root_id,path,sha256,size,mtime_ns,extension,discovered_at
        ) VALUES (1,'/archive/clip.mp4','abc',3,1,'.mp4','now');
        """
    )
    connection.close()

    database = Database(path)
    database.migrate()
    video = database.fetchone("SELECT media_kind FROM source_files WHERE id=1")
    version = database.fetchone("SELECT version FROM schema_version")
    assert video == {"media_kind": "video"}
    assert version == {"version": 13}


def test_default_batch_limits_are_6000_files_and_400_gib(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)

    assert settings.max_batch_files == 6_000
    assert settings.max_batch_bytes == 400 * 1024**3


def test_existing_database_adds_source_missing_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "existing.sqlite3")
    database.migrate()
    database.execute("DELETE FROM schema_version")
    database.execute("INSERT INTO schema_version(version) VALUES (12)")

    database.migrate()

    columns = {row["name"] for row in database.fetchall("PRAGMA table_info(source_files)")}
    assert "missing_at" in columns
    assert database.fetchone("SELECT version FROM schema_version") == {"version": 13}


def test_v11_default_batch_limits_migrate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "limits.sqlite3"
    database = Database(path)
    database.migrate()
    database.execute("DELETE FROM schema_version")
    database.execute("INSERT INTO schema_version(version) VALUES (11)")
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
        ("max_batch_files", "1000", "old"),
    )
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
        ("max_batch_bytes", "53687091200", "old"),
    )
    database.migrate()

    assert database.fetchone("SELECT value FROM app_settings WHERE key='max_batch_files'") == {
        "value": "6000"
    }
    assert database.fetchone("SELECT value FROM app_settings WHERE key='max_batch_bytes'") == {
        "value": "429496729600"
    }


def test_v11_custom_batch_limits_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "custom-limits.sqlite3"
    database = Database(path)
    database.migrate()
    database.execute("DELETE FROM schema_version")
    database.execute("INSERT INTO schema_version(version) VALUES (11)")
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
        ("max_batch_files", "2222", "old"),
    )
    database.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
        ("max_batch_bytes", "123456789", "old"),
    )

    database.migrate()

    assert database.fetchone("SELECT value FROM app_settings WHERE key='max_batch_files'") == {
        "value": "2222"
    }
    assert database.fetchone("SELECT value FROM app_settings WHERE key='max_batch_bytes'") == {
        "value": "123456789"
    }


def test_new_database_does_not_create_picker_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3")
    database.migrate()

    tables = {
        row["name"]
        for row in database.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    }

    assert "google_oauth_states" not in tables
    assert "google_picker_sessions" not in tables
    assert "google_photos_verifications" not in tables
    assert "google_picker_api_requests" not in tables

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import uvicorn
from pixel_relay import cli
from pixel_relay.cli import (
    clean_database_history,
    clean_runtime_files,
    create_backup_archive,
    dashboard_url,
    restore_backup_archive,
)
from pixel_relay.config import Settings
from pixel_relay.database import Database
from pixel_relay.events import EventBroker


def test_default_host_accepts_lan_connections() -> None:
    assert Settings.model_fields["host"].default == "0.0.0.0"


def test_dashboard_url_uses_loopback_for_wildcard_bind() -> None:
    assert dashboard_url("0.0.0.0", 8741) == (
        "http://127.0.0.1:8741",
        "127.0.0.1",
    )


def test_dashboard_url_preserves_specific_bind_host() -> None:
    assert dashboard_url("192.168.1.20", 9000) == (
        "http://192.168.1.20:9000",
        "192.168.1.20",
    )


def test_dashboard_origins_include_both_loopback_names() -> None:
    assert cli.dashboard_origins("127.0.0.1", 8741) == (
        "http://127.0.0.1:8741",
        "http://localhost:8741",
    )


def test_close_dashboard_tabs_passes_only_matching_origins_to_macos(
    monkeypatch,
) -> None:
    captured: list[str] = []

    def run(command, **_kwargs):
        captured.extend(command)
        return SimpleNamespace(returncode=0, stdout="3\n")

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda _command: "/usr/bin/osascript")
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.close_dashboard_tabs("127.0.0.1", 8741) == 3
    assert captured[-2:] == [
        "http://127.0.0.1:8741",
        "http://localhost:8741",
    ]
    assert "accounts.google.com" not in captured


@pytest.mark.asyncio
async def test_server_notifies_sse_before_beginning_shutdown() -> None:
    async def application(_scope, _receive, _send):
        return None

    events = EventBroker()
    server = cli.TheDoPixelServer(
        uvicorn.Config(application, timeout_graceful_shutdown=2),
        events,
    )

    server.handle_exit(signal.SIGINT, None)

    assert events.shutdown_requested is True
    assert server.should_exit is False
    await asyncio.sleep(0.3)
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_server_accepts_shutdown_request_without_os_signal() -> None:
    async def application(_scope, _receive, _send):
        return None

    events = EventBroker()
    server = cli.TheDoPixelServer(
        uvicorn.Config(application, timeout_graceful_shutdown=2),
        events,
    )

    server.request_shutdown()

    assert events.shutdown_requested is True
    assert server.should_exit is False
    await asyncio.sleep(0.3)
    assert server.should_exit is True


def test_clean_removes_only_known_runtime_files(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    database = settings.database_path
    targets = [
        settings.log_path,
        database,
        tmp_path / "pixel-relay.sqlite3-wal",
        tmp_path / "pixel-relay.sqlite3-shm",
    ]
    for target in targets:
        target.write_text("runtime")
    original = tmp_path / "original.jpg"
    original.write_text("authoritative media")

    assert clean_runtime_files(settings) == targets
    assert all(not target.exists() for target in targets)
    assert original.read_text() == "authoritative media"


def test_history_clean_preserves_recent_database_records(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    database = Database(settings.database_path)
    database.migrate()
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    recent = datetime.now(UTC).isoformat()
    database.execute(
        "INSERT INTO device_samples(status_json, created_at) VALUES (?, ?)",
        ("{}", old),
    )
    database.execute(
        "INSERT INTO device_samples(status_json, created_at) VALUES (?, ?)",
        ("{}", recent),
    )
    database.execute(
        """
        INSERT INTO audit_log(action, target_type, detail_json, created_at)
        VALUES ('old', 'test', '{}', ?)
        """,
        (old,),
    )
    result = clean_database_history(settings, 30)

    assert result["device_samples"] == 1
    assert result["audit_records"] == 1
    assert len(database.fetchall("SELECT * FROM device_samples")) == 1


def test_backup_restore_round_trip_preserves_database_state(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    database = Database(settings.database_path)
    database.migrate()
    database.execute(
        "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
        ("destination_root", "/sdcard/original", datetime.now(UTC).isoformat()),
    )

    archive = create_backup_archive(settings)
    database.execute(
        "UPDATE app_settings SET value=? WHERE key=?",
        ("/sdcard/changed", "destination_root"),
    )
    rollback = restore_backup_archive(settings, archive)

    assert archive.is_file()
    assert rollback and rollback.is_file()
    restored = Database(settings.database_path).fetchone(
        "SELECT value FROM app_settings WHERE key=?",
        ("destination_root",),
    )
    assert restored == {"value": "/sdcard/original"}

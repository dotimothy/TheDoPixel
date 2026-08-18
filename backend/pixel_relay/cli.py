from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
import uvicorn

from .api import create_app
from .auth import AuthService
from .config import apply_persisted_settings, get_settings
from .database import Database
from .events import EventBroker
from .repository import Repository
from .transport import DeviceTransport

app = typer.Typer(help="TheDoPixel local media appliance")
admin_app = typer.Typer(help="Administrator management")
device_app = typer.Typer(help="Read-only Pixel status commands")
source_app = typer.Typer(help="Source discovery commands")
batch_app = typer.Typer(help="Batch inspection commands")
config_app = typer.Typer(help="Persistent appliance configuration")
backup_app = typer.Typer(help="Create and restore local state backups")
app.add_typer(admin_app, name="admin")
app.add_typer(device_app, name="device")
app.add_typer(source_app, name="source")
app.add_typer(batch_app, name="batch")
app.add_typer(config_app, name="config")
app.add_typer(backup_app, name="backup")


class CleanLevel(StrEnum):
    LOGS = "logs"
    HISTORY = "history"
    RESET = "reset"


class TheDoPixelServer(uvicorn.Server):
    """Let SSE clients close before Uvicorn begins draining connections."""

    def __init__(self, config: uvicorn.Config, events: EventBroker):
        super().__init__(config)
        self.events = events
        self._shutdown_notice_sent = False

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._captured_signals.append(sig)
        if self._shutdown_notice_sent:
            self.should_exit = True
            if sig == signal.SIGINT:
                self.force_exit = True
            return

        self._shutdown_notice_sent = True
        self.events.request_shutdown()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.should_exit = True
        else:
            # Give browsers enough time to receive the shutdown event and close
            # their SSE connection before Uvicorn inspects active connections.
            loop.call_later(0.25, self._begin_shutdown)

    def _begin_shutdown(self) -> None:
        self.should_exit = True


def dashboard_url(host: str, port: int) -> tuple[str, str]:
    """Return the browser URL and locally reachable host for a bind address."""
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    url_host = f"[{browser_host}]" if ":" in browser_host else browser_host
    return f"http://{url_host}:{port}", browser_host


def open_browser_when_ready(host: str, port: int, timeout_seconds: float = 30) -> None:
    """Open the dashboard after the server begins accepting local connections."""
    url, connect_host = dashboard_url(host, port)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((connect_host, port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)
    typer.echo(f"Dashboard did not become reachable at {url}", err=True)


_CLOSE_TABS_JXA = r"""
function run(argv) {
  const targets = argv;
  const browserNames = [
    "Safari",
    "Google Chrome",
    "Microsoft Edge",
    "Brave Browser",
    "Chromium"
  ];
  const systemEvents = Application("System Events");
  let closed = 0;

  function isTheDoPixelUrl(url) {
    if (typeof url !== "string") return false;
    return targets.some((target) =>
      url === target
      || url.startsWith(target + "/")
      || url.startsWith(target + "?")
      || url.startsWith(target + "#")
    );
  }

  browserNames.forEach((browserName) => {
    try {
      if (!systemEvents.processes.byName(browserName).exists()) return;
      const browser = Application(browserName);
      browser.windows().forEach((browserWindow) => {
        browserWindow.tabs().slice().reverse().forEach((tab) => {
          try {
            if (isTheDoPixelUrl(tab.url())) {
              tab.close();
              closed += 1;
            }
          } catch (_) {
            // A tab or window may disappear while shutdown is in progress.
          }
        });
      });
    } catch (_) {
      // Continue with other installed browsers when one denies automation.
    }
  });
  return String(closed);
}
"""


def dashboard_origins(host: str, port: int) -> tuple[str, ...]:
    """Return exact browser origins that may represent this local dashboard."""
    url, browser_host = dashboard_url(host, port)
    origins = {url}
    if browser_host in {"127.0.0.1", "localhost"}:
        origins.update(
            {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            }
        )
    return tuple(sorted(origins))


def close_dashboard_tabs(host: str, port: int) -> int:
    """Close TheDoPixel dashboard tabs on macOS without touching other URLs."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return 0
    try:
        result = subprocess.run(
            [
                "osascript",
                "-l",
                "JavaScript",
                "-e",
                _CLOSE_TABS_JXA,
                "--",
                *dashboard_origins(host, port),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or 0)
    except ValueError:
        return 0


def cleanup_targets(settings) -> tuple[Path, ...]:
    """Return the exact local runtime files eligible for cleanup."""
    database = settings.database_path
    return (
        *log_targets(settings),
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    )


def log_targets(settings) -> tuple[Path, ...]:
    """Return the active structured log and its bounded rotation backups."""
    return (
        settings.log_path,
        *(Path(f"{settings.log_path}.{index}") for index in range(1, 6)),
    )


def remove_runtime_files(settings, targets: tuple[Path, ...]) -> list[Path]:
    """Remove only exact, known files inside TheDoPixel's data directory."""
    removed: list[Path] = []
    data_dir = settings.data_dir.resolve()
    for target in targets:
        resolved = target.resolve(strict=False)
        if resolved.parent != data_dir:
            raise ValueError(f"Refusing to clean a path outside the data directory: {target}")
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except IsADirectoryError as exc:
            raise ValueError(f"Refusing to remove unexpected directory: {target}") from exc
        removed.append(target)
    return removed


def clean_runtime_files(settings) -> list[Path]:
    """Remove all known TheDoPixel logs and SQLite files."""
    return remove_runtime_files(settings, cleanup_targets(settings))


def clean_log_files(settings) -> list[Path]:
    """Remove only TheDoPixel's active and rotated service logs."""
    return remove_runtime_files(settings, log_targets(settings))


def backup_directory(settings) -> Path:
    return settings.data_dir / "backups"


def create_backup_archive(settings, output: Path | None = None) -> Path:
    """Create an atomic archive containing a consistent SQLite snapshot."""
    settings.prepare()
    if not settings.database_path.is_file():
        raise FileNotFoundError("TheDoPixel database does not exist")
    destination_dir = backup_directory(settings)
    destination_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC)
    destination = (
        output.expanduser().resolve()
        if output
        else destination_dir / f"pixel-relay-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.zip"
    )
    if destination.exists():
        raise FileExistsError(f"Backup already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pixel-relay-backup-", dir=settings.data_dir) as raw:
        temporary = Path(raw)
        snapshot = temporary / "pixel-relay.sqlite3"
        Database(settings.database_path).backup_to(snapshot)
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        manifest = {
            "format": 1,
            "created_at": created_at.isoformat(),
            "database_sha256": digest,
            "includes": ["users", "settings", "sources", "batches", "queue", "audit"],
            "source_media_included": False,
            "pixel_media_included": False,
            "environment_overrides_included": False,
        }
        archive_temporary = temporary / destination.name
        with zipfile.ZipFile(
            archive_temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.write(snapshot, "pixel-relay.sqlite3")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        os.replace(archive_temporary, destination)
    destination.chmod(0o600)
    return destination


def restore_backup_archive(settings, archive_path: Path) -> Path | None:
    """Validate and atomically restore a backup, returning the rollback archive."""
    archive_path = archive_path.expanduser().resolve(strict=True)
    if not archive_path.is_file():
        raise ValueError("Backup path must be a file")
    settings.prepare()
    rollback = create_backup_archive(settings) if settings.database_path.is_file() else None
    try:
        with tempfile.TemporaryDirectory(
            prefix="pixel-relay-restore-", dir=settings.data_dir
        ) as raw:
            temporary = Path(raw)
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                if "pixel-relay.sqlite3" not in names or "manifest.json" not in names:
                    raise ValueError("Backup archive is missing its database or manifest")
                manifest = json.loads(archive.read("manifest.json"))
                database_bytes = archive.read("pixel-relay.sqlite3")
            digest = hashlib.sha256(database_bytes).hexdigest()
            if digest != manifest.get("database_sha256"):
                raise ValueError("Backup database checksum does not match its manifest")
            candidate = temporary / "pixel-relay.sqlite3"
            candidate.write_bytes(database_bytes)
            candidate.chmod(0o600)
            with contextlib.closing(sqlite3.connect(candidate)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("Backup database failed SQLite integrity validation")
            replacement = settings.data_dir / ".pixel-relay.restore.sqlite3"
            shutil.copy2(candidate, replacement)
            replacement.chmod(0o600)
            os.replace(replacement, settings.database_path)
            for sidecar in (
                Path(f"{settings.database_path}-wal"),
                Path(f"{settings.database_path}-shm"),
            ):
                with contextlib.suppress(FileNotFoundError):
                    sidecar.unlink()
            Database(settings.database_path).migrate()
    except Exception:
        if rollback:
            typer.echo(f"Restore failed; current-state rollback backup: {rollback}", err=True)
        raise
    return rollback


def clean_database_history(settings, older_than_days: int) -> dict[str, int]:
    """Prune old non-current history while preserving active queue state."""
    if not settings.database_path.is_file():
        return {
            "expired_sessions": 0,
            "device_samples": 0,
            "audit_records": 0,
            "purged_batch_events": 0,
        }
    database = Database(settings.database_path)
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=older_than_days)
    deleted: dict[str, int] = {}
    with database.transaction() as connection:
        deleted["expired_sessions"] = connection.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (now.isoformat(),),
        ).rowcount
        deleted["device_samples"] = connection.execute(
            "DELETE FROM device_samples WHERE created_at < ?",
            (cutoff.isoformat(),),
        ).rowcount
        deleted["audit_records"] = connection.execute(
            "DELETE FROM audit_log WHERE created_at < ?",
            (cutoff.isoformat(),),
        ).rowcount
        deleted["purged_batch_events"] = connection.execute(
            """
            DELETE FROM state_events
            WHERE created_at < ?
              AND item_id IN (
                SELECT batch_items.id
                FROM batch_items
                JOIN batches ON batches.id = batch_items.batch_id
                WHERE batches.purged_at IS NOT NULL
              )
            """,
            (cutoff.isoformat(),),
        ).rowcount
    with contextlib.closing(database.connect()) as connection:
        connection.execute("VACUUM")
    return deleted


def services() -> tuple[Database, Repository, AuthService]:
    settings = get_settings()
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    repository = Repository(db, settings)
    apply_persisted_settings(settings, repository.setting)
    return db, repository, AuthService(db, settings)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override bind host"),
    port: int | None = typer.Option(None, help="Override bind port"),
    open_browser: bool = typer.Option(
        True,
        "--browser/--no-browser",
        help="Open the dashboard in the default browser after startup",
    ),
) -> None:
    """Start the API, worker, and bundled dashboard."""
    settings = get_settings()
    db, _repository, auth = services()
    if not auth.has_admin():
        if not typer.confirm("No administrator exists. Create one now?"):
            raise typer.Exit(2)
        username = typer.prompt("Administrator username", default="admin")
        password = getpass.getpass("Administrator password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            typer.echo("Passwords did not match", err=True)
            raise typer.Exit(2)
        try:
            user_id = auth.create_admin(username, password)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        db.audit("admin.create", "user", str(user_id), user_id, {"username": username})
    bind_host = host or settings.host
    bind_port = port or settings.port
    if open_browser:
        threading.Thread(
            target=open_browser_when_ready,
            args=(bind_host, bind_port),
            name="pixel-relay-browser",
            daemon=True,
        ).start()
    application = create_app(settings)
    server = TheDoPixelServer(
        uvicorn.Config(
            application,
            host=bind_host,
            port=bind_port,
            proxy_headers=settings.trusted_proxy,
            forwarded_allow_ips="127.0.0.1" if settings.trusted_proxy else "",
            timeout_graceful_shutdown=2,
        ),
        application.state.events,
    )
    try:
        server.run()
    finally:
        close_dashboard_tabs(bind_host, bind_port)


@app.command()
def clean(
    level: Annotated[
        CleanLevel,
        typer.Argument(help="Cleanup level: logs, history, or reset"),
    ] = CleanLevel.LOGS,
    older_than_days: int = typer.Option(
        30,
        "--older-than-days",
        min=1,
        max=3650,
        help="Retention window used by the history level",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation",
    ),
) -> None:
    """Clean logs, old database history, or all local state."""
    settings = get_settings()
    url, connect_host = dashboard_url(settings.host, settings.port)
    try:
        with socket.create_connection((connect_host, settings.port), timeout=0.3):
            typer.echo(
                f"TheDoPixel appears to be running at {url}. Stop it before cleaning.",
                err=True,
            )
            raise typer.Exit(2)
    except OSError:
        pass

    targets = cleanup_targets(settings) if level is CleanLevel.RESET else (settings.log_path,)
    existing = [path for path in targets if path.is_file()]
    if level is CleanLevel.HISTORY and settings.database_path.is_file():
        existing.append(settings.database_path)
    if not existing:
        typer.echo(f"Nothing to clean in {settings.data_dir}")
        return

    if level is CleanLevel.LOGS:
        typer.echo("Level: logs")
        typer.echo("This removes only the TheDoPixel service log.")
    elif level is CleanLevel.HISTORY:
        typer.echo("Level: history")
        typer.echo(
            f"This removes the service log and database history older than {older_than_days} days:"
        )
        typer.echo("  - device telemetry and audit records")
        typer.echo("  - state events belonging to already-purged batches")
        typer.echo("  - expired sessions, regardless of age")
        typer.echo("Users, settings, sources, batches, and active queue state are preserved.")
    else:
        typer.echo("Level: reset")
        typer.echo("This permanently resets TheDoPixel's local state:")
        typer.echo("  - users and sessions")
        typer.echo("  - settings, sources, batches, queue history, and audit records")
        typer.echo("  - service logs")
        typer.echo("Any staged Pixel copies will no longer be tracked by TheDoPixel.")
    typer.echo("Source originals and Pixel media will not be deleted.")
    if not yes and not typer.confirm(f"Run the {level.value} cleanup?"):
        typer.echo("Cleanup cancelled")
        return

    try:
        if level is CleanLevel.RESET:
            removed = clean_runtime_files(settings)
            history_counts = None
        elif level is CleanLevel.HISTORY:
            removed = clean_log_files(settings)
            history_counts = clean_database_history(settings, older_than_days)
        else:
            removed = clean_log_files(settings)
            history_counts = None
    except (OSError, ValueError) as exc:
        typer.echo(f"Cleanup failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for path in removed:
        typer.echo(f"Removed {path}")
    if history_counts is not None:
        for name, count in history_counts.items():
            typer.echo(f"Removed {count} {name.replace('_', ' ')}")
        typer.echo("Database vacuum complete.")
    if level is CleanLevel.RESET:
        typer.echo("The next serve will prompt for a new administrator.")
    typer.echo("Cleanup complete.")


@backup_app.command("create")
def backup_create(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional destination .zip path"),
    ] = None,
) -> None:
    """Create a consistent database and configuration backup."""
    settings = get_settings()
    try:
        archive = create_backup_archive(settings, output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        typer.echo(f"Backup failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Created {archive}")
    typer.echo("Source media and Pixel copies were not included.")


@backup_app.command("list")
def backup_list() -> None:
    """List locally retained backup archives."""
    settings = get_settings()
    directory = backup_directory(settings)
    archives = sorted(directory.glob("pixel-relay-*.zip"), reverse=True)
    if not archives:
        typer.echo(f"No backups in {directory}")
        return
    for archive in archives:
        modified = datetime.fromtimestamp(archive.stat().st_mtime, UTC).isoformat()
        typer.echo(f"{archive.name}\t{archive.stat().st_size} bytes\t{modified}")


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip restore confirmation"),
    ] = False,
) -> None:
    """Restore local state from a validated backup archive."""
    settings = get_settings()
    url, connect_host = dashboard_url(settings.host, settings.port)
    try:
        with socket.create_connection((connect_host, settings.port), timeout=0.3):
            typer.echo(
                f"TheDoPixel appears to be running at {url}. Stop it before restoring.",
                err=True,
            )
            raise typer.Exit(2)
    except OSError:
        pass
    typer.echo(f"Restore from: {archive.resolve()}")
    typer.echo("This replaces users, settings, sources, batches, queue state, and audit data.")
    typer.echo("Source media and Pixel copies are not changed.")
    if not yes and not typer.confirm("Restore this backup?"):
        typer.echo("Restore cancelled")
        return
    try:
        rollback = restore_backup_archive(settings, archive)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        typer.echo(f"Restore failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Restore complete.")
    if rollback:
        typer.echo(f"Pre-restore rollback backup: {rollback}")


@admin_app.command("init")
def admin_init(username: str = typer.Option("admin", prompt=True)) -> None:
    """Create the one local administrator."""
    db, _repository, auth = services()
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        typer.echo("Passwords did not match", err=True)
        raise typer.Exit(2)
    try:
        user_id = auth.create_admin(username, password)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    db.audit("admin.create", "user", str(user_id), user_id, {"username": username})
    typer.echo(f"Created administrator {username!r}")


@device_app.command("status")
def device_status() -> None:
    """Print a read-only JSON device snapshot."""
    _db, repository, _auth = services()
    snapshot = asyncio.run(
        DeviceTransport(repository.settings).snapshot(repository.expected_uuid())
    )
    typer.echo(json.dumps(snapshot.dict(), indent=2))


@source_app.command("list")
def source_list() -> None:
    """List configured source roots."""
    _db, repository, _auth = services()
    typer.echo(json.dumps(repository.list_roots(), indent=2))


@source_app.command("add")
def source_add(name: str, path: Path) -> None:
    """Add an allowlisted source directory."""
    db, repository, _auth = services()
    root = repository.add_root(name, str(path))
    db.audit("source.create_cli", "source_root", str(root["id"]))
    typer.echo(json.dumps(root, indent=2))


@source_app.command("scan")
def source_scan(
    root_id: int,
    full_verify: Annotated[
        bool,
        typer.Option("--full-verify", help="Recalculate SHA-256 for every media file."),
    ] = False,
) -> None:
    """Run an on-demand recursive scan."""
    db, repository, _auth = services()
    result = repository.scan_root(root_id, full_verify=full_verify)
    db.audit(
        "source.scan_cli",
        "source_root",
        str(root_id),
        detail={"discovered": len(result["files"])},
    )
    typer.echo(json.dumps(result, indent=2))


@batch_app.command("list")
def batch_list() -> None:
    """List batches and aggregate states."""
    _db, repository, _auth = services()
    typer.echo(json.dumps(repository.list_batches(), indent=2))


@config_app.command("storage-uuid")
def set_storage_uuid(uuid: str) -> None:
    """Set the adopted primary-storage UUID required for transfers."""
    db, repository, _auth = services()
    from .adb import SAFE_UUID

    value = uuid.strip()
    if not value or not SAFE_UUID.fullmatch(value):
        typer.echo("UUID contains invalid characters", err=True)
        raise typer.Exit(2)
    repository.set_setting("expected_primary_uuid", value)
    db.audit("setting.update_cli", "setting", "expected_primary_uuid")
    typer.echo(f"Expected primary-storage UUID set to {value}")


@config_app.command("connection-mode")
def set_connection_mode(mode: str) -> None:
    """Select network ADB or USB ADB for the appliance worker."""
    if mode not in {"network", "usb", "ftp"}:
        typer.echo("Mode must be 'network', 'usb', or 'ftp'", err=True)
        raise typer.Exit(2)
    db, repository, _auth = services()
    repository.set_setting("connection_mode", mode)
    db.audit("setting.update_cli", "setting", "connection_mode")
    typer.echo(f"Device connection mode set to {mode}")


@app.command()
def doctor() -> None:
    """Check local prerequisites without changing the Pixel."""
    settings = get_settings()
    db, repository, auth = services()
    checks = {
        "adb": shutil.which(settings.adb_path) is not None,
        "database": db.path.exists(),
        "administrator": auth.has_admin(),
        "frontend": (settings.frontend_dist / "index.html").is_file(),
        "expected_storage_uuid": bool(repository.expected_uuid()),
        "import_root": bool(settings.import_root and settings.import_root.is_dir()),
    }
    typer.echo(json.dumps(checks, indent=2))
    if not all(value for key, value in checks.items() if key != "import_root"):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

import asyncio
import json
import threading
import time
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pixel_relay import api as api_module
from pixel_relay import config as config_module
from pixel_relay.api import create_app
from pixel_relay.auth import AuthService
from pixel_relay.config import Settings
from pixel_relay.database import Database
from pixel_relay.states import ItemState


def configured_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "dist",
        device_poll_seconds=3600,
        worker_enabled=False,
    )
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "a secure local password")
    return create_app(settings)


def test_default_import_root_is_project_data_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, data_dir=tmp_path / "runtime")

    assert settings.import_root == tmp_path / "data"
    settings.prepare()
    assert settings.import_root.is_dir()


def test_default_data_directory_uses_local_app_data_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    assert config_module.default_data_dir() == tmp_path / "LocalAppData" / "PixelRelay"


def test_health_is_public_but_dashboard_is_private(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/dashboard").status_code == 401


def test_authenticated_user_can_request_graceful_server_shutdown(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    callbacks: list[str] = []
    app.state.shutdown_callback = lambda: callbacks.append("shutdown")

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/server/shutdown",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 202
    assert response.json() == {"shutdown_requested": True}
    assert callbacks == ["shutdown"]
    assert app.state.events.shutdown_requested is True


def test_server_shutdown_requires_runtime_control(tmp_path: Path) -> None:
    app = configured_app(tmp_path)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/server/shutdown",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "server_shutdown_unavailable"


def test_app_update_installs_and_restarts_when_git_downloads_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "frontend").mkdir()
    callbacks: list[str] = []
    commands: list[list[str]] = []
    app.state.restart_callback = lambda: callbacks.append("restart")
    monkeypatch.setattr(api_module, "application_root", lambda: checkout)
    monkeypatch.setattr(api_module.shutil, "which", lambda command: f"/tools/{command}")

    def run(command, **_kwargs):
        commands.append(command)
        stdout = "Updating abc..def\n" if command[:2] == ["git", "pull"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(api_module.subprocess, "run", run)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/app/update",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 200
    assert response.json()["restarting"] is True
    assert callbacks == ["restart"]
    assert commands == [
        ["git", "status", "--porcelain"],
        ["git", "pull", "--ff-only"],
        ["/tools/uv", "sync", "--project", str(checkout)],
        ["/tools/npm", "--prefix", str(checkout / "frontend"), "ci"],
        ["/tools/npm", "--prefix", str(checkout / "frontend"), "run", "build"],
    ]


def test_dashboard_separates_active_batch_from_five_other_in_progress_batches(
    tmp_path: Path,
) -> None:
    app = configured_app(tmp_path)
    source_folder = tmp_path / "overview-batches"
    source_folder.mkdir()
    root = app.state.repository.add_root("Overview", str(source_folder))
    active_names = []
    active_batches = []
    for index in range(7):
        source = source_folder / f"active-{index}.jpg"
        source.write_bytes(f"active-{index}".encode())
        record = app.state.repository.register_file(source, root["id"])
        batch = app.state.repository.create_batch(f"Active {index}", [record["id"]], 1)
        active_names.append(batch["name"])
        active_batches.append(batch)

    ready_source = source_folder / "ready.jpg"
    ready_source.write_bytes(b"ready")
    ready_record = app.state.repository.register_file(ready_source, root["id"])
    ready = app.state.repository.create_batch("Ready", [ready_record["id"]], 1)
    ready_item = ready["items"][0]["id"]
    app.state.repository.transition(ready_item, ItemState.TRANSFERRING)
    app.state.repository.transition(ready_item, ItemState.STAGED_ON_PIXEL)
    app.state.repository.transition(ready_item, ItemState.AWAITING_BACKUP_CONFIRMATION)

    failed_source = source_folder / "failed.jpg"
    failed_source.write_bytes(b"failed")
    failed_record = app.state.repository.register_file(failed_source, root["id"])
    failed = app.state.repository.create_batch("Needs attention", [failed_record["id"]], 1)
    app.state.repository.transition(failed["items"][0]["id"], ItemState.TRANSFER_FAILED)
    app.state.worker.active_batch_id = active_batches[-1]["id"]

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.get("/api/v1/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_batch"]["name"] == active_names[-1]
    names = [batch["name"] for batch in payload["batches"]]
    assert names == list(reversed(active_names[-6:-1]))


def test_global_queue_can_drain_after_active_batch_and_resume(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    source_folder = tmp_path / "queue-control"
    source_folder.mkdir()
    source = source_folder / "photo.jpg"
    source.write_bytes(b"queue-control")
    root = app.state.repository.add_root("Queue control", str(source_folder))
    record = app.state.repository.register_file(source, root["id"])
    batch = app.state.repository.create_batch("Active queue batch", [record["id"]], 1)
    app.state.worker.active_batch_id = batch["id"]

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        draining = client.post("/api/v1/queue/stop", headers=headers)
        assert draining.status_code == 200
        assert draining.json()["mode"] == "draining"
        assert draining.json()["drain_batch_id"] == batch["id"]
        assert app.state.repository.setting("queue_mode") == "draining"

        started = client.post("/api/v1/queue/start", headers=headers)
        assert started.status_code == 200
        assert started.json()["mode"] == "running"
        assert started.json()["drain_batch_id"] is None

        app.state.worker.active_batch_id = None
        stopped = client.post("/api/v1/queue/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["mode"] == "stopped"


def test_login_csrf_and_settings_flow(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        assert login.status_code == 200
        session_cookie = login.headers["set-cookie"].lower()
        assert "samesite=lax" in session_cookie
        assert "httponly" in session_cookie
        csrf = login.json()["csrf_token"]
        assert client.get("/api/v1/settings").status_code == 200

        rejected = client.patch(
            "/api/v1/settings",
            json={"expected_primary_uuid": "adopted-uuid"},
        )
        assert rejected.status_code == 403
        accepted = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={"expected_primary_uuid": "adopted-uuid"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["expected_primary_uuid"] == "adopted-uuid"


def test_source_path_must_exist(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        response = client.post(
            "/api/v1/sources",
            headers={"X-CSRF-Token": csrf},
            json={"name": "Missing", "path": str(tmp_path / "does-not-exist")},
        )
        assert response.status_code == 400


def test_authenticated_clients_can_browse_server_directories(tmp_path: Path) -> None:
    visible = tmp_path / "Visible Folder"
    visible.mkdir()
    (tmp_path / "not-a-folder.jpg").write_bytes(b"media")
    app = configured_app(tmp_path)

    with TestClient(app) as client:
        assert (
            client.get(
                "/api/v1/system/directories",
                params={"path": str(tmp_path)},
            ).status_code
            == 401
        )
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )

        response = client.get(
            "/api/v1/system/directories",
            params={"path": str(tmp_path)},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["path"] == str(tmp_path.resolve())
        assert payload["parent"] == str(tmp_path.parent.resolve())
        assert {"name": visible.name, "path": str(visible)} in payload["entries"]
        assert all(entry["name"] != "not-a-folder.jpg" for entry in payload["entries"])


def test_directory_browser_uses_host_default_without_a_macos_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(api_module, "default_server_directory", lambda: tmp_path)
    app = configured_app(tmp_path)

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.get("/api/v1/system/directories")

    assert response.status_code == 200
    assert response.json()["path"] == str(tmp_path.resolve())


def test_source_can_be_removed_without_deleting_folder(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    source_folder = tmp_path / "archive"
    source_folder.mkdir()
    root = app.state.repository.add_root("Archive", str(source_folder))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        assert client.delete(f"/api/v1/sources/{root['id']}").status_code == 403

        removed = client.delete(
            f"/api/v1/sources/{root['id']}",
            headers={"X-CSRF-Token": csrf},
        )

        assert removed.status_code == 200
        assert removed.json()["originals_deleted"] is False
        assert source_folder.is_dir()
        assert client.get("/api/v1/sources").json() == []


def test_built_frontend_and_operational_logs_are_served(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    dist = app.state.settings.frontend_dist
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>TheDoPixel</title>")
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        assert client.get("/").status_code == 200
        logs = client.get("/api/v1/logs")
        assert logs.status_code == 200
        assert isinstance(logs.json(), list)
        assert login.headers["content-type"].startswith("application/json")


def test_runtime_settings_are_editable_and_persisted(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    import_root = tmp_path / "relay-imports"
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        response = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={
                "connection_mode": "usb",
                "device_serial": "192.168.1.99:5555",
                "max_batch_files": 42,
                "pause_temperature_c": 44,
                "resume_temperature_c": 39,
                "import_root": str(import_root),
            },
        )
        assert response.status_code == 200
        assert response.json()["connection_mode"] == "usb"
        assert response.json()["import_root"] == str(import_root)
        assert import_root.is_dir()
        assert client.get("/api/v1/settings").json()["max_batch_files"] == 42

        cleared = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={"import_root": ""},
        )
        assert cleared.status_code == 200
        assert cleared.json()["import_root"] == str(Path.cwd() / "data")


def test_storage_buffer_cannot_exceed_measured_pixel_internal_storage(
    tmp_path: Path,
) -> None:
    app = configured_app(tmp_path)
    internal_capacity = 32 * 1024**3
    app.state.worker.latest_device["internal_storage_total_bytes"] = internal_capacity
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        settings = client.get("/api/v1/settings").json()
        assert settings["pixel_internal_storage_bytes"] == internal_capacity

        rejected = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={"reserve_bytes": internal_capacity + 1},
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "storage_buffer_too_large"

        accepted = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={"reserve_bytes": internal_capacity},
        )
        assert accepted.status_code == 200
        assert accepted.json()["reserve_bytes"] == internal_capacity


def test_storage_options_explain_adopted_and_portable_volumes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    app.state.settings.connection_mode = "ftp"
    adopted_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    app.state.worker.latest_device.update(
        {
            "state": "device",
            "primary_storage_uuid": adopted_uuid,
            "storage_total_bytes": 500 * 1024**3,
            "storage_free_bytes": 400 * 1024**3,
            "internal_storage_total_bytes": 32 * 1024**3,
            "internal_storage_free_bytes": 20 * 1024**3,
            "disks": ["disk:8,96"],
            "volumes": [
                "private mounted null",
                "public:8,97 mounted 7479-08F4",
                f"private:8,98 mounted {adopted_uuid}",
                f"emulated:8,98;0 mounted {adopted_uuid}",
            ],
        }
    )

    async def storage_devices() -> dict:
        return {
            "current_primary_uuid": adopted_uuid,
            "dump_supported": True,
            "disks": [
                {
                    "disk_id": "disk:8,96",
                    "flags": ["FLAG_ADOPTABLE", "FLAG_USB"],
                    "adoptable": True,
                    "default_primary": False,
                    "usb": True,
                    "sd": False,
                    "size_bytes": 500 * 1024**3,
                    "label": "Relay SSD",
                    "volume_ids": ["public:8,97", "private:8,98"],
                    "sys_path": "/devices/usb1",
                    "volumes": [],
                }
            ],
            "ignored_disks": [
                {
                    "disk_id": "disk:8,80",
                    "flags": ["ADOPTABLE", "USB"],
                    "adoptable": True,
                    "default_primary": False,
                    "usb": True,
                    "sd": False,
                    "size_bytes": -1,
                    "label": "Mass",
                    "volume_ids": [],
                    "sys_path": "/devices/usb1/block/sdg",
                    "volumes": [],
                    "ignored_reason": "empty_usb_bridge",
                }
            ],
            "volumes": [
                {
                    "volume_id": "private",
                    "volume_type": "private",
                    "state": "mounted",
                    "fs_uuid": None,
                    "disk_id": None,
                },
                {
                    "volume_id": "emulated",
                    "volume_type": "emulated",
                    "state": "mounted",
                    "fs_uuid": None,
                    "disk_id": None,
                },
                {
                    "volume_id": "private:1,2",
                    "volume_type": "private",
                    "state": "mounted",
                    "fs_uuid": "INTERNAL-METADATA-UUID",
                    "disk_id": None,
                },
                {
                    "volume_id": "public:259,1",
                    "volume_type": "public",
                    "state": "mounted",
                    "fs_uuid": "UNMATCHED-DISK-UUID",
                    "disk_id": "disk:259,0",
                },
                {
                    "volume_id": "public:8,97",
                    "volume_type": "public",
                    "state": "mounted",
                    "fs_uuid": "7479-08F4",
                    "disk_id": "disk:8,96",
                },
                {
                    "volume_id": "private:8,98",
                    "volume_type": "private",
                    "state": "mounted",
                    "fs_uuid": adopted_uuid,
                    "disk_id": "disk:8,96",
                },
            ],
        }

    monkeypatch.setattr(app.state.adb, "storage_devices", storage_devices)
    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.get("/api/v1/device/storage-options?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    internal = next(option for option in payload["options"] if option["kind"] == "internal")
    adopted = next(option for option in payload["options"] if option["kind"] == "adopted")
    portable = next(option for option in payload["options"] if option["kind"] == "portable")
    assert internal["volume_ids"] == ["private", "emulated"]
    assert not any(
        option["uuid"] in {"INTERNAL-METADATA-UUID", "UNMATCHED-DISK-UUID"}
        for option in payload["options"]
    )
    assert adopted["uuid"] == adopted_uuid
    assert adopted["current"] is True
    assert adopted["selectable"] is True
    assert adopted["free_bytes"] == 400 * 1024**3
    assert portable["uuid"] == "7479-08F4"
    assert portable["selectable"] is False
    assert portable["total_bytes"] == 500 * 1024**3
    assert payload["media"][0]["label"] == "Relay SSD"
    assert payload["media"][0]["usb"] is True
    assert payload["disks"] == ["disk:8,96"]
    assert payload["ignored_media"][0]["disk_id"] == "disk:8,80"
    assert not any(option.get("disk_id") == "disk:8,80" for option in payload["options"])
    assert payload["details_supported"] is True


def test_storage_adoption_records_uuid_without_typed_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    adopted_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    calls: list[dict] = []

    async def adopt_storage(disk_id: str, **options) -> dict:
        progress = options.pop("progress")
        await progress(
            {
                "stage": "partitioning",
                "message": "Erasing and encrypting the drive",
                "step": 4,
                "step_count": 7,
                "percent": 25,
            }
        )
        calls.append({"disk_id": disk_id, **options})
        return {
            "disk_id": disk_id,
            "adopted_uuid": adopted_uuid,
            "migrated_primary": True,
            "migration_error": None,
            "force_adoptable_enabled": True,
            "storage": {},
        }

    async def refresh_device() -> dict:
        return {
            "state": "device",
            "primary_storage_uuid": adopted_uuid,
            "storage_ready": True,
        }

    monkeypatch.setattr(app.state.adb, "adopt_storage", adopt_storage)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        accepted = client.post(
            "/api/v1/device/storage/adopt",
            headers={"X-CSRF-Token": csrf},
            json={
                "disk_id": "disk:8,96",
                "force_adoptable": True,
                "migrate_primary": True,
            },
        )
        status = client.get("/api/v1/device/storage/adoption")

    assert calls == [
        {
            "disk_id": "disk:8,96",
            "force_adoptable": True,
            "migrate_primary": True,
        }
    ]
    assert accepted.status_code == 202
    assert accepted.json()["operation_id"]
    assert status.status_code == 200
    assert status.json()["operation"]["status"] == "completed"
    assert status.json()["operation"]["result"]["adopted_uuid"] == adopted_uuid
    assert app.state.repository.expected_uuid() == adopted_uuid
    assert app.state.worker.maintenance_reason is None


def test_storage_adoption_runs_independently_of_start_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    started = threading.Event()
    release = threading.Event()

    async def adopt_storage(disk_id: str, **_options) -> dict:
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {
            "disk_id": disk_id,
            "adopted_uuid": "background-adopted-uuid",
            "migrated_primary": False,
            "migration_error": None,
            "force_adoptable_enabled": False,
            "storage": {"disks": [], "volumes": []},
        }

    async def refresh_device() -> dict:
        return {"state": "device", "storage_ready": True}

    monkeypatch.setattr(app.state.adb, "adopt_storage", adopt_storage)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        accepted = client.post(
            "/api/v1/device/storage/adopt",
            headers={"X-CSRF-Token": csrf},
            json={"disk_id": "disk:8,96"},
        )

        assert accepted.status_code == 202
        assert started.wait(timeout=1)
        running = client.get("/api/v1/device/storage/adoption").json()["operation"]
        assert running["status"] == "running"
        assert app.state.worker.maintenance_reason == "storage_adoption"

        duplicate = client.post(
            "/api/v1/device/storage/adopt",
            headers={"X-CSRF-Token": csrf},
            json={"disk_id": "disk:8,112"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "adoption_in_progress"

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            completed = client.get("/api/v1/device/storage/adoption").json()["operation"]
            if completed["status"] == "completed":
                break
            time.sleep(0.01)
        assert completed["status"] == "completed"
        assert completed["result"]["adopted_uuid"] == "background-adopted-uuid"

        dismissed = client.post(
            "/api/v1/device/storage/adoption/dismiss",
            headers={"X-CSRF-Token": csrf},
        )
        assert dismissed.status_code == 200
        assert client.get("/api/v1/device/storage/adoption").json()["operation"] is None


def test_primary_storage_switch_updates_pixel_and_uuid_lock_together(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    target_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    calls: list[str] = []

    async def switch_primary_storage(target: str, **options) -> dict:
        calls.append(target)
        await options["progress"](
            {
                "stage": "migrating",
                "message": "Migrating /sdcard",
                "step": 2,
                "step_count": 4,
                "percent": 20,
            }
        )
        return {
            "previous_uuid": "",
            "target_uuid": target,
            "changed": True,
            "storage": {
                "disks": [],
                "volumes": [],
                "current_primary_uuid": target,
            },
        }

    async def refresh_device() -> dict:
        return {
            "state": "device",
            "primary_storage_uuid": target_uuid,
            "storage_ready": True,
        }

    monkeypatch.setattr(app.state.adb, "switch_primary_storage", switch_primary_storage)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        accepted = client.post(
            "/api/v1/device/storage/primary-switch",
            headers=headers,
            json={"target_uuid": target_uuid},
        )
        status = client.get("/api/v1/device/storage/primary-switch")

    assert accepted.status_code == 202
    assert calls == [target_uuid]
    operation = status.json()["operation"]
    assert operation["status"] == "completed"
    assert operation["result"]["target_uuid"] == target_uuid
    assert operation["result"]["destination_root"] == ("/sdcard/DCIM/Camera/PixelRelay")
    assert app.state.repository.expected_uuid() == target_uuid
    assert app.state.settings.destination_root == "/sdcard/DCIM/Camera/PixelRelay"
    assert app.state.worker.maintenance_reason is None


def test_storage_unmount_records_the_exact_disk_and_volumes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    calls: list[str] = []

    async def unmount_storage(disk_id: str) -> dict:
        calls.append(disk_id)
        return {
            "disk_id": disk_id,
            "unmounted_volume_ids": ["public:8,97"],
            "storage": {"disks": [], "volumes": []},
        }

    async def refresh_device() -> dict:
        return {"state": "device", "storage_ready": True}

    monkeypatch.setattr(app.state.adb, "unmount_storage", unmount_storage)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/storage/unmount",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"disk_id": "disk:8,96"},
        )

    assert response.status_code == 200
    assert response.json()["unmounted_volume_ids"] == ["public:8,97"]
    assert calls == ["disk:8,96"]
    assert app.state.worker.maintenance_reason is None


def test_adb_speed_test_reports_current_connection_and_releases_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)

    async def speed_test() -> dict:
        return {
            "connection_mode": "usb",
            "serial": "USB",
            "size_bytes": 32 * 1024**2,
            "duration_seconds": 2.0,
            "bytes_per_second": 16 * 1024**2,
            "megabytes_per_second": 16.0,
            "megabits_per_second": 134.217728,
            "checksum_verified": True,
            "temporary_files_removed": True,
        }

    monkeypatch.setattr(app.state.adb, "speed_test", speed_test)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/adb-speed-test",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 200
    assert response.json()["connection_mode"] == "usb"
    assert response.json()["bytes_per_second"] == 16 * 1024**2
    assert response.json()["checksum_verified"] is True
    assert app.state.worker.maintenance_reason is None


def test_ftp_speed_test_reports_upload_and_verification_and_releases_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)

    received: list[dict] = []

    async def ftp_speed_test(overrides: dict) -> dict:
        received.append(overrides)
        return {
            "connection_mode": "ftp",
            "server": "pixel.local:2121",
            "size_bytes": 32 * 1024**2,
            "upload_duration_seconds": 2.0,
            "upload_bytes_per_second": 16 * 1024**2,
            "upload_megabytes_per_second": 16.0,
            "upload_megabits_per_second": 134.217728,
            "verification_duration_seconds": 4.0,
            "verification_bytes_per_second": 8 * 1024**2,
            "verified_duration_seconds": 6.0,
            "verified_bytes_per_second": 32 * 1024**2 / 6,
            "checksum_verified": True,
            "temporary_files_removed": True,
        }

    monkeypatch.setattr(app.state.adb, "ftp_speed_test", ftp_speed_test)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/ftp-speed-test",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "ftp_host": "pixel.local",
                "ftp_port": 2121,
                "ftp_username": "pixel",
                "ftp_password": "draft-secret",
                "ftp_destination_root": "/DCIM/Camera/PixelRelay",
            },
        )

    assert response.status_code == 200
    assert response.json()["connection_mode"] == "ftp"
    assert response.json()["upload_bytes_per_second"] == 16 * 1024**2
    assert response.json()["checksum_verified"] is True
    assert received == [
        {
            "ftp_host": "pixel.local",
            "ftp_port": 2121,
            "ftp_username": "pixel",
            "ftp_password": "draft-secret",
            "ftp_destination_root": "/DCIM/Camera/PixelRelay",
        }
    ]
    assert app.state.worker.maintenance_reason is None


def test_ftp_connection_test_uses_draft_values_without_saving_them(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    received: list[dict] = []

    async def ftp_connection_test(overrides: dict) -> dict:
        received.append(overrides)
        return {
            "connected": True,
            "server": "draft.pixel:2021",
            "destination_root": "/draft/PixelRelay",
        }

    monkeypatch.setattr(app.state.adb, "ftp_connection_test", ftp_connection_test)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/ftp-connection-test",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "ftp_host": " draft.pixel ",
                "ftp_port": 2021,
                "ftp_username": " draft-user ",
                "ftp_password": "draft-secret",
                "ftp_destination_root": "/draft/PixelRelay/",
            },
        )

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert received == [
        {
            "ftp_host": "draft.pixel",
            "ftp_port": 2021,
            "ftp_username": "draft-user",
            "ftp_password": "draft-secret",
            "ftp_destination_root": "/draft/PixelRelay",
        }
    ]
    assert app.state.settings.ftp_host != "draft.pixel"
    assert app.state.repository.setting("ftp_password") == ""


def test_ftp_password_is_never_returned_or_written_to_audit(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        response = client.patch(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={
                "connection_mode": "ftp",
                "ftp_host": "pixel.local",
                "ftp_port": 2121,
                "ftp_username": "pixel",
                "ftp_password": "ftp-secret",
                "ftp_destination_root": "/DCIM/Camera/PixelRelay",
            },
        )
        assert response.status_code == 200
        assert "ftp_password" not in response.json()
        assert response.json()["ftp_password_configured"] is True
        assert "ftp-secret" not in str(app.state.repository.audit_entries())


def test_batch_can_be_manually_confirmed_without_picker(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    repository = app.state.repository
    source_folder = tmp_path / "photos"
    source_folder.mkdir()
    source = source_folder / "IMG_1234.JPG"
    source.write_bytes(b"photo")
    root = repository.add_root("Photos", str(source_folder))
    record = repository.register_file(source, root["id"])
    batch = repository.create_batch("Cloud check", [record["id"]], 1)
    item_id = batch["items"][0]["id"]
    repository.transition(item_id, ItemState.TRANSFERRING)
    repository.transition(item_id, ItemState.STAGED_ON_PIXEL)
    repository.transition(item_id, ItemState.AWAITING_BACKUP_CONFIRMATION)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        csrf = login.json()["csrf_token"]
        confirmed = client.post(
            f"/api/v1/batches/{batch['id']}/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"acknowledgement": "I verified this batch in Google Photos"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["states"] == {"confirmed_backed_up": 1}

        manifest = client.get(f"/api/v1/batches/{batch['id']}/manifest")
        assert manifest.status_code == 200
        assert "attachment;" in manifest.headers["content-disposition"]
        assert manifest.json()["items"][0]["sha256"] == record["sha256"]
        assert manifest.json()["items"][0]["events"][-1]["to_state"] == ("confirmed_backed_up")

        backed_up = client.get("/api/v1/backups/items?limit=10&offset=0")
        assert backed_up.status_code == 200
        assert backed_up.json()["total"] == 1
        assert backed_up.json()["items"][0]["batch_id"] == batch["id"]
        assert backed_up.json()["items"][0]["state"] == "confirmed_backed_up"

        assert all("picker" not in path for path in app.openapi()["paths"])


def test_clean_slate_removes_only_relay_tree_and_reconciles_batches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)
    repository = app.state.repository
    source_folder = tmp_path / "clean-slate-sources"
    source_folder.mkdir()
    root = repository.add_root("Clean slate", str(source_folder))

    confirmed_source = source_folder / "confirmed.jpg"
    confirmed_source.write_bytes(b"confirmed-source")
    confirmed_record = repository.register_file(confirmed_source, root["id"])
    confirmed = repository.create_batch("Confirmed", [confirmed_record["id"]], 1)
    confirmed_item = confirmed["items"][0]
    repository.transition(confirmed_item["id"], ItemState.TRANSFERRING)
    repository.transition(confirmed_item["id"], ItemState.STAGED_ON_PIXEL)
    repository.transition(confirmed_item["id"], ItemState.AWAITING_BACKUP_CONFIRMATION)
    repository.confirm_batch(confirmed["id"], 1)

    unfinished_source = source_folder / "unfinished.jpg"
    unfinished_source.write_bytes(b"unfinished-source")
    unfinished_record = repository.register_file(unfinished_source, root["id"])
    unfinished = repository.create_batch("Unfinished", [unfinished_record["id"]], 1)
    unfinished_item = unfinished["items"][0]
    repository.transition(unfinished_item["id"], ItemState.TRANSFERRING)
    repository.transition(unfinished_item["id"], ItemState.STAGED_ON_PIXEL)
    repository.transition(unfinished_item["id"], ItemState.AWAITING_BACKUP_CONFIRMATION)

    destination = app.state.settings.destination_root
    reset_calls: list[str] = []

    async def ensure_ready(_expected_uuid: str):
        return SimpleNamespace(storage_total_bytes=1000, storage_free_bytes=100)

    async def storage_inventory(path: str):
        assert path == destination
        return [
            {"path": confirmed_item["remote_path"], "allocated_bytes": 10},
            {"path": unfinished_item["remote_path"], "allocated_bytes": 20},
            {
                "path": f"{destination}/0123456789abcdef0123456789abcdef/photos/orphan.jpg",
                "allocated_bytes": 30,
            },
        ]

    async def reset_destination_tree() -> str:
        reset_calls.append(destination)
        return destination

    async def refresh_device() -> dict:
        return {"state": "device"}

    monkeypatch.setattr(app.state.adb, "ensure_ready", ensure_ready)
    monkeypatch.setattr(app.state.adb, "storage_inventory", storage_inventory)
    monkeypatch.setattr(app.state.adb, "reset_destination_tree", reset_destination_tree)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        rejected = client.post(
            "/api/v1/device/storage/clean-slate",
            headers=headers,
            json={"acknowledgement": "DELETE EVERYTHING"},
        )
        response = client.post(
            "/api/v1/device/storage/clean-slate",
            headers=headers,
            json={"acknowledgement": "DELETE PIXEL RELAY TREE"},
        )

    assert rejected.status_code == 422
    assert response.status_code == 200
    assert response.json()["known_files_deleted"] == 3
    assert response.json()["known_bytes_deleted"] == 60
    assert response.json()["confirmed_batches_purged"] == 1
    assert response.json()["unconfirmed_batches_cancelled"] == 1
    assert reset_calls == [destination]
    confirmed_after = repository.get_batch(confirmed["id"])
    unfinished_after = repository.get_batch(unfinished["id"])
    assert confirmed_after["states"] == {"purged_from_pixel": 1}
    assert confirmed_after["purged_at"]
    assert unfinished_after["states"] == {"cancelled": 1}
    assert unfinished_after["cancelled_at"]
    assert unfinished_after["purged_at"]
    assert confirmed_source.read_bytes() == b"confirmed-source"
    assert unfinished_source.read_bytes() == b"unfinished-source"


def test_batch_creation_uses_current_pixel_storage_for_balanced_splitting(
    tmp_path: Path,
) -> None:
    app = configured_app(tmp_path)
    app.state.settings.max_batch_bytes = 100
    app.state.settings.reserve_bytes = 5
    app.state.settings.reserve_percent = 10
    app.state.worker.latest_device.update(
        {
            "storage_free_bytes": 35,
            "storage_total_bytes": 100,
        }
    )
    source_folder = tmp_path / "storage-plan"
    source_folder.mkdir()
    root = app.state.repository.add_root("Storage plan", str(source_folder))
    records = []
    for index in range(4):
        source = source_folder / f"photo-{index}.jpg"
        source.write_bytes(bytes([index]) * 10)
        records.append(app.state.repository.register_file(source, root["id"]))

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/batches",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={
                "name": "Storage-aware",
                "file_ids": [record["id"] for record in records],
            },
        )

    assert response.status_code == 201
    batches = response.json()
    assert [batch["total_bytes"] for batch in batches] == [20, 20]
    assert all(batch["planned_capacity_bytes"] == 25 for batch in batches)
    assert all(batch["split_reason"] == "pixel_storage" for batch in batches)


def test_batch_preflight_is_read_only_and_uses_pixel_capacity(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    app.state.settings.max_batch_bytes = 100
    app.state.settings.reserve_bytes = 5
    app.state.settings.reserve_percent = 10
    app.state.worker.latest_device.update({"storage_free_bytes": 35, "storage_total_bytes": 100})
    source_folder = tmp_path / "preflight"
    source_folder.mkdir()
    root = app.state.repository.add_root("Preflight", str(source_folder))
    records = []
    for index in range(4):
        source = source_folder / f"photo-{index}.jpg"
        source.write_bytes(bytes([index + 1]) * 10)
        records.append(app.state.repository.register_file(source, root["id"]))

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/batches/plan",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"name": "Preview", "file_ids": [record["id"] for record in records]},
        )

    assert response.status_code == 200
    assert response.json()["batch_count"] == 2
    assert response.json()["batch_byte_limit"] == 25
    assert response.json()["storage_reserve_bytes"] == 10
    assert app.state.repository.list_batches() == []


def test_batch_pause_and_resume_endpoints_control_queue_eligibility(
    tmp_path: Path,
) -> None:
    app = configured_app(tmp_path)
    source_folder = tmp_path / "pause-api"
    source_folder.mkdir()
    source = source_folder / "photo.jpg"
    source.write_bytes(b"pause-me")
    root = app.state.repository.add_root("Pause API", str(source_folder))
    record = app.state.repository.register_file(source, root["id"])
    batch = app.state.repository.create_batch("Controllable", [record["id"]], 1)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        paused = client.post(f"/api/v1/batches/{batch['id']}/pause", headers=headers)
        assert paused.status_code == 200
        assert paused.json()["paused_at"]
        assert app.state.repository.next_work_item() is None

        resumed = client.post(f"/api/v1/batches/{batch['id']}/resume", headers=headers)
        assert resumed.status_code == 200
        assert resumed.json()["paused_at"] is None
        assert app.state.repository.next_work_item()["batch_id"] == batch["id"]


def test_device_telemetry_returns_bounded_history_and_summary(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    start = datetime.now(UTC) - timedelta(hours=2)
    for index in range(30):
        app.state.db.execute(
            "INSERT INTO device_samples(status_json, created_at) VALUES (?, ?)",
            (
                json.dumps(
                    {
                        "state": "device",
                        "battery_level": 60 + index,
                        "temperature_c": 30 + index / 10,
                        "storage_free_bytes": 1000 - index * 10,
                        "storage_total_bytes": 2000,
                        "storage_used_bytes": 1000 + index * 10,
                    }
                ),
                (start + timedelta(minutes=index * 4)).isoformat(),
            ),
        )

    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.get(
            "/api/v1/device/telemetry",
            params={"hours": 6, "max_points": 24},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sample_count"] <= 25
    assert payload["points"][-1]["battery_level"] == 89
    assert payload["summary"]["temperature_c"]["minimum"] == 30
    assert payload["summary"]["storage_free_bytes"]["latest"] == 710


def test_enable_adb_over_ip_switches_to_verified_network_connection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)

    async def enable_tcpip(port: int) -> dict:
        assert port == 5566
        return {
            "enabled": True,
            "connected": True,
            "port": 5566,
            "address": "192.168.1.35",
            "serial": "192.168.1.35:5566",
            "addresses": ["192.168.1.35"],
            "connection_attempts": [],
            "port_diagnostics": {
                "inspection_supported": True,
                "listeners": [],
                "adb_tcp_port_before_restart": None,
                "inspection_error": None,
            },
        }

    async def refresh_device() -> dict:
        device = {
            "state": "device",
            "connection_mode": "network",
            "serial": "192.168.1.35:5566",
        }
        app.state.worker.latest_device = device
        return device

    monkeypatch.setattr(app.state.adb, "enable_tcpip", enable_tcpip)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/adb-over-ip",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"port": 5566},
        )

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["device"]["connection_mode"] == "network"
    assert app.state.settings.connection_mode == "network"
    assert app.state.settings.device_serial == "192.168.1.35:5566"
    assert app.state.repository.setting("connection_mode") == "network"


def test_authenticated_user_can_restart_fixed_adb_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = configured_app(tmp_path)

    async def restart_server() -> dict:
        return {
            "restarted": True,
            "stop_returncode": 0,
            "stop_output": None,
            "start_output": None,
        }

    async def refresh_device() -> dict:
        return {"state": "device", "connection_mode": "usb", "serial": "USB"}

    monkeypatch.setattr(app.state.adb, "restart_server", restart_server)
    monkeypatch.setattr(app.state.worker, "refresh_device", refresh_device)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "a secure local password"},
        )
        response = client.post(
            "/api/v1/device/adb-server/restart",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        )

    assert response.status_code == 200
    assert response.json()["restarted"] is True
    assert response.json()["device"]["state"] == "device"

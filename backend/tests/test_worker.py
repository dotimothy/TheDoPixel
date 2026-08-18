import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pixel_relay.auth import AuthService
from pixel_relay.config import Settings
from pixel_relay.database import Database
from pixel_relay.events import EventBroker
from pixel_relay.repository import Repository
from pixel_relay.states import ItemState
from pixel_relay.worker import RelayWorker


class FakeAdb:
    def __init__(self, scan_result: bool = True) -> None:
        self.hashes: dict[str, str] = {}
        self.removed: list[str] = []
        self.removed_batch_directories: list[str] = []
        self.scan_result = scan_result

    async def ensure_ready(self, _expected_uuid: str):
        return SimpleNamespace(
            temperature_c=31.0,
            storage_total_bytes=500 * 1024**3,
            storage_free_bytes=400 * 1024**3,
        )

    async def ensure_directory(self, _path: str) -> None:
        return None

    async def remote_sha256(self, path: str) -> str:
        return self.hashes.get(path, "")

    async def push(self, source: Path, destination: str, progress=None) -> None:
        from pixel_relay.files import sha256_file

        if progress:
            progress(0, source.stat().st_size)
            progress(source.stat().st_size // 2, source.stat().st_size)
        self.hashes[destination] = sha256_file(source)
        if progress:
            progress(source.stat().st_size, source.stat().st_size)

    async def scan_media(self, _path: str) -> bool:
        return self.scan_result

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)
        self.hashes.pop(path, None)

    async def remove_directory(self, _path: str) -> None:
        return None

    async def remove_batch_directory(self, path: str) -> None:
        self.removed_batch_directories.append(path)
        prefix = f"{path.rstrip('/')}/"
        for remote_path in list(self.hashes):
            if remote_path.startswith(prefix):
                self.hashes.pop(remote_path)


@pytest.mark.asyncio
async def test_queue_drain_finishes_current_batch_before_stopping(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "local")
    repository = Repository(db, settings)
    root_path = tmp_path / "drain"
    root_path.mkdir()
    for name in ("first.jpg", "second.jpg", "later.jpg"):
        (root_path / name).write_bytes(name.encode())
    root = repository.add_root("Drain", str(root_path))
    records = repository.scan_root(root["id"])["files"]
    current = repository.create_batch("Current", [record["id"] for record in records[:2]], 1)
    later = repository.create_batch("Later", [records[2]["id"]], 1)
    worker = RelayWorker(
        db,
        repository,
        FakeAdb(),  # type: ignore[arg-type]
        EventBroker(),
    )
    worker.latest_device = {
        "state": "device",
        "storage_total_bytes": 500 * 1024**3,
        "storage_free_bytes": 400 * 1024**3,
    }
    worker.queue_mode = "draining"
    worker.queue_drain_batch_id = current["id"]

    queue_task = asyncio.create_task(worker._run_queue())
    try:
        for _attempt in range(200):
            if worker.queue_mode == "stopped":
                break
            await asyncio.sleep(0.01)
        assert worker.queue_mode == "stopped"
    finally:
        worker._stop.set()
        worker.wake()
        await asyncio.wait_for(queue_task, timeout=2)

    assert repository.get_batch(current["id"])["states"] == {
        ItemState.AWAITING_BACKUP_CONFIRMATION: 2
    }
    assert repository.get_batch(later["id"])["states"] == {ItemState.QUEUED: 1}
    assert repository.setting("queue_mode") == "stopped"


@pytest.mark.asyncio
async def test_mixed_batch_transfers_confirms_and_purges_without_source_deletion(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pixel_relay.adb.progress")
    settings = Settings(
        data_dir=tmp_path / "data",
        expected_primary_uuid="adopted-uuid",
    )
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "a sufficiently long password")
    repository = Repository(db, settings)

    root_path = tmp_path / "archive"
    root_path.mkdir()
    photo = root_path / "photo.jpg"
    video = root_path / "video.mp4"
    photo.write_bytes(b"photo-original")
    video.write_bytes(b"video-original")
    root = repository.add_root("Archive", str(root_path))
    scan = repository.scan_root(root["id"])
    batch = repository.create_batch("Mixed relay", [item["id"] for item in scan["files"]], 1)

    fake_adb = FakeAdb()
    worker = RelayWorker(db, repository, fake_adb, EventBroker())  # type: ignore[arg-type]
    for item in batch["items"]:
        await worker._process(repository.get_item(item["id"]))  # type: ignore[arg-type]

    ready = repository.get_batch(batch["id"])
    assert ready["states"] == {"awaiting_backup_confirmation": 2}
    assert ready["transfer_bytes"] == ready["total_bytes"]
    assert {item["media_kind"] for item in ready["items"]} == {"photo", "video"}

    repository.confirm_batch(batch["id"], 1)
    purged = await worker.purge_batch(batch["id"], 1)
    assert purged["states"] == {"purged_from_pixel": 2}
    assert fake_adb.removed == []
    assert fake_adb.removed_batch_directories == [
        batch["items"][0]["remote_path"].rsplit("/", 2)[0]
    ]
    assert photo.read_bytes() == b"photo-original"
    assert video.read_bytes() == b"video-original"
    messages = [record.getMessage() for record in caplog.records]
    assert any("ADB batch 'Mixed relay'" in message for message in messages)
    assert any("adb push complete" in message for message in messages)
    assert any("2/2 items ready" in message for message in messages)


@pytest.mark.asyncio
async def test_raw_advances_when_generic_mediastore_query_cannot_find_it(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "local")
    repository = Repository(db, settings)
    root_path = tmp_path / "raw"
    root_path.mkdir()
    raw = root_path / "capture.arw"
    raw.write_bytes(b"raw-original")
    root = repository.add_root("RAW archive", str(root_path))
    scan = repository.scan_root(root["id"])
    batch = repository.create_batch("RAW relay", [scan["files"][0]["id"]], 1)
    worker = RelayWorker(
        db,
        repository,
        FakeAdb(scan_result=False),  # type: ignore[arg-type]
        EventBroker(),
    )

    await worker._process(repository.get_item(batch["items"][0]["id"]))  # type: ignore[arg-type]

    result = repository.get_batch(batch["id"])
    assert result["states"] == {"awaiting_backup_confirmation": 1}
    event = db.fetchone(
        "SELECT detail FROM state_events WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (batch["items"][0]["id"],),
    )
    assert event and "RAW staged" in event["detail"]


@pytest.mark.asyncio
async def test_cancelled_batch_pixel_copy_can_be_cleaned_up(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "local")
    repository = Repository(db, settings)
    root_path = tmp_path / "cancel"
    root_path.mkdir()
    photo = root_path / "photo.jpg"
    photo.write_bytes(b"keep-source")
    root = repository.add_root("Cancel", str(root_path))
    record = repository.scan_root(root["id"])["files"][0]
    batch = repository.create_batch("Cancel after staging", [record["id"]], 1)
    fake_adb = FakeAdb()
    worker = RelayWorker(db, repository, fake_adb, EventBroker())  # type: ignore[arg-type]

    await worker._process(repository.get_item(batch["items"][0]["id"]))  # type: ignore[arg-type]
    cancelled = repository.cancel_batch(batch["id"], 1)
    assert cancelled["states"] == {ItemState.CANCELLED_ON_PIXEL: 1}

    purged = await worker.purge_batch(batch["id"], 1)

    assert purged["states"] == {ItemState.PURGED_FROM_PIXEL: 1}
    assert photo.read_bytes() == b"keep-source"

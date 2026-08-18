from datetime import UTC, datetime, timedelta
from pathlib import Path

import pixel_relay.repository as repository_module
import pytest
from pixel_relay.auth import AuthService
from pixel_relay.config import Settings
from pixel_relay.database import Database
from pixel_relay.repository import DomainError, Repository
from pixel_relay.states import ItemState


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    settings = Settings(
        data_dir=tmp_path / "data",
        import_root=tmp_path / "imports",
        expected_primary_uuid="adopted-uuid",
    )
    settings.prepare()
    db = Database(settings.database_path)
    db.migrate()
    AuthService(db, settings).create_admin("admin", "a sufficiently long password")
    return Repository(db, settings)


def source_file(repository: Repository, tmp_path: Path):
    root_path = tmp_path / "media"
    root_path.mkdir()
    media = root_path / "photo.jpg"
    media.write_bytes(b"not-a-real-jpeg-but-safe-for-transfer")
    root = repository.add_root("Archive", str(root_path))
    return repository.register_file(media, root["id"]), media


def test_unreadable_source_reports_issue_and_scan_fails_visibly(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = tmp_path / "permission-blocked"
    root_path.mkdir()
    root = repository.add_root("Blocked drive", str(root_path))
    real_scandir = repository_module.os.scandir

    def permission_blocked(path: str | Path):
        if Path(path) == root_path:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_scandir(path)

    monkeypatch.setattr(repository_module.os, "scandir", permission_blocked)

    listed = repository.list_roots()[0]
    assert listed["available"] is False
    assert listed["issue_code"] == "permission_denied"
    assert "Full Disk Access" in listed["issue"]

    with pytest.raises(DomainError) as raised:
        repository.scan_root(root["id"])
    assert raised.value.code == "root_permission_denied"
    assert raised.value.status_code == 403
    assert "permission to read" in str(raised.value)


def test_scan_and_batch_preserve_authoritative_source(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    batch = repository.create_batch("Test batch", [record["id"]], 1)
    assert batch["states"] == {"queued": 1}
    assert media.exists()
    item = batch["items"][0]
    assert item["remote_path"].startswith(f"/sdcard/DCIM/Camera/PixelRelay/{batch['id']}/")
    assert item["remote_path"].endswith(".jpg")
    assert Path(item["remote_path"]).name == media.name
    assert "/photos/" in item["remote_path"]
    assert item["media_kind"] == "photo"
    assert item["mtime_ns"] == record["mtime_ns"]


def test_remove_source_hides_registration_but_preserves_originals_and_history(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    batch = repository.create_batch("Historical batch", [record["id"]], 1)
    root_id = record["root_id"]

    removed = repository.remove_root(root_id, 1)

    assert removed["originals_deleted"] is False
    assert removed["discovered_records_retained"] == 1
    assert media.exists()
    assert repository.list_roots() == []
    assert repository.list_files() == []
    assert repository.get_batch(batch["id"])["items"][0]["path"] == str(media)
    assert repository.db.fetchone(
        "SELECT id FROM source_files WHERE id=?",
        (record["id"],),
    )

    reactivated = repository.add_root("Archive again", str(media.parent))
    assert reactivated["id"] == root_id
    assert repository.list_roots()[0]["name"] == "Archive again"
    assert repository.list_files()[0]["id"] == record["id"]


def test_photos_and_videos_are_classified_and_staged_separately(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "mixed-media"
    root_path.mkdir()
    (root_path / "still.jpg").write_bytes(b"still")
    (root_path / "clip.mp4").write_bytes(b"video")
    root = repository.add_root("Mixed", str(root_path))
    result = repository.scan_root(root["id"])
    assert {file["media_kind"] for file in result["files"]} == {"photo", "video"}

    batch = repository.create_batch("Mixed batch", [file["id"] for file in result["files"]], 1)
    by_kind = {item["media_kind"]: item for item in batch["items"]}
    assert "/photos/" in by_kind["photo"]["remote_path"]
    assert "/videos/" in by_kind["video"]["remote_path"]
    assert batch["photo_count"] == 1
    assert batch["video_count"] == 1


def test_scan_reports_candidate_progress(repository: Repository, tmp_path: Path) -> None:
    root_path = tmp_path / "scan-progress"
    root_path.mkdir()
    (root_path / "still.jpg").write_bytes(b"still")
    (root_path / "clip.mp4").write_bytes(b"video")
    (root_path / "notes.txt").write_text("not media")
    (root_path / "nested").mkdir()
    root = repository.add_root("Progress", str(root_path))
    updates: list[dict] = []

    result = repository.scan_root(root["id"], progress=updates.append)

    assert updates[0]["phase"] == "enumerating"
    assert updates[-1]["phase"] == "complete"
    assert updates[-1]["processed"] == updates[-1]["total"] == 2
    assert updates[-1]["discovered"] == len(result["files"]) == 2
    assert updates[-1]["skipped"] == len(result["skipped"]) == 0
    assert updates[-1]["issues"] == []
    assert {"enumerating", "hashing", "saving", "complete"} <= {
        update["phase"] for update in updates
    }


def test_scan_progress_includes_nested_folder_issues(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = tmp_path / "partially-readable"
    blocked_path = root_path / "blocked"
    blocked_path.mkdir(parents=True)
    (root_path / "still.jpg").write_bytes(b"still")
    root = repository.add_root("Partial drive", str(root_path))
    real_scandir = repository_module.os.scandir

    def permission_blocked(path: str | Path):
        if Path(path) == blocked_path:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_scandir(path)

    monkeypatch.setattr(repository_module.os, "scandir", permission_blocked)
    updates: list[dict] = []

    result = repository.scan_root(root["id"], progress=updates.append)

    assert len(result["files"]) == 1
    assert len(result["skipped"]) == 1
    assert updates[-1]["skipped"] == 1
    assert updates[-1]["issues"] == result["skipped"]


def test_incremental_scan_reuses_hashes_and_full_verify_rehashes(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "incremental"
    root_path.mkdir()
    (root_path / "one.jpg").write_bytes(b"one")
    (root_path / "two.mp4").write_bytes(b"two")
    root = repository.add_root("Incremental", str(root_path))
    original_hash = repository_module.sha256_file
    hashed_paths: list[Path] = []

    def tracking_hash(path: Path) -> str:
        hashed_paths.append(path)
        return original_hash(path)

    monkeypatch.setattr(repository_module, "sha256_file", tracking_hash)

    first = repository.scan_root(root["id"])
    second = repository.scan_root(root["id"])
    (root_path / "one.jpg").write_bytes(b"one changed")
    changed = repository.scan_root(root["id"])
    verified = repository.scan_root(root["id"], full_verify=True)

    assert first["stats"]["hashed"] == 2
    assert first["stats"]["cached"] == 0
    assert second["stats"]["hashed"] == 0
    assert second["stats"]["cached"] == 2
    assert changed["stats"]["hashed"] == 1
    assert changed["stats"]["cached"] == 1
    assert verified["stats"]["hashed"] == 2
    assert verified["stats"]["cached"] == 0
    assert len(hashed_paths) == 5


def test_transfer_progress_is_persisted_and_aggregated(
    repository: Repository, tmp_path: Path
) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("Progress", [record["id"]], 1)
    item = batch["items"][0]
    repository.transition(item["id"], ItemState.TRANSFERRING)

    repository.update_transfer_progress(item["id"], record["size"] // 2, record["size"])

    current = repository.get_batch(batch["id"])
    assert current["transfer_bytes"] == record["size"] // 2
    assert current["items"][0]["transfer_total_bytes"] == record["size"]
    assert current["processing_started_at"]
    assert current["transfer_rate_bytes_per_second"] > 0
    assert current["eta_seconds"] > 0
    assert repository.list_batches()[0]["transfer_bytes"] == record["size"] // 2

    repository.transition(item["id"], ItemState.STAGED_ON_PIXEL)
    assert repository.get_batch(batch["id"])["transfer_bytes"] == record["size"]


def test_camera_raw_files_are_classified_as_photos(repository: Repository, tmp_path: Path) -> None:
    root_path = tmp_path / "raw-media"
    root_path.mkdir()
    raw = root_path / "capture.cr3"
    raw.write_bytes(b"camera-raw")
    root = repository.add_root("RAW", str(root_path))
    record = repository.register_file(raw, root["id"])
    assert record["extension"] == ".cr3"
    assert record["media_kind"] == "photo"
    batch = repository.create_batch("RAW batch", [record["id"]], 1)
    assert batch["raw_count"] == 1
    assert batch["photo_count"] == 0
    listed = repository.list_batches()[0]
    assert listed["raw_count"] == 1
    assert listed["photo_count"] == 0


def test_large_selection_splits_into_numbered_batches(
    repository: Repository, tmp_path: Path
) -> None:
    repository.settings.max_batch_files = 2
    root_path = tmp_path / "large-folder"
    root_path.mkdir()
    for index in range(5):
        (root_path / f"photo-{index}.jpg").write_bytes(f"photo-{index}".encode())
    root = repository.add_root("Large", str(root_path))
    scan = repository.scan_root(root["id"])

    batches = repository.create_batches(
        "Family archive",
        [file["id"] for file in scan["files"]],
        1,
    )

    assert [batch["name"] for batch in batches] == [
        "Family archive · 1 of 3",
        "Family archive · 2 of 3",
        "Family archive · 3 of 3",
    ]
    assert [len(batch["items"]) for batch in batches] == [2, 2, 1]


def test_batch_preflight_matches_creation_without_mutating_queue(
    repository: Repository, tmp_path: Path
) -> None:
    repository.settings.max_batch_files = 2
    root_path = tmp_path / "preflight"
    root_path.mkdir()
    for index in range(3):
        (root_path / f"photo-{index}.jpg").write_bytes(bytes([index + 1]) * 10)
    root = repository.add_root("Preflight", str(root_path))
    scan = repository.scan_root(root["id"])
    file_ids = [file["id"] for file in scan["files"]]

    plan = repository.plan_batches("Preview", file_ids)

    assert plan["batch_count"] == 2
    assert plan["unique_content_count"] == 3
    assert plan["total_bytes"] == 30
    assert repository.list_batches() == []

    created = repository.create_batches("Preview", file_ids, 1)
    assert [batch["name"] for batch in created] == [part["name"] for part in plan["parts"]]
    assert [batch["total_bytes"] for batch in created] == [
        part["total_bytes"] for part in plan["parts"]
    ]


def test_unsettled_batch_listing_skips_history_unless_explicitly_included(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "dashboard-batches"
    root_path.mkdir()
    (root_path / "open.jpg").write_bytes(b"open")
    (root_path / "cancelled.jpg").write_bytes(b"cancelled")
    root = repository.add_root("Dashboard", str(root_path))
    files = repository.scan_root(root["id"])["files"]
    by_name = {Path(file["path"]).name: file for file in files}
    open_batch = repository.create_batch("Open", [by_name["open.jpg"]["id"]], 1)
    cancelled_batch = repository.create_batch("Cancelled", [by_name["cancelled.jpg"]["id"]], 1)
    repository.cancel_batch(cancelled_batch["id"], 1)

    unsettled = repository.list_batches(unsettled_only=True)
    with_active = repository.list_batches(
        unsettled_only=True,
        include_batch_id=cancelled_batch["id"],
    )

    assert {batch["id"] for batch in unsettled} == {open_batch["id"]}
    assert {batch["id"] for batch in with_active} == {
        open_batch["id"],
        cancelled_batch["id"],
    }
    assert len(repository.list_batches()) == 2


def test_source_files_report_prior_confirmed_and_purged_history(
    repository: Repository, tmp_path: Path
) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("History", [record["id"]], 1)
    item_id = batch["items"][0]["id"]
    repository.transition(item_id, ItemState.TRANSFERRING)
    repository.transition(item_id, ItemState.STAGED_ON_PIXEL)
    repository.transition(item_id, ItemState.AWAITING_BACKUP_CONFIRMATION)
    repository.confirm_batch(batch["id"], 1)
    repository.transition(item_id, ItemState.PURGED_FROM_PIXEL)

    available = repository.list_files(unbatched_only=True)

    assert len(available) == 1
    assert available[0]["previous_batch_count"] == 1
    assert available[0]["previously_confirmed"] == 1
    assert available[0]["previously_purged"] == 1


def test_batch_reports_stalled_transfer_activity(repository: Repository, tmp_path: Path) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("Stalled", [record["id"]], 1)
    item_id = batch["items"][0]["id"]
    repository.transition(item_id, ItemState.TRANSFERRING)
    stale = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    repository.db.execute(
        "UPDATE batch_items SET updated_at=? WHERE id=?",
        (stale, item_id),
    )

    stalled = repository.get_batch(batch["id"])

    assert stalled["stalled"] is True
    assert stalled["stalled_for_seconds"] >= 15 * 60
    assert stalled["stall_reason"] == "Transfer has not reported progress"


def test_storage_limited_series_advances_before_confirmation_only_when_next_part_fits(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "balanced"
    root_path.mkdir()
    for name, size in (("a.jpg", 8), ("b.jpg", 7), ("c.jpg", 6), ("d.jpg", 5)):
        (root_path / name).write_bytes(b"x" * size)
    root = repository.add_root("Balanced", str(root_path))
    scan = repository.scan_root(root["id"])

    batches = repository.create_batches(
        "Balanced archive",
        [file["id"] for file in scan["files"]],
        1,
        max_bytes=15,
    )

    assert [batch["total_bytes"] for batch in batches] == [13, 13]
    assert [batch["series_index"] for batch in batches] == [1, 2]
    assert all(batch["series_total"] == 2 for batch in batches)
    assert all(batch["planned_capacity_bytes"] == 15 for batch in batches)
    assert all(batch["split_reason"] == "pixel_storage" for batch in batches)
    assert len({batch["series_id"] for batch in batches}) == 1

    first, second = batches
    assert second["series_blocked"] is True
    assert repository.next_work_item()["batch_id"] == first["id"]
    for item in first["items"]:
        repository.transition(item["id"], ItemState.TRANSFERRING)
        repository.transition(item["id"], ItemState.STAGED_ON_PIXEL)
        repository.transition(item["id"], ItemState.AWAITING_BACKUP_CONFIRMATION)
    assert repository.get_batch(second["id"])["series_blocked"] is False
    assert repository.next_work_item(available_bytes=12) is None
    assert repository.next_work_item(available_bytes=13)["batch_id"] == second["id"]


def test_different_source_folders_create_distinct_batches_and_keep_names(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "family"
    first_folder = root_path / "2024"
    second_folder = root_path / "2025"
    first_folder.mkdir(parents=True)
    second_folder.mkdir()
    first_photo = first_folder / "IMG_0001.JPG"
    second_photo = second_folder / "IMG_0002.JPG"
    first_photo.write_bytes(b"first")
    second_photo.write_bytes(b"second")
    root = repository.add_root("Family", str(root_path))
    scan = repository.scan_root(root["id"])

    batches = repository.create_batches(
        "Family archive",
        [file["id"] for file in scan["files"]],
        1,
    )

    assert {batch["name"] for batch in batches} == {
        "Family archive · 2024",
        "Family archive · 2025",
    }
    assert len(batches) == 2
    for batch in batches:
        parents = {Path(item["path"]).parent for item in batch["items"]}
        assert len(parents) == 1
        assert {Path(item["remote_path"]).name for item in batch["items"]} == {
            Path(item["path"]).name for item in batch["items"]
        }


def test_archive_names_default_to_containing_folder_names(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "archive"
    vacation = root_path / "Trips" / "Summer 2025"
    birthday = root_path / "Family" / "Birthday"
    vacation.mkdir(parents=True)
    birthday.mkdir(parents=True)
    (vacation / "beach.jpg").write_bytes(b"beach")
    (birthday / "cake.jpg").write_bytes(b"cake")
    root = repository.add_root("Archive", str(root_path))
    scan = repository.scan_root(root["id"])

    batches = repository.create_batches(
        None,
        [file["id"] for file in scan["files"]],
        1,
    )

    assert {batch["name"] for batch in batches} == {"Summer 2025", "Birthday"}


def test_browser_upload_prefix_is_not_used_as_pixel_filename(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "imports"
    upload_folder = root_path / "pixel-relay-imports"
    upload_folder.mkdir(parents=True)
    stored = upload_folder / f"{'a' * 32}-birthday-photo.jpg"
    stored.write_bytes(b"uploaded")
    root = repository.add_root("Uploads", str(root_path))
    record = repository.register_file(stored, root["id"])

    item = repository.create_batch("Upload", [record["id"]], 1)["items"][0]

    assert Path(item["remote_path"]).name == "birthday-photo.jpg"


def test_pixel_filename_preserves_spaces_unicode_and_extension_case(
    repository: Repository, tmp_path: Path
) -> None:
    root_path = tmp_path / "named"
    root_path.mkdir()
    media = root_path / "Family fête (Final).JPG"
    media.write_bytes(b"named-photo")
    root = repository.add_root("Named", str(root_path))
    record = repository.register_file(media, root["id"])

    item = repository.create_batch("Named", [record["id"]], 1)["items"][0]

    assert Path(item["remote_path"]).name == media.name


def test_batch_deletion_preserves_sources_and_rejects_active_batch(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    batch = repository.create_batch("Delete me", [record["id"]], 1)

    deleted = repository.delete_batch(batch["id"], 1)

    assert deleted["file_count"] == 1
    assert media.read_bytes() == b"not-a-real-jpeg-but-safe-for-transfer"
    assert repository.db.fetchone("SELECT * FROM source_files WHERE id=?", (record["id"],))
    with pytest.raises(DomainError, match="not found"):
        repository.get_batch(batch["id"])

    active = repository.create_batch("Active", [record["id"]], 1)
    repository.transition(active["items"][0]["id"], ItemState.TRANSFERRING)
    with pytest.raises(DomainError) as error:
        repository.delete_batch(active["id"], 1)
    assert error.value.code == "batch_delete_unsafe"


def test_batch_cancellation_stops_queue_and_tracks_pixel_cleanup(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    queued = repository.create_batch("Cancel queued", [record["id"]], 1)

    cancelled = repository.cancel_batch(queued["id"], 1)

    assert cancelled["cancelled_at"]
    assert cancelled["states"] == {"cancelled": 1}
    assert repository.next_work_item() is None
    assert media.is_file()

    staged = repository.create_batch("Cancel staged", [record["id"]], 1)
    item_id = staged["items"][0]["id"]
    repository.transition(item_id, ItemState.TRANSFERRING)
    repository.transition(item_id, ItemState.STAGED_ON_PIXEL)

    cancelled_staged = repository.cancel_batch(staged["id"], 1)

    assert cancelled_staged["states"] == {"cancelled_on_pixel": 1}


def test_batch_pause_stops_new_work_and_resume_preserves_progress(
    repository: Repository,
    tmp_path: Path,
) -> None:
    record, media = source_file(repository, tmp_path)
    batch = repository.create_batch("Pause safely", [record["id"]], 1)

    paused = repository.pause_batch(batch["id"], 1)

    assert paused["paused_at"]
    assert paused["paused_by"] == 1
    assert repository.next_work_item() is None
    assert media.is_file()

    resumed = repository.resume_batch(batch["id"], 1)

    assert resumed["paused_at"] is None
    assert resumed["paused_by"] is None
    assert resumed["states"] == {"queued": 1}
    assert repository.next_work_item()["batch_id"] == batch["id"]
    assert media.is_file()
    assert [entry["action"] for entry in repository.audit_entries(10)[:2]] == [
        "batch.resume",
        "batch.pause",
    ]


def test_settled_cancelled_batch_entry_can_be_deleted(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    batch = repository.create_batch("Cancelled entry", [record["id"]], 1)
    item_id = batch["items"][0]["id"]
    repository.transition(item_id, ItemState.TRANSFERRING)
    repository.transition(item_id, ItemState.STAGED_ON_PIXEL)
    cancelled = repository.cancel_batch(batch["id"], 1)
    assert cancelled["states"] == {"cancelled_on_pixel": 1}

    deleted = repository.delete_batch(batch["id"], 1)

    assert deleted["id"] == batch["id"]
    assert media.is_file()
    with pytest.raises(DomainError, match="not found"):
        repository.get_batch(batch["id"])


def test_settled_batch_can_be_retriggered_without_rewriting_history(
    repository: Repository, tmp_path: Path
) -> None:
    record, media = source_file(repository, tmp_path)
    original = repository.create_batch("Family archive", [record["id"]], 1)
    repository.cancel_batch(original["id"], 1)

    rerun = repository.retrigger_batch(original["id"], 1)

    assert rerun["id"] != original["id"]
    assert rerun["name"] == "Family archive · rerun"
    assert rerun["states"] == {"queued": 1}
    assert rerun["items"][0]["source_file_id"] == record["id"]
    assert repository.get_batch(original["id"])["states"] == {"cancelled": 1}
    assert media.is_file()


def test_batch_retrigger_rejects_live_pixel_copies(repository: Repository, tmp_path: Path) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("Active", [record["id"]], 1)

    with pytest.raises(DomainError) as error:
        repository.retrigger_batch(batch["id"], 1)

    assert error.value.code == "batch_retrigger_unsafe"


def test_cancelled_batch_cannot_be_deleted_while_transfer_is_settling(
    repository: Repository, tmp_path: Path
) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("Still settling", [record["id"]], 1)
    repository.transition(batch["items"][0]["id"], ItemState.TRANSFERRING)
    repository.cancel_batch(batch["id"], 1)

    with pytest.raises(DomainError) as error:
        repository.delete_batch(batch["id"], 1)

    assert error.value.code == "batch_delete_unsafe"


def test_path_escape_is_rejected(repository: Repository, tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    root_path.mkdir()
    root = repository.add_root("Root", str(root_path))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError):
        repository.register_file(outside, root["id"])


def test_unsupported_media_is_rejected(repository: Repository, tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    root_path.mkdir()
    document = root_path / "notes.txt"
    document.write_text("not media")
    root = repository.add_root("Root", str(root_path))
    with pytest.raises(DomainError, match="Unsupported"):
        repository.register_file(document, root["id"])


def test_macos_appledouble_files_are_filtered(repository: Repository, tmp_path: Path) -> None:
    root_path = tmp_path / "mac-export"
    root_path.mkdir()
    photo = root_path / "IMG_0001.jpg"
    sidecar = root_path / "._IMG_0001.jpg"
    photo.write_bytes(b"photo")
    sidecar.write_bytes(b"AppleDouble metadata")
    root = repository.add_root("Mac export", str(root_path))

    result = repository.scan_root(root["id"])

    assert [Path(file["path"]).name for file in result["files"]] == ["IMG_0001.jpg"]
    with pytest.raises(DomainError, match="AppleDouble"):
        repository.register_file(sidecar, root["id"])


def test_confirmation_requires_every_item_ready(repository: Repository, tmp_path: Path) -> None:
    record, _media = source_file(repository, tmp_path)
    batch = repository.create_batch("Confirm me", [record["id"]], 1)
    item_id = batch["items"][0]["id"]
    with pytest.raises(DomainError) as error:
        repository.confirm_batch(batch["id"], 1)
    assert error.value.code == "batch_not_ready"

    repository.transition(item_id, ItemState.TRANSFERRING)
    repository.transition(item_id, ItemState.STAGED_ON_PIXEL)
    repository.transition(item_id, ItemState.AWAITING_BACKUP_CONFIRMATION)
    awaiting = repository.list_backed_up_items()
    assert awaiting["total"] == 0
    assert awaiting["uploaded_total"] == 1
    assert awaiting["uploaded_total_bytes"] == record["size"]
    assert awaiting["awaiting_verification_count"] == 1
    assert awaiting["awaiting_verification_bytes"] == record["size"]

    confirmed = repository.confirm_batch(batch["id"], 1)
    assert confirmed["states"] == {"confirmed_backed_up": 1}
    assert confirmed["confirmed_at"]

    inventory = repository.list_backed_up_items()
    assert inventory["total"] == 1
    assert inventory["uploaded_total"] == 1
    assert inventory["awaiting_verification_count"] == 0
    assert inventory["photo_count"] == 1
    assert inventory["uploaded_photo_count"] == 1
    assert inventory["retained_on_pixel_count"] == 1
    assert inventory["items"][0]["path"] == record["path"]
    assert inventory["items"][0]["batch_name"] == "Confirm me"
    assert inventory["items"][0]["state"] == "confirmed_backed_up"

    repository.transition(item_id, ItemState.PURGED_FROM_PIXEL)
    purged = repository.list_backed_up_items()
    assert purged["total"] == 1
    assert purged["retained_on_pixel_count"] == 0
    assert purged["purged_from_pixel_count"] == 1
    assert purged["items"][0]["state"] == "purged_from_pixel"

    repeated = repository.create_batch("Confirm again", [record["id"]], 1)
    repeated_item_id = repeated["items"][0]["id"]
    repository.transition(repeated_item_id, ItemState.TRANSFERRING)
    repository.transition(repeated_item_id, ItemState.STAGED_ON_PIXEL)
    repository.transition(repeated_item_id, ItemState.AWAITING_BACKUP_CONFIRMATION)
    repeated_awaiting = repository.list_backed_up_items()
    assert repeated_awaiting["uploaded_total"] == 1
    assert repeated_awaiting["awaiting_verification_count"] == 0
    repository.confirm_batch(repeated["id"], 1)

    deduplicated = repository.list_backed_up_items()
    assert deduplicated["total"] == 1
    assert deduplicated["total_bytes"] == record["size"]
    assert deduplicated["retained_on_pixel_count"] == 1
    assert deduplicated["purged_from_pixel_count"] == 0
    assert deduplicated["items"][0]["confirmation_count"] == 2
    assert deduplicated["items"][0]["retained_copy_count"] == 1
    assert deduplicated["items"][0]["purged_copy_count"] == 1
    assert deduplicated["items"][0]["batch_name"] == "Confirm again"


def test_unique_upload_totals_include_unverified_content_without_double_counting(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "aggregate-uploads"
    root_path.mkdir()
    confirmed_photo = root_path / "confirmed.jpg"
    awaiting_video = root_path / "awaiting.mp4"
    confirmed_photo.write_bytes(b"confirmed-photo")
    awaiting_video.write_bytes(b"awaiting-video")
    root = repository.add_root("Aggregate", str(root_path))
    photo_record = repository.register_file(confirmed_photo, root["id"])
    video_record = repository.register_file(awaiting_video, root["id"])

    confirmed_batch = repository.create_batch("Confirmed", [photo_record["id"]], 1)
    confirmed_item = confirmed_batch["items"][0]["id"]
    repository.transition(confirmed_item, ItemState.TRANSFERRING)
    repository.transition(confirmed_item, ItemState.STAGED_ON_PIXEL)
    repository.transition(confirmed_item, ItemState.AWAITING_BACKUP_CONFIRMATION)
    repository.confirm_batch(confirmed_batch["id"], 1)

    duplicate_batch = repository.create_batch("Duplicate awaiting", [photo_record["id"]], 1)
    duplicate_item = duplicate_batch["items"][0]["id"]
    repository.transition(duplicate_item, ItemState.TRANSFERRING)
    repository.transition(duplicate_item, ItemState.STAGED_ON_PIXEL)
    repository.transition(duplicate_item, ItemState.AWAITING_BACKUP_CONFIRMATION)

    awaiting_batch = repository.create_batch("Awaiting", [video_record["id"]], 1)
    awaiting_item = awaiting_batch["items"][0]["id"]
    repository.transition(awaiting_item, ItemState.TRANSFERRING)
    repository.transition(awaiting_item, ItemState.STAGED_ON_PIXEL)
    repository.transition(awaiting_item, ItemState.AWAITING_BACKUP_CONFIRMATION)

    totals = repository.list_backed_up_items()

    assert totals["total"] == 1
    assert totals["uploaded_total"] == 2
    assert totals["uploaded_total_bytes"] == photo_record["size"] + video_record["size"]
    assert totals["awaiting_verification_count"] == 1
    assert totals["awaiting_verification_bytes"] == video_record["size"]
    assert totals["uploaded_photo_count"] == 1
    assert totals["uploaded_video_count"] == 1


def test_password_and_session_are_hashed(repository: Repository) -> None:
    auth = AuthService(repository.db, repository.settings)
    user_id = 1
    row = repository.db.fetchone("SELECT * FROM users WHERE id=?", (user_id,))
    assert row
    assert "sufficiently long password" not in row["password_hash"]
    assert auth.authenticate("admin", "a sufficiently long password")
    assert auth.authenticate("admin", "wrong password") is None

    token, csrf = auth.create_session(user_id)
    assert token not in str(repository.db.fetchall("SELECT * FROM sessions"))
    session = auth.get_session(token)
    assert session and session["csrf_token"] == csrf


def test_short_local_password_is_allowed(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "short-password")
    settings.prepare()
    database = Database(settings.database_path)
    database.migrate()
    auth = AuthService(database, settings)
    user_id = auth.create_admin("local", "1234")
    assert auth.authenticate("local", "1234")["id"] == user_id


def test_invalid_transition_is_rejected(repository: Repository, tmp_path: Path) -> None:
    record, _media = source_file(repository, tmp_path)
    item = repository.create_batch("Invalid", [record["id"]], 1)["items"][0]
    with pytest.raises(DomainError) as error:
        repository.transition(item["id"], ItemState.PURGED_FROM_PIXEL)
    assert error.value.code == "invalid_transition"

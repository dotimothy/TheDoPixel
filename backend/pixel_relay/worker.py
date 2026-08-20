from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from prometheus_client import Counter, Gauge

from .adb import AdbError, ChecksumMismatch, DeviceOffline, SourceUnavailable, StorageMissing
from .database import Database, utcnow
from .events import EventBroker
from .files import local_path, sha256_file, source_path_name
from .repository import DomainError, Repository
from .states import ItemState
from .transport import DeviceTransport

logger = logging.getLogger(__name__)
adb_progress_logger = logging.getLogger("pixel_relay.adb.progress")
ftp_progress_logger = logging.getLogger("pixel_relay.ftp.progress")

TRANSFERRED_BYTES = Counter(
    "pixel_relay_transferred_bytes_total", "Bytes successfully staged on the Pixel"
)
TRANSFER_FAILURES = Counter("pixel_relay_transfer_failures_total", "Transfer failures", ["code"])
QUEUE_ITEMS = Gauge("pixel_relay_queue_items", "Queue items by state", ["state"])
DEVICE_ONLINE = Gauge("pixel_relay_device_online", "Whether the Pixel is online")
DEVICE_TEMPERATURE = Gauge("pixel_relay_device_temperature_celsius", "Pixel battery temperature")


class RelayWorker:
    def __init__(
        self,
        db: Database,
        repository: Repository,
        adb: DeviceTransport,
        events: EventBroker,
    ):
        self.db = db
        self.repository = repository
        self.adb = adb
        self.events = events
        self.settings = repository.settings
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.active_batch_id: str | None = None
        queue_mode = repository.setting("queue_mode", "running")
        self.queue_mode = (
            queue_mode if queue_mode in {"running", "draining", "stopped"} else "running"
        )
        self.queue_drain_batch_id = (
            repository.setting("queue_drain_batch_id") or None
            if self.queue_mode == "draining"
            else None
        )
        if self.queue_mode == "draining" and not self.queue_drain_batch_id:
            self.queue_mode = "stopped"
        self.maintenance_reason: str | None = None
        self.storage_adoption: dict | None = None
        self.storage_adoption_task: asyncio.Task | None = None
        self.storage_primary_switch: dict | None = None
        self.storage_primary_switch_task: asyncio.Task | None = None
        self.latest_device: dict = {"state": "unknown", "serial": self.settings.device_serial}
        self.latest_storage_media: dict | None = None

    async def start(self) -> None:
        logger.info(
            "Relay worker starting",
            extra={
                "context": {
                    "connection_mode": self.settings.connection_mode,
                    "device_poll_seconds": self.settings.device_poll_seconds,
                }
            },
        )
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run_queue(), name="pixel-relay-queue"),
            asyncio.create_task(self._monitor_device(), name="pixel-relay-device"),
        ]

    async def stop(self) -> None:
        logger.info(
            "Relay worker stopping",
            extra={"context": {"active_tasks": len(self._tasks)}},
        )
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Relay worker stopped")

    def wake(self) -> None:
        self._wake.set()

    async def refresh_device(self) -> dict:
        snapshot = await self.adb.snapshot(self.repository.expected_uuid())
        data = snapshot.dict()
        self.latest_device = data
        self.db.execute(
            "INSERT INTO device_samples(status_json, created_at) VALUES (?, ?)",
            (json.dumps(data), utcnow()),
        )
        self.db.execute(
            """
            DELETE FROM device_samples
            WHERE id NOT IN (
              SELECT id FROM device_samples ORDER BY id DESC LIMIT 10000
            )
            """
        )
        DEVICE_ONLINE.set(1 if snapshot.state == "device" else 0)
        if snapshot.temperature_c is not None:
            DEVICE_TEMPERATURE.set(snapshot.temperature_c)
        logger.log(
            logging.INFO if snapshot.state == "device" else logging.WARNING,
            "Pixel device snapshot recorded",
            extra={
                "context": {
                    "state": snapshot.state,
                    "connection_mode": snapshot.connection_mode,
                    "model": snapshot.model,
                    "android_version": snapshot.android_version,
                    "battery_level": snapshot.battery_level,
                    "temperature_c": snapshot.temperature_c,
                    "charging": snapshot.charging,
                    "ethernet": snapshot.ethernet,
                    "network_type": snapshot.network_type,
                    "network_interface": snapshot.network_interface,
                    "network_addresses": snapshot.network_addresses,
                    "network_gateway": snapshot.network_gateway,
                    "network_dns_servers": snapshot.network_dns_servers,
                    "network_ssid": snapshot.network_ssid,
                    "network_validated": snapshot.network_validated,
                    "network_metered": snapshot.network_metered,
                    "storage_free_bytes": snapshot.storage_free_bytes,
                    "internal_storage_free_bytes": snapshot.internal_storage_free_bytes,
                    "storage_ready": snapshot.storage_ready,
                    "photos_installed": snapshot.photos_installed,
                    "photos_enabled": snapshot.photos_enabled,
                    "photos_running": snapshot.photos_running,
                    "error": snapshot.error,
                }
            },
        )
        await self.events.publish("device", data)
        await self._resume_paused(data)
        return data

    async def _monitor_device(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh_device()
                self._update_queue_metrics()
            except Exception:
                logger.exception("Device monitor failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.device_poll_seconds)

    async def _resume_paused(self, snapshot: dict) -> None:
        if snapshot.get("state") != "device" or not snapshot.get("storage_ready"):
            return
        temperature = snapshot.get("temperature_c")
        rows = self.db.fetchall(
            """
            SELECT id, state, resume_state FROM batch_items
            WHERE state IN ('device_offline', 'storage_missing', 'temperature_paused')
            """
        )
        for item in rows:
            if (
                item["state"] == ItemState.TEMPERATURE_PAUSED
                and temperature is not None
                and temperature >= self.settings.resume_temperature_c
            ):
                continue
            target = ItemState(item["resume_state"] or ItemState.QUEUED)
            try:
                self.repository.transition(item["id"], target, detail="Device condition recovered")
                await self.events.publish(
                    "queue", {"item_id": item["id"], "state": target, "automatic": True}
                )
                self.wake()
            except DomainError:
                logger.exception("Could not resume paused item %s", item["id"])

    async def _run_queue(self) -> None:
        while not self._stop.is_set():
            if self.maintenance_reason:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                continue
            if self.queue_mode == "stopped":
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=10)
                continue
            if self.latest_device.get("state") == "unknown":
                try:
                    await self.refresh_device()
                except Exception:
                    logger.exception("Initial storage-capacity refresh failed")
            item = self.repository.next_work_item(
                available_bytes=self.available_transfer_bytes(),
                batch_id=(self.queue_drain_batch_id if self.queue_mode == "draining" else None),
            )
            if not item:
                if self.queue_mode == "draining":
                    await self._finish_queue_drain()
                    continue
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=10)
                continue
            self.active_batch_id = item["batch_id"]
            await self.events.publish(
                "batch",
                {
                    "action": "active",
                    "batch_id": item["batch_id"],
                    "item_id": item["id"],
                },
            )
            try:
                await self._process(item)
            finally:
                self.active_batch_id = None
                await self.events.publish(
                    "batch",
                    {
                        "action": "idle",
                        "batch_id": item["batch_id"],
                        "item_id": item["id"],
                    },
                )

    def queue_status(self) -> dict:
        return {
            "mode": self.queue_mode,
            "running": self.queue_mode == "running",
            "drain_requested": self.queue_mode == "draining",
            "stopped": self.queue_mode == "stopped",
            "drain_batch_id": self.queue_drain_batch_id,
            "active_batch_id": self.active_batch_id,
        }

    def request_queue_stop(self, user_id: int) -> dict:
        if self.queue_mode == "stopped":
            return self.queue_status()
        active_batch_id = self.active_batch_id
        self.queue_mode = "draining" if active_batch_id else "stopped"
        self.queue_drain_batch_id = active_batch_id
        self._persist_queue_mode(user_id)
        self.db.audit(
            "queue.stop_requested",
            "queue",
            active_batch_id,
            user_id,
            {"mode": self.queue_mode},
        )
        self.wake()
        return self.queue_status()

    def start_queue(self, user_id: int) -> dict:
        self.queue_mode = "running"
        self.queue_drain_batch_id = None
        self._persist_queue_mode(user_id)
        self.db.audit("queue.start", "queue", user_id=user_id)
        self.wake()
        return self.queue_status()

    def _persist_queue_mode(self, user_id: int | None = None) -> None:
        self.repository.set_setting("queue_mode", self.queue_mode, user_id)
        self.repository.set_setting(
            "queue_drain_batch_id",
            self.queue_drain_batch_id or "",
            user_id,
        )

    async def _finish_queue_drain(self) -> None:
        drained_batch_id = self.queue_drain_batch_id
        self.queue_mode = "stopped"
        self.queue_drain_batch_id = None
        self._persist_queue_mode()
        self.db.audit("queue.stopped", "queue", drained_batch_id)
        await self.events.publish(
            "queue",
            {
                "action": "stopped",
                "after_batch_id": drained_batch_id,
            },
        )

    async def _process(self, item: dict) -> None:
        item_id = item["id"]
        self._log_adb_progress(item, "checking device and storage readiness")
        try:
            snapshot = await self.adb.ensure_ready(self.repository.expected_uuid())
            self._record_live_storage(snapshot)
            if (
                snapshot.temperature_c is not None
                and snapshot.temperature_c >= self.settings.pause_temperature_c
            ):
                self.repository.transition(
                    item_id,
                    ItemState.TEMPERATURE_PAUSED,
                    detail=f"Battery temperature is {snapshot.temperature_c:.1f}°C",
                    error_code="temperature_high",
                    resume_state=ItemState.QUEUED,
                )
                self._log_adb_progress(
                    item,
                    f"paused at {snapshot.temperature_c:.1f}°C",
                    level=logging.WARNING,
                )
                return
            reserve = max(
                self.settings.reserve_bytes,
                int((snapshot.storage_total_bytes or 0) * self.settings.reserve_percent / 100),
            )
            if (
                snapshot.storage_free_bytes is not None
                and snapshot.storage_free_bytes - item["size"] < reserve
            ):
                raise StorageMissing("Insufficient Pixel free space after safety reserve")
            source = local_path(item["path"])
            if not source.is_file():
                hint = (
                    " Reconnect the drive with the same drive letter, then rescan the source."
                    if os.name == "nt"
                    else " Reconnect or remount the source drive, then rescan the source."
                )
                raise SourceUnavailable(f"Source file cannot be found: {item['path']}.{hint}")
            if source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
                raise ChecksumMismatch("Source file changed after batch creation")
            if await self._stop_if_cancelled(item, pixel_copy_possible=False):
                return

            self._log_adb_progress(
                item,
                f"source verified; transferring {item['media_kind']} ({item['size']} bytes)",
            )
            self.repository.transition(item_id, ItemState.TRANSFERRING, detail="Transfer started")
            await self.events.publish(
                "queue", {"item_id": item_id, "state": ItemState.TRANSFERRING}
            )
            if await self._stop_if_cancelled(item, pixel_copy_possible=False):
                return
            remote_directory = item["remote_path"].rsplit("/", 1)[0]
            await self.adb.ensure_directory(remote_directory)
            transfer_progress = self._transfer_progress_callback(item)

            remote_hash = ""
            with contextlib.suppress(AdbError):
                remote_hash = await self.adb.remote_sha256(item["remote_path"])
            copied_to_pixel = remote_hash != item["sha256"]
            if remote_hash != item["sha256"]:
                self._log_adb_progress(item, "starting adb push")
                await self.adb.push(source, item["remote_path"], transfer_progress)
                self._log_adb_progress(item, "adb push complete; verifying Pixel checksum")
                remote_hash = await self.adb.remote_sha256(item["remote_path"])
            else:
                transfer_progress(item["size"], item["size"])
                self._log_adb_progress(item, "existing Pixel copy already matches checksum")
            if remote_hash != item["sha256"]:
                raise ChecksumMismatch("Pixel checksum did not match the source")
            if copied_to_pixel:
                self._consume_estimated_storage(item["size"])
            if await self._stop_if_cancelled(item, pixel_copy_possible=True):
                return

            self._log_adb_progress(item, "Pixel SHA-256 checksum verified")
            self.repository.transition(
                item_id, ItemState.STAGED_ON_PIXEL, detail="SHA-256 verified"
            )
            TRANSFERRED_BYTES.inc(item["size"])
            if await self._stop_if_cancelled(item, pixel_copy_possible=True):
                return
            media_indexed = await self.adb.scan_media(item["remote_path"])
            raw_media_fallback = (
                not media_indexed and item["extension"].lower() in self.settings.raw_extensions
            )
            if not media_indexed and not raw_media_fallback:
                self.repository.transition(
                    item_id,
                    ItemState.MEDIA_SCAN_FAILED,
                    detail="MediaStore did not return the staged file",
                    error_code="media_scan_failed",
                )
                await self.events.publish(
                    "queue", {"item_id": item_id, "state": ItemState.MEDIA_SCAN_FAILED}
                )
                self._log_adb_progress(item, "MediaStore indexing failed", level=logging.WARNING)
                return
            if raw_media_fallback:
                self._log_adb_progress(
                    item,
                    "generic MediaStore query did not return RAW; allowing manual "
                    "Google Photos confirmation",
                    level=logging.WARNING,
                )
            if await self._stop_if_cancelled(item, pixel_copy_possible=True):
                return
            self.repository.transition(
                item_id,
                ItemState.AWAITING_BACKUP_CONFIRMATION,
                detail=(
                    "RAW staged; generic MediaStore confirmation unavailable"
                    if raw_media_fallback
                    else "MediaStore scan verified"
                ),
            )
            await self.events.publish(
                "queue",
                {"item_id": item_id, "state": ItemState.AWAITING_BACKUP_CONFIRMATION},
            )
            self._log_adb_progress(
                item,
                (
                    "RAW staged and awaiting manual Google Photos confirmation"
                    if raw_media_fallback
                    else "indexed and awaiting Google Photos confirmation"
                ),
            )
        except DeviceOffline as exc:
            if await self._stop_if_cancelled(item):
                return
            self._log_adb_progress(item, f"paused: {exc}", level=logging.WARNING)
            self._pause(item_id, ItemState.DEVICE_OFFLINE, exc, ItemState.QUEUED)
        except StorageMissing as exc:
            if await self._stop_if_cancelled(item):
                return
            self._log_adb_progress(item, f"paused: {exc}", level=logging.WARNING)
            self._pause(item_id, ItemState.STORAGE_MISSING, exc, ItemState.QUEUED)
        except AdbError as exc:
            if await self._stop_if_cancelled(item):
                return
            self._log_adb_progress(item, f"failed: {exc}", level=logging.ERROR)
            current = self.repository.get_item(item_id)
            if current and current["state"] in {ItemState.QUEUED, ItemState.TRANSFERRING}:
                self.repository.transition(
                    item_id,
                    ItemState.TRANSFER_FAILED,
                    detail=str(exc),
                    error_code=exc.code,
                )
                TRANSFER_FAILURES.labels(code=exc.code).inc()
                await self.events.publish(
                    "queue",
                    {"item_id": item_id, "state": ItemState.TRANSFER_FAILED, "error": str(exc)},
                )
        except Exception as exc:
            if await self._stop_if_cancelled(item):
                return
            self._log_adb_progress(item, f"unexpected failure: {exc}", level=logging.ERROR)
            logger.exception("Unexpected queue failure for %s", item_id)
            current = self.repository.get_item(item_id)
            if current and current["state"] in {ItemState.QUEUED, ItemState.TRANSFERRING}:
                self.repository.transition(
                    item_id,
                    ItemState.TRANSFER_FAILED,
                    detail=str(exc),
                    error_code="internal_error",
                )
                TRANSFER_FAILURES.labels(code="internal_error").inc()
        finally:
            self._update_queue_metrics()

    def available_transfer_bytes(self) -> int | None:
        free = self.latest_device.get("storage_free_bytes")
        total = self.latest_device.get("storage_total_bytes")
        if not isinstance(free, int) or free < 0:
            return None
        reserve = max(
            self.settings.reserve_bytes,
            int(total * self.settings.reserve_percent / 100)
            if isinstance(total, int) and total > 0
            else 0,
        )
        return max(0, free - reserve)

    def _record_live_storage(self, snapshot: object) -> None:
        for field in ("storage_free_bytes", "storage_total_bytes", "storage_used_bytes"):
            value = getattr(snapshot, field, None)
            if isinstance(value, int) and value >= 0:
                self.latest_device[field] = value

    def _consume_estimated_storage(self, size: int) -> None:
        """Keep scheduling conservative between device-monitor refreshes."""
        free = self.latest_device.get("storage_free_bytes")
        if isinstance(free, int):
            self.latest_device["storage_free_bytes"] = max(0, free - size)
        used = self.latest_device.get("storage_used_bytes")
        if isinstance(used, int):
            self.latest_device["storage_used_bytes"] = used + size

    def _transfer_progress_callback(self, item: dict):
        loop = asyncio.get_running_loop()
        last_bytes = -1
        last_update = 0.0

        def report(transferred_bytes: int, total_bytes: int) -> None:
            nonlocal last_bytes, last_update
            now = time.monotonic()
            minimum_step = max(256 * 1024, total_bytes // 200)
            complete = transferred_bytes >= total_bytes
            if (
                not complete
                and last_bytes >= 0
                and transferred_bytes - last_bytes < minimum_step
                and now - last_update < 0.5
            ):
                return
            last_bytes = transferred_bytes
            last_update = now
            self.repository.update_transfer_progress(
                item["id"],
                transferred_bytes,
                total_bytes,
            )
            payload = {
                "item_id": item["id"],
                "batch_id": item["batch_id"],
                "state": ItemState.TRANSFERRING,
                "transferred_bytes": min(transferred_bytes, total_bytes),
                "total_bytes": total_bytes,
            }
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.events.publish("queue", payload))
            )

        return report

    async def _stop_if_cancelled(
        self,
        item: dict,
        *,
        pixel_copy_possible: bool | None = None,
    ) -> bool:
        if not self.repository.batch_cancelled(item["batch_id"]):
            return False
        current = self.repository.get_item(item["id"])
        if not current:
            return True
        state = ItemState(current["state"])
        if state in {
            ItemState.CANCELLED,
            ItemState.CANCELLED_ON_PIXEL,
            ItemState.PURGED_FROM_PIXEL,
        }:
            return True
        if pixel_copy_possible is None:
            pixel_copy_possible = (
                state
                in {
                    ItemState.TRANSFERRING,
                    ItemState.STAGED_ON_PIXEL,
                    ItemState.MEDIA_SCAN_FAILED,
                    ItemState.AWAITING_BACKUP_CONFIRMATION,
                }
                or int(current.get("attempts") or 0) > 0
            )
        target = ItemState.CANCELLED_ON_PIXEL if pixel_copy_possible else ItemState.CANCELLED
        self.repository.transition(
            item["id"],
            target,
            detail="Batch cancellation acknowledged by worker",
        )
        await self.events.publish(
            "queue",
            {"item_id": item["id"], "state": target, "automatic": True},
        )
        self._log_adb_progress(
            item,
            "cancelled; Pixel copy retained for cleanup"
            if target == ItemState.CANCELLED_ON_PIXEL
            else "cancelled before creating a Pixel copy",
        )
        return True

    def _log_adb_progress(
        self,
        item: dict,
        message: str,
        *,
        level: int = logging.INFO,
    ) -> None:
        batch = self.db.fetchone("SELECT name FROM batches WHERE id=?", (item["batch_id"],))
        progress = self.db.fetchone(
            """
            SELECT COUNT(*) AS total,
              SUM(
                CASE WHEN state IN (
                  'awaiting_backup_confirmation',
                  'confirmed_backed_up',
                  'purged_from_pixel'
                ) THEN 1 ELSE 0 END
              ) AS ready
            FROM batch_items
            WHERE batch_id=?
            """,
            (item["batch_id"],),
        )
        transport_name = "FTP" if self.settings.connection_mode == "ftp" else "ADB"
        progress_logger = (
            ftp_progress_logger if self.settings.connection_mode == "ftp" else adb_progress_logger
        )
        ready = int(progress["ready"] or 0) if progress else 0
        total = int(progress["total"] or 0) if progress else 0
        progress_logger.log(
            level,
            "%s batch %r [%s], item %s: %s (%d/%d items ready)",
            transport_name,
            batch["name"] if batch else item["batch_id"],
            item["batch_id"][:8],
            item["id"][:8],
            message,
            ready,
            total,
            extra={
                "context": {
                    "transport": self.settings.connection_mode,
                    "batch_id": item["batch_id"],
                    "item_id": item["id"],
                    "media_kind": item.get("media_kind"),
                    "source_name": source_path_name(item["path"]),
                    "remote_path": item.get("remote_path"),
                    "item_size_bytes": item.get("size"),
                    "ready_items": ready,
                    "total_items": total,
                }
            },
        )

    def _pause(
        self, item_id: str, state: ItemState, exc: Exception, resume_state: ItemState
    ) -> None:
        current = self.repository.get_item(item_id)
        if current and current["state"] in {ItemState.QUEUED, ItemState.TRANSFERRING}:
            self.repository.transition(
                item_id,
                state,
                detail=str(exc),
                error_code=state,
                resume_state=resume_state,
            )
            asyncio.create_task(
                self.events.publish(
                    "queue", {"item_id": item_id, "state": state, "error": str(exc)}
                )
            )

    async def purge_batch(self, batch_id: str, user_id: int) -> dict:
        batch = self.repository.get_batch(batch_id)
        cancelled = bool(batch.get("cancelled_at"))
        allowed_states = (
            {
                ItemState.CANCELLED,
                ItemState.CANCELLED_ON_PIXEL,
                ItemState.PURGED_FROM_PIXEL,
                ItemState.PURGE_FAILED,
            }
            if cancelled
            else {ItemState.CONFIRMED_BACKED_UP, ItemState.PURGED_FROM_PIXEL}
        )
        if not batch["items"] or any(
            item["state"] not in allowed_states for item in batch["items"]
        ):
            raise DomainError(
                "batch_not_confirmed",
                "Every batch item must be confirmed or safely cancelled before purge",
                status_code=409,
            )
        purge_items = [
            item
            for item in batch["items"]
            if item["state"] in {ItemState.CONFIRMED_BACKED_UP, ItemState.CANCELLED_ON_PIXEL}
            or (
                item["state"] == ItemState.PURGE_FAILED
                and item.get("resume_state") == ItemState.CANCELLED_ON_PIXEL
            )
        ]
        batch_directories = {item["remote_path"].rsplit("/", 2)[0] for item in batch["items"]}
        if (
            len(batch_directories) != 1
            or next(iter(batch_directories)).rsplit("/", 1)[-1] != batch_id
        ):
            raise DomainError(
                "unsafe_batch_path",
                "Batch files do not share their generated destination directory",
                status_code=409,
            )
        batch_directory = next(iter(batch_directories))
        try:
            if purge_items:
                self._log_adb_progress(purge_items[0], "checking device before Pixel purge")
            snapshot = (
                await self.adb.ensure_ready(self.repository.expected_uuid())
                if purge_items
                else None
            )
            if (
                snapshot
                and snapshot.temperature_c is not None
                and snapshot.temperature_c >= self.settings.pause_temperature_c
            ):
                raise DomainError(
                    "temperature_high",
                    "Pixel is too warm to purge safely",
                    status_code=409,
                )
        except DeviceOffline as exc:
            for item in purge_items:
                self.repository.transition(
                    item["id"],
                    ItemState.DEVICE_OFFLINE,
                    detail=str(exc),
                    error_code=exc.code,
                    resume_state=(
                        ItemState.CANCELLED_ON_PIXEL if cancelled else ItemState.CONFIRMED_BACKED_UP
                    ),
                )
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        except StorageMissing as exc:
            for item in purge_items:
                self.repository.transition(
                    item["id"],
                    ItemState.STORAGE_MISSING,
                    detail=str(exc),
                    error_code=exc.code,
                    resume_state=(
                        ItemState.CANCELLED_ON_PIXEL if cancelled else ItemState.CONFIRMED_BACKED_UP
                    ),
                )
            raise DomainError(exc.code, str(exc), status_code=409) from exc

        if purge_items:
            try:
                self._log_adb_progress(
                    purge_items[0],
                    f"removing batch directory with {len(purge_items)} tracked Pixel copies",
                )
                await self.adb.remove_batch_directory(batch_directory)
                self.repository.mark_items_purged([item["id"] for item in purge_items])
                self._log_adb_progress(
                    purge_items[0],
                    f"batch directory removed; {len(purge_items)} tracked copies marked purged",
                )
            except ValueError as exc:
                raise DomainError(
                    "unsafe_batch_path",
                    "Pixel Relay refused an unsafe batch-directory purge",
                    status_code=409,
                ) from exc
            except AdbError as exc:
                for item in purge_items:
                    self._log_adb_progress(
                        item,
                        f"Pixel purge failed: {exc}",
                        level=logging.ERROR,
                    )
                    self.repository.transition(
                        item["id"],
                        ItemState.PURGE_FAILED,
                        detail=str(exc),
                        error_code=exc.code,
                        resume_state=(
                            ItemState.CANCELLED_ON_PIXEL
                            if cancelled
                            else ItemState.CONFIRMED_BACKED_UP
                        ),
                    )
                raise DomainError(exc.code, str(exc), status_code=409) from exc
        result = self.repository.get_batch(batch_id)
        completed_states = (
            {ItemState.CANCELLED, ItemState.PURGED_FROM_PIXEL}
            if cancelled
            else {ItemState.PURGED_FROM_PIXEL}
        )
        if all(item["state"] in completed_states for item in result["items"]):
            self.db.execute("UPDATE batches SET purged_at=? WHERE id=?", (utcnow(), batch_id))
            self.db.audit("batch.purge", "batch", batch_id, user_id)
            result = self.repository.get_batch(batch_id)
        if purge_items:
            with contextlib.suppress(Exception):
                await self.refresh_device()
        await self.events.publish("batch", {"batch_id": batch_id, "action": "purge"})
        logger.info(
            "Pixel batch purge finished",
            extra={
                "context": {
                    "batch_id": batch_id,
                    "item_count": len(result["items"]),
                    "states": result["states"],
                }
            },
        )
        return result

    def _update_queue_metrics(self) -> None:
        summary = self.repository.queue_summary()["states"]
        for state in ItemState:
            QUEUE_ITEMS.labels(state=state).set(summary.get(state, 0))

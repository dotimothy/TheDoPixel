from __future__ import annotations

import asyncio
import ftplib
import hashlib
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adb import (
    AdbError,
    DeviceOffline,
    DeviceSnapshot,
    SafeAdb,
    validate_generated_batch_directory,
)
from .config import Settings

SAFE_FTP_PATH = re.compile(r"^/[^\x00-\x1f\x7f]+$")
logger = logging.getLogger(__name__)


class FtpTransport:
    """A constrained FTP client for Pixel-hosted file servers."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    @staticmethod
    def validate_remote_path(path: str) -> str:
        segments = path.split("/")[1:]
        if (
            not SAFE_FTP_PATH.fullmatch(path)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(len(segment.encode("utf-8")) > 255 for segment in segments)
        ):
            raise ValueError("Unsafe FTP path")
        return path

    @contextmanager
    def _session(self):
        ftp = ftplib.FTP()
        try:
            ftp.connect(
                self.settings.ftp_host,
                self.settings.ftp_port,
                timeout=self.settings.command_timeout_seconds,
            )
            ftp.login(self.settings.ftp_username, self.settings.ftp_password)
            ftp.set_pasv(True)
            ftp.voidcmd("TYPE I")
            yield ftp
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()
        except ftplib.all_errors as exc:
            ftp.close()
            raise DeviceOffline(
                f"FTP server {self.settings.ftp_host}:{self.settings.ftp_port} "
                f"is unavailable: {exc}"
            ) from exc

    async def _execute(
        self,
        operation,
        operation_name: str,
        operation_context: dict[str, Any] | None = None,
    ):
        started = time.perf_counter()
        context = {
            "operation": operation_name,
            "host": self.settings.ftp_host,
            "port": self.settings.ftp_port,
            **(operation_context or {}),
        }
        async with self._lock:
            try:
                result = await asyncio.to_thread(operation)
            except Exception:
                logger.exception(
                    "FTP operation failed",
                    extra={
                        "context": {
                            **context,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000,
                                2,
                            ),
                        }
                    },
                )
                raise
        logger.info(
            "FTP operation completed",
            extra={
                "context": {
                    **context,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            },
        )
        return result

    async def snapshot(self, expected_uuid: str = "") -> DeviceSnapshot:
        snapshot = DeviceSnapshot(
            state="unknown",
            serial=f"ftp://{self.settings.ftp_host}:{self.settings.ftp_port}",
            model="Pixel FTP",
            connection_mode="ftp",
            expected_primary_uuid=expected_uuid or None,
            disks=[],
            volumes=[],
            observed_at=datetime.now(UTC).isoformat(),
        )
        try:
            await self._execute(self._probe, "probe")
            snapshot.state = "device"
            snapshot.storage_ready = True
        except AdbError as exc:
            snapshot.state = exc.code
            snapshot.error = str(exc)
        return snapshot

    async def connection_test(self) -> dict[str, Any]:
        await self._execute(self._probe, "connection_test")
        return {
            "connected": True,
            "server": f"{self.settings.ftp_host}:{self.settings.ftp_port}",
            "destination_root": self.settings.ftp_destination_root,
        }

    def _probe(self) -> None:
        with self._session() as ftp:
            ftp.pwd()

    async def ensure_ready(self, expected_uuid: str) -> DeviceSnapshot:
        snapshot = await self.snapshot(expected_uuid)
        if snapshot.state != "device":
            raise DeviceOffline(snapshot.error or "Pixel FTP server is unavailable")
        return snapshot

    async def ensure_directory(self, path: str) -> None:
        path = self.validate_remote_path(path)

        def create() -> None:
            with self._session() as ftp:
                current = ""
                for segment in path.strip("/").split("/"):
                    current = f"{current}/{segment}"
                    try:
                        ftp.mkd(current)
                    except ftplib.error_perm as exc:
                        if not str(exc).startswith("550"):
                            raise

        await self._execute(create, "ensure_directory", {"remote_path": path})

    async def push(
        self,
        source: Path,
        destination: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        destination = self.validate_remote_path(destination)
        total_bytes = source.stat().st_size

        def upload() -> None:
            transferred = 0

            def block_sent(block: bytes) -> None:
                nonlocal transferred
                transferred += len(block)
                if progress:
                    progress(min(transferred, total_bytes), total_bytes)

            with self._session() as ftp, source.open("rb") as handle:
                if progress:
                    progress(0, total_bytes)
                ftp.storbinary(
                    f"STOR {destination}",
                    handle,
                    blocksize=1024 * 1024,
                    callback=block_sent,
                )
                if progress:
                    progress(total_bytes, total_bytes)

        await self._execute(
            upload,
            "upload",
            {
                "source_name": source.name,
                "source_size_bytes": source.stat().st_size,
                "remote_path": destination,
            },
        )

    async def remote_sha256(self, path: str) -> str:
        path = self.validate_remote_path(path)

        def hash_remote() -> str:
            digest = hashlib.sha256()
            with self._session() as ftp:
                try:
                    ftp.retrbinary(f"RETR {path}", digest.update, blocksize=1024 * 1024)
                except ftplib.error_perm as exc:
                    raise AdbError(f"FTP file is unavailable: {path}") from exc
            return digest.hexdigest()

        return await self._execute(
            hash_remote,
            "remote_sha256",
            {"remote_path": path},
        )

    async def scan_media(self, _path: str) -> bool:
        # Android FTP server apps generally write through MediaStore/SAF. FTP
        # has no portable command that can independently verify Android indexing.
        return True

    async def speed_test(self, size_bytes: int = 32 * 1024**2) -> dict[str, Any]:
        """Measure a verified FTP upload using disposable non-media data."""
        if not 1024**2 <= size_bytes <= 256 * 1024**2:
            raise ValueError("FTP speed-test size must be between 1 MiB and 256 MiB")
        token = uuid.uuid4().hex
        root = self.validate_remote_path(self.settings.ftp_destination_root)
        local_path = self.settings.data_dir / f".ftp-speedtest-{token}.bin"
        remote_path = f"{root}/.pixel-relay-speedtest-{token}.bin"
        removed = False
        try:
            payload = await asyncio.to_thread(os.urandom, size_bytes)
            local_sha256 = hashlib.sha256(payload).hexdigest()
            await asyncio.to_thread(local_path.write_bytes, payload)
            del payload
            await self.ensure_directory(root)

            upload_started = time.perf_counter()
            await self.push(local_path, remote_path)
            upload_seconds = max(time.perf_counter() - upload_started, 0.000001)

            verification_started = time.perf_counter()
            remote_sha256 = await self.remote_sha256(remote_path)
            verification_seconds = max(
                time.perf_counter() - verification_started,
                0.000001,
            )
            if remote_sha256 != local_sha256:
                raise AdbError("FTP speed-test file failed SHA-256 verification")

            await self.remove_file(remote_path)
            removed = True
            upload_rate = size_bytes / upload_seconds
            verification_rate = size_bytes / verification_seconds
            verified_seconds = upload_seconds + verification_seconds
            return {
                "connection_mode": "ftp",
                "server": f"{self.settings.ftp_host}:{self.settings.ftp_port}",
                "size_bytes": size_bytes,
                "upload_duration_seconds": upload_seconds,
                "upload_bytes_per_second": upload_rate,
                "upload_megabytes_per_second": upload_rate / 1024**2,
                "upload_megabits_per_second": upload_rate * 8 / 1_000_000,
                "verification_duration_seconds": verification_seconds,
                "verification_bytes_per_second": verification_rate,
                "verified_duration_seconds": verified_seconds,
                "verified_bytes_per_second": size_bytes / verified_seconds,
                "checksum_verified": True,
                "temporary_files_removed": True,
            }
        finally:
            local_path.unlink(missing_ok=True)
            if not removed:
                with suppress(AdbError):
                    await self.remove_file(remote_path)

    async def remove_file(self, path: str) -> None:
        path = self.validate_remote_path(path)

        def remove() -> None:
            with self._session() as ftp:
                ftp.delete(path)
                try:
                    ftp.size(path)
                except ftplib.error_perm:
                    return
                raise AdbError(f"FTP file still exists after purge: {path}")

        await self._execute(remove, "remove_file", {"remote_path": path})

    async def remove_directory(self, path: str) -> None:
        path = self.validate_remote_path(path)

        def remove() -> None:
            with self._session() as ftp, suppress(ftplib.error_perm):
                ftp.rmd(path)

        await self._execute(remove, "remove_directory", {"remote_path": path})

    async def remove_batch_directory(self, path: str) -> None:
        path = self.validate_remote_path(path)
        root = self.validate_remote_path(self.settings.ftp_destination_root)
        validate_generated_batch_directory(path, root)

        await self._remove_tree(path, recreate=False, operation="remove_batch_directory")

    async def reset_destination_tree(self) -> str:
        path = self.validate_remote_path(self.settings.ftp_destination_root)
        await self._remove_tree(path, recreate=True, operation="reset_destination_tree")
        return path

    async def _remove_tree(self, path: str, *, recreate: bool, operation: str) -> None:

        def remove() -> None:
            with self._session() as ftp:
                pending = [path]
                directories: list[str] = []
                while pending:
                    directory = pending.pop()
                    directories.append(directory)
                    try:
                        entries = list(ftp.mlsd(directory))
                    except ftplib.error_perm as exc:
                        if str(exc).startswith("550") and directory == path:
                            if recreate:
                                ftp.mkd(path)
                            return
                        raise
                    for name, facts in entries:
                        if name in {".", ".."}:
                            continue
                        child = self.validate_remote_path(f"{directory.rstrip('/')}/{name}")
                        entry_type = facts.get("type")
                        if entry_type == "dir":
                            pending.append(child)
                        elif entry_type not in {"cdir", "pdir"}:
                            ftp.delete(child)
                for directory in sorted(
                    directories,
                    key=lambda value: value.count("/"),
                    reverse=True,
                ):
                    ftp.rmd(directory)
                if recreate:
                    ftp.mkd(path)
                    if list(ftp.mlsd(path)):
                        raise AdbError(f"FTP destination is not empty after reset: {path}")

        await self._execute(remove, operation, {"remote_path": path})

    async def trim_caches(self, _desired_free_bytes: int) -> None:
        raise AdbError("Android app-cache cleanup requires USB or network ADB")

    async def storage_inventory(self, root: str) -> list[dict[str, Any]]:
        root = self.validate_remote_path(root)

        def inspect() -> list[dict[str, Any]]:
            files: list[dict[str, Any]] = []
            with self._session() as ftp:
                pending = [root]
                while pending:
                    directory = pending.pop()
                    try:
                        entries = list(ftp.mlsd(directory))
                    except ftplib.all_errors as exc:
                        raise AdbError(
                            "The Pixel FTP server does not support storage inventory"
                        ) from exc
                    for name, facts in entries:
                        if name in {".", ".."}:
                            continue
                        path = self.validate_remote_path(f"{directory.rstrip('/')}/{name}")
                        if facts.get("type") == "dir":
                            pending.append(path)
                        elif facts.get("type") == "file":
                            files.append(
                                {
                                    "path": path,
                                    "allocated_bytes": int(facts.get("size", 0)),
                                }
                            )
                        if len(files) + len(pending) > 100_000:
                            raise AdbError("FTP storage inventory exceeds the safety limit")
            return files

        return await self._execute(
            inspect,
            "storage_inventory",
            {"remote_root": root},
        )


class DeviceTransport:
    """Runtime-switchable transport shared by monitoring and the queue worker."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.adb = SafeAdb(settings)
        self.ftp = FtpTransport(settings)

    @property
    def connection_mode(self) -> str:
        return self.settings.connection_mode

    @connection_mode.setter
    def connection_mode(self, value: str) -> None:
        self.settings.connection_mode = value  # type: ignore[assignment]
        if value in {"network", "usb"}:
            self.adb.connection_mode = value

    @property
    def serial(self) -> str:
        return self.settings.device_serial

    @serial.setter
    def serial(self, value: str) -> None:
        self.settings.device_serial = value
        self.adb.serial = value

    @property
    def active(self) -> Any:
        if self.settings.connection_mode == "ftp":
            return self.ftp
        return self.control

    @property
    def control(self) -> SafeAdb:
        mode = self.settings.connection_mode
        if mode == "ftp":
            mode = "network" if ":" in self.settings.device_serial else "usb"
        self.adb.connection_mode = mode
        self.adb.serial = self.settings.device_serial
        return self.adb

    def ftp_with_overrides(self, overrides: dict[str, Any] | None = None) -> FtpTransport:
        if not overrides:
            return self.ftp
        effective = self.settings.model_copy(update=overrides)
        return FtpTransport(effective)

    async def snapshot(self, expected_uuid: str = "") -> DeviceSnapshot:
        return await self.control.snapshot(expected_uuid)

    async def ensure_ready(self, expected_uuid: str) -> DeviceSnapshot:
        snapshot = await self.control.ensure_ready(expected_uuid)
        if self.settings.connection_mode == "ftp":
            await self.ftp.ensure_ready("")
        return snapshot

    async def ensure_directory(self, path: str) -> None:
        await self.active.ensure_directory(path)

    async def push(
        self,
        source: Path,
        destination: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        await self.active.push(source, destination, progress)

    async def remote_sha256(self, path: str) -> str:
        return await self.active.remote_sha256(path)

    async def scan_media(self, path: str) -> bool:
        if self.settings.connection_mode == "ftp":
            return await self.control.scan_media(self.ftp_path_to_adb(path))
        return await self.active.scan_media(path)

    async def remove_file(self, path: str) -> None:
        await self.active.remove_file(path)

    async def remove_directory(self, path: str) -> None:
        await self.active.remove_directory(path)

    async def remove_batch_directory(self, path: str) -> None:
        await self.active.remove_batch_directory(path)

    async def reset_destination_tree(self) -> str:
        return await self.active.reset_destination_tree()

    async def trim_caches(self, desired_free_bytes: int) -> None:
        await self.control.trim_caches(desired_free_bytes)

    async def enable_tcpip(self, port: int = 5555) -> dict[str, Any]:
        return await self.control.enable_tcpip(port)

    async def restart_server(self) -> dict[str, Any]:
        return await self.control.restart_server()

    async def speed_test(self) -> dict[str, Any]:
        return await self.control.speed_test()

    async def ftp_connection_test(
        self,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.ftp_with_overrides(overrides).connection_test()

    async def ftp_speed_test(
        self,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.ftp_with_overrides(overrides).speed_test()

    async def storage_devices(self) -> dict[str, Any]:
        return await self.control.storage_devices()

    async def adopt_storage(
        self,
        disk_id: str,
        *,
        force_adoptable: bool,
        migrate_primary: bool,
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        return await self.control.adopt_storage(
            disk_id,
            force_adoptable=force_adoptable,
            migrate_primary=migrate_primary,
            progress=progress,
        )

    async def switch_primary_storage(
        self,
        target_uuid: str,
        *,
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        return await self.control.switch_primary_storage(target_uuid, progress=progress)

    async def unmount_storage(self, disk_id: str) -> dict[str, Any]:
        return await self.control.unmount_storage(disk_id)

    async def storage_inventory(self, root: str) -> list[dict[str, Any]]:
        return await self.active.storage_inventory(root)

    def ftp_path_to_adb(self, path: str) -> str:
        ftp_root = self.settings.ftp_destination_root.rstrip("/")
        adb_root = self.settings.destination_root.rstrip("/")
        if path == ftp_root:
            return adb_root
        if path.startswith(f"{ftp_root}/"):
            return f"{adb_root}{path[len(ftp_root) :]}"
        return path

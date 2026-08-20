from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Settings

logger = logging.getLogger(__name__)

SAFE_REMOTE_PATH = re.compile(r"^/sdcard/[^\x00-\x1f\x7f]+$")
SAFE_UUID = re.compile(r"^[A-Za-z0-9._-]*$")
SAFE_DISK_ID = re.compile(r"^disk:\d+,\d+$")
SAFE_PHYSICAL_VOLUME_ID = re.compile(r"^(?:public|private|stub):\d+,\d+$")
SAFE_BATCH_DIRECTORY_NAME = re.compile(r"^[0-9a-f]{32}$")


def validate_generated_batch_directory(path: str, destination_root: str) -> str:
    """Allow recursive deletion only for one generated batch directory."""
    root = destination_root.rstrip("/")
    parent, separator, name = path.rpartition("/")
    if separator != "/" or parent != root or not SAFE_BATCH_DIRECTORY_NAME.fullmatch(name):
        raise ValueError("Batch directory must be a generated UUID beneath the destination root")
    return path


def output_preview(output: bytes, limit: int = 512) -> str | None:
    """Return a bounded, single-line-safe preview for diagnostic logs."""
    text = output.decode(errors="replace").replace("\r", "").strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… ({len(text) - limit} more characters)"


def primary_storage_move_guidance(output: str) -> str:
    """Translate Android PackageManager move codes into operator guidance."""
    match = re.search(r"Failure\s*\[\s*(-?\d+)\s*\]", output, re.IGNORECASE)
    code = int(match.group(1)) if match else None
    guidance = {
        -1: (
            "The destination does not have enough free space. Free space on the "
            "selected medium and retry."
        ),
        -5: (
            "Android considers the destination invalid. Confirm the drive is adopted, "
            "mounted, and still reported with this UUID."
        ),
        -6: (
            "Android encountered an internal storage-migration error. Reboot the Pixel, "
            "unlock it fully, verify the drive is mounted, and retry."
        ),
        -7: (
            "Android already has another package or storage move pending. Wait for it "
            "to finish, then retry."
        ),
        -8: (
            "A device-administrator app prevents this storage move. Disable the relevant "
            "device administrator before retrying."
        ),
        -9: "Android policy does not allow this move to internal storage.",
        -10: (
            "Android refused because the user profile is locked. Unlock the Pixel with "
            "its PIN, pattern, or password and wait for the home screen before retrying."
        ),
    }
    return guidance.get(
        code,
        "Keep the Pixel unlocked, verify the selected drive is mounted, and check "
        "available space before retrying.",
    )


def parse_adb_progress(output: str) -> int | None:
    """Extract the latest percentage emitted by `adb push -p`."""
    matches = re.findall(r"(?<!\d)(100|[1-9]?\d)%(?!\d)", output)
    return int(matches[-1]) if matches else None


def ipv4_first(values: list[str]) -> list[str]:
    """Keep the source order within each family while presenting IPv4 first."""
    return sorted(values, key=lambda value: 1 if ":" in value.split("/", 1)[0] else 0)


def parse_ipv4_addresses(output: str) -> list[str]:
    """Extract unique global IPv4 addresses from Android `ip` output."""
    addresses = re.findall(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/\d+", output)
    route_sources = re.findall(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", output)
    return list(dict.fromkeys([*route_sources, *addresses]))


def parse_port_listeners(output: str, port: int) -> list[dict[str, Any]]:
    """Extract listeners for one exact TCP port without changing Pixel state."""
    listeners: list[dict[str, Any]] = []
    port_pattern = re.compile(rf"(?:\]:|:){port}\b")
    for line in output.splitlines():
        if "LISTEN" not in line.upper() or not port_pattern.search(line):
            continue
        processes = re.findall(r'\("([^"]+)",pid=(\d+),fd=(\d+)\)', line)
        if processes:
            listeners.extend(
                {
                    "name": name,
                    "pid": int(pid),
                    "fd": int(fd),
                    "local_address": next(
                        (token for token in line.split() if port_pattern.search(token)),
                        f"*:{port}",
                    ),
                }
                for name, pid, fd in processes
            )
        else:
            listeners.append(
                {
                    "name": None,
                    "pid": None,
                    "fd": None,
                    "local_address": next(
                        (token for token in line.split() if port_pattern.search(token)),
                        f"*:{port}",
                    ),
                }
            )
    return listeners


class AdbError(RuntimeError):
    code = "adb_error"

    def __init__(self, message: str, *, output: str = ""):
        super().__init__(message)
        self.output = output


class DeviceOffline(AdbError):
    code = "device_offline"


class StorageMissing(AdbError):
    code = "storage_missing"


class ChecksumMismatch(AdbError):
    code = "checksum_mismatch"


class SourceUnavailable(AdbError):
    code = "source_unavailable"


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class DeviceSnapshot:
    state: str = "unknown"
    serial: str = ""
    connection_mode: str = "network"
    model: str | None = None
    android_version: str | None = None
    battery_level: int | None = None
    temperature_c: float | None = None
    charging: bool | None = None
    battery_status: int | None = None
    ethernet: bool | None = None
    network_type: str | None = None
    network_interface: str | None = None
    network_addresses: list[str] | None = None
    network_gateway: str | None = None
    network_dns_servers: list[str] | None = None
    network_ssid: str | None = None
    network_validated: bool | None = None
    network_metered: bool | None = None
    storage_total_bytes: int | None = None
    storage_used_bytes: int | None = None
    storage_free_bytes: int | None = None
    internal_storage_total_bytes: int | None = None
    internal_storage_free_bytes: int | None = None
    primary_storage_uuid: str | None = None
    expected_primary_uuid: str | None = None
    storage_ready: bool = False
    disks: list[str] | None = None
    volumes: list[str] | None = None
    photos_installed: bool | None = None
    photos_enabled: bool | None = None
    photos_running: bool | None = None
    photos_version: str | None = None
    error: str | None = None
    observed_at: str | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


class SafeAdb:
    _allowed_shell_commands = {
        "am",
        "content",
        "df",
        "du",
        "dumpsys",
        "getprop",
        "ip",
        "mkdir",
        "pidof",
        "pm",
        "rm",
        "rmdir",
        "sha256sum",
        "sm",
        "stat",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.serial = settings.device_serial
        self.connection_mode = settings.connection_mode
        self._lock = asyncio.Lock()

    @property
    def selector(self) -> list[str]:
        return ["-d"] if self.connection_mode == "usb" else ["-s", self.serial]

    async def _run(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> CommandResult:
        effective_timeout = timeout or self.settings.command_timeout_seconds
        started = time.perf_counter()
        command_context = {
            "connection_mode": self.connection_mode,
            "arguments": args,
            "timeout_seconds": effective_timeout,
        }
        process: asyncio.subprocess.Process | None = None
        async with self._lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    self.settings.adb_path,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except FileNotFoundError as exc:
                logger.error(
                    "ADB executable was not found",
                    extra={
                        "context": {
                            **command_context,
                            "adb_path": self.settings.adb_path,
                        }
                    },
                )
                raise AdbError(f"ADB executable not found: {self.settings.adb_path}") from exc
            except asyncio.CancelledError:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            except TimeoutError as exc:
                assert process is not None
                process.kill()
                await process.wait()
                logger.error(
                    "ADB command timed out",
                    extra={
                        "context": {
                            **command_context,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000,
                                2,
                            ),
                        }
                    },
                )
                raise AdbError(f"ADB command timed out after {effective_timeout} seconds") from exc
        assert process is not None
        result = CommandResult(
            process.returncode or 0,
            stdout_bytes.decode(errors="replace").replace("\r", "").strip(),
            stderr_bytes.decode(errors="replace").replace("\r", "").strip(),
        )
        logger.log(
            logging.INFO if result.returncode == 0 else logging.WARNING,
            "ADB command completed",
            extra={
                "context": {
                    **command_context,
                    "return_code": result.returncode,
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000,
                        2,
                    ),
                    "stdout_bytes": len(stdout_bytes),
                    "stderr_bytes": len(stderr_bytes),
                    "stdout_preview": output_preview(stdout_bytes),
                    "stderr_preview": output_preview(stderr_bytes),
                }
            },
        )
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "device offline" in combined or "error: closed" in combined:
            raise DeviceOffline("Pixel is offline", output=combined)
        if "unauthorized" in combined:
            raise DeviceOffline("Pixel has not authorized this ADB host", output=combined)
        if check and result.returncode:
            detail = result.stderr or result.stdout
            raise AdbError(
                detail
                or (
                    f"ADB command failed with exit status {result.returncode}; "
                    "Android returned no diagnostic output"
                ),
                output=detail,
            )
        return result

    async def shell(
        self,
        *args: str,
        timeout: int | None = None,
        check: bool = True,
    ) -> CommandResult:
        if not args or args[0] not in self._allowed_shell_commands:
            raise ValueError("ADB shell command is not allowlisted")
        return await self._run(
            [*self.selector, "shell", shlex.join(args)],
            timeout=timeout,
            check=check,
        )

    async def connect(self) -> None:
        if self.connection_mode == "network" and ":" in self.serial:
            await self._run(["connect", self.serial], check=False)
        state = await self._run([*self.selector, "get-state"], check=False)
        if state.stdout != "device":
            raise DeviceOffline(f"Pixel state is {state.stdout or state.stderr or 'unavailable'}")

    async def restart_server(self) -> dict[str, Any]:
        """Restart only the local ADB host server using fixed commands."""
        stopped = await self._run(["kill-server"], check=False)
        started = await self._run(["start-server"])
        return {
            "restarted": True,
            "stop_returncode": stopped.returncode,
            "stop_output": stopped.stdout or stopped.stderr or None,
            "start_output": started.stdout or started.stderr or None,
        }

    async def enable_tcpip(self, port: int = 5555) -> dict[str, Any]:
        """Enable the fixed ADB-over-IP service using an authorized USB Pixel."""
        if not 1 <= port <= 65535:
            raise ValueError("ADB TCP/IP port must be between 1 and 65535")
        state = await self._run(["-d", "get-state"], check=False)
        if state.stdout != "device":
            raise DeviceOffline(
                "Connect and authorize exactly one Pixel over USB before enabling ADB over IP"
            )

        listener_check = await self._run(
            ["-d", "shell", shlex.join(("ss", "-ltnp"))],
            check=False,
        )
        adb_tcp_port_check = await self._run(
            ["-d", "shell", shlex.join(("getprop", "service.adb.tcp.port"))],
            check=False,
        )
        try:
            adb_tcp_port = int(adb_tcp_port_check.stdout.strip())
        except ValueError:
            adb_tcp_port = None
        listeners = parse_port_listeners(listener_check.stdout, port)
        if adb_tcp_port == port:
            listeners = [
                {
                    **listener,
                    "name": listener["name"] or "adbd",
                    "identity_inferred": listener["name"] is None,
                }
                for listener in listeners
            ]
        port_diagnostics = {
            "inspection_supported": listener_check.returncode == 0,
            "listeners": listeners,
            "adb_tcp_port_before_restart": adb_tcp_port,
            "inspection_error": (
                None
                if listener_check.returncode == 0
                else (
                    listener_check.stderr or listener_check.stdout or "Port inspection unavailable"
                )[:512]
            ),
        }

        route = await self._run(
            ["-d", "shell", shlex.join(("ip", "-4", "route"))],
            check=False,
        )
        addresses = parse_ipv4_addresses(route.stdout)
        address_details = await self._run(
            [
                "-d",
                "shell",
                shlex.join(("ip", "-4", "addr", "show", "scope", "global")),
            ],
            check=False,
        )
        addresses = list(dict.fromkeys([*addresses, *parse_ipv4_addresses(address_details.stdout)]))

        result = await self._run(["-d", "tcpip", str(port)])
        await asyncio.sleep(0.75)
        attempts: list[dict[str, str | int]] = []
        for address in addresses:
            serial = f"{address}:{port}"
            connection = await self._run(
                ["connect", serial],
                timeout=max(10, self.settings.command_timeout_seconds),
                check=False,
            )
            network_state = await self._run(
                ["-s", serial, "get-state"],
                timeout=max(10, self.settings.command_timeout_seconds),
                check=False,
            )
            attempts.append(
                {
                    "serial": serial,
                    "returncode": connection.returncode,
                    "output": connection.stdout or connection.stderr,
                }
            )
            if network_state.stdout == "device":
                return {
                    "enabled": True,
                    "connected": True,
                    "port": port,
                    "address": address,
                    "serial": serial,
                    "addresses": addresses,
                    "adb_output": result.stdout or result.stderr,
                    "connection_attempts": attempts,
                    "port_diagnostics": port_diagnostics,
                }
        return {
            "enabled": True,
            "connected": False,
            "port": port,
            "address": addresses[0] if addresses else None,
            "serial": f"{addresses[0]}:{port}" if addresses else None,
            "addresses": addresses,
            "adb_output": result.stdout or result.stderr,
            "connection_attempts": attempts,
            "port_diagnostics": port_diagnostics,
        }

    async def snapshot(self, expected_uuid: str = "") -> DeviceSnapshot:
        from datetime import UTC, datetime

        snapshot = DeviceSnapshot(
            serial=self.serial if self.connection_mode == "network" else "USB",
            connection_mode=self.connection_mode,
            expected_primary_uuid=expected_uuid or None,
            observed_at=datetime.now(UTC).isoformat(),
            disks=[],
            volumes=[],
        )
        try:
            await self.connect()
            snapshot.state = "device"
        except AdbError as exc:
            snapshot.state = exc.code
            snapshot.error = str(exc)
            return snapshot

        async def optional(*args: str) -> str:
            try:
                return (await self.shell(*args)).stdout
            except AdbError:
                return ""

        battery = parse_battery(await optional("dumpsys", "battery"))
        snapshot.battery_level = battery.get("level")
        snapshot.temperature_c = battery.get("temperature_c")
        snapshot.charging = battery.get("charging")
        snapshot.battery_status = battery.get("status")
        snapshot.model = await optional("getprop", "ro.product.model") or None
        snapshot.android_version = await optional("getprop", "ro.build.version.release") or None
        snapshot.primary_storage_uuid = await optional("sm", "get-primary-storage-uuid") or None
        snapshot.disks = split_lines(await optional("sm", "list-disks"))
        snapshot.volumes = split_lines(await optional("sm", "list-volumes", "all"))
        storage = parse_df(await optional("df", "-k", "/sdcard"))
        if storage:
            snapshot.storage_total_bytes = storage["total"] * 1024
            snapshot.storage_used_bytes = storage["used"] * 1024
            snapshot.storage_free_bytes = storage["free"] * 1024
        internal_storage = parse_df(await optional("df", "-k", "/data"))
        if internal_storage:
            snapshot.internal_storage_total_bytes = internal_storage["total"] * 1024
            snapshot.internal_storage_free_bytes = internal_storage["free"] * 1024
        snapshot.storage_ready = bool(
            storage and (not expected_uuid or snapshot.primary_storage_uuid == expected_uuid)
        )
        routes = await optional("ip", "route")
        connectivity = parse_connectivity(await optional("dumpsys", "connectivity"))
        snapshot.network_type = connectivity.get("network_type")
        snapshot.network_interface = connectivity.get("interface")
        snapshot.network_addresses = connectivity.get("addresses")
        snapshot.network_gateway = connectivity.get("gateway")
        snapshot.network_dns_servers = connectivity.get("dns_servers")
        snapshot.network_ssid = connectivity.get("ssid")
        snapshot.network_validated = connectivity.get("validated")
        snapshot.network_metered = connectivity.get("metered")
        snapshot.ethernet = snapshot.network_type == "ETHERNET" or any(
            token in routes for token in ("eth0", "ethernet")
        )
        package = await optional("dumpsys", "package", "com.google.android.apps.photos")
        snapshot.photos_installed = "com.google.android.apps.photos" in package
        snapshot.photos_enabled = snapshot.photos_installed and "enabled=0" not in package
        version = re.search(r"\bversionName=([^\s]+)", package)
        snapshot.photos_version = version.group(1) if version else None
        snapshot.photos_running = bool(await optional("pidof", "com.google.android.apps.photos"))
        return snapshot

    async def ensure_ready(self, expected_uuid: str) -> DeviceSnapshot:
        snapshot = await self.snapshot(expected_uuid)
        if snapshot.state != "device":
            raise DeviceOffline(snapshot.error or "Pixel is offline")
        if not snapshot.storage_ready:
            if expected_uuid:
                raise StorageMissing(
                    f"Primary storage UUID is {snapshot.primary_storage_uuid or 'unavailable'}; "
                    f"expected {expected_uuid}"
                )
            raise StorageMissing("Phone internal shared storage /sdcard is unavailable")
        return snapshot

    @staticmethod
    def validate_remote_path(path: str) -> str:
        segments = path.split("/")[1:]
        if (
            not SAFE_REMOTE_PATH.fullmatch(path)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(len(segment.encode("utf-8")) > 255 for segment in segments)
        ):
            raise ValueError("Unsafe remote path")
        return path

    async def ensure_directory(self, path: str) -> None:
        await self.shell("mkdir", "-p", self.validate_remote_path(path))

    async def push(
        self,
        source: Path,
        destination: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        destination = self.validate_remote_path(destination)
        total_bytes = source.stat().st_size
        if progress:
            progress(0, total_bytes)
        args = [*self.selector, "push", "-p", str(source), destination]
        started = time.perf_counter()
        stdout_bytes = bytearray()
        stderr_bytes = bytearray()
        last_percent = -1

        async def read_stream(
            stream: asyncio.StreamReader,
            capture: bytearray,
        ) -> None:
            nonlocal last_percent
            tail = ""
            while chunk := await stream.read(4096):
                capture.extend(chunk)
                tail = f"{tail}{chunk.decode(errors='replace')}"[-2048:]
                percent = parse_adb_progress(tail)
                if progress and percent is not None and percent > last_percent:
                    last_percent = percent
                    progress(min(total_bytes, total_bytes * percent // 100), total_bytes)

        async with self._lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    self.settings.adb_path,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdout and process.stderr
                async with asyncio.timeout(self.settings.push_timeout_seconds):
                    await asyncio.gather(
                        read_stream(process.stdout, stdout_bytes),
                        read_stream(process.stderr, stderr_bytes),
                    )
                    returncode = await process.wait()
            except FileNotFoundError as exc:
                raise AdbError(f"ADB executable not found: {self.settings.adb_path}") from exc
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise AdbError(
                    f"ADB push timed out after {self.settings.push_timeout_seconds} seconds"
                ) from exc
        result = CommandResult(
            returncode,
            bytes(stdout_bytes).decode(errors="replace").replace("\r", "").strip(),
            bytes(stderr_bytes).decode(errors="replace").replace("\r", "").strip(),
        )
        logger.log(
            logging.INFO if result.returncode == 0 else logging.WARNING,
            "ADB push completed",
            extra={
                "context": {
                    "connection_mode": self.connection_mode,
                    "arguments": args,
                    "return_code": result.returncode,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "source_size_bytes": total_bytes,
                    "stdout_preview": output_preview(bytes(stdout_bytes)),
                    "stderr_preview": output_preview(bytes(stderr_bytes)),
                }
            },
        )
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "device offline" in combined or "error: closed" in combined:
            raise DeviceOffline("Pixel disconnected during transfer", output=combined)
        if result.returncode:
            raise AdbError(
                result.stderr
                or result.stdout
                or (
                    f"ADB push failed with exit status {result.returncode}; "
                    "Android returned no diagnostic output"
                )
            )
        if progress:
            progress(total_bytes, total_bytes)

    async def speed_test(self, size_bytes: int = 32 * 1024**2) -> dict[str, Any]:
        """Measure one verified ADB upload using disposable non-media data."""
        if not 1024**2 <= size_bytes <= 256 * 1024**2:
            raise ValueError("ADB speed-test size must be between 1 MiB and 256 MiB")
        await self.connect()
        token = uuid.uuid4().hex
        local_path = self.settings.data_dir / f".adb-speedtest-{token}.bin"
        remote_path = f"/sdcard/Download/.pixel-relay-speedtest-{token}.bin"
        try:
            payload = await asyncio.to_thread(os.urandom, size_bytes)
            local_sha256 = hashlib.sha256(payload).hexdigest()
            await asyncio.to_thread(local_path.write_bytes, payload)
            del payload

            started = time.perf_counter()
            try:
                await self.push(local_path, remote_path)
            except AdbError as exc:
                raise AdbError(
                    f"ADB speed-test upload failed over {self.connection_mode}: {exc}",
                    output=exc.output,
                ) from exc
            elapsed_seconds = max(time.perf_counter() - started, 0.000001)
            remote_sha256 = await self.remote_sha256(remote_path)
            if remote_sha256 != local_sha256:
                raise ChecksumMismatch("ADB speed-test file failed SHA-256 verification")

            await self.shell("rm", "-f", "--", remote_path, check=False)
            remaining = await self.shell("stat", remote_path, check=False)
            if remaining.returncode == 0:
                raise AdbError(
                    "Speed test completed, but its temporary Pixel file could not be removed"
                )
            bytes_per_second = size_bytes / elapsed_seconds
            return {
                "connection_mode": self.connection_mode,
                "serial": "USB" if self.connection_mode == "usb" else self.serial,
                "size_bytes": size_bytes,
                "duration_seconds": elapsed_seconds,
                "bytes_per_second": bytes_per_second,
                "megabytes_per_second": bytes_per_second / 1024**2,
                "megabits_per_second": bytes_per_second * 8 / 1_000_000,
                "checksum_verified": True,
                "temporary_files_removed": True,
            }
        finally:
            local_path.unlink(missing_ok=True)
            with contextlib.suppress(AdbError):
                await self.shell("rm", "-f", "--", remote_path, check=False)

    async def remote_sha256(self, path: str) -> str:
        path = self.validate_remote_path(path)
        result = await self.shell(
            "sha256sum",
            path,
            timeout=self.settings.push_timeout_seconds,
            check=False,
        )
        match = re.match(r"^([a-fA-F0-9]{64})\s", result.stdout)
        if match:
            return match.group(1).lower()

        digest = hashlib.sha256()
        async with self._lock:
            process = await asyncio.create_subprocess_exec(
                self.settings.adb_path,
                *self.selector,
                "exec-out",
                shlex.join(("cat", path)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(self.settings.push_timeout_seconds):
                    assert process.stdout
                    while chunk := await process.stdout.read(1024 * 1024):
                        digest.update(chunk)
                    stderr = (await process.stderr.read()).decode(errors="replace")
                    returncode = await process.wait()
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise AdbError("Remote checksum stream timed out") from exc
        if returncode:
            if "offline" in stderr.lower():
                raise DeviceOffline("Pixel disconnected while verifying transfer")
            raise AdbError(stderr or "Unable to hash remote file")
        return digest.hexdigest()

    async def scan_media(self, path: str) -> bool:
        path = self.validate_remote_path(path)
        result = await self.shell(
            "am",
            "broadcast",
            "-W",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            f"file://{quote(path, safe='/')}",
            check=False,
        )
        if result.returncode:
            return False
        media_paths = [path]
        if path == "/sdcard":
            media_paths.append("/storage/emulated/0")
        elif path.startswith("/sdcard/"):
            media_paths.append(f"/storage/emulated/0/{path.removeprefix('/sdcard/')}")
        where = " OR ".join(
            f"_data='{media_path.replace(chr(39), chr(39) * 2)}'" for media_path in media_paths
        )
        if len(media_paths) > 1:
            where = f"({where})"
        query = await self.shell(
            "content",
            "query",
            "--uri",
            "content://media/external/file",
            "--where",
            where,
            check=False,
        )
        output = f"{query.stdout}\n{query.stderr}"
        return (
            query.returncode == 0
            and "No result found" not in output
            and "Error while accessing provider" not in output
            and bool(query.stdout)
        )

    async def remove_file(self, path: str) -> None:
        path = self.validate_remote_path(path)
        await self.shell("rm", "-f", "--", path)
        check = await self.shell("stat", path, check=False)
        if check.returncode == 0:
            raise AdbError(f"Pixel file still exists after purge: {path}")

    async def remove_directory(self, path: str) -> None:
        await self.shell("rmdir", self.validate_remote_path(path), check=False)

    async def remove_batch_directory(self, path: str) -> None:
        path = self.validate_remote_path(path)
        root = self.validate_remote_path(self.settings.destination_root)
        validate_generated_batch_directory(path, root)
        await self.shell(
            "rm",
            "-rf",
            "--",
            path,
            timeout=self.settings.push_timeout_seconds,
        )
        check = await self.shell("stat", path, check=False)
        if check.returncode == 0:
            raise AdbError(f"Pixel batch directory still exists after purge: {path}")

    async def reset_destination_tree(self) -> str:
        path = self.validate_remote_path(self.settings.destination_root)
        await self.shell(
            "rm",
            "-rf",
            "--",
            path,
            timeout=self.settings.push_timeout_seconds,
        )
        check = await self.shell("stat", path, check=False)
        if check.returncode == 0:
            raise AdbError(f"Pixel Relay destination still exists after reset: {path}")
        await self.shell("mkdir", "-p", path)
        return path

    async def trim_caches(self, desired_free_bytes: int) -> None:
        if not 1 <= desired_free_bytes <= 10 * 1024**4:
            raise ValueError("Desired free space is outside the safe cache-trim range")
        await self.shell(
            "pm",
            "trim-caches",
            str(desired_free_bytes),
            "internal",
            timeout=max(120, self.settings.command_timeout_seconds),
        )

    async def storage_devices(self) -> dict[str, Any]:
        """Return read-only Android storage media details for the adoption UI."""
        await self.connect()
        all_disks = [
            disk_id
            for disk_id in split_lines((await self.shell("sm", "list-disks")).stdout)
            if SAFE_DISK_ID.fullmatch(disk_id)
        ]
        adoptable_disks = set(
            split_lines((await self.shell("sm", "list-disks", "adoptable")).stdout)
        )
        simple_volumes = parse_storage_volumes(
            split_lines((await self.shell("sm", "list-volumes", "all")).stdout)
        )
        storage_dump = await self.shell("dumpsys", "mount", check=False)
        details = (
            parse_storage_service_dump(storage_dump.stdout)
            if storage_dump.returncode == 0
            else {"disks": [], "volumes": []}
        )
        detailed_disks = {disk["disk_id"]: disk for disk in details["disks"]}
        detailed_volumes = {volume["volume_id"]: volume for volume in details["volumes"]}

        volumes: list[dict[str, Any]] = []
        for volume in simple_volumes:
            detail = detailed_volumes.get(volume["volume_id"], {})
            volumes.append({**detail, **volume})

        disks: list[dict[str, Any]] = []
        ignored_disks: list[dict[str, Any]] = []
        for disk_id in all_disks:
            detail = detailed_disks.get(
                disk_id,
                {
                    "disk_id": disk_id,
                    "flags": [],
                    "adoptable": False,
                    "default_primary": False,
                    "usb": False,
                    "sd": False,
                    "size_bytes": None,
                    "label": None,
                    "volume_ids": [],
                    "sys_path": None,
                },
            )
            related = [
                volume
                for volume in volumes
                if volume.get("disk_id") == disk_id
                or volume["volume_id"] in detail.get("volume_ids", [])
            ]
            medium = {
                **detail,
                "adoptable": disk_id in adoptable_disks or bool(detail.get("adoptable")),
                "volumes": related,
            }
            # USB hubs and multi-card readers can expose an empty mass-storage
            # LUN indefinitely. Android represents that placeholder as a disk
            # with size=-1 and no volumes. It is external hardware, but it is
            # not media and must never be offered for adoption or selection.
            if (
                isinstance(detail.get("size_bytes"), int)
                and detail["size_bytes"] < 0
                and not related
            ):
                ignored_disks.append(
                    {
                        **medium,
                        "ignored_reason": "empty_usb_bridge",
                    }
                )
            else:
                disks.append(medium)

        primary = (await self.shell("sm", "get-primary-storage-uuid")).stdout
        return {
            "disks": disks,
            "ignored_disks": ignored_disks,
            "volumes": volumes,
            "current_primary_uuid": ("" if primary.lower() in {"", "null", "none"} else primary),
            "dump_supported": storage_dump.returncode == 0,
        }

    async def switch_primary_storage(
        self,
        target_uuid: str,
        *,
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Migrate Android primary shared storage to internal or an adopted UUID."""
        target_uuid = target_uuid.strip()
        if not SAFE_UUID.fullmatch(target_uuid):
            raise ValueError("Invalid Android storage UUID")

        async def report(stage: str, message: str, step: int, percent: int) -> None:
            if progress:
                await progress(
                    {
                        "stage": stage,
                        "message": message,
                        "step": step,
                        "step_count": 4,
                        "percent": percent,
                    }
                )

        await report("inspecting", "Inspecting Android primary storage", 1, 5)
        before = await self.storage_devices()
        previous_uuid = str(before.get("current_primary_uuid") or "")
        if target_uuid:
            physical_disk_ids = {disk["disk_id"] for disk in before["disks"]}
            target_volume = next(
                (
                    volume
                    for volume in before["volumes"]
                    if volume.get("fs_uuid") == target_uuid
                    and volume.get("volume_type") in {"private", "emulated"}
                    and volume.get("disk_id") in physical_disk_ids
                    and str(volume.get("state") or "").startswith("mounted")
                ),
                None,
            )
            if not target_volume:
                raise AdbError(
                    f"Adopted storage {target_uuid} is not mounted on a detected "
                    "physical drive; refusing to change Android primary storage"
                )
        if previous_uuid == target_uuid:
            await report("complete", "Selected storage is already Android primary", 4, 100)
            return {
                "previous_uuid": previous_uuid,
                "target_uuid": target_uuid,
                "changed": False,
                "storage": before,
            }

        target_argument = target_uuid or "internal"
        target_label = f"adopted storage {target_uuid}" if target_uuid else "phone internal storage"
        await report(
            "migrating",
            f"Migrating /sdcard to {target_label}; keep the Pixel and drive connected",
            2,
            20,
        )
        result = await self.shell(
            "pm",
            "move-primary-storage",
            target_argument,
            timeout=max(self.settings.push_timeout_seconds, 60 * 60),
            check=False,
        )
        if result.returncode or "success" not in result.stdout.lower():
            detail = (
                result.stderr
                or result.stdout
                or (
                    f"ADB exited with status {result.returncode} and Android "
                    "returned no diagnostic output"
                )
            )
            raise AdbError(
                f"Android could not move /sdcard to {target_label}. "
                f"{primary_storage_move_guidance(detail)} ADB detail: {detail}",
                output=detail,
            )

        await report("verifying", "Verifying Android's new primary storage", 3, 90)
        after = before
        for _attempt in range(10):
            after = await self.storage_devices()
            if str(after.get("current_primary_uuid") or "") == target_uuid:
                break
            await asyncio.sleep(2)
        else:
            observed = str(after.get("current_primary_uuid") or "") or "internal"
            raise AdbError(
                "Android reported a successful primary-storage move, but its current "
                f"primary is still {observed}; expected {target_argument}"
            )

        await report("complete", f"/sdcard now uses {target_label}", 4, 100)
        return {
            "previous_uuid": previous_uuid,
            "target_uuid": target_uuid,
            "changed": True,
            "storage": after,
        }

    async def adopt_storage(
        self,
        disk_id: str,
        *,
        force_adoptable: bool,
        migrate_primary: bool,
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Adopt one exact detected disk using fixed Android storage commands."""

        async def report(stage: str, message: str, step: int, percent: int) -> None:
            if progress:
                await progress(
                    {
                        "stage": stage,
                        "message": message,
                        "step": step,
                        "step_count": 7,
                        "percent": percent,
                    }
                )

        def stage_failure(
            summary: str,
            guidance: str,
            exc: AdbError,
        ) -> AdbError:
            detail = str(exc).strip() or "Android returned no diagnostic output"
            return AdbError(
                f"{summary}. {guidance} ADB detail: {detail}",
                output=exc.output,
            )

        if not SAFE_DISK_ID.fullmatch(disk_id):
            raise ValueError("Invalid Android disk identifier")
        await report("inspecting", "Inspecting Android storage media", 1, 5)
        try:
            before = await self.storage_devices()
        except AdbError as exc:
            raise stage_failure(
                "Pixel Relay could not inspect the drive before adoption",
                "Keep the Pixel unlocked, confirm USB debugging is authorized, "
                "and verify the drive is still connected.",
                exc,
            ) from exc
        disk = next(
            (candidate for candidate in before["disks"] if candidate["disk_id"] == disk_id),
            None,
        )
        if not disk:
            raise AdbError("The selected storage medium is no longer detected")
        incomplete_private = next(
            (
                volume
                for volume in disk.get("volumes", [])
                if volume.get("volume_type") == "private"
                and volume.get("state") == "unmountable"
                and not volume.get("fs_uuid")
            ),
            None,
        )
        if incomplete_private:
            raise AdbError(
                "This drive is already in an incomplete Android adoption state: "
                f"{incomplete_private['volume_id']} is UNMOUNTABLE and has no filesystem "
                "UUID. Do not retry adoption yet. Reset the drive to portable storage "
                "in Android Settings or reformat it on a computer, reconnect it, refresh "
                "the storage list, and then adopt it again."
            )
        if any(
            volume.get("volume_type") in {"private", "emulated"} and volume.get("fs_uuid")
            for volume in disk.get("volumes", [])
        ):
            raise AdbError(
                "This medium is already adopted; refusing to repartition and erase it again"
            )
        if not disk["adoptable"] and not force_adoptable:
            raise AdbError(
                "Android does not currently mark this medium as adoptable; "
                "enable the explicit force-adoptable option to continue"
            )
        await report("verified", "Selected drive verified", 2, 12)
        if force_adoptable and not disk["adoptable"]:
            await report(
                "enabling_adoption",
                "Enabling Android USB adoption support",
                3,
                18,
            )
            try:
                await self.shell("sm", "set-force-adoptable", "true")
            except AdbError as exc:
                raise stage_failure(
                    "Android rejected the request to enable forced USB adoption",
                    "This Pixel build may not support forced adoption. Refresh the "
                    "storage list and retry without Force if Android marks the drive "
                    "as adoptable.",
                    exc,
                ) from exc
            await asyncio.sleep(1)

        previous_private_uuids = {
            str(volume["fs_uuid"])
            for volume in before["volumes"]
            if volume.get("volume_type") in {"private", "emulated"} and volume.get("fs_uuid")
        }
        await report(
            "partitioning",
            "Erasing and encrypting the drive; keep it connected",
            4,
            25,
        )
        try:
            await self.shell(
                "sm",
                "partition",
                disk_id,
                "private",
                timeout=max(600, self.settings.command_timeout_seconds),
            )
        except AdbError as exc:
            raise stage_failure(
                f"Android could not erase and adopt {disk_id}",
                "Check the hub and SSD power, keep the Pixel unlocked, and make sure "
                "no file-manager app is using the drive. Refresh storage before "
                "retrying because Android may have partially repartitioned it.",
                exc,
            ) from exc

        await report(
            "discovering_volume",
            "Partition created; waiting for Android to register the private volume",
            5,
            48,
        )
        adopted_uuid: str | None = None
        after: dict[str, Any] = before
        for attempt in range(60):
            await asyncio.sleep(2)
            try:
                after = await self.storage_devices()
            except AdbError as exc:
                raise stage_failure(
                    "The partition command finished, but Pixel Relay could not read "
                    "the new Android volume",
                    "Keep the SSD connected and refresh the storage list before "
                    "deciding whether to retry adoption.",
                    exc,
                ) from exc
            candidates = [
                volume
                for volume in after["volumes"]
                if volume.get("volume_type") in {"private", "emulated"}
                and volume.get("fs_uuid")
                and (
                    volume.get("disk_id") == disk_id
                    or volume["fs_uuid"] not in previous_private_uuids
                )
            ]
            if candidates:
                adopted_uuid = str(candidates[0]["fs_uuid"])
                break
            if attempt and attempt % 5 == 0:
                await report(
                    "discovering_volume",
                    "Still waiting for Android to register the private volume",
                    5,
                    min(64, 48 + attempt // 3),
                )
        if not adopted_uuid:
            raise AdbError(
                "Android partitioned the medium but did not report its adopted UUID "
                "within two minutes"
            )

        migrated = False
        migration_error: str | None = None
        if migrate_primary:
            await report(
                "migrating_primary",
                "Migrating /sdcard to the adopted drive; this may take a long time",
                6,
                70,
            )
            result = await self.shell(
                "pm",
                "move-primary-storage",
                adopted_uuid,
                timeout=max(self.settings.push_timeout_seconds, 60 * 60),
                check=False,
            )
            if result.returncode or "success" not in result.stdout.lower():
                response = (
                    result.stderr
                    or result.stdout
                    or (
                        f"ADB exited with status {result.returncode} and Android "
                        "returned no diagnostic output"
                    )
                )
                migration_error = (
                    "Adoption succeeded, but Android did not migrate /sdcard to the "
                    f"new drive. {primary_storage_move_guidance(response)} "
                    f"ADB detail: {response}"
                )
            else:
                migrated = True
            try:
                after = await self.storage_devices()
            except AdbError as exc:
                migration_error = (
                    f"{migration_error + ' ' if migration_error else ''}"
                    "Adoption succeeded, but Pixel Relay could not refresh storage "
                    f"after migration. ADB detail: {str(exc).strip()}"
                )
        await report(
            "finalizing",
            (
                "Primary-storage migration finished"
                if migrated
                else "Adopted volume is ready; finalizing device state"
            ),
            6,
            90,
        )
        return {
            "disk_id": disk_id,
            "adopted_uuid": adopted_uuid,
            "migrated_primary": migrated,
            "migration_error": migration_error,
            "force_adoptable_enabled": force_adoptable and not disk["adoptable"],
            "storage": after,
        }

    async def unmount_storage(self, disk_id: str) -> dict[str, Any]:
        """Unmount the physical volumes belonging to one exact detected disk."""
        if not SAFE_DISK_ID.fullmatch(disk_id):
            raise ValueError("Invalid Android disk identifier")
        before = await self.storage_devices()
        disk = next(
            (candidate for candidate in before["disks"] if candidate["disk_id"] == disk_id),
            None,
        )
        if not disk:
            raise AdbError("The selected storage medium is no longer detected")

        volume_ids = [
            str(volume["volume_id"])
            for volume in disk.get("volumes", [])
            if volume.get("volume_type") in {"public", "private", "stub"}
            and str(volume.get("state") or "").startswith("mounted")
            and SAFE_PHYSICAL_VOLUME_ID.fullmatch(str(volume.get("volume_id") or ""))
        ]
        if not volume_ids:
            raise AdbError("The selected storage medium has no mounted physical volumes")

        for volume_id in volume_ids:
            await self.shell(
                "sm",
                "unmount",
                volume_id,
                timeout=max(120, self.settings.command_timeout_seconds),
            )

        after = before
        for _attempt in range(20):
            await asyncio.sleep(0.5)
            after = await self.storage_devices()
            remaining = {
                str(volume["volume_id"])
                for volume in after.get("volumes", [])
                if str(volume.get("state") or "").startswith("mounted")
            }
            if not any(volume_id in remaining for volume_id in volume_ids):
                break
        else:
            raise AdbError(
                "Android accepted the unmount request but still reports the volume as mounted"
            )

        return {
            "disk_id": disk_id,
            "unmounted_volume_ids": volume_ids,
            "storage": after,
        }

    async def storage_inventory(self, root: str) -> list[dict[str, Any]]:
        root = self.validate_remote_path(root)
        result = await self.shell("du", "-ak", root, check=False)
        if result.returncode and "No such file" not in f"{result.stdout}\n{result.stderr}":
            raise AdbError(result.stderr or result.stdout or "Unable to inspect Pixel storage")
        return parse_du_inventory(result.stdout, root)


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def parse_storage_volumes(lines: list[str]) -> list[dict[str, str | None]]:
    """Parse the stable `sm list-volumes all` id/state/fsUuid columns."""
    volumes: list[dict[str, str | None]] = []
    for line in lines:
        parts = line.split(maxsplit=2)
        if len(parts) not in {2, 3}:
            continue
        volume_id, state = parts[:2]
        raw_uuid = parts[2] if len(parts) == 3 else ""
        volume_type = re.split(r"[:;]", volume_id, maxsplit=1)[0]
        fs_uuid = None if raw_uuid.lower() in {"", "null", "none"} else raw_uuid
        volumes.append(
            {
                "volume_id": volume_id,
                "volume_type": volume_type,
                "state": state,
                "fs_uuid": fs_uuid,
            }
        )
    return volumes


def _dump_pair(block: str, key: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(key)}=((?:(?!\s+[A-Za-z]\w*=).)*)",
        block,
        flags=re.DOTALL,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return None if value.lower() in {"", "null", "none"} else value


def parse_storage_service_dump(output: str) -> dict[str, list[dict[str, Any]]]:
    """Extract physical-disk and volume metadata from `dumpsys mount`."""

    def blocks(kind: str) -> list[tuple[str | None, str]]:
        matches = list(
            re.finditer(
                rf"(?m)^\s*{kind}(?:\{{([^}}]+)\}})?:\s*$",
                output,
            )
        )
        result: list[tuple[str | None, str]] = []
        boundary = re.compile(
            r"(?m)^\s*(?:DiskInfo|VolumeInfo)(?:\{[^}]+\})?:\s*$|"
            r"^\s*(?:Disks|Volumes|Records):\s*$"
        )
        for match in matches:
            next_boundary = boundary.search(output, match.end())
            end = next_boundary.start() if next_boundary else len(output)
            result.append((match.group(1), output[match.end() : end]))
        return result

    disks: list[dict[str, Any]] = []
    for header_id, block in blocks("DiskInfo"):
        disk_id = header_id or _dump_pair(block, "id")
        if not disk_id:
            continue
        flags = _dump_pair(block, "flags") or ""
        raw_volume_ids = _dump_pair(block, "volumeIds") or ""
        raw_size = _dump_pair(block, "size")
        flag_values = [flag for flag in flags.split("|") if flag]
        flag_set = set(flag_values)
        disks.append(
            {
                "disk_id": disk_id,
                "flags": flag_values,
                "adoptable": bool({"ADOPTABLE", "FLAG_ADOPTABLE"} & flag_set),
                "default_primary": bool({"DEFAULT_PRIMARY", "FLAG_DEFAULT_PRIMARY"} & flag_set),
                "usb": bool({"USB", "FLAG_USB"} & flag_set),
                "sd": bool({"SD", "FLAG_SD"} & flag_set),
                "size_bytes": (
                    int(raw_size) if raw_size and re.fullmatch(r"-?\d+", raw_size) else None
                ),
                "label": _dump_pair(block, "label"),
                "volume_ids": [
                    value.strip()
                    for value in raw_volume_ids.strip("[]").split(", ")
                    if value.strip()
                ],
                "sys_path": _dump_pair(block, "sysPath"),
            }
        )

    volumes: list[dict[str, Any]] = []
    for header_id, block in blocks("VolumeInfo"):
        volume_id = header_id or _dump_pair(block, "id")
        if not volume_id:
            continue
        raw_type = _dump_pair(block, "type") or ""
        raw_state = _dump_pair(block, "state") or ""
        volumes.append(
            {
                "volume_id": volume_id,
                "volume_type": raw_type.removeprefix("TYPE_").lower() or None,
                "disk_id": _dump_pair(block, "diskId"),
                "state": raw_state.removeprefix("STATE_").lower() or None,
                "fs_type": _dump_pair(block, "fsType"),
                "fs_uuid": _dump_pair(block, "fsUuid"),
                "fs_label": _dump_pair(block, "fsLabel"),
                "path": _dump_pair(block, "path"),
            }
        )
    return {"disks": disks, "volumes": volumes}


def parse_connectivity(output: str) -> dict[str, Any]:
    active = re.search(r"Active default network:\s*(\d+)", output)
    if not active:
        return {}
    network_id = active.group(1)
    line = next(
        (
            candidate
            for candidate in output.splitlines()
            if "NetworkAgentInfo{" in candidate and f"network{{{network_id}}}" in candidate
        ),
        "",
    )
    if not line:
        return {}

    def match(pattern: str) -> str | None:
        found = re.search(pattern, line)
        return found.group(1).strip() if found else None

    addresses = ipv4_first(
        [
            address.strip()
            for address in (match(r"LinkAddresses:\s*\[\s*([^\]]*)\]") or "").split(",")
            if address.strip()
        ]
    )
    dns_servers = ipv4_first(
        [
            server.strip().lstrip("/")
            for server in (match(r"DnsAddresses:\s*\[\s*([^\]]*)\]") or "").split(",")
            if server.strip()
        ]
    )
    gateway = match(r"(?:0\.0\.0\.0/0|::/0)\s*->\s*([^\s,\]]+)")
    capabilities = match(r"Capabilities:\s*([^\s\]]+)") or ""
    return {
        "network_type": match(r"type:\s*([A-Z]+)"),
        "interface": match(r"InterfaceName:\s*([^\s]+)"),
        "addresses": addresses,
        "gateway": gateway,
        "dns_servers": dns_servers,
        "ssid": match(r"\bSSID:\s*\"([^\"]+)\""),
        "validated": "VALIDATED" in capabilities.split("&"),
        "metered": "NOT_METERED" not in capabilities.split("&"),
    }


def parse_battery(output: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, value = line.strip().split(":", 1)
            values[key.strip()] = value.strip()
    result: dict[str, Any] = {}
    for key in ("level", "status"):
        with contextlib.suppress(KeyError, ValueError):
            result[key] = int(values[key])
    with contextlib.suppress(KeyError, ValueError):
        result["temperature_c"] = int(values["temperature"]) / 10
    powered = [
        values.get("AC powered", "false"),
        values.get("USB powered", "false"),
        values.get("Wireless powered", "false"),
    ]
    result["charging"] = any(value.lower() == "true" for value in powered)
    return result


def parse_df(output: str) -> dict[str, int] | None:
    lines = [line for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        columns = line.split()
        if len(columns) < 6 or not columns[-1].startswith("/"):
            continue
        try:
            return {
                "total": int(columns[-5]),
                "used": int(columns[-4]),
                "free": int(columns[-3]),
            }
        except ValueError:
            continue
    return None


def parse_du_inventory(output: str, root: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in output.splitlines():
        size_text, separator, path = line.partition("\t")
        if not separator:
            columns = line.split(maxsplit=1)
            if len(columns) != 2:
                continue
            size_text, path = columns
        try:
            size_bytes = int(size_text) * 1024
        except ValueError:
            continue
        if (
            path.startswith(f"{root}/")
            and SAFE_REMOTE_PATH.fullmatch(path)
            and len(Path(path).relative_to(root).parts) == 3
            and re.fullmatch(r"[a-fA-F0-9]{32}", Path(path).relative_to(root).parts[0])
            and Path(path).relative_to(root).parts[1] in {"photos", "videos"}
        ):
            files.append({"path": path, "allocated_bytes": size_bytes})
    return files

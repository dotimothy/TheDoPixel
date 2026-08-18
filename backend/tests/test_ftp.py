from __future__ import annotations

import ftplib
from pathlib import Path

import pytest
from pixel_relay.config import Settings
from pixel_relay.transport import DeviceTransport, FtpTransport


class FakeFtp:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    connections = 0

    def connect(self, host: str, port: int, timeout: int) -> None:
        type(self).connections += 1
        assert host == "pixel.local"
        assert port == 2121
        assert timeout > 0

    def login(self, username: str, password: str) -> None:
        assert username == "pixel"
        assert password == "secret"

    def set_pasv(self, _enabled: bool) -> None:
        pass

    def voidcmd(self, _command: str) -> str:
        return "200 OK"

    def pwd(self) -> str:
        return "/"

    def mkd(self, path: str) -> str:
        if path in self.directories:
            raise ftplib.error_perm("550 exists")
        self.directories.add(path)
        return path

    def storbinary(self, command: str, handle, blocksize: int, callback=None) -> str:
        path = command.removeprefix("STOR ")
        chunks: list[bytes] = []
        while chunk := handle.read(blocksize):
            chunks.append(chunk)
            if callback:
                callback(chunk)
        self.files[path] = b"".join(chunks)
        return "226 stored"

    def retrbinary(self, command: str, callback, blocksize: int) -> str:
        path = command.removeprefix("RETR ")
        if path not in self.files:
            raise ftplib.error_perm("550 missing")
        callback(self.files[path])
        return "226 sent"

    def delete(self, path: str) -> str:
        if path not in self.files:
            raise ftplib.error_perm("550 missing")
        del self.files[path]
        return "250 deleted"

    def size(self, path: str) -> int:
        if path not in self.files:
            raise ftplib.error_perm("550 missing")
        return len(self.files[path])

    def rmd(self, path: str) -> str:
        self.directories.discard(path)
        return "250 removed"

    def mlsd(self, directory: str):
        prefix = f"{directory.rstrip('/')}/"
        entries: dict[str, str] = {}
        for path in [*self.directories, *self.files]:
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            if not remainder:
                continue
            name, separator, _rest = remainder.partition("/")
            child = f"{prefix}{name}"
            entries[name] = "dir" if separator or child in self.directories else "file"
        return [(name, {"type": entry_type}) for name, entry_type in entries.items()]

    def quit(self) -> str:
        return "221 bye"

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_ftp(monkeypatch):
    FakeFtp.files = {}
    FakeFtp.directories = set()
    FakeFtp.connections = 0
    monkeypatch.setattr("pixel_relay.transport.ftplib.FTP", FakeFtp)


@pytest.mark.asyncio
async def test_ftp_transport_uploads_verifies_and_purges(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="ftp",
        ftp_host="pixel.local",
        ftp_port=2121,
        ftp_username="pixel",
        ftp_password="secret",
    )
    source = tmp_path / "photo.raw"
    source.write_bytes(b"raw-photo")
    transport = FtpTransport(settings)
    destination = "/DCIM/Camera/PixelRelay/batch/photos/item.raw"
    progress: list[tuple[int, int]] = []

    snapshot = await transport.ensure_ready("")
    assert snapshot.state == "device"
    assert snapshot.connection_mode == "ftp"
    await transport.ensure_directory(destination.rsplit("/", 1)[0])
    await transport.push(
        source,
        destination,
        lambda transferred, total: progress.append((transferred, total)),
    )
    assert progress[0] == (0, source.stat().st_size)
    assert progress[-1] == (source.stat().st_size, source.stat().st_size)
    assert await transport.remote_sha256(destination) == (
        "908c86d2ad4f3b759e81e33534192cffe906bf45b093f4c39efa27ae3ccb9d22"
    )
    await transport.remove_file(destination)
    assert destination not in FakeFtp.files


@pytest.mark.asyncio
async def test_ftp_speed_test_uploads_verifies_and_removes_temporary_file(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ftp_host="pixel.local",
        ftp_port=2121,
        ftp_username="pixel",
        ftp_password="secret",
        ftp_destination_root="/DCIM/Camera/PixelRelay",
    )
    result = await FtpTransport(settings).speed_test(1024**2)

    assert result["connection_mode"] == "ftp"
    assert result["server"] == "pixel.local:2121"
    assert result["size_bytes"] == 1024**2
    assert result["upload_bytes_per_second"] > 0
    assert result["verification_bytes_per_second"] > 0
    assert result["verified_bytes_per_second"] > 0
    assert result["checksum_verified"] is True
    assert result["temporary_files_removed"] is True
    assert not FakeFtp.files
    assert not list(tmp_path.glob(".ftp-speedtest-*"))


@pytest.mark.asyncio
async def test_ftp_removes_a_whole_generated_batch_in_one_session(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="ftp",
        ftp_host="pixel.local",
        ftp_port=2121,
        ftp_username="pixel",
        ftp_password="secret",
        ftp_destination_root="/DCIM/Camera/PixelRelay",
    )
    batch_id = "0123456789abcdef0123456789abcdef"
    batch_directory = f"/DCIM/Camera/PixelRelay/{batch_id}"
    FakeFtp.directories = {
        batch_directory,
        f"{batch_directory}/photos",
        f"{batch_directory}/videos",
    }
    FakeFtp.files = {
        f"{batch_directory}/photos/photo.jpg": b"photo",
        f"{batch_directory}/videos/video.mp4": b"video",
    }
    transport = FtpTransport(settings)

    await transport.remove_batch_directory(batch_directory)

    assert FakeFtp.connections == 1
    assert FakeFtp.files == {}
    assert not any(path.startswith(batch_directory) for path in FakeFtp.directories)


@pytest.mark.asyncio
async def test_ftp_refuses_recursive_delete_outside_generated_batch(tmp_path: Path) -> None:
    transport = FtpTransport(Settings(data_dir=tmp_path))

    with pytest.raises(ValueError):
        await transport.remove_batch_directory("/DCIM/Camera/PixelRelay")


@pytest.mark.asyncio
async def test_ftp_resets_and_recreates_the_configured_destination(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="ftp",
        ftp_host="pixel.local",
        ftp_port=2121,
        ftp_username="pixel",
        ftp_password="secret",
        ftp_destination_root="/DCIM/Camera/PixelRelay",
    )
    root = settings.ftp_destination_root
    FakeFtp.directories = {root, f"{root}/misc"}
    FakeFtp.files = {f"{root}/misc/untracked.bin": b"orphan"}

    result = await FtpTransport(settings).reset_destination_tree()

    assert result == root
    assert FakeFtp.connections == 1
    assert FakeFtp.files == {}
    assert FakeFtp.directories == {root}


def test_transport_manager_selects_ftp(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="ftp",
        device_serial="192.168.1.35:5555",
    )
    manager = DeviceTransport(settings)
    assert manager.active is manager.ftp
    assert manager.control is manager.adb
    assert manager.adb.connection_mode == "network"


@pytest.mark.asyncio
async def test_ftp_mode_uses_adb_for_device_status_and_media_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="ftp",
        device_serial="192.168.1.35:5555",
        destination_root="/sdcard/DCIM/Camera/PixelRelay",
        ftp_destination_root="/DCIM/Camera/PixelRelay",
    )
    manager = DeviceTransport(settings)
    calls: list[tuple[str, str]] = []

    async def snapshot(expected_uuid: str):
        calls.append(("snapshot", expected_uuid))
        return object()

    async def scan_media(path: str) -> bool:
        calls.append(("scan", path))
        return True

    monkeypatch.setattr(manager.adb, "snapshot", snapshot)
    monkeypatch.setattr(manager.adb, "scan_media", scan_media)

    result = await manager.snapshot("adopted-uuid")
    scanned = await manager.scan_media("/DCIM/Camera/PixelRelay/batch/photos/photo.jpg")

    assert result is not None
    assert scanned is True
    assert calls == [
        ("snapshot", "adopted-uuid"),
        (
            "scan",
            "/sdcard/DCIM/Camera/PixelRelay/batch/photos/photo.jpg",
        ),
    ]


@pytest.mark.parametrize(
    "path",
    ["/DCIM/../secret", "relative/file.jpg", "/DCIM/file\nname.jpg"],
)
def test_ftp_paths_reject_unsafe_values(tmp_path: Path, path: str) -> None:
    transport = FtpTransport(Settings(data_dir=tmp_path))
    with pytest.raises(ValueError):
        transport.validate_remote_path(path)

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RAW_MEDIA_EXTENSIONS = frozenset(
    {
        ".3fr",
        ".arw",
        ".cr2",
        ".cr3",
        ".dng",
        ".erf",
        ".iiq",
        ".mef",
        ".mos",
        ".mrw",
        ".nef",
        ".nrw",
        ".orf",
        ".pef",
        ".raf",
        ".raw",
        ".rw2",
        ".rwl",
        ".sr2",
        ".srf",
        ".x3f",
    }
)


def default_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "PixelRelay"
        return Path.home() / "AppData" / "Local" / "PixelRelay"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PixelRelay"
    state_home = os.environ.get("XDG_STATE_HOME")
    return (
        Path(state_home) / "pixel-relay"
        if state_home
        else Path.home() / ".local" / "state" / "pixel-relay"
    )


def default_import_root() -> Path:
    return (Path.cwd() / "data").resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIXEL_RELAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "0.0.0.0"
    port: int = 8741
    adb_path: str = "adb"
    connection_mode: Literal["network", "usb", "ftp"] = "usb"
    device_serial: str = "192.168.1.35:5555"
    ftp_host: str = "192.168.1.35"
    ftp_port: int = 21
    ftp_username: str = "anonymous"
    ftp_password: str = ""
    ftp_destination_root: str = "/DCIM/Camera/PixelRelay"
    expected_primary_uuid: str = ""
    destination_root: str = "/sdcard/DCIM/Camera/PixelRelay"
    import_root: Path | None = Field(default_factory=default_import_root)
    session_hours: int = 12
    secure_cookies: bool = False
    trusted_proxy: bool = False
    max_batch_files: int = 6_000
    max_batch_bytes: int = 400 * 1024**3
    reserve_bytes: int = 10 * 1024**3
    reserve_percent: int = 10
    pause_temperature_c: float = 42.0
    resume_temperature_c: float = 38.0
    device_poll_seconds: int = 30
    worker_enabled: bool = True
    command_timeout_seconds: int = 30
    push_timeout_seconds: int = 60 * 60
    frontend_dist: Path = Path("frontend/dist")
    media_extensions: str = (
        ".jpg,.jpeg,.png,.gif,.webp,.heic,.heif,.dng,.cr2,.cr3,.nef,.nrw,"
        ".arw,.srf,.sr2,.raf,.orf,.rw2,.pef,.x3f,.3fr,.erf,.mef,.mos,.mrw,"
        ".raw,.rwl,.iiq,.mp4,.mov,.m4v,.avi,.mkv,.3gp"
    )

    @field_validator("destination_root")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        value = value.rstrip("/")
        invalid_segment = any(part in {"", ".", ".."} for part in value.split("/")[1:])
        if not value.startswith("/sdcard/") or invalid_segment:
            raise ValueError("destination_root must be a normalized path beneath /sdcard")
        return value

    @property
    def database_path(self) -> Path:
        return self.data_dir / "pixel-relay.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "pixel-relay.log"

    @property
    def allowed_extensions(self) -> frozenset[str]:
        return frozenset(
            extension.strip().lower()
            if extension.strip().startswith(".")
            else f".{extension.strip().lower()}"
            for extension in self.media_extensions.split(",")
            if extension.strip()
        )

    @property
    def video_extensions(self) -> frozenset[str]:
        return frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"})

    @property
    def raw_extensions(self) -> frozenset[str]:
        return RAW_MEDIA_EXTENSIONS

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.import_root:
            self.import_root.mkdir(parents=True, exist_ok=True)


def apply_persisted_settings(settings: Settings, get_value: Callable[[str, str], str]) -> None:
    """Apply administrator-editable settings without trusting raw SQLite values."""
    mode = get_value("connection_mode", settings.connection_mode)
    if mode in {"network", "usb", "ftp"}:
        settings.connection_mode = mode

    serial = get_value("device_serial", settings.device_serial).strip()
    if serial and len(serial) <= 255:
        settings.device_serial = serial

    ftp_host = get_value("ftp_host", settings.ftp_host).strip()
    if ftp_host and len(ftp_host) <= 255:
        settings.ftp_host = ftp_host
    ftp_username = get_value("ftp_username", settings.ftp_username).strip()
    if len(ftp_username) <= 255:
        settings.ftp_username = ftp_username
    settings.ftp_password = get_value("ftp_password", settings.ftp_password)
    ftp_destination = get_value("ftp_destination_root", settings.ftp_destination_root).strip()
    if (
        ftp_destination.startswith("/")
        and ".." not in ftp_destination.split("/")
        and len(ftp_destination) <= 1024
    ):
        settings.ftp_destination_root = ftp_destination.rstrip("/")
    try:
        ftp_port = int(get_value("ftp_port", str(settings.ftp_port)))
    except ValueError:
        ftp_port = settings.ftp_port
    if 1 <= ftp_port <= 65535:
        settings.ftp_port = ftp_port

    destination = get_value("destination_root", settings.destination_root).strip()
    with contextlib.suppress(ValueError):
        settings.destination_root = Settings.validate_destination(destination)

    import_root = get_value(
        "import_root", str(settings.import_root) if settings.import_root else ""
    ).strip()
    if import_root:
        settings.import_root = Path(import_root).expanduser()
    else:
        settings.import_root = default_import_root()

    integer_fields = {
        "max_batch_files": (1, 100_000),
        "max_batch_bytes": (1, 10 * 1024**4),
        "reserve_bytes": (0, 10 * 1024**4),
    }
    for key, (minimum, maximum) in integer_fields.items():
        raw = get_value(key, "")
        try:
            value = int(raw)
        except ValueError:
            continue
        if minimum <= value <= maximum:
            setattr(settings, key, value)

    decimal_fields = {
        "pause_temperature_c": (30.0, 80.0),
        "resume_temperature_c": (20.0, 75.0),
    }
    for key, (minimum, maximum) in decimal_fields.items():
        raw = get_value(key, "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if minimum <= value <= maximum:
            setattr(settings, key, value)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings

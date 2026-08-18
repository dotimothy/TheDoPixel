from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    id: int
    username: str
    csrf_token: str | None = None


class SourceRootCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=4096)


class ScanRequest(BaseModel):
    paths: list[str] | None = None
    full_verify: bool = False


class BatchCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    file_ids: list[int] = Field(min_length=1)


class BatchPlanRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    file_ids: list[int] = Field(min_length=1)


class BatchCancelRequest(BaseModel):
    acknowledgement: Literal["CANCEL BATCH"]


class ConfirmationRequest(BaseModel):
    acknowledgement: Literal["I verified this batch in Google Photos"]


class RetryRequest(BaseModel):
    include_purge_failures: bool = False


class AdbTcpipRequest(BaseModel):
    port: int = Field(default=5555, ge=1, le=65535)


class StorageAdoptRequest(BaseModel):
    disk_id: str = Field(pattern=r"^disk:\d+,\d+$", max_length=64)
    force_adoptable: bool = False
    migrate_primary: bool = False


class StorageUnmountRequest(BaseModel):
    disk_id: str = Field(pattern=r"^disk:\d+,\d+$", max_length=64)


class StoragePrimaryRequest(BaseModel):
    target_uuid: str = Field(default="", pattern=r"^[A-Za-z0-9._-]*$", max_length=128)


class SettingUpdate(BaseModel):
    device_serial: str | None = Field(default=None, max_length=255)
    expected_primary_uuid: str | None = Field(default=None, max_length=128)
    connection_mode: Literal["network", "usb", "ftp"] | None = None
    ftp_host: str | None = Field(default=None, max_length=255)
    ftp_port: int | None = Field(default=None, ge=1, le=65535)
    ftp_username: str | None = Field(default=None, max_length=255)
    ftp_password: str | None = Field(default=None, max_length=1024)
    ftp_destination_root: str | None = Field(default=None, max_length=1024)
    destination_root: str | None = Field(default=None, max_length=255)
    import_root: str | None = Field(default=None, max_length=4096)
    max_batch_files: int | None = Field(default=None, ge=1, le=100_000)
    max_batch_bytes: int | None = Field(default=None, ge=1, le=10 * 1024**4)
    reserve_bytes: int | None = Field(default=None, ge=0, le=10 * 1024**4)
    pause_temperature_c: float | None = Field(default=None, ge=30, le=80)
    resume_temperature_c: float | None = Field(default=None, ge=20, le=75)


class FtpTestRequest(BaseModel):
    ftp_host: str = Field(min_length=1, max_length=255)
    ftp_port: int = Field(ge=1, le=65535)
    ftp_username: str = Field(default="", max_length=255)
    ftp_password: str | None = Field(default=None, max_length=1024)
    ftp_destination_root: str = Field(min_length=1, max_length=1024)


class OrphanPurgeRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)


class StorageTreeResetRequest(BaseModel):
    acknowledgement: Literal["DELETE PIXEL RELAY TREE"]


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: Any | None = None

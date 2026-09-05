from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import string
import subprocess
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from math import ceil
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .adb import SAFE_UUID, AdbError, parse_storage_volumes
from .auth import (
    SESSION_COOKIE,
    AuthService,
    login_limiter,
    require_csrf,
    require_user,
    set_session_cookie,
)
from .config import Settings, apply_persisted_settings, get_settings
from .database import Database
from .events import EventBroker
from .files import atomic_upload, is_macos_metadata, local_path
from .models import (
    AdbTcpipRequest,
    BatchCancelRequest,
    BatchCreate,
    BatchPlanRequest,
    ConfirmationRequest,
    FtpTestRequest,
    LoginRequest,
    OrphanPurgeRequest,
    RetryRequest,
    ScanRequest,
    SettingUpdate,
    SourceRootCreate,
    StorageAdoptRequest,
    StoragePrimaryRequest,
    StorageTreeResetRequest,
    StorageUnmountRequest,
)
from .repository import DomainError, Repository
from .states import ItemState
from .transport import DeviceTransport
from .worker import RelayWorker

logger = logging.getLogger(__name__)
Authenticated = Annotated[dict, Depends(require_user)]
MutatingUser = Annotated[dict, Depends(require_csrf)]
UploadedMedia = Annotated[UploadFile, File()]


def default_server_directory() -> Path:
    volumes = Path("/Volumes")
    return volumes if sys.platform == "darwin" and volumes.is_dir() else Path.home()


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        supplied_context = getattr(record, "context", None)
        context = {
            "source": f"{record.module}.{record.funcName}:{record.lineno}",
            "process_id": record.process,
            "thread": record.threadName,
        }
        if isinstance(supplied_context, dict):
            context.update(supplied_context)
        payload["context"] = context
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    settings.prepare()
    formatter = JsonLogFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=10 * 1024**2,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[stream_handler, file_handler],
        force=True,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare()
    configure_logging(settings)
    db = Database(settings.database_path)
    db.migrate()
    repository = Repository(db, settings)
    apply_persisted_settings(settings, repository.setting)
    adb = DeviceTransport(settings)
    events = EventBroker()
    auth = AuthService(db, settings)
    worker = RelayWorker(db, repository, adb, events)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock_path = settings.data_dir / "pixel-relay.lock"
        logger.info(
            "TheDoPixel startup beginning",
            extra={
                "context": {
                    "data_dir": str(settings.data_dir),
                    "database_path": str(settings.database_path),
                    "import_root": str(settings.import_root),
                    "connection_mode": settings.connection_mode,
                    "destination_root": settings.destination_root,
                    "worker_enabled": settings.worker_enabled,
                }
            },
        )
        instance_lock = FileLock(lock_path, timeout=0)
        try:
            instance_lock.acquire()
        except Timeout as exc:
            raise RuntimeError(f"Another TheDoPixel process is using {settings.data_dir}") from exc
        app.state.instance_lock = instance_lock
        interrupted = db.fetchall("SELECT id FROM batch_items WHERE state='transferring'")
        if interrupted:
            logger.warning(
                "Recovering interrupted transfers after restart",
                extra={"context": {"interrupted_items": len(interrupted)}},
            )
        for item in interrupted:
            repository.transition(
                item["id"],
                "transfer_failed",
                detail="Service restarted during transfer; remote copy will be reconciled",
                error_code="service_restarted",
            )
            repository.transition(item["id"], "queued", detail="Automatic restart recovery")
        if settings.worker_enabled:
            await worker.start()
        logger.info(
            "TheDoPixel startup complete",
            extra={"context": {"worker_started": settings.worker_enabled}},
        )
        try:
            yield
        finally:
            logger.info("TheDoPixel shutdown beginning")
            adoption_task = worker.storage_adoption_task
            if adoption_task and not adoption_task.done():
                adoption_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await adoption_task
            primary_task = worker.storage_primary_switch_task
            if primary_task and not primary_task.done():
                primary_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await primary_task
            if settings.worker_enabled:
                await worker.stop()
            instance_lock.release()
            logger.info("TheDoPixel shutdown complete")

    app = FastAPI(
        title="TheDoPixel",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.repository = repository
    app.state.adb = adb
    app.state.events = events
    app.state.auth = auth
    app.state.worker = worker

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'"
        )
        return response

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        context = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else "unknown",
        }
        logger.info("HTTP request started", extra={"context": context})
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "HTTP request failed unexpectedly",
                extra={
                    "context": {
                        **context,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.log(
            logging.WARNING if response.status_code >= 400 else logging.INFO,
            "HTTP request completed",
            extra={
                "context": {
                    **context,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                }
            },
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        logger.warning(
            "Domain request rejected",
            extra={
                "context": {
                    "method": request.method,
                    "path": request.url.path,
                    "error_code": exc.code,
                    "status_code": exc.status_code,
                    "detail": str(exc),
                }
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": str(exc)},
        )

    router = APIRouter(prefix="/api/v1")

    def enrich_batch(batch: dict) -> dict:
        available_bytes = worker.available_transfer_bytes()
        item_count = int(batch.get("file_count") or len(batch.get("items") or []))
        queued_count = int(batch.get("states", {}).get(ItemState.QUEUED, 0) or 0)
        storage_blocked = (
            available_bytes is not None
            and item_count > 0
            and queued_count == item_count
            and int(batch.get("total_bytes") or 0) > available_bytes
            and not batch.get("series_blocked")
            and not batch.get("paused_at")
            and not batch.get("cancelled_at")
        )
        return {
            **batch,
            "processing": worker.active_batch_id == batch["id"],
            "storage_blocked": storage_blocked,
            "storage_available_bytes": available_bytes,
        }

    def queue_response() -> dict:
        return {
            **repository.queue_summary(),
            **worker.queue_status(),
        }

    def settings_response() -> dict:
        internal_storage_bytes = worker.latest_device.get("internal_storage_total_bytes")
        return {
            "device_serial": settings.device_serial,
            "connection_mode": adb.connection_mode,
            "ftp_host": settings.ftp_host,
            "ftp_port": settings.ftp_port,
            "ftp_username": settings.ftp_username,
            "ftp_password_configured": bool(settings.ftp_password),
            "ftp_destination_root": settings.ftp_destination_root,
            "expected_primary_uuid": repository.expected_uuid(),
            "destination_root": settings.destination_root,
            "import_root": str(settings.import_root) if settings.import_root else None,
            "max_batch_files": settings.max_batch_files,
            "max_batch_bytes": settings.max_batch_bytes,
            "reserve_bytes": settings.reserve_bytes,
            "pixel_internal_storage_bytes": internal_storage_bytes,
            "pause_temperature_c": settings.pause_temperature_c,
            "resume_temperature_c": settings.resume_temperature_c,
            "allowed_extensions": sorted(settings.allowed_extensions),
        }

    def ftp_test_overrides(payload: FtpTestRequest) -> dict:
        host = payload.ftp_host.strip()
        destination = payload.ftp_destination_root.strip().rstrip("/")
        if not host:
            raise DomainError("invalid_ftp_host", "FTP host cannot be empty")
        if not destination.startswith("/") or ".." in destination.split("/") or not destination:
            raise DomainError(
                "invalid_ftp_destination",
                "FTP destination must be an absolute path without '..'",
            )
        overrides = {
            "ftp_host": host,
            "ftp_port": payload.ftp_port,
            "ftp_username": payload.ftp_username.strip(),
            "ftp_destination_root": destination,
        }
        if payload.ftp_password is not None:
            overrides["ftp_password"] = payload.ftp_password
        return overrides

    @router.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "configured": auth.has_admin(),
            "version": app.version,
        }

    @router.post("/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
        key = request.client.host if request.client else "unknown"
        login_limiter.check(key)
        user = auth.authenticate(payload.username, payload.password)
        if not user:
            db.audit("auth.login_failed", "user", payload.username)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        login_limiter.clear(key)
        token, csrf = auth.create_session(user["id"])
        set_session_cookie(response, token, settings)
        db.audit("auth.login", "user", str(user["id"]), user["id"])
        return {"id": user["id"], "username": user["username"], "csrf_token": csrf}

    @router.post("/auth/logout")
    async def logout(
        request: Request,
        response: Response,
        user: MutatingUser,
    ) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            auth.revoke(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        db.audit("auth.logout", "user", str(user["user_id"]), user["user_id"])
        return {"ok": True}

    @router.get("/auth/me")
    async def me(user: Authenticated) -> dict:
        return {
            "id": user["user_id"],
            "username": user["username"],
            "csrf_token": user["csrf_token"],
        }

    @router.get("/dashboard")
    async def dashboard(_user: Authenticated) -> dict:
        attention_states = {
            ItemState.TRANSFER_FAILED,
            ItemState.MEDIA_SCAN_FAILED,
            ItemState.DEVICE_OFFLINE,
            ItemState.STORAGE_MISSING,
            ItemState.TEMPERATURE_PAUSED,
            ItemState.PURGE_FAILED,
        }
        all_batches = repository.list_batches(
            unsettled_only=True,
            include_batch_id=worker.active_batch_id,
        )
        active_batch = next(
            (enrich_batch(batch) for batch in all_batches if batch["id"] == worker.active_batch_id),
            None,
        )
        in_progress_batches = []
        for batch in all_batches:
            if batch["id"] == worker.active_batch_id:
                continue
            item_count = int(batch.get("file_count") or 0)
            states = batch.get("states", {})
            ready_to_verify = (
                item_count > 0
                and int(states.get(ItemState.AWAITING_BACKUP_CONFIRMATION, 0) or 0) == item_count
            )
            needs_attention = any(states.get(state, 0) for state in attention_states)
            if (
                not batch.get("cancelled_at")
                and not batch.get("confirmed_at")
                and not batch.get("purged_at")
                and not ready_to_verify
                and not needs_attention
            ):
                in_progress_batches.append(enrich_batch(batch))
                if len(in_progress_batches) == 5:
                    break
        return {
            "device": worker.latest_device,
            "queue": queue_response(),
            "active_batch": active_batch,
            "batches": in_progress_batches,
            "sources": repository.list_roots(),
        }

    @router.post("/queue/stop")
    async def stop_queue(user: MutatingUser) -> dict:
        status = worker.request_queue_stop(user["user_id"])
        await events.publish("queue", {"action": "stop_requested", **status})
        return queue_response()

    @router.post("/queue/start")
    async def start_queue(user: MutatingUser) -> dict:
        status = worker.start_queue(user["user_id"])
        await events.publish("queue", {"action": "started", **status})
        return queue_response()

    @router.get("/device")
    async def device(_user: Authenticated) -> dict:
        return worker.latest_device

    @router.get("/device/storage-options")
    async def device_storage_options(
        _user: Authenticated,
        refresh: bool = Query(default=False),
    ) -> dict:
        snapshot = worker.latest_device
        configured_uuid = repository.expected_uuid()
        media_error: str | None = None
        if refresh:
            try:
                media = await adb.storage_devices()
                worker.latest_storage_media = media
            except AdbError as exc:
                media_error = str(exc)
                media = {
                    "disks": [],
                    "ignored_disks": [],
                    "volumes": parse_storage_volumes(snapshot.get("volumes") or []),
                    "current_primary_uuid": snapshot.get("primary_storage_uuid") or "",
                    "dump_supported": False,
                }
        elif worker.latest_storage_media is not None:
            media = worker.latest_storage_media
        else:
            media = {
                # A bare `sm list-disks` identifier cannot distinguish real
                # media from an empty USB reader/LUN. Do not expose an
                # unclassified identifier as an adoption candidate.
                "disks": [],
                "ignored_disks": [],
                "volumes": parse_storage_volumes(snapshot.get("volumes") or []),
                "current_primary_uuid": snapshot.get("primary_storage_uuid") or "",
                "dump_supported": False,
            }
        raw_primary_uuid = media.get("current_primary_uuid") or snapshot.get("primary_storage_uuid")
        current_uuid = (
            str(raw_primary_uuid)
            if raw_primary_uuid and str(raw_primary_uuid).lower() not in {"null", "none"}
            else ""
        )
        records = media["volumes"]
        disk_by_id = {disk["disk_id"]: disk for disk in media["disks"]}
        physical_disk_ids = set(disk_by_id)
        options: list[dict] = [
            {
                "id": "internal",
                "uuid": "",
                "label": "Phone internal storage",
                "kind": "internal",
                "state": "mounted" if snapshot.get("state") == "device" else "unavailable",
                "current": not current_uuid,
                "configured": not configured_uuid,
                "selectable": True,
                "volume_ids": [
                    record["volume_id"]
                    for record in records
                    if record["volume_type"] in {"private", "emulated"}
                    and not record["fs_uuid"]
                    and not record.get("disk_id")
                    and not re.search(r":\d+,\d+", record["volume_id"])
                ],
                "total_bytes": snapshot.get("internal_storage_total_bytes"),
                "free_bytes": snapshot.get("internal_storage_free_bytes"),
                "description": (
                    "The stock default. Pixel Relay writes through /sdcard to the "
                    "phone's internal shared storage."
                    if not current_uuid
                    else "Selecting this migrates Android's /sdcard data back from "
                    f"{current_uuid} to phone internal storage."
                ),
            }
        ]

        adopted: dict[str, dict] = {}
        portable: list[dict] = []
        for record in records:
            disk_id = record.get("disk_id")
            if not disk_id or disk_id not in physical_disk_ids:
                # Internal private/emulated volumes must never be inferred as
                # adopted or portable targets, even if Android reports a UUID.
                continue
            fs_uuid = record["fs_uuid"]
            if not fs_uuid:
                continue
            if record["volume_type"] in {"private", "emulated"}:
                medium = disk_by_id[disk_id]
                option = adopted.setdefault(
                    fs_uuid,
                    {
                        "id": f"adopted-{fs_uuid}",
                        "uuid": fs_uuid,
                        "label": medium.get("label") or "Adopted private storage",
                        "kind": "adopted",
                        "state": record["state"],
                        "current": fs_uuid == current_uuid,
                        "configured": fs_uuid == configured_uuid,
                        "selectable": False,
                        "volume_ids": [],
                        "total_bytes": (
                            snapshot.get("storage_total_bytes") if fs_uuid == current_uuid else None
                        ),
                        "free_bytes": (
                            snapshot.get("storage_free_bytes") if fs_uuid == current_uuid else None
                        ),
                        "description": (
                            "Android's current /sdcard storage."
                            if fs_uuid == current_uuid
                            else "Encrypted Android private storage. Choosing it migrates "
                            "/sdcard data to this drive before Pixel Relay starts using it."
                        ),
                        "disk_id": disk_id,
                    },
                )
                option["volume_ids"].append(record["volume_id"])
                if record["state"] == "mounted":
                    option["state"] = "mounted"
                    option["selectable"] = True
            elif record["volume_type"] == "public":
                medium = disk_by_id[disk_id]
                portable.append(
                    {
                        "id": f"portable-{record['volume_id']}",
                        "uuid": fs_uuid,
                        "label": medium.get("label") or "Portable USB storage",
                        "kind": "portable",
                        "state": record["state"],
                        "current": False,
                        "configured": False,
                        "selectable": False,
                        "volume_ids": [record["volume_id"]],
                        "total_bytes": medium.get("size_bytes"),
                        "free_bytes": None,
                        "description": (
                            "Visible for reference, but not eligible as Pixel Relay's "
                            "/sdcard target. It must be adopted by Android first."
                        ),
                        "disk_id": disk_id,
                    }
                )
        options.extend(adopted.values())
        options.extend(portable)
        if configured_uuid and not any(option["uuid"] == configured_uuid for option in options):
            options.append(
                {
                    "id": f"missing-{configured_uuid}",
                    "uuid": configured_uuid,
                    "label": "Configured storage not detected",
                    "kind": "unavailable",
                    "state": "missing",
                    "current": configured_uuid == current_uuid,
                    "configured": True,
                    "selectable": False,
                    "volume_ids": [],
                    "total_bytes": None,
                    "free_bytes": None,
                    "description": (
                        "This saved UUID was not reported by Android. Choose another "
                        "storage target or reconnect the adopted drive."
                    ),
                }
            )
        return {
            "device_state": snapshot.get("state", "unknown"),
            "connection_mode": settings.connection_mode,
            "current_primary_uuid": current_uuid,
            "configured_uuid": configured_uuid,
            "disks": [disk["disk_id"] for disk in media["disks"]],
            "media": media["disks"],
            "ignored_media": media.get("ignored_disks", []),
            "media_error": media_error,
            "details_supported": media["dump_supported"],
            "options": options,
            "observed_at": snapshot.get("observed_at"),
        }

    @router.get("/device/storage/adoption")
    async def storage_adoption_status(_user: Authenticated) -> dict:
        return {
            "operation": worker.storage_adoption,
        }

    @router.post("/device/storage/adoption/dismiss")
    async def dismiss_storage_adoption(_user: MutatingUser) -> dict:
        operation = worker.storage_adoption
        if operation and operation["status"] == "running":
            raise DomainError(
                "adoption_in_progress",
                "Drive adoption is still running and cannot be dismissed",
                status_code=409,
            )
        worker.storage_adoption = None
        worker.storage_adoption_task = None
        return {"dismissed": True}

    @router.get("/device/storage/primary-switch")
    async def storage_primary_switch_status(_user: Authenticated) -> dict:
        return {"operation": worker.storage_primary_switch}

    @router.post("/device/storage/primary-switch/dismiss")
    async def dismiss_storage_primary_switch(_user: MutatingUser) -> dict:
        operation = worker.storage_primary_switch
        if operation and operation["status"] == "running":
            raise DomainError(
                "primary_switch_in_progress",
                "Android primary-storage migration is still running and cannot be dismissed",
                status_code=409,
            )
        worker.storage_primary_switch = None
        worker.storage_primary_switch_task = None
        return {"dismissed": True}

    @router.post("/device/storage/primary-switch", status_code=202)
    async def switch_device_primary_storage(
        payload: StoragePrimaryRequest,
        user: MutatingUser,
    ) -> dict:
        existing = worker.storage_primary_switch
        if existing and existing["status"] == "running":
            raise DomainError(
                "primary_switch_in_progress",
                (
                    "Android primary-storage migration is already running "
                    f"({existing['operation_id']})"
                ),
                status_code=409,
            )
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before changing primary storage",
                status_code=409,
            )

        target_uuid = payload.target_uuid.strip()
        operation_id = str(uuid.uuid4())
        operation = {
            "operation_id": operation_id,
            "target_uuid": target_uuid,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "progress": {
                "action": "primary_switch_progress",
                "operation_id": operation_id,
                "target_uuid": target_uuid,
                "stage": "queued",
                "message": "Android primary-storage migration queued",
                "step": 1,
                "step_count": 4,
                "percent": 1,
                "complete": False,
                "failed": False,
            },
            "result": None,
            "error": None,
        }

        async def report_primary_progress(progress: dict) -> None:
            event_data = {
                "action": "primary_switch_progress",
                "operation_id": operation_id,
                "target_uuid": target_uuid,
                "complete": False,
                "failed": False,
                **progress,
            }
            operation["progress"] = event_data
            await events.publish("storage", event_data)

        worker.storage_primary_switch = operation
        worker.maintenance_reason = "storage_primary_switch"
        db.audit(
            "device.storage_primary_switch_started",
            "android_storage_uuid",
            target_uuid or "internal",
            user["user_id"],
            {"destination_root": settings.destination_root},
        )

        async def run_primary_switch() -> None:
            try:
                result = await adb.switch_primary_storage(
                    target_uuid,
                    progress=report_primary_progress,
                )
                worker.latest_storage_media = result["storage"]
                repository.set_setting(
                    "expected_primary_uuid",
                    target_uuid,
                    user["user_id"],
                )
                apply_persisted_settings(settings, repository.setting)
                device = await worker.refresh_device()
                operation["status"] = "completed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["result"] = {
                    "previous_uuid": result["previous_uuid"],
                    "target_uuid": result["target_uuid"],
                    "changed": result["changed"],
                    "destination_root": settings.destination_root,
                    "device": device,
                }
                db.audit(
                    "device.storage_primary_switch_completed",
                    "android_storage_uuid",
                    target_uuid or "internal",
                    user["user_id"],
                    {
                        "previous_uuid": result["previous_uuid"],
                        "changed": result["changed"],
                        "destination_root": settings.destination_root,
                    },
                )
                await events.publish(
                    "setting",
                    {
                        "action": "storage_primary_changed",
                        "uuid": target_uuid,
                        "destination_root": settings.destination_root,
                    },
                )
                await report_primary_progress(
                    {
                        "stage": "complete",
                        "message": (
                            f"Android /sdcard now uses {target_uuid}"
                            if target_uuid
                            else "Android /sdcard now uses phone internal storage"
                        ),
                        "step": 4,
                        "step_count": 4,
                        "percent": 100,
                        "complete": True,
                        "failed": False,
                    }
                )
            except asyncio.CancelledError:
                operation["status"] = "failed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["error"] = (
                    "Pixel Relay stopped while Android primary storage was changing"
                )
                raise
            except Exception as exc:
                message = str(exc) or "Android primary-storage migration failed"
                operation["status"] = "failed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["error"] = message
                db.audit(
                    "device.storage_primary_switch_failed",
                    "android_storage_uuid",
                    target_uuid or "internal",
                    user["user_id"],
                    {"error": message},
                )
                logger.exception("Background Android primary-storage migration failed")
                await report_primary_progress(
                    {
                        "stage": "failed",
                        "message": message,
                        "step": 4,
                        "step_count": 4,
                        "percent": 100,
                        "complete": False,
                        "failed": True,
                    }
                )
            finally:
                worker.maintenance_reason = None
                worker.wake()

        task = asyncio.create_task(
            run_primary_switch(),
            name=f"pixel-relay-primary-storage-{operation_id}",
        )
        worker.storage_primary_switch_task = task
        return operation

    @router.post("/device/storage/adopt", status_code=202)
    async def adopt_device_storage(
        payload: StorageAdoptRequest,
        user: MutatingUser,
    ) -> dict:
        existing = worker.storage_adoption
        if existing and existing["status"] == "running":
            raise DomainError(
                "adoption_in_progress",
                (
                    "A drive adoption is already running "
                    f"for {existing['disk_id']} ({existing['operation_id']})"
                ),
                status_code=409,
            )
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before adopting storage",
                status_code=409,
            )

        operation_id = str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat()
        operation = {
            "operation_id": operation_id,
            "disk_id": payload.disk_id,
            "status": "running",
            "force_adoptable": payload.force_adoptable,
            "migrate_primary": payload.migrate_primary,
            "started_at": started_at,
            "finished_at": None,
            "progress": {
                "action": "adoption_progress",
                "operation_id": operation_id,
                "disk_id": payload.disk_id,
                "stage": "queued",
                "message": "Drive adoption queued on the Pixel Relay server",
                "step": 1,
                "step_count": 7,
                "percent": 1,
                "complete": False,
                "failed": False,
            },
            "result": None,
            "error": None,
        }

        async def report_adoption_progress(progress: dict) -> None:
            event_data = {
                "action": "adoption_progress",
                "operation_id": operation_id,
                "disk_id": payload.disk_id,
                "complete": False,
                "failed": False,
                **progress,
            }
            operation["progress"] = event_data
            await events.publish(
                "storage",
                event_data,
            )

        worker.storage_adoption = operation
        worker.maintenance_reason = "storage_adoption"
        db.audit(
            "device.storage_adoption_started",
            "android_disk",
            payload.disk_id,
            user["user_id"],
            {
                "force_adoptable": payload.force_adoptable,
                "migrate_primary": payload.migrate_primary,
            },
        )

        async def run_adoption() -> None:
            try:
                await report_adoption_progress(
                    {
                        "stage": "preparing",
                        "message": "Pausing Pixel Relay queue work",
                        "step": 1,
                        "step_count": 7,
                        "percent": 2,
                    }
                )
                result = await adb.adopt_storage(
                    payload.disk_id,
                    force_adoptable=payload.force_adoptable,
                    migrate_primary=payload.migrate_primary,
                    progress=report_adoption_progress,
                )
                worker.latest_storage_media = result["storage"]
                if result["migrated_primary"]:
                    repository.set_setting(
                        "expected_primary_uuid",
                        result["adopted_uuid"],
                        user["user_id"],
                    )
                    apply_persisted_settings(settings, repository.setting)
                await report_adoption_progress(
                    {
                        "stage": "refreshing",
                        "message": "Refreshing Pixel storage status",
                        "step": 7,
                        "step_count": 7,
                        "percent": 96,
                    }
                )
                result["device"] = await worker.refresh_device()
                operation["status"] = "completed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["result"] = {
                    "adopted_uuid": result["adopted_uuid"],
                    "migrated_primary": result["migrated_primary"],
                    "migration_error": result["migration_error"],
                    "force_adoptable_enabled": result["force_adoptable_enabled"],
                }
                db.audit(
                    "device.storage_adoption_completed",
                    "android_disk",
                    payload.disk_id,
                    user["user_id"],
                    operation["result"],
                )
                await events.publish(
                    "setting",
                    {
                        "action": "storage_adopted",
                        "disk_id": payload.disk_id,
                        "uuid": result["adopted_uuid"],
                    },
                )
                await report_adoption_progress(
                    {
                        "stage": "complete",
                        "message": (
                            "Drive adopted; primary migration needs attention"
                            if result["migration_error"]
                            else "Drive adoption completed"
                        ),
                        "step": 7,
                        "step_count": 7,
                        "percent": 100,
                        "complete": True,
                        "failed": False,
                    }
                )
            except asyncio.CancelledError:
                operation["status"] = "failed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["error"] = "Pixel Relay stopped while drive adoption was running"
                raise
            except Exception as exc:
                message = str(exc) or "Drive adoption failed without diagnostic output"
                operation["status"] = "failed"
                operation["finished_at"] = datetime.now(UTC).isoformat()
                operation["error"] = message
                db.audit(
                    "device.storage_adoption_failed",
                    "android_disk",
                    payload.disk_id,
                    user["user_id"],
                    {"error": message},
                )
                logger.exception("Background drive adoption failed")
                await report_adoption_progress(
                    {
                        "stage": "failed",
                        "message": message,
                        "step": 7,
                        "step_count": 7,
                        "percent": 100,
                        "complete": False,
                        "failed": True,
                    }
                )
            finally:
                worker.maintenance_reason = None
                worker.wake()

        task = asyncio.create_task(
            run_adoption(),
            name=f"pixel-relay-storage-adoption-{operation_id}",
        )
        worker.storage_adoption_task = task
        return operation

    @router.post("/device/storage/unmount")
    async def unmount_device_storage(
        payload: StorageUnmountRequest,
        user: MutatingUser,
    ) -> dict:
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before unmounting storage",
                status_code=409,
            )

        worker.maintenance_reason = "storage_unmount"
        db.audit(
            "device.storage_unmount_started",
            "android_disk",
            payload.disk_id,
            user["user_id"],
            {},
        )
        try:
            result = await adb.unmount_storage(payload.disk_id)
            worker.latest_storage_media = result["storage"]
            result["device"] = await worker.refresh_device()
            db.audit(
                "device.storage_unmount_completed",
                "android_disk",
                payload.disk_id,
                user["user_id"],
                {"unmounted_volume_ids": result["unmounted_volume_ids"]},
            )
            await events.publish(
                "setting",
                {
                    "action": "storage_unmounted",
                    "disk_id": payload.disk_id,
                    "volume_ids": result["unmounted_volume_ids"],
                },
            )
            return result
        except AdbError as exc:
            db.audit(
                "device.storage_unmount_failed",
                "android_disk",
                payload.disk_id,
                user["user_id"],
                {"error": str(exc)},
            )
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        finally:
            worker.maintenance_reason = None
            worker.wake()

    @router.get("/device/telemetry")
    async def device_telemetry(
        _user: Authenticated,
        hours: int = Query(default=24, ge=1, le=24 * 90),
        max_points: int = Query(default=240, ge=24, le=1000),
    ) -> dict:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = db.fetchall(
            """
            SELECT status_json, created_at
            FROM device_samples
            WHERE created_at >= ?
            ORDER BY created_at
            """,
            (cutoff,),
        )
        if len(rows) > max_points:
            stride = ceil(len(rows) / max_points)
            sampled = rows[::stride]
            if sampled[-1] is not rows[-1]:
                sampled.append(rows[-1])
            rows = sampled
        points = []
        for row in rows:
            try:
                status = json.loads(row["status_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            points.append(
                {
                    "observed_at": row["created_at"],
                    "state": status.get("state"),
                    "battery_level": status.get("battery_level"),
                    "temperature_c": status.get("temperature_c"),
                    "charging": status.get("charging"),
                    "storage_total_bytes": status.get("storage_total_bytes"),
                    "storage_used_bytes": status.get("storage_used_bytes"),
                    "storage_free_bytes": status.get("storage_free_bytes"),
                }
            )

        def metric_summary(key: str) -> dict:
            values = [
                float(point[key]) for point in points if isinstance(point.get(key), (int, float))
            ]
            return {
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "average": round(sum(values) / len(values), 2) if values else None,
                "latest": values[-1] if values else None,
            }

        return {
            "hours": hours,
            "sample_count": len(points),
            "points": points,
            "summary": {
                "battery_level": metric_summary("battery_level"),
                "temperature_c": metric_summary("temperature_c"),
                "storage_free_bytes": metric_summary("storage_free_bytes"),
            },
        }

    async def pixel_storage_inventory() -> dict:
        destination_root = (
            settings.ftp_destination_root
            if settings.connection_mode == "ftp"
            else settings.destination_root
        )
        try:
            snapshot = await adb.ensure_ready(repository.expected_uuid())
            remote_files = await adb.storage_inventory(destination_root)
        except AdbError as exc:
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        tracked_rows = db.fetchall(
            """
            SELECT batch_items.remote_path, batch_items.state, batch_items.batch_id,
              batches.name AS batch_name
            FROM batch_items
            JOIN batches ON batches.id=batch_items.batch_id
            """
        )
        tracked = {row["remote_path"]: row for row in tracked_rows}
        files = []
        for remote in remote_files:
            record = tracked.get(remote["path"])
            files.append(
                {
                    **remote,
                    "tracked": record is not None,
                    "state": record["state"] if record else None,
                    "batch_id": record["batch_id"] if record else None,
                    "batch_name": record["batch_name"] if record else None,
                }
            )
        return {
            "destination_root": destination_root,
            "connection_mode": settings.connection_mode,
            "storage_total_bytes": snapshot.storage_total_bytes,
            "storage_free_bytes": snapshot.storage_free_bytes,
            "relay_allocated_bytes": sum(file["allocated_bytes"] for file in files),
            "tracked_count": sum(file["tracked"] for file in files),
            "orphan_count": sum(not file["tracked"] for file in files),
            "files": files,
        }

    @router.get("/device/storage")
    async def device_storage(_user: Authenticated) -> dict:
        return await pixel_storage_inventory()

    async def remove_storage_orphans(
        inventory: dict,
        requested_paths: list[str],
        *,
        prune_known_batches: bool = False,
    ) -> dict:
        current = {file["path"]: file for file in inventory["files"]}
        paths = list(dict.fromkeys(requested_paths))
        missing = [path for path in paths if path not in current]
        tracked = [path for path in paths if path in current and current[path]["tracked"]]
        if missing:
            raise DomainError(
                "storage_file_missing",
                "One or more selected files are no longer present in TheDoPixel storage",
                status_code=409,
            )
        if tracked:
            raise DomainError(
                "storage_file_tracked",
                "Tracked Pixel copies must be managed through their batch purge workflow",
                status_code=409,
            )
        deleted: list[str] = []
        try:
            for path in paths:
                await adb.remove_file(path)
                deleted.append(path)
            directories = sorted(
                {path.rsplit("/", 1)[0] for path in paths}
                | {path.rsplit("/", 2)[0] for path in paths},
                key=lambda path: path.count("/"),
                reverse=True,
            )
            if prune_known_batches:
                root = inventory["destination_root"].rstrip("/")
                for batch in repository.list_batches():
                    batch_root = f"{root}/{batch['id']}"
                    directories.extend([f"{batch_root}/photos", f"{batch_root}/videos", batch_root])
                directories = sorted(
                    set(directories),
                    key=lambda path: path.count("/"),
                    reverse=True,
                )
            for directory in directories:
                if directory != inventory["destination_root"]:
                    await adb.remove_directory(directory)
        except AdbError as exc:
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        return {
            "deleted": deleted,
            "deleted_count": len(deleted),
            "directories_checked": len(directories),
        }

    @router.post("/device/storage/orphans/purge")
    async def purge_storage_orphans(
        payload: OrphanPurgeRequest,
        user: MutatingUser,
    ) -> dict:
        inventory = await pixel_storage_inventory()
        result = await remove_storage_orphans(inventory, payload.paths)
        db.audit(
            "device.storage_orphans_purge",
            "device_storage",
            inventory["destination_root"],
            user["user_id"],
            {"deleted_count": result["deleted_count"], "paths": result["deleted"]},
        )
        await events.publish("device", {"action": "storage_cleanup"})
        return result

    @router.post("/device/storage/cleanup")
    async def general_storage_cleanup(user: MutatingUser) -> dict:
        inventory = await pixel_storage_inventory()
        orphan_paths = [file["path"] for file in inventory["files"] if not file["tracked"]]
        result = await remove_storage_orphans(
            inventory,
            orphan_paths,
            prune_known_batches=True,
        )
        db.audit(
            "device.storage_general_cleanup",
            "device_storage",
            inventory["destination_root"],
            user["user_id"],
            {
                "deleted_count": result["deleted_count"],
                "paths": result["deleted"],
                "directories_checked": result["directories_checked"],
                "tracked_files_deleted": 0,
            },
        )
        await events.publish("device", {"action": "storage_cleanup"})
        return {
            **result,
            "tracked_files_deleted": 0,
        }

    @router.post("/device/storage/clean-slate")
    async def clean_slate_storage(
        _payload: StorageTreeResetRequest,
        user: MutatingUser,
    ) -> dict:
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before resetting Relay storage",
                status_code=409,
            )
        destination_root = (
            settings.ftp_destination_root
            if settings.connection_mode == "ftp"
            else settings.destination_root
        )
        plan = repository.destination_reset_plan(destination_root)
        worker.maintenance_reason = "storage_clean_slate"
        try:
            inventory = await pixel_storage_inventory()
            reset_root = await adb.reset_destination_tree()
            if reset_root != destination_root:
                raise DomainError(
                    "storage_reset_mismatch",
                    "The transport reset a different destination than requested",
                    status_code=409,
                )
            result = repository.reconcile_destination_reset(
                destination_root,
                user["user_id"],
            )
            result.update(
                {
                    "known_files_deleted": len(inventory["files"]),
                    "known_bytes_deleted": inventory["relay_allocated_bytes"],
                }
            )
            db.audit(
                "device.storage_clean_slate",
                "device_storage",
                destination_root,
                user["user_id"],
                {
                    **result,
                    "planned_batch_count": plan["batch_count"],
                    "planned_item_count": plan["item_count"],
                },
            )
            with contextlib.suppress(Exception):
                await worker.refresh_device()
            await events.publish(
                "device",
                {"action": "storage_clean_slate", "destination_root": destination_root},
            )
            await events.publish("batch", {"action": "storage_clean_slate"})
            return result
        except AdbError as exc:
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        finally:
            worker.maintenance_reason = None
            worker.wake()

    @router.post("/device/storage/free-space")
    async def free_pixel_space(user: MutatingUser) -> dict:
        inventory = await pixel_storage_inventory()
        before_free = inventory.get("storage_free_bytes")
        orphan_paths = [file["path"] for file in inventory["files"] if not file["tracked"]]
        cleanup = await remove_storage_orphans(
            inventory,
            orphan_paths,
            prune_known_batches=True,
        )
        cache_trim_supported = True
        target_free = inventory.get("storage_total_bytes") or 10 * 1024**4
        await adb.trim_caches(int(target_free))
        refreshed = await worker.refresh_device()
        after_free = refreshed.get("storage_free_bytes")
        reclaimed = (
            max(0, int(after_free) - int(before_free))
            if isinstance(before_free, int) and isinstance(after_free, int)
            else None
        )
        result = {
            **cleanup,
            "cache_trim_supported": cache_trim_supported,
            "cache_trimmed": cache_trim_supported,
            "free_before_bytes": before_free,
            "free_after_bytes": after_free,
            "reclaimed_bytes": reclaimed,
            "tracked_files_deleted": 0,
        }
        db.audit(
            "device.storage_free_space",
            "device_storage",
            inventory["destination_root"],
            user["user_id"],
            {
                "deleted_orphan_count": cleanup["deleted_count"],
                "directories_checked": cleanup["directories_checked"],
                "cache_trimmed": cache_trim_supported,
                "reclaimed_bytes": reclaimed,
                "tracked_files_deleted": 0,
            },
        )
        return result

    @router.get("/system/directories")
    async def list_server_directories(
        _user: Authenticated,
        path: str | None = Query(default=None, min_length=1, max_length=4096),
    ) -> dict:
        try:
            directory = (
                local_path(path).expanduser().resolve(strict=True)
                if path
                else default_server_directory().resolve(strict=True)
            )
        except (OSError, RuntimeError) as exc:
            raise DomainError(
                "directory_not_found",
                "The server directory does not exist or cannot be resolved",
                status_code=404,
            ) from exc
        if not directory.is_dir():
            raise DomainError(
                "not_a_directory",
                "The selected server path is not a directory",
            )

        def read_directories() -> list[dict[str, str]]:
            with os.scandir(directory) as iterator:
                return sorted(
                    (
                        {"name": entry.name, "path": entry.path}
                        for entry in iterator
                        if entry.is_dir(follow_symlinks=True)
                    ),
                    key=lambda entry: entry["name"].casefold(),
                )

        try:
            entries = await asyncio.to_thread(read_directories)
        except PermissionError as exc:
            raise DomainError(
                "directory_permission_denied",
                "The service cannot read this server directory",
                status_code=403,
            ) from exc
        shortcuts = [Path.home()]
        if sys.platform == "win32":
            shortcuts.extend(
                Path(f"{letter}:\\")
                for letter in string.ascii_uppercase
                if Path(f"{letter}:\\").is_dir()
            )
        else:
            shortcuts = [Path("/"), *shortcuts, Path("/Volumes")]
        unique_shortcuts = list(dict.fromkeys(shortcut.resolve() for shortcut in shortcuts))
        return {
            "path": str(directory),
            "parent": None if directory.parent == directory else str(directory.parent),
            "entries": entries,
            "shortcuts": [
                {
                    "name": (
                        "Server root"
                        if shortcut == Path("/")
                        else shortcut.name or shortcut.anchor or str(shortcut)
                    ),
                    "path": str(shortcut),
                }
                for shortcut in unique_shortcuts
                if shortcut.is_dir()
            ],
        }

    @router.post("/device/refresh")
    async def refresh_device(_user: MutatingUser) -> dict:
        return await worker.refresh_device()

    @router.post("/device/adb-server/restart")
    async def restart_adb_server(user: MutatingUser) -> dict:
        try:
            result = await adb.restart_server()
        except AdbError as exc:
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        result["device"] = await worker.refresh_device()
        db.audit(
            "device.adb_server_restart",
            "adb_server",
            "local",
            user["user_id"],
            {
                "restarted": result["restarted"],
                "stop_returncode": result["stop_returncode"],
            },
        )
        await events.publish("device", {"action": "adb_server_restarted"})
        return result

    @router.post("/device/adb-speed-test")
    async def adb_speed_test(user: MutatingUser) -> dict:
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before running an ADB speed test",
                status_code=409,
            )
        worker.maintenance_reason = "adb_speed_test"
        db.audit(
            "device.adb_speed_test_started",
            "device",
            settings.device_serial if settings.connection_mode in {"network", "ftp"} else "USB",
            user["user_id"],
            {"connection_mode": settings.connection_mode},
        )
        try:
            result = await adb.speed_test()
            db.audit(
                "device.adb_speed_test_completed",
                "device",
                str(result["serial"]),
                user["user_id"],
                {
                    "connection_mode": result["connection_mode"],
                    "size_bytes": result["size_bytes"],
                    "duration_seconds": result["duration_seconds"],
                    "bytes_per_second": result["bytes_per_second"],
                    "checksum_verified": result["checksum_verified"],
                    "temporary_files_removed": result["temporary_files_removed"],
                },
            )
            await events.publish(
                "device",
                {
                    "action": "adb_speed_test_completed",
                    "connection_mode": result["connection_mode"],
                },
            )
            return result
        except AdbError as exc:
            db.audit(
                "device.adb_speed_test_failed",
                "device",
                settings.device_serial if settings.connection_mode in {"network", "ftp"} else "USB",
                user["user_id"],
                {
                    "connection_mode": settings.connection_mode,
                    "error": str(exc),
                },
            )
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        finally:
            worker.maintenance_reason = None
            worker.wake()

    @router.post("/device/ftp-connection-test")
    async def ftp_connection_test(
        payload: FtpTestRequest,
        user: MutatingUser,
    ) -> dict:
        overrides = ftp_test_overrides(payload)
        server = f"{overrides['ftp_host']}:{overrides['ftp_port']}"
        try:
            result = await adb.ftp_connection_test(overrides)
        except AdbError as exc:
            db.audit(
                "device.ftp_connection_test_failed",
                "device",
                server,
                user["user_id"],
                {"error": str(exc)},
            )
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        db.audit(
            "device.ftp_connection_test_completed",
            "device",
            server,
            user["user_id"],
            {"destination_root": overrides["ftp_destination_root"]},
        )
        return result

    @router.post("/device/ftp-speed-test")
    async def ftp_speed_test(payload: FtpTestRequest, user: MutatingUser) -> dict:
        if worker.active_batch_id or worker.maintenance_reason:
            raise DomainError(
                "device_busy",
                "Wait for active Pixel work to finish before running an FTP speed test",
                status_code=409,
            )
        worker.maintenance_reason = "ftp_speed_test"
        overrides = ftp_test_overrides(payload)
        server = f"{overrides['ftp_host']}:{overrides['ftp_port']}"
        db.audit(
            "device.ftp_speed_test_started",
            "device",
            server,
            user["user_id"],
            {"destination_root": overrides["ftp_destination_root"]},
        )
        try:
            result = await adb.ftp_speed_test(overrides)
            db.audit(
                "device.ftp_speed_test_completed",
                "device",
                server,
                user["user_id"],
                {
                    "size_bytes": result["size_bytes"],
                    "upload_duration_seconds": result["upload_duration_seconds"],
                    "upload_bytes_per_second": result["upload_bytes_per_second"],
                    "verification_duration_seconds": result["verification_duration_seconds"],
                    "verified_bytes_per_second": result["verified_bytes_per_second"],
                    "checksum_verified": result["checksum_verified"],
                    "temporary_files_removed": result["temporary_files_removed"],
                },
            )
            await events.publish(
                "device",
                {"action": "ftp_speed_test_completed", "connection_mode": "ftp"},
            )
            return result
        except AdbError as exc:
            db.audit(
                "device.ftp_speed_test_failed",
                "device",
                server,
                user["user_id"],
                {"error": str(exc)},
            )
            raise DomainError(exc.code, str(exc), status_code=409) from exc
        finally:
            worker.maintenance_reason = None
            worker.wake()

    @router.post("/device/adb-over-ip")
    async def enable_adb_over_ip(payload: AdbTcpipRequest, user: MutatingUser) -> dict:
        try:
            result = await adb.enable_tcpip(payload.port)
        except AdbError as exc:
            raise DomainError(exc.code, str(exc), status_code=409) from exc

        if result["connected"]:
            repository.set_setting(
                "device_serial",
                str(result["serial"]),
                user["user_id"],
            )
            repository.set_setting("connection_mode", "network", user["user_id"])
            apply_persisted_settings(settings, repository.setting)
            adb.serial = settings.device_serial
            adb.connection_mode = settings.connection_mode
            result["device"] = await worker.refresh_device()

        db.audit(
            "device.adb_over_ip_enable",
            "device",
            str(result.get("serial") or "usb"),
            user["user_id"],
            {
                "enabled": result["enabled"],
                "connected": result["connected"],
                "addresses": result["addresses"],
                "port": result["port"],
                "port_diagnostics": result["port_diagnostics"],
            },
        )
        await events.publish(
            "device",
            {
                "action": "adb_over_ip_enabled",
                "connected": result["connected"],
                "serial": result.get("serial"),
            },
        )
        return result

    @router.get("/sources")
    async def sources(_user: Authenticated) -> list[dict]:
        return repository.list_roots()

    @router.post("/sources", status_code=201)
    async def create_source(payload: SourceRootCreate, user: MutatingUser) -> dict:
        root = repository.add_root(payload.name, payload.path)
        db.audit("source.create", "source_root", str(root["id"]), user["user_id"])
        await events.publish("source", {"action": "created", "source": root})
        return root

    @router.delete("/sources/{root_id}")
    async def remove_source(root_id: int, user: MutatingUser) -> dict:
        removed = repository.remove_root(root_id, user["user_id"])
        await events.publish(
            "source",
            {"action": "removed", "root_id": root_id},
        )
        return removed

    @router.post("/sources/{root_id}/scan")
    async def scan_source(root_id: int, payload: ScanRequest, user: MutatingUser) -> dict:
        loop = asyncio.get_running_loop()
        last_progress_at = 0.0
        last_phase = ""
        latest_progress = {
            "phase": "enumerating",
            "processed": 0,
            "total": 0,
            "examined": 0,
            "discovered": 0,
            "skipped": 0,
            "cached": 0,
            "hashed": 0,
            "full_verify": payload.full_verify,
        }

        def report_progress(scan_progress: dict) -> None:
            nonlocal last_phase, last_progress_at
            latest_progress.update(scan_progress)
            now = time.monotonic()
            phase_changed = scan_progress["phase"] != last_phase
            if not phase_changed and now - last_progress_at < 0.25:
                return
            last_progress_at = now
            last_phase = scan_progress["phase"]
            event_data = {
                "root_id": root_id,
                **scan_progress,
                "complete": False,
                "failed": False,
            }
            loop.call_soon_threadsafe(
                lambda payload=event_data: asyncio.create_task(events.publish("scan", payload))
            )

        await events.publish(
            "scan",
            {
                "root_id": root_id,
                **latest_progress,
                "current_name": None,
                "complete": False,
                "failed": False,
            },
        )
        try:
            result = await asyncio.to_thread(
                repository.scan_root,
                root_id,
                payload.paths,
                report_progress,
                full_verify=payload.full_verify,
            )
        except Exception as exc:
            await events.publish(
                "scan",
                {
                    "root_id": root_id,
                    **latest_progress,
                    "current_name": None,
                    "complete": True,
                    "failed": True,
                    "message": str(exc),
                },
            )
            raise
        db.audit(
            "source.scan",
            "source_root",
            str(root_id),
            user["user_id"],
            {
                "discovered": len(result["files"]),
                "skipped": len(result["skipped"]),
                **result["stats"],
            },
        )
        await events.publish(
            "scan",
            {
                "root_id": root_id,
                **latest_progress,
                "current_name": None,
                "complete": True,
                "failed": False,
            },
        )
        await events.publish(
            "source",
            {"action": "scanned", "root_id": root_id, "count": len(result["files"])},
        )
        return result

    @router.get("/files")
    async def files(_user: Authenticated, unbatched_only: bool = Query(default=True)) -> list[dict]:
        return await asyncio.to_thread(
            repository.list_files,
            unbatched_only=unbatched_only,
        )

    @router.post("/uploads", status_code=201)
    async def upload(
        user: MutatingUser,
        media: UploadedMedia,
    ) -> dict:
        root = repository.ensure_import_root()
        submitted_name = (media.filename or "media").replace("\\", "/").rsplit("/", 1)[-1]
        submitted_path = Path(submitted_name)
        if is_macos_metadata(submitted_path):
            raise DomainError(
                "macos_metadata",
                "macOS AppleDouble metadata files are ignored",
            )
        extension = submitted_path.suffix.lower()
        if extension not in settings.allowed_extensions:
            raise DomainError("unsupported_media", f"Unsupported media extension: {extension}")
        original_stem = re.sub(r"[\x00-\x1f\x7f/]+", "_", submitted_path.stem)
        original_stem = original_stem[:160] or "media"
        original_extension = submitted_path.suffix
        destination = (
            Path(root["path"])
            / "pixel-relay-imports"
            / f"{uuid.uuid4().hex}-{original_stem}{original_extension}"
        )
        try:
            size, digest = await asyncio.to_thread(
                atomic_upload,
                media.file,
                destination,
                max_bytes=settings.max_batch_bytes,
            )
        except ValueError as exc:
            raise DomainError("upload_too_large", str(exc), status_code=413) from exc
        record = await asyncio.to_thread(repository.register_file, destination, root["id"], digest)
        db.audit(
            "source.upload",
            "source_file",
            str(record["id"]),
            user["user_id"],
            {"size": size, "original_name": media.filename},
        )
        await events.publish("source", {"action": "uploaded", "file": record})
        return record

    @router.get("/batches")
    async def batches(_user: Authenticated) -> list[dict]:
        return [enrich_batch(batch) for batch in repository.list_batches()]

    @router.get("/backups/items")
    async def backed_up_items(
        _user: Authenticated,
        limit: int = Query(default=250, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        return repository.list_backed_up_items(limit=limit, offset=offset)

    def current_batch_capacity() -> tuple[int | None, int, int | None, int | None]:
        storage_free = worker.latest_device.get("storage_free_bytes")
        storage_total = worker.latest_device.get("storage_total_bytes")
        reserve = max(
            settings.reserve_bytes,
            int(storage_total * settings.reserve_percent / 100)
            if isinstance(storage_total, int) and storage_total > 0
            else 0,
        )
        storage_batch_limit: int | None = None
        if isinstance(storage_free, int) and storage_free >= 0:
            storage_batch_limit = storage_free - reserve
            if storage_batch_limit <= 0:
                raise DomainError(
                    "pixel_storage_full",
                    "The Pixel has no usable storage above the configured safety reserve",
                    status_code=409,
                )
        return storage_batch_limit, reserve, storage_free, storage_total

    @router.post("/batches/plan")
    async def plan_batch(payload: BatchPlanRequest, _user: Authenticated) -> dict:
        storage_batch_limit, reserve, storage_free, storage_total = current_batch_capacity()
        plan = repository.plan_batches(
            payload.name,
            payload.file_ids,
            max_bytes=storage_batch_limit,
        )
        plan.update(
            {
                "storage_free_bytes": storage_free,
                "storage_total_bytes": storage_total,
                "storage_reserve_bytes": reserve,
            }
        )
        return plan

    @router.post("/batches", status_code=201)
    async def create_batch(payload: BatchCreate, user: MutatingUser) -> list[dict]:
        storage_batch_limit, _reserve, _storage_free, _storage_total = current_batch_capacity()
        batches = repository.create_batches(
            payload.name,
            payload.file_ids,
            user["user_id"],
            max_bytes=storage_batch_limit,
        )
        worker.wake()
        for batch in batches:
            await events.publish("batch", {"action": "created", "batch_id": batch["id"]})
        return batches

    @router.get("/batches/{batch_id}")
    async def get_batch(batch_id: str, _user: Authenticated) -> dict:
        return enrich_batch(repository.get_batch(batch_id))

    @router.get("/batches/{batch_id}/manifest")
    async def batch_manifest(batch_id: str, _user: Authenticated) -> JSONResponse:
        batch = enrich_batch(repository.get_batch(batch_id))
        events = db.fetchall(
            """
            SELECT state_events.item_id, state_events.from_state, state_events.to_state,
              state_events.detail, state_events.created_at
            FROM state_events
            JOIN batch_items ON batch_items.id=state_events.item_id
            WHERE batch_items.batch_id=?
            ORDER BY state_events.created_at, state_events.id
            """,
            (batch_id,),
        )
        events_by_item: dict[str, list[dict]] = {}
        for event in events:
            events_by_item.setdefault(event.pop("item_id"), []).append(event)
        items = []
        for item in batch.pop("items", []):
            items.append(
                {
                    "id": item["id"],
                    "source_path": item["path"],
                    "destination_path": item["remote_path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "mtime_ns": item["mtime_ns"],
                    "media_kind": item["media_kind"],
                    "extension": item["extension"],
                    "state": item["state"],
                    "attempts": item["attempts"],
                    "error_code": item.get("error_code"),
                    "error_detail": item.get("error_detail"),
                    "updated_at": item["updated_at"],
                    "events": events_by_item.get(item["id"], []),
                }
            )
        manifest = {
            "format": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "source_media_included": False,
            "batch": batch,
            "items": items,
        }
        return JSONResponse(
            manifest,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="pixel-relay-{batch_id[:12]}-manifest.json"'
                )
            },
        )

    @router.delete("/batches/{batch_id}")
    async def delete_batch(
        batch_id: str,
        user: MutatingUser,
    ) -> dict:
        if worker.active_batch_id == batch_id:
            raise DomainError(
                "batch_delete_busy",
                "Wait for the active device operation to stop before deleting this batch",
                status_code=409,
            )
        deleted = repository.delete_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish("batch", {"action": "deleted", "batch_id": batch_id})
        return deleted

    @router.post("/batches/{batch_id}/retry")
    async def retry_batch(batch_id: str, payload: RetryRequest, user: MutatingUser) -> dict:
        count = repository.retry_batch(batch_id, payload.include_purge_failures, user["user_id"])
        worker.wake()
        await events.publish("batch", {"action": "retry", "batch_id": batch_id, "items": count})
        return {"retried": count}

    @router.post("/batches/{batch_id}/retrigger", status_code=201)
    async def retrigger_batch(batch_id: str, user: MutatingUser) -> dict:
        batch = repository.retrigger_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish(
            "batch",
            {
                "action": "retriggered",
                "source_batch_id": batch_id,
                "batch_id": batch["id"],
            },
        )
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/pause")
    async def pause_batch(batch_id: str, user: MutatingUser) -> dict:
        batch = repository.pause_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish(
            "batch",
            {
                "action": "paused",
                "batch_id": batch_id,
                "active_item_finishing": worker.active_batch_id == batch_id,
            },
        )
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/resume")
    async def resume_batch(batch_id: str, user: MutatingUser) -> dict:
        batch = repository.resume_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish("batch", {"action": "resumed", "batch_id": batch_id})
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/cancel")
    async def cancel_batch(
        batch_id: str,
        _payload: BatchCancelRequest,
        user: MutatingUser,
    ) -> dict:
        batch = repository.cancel_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish("batch", {"action": "cancelled", "batch_id": batch_id})
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/skip")
    async def skip_stalled_batch(batch_id: str, user: MutatingUser) -> dict:
        """Skip a stalled batch so later split batches can proceed safely."""
        batch = repository.cancel_batch(batch_id, user["user_id"])
        worker.wake()
        await events.publish("batch", {"action": "skipped", "batch_id": batch_id})
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/confirm")
    async def confirm_batch(
        batch_id: str, _payload: ConfirmationRequest, user: MutatingUser
    ) -> dict:
        batch = repository.confirm_batch(batch_id, user["user_id"])
        await events.publish("batch", {"action": "confirmed", "batch_id": batch_id})
        return enrich_batch(batch)

    @router.post("/batches/{batch_id}/purge")
    async def purge_batch(batch_id: str, user: MutatingUser) -> dict:
        batch = await worker.purge_batch(batch_id, user["user_id"])
        worker.wake()
        return batch

    @router.get("/audit")
    async def audit(
        _user: Authenticated, limit: int = Query(default=200, ge=1, le=1000)
    ) -> list[dict]:
        return repository.audit_entries(limit)

    @router.get("/logs")
    async def service_logs(
        _user: Authenticated, limit: int = Query(default=200, ge=1, le=1000)
    ) -> list[dict]:
        def read_tail() -> list[dict]:
            if not settings.log_path.is_file():
                return []
            with settings.log_path.open(errors="replace") as handle:
                lines = deque(handle, maxlen=limit)
            records: list[dict] = []
            for line in reversed(lines):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    records.append(
                        {
                            "timestamp": None,
                            "level": "INFO",
                            "logger": "legacy",
                            "message": line.rstrip(),
                        }
                    )
            return records

        return await asyncio.to_thread(read_tail)

    @router.post("/app/update")
    async def update_app(_user: MutatingUser) -> dict:
        root = Path(__file__).resolve().parents[2]
        if not (root / ".git").is_dir():
            raise DomainError("app_update_unavailable", "This installation is not a Git checkout", status_code=409)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False)
        if status.returncode != 0:
            raise DomainError("app_update_failed", status.stderr.strip() or "Could not inspect the Git checkout", status_code=500)
        if status.stdout.strip():
            raise DomainError("app_update_dirty", "Update skipped because the local Git checkout has uncommitted changes", status_code=409)
        result = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DomainError("app_update_failed", result.stderr.strip() or result.stdout.strip() or "Git update failed", status_code=409)
        return {"updated": "Already up to date" not in result.stdout, "message": result.stdout.strip() or "Repository updated"}

    @router.get("/settings")
    async def read_settings(_user: Authenticated) -> dict:
        return settings_response()

    @router.patch("/settings")
    async def update_settings(payload: SettingUpdate, user: MutatingUser) -> dict:
        updates: dict[str, str] = {}
        if payload.device_serial is not None:
            value = payload.device_serial.strip()
            if not value:
                raise DomainError("invalid_device", "Device serial cannot be empty")
            updates["device_serial"] = value
        if payload.expected_primary_uuid is not None:
            value = payload.expected_primary_uuid.strip()
            if not SAFE_UUID.fullmatch(value):
                raise DomainError("invalid_uuid", "Storage UUID contains invalid characters")
            updates["expected_primary_uuid"] = value
        if payload.connection_mode is not None:
            updates["connection_mode"] = payload.connection_mode
        if payload.ftp_host is not None:
            value = payload.ftp_host.strip()
            if not value:
                raise DomainError("invalid_ftp_host", "FTP host cannot be empty")
            updates["ftp_host"] = value
        if payload.ftp_port is not None:
            updates["ftp_port"] = str(payload.ftp_port)
        if payload.ftp_username is not None:
            updates["ftp_username"] = payload.ftp_username.strip()
        if payload.ftp_password is not None:
            updates["ftp_password"] = payload.ftp_password
        if payload.ftp_destination_root is not None:
            value = payload.ftp_destination_root.strip().rstrip("/")
            if not value.startswith("/") or ".." in value.split("/") or not value:
                raise DomainError(
                    "invalid_ftp_destination",
                    "FTP destination must be an absolute path without '..'",
                )
            updates["ftp_destination_root"] = value
        if payload.destination_root is not None:
            try:
                updates["destination_root"] = Settings.validate_destination(
                    payload.destination_root.strip()
                )
            except ValueError as exc:
                raise DomainError("invalid_destination", str(exc)) from exc
        if payload.import_root is not None:
            import_root = payload.import_root.strip()
            if import_root:
                path = Path(import_root).expanduser()
                if not path.is_absolute():
                    raise DomainError("invalid_import_root", "Import root must be absolute")
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise DomainError(
                        "invalid_import_root",
                        f"Import root could not be created: {exc}",
                    ) from exc
                if not path.is_dir():
                    raise DomainError(
                        "invalid_import_root",
                        "Import root must be a directory",
                    )
                updates["import_root"] = str(path)
            else:
                updates["import_root"] = ""
        for key in (
            "max_batch_files",
            "max_batch_bytes",
            "reserve_bytes",
        ):
            value = getattr(payload, key)
            if value is not None:
                if key == "reserve_bytes" and value != settings.reserve_bytes:
                    internal_capacity = worker.latest_device.get("internal_storage_total_bytes")
                    if not isinstance(internal_capacity, int) or internal_capacity <= 0:
                        raise DomainError(
                            "pixel_capacity_unknown",
                            "Connect and refresh the Pixel before changing its storage buffer",
                            status_code=409,
                        )
                    if value > internal_capacity:
                        raise DomainError(
                            "storage_buffer_too_large",
                            "Pixel storage buffer cannot exceed measured internal storage",
                        )
                updates[key] = str(value)
        for key in ("pause_temperature_c", "resume_temperature_c"):
            value = getattr(payload, key)
            if value is not None:
                updates[key] = str(value)
        effective_pause = float(updates.get("pause_temperature_c", settings.pause_temperature_c))
        effective_resume = float(updates.get("resume_temperature_c", settings.resume_temperature_c))
        if effective_resume >= effective_pause:
            raise DomainError(
                "invalid_temperature_limits", "Resume temperature must be below pause temperature"
            )
        if not updates:
            raise DomainError("no_settings", "No setting was supplied")
        for key, value in updates.items():
            repository.set_setting(
                key,
                value,
                user["user_id"],
                sensitive=key
                in {
                    "ftp_password",
                },
            )
        apply_persisted_settings(settings, repository.setting)
        adb.serial = settings.device_serial
        adb.connection_mode = settings.connection_mode
        await events.publish(
            "setting",
            {
                key: "[redacted]"
                if key
                in {
                    "ftp_password",
                }
                else value
                for key, value in updates.items()
            },
        )
        return settings_response()

    @router.get("/events")
    async def stream_events(
        request: Request,
        _user: Authenticated,
        last_event_id: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        header_id = request.headers.get("Last-Event-ID")
        after_id = int(header_id) if header_id and header_id.isdigit() else last_event_id

        async def generate():
            logger.info(
                "Dashboard event stream connected",
                extra={"context": {"after_event_id": after_id}},
            )
            async for event in events.subscribe(after_id):
                if await request.is_disconnected():
                    break
                yield event.encode() if event else ": keepalive\n\n"
            logger.info(
                "Dashboard event stream disconnected",
                extra={
                    "context": {
                        "shutdown_requested": events.shutdown_requested,
                    }
                },
            )

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/metrics")
    async def metrics_summary(_user: Authenticated) -> dict:
        return queue_response()

    @app.get("/metrics")
    async def prometheus_metrics(_user: Authenticated) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
    mount_frontend(app, settings.frontend_dist)
    return app


def mount_frontend(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith("api/") or path == "metrics":
            raise HTTPException(status_code=404)
        candidate = dist / path
        if path and candidate.is_file() and candidate.resolve().is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "code": "frontend_not_built",
                "message": "Run ./pixel-relay build-ui before starting the service",
            },
        )

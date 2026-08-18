from __future__ import annotations

from enum import StrEnum


class ItemState(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    TRANSFERRING = "transferring"
    STAGED_ON_PIXEL = "staged_on_pixel"
    AWAITING_BACKUP_CONFIRMATION = "awaiting_backup_confirmation"
    CONFIRMED_BACKED_UP = "confirmed_backed_up"
    PURGED_FROM_PIXEL = "purged_from_pixel"
    CANCELLED = "cancelled"
    CANCELLED_ON_PIXEL = "cancelled_on_pixel"
    TRANSFER_FAILED = "transfer_failed"
    MEDIA_SCAN_FAILED = "media_scan_failed"
    DEVICE_OFFLINE = "device_offline"
    STORAGE_MISSING = "storage_missing"
    TEMPERATURE_PAUSED = "temperature_paused"
    PURGE_FAILED = "purge_failed"


TERMINAL_STATES = {ItemState.PURGED_FROM_PIXEL, ItemState.CANCELLED}
RETRYABLE_STATES = {
    ItemState.TRANSFER_FAILED,
    ItemState.MEDIA_SCAN_FAILED,
    ItemState.DEVICE_OFFLINE,
    ItemState.STORAGE_MISSING,
    ItemState.TEMPERATURE_PAUSED,
    ItemState.PURGE_FAILED,
}

VALID_TRANSITIONS: dict[ItemState, set[ItemState]] = {
    ItemState.DISCOVERED: {ItemState.QUEUED},
    ItemState.QUEUED: {
        ItemState.TRANSFERRING,
        ItemState.DEVICE_OFFLINE,
        ItemState.STORAGE_MISSING,
        ItemState.TEMPERATURE_PAUSED,
        ItemState.TRANSFER_FAILED,
        ItemState.CANCELLED,
    },
    ItemState.TRANSFERRING: {
        ItemState.STAGED_ON_PIXEL,
        ItemState.TRANSFER_FAILED,
        ItemState.DEVICE_OFFLINE,
        ItemState.STORAGE_MISSING,
        ItemState.TEMPERATURE_PAUSED,
        ItemState.CANCELLED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.STAGED_ON_PIXEL: {
        ItemState.AWAITING_BACKUP_CONFIRMATION,
        ItemState.MEDIA_SCAN_FAILED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.MEDIA_SCAN_FAILED: {
        ItemState.STAGED_ON_PIXEL,
        ItemState.QUEUED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.AWAITING_BACKUP_CONFIRMATION: {
        ItemState.CONFIRMED_BACKED_UP,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.CONFIRMED_BACKED_UP: {
        ItemState.PURGED_FROM_PIXEL,
        ItemState.PURGE_FAILED,
        ItemState.DEVICE_OFFLINE,
        ItemState.STORAGE_MISSING,
    },
    ItemState.PURGE_FAILED: {
        ItemState.PURGED_FROM_PIXEL,
        ItemState.CONFIRMED_BACKED_UP,
        ItemState.CANCELLED_ON_PIXEL,
        ItemState.DEVICE_OFFLINE,
        ItemState.STORAGE_MISSING,
    },
    ItemState.DEVICE_OFFLINE: {
        ItemState.QUEUED,
        ItemState.CONFIRMED_BACKED_UP,
        ItemState.CANCELLED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.STORAGE_MISSING: {
        ItemState.QUEUED,
        ItemState.CONFIRMED_BACKED_UP,
        ItemState.CANCELLED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.TEMPERATURE_PAUSED: {ItemState.QUEUED, ItemState.CANCELLED},
    ItemState.TRANSFER_FAILED: {
        ItemState.QUEUED,
        ItemState.CANCELLED,
        ItemState.CANCELLED_ON_PIXEL,
    },
    ItemState.CANCELLED: set(),
    ItemState.CANCELLED_ON_PIXEL: {
        ItemState.PURGED_FROM_PIXEL,
        ItemState.PURGE_FAILED,
        ItemState.DEVICE_OFFLINE,
        ItemState.STORAGE_MISSING,
    },
    ItemState.PURGED_FROM_PIXEL: set(),
}


def can_transition(current: str, target: str) -> bool:
    return ItemState(target) in VALID_TRANSITIONS[ItemState(current)]

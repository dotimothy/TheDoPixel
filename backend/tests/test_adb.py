import hashlib
from pathlib import Path

import pytest
from pixel_relay.adb import (
    AdbError,
    CommandResult,
    DeviceSnapshot,
    SafeAdb,
    ipv4_first,
    output_preview,
    parse_adb_progress,
    parse_battery,
    parse_connectivity,
    parse_df,
    parse_du_inventory,
    parse_ipv4_addresses,
    parse_port_listeners,
    parse_storage_service_dump,
    parse_storage_volumes,
    primary_storage_move_guidance,
)
from pixel_relay.config import Settings
from pydantic import ValidationError


def test_parse_pixel_battery() -> None:
    result = parse_battery(
        """
        AC powered: false
        USB powered: true
        Wireless powered: false
        status: 2
        level: 87
        temperature: 326
        """
    )
    assert result == {
        "level": 87,
        "status": 2,
        "temperature_c": 32.6,
        "charging": True,
    }


def test_parse_android_storage_volumes() -> None:
    assert parse_storage_volumes(
        [
            "private mounted null",
            "public:8,97 mounted 7479-08F4",
            "private:8,98 mounted 01234567-89ab-cdef-0123-456789abcdef",
            "emulated:8,98;0 mounted 01234567-89ab-cdef-0123-456789abcdef",
            "private:8,99 unmountable",
            "malformed",
        ]
    ) == [
        {
            "volume_id": "private",
            "volume_type": "private",
            "state": "mounted",
            "fs_uuid": None,
        },
        {
            "volume_id": "public:8,97",
            "volume_type": "public",
            "state": "mounted",
            "fs_uuid": "7479-08F4",
        },
        {
            "volume_id": "private:8,98",
            "volume_type": "private",
            "state": "mounted",
            "fs_uuid": "01234567-89ab-cdef-0123-456789abcdef",
        },
        {
            "volume_id": "emulated:8,98;0",
            "volume_type": "emulated",
            "state": "mounted",
            "fs_uuid": "01234567-89ab-cdef-0123-456789abcdef",
        },
        {
            "volume_id": "private:8,99",
            "volume_type": "private",
            "state": "unmountable",
            "fs_uuid": None,
        },
    ]


def test_parse_android_storage_service_disk_details() -> None:
    parsed = parse_storage_service_dump(
        """
        Disks:
          DiskInfo:
            id=disk:8,96 flags=ADOPTABLE|USB size=500000000000
            label=Relay SSD volumeIds=[public:8,97] sysPath=/devices/usb1

        Volumes:
          VolumeInfo{public:8,97}:
            type=TYPE_PUBLIC diskId=disk:8,96 partGuid=null
            mountFlags=MOUNT_FLAG_VISIBLE mountUserId=0 state=STATE_MOUNTED
            fsType=vfat fsUuid=7479-08F4 fsLabel=RELAY
            path=/storage/7479-08F4 internalPath=/mnt/media_rw/7479-08F4
        """
    )

    assert parsed["disks"] == [
        {
            "disk_id": "disk:8,96",
            "flags": ["ADOPTABLE", "USB"],
            "adoptable": True,
            "default_primary": False,
            "usb": True,
            "sd": False,
            "size_bytes": 500000000000,
            "label": "Relay SSD",
            "volume_ids": ["public:8,97"],
            "sys_path": "/devices/usb1",
        }
    ]
    assert parsed["volumes"][0] == {
        "volume_id": "public:8,97",
        "volume_type": "public",
        "disk_id": "disk:8,96",
        "state": "mounted",
        "fs_type": "vfat",
        "fs_uuid": "7479-08F4",
        "fs_label": "RELAY",
        "path": "/storage/7479-08F4",
    }


def test_parse_android_storage_service_preserves_empty_lun_size() -> None:
    parsed = parse_storage_service_dump(
        """
        Disks:
          DiskInfo:
            id=disk:8,96 flags=ADOPTABLE|USB size=-1
            label=Mass volumeIds=[] sysPath=/devices/usb1/block/sdg
        """
    )

    assert parsed["disks"][0]["size_bytes"] == -1


async def test_storage_devices_ignores_empty_usb_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def connect() -> None:
        return None

    async def shell(*args: str, **_kwargs) -> CommandResult:
        outputs = {
            ("sm", "list-disks"): "disk:8,96\ndisk:8,112",
            ("sm", "list-disks", "adoptable"): "disk:8,96\ndisk:8,112",
            ("sm", "list-volumes", "all"): "public:8,113 mounted LEXAR-UUID",
            (
                "dumpsys",
                "mount",
            ): """
                Disks:
                  DiskInfo:
                    id=disk:8,96 flags=ADOPTABLE|USB size=-1
                    label=Mass volumeIds=[]
                    sysPath=/devices/usb1/block/sdg
                  DiskInfo:
                    id=disk:8,112 flags=ADOPTABLE|USB size=495315845120
                    label=Lexar volumeIds=[public:8,113]
                    sysPath=/devices/usb1/block/sdh
                Volumes:
                  VolumeInfo{public:8,113}:
                    type=TYPE_PUBLIC diskId=disk:8,112 state=STATE_MOUNTED
                    fsType=exfat fsUuid=LEXAR-UUID fsLabel=LEXAR
            """,
            ("sm", "get-primary-storage-uuid"): "null",
        }
        return CommandResult(0, outputs[args], "")

    monkeypatch.setattr(adb, "connect", connect)
    monkeypatch.setattr(adb, "shell", shell)

    result = await adb.storage_devices()

    assert [disk["disk_id"] for disk in result["disks"]] == ["disk:8,112"]
    assert [disk["disk_id"] for disk in result["ignored_disks"]] == ["disk:8,96"]
    ignored = result["ignored_disks"][0]
    assert ignored["ignored_reason"] == "empty_usb_bridge"
    assert ignored["size_bytes"] == -1
    assert ignored["volumes"] == []


def test_parse_active_pixel_network_settings() -> None:
    output = """
    Active default network: 126
    Current Networks:
      NetworkAgentInfo{ ni{[type: WIFI[], state: CONNECTED/CONNECTED]} network{126}
      lp{{InterfaceName: wlan0 LinkAddresses: [ fe80::1/64,192.168.1.10/23 ]
      DnsAddresses: [ /192.168.1.1,/1.1.1.1 ] Routes:
      [ 0.0.0.0/0 -> 192.168.1.1 wlan0 ]}}
      nc{[ Capabilities: NOT_METERED&INTERNET&VALIDATED
      SignalStrength: -2 SSID: "TheDoMesh"]} }
    """

    assert parse_connectivity(" ".join(line.strip() for line in output.splitlines())) == {
        "network_type": "WIFI",
        "interface": "wlan0",
        "addresses": ["192.168.1.10/23", "fe80::1/64"],
        "gateway": "192.168.1.1",
        "dns_servers": ["192.168.1.1", "1.1.1.1"],
        "ssid": "TheDoMesh",
        "validated": True,
        "metered": False,
    }


def test_network_values_present_ipv4_before_ipv6() -> None:
    assert ipv4_first(["fe80::1/64", "192.168.1.35/24", "2001:db8::1/64"]) == [
        "192.168.1.35/24",
        "fe80::1/64",
        "2001:db8::1/64",
    ]


def test_parse_usb_ipv4_addresses_prefers_route_source_and_deduplicates() -> None:
    output = (
        "default via 192.168.1.1 dev eth0 src 192.168.1.35\n"
        "2: eth0 inet 192.168.1.35/24 brd 192.168.1.255 scope global eth0\n"
        "3: wlan0 inet 192.168.1.36/24 scope global wlan0"
    )
    assert parse_ipv4_addresses(output) == ["192.168.1.35", "192.168.1.36"]


def test_parse_selected_port_listener_identity() -> None:
    output = (
        'LISTEN 0 4 0.0.0.0:5566 0.0.0.0:* users:(("ftp-server",pid=412,fd=7))\n'
        'LISTEN 0 4 0.0.0.0:5555 0.0.0.0:* users:(("adbd",pid=91,fd=4))\n'
        "LISTEN 0 4 [::]:5566 [::]:*"
    )
    assert parse_port_listeners(output, 5566) == [
        {
            "name": "ftp-server",
            "pid": 412,
            "fd": 7,
            "local_address": "0.0.0.0:5566",
        },
        {
            "name": None,
            "pid": None,
            "fd": None,
            "local_address": "[::]:5566",
        },
    ]


def test_adb_output_preview_is_bounded() -> None:
    assert output_preview(b"") is None
    assert output_preview(b"connected to 192.168.1.35:5555") == ("connected to 192.168.1.35:5555")
    assert output_preview(b"x" * 20, limit=8) == "xxxxxxxx… (12 more characters)"


def test_primary_storage_failure_explains_locked_android_user() -> None:
    guidance = primary_storage_move_guidance("Failure [-10]")

    assert "user profile is locked" in guidance
    assert "PIN, pattern, or password" in guidance
    assert "home screen" in guidance


def test_adb_push_progress_parser_uses_latest_percentage() -> None:
    assert parse_adb_progress("[ 12%] file.jpg\r[ 47%] file.jpg") == 47
    assert parse_adb_progress("file.jpg: 100%") == 100
    assert parse_adb_progress("no progress") is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Filesystem 1K-blocks Used Available Use% Mounted on\n"
            "/dev/fuse 488245280 12204200 476041080 3% /storage/emulated",
            {"total": 488245280, "used": 12204200, "free": 476041080},
        ),
        ("df: /sdcard: No such file or directory", None),
        ("", None),
    ],
)
def test_parse_df(output: str, expected: dict | None) -> None:
    assert parse_df(output) == expected


def test_parse_storage_inventory_keeps_only_generated_relay_files() -> None:
    root = "/sdcard/DCIM/Camera/PixelRelay"
    batch_id = "0123456789abcdef0123456789abcdef"
    output = (
        f"12\t/sdcard/DCIM/Camera/PixelRelay/{batch_id}/photos/"
        "IMG_0001.jpg\n"
        f"20\t/sdcard/DCIM/Camera/PixelRelay/{batch_id}/photos\n"
        "4\t/sdcard/DCIM/Camera/other.jpg\n"
    )
    assert parse_du_inventory(output, root) == [
        {
            "path": (f"/sdcard/DCIM/Camera/PixelRelay/{batch_id}/photos/IMG_0001.jpg"),
            "allocated_bytes": 12 * 1024,
        }
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/sdcard/DCIM/Camera/PixelRelay/aabbcc/item.jpg",
        "/sdcard/DCIM/Camera/PixelRelay/123/file-1.heic",
        "/sdcard/DCIM/Camera/PixelRelay/123/Family fête (Final).JPG",
    ],
)
def test_remote_path_accepts_generated_paths(path: str) -> None:
    assert SafeAdb.validate_remote_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/data/local/tmp/file.jpg",
        "/sdcard/DCIM/../secret",
        "/sdcard/DCIM/file\nname.jpg",
        "/sdcard/DCIM//file.jpg",
    ],
)
def test_remote_path_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        SafeAdb.validate_remote_path(path)


@pytest.mark.asyncio
async def test_remove_batch_directory_uses_one_recursive_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    batch_id = "0123456789abcdef0123456789abcdef"
    batch_directory = f"/sdcard/DCIM/Camera/PixelRelay/{batch_id}"
    calls: list[tuple[tuple[str, ...], dict]] = []

    async def shell(*args: str, **kwargs) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(1 if args[0] == "stat" else 0, "", "")

    monkeypatch.setattr(adb, "shell", shell)

    await adb.remove_batch_directory(batch_directory)

    assert calls == [
        (("rm", "-rf", "--", batch_directory), {"timeout": 60 * 60}),
        (("stat", batch_directory), {"check": False}),
    ]


@pytest.mark.asyncio
async def test_reset_destination_tree_uses_long_timeout_and_recreates_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    root = "/sdcard/DCIM/Camera/PixelRelay"
    calls: list[tuple[tuple[str, ...], dict]] = []

    async def shell(*args: str, **kwargs) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(1 if args[0] == "stat" else 0, "", "")

    monkeypatch.setattr(adb, "shell", shell)

    assert await adb.reset_destination_tree() == root
    assert calls == [
        (("rm", "-rf", "--", root), {"timeout": 60 * 60}),
        (("stat", root), {"check": False}),
        (("mkdir", "-p", root), {}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/sdcard/DCIM/Camera/PixelRelay",
        "/sdcard/DCIM/Camera/PixelRelay/not-a-batch",
        "/sdcard/DCIM/Camera/other/0123456789abcdef0123456789abcdef",
    ],
)
async def test_remove_batch_directory_rejects_non_generated_paths(
    tmp_path: Path, path: str
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    with pytest.raises(ValueError):
        await adb.remove_batch_directory(path)


def test_destination_must_be_beneath_sdcard(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, destination_root="/data/local/tmp")


def test_usb_mode_uses_the_first_authorized_adb_device(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, connection_mode="usb")
    adb = SafeAdb(settings)
    assert adb.selector == ["-d"]


async def test_cache_trim_uses_only_the_fixed_package_manager_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    captured: list[tuple[tuple[str, ...], dict]] = []

    async def shell(*args: str, **kwargs) -> CommandResult:
        captured.append((args, kwargs))
        return CommandResult(0, "", "")

    monkeypatch.setattr(adb, "shell", shell)

    await adb.trim_caches(25 * 1024**3)

    assert captured == [
        (
            ("pm", "trim-caches", str(25 * 1024**3), "internal"),
            {"timeout": 120},
        )
    ]


async def test_enable_tcpip_uses_usb_and_selected_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    calls: list[tuple[list[str], dict]] = []

    async def run(args: list[str], **kwargs) -> CommandResult:
        calls.append((args, kwargs))
        if args == ["-d", "get-state"] or args[-1:] == ["get-state"]:
            return CommandResult(0, "device", "")
        if args == ["-d", "shell", "ss -ltnp"]:
            return CommandResult(
                0,
                "LISTEN 0 4 *:5566 *:*",
                "",
            )
        if args == ["-d", "shell", "getprop service.adb.tcp.port"]:
            return CommandResult(0, "5566", "")
        if args == ["-d", "shell", "ip -4 route"]:
            return CommandResult(
                0,
                "default via 192.168.1.1 dev eth0 src 192.168.1.35",
                "",
            )
        if args == ["-d", "shell", "ip -4 addr show scope global"]:
            return CommandResult(0, "inet 192.168.1.35/24 scope global eth0", "")
        return CommandResult(0, "restarting in TCP mode port: 5566", "")

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(adb, "_run", run)
    monkeypatch.setattr("pixel_relay.adb.asyncio.sleep", no_wait)

    result = await adb.enable_tcpip(5566)

    assert result["connected"] is True
    assert result["serial"] == "192.168.1.35:5566"
    assert result["port_diagnostics"]["listeners"][0] == {
        "name": "adbd",
        "pid": None,
        "fd": None,
        "local_address": "*:5566",
        "identity_inferred": True,
    }
    assert result["port_diagnostics"]["adb_tcp_port_before_restart"] == 5566
    assert (["-d", "tcpip", "5566"], {}) in calls
    assert any(call[0] == ["connect", "192.168.1.35:5566"] for call in calls)


async def test_restart_adb_server_uses_only_fixed_host_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    calls: list[tuple[list[str], dict]] = []

    async def run(args: list[str], **kwargs) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(0, "", "")

    monkeypatch.setattr(adb, "_run", run)

    result = await adb.restart_server()

    assert result["restarted"] is True
    assert calls == [
        (["kill-server"], {"check": False}),
        (["start-server"], {}),
    ]


async def test_internal_storage_is_ready_without_an_adopted_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    snapshot = DeviceSnapshot(
        state="device",
        storage_ready=True,
        storage_free_bytes=10 * 1024**3,
    )

    async def read_snapshot(_expected_uuid: str = "") -> DeviceSnapshot:
        return snapshot

    monkeypatch.setattr(adb, "snapshot", read_snapshot)
    assert await adb.ensure_ready("") is snapshot


async def test_snapshot_measures_internal_data_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def connect() -> None:
        return None

    async def shell(*args: str, **_kwargs) -> CommandResult:
        if args == ("df", "-k", "/sdcard"):
            return CommandResult(
                0,
                "/dev/fuse 100000 25000 75000 25% /storage/emulated",
                "",
            )
        if args == ("df", "-k", "/data"):
            return CommandResult(
                0,
                "/dev/block/data 30000000 10000000 20000000 34% /data",
                "",
            )
        return CommandResult(0, "", "")

    monkeypatch.setattr(adb, "connect", connect)
    monkeypatch.setattr(adb, "shell", shell)

    snapshot = await adb.snapshot("")

    assert snapshot.storage_total_bytes == 100000 * 1024
    assert snapshot.internal_storage_total_bytes == 30000000 * 1024
    assert snapshot.internal_storage_free_bytes == 20000000 * 1024


async def test_adoption_uses_fixed_partition_and_primary_migration_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    adopted_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    inventories = iter(
        [
            {
                "disks": [{"disk_id": "disk:8,96", "adoptable": True}],
                "volumes": [
                    {
                        "volume_id": "public:8,97",
                        "volume_type": "public",
                        "fs_uuid": "7479-08F4",
                        "disk_id": "disk:8,96",
                    }
                ],
            },
            {
                "disks": [{"disk_id": "disk:8,96", "adoptable": True}],
                "volumes": [
                    {
                        "volume_id": "private:8,98",
                        "volume_type": "private",
                        "fs_uuid": adopted_uuid,
                        "disk_id": "disk:8,96",
                    }
                ],
            },
            {
                "disks": [{"disk_id": "disk:8,96", "adoptable": True}],
                "volumes": [],
                "current_primary_uuid": adopted_uuid,
            },
        ]
    )
    calls: list[tuple[tuple[str, ...], int | None]] = []
    progress_events: list[dict] = []

    async def storage_devices() -> dict:
        return next(inventories)

    async def shell(
        *args: str,
        timeout: int | None = None,
        **_kwargs,
    ) -> CommandResult:
        calls.append((args, timeout))
        return CommandResult(0, "Success", "")

    async def no_sleep(_seconds: float) -> None:
        return None

    async def progress(event: dict) -> None:
        progress_events.append(event)

    monkeypatch.setattr(adb, "storage_devices", storage_devices)
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr("pixel_relay.adb.asyncio.sleep", no_sleep)

    result = await adb.adopt_storage(
        "disk:8,96",
        force_adoptable=False,
        migrate_primary=True,
        progress=progress,
    )

    assert calls[0][0] == ("sm", "partition", "disk:8,96", "private")
    assert calls[1][0] == ("pm", "move-primary-storage", adopted_uuid)
    assert result["adopted_uuid"] == adopted_uuid
    assert result["migrated_primary"] is True
    assert [event["stage"] for event in progress_events] == [
        "inspecting",
        "verified",
        "partitioning",
        "discovering_volume",
        "migrating_primary",
        "finalizing",
    ]


async def test_adoption_partition_failure_explains_stage_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def storage_devices() -> dict:
        return {
            "disks": [
                {
                    "disk_id": "disk:8,96",
                    "adoptable": True,
                    "volumes": [],
                }
            ],
            "volumes": [],
        }

    async def shell(*_args: str, **_kwargs) -> CommandResult:
        raise AdbError(
            "ADB command failed with exit status 1; Android returned no diagnostic output"
        )

    monkeypatch.setattr(adb, "storage_devices", storage_devices)
    monkeypatch.setattr(adb, "shell", shell)

    with pytest.raises(AdbError) as raised:
        await adb.adopt_storage(
            "disk:8,96",
            force_adoptable=False,
            migrate_primary=False,
        )

    message = str(raised.value)
    assert "Android could not erase and adopt disk:8,96" in message
    assert "hub and SSD power" in message
    assert "partially repartitioned" in message
    assert "exit status 1" in message


async def test_primary_storage_switch_moves_sdcard_back_to_internal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    adopted_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    inventories = iter(
        [
            {
                "disks": [{"disk_id": "disk:8,96"}],
                "volumes": [],
                "current_primary_uuid": adopted_uuid,
            },
            {
                "disks": [{"disk_id": "disk:8,96"}],
                "volumes": [],
                "current_primary_uuid": "",
            },
        ]
    )
    calls: list[tuple[tuple[str, ...], int | None]] = []
    progress_events: list[dict] = []

    async def storage_devices() -> dict:
        return next(inventories)

    async def shell(
        *args: str,
        timeout: int | None = None,
        **_kwargs,
    ) -> CommandResult:
        calls.append((args, timeout))
        return CommandResult(0, "Success", "")

    async def progress(event: dict) -> None:
        progress_events.append(event)

    monkeypatch.setattr(adb, "storage_devices", storage_devices)
    monkeypatch.setattr(adb, "shell", shell)

    result = await adb.switch_primary_storage("", progress=progress)

    assert calls == [
        (("pm", "move-primary-storage", "internal"), 60 * 60),
    ]
    assert result["previous_uuid"] == adopted_uuid
    assert result["target_uuid"] == ""
    assert result["changed"] is True
    assert [event["stage"] for event in progress_events] == [
        "inspecting",
        "migrating",
        "verifying",
        "complete",
    ]


async def test_primary_storage_switch_rejects_unmounted_or_nonphysical_uuid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def storage_devices() -> dict:
        return {
            "disks": [{"disk_id": "disk:8,96"}],
            "volumes": [
                {
                    "volume_id": "private:8,98",
                    "volume_type": "private",
                    "disk_id": "disk:8,112",
                    "state": "mounted",
                    "fs_uuid": "wrong-disk",
                }
            ],
            "current_primary_uuid": "",
        }

    monkeypatch.setattr(adb, "storage_devices", storage_devices)

    with pytest.raises(AdbError, match="not mounted on a detected physical drive"):
        await adb.switch_primary_storage("wrong-disk")


async def test_adoption_rejects_an_existing_unmountable_private_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def storage_devices() -> dict:
        return {
            "disks": [
                {
                    "disk_id": "disk:8,96",
                    "adoptable": True,
                    "volumes": [
                        {
                            "volume_id": "private:8,98",
                            "volume_type": "private",
                            "state": "unmountable",
                            "fs_uuid": None,
                        }
                    ],
                }
            ],
            "volumes": [],
        }

    monkeypatch.setattr(adb, "storage_devices", storage_devices)

    with pytest.raises(AdbError) as raised:
        await adb.adopt_storage(
            "disk:8,96",
            force_adoptable=False,
            migrate_primary=False,
        )

    message = str(raised.value)
    assert "incomplete Android adoption state" in message
    assert "private:8,98 is UNMOUNTABLE" in message
    assert "Reset the drive to portable storage" in message


async def test_unmount_storage_uses_only_detected_physical_volume_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    adopted_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    private_volume = {
        "volume_id": "private:8,98",
        "volume_type": "private",
        "fs_uuid": adopted_uuid,
        "disk_id": "disk:8,96",
        "state": "mounted",
    }
    emulated_volume = {
        "volume_id": "emulated:8,98;0",
        "volume_type": "emulated",
        "fs_uuid": adopted_uuid,
        "disk_id": "disk:8,96",
        "state": "mounted",
    }
    inventories = iter(
        [
            {
                "disks": [
                    {
                        "disk_id": "disk:8,96",
                        "volumes": [private_volume, emulated_volume],
                    }
                ],
                "volumes": [private_volume, emulated_volume],
            },
            {
                "disks": [
                    {
                        "disk_id": "disk:8,96",
                        "volumes": [
                            {**private_volume, "state": "unmounted"},
                            {**emulated_volume, "state": "unmounted"},
                        ],
                    }
                ],
                "volumes": [
                    {**private_volume, "state": "unmounted"},
                    {**emulated_volume, "state": "unmounted"},
                ],
            },
        ]
    )
    calls: list[tuple[str, ...]] = []

    async def storage_devices() -> dict:
        return next(inventories)

    async def shell(*args: str, **_kwargs) -> CommandResult:
        calls.append(args)
        return CommandResult(0, "", "")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(adb, "storage_devices", storage_devices)
    monkeypatch.setattr(adb, "shell", shell)
    monkeypatch.setattr("pixel_relay.adb.asyncio.sleep", no_sleep)

    result = await adb.unmount_storage("disk:8,96")

    assert calls == [("sm", "unmount", "private:8,98")]
    assert result["unmounted_volume_ids"] == ["private:8,98"]


async def test_media_query_preserves_sql_quotes_for_android_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    calls: list[tuple[str, ...]] = []

    async def shell(*args: str, **_kwargs) -> CommandResult:
        calls.append(args)
        if args[0] == "content":
            return CommandResult(0, "No result found.", "")
        return CommandResult(0, "Broadcast completed", "")

    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.scan_media("/sdcard/DCIM/Camera/PixelRelay/item.jpg") is False
    where = calls[1][calls[1].index("--where") + 1]
    assert where == (
        "(_data='/sdcard/DCIM/Camera/PixelRelay/item.jpg' OR "
        "_data='/storage/emulated/0/DCIM/Camera/PixelRelay/item.jpg')"
    )


async def test_media_query_accepts_mediastore_canonical_sdcard_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))

    async def shell(*args: str, **_kwargs) -> CommandResult:
        if args[0] == "content":
            where = args[args.index("--where") + 1]
            assert "/storage/emulated/0/DCIM/Camera/PixelRelay/video.mp4" in where
            return CommandResult(
                0,
                "Row: 0 _id=42, "
                "_data=/storage/emulated/0/DCIM/Camera/PixelRelay/video.mp4, "
                "mime_type=video/mp4",
                "",
            )
        return CommandResult(0, "Broadcast completed", "")

    monkeypatch.setattr(adb, "shell", shell)

    assert await adb.scan_media("/sdcard/DCIM/Camera/PixelRelay/video.mp4") is True


async def test_remote_checksum_uses_large_transfer_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        connection_mode="usb",
        command_timeout_seconds=30,
        push_timeout_seconds=987,
    )
    adb = SafeAdb(settings)
    digest = "a" * 64
    calls: list[tuple[tuple[str, ...], dict]] = []

    async def shell(*args: str, **kwargs) -> CommandResult:
        calls.append((args, kwargs))
        return CommandResult(0, f"{digest}  {args[1]}", "")

    monkeypatch.setattr(adb, "shell", shell)

    assert await adb.remote_sha256("/sdcard/DCIM/Camera/video.mp4") == digest
    assert calls == [
        (
            ("sha256sum", "/sdcard/DCIM/Camera/video.mp4"),
            {"timeout": 987, "check": False},
        )
    ]


async def test_adb_speed_test_verifies_and_removes_disposable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adb = SafeAdb(Settings(data_dir=tmp_path, connection_mode="usb"))
    transferred_source: Path | None = None
    transferred_sha256 = ""
    shell_calls: list[tuple[str, ...]] = []

    async def connect() -> None:
        return None

    async def push(source: Path, destination: str, *_args, **_kwargs) -> None:
        nonlocal transferred_source, transferred_sha256
        transferred_source = source
        transferred_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        assert destination.startswith("/sdcard/Download/.pixel-relay-speedtest-")

    async def remote_sha256(_path: str) -> str:
        return transferred_sha256

    async def shell(*args: str, **_kwargs) -> CommandResult:
        shell_calls.append(args)
        return CommandResult(1 if args[0] == "stat" else 0, "", "")

    monkeypatch.setattr(adb, "connect", connect)
    monkeypatch.setattr(adb, "push", push)
    monkeypatch.setattr(adb, "remote_sha256", remote_sha256)
    monkeypatch.setattr(adb, "shell", shell)

    result = await adb.speed_test(1024**2)

    assert result["connection_mode"] == "usb"
    assert result["size_bytes"] == 1024**2
    assert result["bytes_per_second"] > 0
    assert result["checksum_verified"] is True
    assert result["temporary_files_removed"] is True
    assert transferred_source is not None
    assert not transferred_source.exists()
    assert any(call[0] == "rm" for call in shell_calls)
    assert any(call[0] == "stat" for call in shell_calls)

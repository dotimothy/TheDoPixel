import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("storage adoption API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("supplies the legacy adoption acknowledgement automatically", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      disk_id: "disk:8,96",
      adopted_uuid: "adopted-uuid",
      migrated_primary: false,
      force_adoptable_enabled: false,
      storage: {},
      device: {}
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    await api.adoptStorage("disk:8,96", false, false);

    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(request.body))).toMatchObject({
      disk_id: "disk:8,96",
      acknowledgement: "ERASE disk:8,96",
      force_adoptable: false,
      migrate_primary: false
    });
  });

  it("renders FastAPI validation details as readable text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [{
        type: "missing",
        loc: ["body", "acknowledgement"],
        msg: "Field required"
      }]
    }), {
      status: 422,
      headers: { "Content-Type": "application/json" }
    })));

    await expect(api.adoptStorage("disk:8,96", false, false))
      .rejects.toThrow("acknowledgement: Field required");
  });

  it("unmounts by exact Android disk ID", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      disk_id: "disk:8,96",
      unmounted_volume_ids: ["public:8,97"],
      storage: {},
      device: {}
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    await api.unmountStorage("disk:8,96");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/device/storage/unmount");
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({ disk_id: "disk:8,96" });
  });

  it("runs the ADB speed test on the active connection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      connection_mode: "network",
      serial: "192.168.1.35:5555",
      size_bytes: 33554432,
      duration_seconds: 2,
      bytes_per_second: 16777216,
      megabytes_per_second: 16,
      megabits_per_second: 134.217728,
      checksum_verified: true,
      temporary_files_removed: true
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.adbSpeedTest();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/device/adb-speed-test");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(result.megabytes_per_second).toBe(16);
  });

  it("requires the fixed acknowledgement for a Pixel Relay tree reset", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      destination_root: "/sdcard/DCIM/Camera/PixelRelay",
      batch_count: 0,
      item_count: 0,
      confirmed_batches_purged: 0,
      unconfirmed_batches_cancelled: 0,
      items_purged: 0,
      items_cancelled: 0,
      known_files_deleted: 0,
      known_bytes_deleted: 0
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    await api.cleanSlateStorage();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/device/storage/clean-slate");
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.method).toBe("POST");
    expect(JSON.parse(String(request.body))).toEqual({
      acknowledgement: "DELETE PIXEL RELAY TREE"
    });
  });
});

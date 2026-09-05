import type {
  AuditEntry,
  AdbSpeedTestResult,
  FtpSpeedTestResult,
  Batch,
  BatchPlan,
  BackedUpInventory,
  Dashboard,
  DeviceTelemetry,
  Device,
  PixelStorage,
  QueueSummary,
  RelaySettings,
  ServerDirectoryListing,
  StorageOptions,
  SourceFile,
  SourceRoot,
  StorageAdoptionOperation,
  StoragePrimarySwitchOperation,
  SystemLog,
  User
} from "./types";

let csrfToken = "";

export class ApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function apiErrorDetails(payload: unknown, status: number): { message: string; code?: string } {
  if (!payload || typeof payload !== "object") {
    return { message: `Request failed (${status})` };
  }
  const record = payload as Record<string, unknown>;
  const code = typeof record.code === "string" ? record.code : undefined;
  if (typeof record.message === "string") return { message: record.message, code };
  if (typeof record.detail === "string") return { message: record.detail, code };
  if (Array.isArray(record.detail)) {
    const validationMessages = record.detail.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const issue = item as Record<string, unknown>;
      const location = Array.isArray(issue.loc)
        ? issue.loc.filter((part) => part !== "body").map(String).join(".")
        : "";
      const message = typeof issue.msg === "string" ? issue.msg : "Invalid value";
      return [`${location ? `${location}: ` : ""}${message}`];
    });
    if (validationMessages.length) return { message: validationMessages.join("; "), code };
  }
  return { message: `Request failed (${status})`, code };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method)) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) {
    let payload: unknown = {};
    try {
      payload = await response.json();
    } catch {
      // Preserve the HTTP fallback.
    }
    const error = apiErrorDetails(payload, response.status);
    throw new ApiError(error.message, response.status, error.code);
  }
  return response.json() as Promise<T>;
}

export const api = {
  async me(): Promise<User> {
    const user = await request<User>("/auth/me");
    csrfToken = user.csrf_token;
    return user;
  },
  async login(username: string, password: string): Promise<User> {
    const user = await request<User>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password })
    });
    csrfToken = user.csrf_token;
    return user;
  },
  logout: () => request<{ ok: true }>("/auth/logout", { method: "POST" }),
  shutdownServer: () =>
    request<{ shutdown_requested: true }>("/server/shutdown", { method: "POST" }),
  dashboard: () => request<Dashboard>("/dashboard"),
  stopQueue: () => request<QueueSummary>("/queue/stop", { method: "POST" }),
  startQueue: () => request<QueueSummary>("/queue/start", { method: "POST" }),
  device: () => request<Device>("/device"),
  refreshDevice: () => request<Device>("/device/refresh", { method: "POST" }),
  restartAdbServer: () =>
    request<{
      restarted: boolean;
      stop_returncode: number;
      stop_output?: string | null;
      start_output?: string | null;
      device: Device;
    }>("/device/adb-server/restart", { method: "POST" }),
  adbSpeedTest: () =>
    request<AdbSpeedTestResult>("/device/adb-speed-test", { method: "POST" }),
  ftpConnectionTest: (settings: {
    ftp_host: string;
    ftp_port: number;
    ftp_username: string;
    ftp_password?: string;
    ftp_destination_root: string;
  }) =>
    request<{ connected: true; server: string; destination_root: string }>(
      "/device/ftp-connection-test",
      { method: "POST", body: JSON.stringify(settings) }
    ),
  ftpSpeedTest: (settings: {
    ftp_host: string;
    ftp_port: number;
    ftp_username: string;
    ftp_password?: string;
    ftp_destination_root: string;
  }) =>
    request<FtpSpeedTestResult>("/device/ftp-speed-test", {
      method: "POST",
      body: JSON.stringify(settings)
    }),
  enableAdbOverIp: (port = 5555) =>
    request<{
      enabled: boolean;
      connected: boolean;
      port: number;
      address?: string | null;
      serial?: string | null;
      addresses: string[];
      connection_attempts: {
        serial: string;
        returncode: number;
        output: string;
      }[];
      port_diagnostics: {
        inspection_supported: boolean;
        adb_tcp_port_before_restart?: number | null;
        listeners: {
          name?: string | null;
          pid?: number | null;
          fd?: number | null;
          local_address: string;
          identity_inferred?: boolean;
        }[];
        inspection_error?: string | null;
      };
      device?: Device;
    }>("/device/adb-over-ip", {
      method: "POST",
      body: JSON.stringify({ port })
    }),
  pixelStorage: () => request<PixelStorage>("/device/storage"),
  storageOptions: (refresh = false) =>
    request<StorageOptions>(`/device/storage-options${refresh ? "?refresh=true" : ""}`),
  adoptStorage: (
    diskId: string,
    forceAdoptable: boolean,
    migratePrimary: boolean
  ) =>
    request<StorageAdoptionOperation>("/device/storage/adopt", {
      method: "POST",
      body: JSON.stringify({
        disk_id: diskId,
        // Older running backends still validate this field. Supplying it here
        // preserves compatibility without requiring the operator to type it.
        acknowledgement: `ERASE ${diskId}`,
        force_adoptable: forceAdoptable,
        migrate_primary: migratePrimary
      })
    }),
  storageAdoption: () =>
    request<{ operation: StorageAdoptionOperation | null }>("/device/storage/adoption"),
  dismissStorageAdoption: () =>
    request<{ dismissed: true }>("/device/storage/adoption/dismiss", { method: "POST" }),
  switchPrimaryStorage: (targetUuid: string) =>
    request<StoragePrimarySwitchOperation>("/device/storage/primary-switch", {
      method: "POST",
      body: JSON.stringify({ target_uuid: targetUuid })
    }),
  storagePrimarySwitch: () =>
    request<{ operation: StoragePrimarySwitchOperation | null }>(
      "/device/storage/primary-switch"
    ),
  dismissStoragePrimarySwitch: () =>
    request<{ dismissed: true }>("/device/storage/primary-switch/dismiss", {
      method: "POST"
    }),
  unmountStorage: (diskId: string) =>
    request<{
      disk_id: string;
      unmounted_volume_ids: string[];
      storage: unknown;
      device: Device;
    }>("/device/storage/unmount", {
      method: "POST",
      body: JSON.stringify({ disk_id: diskId })
    }),
  purgeStorageOrphans: (paths: string[]) =>
    request<{ deleted: string[]; deleted_count: number }>("/device/storage/orphans/purge", {
      method: "POST",
      body: JSON.stringify({ paths })
    }),
  freePixelSpace: () =>
    request<{ deleted: string[]; deleted_count: number; directories_checked: number; cache_trim_supported: boolean; cache_trimmed: boolean; free_before_bytes?: number | null; free_after_bytes?: number | null; reclaimed_bytes?: number | null; tracked_files_deleted: number }>("/device/storage/free-space", {
      method: "POST"
    }),
  cleanSlateStorage: () =>
    request<{
      destination_root: string;
      batch_count: number;
      item_count: number;
      confirmed_batches_purged: number;
      unconfirmed_batches_cancelled: number;
      items_purged: number;
      items_cancelled: number;
      known_files_deleted: number;
      known_bytes_deleted: number;
    }>("/device/storage/clean-slate", {
      method: "POST",
      body: JSON.stringify({ acknowledgement: "DELETE PIXEL RELAY TREE" })
    }),
  serverDirectories: (path?: string) =>
    request<ServerDirectoryListing>(
      `/system/directories${path ? `?path=${encodeURIComponent(path)}` : ""}`
    ),
  sources: () => request<SourceRoot[]>("/sources"),
  createSource: (name: string, path: string) =>
    request<SourceRoot>("/sources", { method: "POST", body: JSON.stringify({ name, path }) }),
  removeSource: (id: number) =>
    request<{
      id: number;
      name: string;
      path: string;
      discovered_records_retained: number;
      originals_deleted: false;
    }>(`/sources/${id}`, { method: "DELETE" }),
  scanSource: (id: number, fullVerify = false) =>
    request<{
      files: SourceFile[];
      skipped: { path: string; reason: string }[];
      stats: {
        examined: number;
        candidates: number;
        cached: number;
        hashed: number;
        hash_workers: number;
        full_verify: boolean;
      };
    }>(`/sources/${id}/scan`, {
      method: "POST",
      body: JSON.stringify({ paths: null, full_verify: fullVerify })
    }),
  // Sources & Imports needs both ready and active files so its totals can
  // explain the difference between scan discoveries and batchable media.
  files: () => request<SourceFile[]>("/files?unbatched_only=false"),
  upload: (file: File) => {
    const form = new FormData();
    form.append("media", file);
    return request<SourceFile>("/uploads", { method: "POST", body: form });
  },
  batches: () => request<Batch[]>("/batches"),
  backedUpItems: (limit = 250, offset = 0) =>
    request<BackedUpInventory>(`/backups/items?limit=${limit}&offset=${offset}`),
  batch: (id: string) => request<Batch>(`/batches/${id}`),
  deleteBatch: (id: string) =>
    request<{ id: string; name: string; file_count: number }>(`/batches/${id}`, {
      method: "DELETE"
    }),
  createBatch: (name: string | undefined, fileIds: number[]) =>
    request<Batch[]>("/batches", {
      method: "POST",
      body: JSON.stringify({
        ...(name ? { name } : {}),
        file_ids: fileIds
      })
    }),
  planBatch: (name: string | undefined, fileIds: number[]) =>
    request<BatchPlan>("/batches/plan", {
      method: "POST",
      body: JSON.stringify({
        ...(name ? { name } : {}),
        file_ids: fileIds
      })
    }),
  telemetry: (hours = 24, maxPoints = 240) =>
    request<DeviceTelemetry>(`/device/telemetry?hours=${hours}&max_points=${maxPoints}`),
  retryBatch: (id: string, includePurgeFailures = true) =>
    request<{ retried: number }>(`/batches/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ include_purge_failures: includePurgeFailures })
    }),
  retriggerBatch: (id: string) =>
    request<Batch>(`/batches/${id}/retrigger`, { method: "POST" }),
  pauseBatch: (id: string) =>
    request<Batch>(`/batches/${id}/pause`, { method: "POST" }),
  resumeBatch: (id: string) =>
    request<Batch>(`/batches/${id}/resume`, { method: "POST" }),
  cancelBatch: (id: string) =>
    request<Batch>(`/batches/${id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ acknowledgement: "CANCEL BATCH" })
    }),
  skipStalledBatch: (id: string) =>
    request<Batch>(`/batches/${id}/skip`, { method: "POST" }),
  confirmBatch: (id: string) =>
    request<Batch>(`/batches/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ acknowledgement: "I verified this batch in Google Photos" })
    }),
  purgeBatch: (id: string) =>
    request<Batch>(`/batches/${id}/purge`, {
      method: "POST"
    }),
  audit: () => request<AuditEntry[]>("/audit"),
  logs: (limit = 500) => request<SystemLog[]>(`/logs?limit=${limit}`),
  settings: () => request<RelaySettings>("/settings"),
  updateApp: () => request<{ updated: boolean; restarting: boolean; message: string }>("/app/update", { method: "POST" }),
  updateSettings: (payload: {
    device_serial?: string;
    expected_primary_uuid?: string;
    connection_mode?: "network" | "usb" | "ftp";
    ftp_host?: string;
    ftp_port?: number;
    ftp_username?: string;
    ftp_password?: string;
    ftp_destination_root?: string;
    destination_root?: string;
    import_root?: string;
    max_batch_files?: number;
    max_batch_bytes?: number;
    reserve_bytes?: number;
    pause_temperature_c?: number;
    resume_temperature_c?: number;
  }) => request<Partial<RelaySettings>>("/settings", {
      method: "PATCH",
      body: JSON.stringify(payload)
    })
};

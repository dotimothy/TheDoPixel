export function bytes(value?: number | null): string {
  if (value === undefined || value === null) return "—";
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
}

export function storageUtilization(
  totalBytes?: number | null,
  freeBytes?: number | null,
  usedBytes?: number | null
) {
  if (!Number.isFinite(totalBytes) || (totalBytes ?? 0) <= 0) return null;
  const total = totalBytes as number;
  const availableFree = Number.isFinite(freeBytes)
    ? freeBytes as number
    : Number.isFinite(usedBytes)
      ? total - (usedBytes as number)
      : null;
  if (availableFree == null) return null;
  const free = Math.min(total, Math.max(0, availableFree));
  const used = total - free;
  return {
    totalBytes: total,
    freeBytes: free,
    usedBytes: used,
    utilizedPercent: Math.round((used / total) * 1000) / 10
  };
}

export function relativeTime(value?: string | null): string {
  if (!value) return "Never";
  const delta = new Date(value).getTime() - Date.now();
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const minutes = Math.round(delta / 60_000);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

export function dateTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function duration(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return "Calculating…";
  if (seconds <= 0) return "Done";
  if (seconds < 60) return "<1 min";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) return remainingMinutes ? `${hours} hr ${remainingMinutes} min` : `${hours} hr`;
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return remainingHours ? `${days} d ${remainingHours} hr` : `${days} d`;
}

export function label(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function shortPath(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/");
  return parts.length > 4 ? `…/${parts.slice(-3).join("/")}` : path;
}

export function parentFolderName(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 2] : "root";
}

export function pathBaseName(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts.at(-1) || "";
}

export function googlePhotosDateSearch(
  items: { mtime_ns?: number }[]
): { href: string; dateLabel: string } | null {
  const dates = items
    .map((item) => item.mtime_ns ? new Date(item.mtime_ns / 1_000_000) : null)
    .filter((date): date is Date => Boolean(date && Number.isFinite(date.getTime())))
    .sort((left, right) => left.getTime() - right.getTime());
  if (!dates.length) return null;

  const formatDate = (date: Date) => date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric"
  });
  const first = formatDate(dates[0]);
  const last = formatDate(dates[dates.length - 1]);
  const dateLabel = first === last ? first : `${first}–${last}`;
  const query = first === last ? first : `from ${first} to ${last}`;
  return {
    href: `https://photos.google.com/search/${encodeURIComponent(query)}`,
    dateLabel
  };
}

export function googlePhotosBatchDateSearch(
  batchId: string,
  items: {
    batch_id: string;
    media_kind: "photo" | "video";
    mtime_ns?: number;
  }[]
): { href: string; dateLabel: string } | null {
  const subBatchItems = items.filter((item) => item.batch_id === batchId);
  const photos = subBatchItems.filter((item) => item.media_kind === "photo");
  return googlePhotosDateSearch(photos.length ? photos : subBatchItems);
}

export interface ProgressSegment {
  kind: "queued" | "transferring" | "staged" | "verification" | "complete" | "attention" | "cancelled";
  count: number;
}

const progressWeights: Record<string, number> = {
  discovered: 0,
  queued: 0,
  transferring: 0.45,
  staged_on_pixel: 0.75,
  awaiting_backup_confirmation: 1,
  confirmed_backed_up: 1,
  purged_from_pixel: 1,
  transfer_failed: 0.2,
  media_scan_failed: 0.75,
  device_offline: 0.05,
  storage_missing: 0.05,
  temperature_paused: 0.05,
  purge_failed: 0.95,
  cancelled: 0,
  cancelled_on_pixel: 0.75
};

const progressGroups: ProgressSegment["kind"][] = [
  "queued",
  "transferring",
  "staged",
  "verification",
  "complete",
  "attention",
  "cancelled"
];

function progressGroup(state: string): ProgressSegment["kind"] {
  if (["discovered", "queued"].includes(state)) return "queued";
  if (state === "transferring") return "transferring";
  if (state === "staged_on_pixel") return "staged";
  if (state === "awaiting_backup_confirmation") return "verification";
  if (["confirmed_backed_up", "purged_from_pixel"].includes(state)) return "complete";
  if (["cancelled", "cancelled_on_pixel"].includes(state)) return "cancelled";
  return "attention";
}

export function batchProgress(
  states: Partial<Record<string, number>>,
  totalOverride?: number
) {
  const entries = Object.entries(states);
  const countedTotal = entries.reduce((sum, [, count]) => sum + (count || 0), 0);
  const total = totalOverride || countedTotal;
  const weightedItems = entries.reduce(
    (sum, [state, count]) => sum + (count || 0) * (progressWeights[state] ?? 0),
    0
  );
  const grouped = new Map<ProgressSegment["kind"], number>();
  for (const [state, count] of entries) {
    const kind = progressGroup(state);
    grouped.set(kind, (grouped.get(kind) || 0) + (count || 0));
  }
  const segments = progressGroups
    .map((kind) => ({ kind, count: grouped.get(kind) || 0 }))
    .filter((segment) => segment.count > 0);
  const ready = (states.awaiting_backup_confirmation || 0)
    + (states.confirmed_backed_up || 0)
    + (states.purged_from_pixel || 0);
  return {
    total,
    ready,
    percent: total ? Math.round((weightedItems / total) * 1000) / 10 : 0,
    segments
  };
}

const completedMediaScanStates = [
  "awaiting_backup_confirmation",
  "confirmed_backed_up",
  "purged_from_pixel",
  "media_scan_failed",
  "purge_failed"
];

export function mediaScanProgress(
  states: Partial<Record<string, number>>,
  totalOverride?: number
) {
  const total = totalOverride
    || Object.values(states).reduce<number>((sum, count) => sum + (count || 0), 0);
  const completed = completedMediaScanStates.reduce(
    (sum, state) => sum + (states[state] || 0),
    0
  );
  const scanning = states.staged_on_pixel || 0;
  return {
    completed,
    scanning,
    total,
    percent: total ? Math.round((completed / total) * 1000) / 10 : 0
  };
}

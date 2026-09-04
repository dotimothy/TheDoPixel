export type ItemState =
  | "discovered"
  | "queued"
  | "transferring"
  | "staged_on_pixel"
  | "awaiting_backup_confirmation"
  | "confirmed_backed_up"
  | "purged_from_pixel"
  | "cancelled"
  | "cancelled_on_pixel"
  | "transfer_failed"
  | "media_scan_failed"
  | "device_offline"
  | "storage_missing"
  | "temperature_paused"
  | "purge_failed";

export interface User {
  id: number;
  username: string;
  csrf_token: string;
}

export interface Device {
  state: string;
  serial: string;
  connection_mode?: string | null;
  model?: string | null;
  android_version?: string | null;
  battery_level?: number | null;
  temperature_c?: number | null;
  charging?: boolean | null;
  ethernet?: boolean | null;
  network_type?: string | null;
  network_interface?: string | null;
  network_addresses?: string[] | null;
  network_gateway?: string | null;
  network_dns_servers?: string[] | null;
  network_ssid?: string | null;
  network_validated?: boolean | null;
  network_metered?: boolean | null;
  storage_total_bytes?: number | null;
  storage_used_bytes?: number | null;
  storage_free_bytes?: number | null;
  internal_storage_total_bytes?: number | null;
  internal_storage_free_bytes?: number | null;
  primary_storage_uuid?: string | null;
  expected_primary_uuid?: string | null;
  storage_ready?: boolean | null;
  photos_installed?: boolean | null;
  photos_enabled?: boolean | null;
  photos_running?: boolean | null;
  photos_version?: string | null;
  error?: string | null;
  observed_at?: string | null;
}

export interface PixelStorageFile {
  path: string;
  allocated_bytes: number;
  tracked: boolean;
  state?: ItemState | null;
  batch_id?: string | null;
  batch_name?: string | null;
}

export interface PixelStorage {
  destination_root: string;
  connection_mode: "usb" | "network" | "ftp";
  storage_total_bytes?: number | null;
  storage_free_bytes?: number | null;
  relay_allocated_bytes: number;
  tracked_count: number;
  orphan_count: number;
  files: PixelStorageFile[];
}

export interface AdbSpeedTestResult {
  connection_mode: "usb" | "network";
  serial: string;
  size_bytes: number;
  duration_seconds: number;
  bytes_per_second: number;
  megabytes_per_second: number;
  megabits_per_second: number;
  checksum_verified: boolean;
  temporary_files_removed: boolean;
}

export interface FtpSpeedTestResult {
  connection_mode: "ftp";
  server: string;
  size_bytes: number;
  upload_duration_seconds: number;
  upload_bytes_per_second: number;
  upload_megabytes_per_second: number;
  upload_megabits_per_second: number;
  verification_duration_seconds: number;
  verification_bytes_per_second: number;
  verified_duration_seconds: number;
  verified_bytes_per_second: number;
  checksum_verified: boolean;
  temporary_files_removed: boolean;
}

export interface StorageOption {
  id: string;
  uuid: string;
  label: string;
  kind: "internal" | "adopted" | "portable" | "unavailable";
  state: string;
  current: boolean;
  configured: boolean;
  selectable: boolean;
  volume_ids: string[];
  total_bytes?: number | null;
  free_bytes?: number | null;
  description: string;
  disk_id?: string | null;
}

export interface StorageVolumeDetail {
  volume_id: string;
  volume_type?: string | null;
  disk_id?: string | null;
  state?: string | null;
  fs_type?: string | null;
  fs_uuid?: string | null;
  fs_label?: string | null;
  path?: string | null;
}

export interface StorageMedium {
  disk_id: string;
  flags: string[];
  adoptable: boolean;
  default_primary: boolean;
  usb: boolean;
  sd: boolean;
  size_bytes?: number | null;
  label?: string | null;
  volume_ids: string[];
  sys_path?: string | null;
  volumes: StorageVolumeDetail[];
  ignored_reason?: "empty_usb_bridge" | string;
}

export interface StorageOptions {
  device_state: string;
  connection_mode: "usb" | "network" | "ftp";
  current_primary_uuid: string;
  configured_uuid: string;
  disks: string[];
  media: StorageMedium[];
  ignored_media: StorageMedium[];
  media_error?: string | null;
  details_supported: boolean;
  options: StorageOption[];
  observed_at?: string | null;
}

export interface StorageAdoptionProgress {
  action: "adoption_progress";
  operation_id: string;
  disk_id: string;
  stage: string;
  message: string;
  step: number;
  step_count: number;
  percent: number;
  complete: boolean;
  failed: boolean;
}

export interface StorageAdoptionOperation {
  operation_id: string;
  disk_id: string;
  status: "running" | "completed" | "failed";
  force_adoptable: boolean;
  migrate_primary: boolean;
  started_at: string;
  finished_at?: string | null;
  progress: StorageAdoptionProgress;
  result?: {
    adopted_uuid: string;
    migrated_primary: boolean;
    migration_error?: string | null;
    force_adoptable_enabled: boolean;
  } | null;
  error?: string | null;
}

export interface StoragePrimarySwitchProgress {
  action: "primary_switch_progress";
  operation_id: string;
  target_uuid: string;
  stage: string;
  message: string;
  step: number;
  step_count: number;
  percent: number;
  complete: boolean;
  failed: boolean;
}

export interface StoragePrimarySwitchOperation {
  operation_id: string;
  target_uuid: string;
  status: "running" | "completed" | "failed";
  started_at: string;
  finished_at?: string | null;
  progress: StoragePrimarySwitchProgress;
  result?: {
    previous_uuid: string;
    target_uuid: string;
    changed: boolean;
    destination_root: string;
    device: Device;
  } | null;
  error?: string | null;
}

export interface ServerDirectoryListing {
  path: string;
  parent?: string | null;
  entries: { name: string; path: string }[];
  shortcuts: { name: string; path: string }[];
}

export interface SourceRoot {
  id: number;
  name: string;
  path: string;
  enabled: boolean;
  available: boolean;
  issue_code?: "permission_denied" | "unavailable" | null;
  issue?: string | null;
  created_at: string;
}

export interface SourceFile {
  id: number;
  root_id: number;
  root_name: string;
  path: string;
  sha256: string;
  size: number;
  extension: string;
  media_kind: "photo" | "video";
  discovered_at: string;
  duplicate_content: boolean;
  active_item_count?: number;
  previous_batch_count?: number;
  previously_confirmed?: boolean;
  previously_purged?: boolean;
}

export interface BatchPerformance {
  sample_count: number;
  transferred_bytes: number;
  transfer_seconds: number;
  transfer_rate_bytes_per_second?: number | null;
  scanned_count: number;
  scan_seconds: number;
  average_scan_seconds?: number | null;
}

export interface BatchPlanPart {
  name: string;
  folder: string;
  file_ids: number[];
  file_count: number;
  total_bytes: number;
  photo_count: number;
  raw_count: number;
  video_count: number;
}

export interface BatchPlan {
  selected_count: number;
  unique_content_count: number;
  duplicate_selection_count: number;
  total_bytes: number;
  photo_count: number;
  raw_count: number;
  video_count: number;
  source_count: number;
  folder_count: number;
  batch_count: number;
  batch_byte_limit: number;
  split_reason?: "pixel_storage" | "configured_limits" | "source_folders" | null;
  previously_processed_count: number;
  previously_confirmed_count: number;
  previously_purged_count: number;
  estimated_seconds?: number | null;
  performance_basis: BatchPerformance;
  storage_free_bytes?: number | null;
  storage_total_bytes?: number | null;
  storage_reserve_bytes: number;
  parts: BatchPlanPart[];
}

export interface BatchItem {
  id: string;
  batch_id: string;
  state: ItemState;
  path: string;
  remote_path: string;
  sha256: string;
  size: number;
  mtime_ns?: number;
  media_kind: "photo" | "video";
  attempts: number;
  transfer_bytes?: number;
  transfer_total_bytes?: number;
  transfer_updated_at?: string | null;
  error_code?: string;
  error_detail?: string;
  updated_at: string;
}

export interface Batch {
  id: string;
  name: string;
  created_at: string;
  confirmed_at?: string;
  purged_at?: string;
  cancelled_at?: string;
  paused_at?: string | null;
  paused_by?: number | null;
  total_paused_seconds?: number;
  processing?: boolean;
  series_id?: string | null;
  series_index?: number | null;
  series_total?: number | null;
  planned_capacity_bytes?: number | null;
  split_reason?: "pixel_storage" | "configured_limits" | "source_folders" | null;
  series_blocked?: boolean;
  storage_blocked?: boolean;
  storage_available_bytes?: number | null;
  processing_started_at?: string | null;
  transfer_rate_bytes_per_second?: number | null;
  eta_seconds?: number | null;
  last_activity_at?: string | null;
  stalled?: boolean;
  stalled_for_seconds?: number | null;
  stall_reason?: string | null;
  performance?: BatchPerformance;
  file_count?: number;
  photo_count?: number;
  raw_count?: number;
  video_count?: number;
  total_bytes: number;
  transfer_bytes?: number;
  states: Partial<Record<ItemState, number>>;
  items?: BatchItem[];
}

export interface BackedUpItem {
  id: string;
  state: "confirmed_backed_up" | "purged_from_pixel";
  remote_path: string;
  updated_at: string;
  batch_id: string;
  batch_name: string;
  confirmed_at: string;
  first_confirmed_at: string;
  latest_confirmed_at: string;
  confirmation_count: number;
  retained_copy_count: number;
  purged_copy_count: number;
  purged_at?: string | null;
  source_file_id: number;
  path: string;
  sha256: string;
  size: number;
  mtime_ns?: number;
  extension: string;
  media_kind: "photo" | "video";
}

export interface BackedUpInventory {
  total: number;
  total_bytes: number;
  photo_count: number;
  raw_count: number;
  video_count: number;
  retained_on_pixel_count: number;
  purged_from_pixel_count: number;
  uploaded_total?: number;
  uploaded_total_bytes?: number;
  uploaded_photo_count?: number;
  uploaded_raw_count?: number;
  uploaded_video_count?: number;
  awaiting_verification_count?: number;
  awaiting_verification_bytes?: number;
  limit: number;
  offset: number;
  items: BackedUpItem[];
}

export interface QueueSummary {
  states: Partial<Record<ItemState, number>>;
  last_confirmed_upload?: { id: string; name: string; confirmed_at: string };
  mode?: "running" | "draining" | "stopped";
  running?: boolean;
  drain_requested?: boolean;
  stopped?: boolean;
  drain_batch_id?: string | null;
  active_batch_id?: string | null;
}

export interface Dashboard {
  device: Device;
  queue: QueueSummary;
  active_batch?: Batch | null;
  batches: Batch[];
  sources: SourceRoot[];
}

export interface DeviceTelemetryPoint {
  observed_at: string;
  state?: string | null;
  battery_level?: number | null;
  temperature_c?: number | null;
  charging?: boolean | null;
  storage_total_bytes?: number | null;
  storage_used_bytes?: number | null;
  storage_free_bytes?: number | null;
}

export interface TelemetryMetricSummary {
  minimum?: number | null;
  maximum?: number | null;
  average?: number | null;
  latest?: number | null;
}

export interface DeviceTelemetry {
  hours: number;
  sample_count: number;
  points: DeviceTelemetryPoint[];
  summary: {
    battery_level: TelemetryMetricSummary;
    temperature_c: TelemetryMetricSummary;
    storage_free_bytes: TelemetryMetricSummary;
  };
}

export interface AuditEntry {
  id: number;
  username?: string;
  action: string;
  target_type: string;
  target_id?: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface SystemLog {
  timestamp?: string;
  level: string;
  logger: string;
  message: string;
  exception?: string;
  context?: Record<string, unknown>;
}

export interface RelaySettings {
  device_serial: string;
  connection_mode: "network" | "usb" | "ftp";
  ftp_host: string;
  ftp_port: number;
  ftp_username: string;
  ftp_password_configured: boolean;
  ftp_destination_root: string;
  expected_primary_uuid: string;
  destination_root: string;
  import_root?: string | null;
  max_batch_files: number;
  max_batch_bytes: number;
  reserve_bytes: number;
  pixel_internal_storage_bytes?: number | null;
  pause_temperature_c: number;
  resume_temperature_c: number;
  allowed_extensions: string[];
}

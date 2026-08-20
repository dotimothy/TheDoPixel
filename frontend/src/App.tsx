import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { batchProgress, bytes, dateTime, duration, googlePhotosBatchDateSearch, label, mediaScanProgress, parentFolderName, pathBaseName, relativeTime, shortPath, storageUtilization } from "./format";
import { Icons } from "./Icons";
import type {
  AuditEntry,
  AdbSpeedTestResult,
  BackedUpInventory,
  BackedUpItem,
  Batch,
  BatchItem,
  BatchPlan,
  Dashboard,
  DeviceTelemetry,
  DeviceTelemetryPoint,
  FtpSpeedTestResult,
  ItemState,
  PixelStorage,
  QueueSummary,
  RelaySettings,
  ServerDirectoryListing,
  SourceFile,
  SourceRoot,
  StorageAdoptionOperation,
  StorageAdoptionProgress,
  StorageMedium,
  StorageOptions,
  StoragePrimarySwitchOperation,
  StoragePrimarySwitchProgress,
  SystemLog,
  User
} from "./types";

type Tab = "overview" | "batches" | "sources" | "audit" | "settings";
type BatchFilter = "all" | "processing" | "ready" | "confirmed" | "attention" | "cancelled";
type Notice = { type: "good" | "bad"; message: string };
type InstallChoice = { outcome: "accepted" | "dismissed"; platform: string };
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<InstallChoice>;
}
type ScanProgress = {
  root_id: number;
  phase?: "enumerating" | "hashing" | "saving" | "complete";
  processed: number;
  total: number;
  examined?: number;
  discovered: number;
  skipped: number;
  cached?: number;
  hashed?: number;
  full_verify?: boolean;
  current_name?: string | null;
  complete: boolean;
  failed: boolean;
  message?: string;
  issues?: { path: string; reason: string }[];
};
const nav: { id: Tab; label: string; icon: typeof Icons.pulse }[] = [
  { id: "overview", label: "Overview", icon: Icons.pulse },
  { id: "batches", label: "Batches", icon: Icons.batches },
  { id: "sources", label: "Sources", icon: Icons.source },
  { id: "audit", label: "Audit log", icon: Icons.audit },
  { id: "settings", label: "Settings", icon: Icons.settings }
];

const problemStates: ItemState[] = [
  "transfer_failed",
  "media_scan_failed",
  "device_offline",
  "storage_missing",
  "temperature_paused",
  "purge_failed"
];

const rawExtensions = new Set([
  ".3fr", ".arw", ".cr2", ".cr3", ".dng", ".erf", ".iiq", ".mef", ".mos",
  ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl",
  ".sr2", ".srf", ".x3f"
]);
const sourceFileRenderChunk = 250;

async function serverIsUnavailable(): Promise<boolean> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await fetch("/api/v1/health", { cache: "no-store" });
      return false;
    } catch {
      if (attempt < 2) {
        await new Promise((resolve) => window.setTimeout(resolve, 600));
      }
    }
  }
  return true;
}

function isRaw(file: Pick<SourceFile, "extension">): boolean {
  return rawExtensions.has(file.extension.toLowerCase());
}

function ipv4First(addresses: string[] | null | undefined): string[] {
  return [...(addresses || [])].sort(
    (left, right) => Number(left.includes(":")) - Number(right.includes(":"))
  );
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const temporary = document.createElement("textarea");
  temporary.value = value;
  temporary.setAttribute("readonly", "");
  temporary.style.position = "fixed";
  temporary.style.opacity = "0";
  document.body.appendChild(temporary);
  temporary.select();
  const copied = document.execCommand("copy");
  temporary.remove();
  if (!copied) throw new Error("Browser clipboard access is unavailable");
}

function CopyButton({
  text,
  label: buttonLabel = "Copy error",
  className = ""
}: {
  text: string;
  label?: string;
  className?: string;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  async function copy() {
    try {
      await copyText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    window.setTimeout(() => setCopyState("idle"), 1800);
  }
  return <button type="button" className={`copy-error ${className}`} onClick={() => void copy()}>
    {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : buttonLabel}
  </button>;
}

function isBatchReady(batch: Batch): boolean {
  return Boolean(
    batch.file_count
    && batch.states.awaiting_backup_confirmation === batch.file_count
  );
}

function batchEta(batch: Batch): string {
  const total = batch.file_count || Object.values(batch.states).reduce(
    (sum, count) => sum + (count || 0),
    0
  );
  const complete = total > 0 && (
    (batch.states.awaiting_backup_confirmation || 0)
    + (batch.states.confirmed_backed_up || 0)
    + (batch.states.purged_from_pixel || 0)
  ) === total;
  if (complete) return "Processing complete";
  if (batch.cancelled_at) return "Cancelled";
  if (batch.paused_at) return batch.processing ? "Pausing after current file…" : "Paused by operator";
  if (batch.series_blocked) return "Waiting for prior part";
  if (batch.storage_blocked) return "Waiting for Pixel space";
  if (
    batch.states.device_offline
    || batch.states.storage_missing
    || batch.states.temperature_paused
  ) return "ETA paused";
  if (
    batch.states.transfer_failed
    || batch.states.media_scan_failed
    || batch.states.purge_failed
  ) return "ETA needs attention";
  if ((batch.transfer_bytes || 0) >= batch.total_bytes && batch.states.staged_on_pixel) {
    return "Finishing media scan";
  }
  if (batch.eta_seconds) {
    const rate = batch.transfer_rate_bytes_per_second
      ? ` at ${bytes(batch.transfer_rate_bytes_per_second)}/s`
      : "";
    return `ETA ~${duration(batch.eta_seconds)}${rate}`;
  }
  return batch.processing_started_at ? "ETA calculating…" : "Waiting to start";
}

function matchesBatchFilter(batch: Batch, filter: BatchFilter): boolean {
  if (filter === "all") return true;
  if (filter === "cancelled") return Boolean(batch.cancelled_at);
  if (filter === "confirmed") return Boolean(batch.confirmed_at);
  if (filter === "ready") return isBatchReady(batch) && !batch.confirmed_at;
  const hasAttention = Object.keys(batch.states).some(
    (state) => problemStates.includes(state as ItemState)
  );
  if (filter === "attention") return hasAttention;
  return !batch.cancelled_at && !batch.confirmed_at && !isBatchReady(batch) && !hasAttention;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const query = new URLSearchParams(window.location.search);
  const requestedTab = query.get("tab");
  const [tab, setTab] = useState<Tab>(
    nav.some(({ id }) => id === requestedTab)
      ? requestedTab as Tab
      : "overview"
  );
  const [requestedBatchId, setRequestedBatchId] = useState<string | null>(
    query.get("batch")
  );
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [serverStopped, setServerStopped] = useState(false);
  const [fullscreen, setFullscreen] = useState(Boolean(document.fullscreenElement));
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(
    window.matchMedia("(display-mode: standalone)").matches ||
    Boolean((navigator as Navigator & { standalone?: boolean }).standalone)
  );
  const [notificationPermission, setNotificationPermission] = useState<
    NotificationPermission | "unsupported"
  >("Notification" in window ? Notification.permission : "unsupported");
  const [showNotificationPrompt, setShowNotificationPrompt] = useState(false);
  const readyBatches = useRef<Set<string> | null>(null);
  const stalledBatches = useRef<Set<string> | null>(null);

  const refreshDashboard = useCallback(async () => {
    try {
      const next = await api.dashboard();
      const nextReady = new Set(
        next.batches.filter(isBatchReady).map((batch) => batch.id)
      );
      const nextStalled = new Set(
        next.batches.filter((batch) => batch.stalled).map((batch) => batch.id)
      );
      if (readyBatches.current) {
        for (const batch of next.batches) {
          if (!nextReady.has(batch.id) || readyBatches.current.has(batch.id)) continue;
          const part = batch.series_total
            ? `Part ${batch.series_index} of ${batch.series_total}`
            : "Batch";
          const body = `${part} finished processing and is ready to verify in Google Photos.`;
          if ("Notification" in window && Notification.permission === "granted") {
            void navigator.serviceWorker?.getRegistration()
              .then((registration) => {
                if (registration) {
                  return registration.showNotification(`${batch.name} is ready`, {
                    body,
                    icon: "/icons/app-icon-192.png",
                    badge: "/icons/app-icon-192.png",
                    tag: `batch-ready-${batch.id}`,
                    data: { url: "/?tab=batches" }
                  });
                }
                const notification = new Notification(`${batch.name} is ready`, {
                  body,
                  icon: "/icons/app-icon-192.png",
                  tag: `batch-ready-${batch.id}`
                });
                notification.onclick = () => {
                  window.focus();
                  window.location.assign("/?tab=batches");
                  notification.close();
                };
              })
              .catch(() => setNotice({ type: "good", message: `${batch.name} is ready to verify` }));
          } else {
            setNotice({ type: "good", message: `${batch.name} is ready to verify` });
          }
        }
      }
      if (stalledBatches.current) {
        for (const batch of next.batches) {
          if (!nextStalled.has(batch.id) || stalledBatches.current.has(batch.id)) continue;
          const body = batch.stall_reason || "No batch progress has been recorded recently.";
          if ("Notification" in window && Notification.permission === "granted") {
            void navigator.serviceWorker?.getRegistration()
              .then((registration) => registration?.showNotification(`${batch.name} may be stalled`, {
                body,
                icon: "/icons/app-icon-192.png",
                badge: "/icons/app-icon-192.png",
                tag: `batch-stalled-${batch.id}`,
                data: { url: `/?tab=batches&batch=${batch.id}` }
              }))
              .catch(() => setNotice({ type: "bad", message: `${batch.name} may be stalled` }));
          } else {
            setNotice({ type: "bad", message: `${batch.name} may be stalled` });
          }
        }
      }
      readyBatches.current = nextReady;
      stalledBatches.current = nextStalled;
      setDashboard(next);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setUser(null);
    }
  }, []);

  useEffect(() => {
    api.me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!user) return;
    void refreshDashboard();
    const stream = new EventSource("/api/v1/events");
    let refreshTimer = 0;
    let checkingServer = false;
    let disposed = false;
    stream.onmessage = () => {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => void refreshDashboard(), 250);
    };
    ["device", "queue", "batch", "source", "setting"].forEach((kind) =>
      stream.addEventListener(kind, () => {
        window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => void refreshDashboard(), 250);
      })
    );
    stream.addEventListener("server", () => {
      disposed = true;
      stream.close();
      window.close();
      setServerStopped(true);
    });
    stream.onerror = () => {
      if (checkingServer) return;
      checkingServer = true;
      void serverIsUnavailable()
        .then((unavailable) => {
          if (!unavailable || disposed) return;
          window.close();
          setServerStopped(true);
        })
        .finally(() => {
          checkingServer = false;
        });
    };
    return () => {
      disposed = true;
      window.clearTimeout(refreshTimer);
      stream.close();
    };
  }, [user, refreshDashboard]);

  useEffect(() => {
    if (
      user
      && notificationPermission === "default"
      && sessionStorage.getItem("pixel-relay-notification-prompt-dismissed") !== "true"
    ) {
      setShowNotificationPrompt(true);
    } else {
      setShowNotificationPrompt(false);
    }
  }, [user, notificationPermission]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(
      () => setNotice(null),
      notice.type === "bad" ? 30_000 : 4500
    );
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    const updateFullscreen = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", updateFullscreen);
    return () => document.removeEventListener("fullscreenchange", updateFullscreen);
  }, []);

  useEffect(() => {
    const displayMode = window.matchMedia("(display-mode: standalone)");
    const capturePrompt = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event as BeforeInstallPromptEvent);
    };
    const markInstalled = () => {
      setInstallPrompt(null);
      setInstalled(true);
      setNotice({ type: "good", message: "TheDoPixel shortcut was added" });
    };
    const syncDisplayMode = () => setInstalled(displayMode.matches);

    window.addEventListener("beforeinstallprompt", capturePrompt);
    window.addEventListener("appinstalled", markInstalled);
    displayMode.addEventListener("change", syncDisplayMode);
    return () => {
      window.removeEventListener("beforeinstallprompt", capturePrompt);
      window.removeEventListener("appinstalled", markInstalled);
      displayMode.removeEventListener("change", syncDisplayMode);
    };
  }, []);

  if (serverStopped) return <ServerStoppedScreen />;
  if (checking) return <BootScreen />;
  if (!user) return <Login onLogin={setUser} />;

  const report = (message: string, type: Notice["type"] = "good") => setNotice({ message, type });
  function openBatch(batchId: string) {
    setRequestedBatchId(batchId);
    setTab("batches");
  }
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch (error) {
      report(error instanceof Error ? error.message : "Fullscreen mode is unavailable", "bad");
    }
  }
  async function installApp() {
    if (installed) {
      report("TheDoPixel shortcut is already added");
      return;
    }
    if (installPrompt) {
      await installPrompt.prompt();
      const choice = await installPrompt.userChoice;
      setInstallPrompt(null);
      if (choice.outcome === "dismissed") report("Shortcut was not added", "bad");
      return;
    }

    const isAppleMobile = /iphone|ipad|ipod/i.test(navigator.userAgent);
    if (isAppleMobile) {
      report("Open Share and choose Add to Home Screen");
    } else {
      report("Open the browser menu and choose Add to Home screen or Create shortcut");
    }
  }
  async function enableNotifications() {
    if (!("Notification" in window)) {
      report("This browser does not support system notifications", "bad");
      setShowNotificationPrompt(false);
      return;
    }
    if (Notification.permission === "denied") {
      report("Notifications are blocked. Allow them in this site’s browser settings.", "bad");
      setShowNotificationPrompt(false);
      return;
    }
    try {
      const permission = Notification.permission === "granted"
        ? Notification.permission
        : await Notification.requestPermission();
      setNotificationPermission(permission);
      setShowNotificationPrompt(false);
      if (permission !== "granted") {
        sessionStorage.setItem("pixel-relay-notification-prompt-dismissed", "true");
      }
      report(
        permission === "granted"
          ? "Batch completion alerts are enabled"
          : "Notification permission was not enabled",
        permission === "granted" ? "good" : "bad"
      );
    } catch {
      setShowNotificationPrompt(false);
      sessionStorage.setItem("pixel-relay-notification-prompt-dismissed", "true");
      report("Notifications require browser permission and a secure connection", "bad");
    }
  }
  function postponeNotifications() {
    sessionStorage.setItem("pixel-relay-notification-prompt-dismissed", "true");
    setShowNotificationPrompt(false);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><span /><span /><span /><span /></div>
          <div><strong>TheDoPixel</strong><small>MEDIA APPLIANCE</small></div>
        </div>
        <nav>
          {nav.map(({ id, label: navLabel, icon: NavIcon }) => (
            <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
              <NavIcon /><span>{navLabel}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className={`connection-dot ${dashboard?.device.state === "device" ? "online" : ""}`} />
          <div><strong>{dashboard?.device.model || "Pixel 1"}</strong><small>{dashboard?.device.state === "device" ? "Connected" : "Needs attention"}</small></div>
          <button className="icon-button" aria-label="Log out" onClick={async () => { await api.logout(); setUser(null); }}>
            <Icons.logout />
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <button className="mobile-brand" onClick={() => setTab("overview")}><div className="brand-mark mini"><span /><span /><span /><span /></div> TheDoPixel</button>
          <div className="eyebrow">THE DO LAB / THE DO PIXEL</div>
          <div className="top-actions">
            <span className="secure-label">PRIVATE NETWORK</span>
            <button
              className={`top-action-button notification-button ${notificationPermission === "granted" ? "enabled" : ""}`}
              type="button"
              aria-label={notificationPermission === "granted" ? "Batch alerts enabled" : "Enable batch alerts"}
              title={notificationPermission === "granted" ? "Batch alerts enabled" : "Enable batch alerts"}
              onClick={() => void enableNotifications()}
            >
              <Icons.bell />
              <span>{notificationPermission === "granted" ? "Alerts on" : "Enable alerts"}</span>
            </button>
            <button
              className="top-action-button install-button"
              type="button"
              aria-label={installed ? "TheDoPixel shortcut is added" : "Add TheDoPixel shortcut"}
              title={installed ? "TheDoPixel shortcut is added" : "Add TheDoPixel shortcut"}
              disabled={installed}
              onClick={() => void installApp()}
            >
              {installed ? <Icons.check /> : <Icons.install />}
              <span>{installed ? "Added" : "Add shortcut"}</span>
            </button>
            <button
              className="top-action-button fullscreen-button"
              type="button"
              aria-label={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              title={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              disabled={!document.fullscreenEnabled}
              onClick={() => void toggleFullscreen()}
            >
              {fullscreen ? <Icons.fullscreenExit /> : <Icons.fullscreen />}
              <span>{fullscreen ? "Exit" : "Fullscreen"}</span>
            </button>
            <span className="avatar">{user.username.slice(0, 2).toUpperCase()}</span>
          </div>
        </header>
        <div className="mobile-nav">
          {nav.map(({ id, label: navLabel, icon: NavIcon }) => (
            <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
              <NavIcon /><span>{navLabel}</span>
            </button>
          ))}
        </div>
        <section className="content">
          {tab === "overview" && <Overview dashboard={dashboard} refresh={refreshDashboard} report={report} openBatch={openBatch} />}
          {tab === "batches" && <Batches queue={dashboard?.queue} refreshQueue={refreshDashboard} report={report} requestedBatchId={requestedBatchId} batchRequestHandled={() => setRequestedBatchId(null)} />}
          {tab === "sources" && <Sources report={report} />}
          {tab === "audit" && <Audit />}
          {tab === "settings" && <Settings report={report} />}
        </section>
      </main>
      {notice && <div className={`toast ${notice.type}`} role={notice.type === "bad" ? "alert" : "status"}>
        <span>{notice.type === "good" ? "✓" : "!"}</span>
        <p>{notice.message}</p>
        {notice.type === "bad" && <CopyButton text={notice.message} />}
        <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={() => setNotice(null)}>×</button>
      </div>}
      {showNotificationPrompt && <div className="notification-prompt-backdrop" role="presentation">
        <section className="notification-prompt" role="dialog" aria-modal="true" aria-labelledby="notification-prompt-title">
          <div className="notification-prompt-icon"><Icons.bell /></div>
          <div>
            <span className="card-kicker">BATCH ALERTS</span>
            <h2 id="notification-prompt-title">Know when each part is ready</h2>
            <p>Allow TheDoPixel to notify you when a batch or multi-batch part finishes processing.</p>
          </div>
          <div className="notification-prompt-actions">
            <button className="secondary" type="button" onClick={postponeNotifications}>Not now</button>
            <button className="primary" type="button" onClick={() => void enableNotifications()}><Icons.bell /> Enable alerts</button>
          </div>
        </section>
      </div>}
    </div>
  );
}

function BootScreen() {
  return <div className="boot"><div className="brand-mark hero"><span /><span /><span /><span /></div><p>Starting TheDoPixel…</p></div>;
}

function ServerStoppedScreen() {
  return <div className="boot stopped-screen"><div className="brand-mark hero"><span /><span /><span /><span /></div><h1>TheDoPixel stopped</h1><p>This tab can be closed. Restart the server and reload to reconnect.</p></div>;
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onLogin(await api.login(username, password));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to sign in");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <div className="login-grid" />
      <div className="login-intro">
        <div className="brand login-brand"><div className="brand-mark"><span /><span /><span /><span /></div><div><strong>TheDoPixel</strong><small>MEDIA APPLIANCE</small></div></div>
        <h1>Your archive,<br /><em>in motion.</em></h1>
        <p>A private bridge from your source library to Google Photos, powered by a genuine Pixel.</p>
        <div className="signal-line"><i /><i /><i /><i /><i /></div>
      </div>
      <form className="login-card" onSubmit={submit}>
        <div className="card-kicker">AUTHORIZED ACCESS</div>
        <h2>Welcome back</h2>
        <p>Sign in to manage the relay.</p>
        <label>Username<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" autoFocus /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary wide" disabled={busy}>{busy ? "Authenticating…" : "Enter dashboard"}<span>→</span></button>
        <small className="privacy-note">Local authentication · Private network only</small>
      </form>
    </div>
  );
}

function QueueControl({ queue, activeBatch, refresh, report }: { queue?: QueueSummary; activeBatch?: Batch | null; refresh: () => Promise<void>; report: (message: string, type?: Notice["type"]) => void }) {
  const [busy, setBusy] = useState(false);
  const mode = queue?.mode || "running";
  const draining = mode === "draining";
  const stopped = mode === "stopped";
  const heading = stopped
    ? "Queue stopped"
    : draining
      ? "Stopping after current batch"
      : "Queue running";
  const detail = stopped
    ? "No queued batch will start until you resume the queue."
    : draining
      ? `${activeBatch?.name || "The active batch"} will finish all remaining files; later batches will stay queued.`
      : activeBatch
        ? `Currently processing “${activeBatch.name}”. Stop waits for this entire batch to finish.`
        : "Queued work starts automatically. With no active batch, Stop pauses the queue immediately.";
  async function toggle() {
    setBusy(true);
    try {
      if (stopped || draining) {
        await api.startQueue();
        report(draining ? "Pending queue stop cancelled" : "Queue started");
      } else {
        const next = await api.stopQueue();
        report(next.mode === "draining" ? "Queue will stop after the current batch finishes" : "Queue stopped");
      }
      await refresh();
    } catch (error) {
      report(error instanceof Error ? error.message : "Queue control failed", "bad");
    } finally {
      setBusy(false);
    }
  }
  return <section className={`queue-control ${mode}`}>
    <div className="queue-control-state"><i /><div><span className="panel-kicker">GLOBAL QUEUE</span><strong>{heading}</strong><small>{detail}</small></div></div>
    <button type="button" className={stopped || draining ? "primary" : "secondary"} disabled={busy} onClick={() => void toggle()}>{busy ? "Updating…" : stopped || draining ? "Start queue" : "Stop after current batch"}</button>
  </section>;
}

function Overview({ dashboard, refresh, report, openBatch }: { dashboard: Dashboard | null; refresh: () => Promise<void>; report: (message: string, type?: Notice["type"]) => void; openBatch: (batchId: string) => void }) {
  const [refreshing, setRefreshing] = useState(false);
  const [restartingAdbServer, setRestartingAdbServer] = useState(false);
  const [enablingNetworkAdb, setEnablingNetworkAdb] = useState(false);
  const [adbPort, setAdbPort] = useState("5555");
  const adbPortInitialized = useRef(false);
  useEffect(() => {
    if (!dashboard || adbPortInitialized.current) return;
    const configuredPort = dashboard.device.serial?.match(/:(\d+)$/)?.[1];
    if (configuredPort) setAdbPort(configuredPort);
    adbPortInitialized.current = true;
  }, [dashboard]);
  if (!dashboard) return <SectionLoader />;
  const { device, queue } = dashboard;
  const activeBatch = dashboard.active_batch
    ?? dashboard.batches.find((batch) => batch.processing)
    ?? null;
  const batches = dashboard.batches.filter((batch) => batch.id !== activeBatch?.id);
  const networkAddresses = ipv4First(device.network_addresses);
  const copyableIp = networkAddresses[0]?.split("/", 1)[0] || "";
  const adbPortNumber = Number(adbPort);
  const adbPortValid = Number.isInteger(adbPortNumber) && adbPortNumber >= 1 && adbPortNumber <= 65535;
  const errors = Object.entries(queue.states).filter(([state]) => problemStates.includes(state as ItemState)).reduce((sum, [, count]) => sum + (count || 0), 0);
  const awaiting = queue.states.awaiting_backup_confirmation || 0;
  const queued = (queue.states.queued || 0) + (queue.states.transferring || 0);
  const targetStorage = device.storage_ready
    ? storageUtilization(
        device.storage_total_bytes,
        device.storage_free_bytes,
        device.storage_used_bytes
      )
    : null;
  const targetStorageKind = device.connection_mode === "ftp"
    ? "FTP target"
    : device.primary_storage_uuid
      && !["null", "none"].includes(device.primary_storage_uuid.toLowerCase())
      ? "Adopted storage"
      : "Phone internal storage";
  const photosProbeUnavailable = device.connection_mode === "ftp";
  const photosProbe = (() => {
    if (photosProbeUnavailable) {
      return {
        heading: "ADB probe unavailable",
        summary: "FTP can transfer media, but it cannot inspect Android app state."
      };
    }
    if (device.state !== "device") {
      return {
        heading: "Waiting for Pixel",
        summary: "Connect the Pixel, then refresh to inspect the Google Photos app."
      };
    }
    if (!device.photos_installed) {
      return {
        heading: "Photos not detected",
        summary: "The Google Photos Android package was not found on this Pixel."
      };
    }
    if (device.photos_running) {
      return {
        heading: "Photos is running",
        summary: "The app process is active on the Pixel."
      };
    }
    return {
      heading: "Photos is ready",
      summary: "The app is installed; its process is currently idle."
    };
  })();
  const probeValue = (value: boolean | null | undefined, yes: string, no: string) =>
    photosProbeUnavailable || device.state !== "device" || value == null ? "Unavailable" : value ? yes : no;
  const probeTone = (value: boolean | null | undefined) =>
    photosProbeUnavailable || device.state !== "device" || value == null ? "" : value ? "good" : "bad";
  const probeAccess = photosProbeUnavailable
    ? "FTP only"
    : device.connection_mode === "usb"
      ? "ADB · USB"
      : "ADB · Network";
  async function refreshDevice() {
    setRefreshing(true);
    try {
      await api.refreshDevice();
      await refresh();
      report("Device health refreshed");
    } catch (error) {
      report(error instanceof Error ? error.message : "Refresh failed", "bad");
    } finally {
      setRefreshing(false);
    }
  }
  async function restartAdbServer() {
    setRestartingAdbServer(true);
    try {
      const result = await api.restartAdbServer();
      await refresh();
      report(
        result.device.state === "device"
          ? "ADB server restarted and the Pixel reconnected"
          : "ADB server restarted; the Pixel is not currently connected"
      );
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not restart the ADB server", "bad");
    } finally {
      setRestartingAdbServer(false);
    }
  }
  async function enableAdbOverIp() {
    if (!adbPortValid) {
      report("ADB port must be between 1 and 65535", "bad");
      return;
    }
    setEnablingNetworkAdb(true);
    try {
      const result = await api.enableAdbOverIp(adbPortNumber);
      if (result.connected) {
        await refresh();
        report(`ADB over IP enabled at ${result.serial}; Pixel Relay switched to network mode`);
      } else {
        const listeners = result.port_diagnostics.listeners;
        const adbWasAlreadyListening =
          result.port_diagnostics.adb_tcp_port_before_restart === result.port;
        const listenerDetail = adbWasAlreadyListening
          ? ` Android's ADB daemon was already listening on port ${result.port} before restart.`
          : listeners.length
          ? ` Before restart, port ${result.port} was held by ${listeners.map((listener) =>
              listener.name
                ? `${listener.name}${listener.pid ? ` (PID ${listener.pid})` : ""}`
                : `an unidentified listener at ${listener.local_address}`
            ).join(", ")}.`
          : result.port_diagnostics.inspection_supported
            ? ` No process was listening on port ${result.port} before the ADB restart, so routing or interface reachability is more likely.`
            : ` Pixel Relay could not inspect port ownership: ${result.port_diagnostics.inspection_error || "unsupported by this Android build"}.`;
        const lastAttempt = result.connection_attempts.at(-1);
        const attemptOutput = lastAttempt?.output.trim().replace(/\s+/g, " ").replace(/[.\s]+$/, "");
        const connectionDetail = attemptOutput
          ? ` Host ADB reported: ${attemptOutput}.`
          : "";
        const routeGuidance = attemptOutput && /no route to host/i.test(attemptOutput)
          ? " Check the relay host's route, firewall, and local-network access."
          : "";
        report(
          result.address
            ? `ADB port ${result.port} was enabled, but Pixel Relay could not connect to ${result.serial}.${connectionDetail}${listenerDetail}${routeGuidance}`
            : `ADB port ${result.port} was enabled, but no Pixel IPv4 address was found.${listenerDetail}`,
          "bad"
        );
      }
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not enable ADB over IP", "bad");
    } finally {
      setEnablingNetworkAdb(false);
    }
  }
  async function copyIp() {
    if (!copyableIp) return;
    try {
      await copyText(copyableIp);
      report(`Copied ${copyableIp}`);
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not copy the Pixel IP", "bad");
    }
  }
  return (
    <>
      <div className="page-heading">
        <div><div className="page-kicker">OPERATIONS</div><h1>Good {timeGreeting()}. <span>Here’s the relay.</span></h1></div>
        <div className="page-heading-actions"><button className="secondary" onClick={restartAdbServer} disabled={restartingAdbServer}><Icons.refresh className={restartingAdbServer ? "spin" : ""} /> {restartingAdbServer ? "Restarting ADB…" : "Restart ADB server"}</button><button className="secondary" onClick={refreshDevice} disabled={refreshing}><Icons.refresh className={refreshing ? "spin" : ""} /> Refresh device</button></div>
      </div>
      <div className="device-banner">
        <div className="device-illustration"><Icons.phone /><span className={device.state === "device" ? "online" : ""} /></div>
        <div className="device-identity">
          <div className="status-line"><span className={`status-pill ${device.state === "device" ? "good" : "bad"}`}>{device.state === "device" ? "ONLINE" : label(device.state)}</span><small>{device.serial}</small></div>
          <h2>{device.model || "Google Pixel"}</h2>
          <p>Android {device.android_version || "—"} · Last seen {relativeTime(device.observed_at)}</p>
        </div>
        <div className="device-stats">
          <MiniStat icon={Icons.battery} label="Battery" value={device.battery_level != null ? `${device.battery_level}%` : "—"} note={device.charging == null ? "Unavailable" : device.charging ? "Charging" : "Not charging"} good={device.charging === true} />
          <MiniStat icon={Icons.temperature} label="Temperature" value={device.temperature_c != null ? `${device.temperature_c.toFixed(1)}°C` : "—"} note={device.temperature_c == null ? "Unavailable" : device.temperature_c >= 42 ? "Paused" : "Nominal"} good={device.temperature_c != null && device.temperature_c < 42} />
          <MiniStat icon={Icons.network} label="Ethernet" value={device.ethernet == null ? "—" : device.ethernet ? "Active" : "Inactive"} note={device.ethernet == null ? "Unavailable" : "ADB over LAN"} good={device.ethernet === true} />
          <MiniStat
            icon={Icons.storage}
            label="Target storage"
            value={targetStorage ? `${bytes(targetStorage.freeBytes)} / ${bytes(targetStorage.totalBytes)}` : "—"}
            note={targetStorage
              ? `${targetStorage.utilizedPercent.toFixed(1)}% utilized · ${targetStorageKind}`
              : device.connection_mode === "ftp" && device.storage_ready
                ? "FTP path reachable · capacity unavailable"
                : device.storage_ready
                  ? `${targetStorageKind} · capacity unavailable`
                  : "Target unavailable or migration in progress"}
            good={device.storage_ready === true}
          />
        </div>
      </div>
      {device.error && <div className="alert-strip"><Icons.warning /> <div><strong>Device needs attention</strong><span>{device.error}</span></div></div>}
      <section className="network-panel">
        <div className="network-panel-head"><Icons.network /><div><span className="panel-kicker">PIXEL NETWORK</span><strong>{device.network_type ? label(device.network_type) : device.connection_mode === "ftp" ? "FTP connection" : "Network unavailable"}</strong><div className="network-port-control"><label>ADB port<input type="number" min="1" max="65535" inputMode="numeric" value={adbPort} onChange={(event) => setAdbPort(event.target.value)} aria-invalid={!adbPortValid} /></label><button type="button" className="secondary small" onClick={copyIp} disabled={!copyableIp}>Copy IP</button></div><button type="button" className="secondary small network-adb-button" onClick={enableAdbOverIp} disabled={enablingNetworkAdb || !adbPortValid}><Icons.network /> {enablingNetworkAdb ? "Enabling…" : device.connection_mode === "network" && device.state === "device" ? "Reconfigure ADB over IP" : "Enable ADB over IP"}</button></div></div>
        <div className="network-settings">
          <span><small>Interface</small><b>{device.network_interface || "—"}</b></span>
          <span><small>IP address</small><b title={networkAddresses.join(", ")}>{networkAddresses.join(", ") || "—"}</b></span>
          <span><small>Gateway</small><b>{device.network_gateway || "—"}</b></span>
          <span><small>DNS</small><b title={device.network_dns_servers?.join(", ")}>{device.network_dns_servers?.join(", ") || "—"}</b></span>
          <span><small>Wi-Fi network</small><b>{device.network_ssid || "—"}</b></span>
        </div>
      </section>
      <div className="metric-grid">
        <MetricCard label="In motion" value={queued} sub="Queued or transferring" accent="cyan" />
        <MetricCard label="Ready to verify" value={awaiting} sub="Human confirmation required" accent="amber" />
        <MetricCard label="Attention" value={errors} sub={errors ? "Open errors and pauses" : "No active errors"} accent={errors ? "red" : "green"} />
        <MetricCard label="Last confirmed" value={queue.last_confirmed_upload?.name || "—"} sub={relativeTime(queue.last_confirmed_upload?.confirmed_at)} accent="violet" text />
      </div>
      <QueueControl queue={queue} activeBatch={activeBatch} refresh={refresh} report={report} />
      <section className={`panel active-batch-panel overview-active-batch ${activeBatch ? "" : "idle"}`}>
        <div className="panel-head"><div><span className="panel-kicker active-now-label"><i /> ACTIVE NOW</span><h2>{activeBatch ? "Currently being worked on" : "No batch active right now"}</h2></div><small>{activeBatch ? "Pixel Relay is transferring or verifying this batch." : "The next eligible queued batch will appear here automatically."}</small></div>
        {activeBatch && <div className="batch-list compact">
          <BatchRow batch={activeBatch} onClick={() => openBatch(activeBatch.id)} />
        </div>}
      </section>
      <div className="overview-columns">
        <section className="panel">
          <div className="panel-head"><div><span className="panel-kicker">UP NEXT</span><h2>Other batch summaries</h2></div><small>Up to five in-progress batches</small></div>
          <div className="batch-summary-list">
            {batches.length ? batches.map((batch) => <BatchSummaryRow key={batch.id} batch={batch} onClick={() => openBatch(batch.id)} />) : <Empty icon="✓" title={activeBatch ? "No other batches in progress" : "No batches in progress"} text={activeBatch ? "Only the active batch currently needs work." : "Queued batches will appear here."} />}
          </div>
        </section>
        <section className="panel photos-card">
          <div className="photos-orbit"><Icons.photos /><i /><i /><i /><i /></div>
          <span className="panel-kicker">GOOGLE PHOTOS PROBE</span>
          <h2>{photosProbe.heading}</h2>
          <p>{photosProbe.summary}</p>
          <div className="probe-grid">
            <span className={probeTone(device.photos_installed)}>Installed <b>{probeValue(device.photos_installed, "Yes", "Not found")}</b></span>
            <span className={photosProbeUnavailable || device.state !== "device" || device.photos_running == null ? "" : device.photos_running ? "good" : "warn"}>Process <b>{probeValue(device.photos_running, "Running", "Idle")}</b></span>
            <span>Version <b title={device.photos_version || undefined}>{photosProbeUnavailable ? "Unavailable" : device.photos_version || "Unknown"}</b></span>
            <span>Access <b>{probeAccess}</b></span>
            <span>Checked <b title={dateTime(device.observed_at)}>{relativeTime(device.observed_at)}</b></span>
          </div>
          <a className="secondary photos-open" href="https://photos.google.com/" target="_blank" rel="noreferrer"><Icons.photos /> Open Google Photos</a>
        </section>
      </div>
      <TelemetryPanel />
    </>
  );
}

type TelemetryMetric = "battery_level" | "temperature_c" | "storage_free_bytes";

function TelemetryPanel() {
  const [hours, setHours] = useState(24);
  const [telemetry, setTelemetry] = useState<DeviceTelemetry | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    let timer = 0;
    async function load() {
      try {
        const next = await api.telemetry(hours);
        if (!stopped) {
          setTelemetry(next);
          setError("");
        }
      } catch (reason) {
        if (!stopped) setError(reason instanceof Error ? reason.message : "Telemetry unavailable");
      } finally {
        if (!stopped) timer = window.setTimeout(() => void load(), 60_000);
      }
    }
    void load();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [hours]);
  return <section className="panel telemetry-panel">
    <div className="panel-head telemetry-head">
      <div><span className="panel-kicker">DEVICE ANALYTICS</span><h2>Pixel health over time</h2><small>{telemetry?.sample_count || 0} readings in this window</small></div>
      <div className="telemetry-ranges" role="group" aria-label="Telemetry time range">
        {[6, 24, 168, 720].map((range) => <button key={range} className={hours === range ? "active" : ""} onClick={() => setHours(range)}>{range < 24 ? `${range}h` : range === 24 ? "24h" : range === 168 ? "7d" : "30d"}</button>)}
      </div>
    </div>
    {error && <div className="telemetry-error">{error}</div>}
    <div className="telemetry-grid">
      <TelemetryChart title="Battery" metric="battery_level" data={telemetry} color="#31d7c4" format={(value) => `${Math.round(value)}%`} fixedDomain={[0, 100]} />
      <TelemetryChart title="Temperature" metric="temperature_c" data={telemetry} color="#f0b44c" format={(value) => `${value.toFixed(1)}°C`} />
      <TelemetryChart title="Free storage" metric="storage_free_bytes" data={telemetry} color="#a48aff" format={bytes} />
    </div>
  </section>;
}

function TelemetryChart({ title, metric, data, color, format, fixedDomain }: { title: string; metric: TelemetryMetric; data: DeviceTelemetry | null; color: string; format: (value: number) => string; fixedDomain?: [number, number] }) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const points = (data?.points || []).filter(
    (point): point is DeviceTelemetryPoint & Record<TelemetryMetric, number> =>
      typeof point[metric] === "number" && Number.isFinite(point[metric])
  );
  const values = points.map((point) => point[metric]);
  const summary = data?.summary[metric];
  if (!points.length) return <article className="telemetry-chart empty"><div><strong>{title}</strong><span>No readings yet</span></div><div className="telemetry-empty-line" /></article>;
  const width = 520;
  const height = 150;
  const padding = 14;
  const timestamps = points.map((point) => new Date(point.observed_at).getTime());
  const firstTime = Math.min(...timestamps);
  const lastTime = Math.max(...timestamps);
  const observedMin = Math.min(...values);
  const observedMax = Math.max(...values);
  const spread = Math.max(1, observedMax - observedMin);
  const minimum = fixedDomain?.[0] ?? Math.max(0, observedMin - spread * 0.12);
  const maximum = fixedDomain?.[1] ?? observedMax + spread * 0.12;
  const range = Math.max(1, maximum - minimum);
  const x = (timestamp: number, index: number) => lastTime === firstTime
    ? padding + (points.length === 1 ? (width - padding * 2) / 2 : index / (points.length - 1) * (width - padding * 2))
    : padding + (timestamp - firstTime) / (lastTime - firstTime) * (width - padding * 2);
  const y = (value: number) => height - padding - (value - minimum) / range * (height - padding * 2);
  const linePoints = points.map((point, index) => `${x(timestamps[index], index).toFixed(1)},${y(point[metric]).toFixed(1)}`).join(" ");
  const areaPoints = `${padding},${height - padding} ${linePoints} ${width - padding},${height - padding}`;
  const gradientId = `telemetry-${metric}`;
  const activeIndex = hoveredIndex != null && hoveredIndex < points.length ? hoveredIndex : null;
  const activePoint = activeIndex == null ? null : points[activeIndex];
  const activeX = activeIndex == null ? null : x(timestamps[activeIndex], activeIndex);
  const activeY = activePoint == null ? null : y(activePoint[metric]);
  const tooltipWidth = 168;
  const tooltipHeight = 43;
  const tooltipX = activeX == null
    ? 0
    : Math.max(padding, Math.min(width - padding - tooltipWidth, activeX - tooltipWidth / 2));
  const tooltipY = activeY == null
    ? 0
    : activeY > 62 ? activeY - tooltipHeight - 10 : activeY + 10;
  return <article className="telemetry-chart">
    <div className="telemetry-chart-title"><div><strong>{title}</strong><span>Current {format(summary?.latest ?? values.at(-1)!)}</span></div><div><span>Avg {summary?.average == null ? "—" : format(summary.average)}</span><span>{format(observedMin)}–{format(observedMax)}</span></div></div>
    <svg
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${title} over the selected time range. Move across the plot to inspect a reading.`}
      onPointerMove={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const pointerX = (event.clientX - bounds.left) / bounds.width * width;
        let nearest = 0;
        let nearestDistance = Number.POSITIVE_INFINITY;
        timestamps.forEach((timestamp, index) => {
          const distance = Math.abs(x(timestamp, index) - pointerX);
          if (distance < nearestDistance) {
            nearest = index;
            nearestDistance = distance;
          }
        });
        setHoveredIndex(nearest);
      }}
      onPointerLeave={() => setHoveredIndex(null)}
    >
      <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={color} stopOpacity=".24" /><stop offset="1" stopColor={color} stopOpacity="0" /></linearGradient></defs>
      {[.25, .5, .75].map((position) => <line key={position} x1={padding} x2={width - padding} y1={height * position} y2={height * position} className="telemetry-gridline" />)}
      <polygon points={areaPoints} fill={`url(#${gradientId})`} />
      <polyline points={linePoints} fill="none" stroke={color} strokeWidth="3" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(timestamps.at(-1)!, points.length - 1)} cy={y(values.at(-1)!)} r="4" fill={color} />
      {activePoint && activeX != null && activeY != null && <g className="telemetry-hover">
        <line x1={activeX} x2={activeX} y1={padding} y2={height - padding} />
        <circle cx={activeX} cy={activeY} r="5" fill={color} />
        <rect x={tooltipX} y={tooltipY} width={tooltipWidth} height={tooltipHeight} rx="6" />
        <text className="telemetry-tooltip-value" x={tooltipX + 10} y={tooltipY + 17}>{format(activePoint[metric])}</text>
        <text className="telemetry-tooltip-time" x={tooltipX + 10} y={tooltipY + 33}>{new Date(activePoint.observed_at).toLocaleString()}</text>
      </g>}
    </svg>
    <div className="telemetry-axis"><span>{new Date(firstTime).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}</span><span>{new Date(lastTime).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}</span></div>
  </article>;
}

function MiniStat({ icon: Icon, label: statLabel, value, note, good }: { icon: typeof Icons.battery; label: string; value: string; note: string; good?: boolean }) {
  return <div className="mini-stat"><Icon /><div><span>{statLabel}</span><strong>{value}</strong><small className={good ? "positive" : ""}>{note}</small></div></div>;
}

function MetricCard({ label: cardLabel, value, sub, accent, text = false }: { label: string; value: string | number; sub: string; accent: string; text?: boolean }) {
  return <div className={`metric-card ${accent}`}><span>{cardLabel}</span><strong className={text ? "textual" : ""}>{value}</strong><small>{sub}</small><i /></div>;
}

function Batches({ queue, refreshQueue, report, requestedBatchId, batchRequestHandled }: { queue?: QueueSummary; refreshQueue: () => Promise<void>; report: (message: string, type?: Notice["type"]) => void; requestedBatchId: string | null; batchRequestHandled: () => void }) {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [backupSummary, setBackupSummary] = useState<BackedUpInventory | null>(null);
  const [selected, setSelected] = useState<Batch | null>(null);
  const [selectedBatchIds, setSelectedBatchIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [filter, setFilter] = useState<BatchFilter>("all");
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setBatches(await api.batches());
    if (selected) setSelected(await api.batch(selected.id));
    setLoading(false);
  }, [selected?.id]);
  useEffect(() => {
    let stopped = false;
    let timer = 0;
    async function poll() {
      try {
        await load();
      } catch {
        if (!stopped) setLoading(false);
      } finally {
        if (!stopped) timer = window.setTimeout(() => void poll(), 750);
      }
    }
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [load]);
  useEffect(() => {
    if (!requestedBatchId) return;
    let stopped = false;
    void api.batch(requestedBatchId)
      .then((batch) => {
        if (!stopped) setSelected(batch);
      })
      .catch((error) => {
        if (!stopped) report(error instanceof Error ? error.message : "Batch could not be opened", "bad");
      })
      .finally(() => {
        if (!stopped) batchRequestHandled();
      });
    return () => {
      stopped = true;
    };
  }, [requestedBatchId]);
  useEffect(() => {
    const availableIds = new Set(batches.map((batch) => batch.id));
    setSelectedBatchIds((current) => {
      const next = new Set([...current].filter((id) => availableIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [batches]);
  if (loading) return <SectionLoader />;
  const filters: { id: BatchFilter; label: string }[] = [
    { id: "all", label: "All" },
    { id: "processing", label: "Processing" },
    { id: "ready", label: "Ready to verify" },
    { id: "confirmed", label: "Photos confirmed" },
    { id: "attention", label: "Attention" },
    { id: "cancelled", label: "Cancelled" }
  ];
  const activeBatch = batches.find((batch) => batch.processing);
  const readyToVerify = batches.filter((batch) =>
    isBatchReady(batch) && !batch.confirmed_at && !batch.cancelled_at && !batch.purged_at
  );
  const nonCancelledBatches = batches.filter((batch) => !batch.cancelled_at);
  const allBatchesVerified = nonCancelledBatches.length > 0
    && nonCancelledBatches.every((batch) => Boolean(batch.confirmed_at || batch.purged_at));
  const verifiedToPurge = nonCancelledBatches.filter((batch) =>
    Boolean(batch.confirmed_at)
    && !batch.purged_at
    && Boolean(batch.states.confirmed_backed_up)
    && (batch.states.confirmed_backed_up || 0) + (batch.states.purged_from_pixel || 0)
      === batch.file_count
  );
  const visibleBatches = batches.filter(
    (batch) => batch.id !== activeBatch?.id && matchesBatchFilter(batch, filter)
  );
  const selectedBatches = batches.filter((batch) => selectedBatchIds.has(batch.id));
  const allVisibleSelected = visibleBatches.length > 0
    && visibleBatches.every((batch) => selectedBatchIds.has(batch.id));
  const bulkEligibility = {
    pause: selectedBatches.filter((batch) =>
      !batch.cancelled_at && !batch.confirmed_at && !batch.purged_at
      && !batch.paused_at && !isBatchReady(batch)
    ),
    resume: selectedBatches.filter((batch) =>
      !batch.cancelled_at && !batch.confirmed_at && !batch.purged_at
      && Boolean(batch.paused_at)
    ),
    retry: selectedBatches.filter((batch) =>
      Object.keys(batch.states).some((state) => problemStates.includes(state as ItemState))
    ),
    cancel: selectedBatches.filter((batch) =>
      !batch.cancelled_at && !batch.confirmed_at && !batch.purged_at
    )
  };
  function toggleBatchSelection(batchId: string) {
    setSelectedBatchIds((current) => {
      const next = new Set(current);
      if (next.has(batchId)) next.delete(batchId);
      else next.add(batchId);
      return next;
    });
  }
  function toggleVisibleSelection() {
    setSelectedBatchIds((current) => {
      const next = new Set(current);
      visibleBatches.forEach((batch) => {
        if (allVisibleSelected) next.delete(batch.id);
        else next.add(batch.id);
      });
      return next;
    });
  }
  async function runBulkAction(action: keyof typeof bulkEligibility) {
    const targets = bulkEligibility[action];
    if (!targets.length) return;
    if (
      action === "cancel"
      && !window.confirm(
        `Cancel ${targets.length} selected ${targets.length === 1 ? "batch" : "batches"}? `
        + "Current file operations will finish safely, and Pixel copies remain tracked for cleanup."
      )
    ) return;
    setBulkBusy(true);
    const actionCalls = {
      pause: (id: string) => api.pauseBatch(id),
      resume: (id: string) => api.resumeBatch(id),
      retry: (id: string) => api.retryBatch(id),
      cancel: (id: string) => api.cancelBatch(id)
    };
    try {
      const results = await Promise.allSettled(
        targets.map((batch) => actionCalls[action](batch.id))
      );
      const failures = results.filter(
        (result): result is PromiseRejectedResult => result.status === "rejected"
      );
      await Promise.all([load(), refreshQueue()]);
      const succeeded = results.length - failures.length;
      const actionLabel = {
        pause: "paused",
        resume: "resumed",
        retry: "retried",
        cancel: "cancelled"
      }[action];
      if (failures.length) {
        const firstError = failures[0].reason instanceof Error
          ? failures[0].reason.message
          : "Unknown error";
        report(
          `${succeeded} of ${results.length} selected batches ${actionLabel}. `
          + `${failures.length} failed: ${firstError}`,
          "bad"
        );
      } else {
        report(`${succeeded} selected ${succeeded === 1 ? "batch" : "batches"} ${actionLabel}`);
      }
    } catch (error) {
      report(error instanceof Error ? error.message : "Bulk batch action failed", "bad");
    } finally {
      setBulkBusy(false);
    }
  }
  async function verifyAllReady() {
    if (!readyToVerify.length) return;
    const count = readyToVerify.length;
    if (!window.confirm(
      `Mark all ${count} ready ${count === 1 ? "batch" : "batches"} as verified? `
      + "Only continue after confirming every batch is backed up in Google Photos. "
      + "This does not purge any Pixel copies."
    )) return;
    setBulkBusy(true);
    try {
      const results = await Promise.allSettled(
        readyToVerify.map((batch) => api.confirmBatch(batch.id))
      );
      const failures = results.filter(
        (result): result is PromiseRejectedResult => result.status === "rejected"
      );
      await Promise.all([load(), refreshQueue()]);
      const succeeded = results.length - failures.length;
      if (failures.length) {
        const firstError = failures[0].reason instanceof Error
          ? failures[0].reason.message
          : "Unknown error";
        report(
          `${succeeded} of ${results.length} ready batches verified. `
          + `${failures.length} failed: ${firstError}`,
          "bad"
        );
      } else {
        report(`${succeeded} ready ${succeeded === 1 ? "batch" : "batches"} marked as backed up`);
      }
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not verify ready batches", "bad");
    } finally {
      setBulkBusy(false);
    }
  }
  async function purgeAllVerified() {
    if (!allBatchesVerified || !verifiedToPurge.length) return;
    const count = verifiedToPurge.length;
    if (!window.confirm(
      `Permanently remove Pixel copies for all ${count} verified `
      + `${count === 1 ? "batch" : "batches"}? `
      + "Source files and Google Photos copies will not be deleted. "
      + "This cannot be undone from Pixel Relay."
    )) return;
    setBulkBusy(true);
    const failures: unknown[] = [];
    let succeeded = 0;
    try {
      for (const batch of verifiedToPurge) {
        try {
          await api.purgeBatch(batch.id);
          succeeded += 1;
        } catch (error) {
          failures.push(error);
        }
      }
      await Promise.all([load(), refreshQueue()]);
      if (failures.length) {
        const firstError = failures[0] instanceof Error
          ? failures[0].message
          : "Unknown error";
        report(
          `${succeeded} of ${count} verified batches purged. `
          + `${failures.length} failed: ${firstError}`,
          "bad"
        );
      } else {
        report(`Pixel copies purged for ${succeeded} verified ${succeeded === 1 ? "batch" : "batches"}`);
      }
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not purge verified batches", "bad");
    } finally {
      setBulkBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading"><div><div className="page-kicker">RELAY WORK</div><h1>Batches <span>& queue</span></h1><p>Every file keeps a complete, restart-safe state history.</p></div><div className="page-heading-actions"><button type="button" className="primary amber" disabled={bulkBusy || !readyToVerify.length} onClick={() => void verifyAllReady()}>{bulkBusy ? "Updating…" : `Verify all ready (${readyToVerify.length})`}</button><button type="button" className="danger" disabled={bulkBusy || !allBatchesVerified || !verifiedToPurge.length} title={!allBatchesVerified ? "Every non-cancelled batch must be verified first" : undefined} onClick={() => void purgeAllVerified()}>{bulkBusy ? "Updating…" : `Purge all verified (${verifiedToPurge.length})`}</button><button type="button" className="secondary" disabled={bulkBusy} onClick={() => void load()}><Icons.refresh /> Refresh</button></div></div>
      <QueueControl queue={queue} activeBatch={activeBatch} refresh={refreshQueue} report={report} />
      <section className="panel unique-upload-panel">
        <div className="panel-head"><div><span className="panel-kicker">ALL-TIME UNIQUE TOTALS</span><h2>Unique relay uploads</h2></div><small>Confirmed and awaiting-verification files, deduplicated together by SHA-256.</small></div>
        {backupSummary ? <div className="backed-up-summary">
          <span><small>All unique files</small><b>{(backupSummary.uploaded_total ?? backupSummary.total).toLocaleString()}</b><em>{bytes(backupSummary.uploaded_total_bytes ?? backupSummary.total_bytes)} total</em></span>
          <span><small>Confirmed</small><b>{backupSummary.total.toLocaleString()}</b><em>{bytes(backupSummary.total_bytes)} verified</em></span>
          <span><small>Awaiting verification</small><b>{(backupSummary.awaiting_verification_count ?? 0).toLocaleString()}</b><em>{bytes(backupSummary.awaiting_verification_bytes ?? 0)} not yet confirmed</em></span>
          <span><small>Unique media mix</small><b>{(backupSummary.uploaded_photo_count ?? backupSummary.photo_count).toLocaleString()} / {(backupSummary.uploaded_raw_count ?? backupSummary.raw_count).toLocaleString()} / {(backupSummary.uploaded_video_count ?? backupSummary.video_count).toLocaleString()}</b><em>Photos / RAW / Videos</em></span>
        </div> : <div className="aggregate-loading">Calculating unique relay uploads…</div>}
      </section>
      {activeBatch && <section className="panel active-batch-panel">
        <div className="panel-head"><div><span className="panel-kicker active-now-label"><i /> ACTIVE NOW</span><h2>Currently being worked on</h2></div><small>Pixel Relay is transferring or verifying this batch.</small></div>
        <div className="batch-list compact">
          <BatchRow batch={activeBatch} onClick={async () => setSelected(await api.batch(activeBatch.id))} />
        </div>
      </section>}
      <section className="panel">
        <div className="batch-filter-bar" role="group" aria-label="Filter batches">
          {filters.map(({ id, label: filterLabel }) => (
            <button key={id} className={filter === id ? "active" : ""} onClick={() => setFilter(id)}>
              {filterLabel} <b>{batches.filter((batch) => matchesBatchFilter(batch, id)).length}</b>
            </button>
          ))}
          <small>“Photos confirmed” means manually verified in Pixel Relay.</small>
        </div>
        <div className="batch-bulk-bar">
          <label className="batch-select-all">
            <input
              type="checkbox"
              checked={allVisibleSelected}
              ref={(input) => {
                if (input) {
                  input.indeterminate = !allVisibleSelected
                    && visibleBatches.some((batch) => selectedBatchIds.has(batch.id));
                }
              }}
              disabled={!visibleBatches.length || bulkBusy}
              onChange={toggleVisibleSelection}
            />
            <span>{allVisibleSelected ? "Deselect shown" : "Select shown"}</span>
          </label>
          <strong>{selectedBatches.length.toLocaleString()} selected</strong>
          <div className="batch-bulk-actions">
            <button type="button" className="secondary small" disabled={bulkBusy || !bulkEligibility.pause.length} onClick={() => void runBulkAction("pause")}>Pause ({bulkEligibility.pause.length})</button>
            <button type="button" className="secondary small" disabled={bulkBusy || !bulkEligibility.resume.length} onClick={() => void runBulkAction("resume")}>Resume ({bulkEligibility.resume.length})</button>
            <button type="button" className="secondary small" disabled={bulkBusy || !bulkEligibility.retry.length} onClick={() => void runBulkAction("retry")}><Icons.refresh /> Retry ({bulkEligibility.retry.length})</button>
            <button type="button" className="danger small" disabled={bulkBusy || !bulkEligibility.cancel.length} onClick={() => void runBulkAction("cancel")}>Cancel ({bulkEligibility.cancel.length})</button>
            <button type="button" className="secondary small" disabled={bulkBusy || !selectedBatchIds.size} onClick={() => setSelectedBatchIds(new Set())}>Clear</button>
          </div>
        </div>
        <div className="table-head batch-selectable-head"><span /><span>Batch</span><span>Progress</span><span>Size</span><span>Created</span><span /></div>
        <div className="batch-list">
          {visibleBatches.map((batch) => <div className={`selectable-batch-row ${selectedBatchIds.has(batch.id) ? "selected" : ""}`} key={batch.id}>
            <label className="batch-select" title={`Select ${batch.name}`}>
              <input
                type="checkbox"
                aria-label={`Select ${batch.name}`}
                checked={selectedBatchIds.has(batch.id)}
                disabled={bulkBusy}
                onChange={() => toggleBatchSelection(batch.id)}
              />
            </label>
            <BatchRow batch={batch} detailed onClick={async () => setSelected(await api.batch(batch.id))} />
          </div>)}
          {!batches.length && <Empty icon="□" title="No batches" text="Create one from discovered source media." />}
          {batches.length > 0 && !visibleBatches.length && <Empty icon="○" title={activeBatch && matchesBatchFilter(activeBatch, filter) ? "No other matching batches" : "No matching batches"} text={activeBatch && matchesBatchFilter(activeBatch, filter) ? "The active batch is shown separately above." : "Choose another status filter."} />}
        </div>
      </section>
      <BackedUpMediaInventory report={report} onSummary={setBackupSummary} />
      {selected && <BatchDrawer batch={selected} close={() => setSelected(null)} reload={async () => { await load(); setSelected(await api.batch(selected.id)); }} removed={async () => { setSelected(null); setBatches(await api.batches()); }} retriggered={async (batch) => { await load(); setSelected(batch); }} report={report} />}
    </>
  );
}

function BackedUpMediaInventory({ report, onSummary }: { report: (message: string, type?: Notice["type"]) => void; onSummary: (inventory: BackedUpInventory) => void }) {
  const pageSize = 250;
  const [inventory, setInventory] = useState<BackedUpInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [query, setQuery] = useState("");
  const [mediaFilter, setMediaFilter] = useState<"all" | "photo" | "raw" | "video">("all");
  const load = useCallback(async (reset = true) => {
    reset ? setLoading(true) : setLoadingMore(true);
    try {
      const offset = reset ? 0 : inventory?.items.length || 0;
      const next = await api.backedUpItems(pageSize, offset);
      onSummary(next);
      setInventory((current) => ({
        ...next,
        items: reset ? next.items : [...(current?.items || []), ...next.items]
      }));
    } catch (error) {
      report(error instanceof Error ? error.message : "Backed-up media could not be loaded", "bad");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [inventory?.items.length]);
  useEffect(() => {
    void load(true);
  }, []);
  useEffect(() => {
    const stream = new EventSource("/api/v1/events");
    let timer = 0;
    const refresh = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void load(true), 300);
    };
    stream.addEventListener("batch", refresh);
    return () => {
      window.clearTimeout(timer);
      stream.removeEventListener("batch", refresh);
      stream.close();
    };
  }, []);

  const normalizedQuery = query.trim().toLowerCase();
  const visible = (inventory?.items || []).filter((item) => {
    const raw = isRaw(item);
    if (mediaFilter === "raw" && !raw) return false;
    if (mediaFilter === "photo" && (item.media_kind !== "photo" || raw)) return false;
    if (mediaFilter === "video" && item.media_kind !== "video") return false;
    return !normalizedQuery
      || item.path.toLowerCase().includes(normalizedQuery)
      || item.batch_name.toLowerCase().includes(normalizedQuery);
  });
  const mediaLabel = (item: BackedUpItem) => isRaw(item)
    ? "RAW"
    : item.media_kind === "video"
      ? "VIDEO"
      : "PHOTO";

  return <section className="panel backed-up-inventory">
    <div className="panel-head backed-up-head">
      <div><span className="panel-kicker">CONFIRMED GOOGLE PHOTOS HISTORY</span><h2>Backed-up media</h2><p>One row per unique SHA-256. Repeated confirmations are counted without duplicating media totals.</p></div>
      <button type="button" className="secondary small" disabled={loading} onClick={() => void load(true)}><Icons.refresh className={loading ? "spin" : ""} /> {loading ? "Loading…" : "Refresh"}</button>
    </div>
    {inventory && <div className="backed-up-summary">
      <span><small>Unique confirmed</small><b>{inventory.total.toLocaleString()}</b><em>{bytes(inventory.total_bytes)}</em></span>
      <span><small>Photos</small><b>{inventory.photo_count.toLocaleString()}</b><em>{inventory.raw_count.toLocaleString()} RAW</em></span>
      <span><small>Videos</small><b>{inventory.video_count.toLocaleString()}</b><em>Confirmed</em></span>
      <span><small>Pixel copies</small><b>{inventory.retained_on_pixel_count.toLocaleString()} retained</b><em>{inventory.purged_from_pixel_count.toLocaleString()} purged</em></span>
    </div>}
    <div className="backed-up-controls">
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter by filename or batch…" />
      <div role="group" aria-label="Filter backed-up media">
        {(["all", "photo", "raw", "video"] as const).map((kind) => <button type="button" key={kind} className={mediaFilter === kind ? "active" : ""} onClick={() => setMediaFilter(kind)}>{kind === "all" ? "All" : kind.toUpperCase()}</button>)}
      </div>
    </div>
    <div className="backed-up-list">
      {visible.map((item) => <div className="backed-up-row" key={item.id}>
        <span className={`file-type ${isRaw(item) ? "raw" : item.media_kind}`}>{mediaLabel(item)}</span>
        <div><strong title={item.path}>{shortPath(item.path)}</strong><small>{item.batch_name} · {bytes(item.size)} · Last confirmed {dateTime(item.latest_confirmed_at)}{item.confirmation_count > 1 ? ` · ${item.confirmation_count.toLocaleString()} confirmations since ${dateTime(item.first_confirmed_at)}` : ""}</small></div>
        <span className={`backup-copy-state ${item.retained_copy_count ? "retained" : "purged"}`}>{item.retained_copy_count ? `${item.retained_copy_count} RETAINED${item.purged_copy_count ? ` · ${item.purged_copy_count} PURGED` : ""}` : `${item.purged_copy_count} PIXEL ${item.purged_copy_count === 1 ? "COPY" : "COPIES"} PURGED`}</span>
        <a className="secondary small" href={`/?tab=batches&batch=${encodeURIComponent(item.batch_id)}`}>Open batch</a>
      </div>)}
      {!loading && inventory && !visible.length && <Empty icon="✓" title={inventory.total ? "No matching backed-up items" : "No confirmed backups yet"} text={inventory.total ? "Change the media or text filter." : "Items appear after you explicitly confirm a completed Google Photos backup."} />}
      {loading && !inventory && <SectionLoader />}
    </div>
    {inventory && <div className="backed-up-footer"><small>Showing {inventory.items.length.toLocaleString()} of {inventory.total.toLocaleString()} unique confirmed items{visible.length !== inventory.items.length ? ` · ${visible.length.toLocaleString()} match the current filter` : ""}</small>{inventory.items.length < inventory.total && <button type="button" className="secondary small" disabled={loadingMore} onClick={() => void load(false)}>{loadingMore ? "Loading…" : "Load more"}</button>}</div>}
  </section>;
}

function BatchRow({ batch, detailed = false, onClick }: { batch: Batch; detailed?: boolean; onClick?: () => void }) {
  const progress = batchProgress(batch.states, batch.file_count);
  const mediaScan = mediaScanProgress(batch.states, batch.file_count);
  const transferredBytes = Math.min(batch.total_bytes, batch.transfer_bytes || 0);
  const transferPercent = batch.total_bytes
    ? Math.round((transferredBytes / batch.total_bytes) * 1000) / 10
    : 0;
  const hasError = Object.keys(batch.states).some((state) => problemStates.includes(state as ItemState)) || Boolean(batch.stalled);
  const status = batch.cancelled_at ? "Cancelled" : batch.purged_at ? "Purged" : batch.confirmed_at ? "Confirmed" : batch.paused_at ? (batch.processing ? "Pausing safely" : "Paused") : batch.stalled ? "Stalled" : hasError ? "Attention" : batch.series_blocked ? "Waiting for prior part" : batch.storage_blocked ? "Waiting for Pixel space" : progress.ready === progress.total && progress.total > 0 ? "Ready to verify" : "Processing";
  const activeDetail = [
    batch.states.transferring ? `${batch.states.transferring} transferring` : "",
    mediaScan.scanning ? `${mediaScan.scanning} scanning` : "",
    progress.segments.find((segment) => segment.kind === "attention")?.count
      ? `${progress.segments.find((segment) => segment.kind === "attention")!.count} attention`
      : "",
    batch.stalled ? `stalled ${duration(batch.stalled_for_seconds)}` : ""
  ].filter(Boolean).join(" · ");
  return (
    <button className={`batch-row ${detailed ? "detailed" : ""}`} onClick={onClick}>
      <div className="batch-name"><span className={`batch-glyph ${hasError ? "error" : ""}`}>{batch.cancelled_at ? "×" : batch.purged_at ? "✓" : batch.paused_at ? "Ⅱ" : "↗"}</span><div><strong>{batch.name}</strong><small>{batch.photo_count || 0} photo{batch.photo_count === 1 ? "" : "s"} · {batch.raw_count || 0} RAW · {batch.video_count || 0} video{batch.video_count === 1 ? "" : "s"} · <b>{status}</b>{batch.series_total ? ` · PART ${batch.series_index}/${batch.series_total}` : ""}</small></div></div>
      <div className="progress-cell">
        <div className="progress-line">
          <b>TX</b>
          <div className="transfer-meter" aria-label={`${transferPercent}% transferred`}>
            <i style={{ width: `${transferPercent}%` }} />
          </div>
          <span>{transferPercent.toFixed(1)}%</span>
        </div>
        <div className="progress-line">
          <b>FLOW</b>
          <div className="progress-meter" aria-label={`${progress.percent}% workflow progress`}>
            {progress.segments.map((segment) => <i key={segment.kind} className={segment.kind} style={{ width: `${(segment.count / progress.total) * 100}%` }} title={`${segment.count} ${label(segment.kind)}`} />)}
          </div>
          <span>{progress.percent.toFixed(1)}%</span>
        </div>
        <div className="progress-line">
          <b>SCAN</b>
          <div className="media-scan-meter" aria-label={`${mediaScan.percent}% scanned`}>
            <i style={{ width: `${mediaScan.percent}%` }} />
          </div>
          <span>{mediaScan.percent.toFixed(1)}%</span>
        </div>
        <small>{bytes(transferredBytes)} / {bytes(batch.total_bytes)} transferred · {mediaScan.completed}/{mediaScan.total} scan checks · {progress.ready}/{progress.total} ready · {batchEta(batch)}{activeDetail ? ` · ${activeDetail}` : ""}</small>
      </div>
      {detailed && <><span className="muted">{bytes(batch.total_bytes)}</span><span className="muted">{relativeTime(batch.created_at)}</span><span className="row-arrow">›</span></>}
    </button>
  );
}

function BatchSummaryRow({ batch, onClick }: { batch: Batch; onClick: () => void }) {
  const total = batch.file_count || Object.values(batch.states).reduce(
    (sum, count) => sum + (count || 0),
    0
  );
  const progress = batchProgress(batch.states, total);
  return <button className="batch-summary-row" onClick={onClick}>
    <span className="batch-glyph">↗</span>
    <span className="batch-summary-name"><strong>{batch.name}</strong><small>{batch.photo_count || 0} photos · {batch.raw_count || 0} RAW · {batch.video_count || 0} videos{batch.series_total ? ` · Part ${batch.series_index}/${batch.series_total}` : ""}</small></span>
    <span className="batch-summary-stat"><strong>{progress.ready}/{progress.total}</strong><small>ready</small></span>
    <span className="batch-summary-stat"><strong>{bytes(batch.total_bytes)}</strong><small>{batchEta(batch)}</small></span>
    <span className="row-arrow">›</span>
  </button>;
}

function BatchDrawer({ batch, close, reload, removed, retriggered, report }: { batch: Batch; close: () => void; reload: () => Promise<void>; removed: () => Promise<void>; retriggered: (batch: Batch) => Promise<void>; report: (message: string, type?: Notice["type"]) => void }) {
  const [busy, setBusy] = useState(false);
  const items = batch.items || [];
  const ready = items.length > 0 && items.every((item) => item.state === "awaiting_backup_confirmation");
  const confirmed = items.length > 0 && items.every((item) => ["confirmed_backed_up", "purged_from_pixel"].includes(item.state));
  const photosSearch = ready || confirmed
    ? googlePhotosBatchDateSearch(batch.id, items)
    : null;
  const retryable = items.some((item) => problemStates.includes(item.state));
  const cancellable = !batch.cancelled_at && !batch.confirmed_at && !batch.purged_at;
  const pausable = cancellable && !batch.paused_at && !ready;
  const resumable = cancellable && Boolean(batch.paused_at);
  const cancellationSettling = Boolean(
    batch.cancelled_at
    && (batch.processing || items.some((item) => item.state === "transferring"))
  );
  const cancelledNeedsCleanup = Boolean(
    batch.cancelled_at
    && items.some((item) => ["cancelled_on_pixel", "purge_failed"].includes(item.state))
  );
  const deletable = items.length > 0 && (
    (Boolean(batch.cancelled_at) && !cancellationSettling)
    ||
    items.every((item) => item.state === "queued" && item.attempts === 0)
    || items.every((item) => ["cancelled", "purged_from_pixel"].includes(item.state))
  );
  const cancelledCopiesMayRemain = Boolean(
    batch.cancelled_at
    && items.some((item) => !["cancelled", "purged_from_pixel"].includes(item.state))
  );
  const retriggerable = items.length > 0
    && items.every((item) => ["cancelled", "purged_from_pixel"].includes(item.state));
  async function action(run: () => Promise<unknown>, success: string) {
    setBusy(true);
    try { await run(); await reload(); report(success); }
    catch (error) { report(error instanceof Error ? error.message : "Action failed", "bad"); }
    finally { setBusy(false); }
  }
  async function removeBatch() {
    if (!window.confirm(`Delete the local batch entry for “${batch.name}”?`)) return;
    setBusy(true);
    try {
      await api.deleteBatch(batch.id);
      await removed();
      report("Batch records deleted");
    } catch (error) {
      report(error instanceof Error ? error.message : "Batch could not be deleted", "bad");
    } finally {
      setBusy(false);
    }
  }
  async function retriggerBatch() {
    if (!window.confirm(`Run “${batch.name}” again as a new batch?`)) return;
    setBusy(true);
    try {
      const next = await api.retriggerBatch(batch.id);
      await retriggered(next);
      report("New batch created and queued");
    } catch (error) {
      report(error instanceof Error ? error.message : "Batch could not be run again", "bad");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}>
      <aside className="drawer">
        <button className="drawer-close" onClick={close}><Icons.close /></button>
        <span className="page-kicker">BATCH / {batch.id.slice(0, 8)}</span>
        <h2>{batch.name}</h2>
        <p>{items.length} files · {bytes(batch.total_bytes)} · Created {dateTime(batch.created_at)}{batch.series_total ? ` · Part ${batch.series_index} of ${batch.series_total}` : ""} · {batchEta(batch)}</p>
        {photosSearch && <div className="safety-action google-photos-action batch-top-action"><div><strong>Review this batch part in Google Photos</strong><small>Date filter: {photosSearch.dateLabel}, based only on photo/RAW modified dates in this batch{batch.series_total ? ` part ${batch.series_index} of ${batch.series_total}` : ""}.</small></div><a className="secondary" href={photosSearch.href} target="_blank" rel="noreferrer"><Icons.photos /> Open Google Photos</a></div>}
        {batch.paused_at && <div className="series-note"><strong>{batch.processing ? "Pause requested" : "Batch paused"}</strong><small>{batch.processing ? "The current transfer and integrity check will finish safely. Pixel Relay will not start another file from this batch." : `No additional files will transfer until this batch is resumed. Paused ${relativeTime(batch.paused_at)}.`}</small></div>}
        {batch.stalled && <div className="stalled-alert"><Icons.warning /><div><strong>Batch activity appears stalled</strong><small>{batch.stall_reason || "No progress has been recorded"} for {duration(batch.stalled_for_seconds)}. Last activity {relativeTime(batch.last_activity_at)}.</small></div></div>}
        {batch.series_total && <div className="series-note"><strong>{batch.split_reason === "pixel_storage" ? "Storage-aware batch series" : "Automatic batch series"}</strong><small>{batch.series_index === 1 ? `Pixel Relay split this selection into ${batch.series_total} balanced parts. A later part may start as soon as earlier parts are fully staged and the entire next part fits above the storage reserve; Google Photos confirmation is not required to continue.` : `This part waits until every earlier part is fully staged. It can then start before those parts are confirmed whenever its entire remaining payload fits above the storage reserve.`}{batch.planned_capacity_bytes ? ` Planned limit: ${bytes(batch.planned_capacity_bytes)} per part.` : ""}</small></div>}
        {batch.storage_blocked && <div className="series-note"><strong>Waiting for enough Pixel space</strong><small>This batch needs {bytes(batch.total_bytes)} before it starts, while {bytes(batch.storage_available_bytes)} is currently available above the safety reserve. Confirm and purge an earlier Pixel copy, or free other space, to continue.</small></div>}
        {batch.performance && batch.performance.sample_count > 0 && <div className="performance-panel">
          <span><small>Transfer + checksum</small><strong>{batch.performance.transfer_rate_bytes_per_second ? `${bytes(batch.performance.transfer_rate_bytes_per_second)}/s` : "Calculating…"}</strong><em>{duration(batch.performance.transfer_seconds)}</em></span>
          <span><small>Media scans</small><strong>{batch.performance.scanned_count.toLocaleString()}</strong><em>{batch.performance.average_scan_seconds == null ? "In progress" : `${batch.performance.average_scan_seconds.toFixed(1)} sec average`}</em></span>
          <span><small>Measured data</small><strong>{bytes(batch.performance.transferred_bytes)}</strong><em>{batch.performance.sample_count} completed files</em></span>
        </div>}
        <div className="state-summary">
          {Object.entries(batch.states).map(([state, count]) => <span key={state} className={`state ${state}`}>{label(state)} <b>{count}</b></span>)}
        </div>
        <div className="item-list">
          {items.map((item) => <BatchItemRow key={item.id} item={item} />)}
        </div>
        <div className="drawer-actions">
          <a className="secondary manifest-download" href={`/api/v1/batches/${batch.id}/manifest`} download><Icons.audit /> Download integrity manifest</a>
          {pausable && <div className="safety-action"><div><strong>Pause batch</strong><small>Stops before the next file. A file currently transferring will finish its copy, checksum, and media scan first.</small></div><button className="secondary" disabled={busy} onClick={() => void action(() => api.pauseBatch(batch.id), batch.processing ? "Pause requested; the current file will finish safely" : "Batch paused")}>Pause batch</button></div>}
          {resumable && <div className="safety-action"><div><strong>Resume batch</strong><small>Returns this batch to the queue without resetting completed files or transfer history.</small></div><button className="primary" disabled={busy} onClick={() => void action(() => api.resumeBatch(batch.id), "Batch resumed")}>Resume batch</button></div>}
          {cancellable && <div className="safety-action"><div><strong>Cancel batch</strong><small>Stops remaining queue work. A transfer already in progress will stop after its current integrity check, and any Pixel copy will remain tracked for cleanup.</small></div><button className="secondary" disabled={busy} onClick={() => { if (window.confirm(`Cancel “${batch.name}”?`)) void action(() => api.cancelBatch(batch.id), "Batch cancelled"); }}>Cancel batch</button></div>}
          {cancellationSettling && <div className="safety-action"><div><strong>Cancellation in progress</strong><small>Waiting for the active device operation to return safely.</small></div></div>}
          {retryable && <button className="secondary" disabled={busy} onClick={() => void action(() => api.retryBatch(batch.id), "Retry queued")}><Icons.refresh /> Retry failed items</button>}
          {ready && <div className="safety-action"><div><strong>1. Confirm backup</strong><small>After Google Photos reports that backup is complete, confirm the entire batch here.</small></div><button className="primary amber" disabled={busy} onClick={() => void action(() => api.confirmBatch(batch.id), "Batch marked as backed up")}>I verified this batch</button></div>}
          {confirmed && !batch.purged_at && <div className="safety-action danger-zone"><div><strong>2. Purge Pixel copies</strong><small>Removes this batch’s Pixel copies after confirmation. Source files are never touched.</small></div><button className="danger" disabled={busy} onClick={() => { if (window.confirm(`Purge Pixel copies for “${batch.name}”?`)) void action(() => api.purgeBatch(batch.id), "Pixel copies purged"); }}>Purge Pixel copies</button></div>}
          {cancelledNeedsCleanup && !cancellationSettling && !batch.purged_at && <div className="safety-action danger-zone"><div><strong>Remove cancelled Pixel copies</strong><small>This removes only copies associated with the cancelled batch.</small></div><button className="danger" disabled={busy} onClick={() => { if (window.confirm(`Remove cancelled Pixel copies for “${batch.name}”?`)) void action(() => api.purgeBatch(batch.id), "Cancelled Pixel copies removed"); }}>Clean up Pixel copies</button></div>}
          {retriggerable && <div className="safety-action retrigger-action"><div><strong>Run this batch again</strong><small>Creates a new queued batch from the same source-file records and preserves this batch’s history.</small></div><button className="secondary" disabled={busy} onClick={() => void retriggerBatch()}><Icons.refresh /> Run again</button></div>}
          {deletable && <div className="safety-action danger-zone"><div><strong>{batch.cancelled_at ? "Delete cancelled batch entry" : "Delete batch records"}</strong><small>This deletes the local batch entry and its history.{cancelledCopiesMayRemain ? " Copies already transferred to the Pixel are not removed." : ""}</small></div><button className="danger" disabled={busy} onClick={() => void removeBatch()}>{batch.cancelled_at ? "Delete cancelled batch" : "Delete batch"}</button></div>}
        </div>
      </aside>
    </div>
  );
}

function BatchItemRow({ item }: { item: BatchItem }) {
  const transferred = Math.min(item.size, item.transfer_bytes || 0);
  const transferPercent = item.size ? Math.round((transferred / item.size) * 1000) / 10 : 0;
  return <div className="item-row"><span className={`state-dot ${item.state}`} /><div><strong title={item.path}>{shortPath(item.path)}</strong><small><b className={`kind ${item.media_kind}`}>{item.media_kind}</b> · {bytes(item.size)} · {item.sha256.slice(0, 12)}…</small>{item.state === "transferring" && <div className="item-transfer"><i style={{ width: `${transferPercent}%` }} /><span>{bytes(transferred)} / {bytes(item.size)} · {transferPercent.toFixed(1)}%</span></div>}{item.state === "staged_on_pixel" && <div className="item-scan"><i /><span>MediaStore scan in progress</span></div>}{item.error_detail && <span className="item-error"><em>{item.error_detail}</em><CopyButton text={item.error_detail} /></span>}</div><span className={`state ${item.state}`}>{item.state === "staged_on_pixel" ? "Scanning media" : label(item.state)}</span></div>;
}

function Sources({ report }: { report: (message: string, type?: Notice["type"]) => void }) {
  const [roots, setRoots] = useState<SourceRoot[]>([]);
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [excludedBatchSources, setExcludedBatchSources] = useState<Set<number>>(new Set());
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [batchName, setBatchName] = useState("");
  const [mediaFilter, setMediaFilter] = useState<"all" | "photo" | "raw" | "video">("all");
  const [historyFilter, setHistoryFilter] = useState<"all" | "new" | "processed">("all");
  const [batchPlan, setBatchPlan] = useState<BatchPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState("");
  const [busy, setBusy] = useState(false);
  const [visibleFileLimit, setVisibleFileLimit] = useState(sourceFileRenderChunk);
  const [serverBrowserOpen, setServerBrowserOpen] = useState(false);
  const [scanProgress, setScanProgress] = useState<Record<number, ScanProgress>>({});
  const fileInput = useRef<HTMLInputElement>(null);
  const load = async () => { const [nextRoots, nextFiles] = await Promise.all([api.sources(), api.files()]); setRoots(nextRoots); setFiles(nextFiles); };
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    const stream = new EventSource("/api/v1/events");
    const onScan = (event: Event) => {
      try {
        const envelope = JSON.parse((event as MessageEvent<string>).data) as { data: ScanProgress };
        const progress = envelope.data;
        setScanProgress((current) => ({ ...current, [progress.root_id]: progress }));
      } catch {
        // Ignore a malformed event and keep the last valid scan reading visible.
      }
    };
    stream.addEventListener("scan", onScan);
    return () => {
      stream.removeEventListener("scan", onScan);
      stream.close();
    };
  }, []);
  const selectedIds = [...selected].sort((left, right) => left - right);
  const selectionKey = selectedIds.join(",");
  useEffect(() => {
    if (!selectedIds.length) {
      setBatchPlan(null);
      setPlanError("");
      setPlanning(false);
      return;
    }
    let stopped = false;
    const timer = window.setTimeout(() => {
      setPlanning(true);
      setPlanError("");
      void api.planBatch(batchName.trim() || undefined, selectedIds)
        .then((plan) => {
          if (!stopped) setBatchPlan(plan);
        })
        .catch((error) => {
          if (!stopped) {
            setBatchPlan(null);
            setPlanError(error instanceof Error ? error.message : "Unable to plan batch");
          }
        })
        .finally(() => {
          if (!stopped) setPlanning(false);
        });
    }, 250);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [selectionKey, batchName]);
  async function act<T>(run: () => Promise<T>, success: string | ((result: T) => string)) {
    setBusy(true);
    try { const result = await run(); await load(); report(typeof success === "function" ? success(result) : success); }
    catch (error) { report(error instanceof Error ? error.message : "Action failed", "bad"); }
    finally { setBusy(false); }
  }
  const availableRootIds = new Set(roots.filter((root) => root.available).map((root) => root.id));
  const eligibleFiles = files.filter(
    (file) => availableRootIds.has(file.root_id) && !excludedBatchSources.has(file.root_id)
  );
  const matchesMediaFilter = (file: SourceFile) => mediaFilter === "all"
    || (mediaFilter === "raw" && isRaw(file))
    || (mediaFilter === "photo" && file.media_kind === "photo" && !isRaw(file))
    || (mediaFilter === "video" && file.media_kind === "video");
  const mediaEligibleFiles = eligibleFiles.filter(matchesMediaFilter);
  const historyEligibleFiles = eligibleFiles.filter(
    (file) => historyFilter === "all"
      || (historyFilter === "new" && !(file.previous_batch_count || 0))
      || (historyFilter === "processed" && Boolean(file.previous_batch_count))
  );
  const selectedBytes = eligibleFiles.filter((file) => selected.has(file.id)).reduce((sum, file) => sum + file.size, 0);
  const visibleFiles = historyEligibleFiles.filter(matchesMediaFilter);
  const renderedFiles = visibleFiles.slice(0, visibleFileLimit);
  const remainingVisibleFiles = visibleFiles.length - renderedFiles.length;
  const allVisibleSelected = visibleFiles.length > 0
    && visibleFiles.every((file) => selected.has(file.id));
  const selectedFolderNames = [...new Set(
    eligibleFiles
      .filter((file) => selected.has(file.id))
      .map((file) => parentFolderName(file.path))
  )];
  const defaultArchiveName = selectedFolderNames.length === 1
    ? selectedFolderNames[0]
    : selectedFolderNames.length > 1
      ? `${selectedFolderNames.length} folder names`
      : "Select media to choose folder names";
  const photoCount = historyEligibleFiles.filter((file) => file.media_kind === "photo" && !isRaw(file)).length;
  const rawCount = historyEligibleFiles.filter(isRaw).length;
  const videoCount = historyEligibleFiles.filter((file) => file.media_kind === "video").length;
  const newCount = mediaEligibleFiles.filter((file) => !(file.previous_batch_count || 0)).length;
  const processedCount = mediaEligibleFiles.length - newCount;
  const availableSourceCount = roots.filter((root) => root.available).length;
  const includedSourceCount = roots.filter(
    (root) => root.available && !excludedBatchSources.has(root.id)
  ).length;
  function toggleBatchSource(rootId: number) {
    const removing = !excludedBatchSources.has(rootId);
    setVisibleFileLimit(sourceFileRenderChunk);
    setExcludedBatchSources((previous) => {
      const next = new Set(previous);
      removing ? next.add(rootId) : next.delete(rootId);
      return next;
    });
    if (removing) {
      const sourceFileIds = new Set(
        files.filter((file) => file.root_id === rootId).map((file) => file.id)
      );
      setSelected((previous) => new Set(
        [...previous].filter((fileId) => !sourceFileIds.has(fileId))
      ));
    }
  }
  async function removeRoot(root: SourceRoot) {
    if (!window.confirm(`Remove “${root.name}” from TheDoPixel?\n\nThe folder and every original file will remain untouched.`)) return;
    await act(
      async () => {
        await api.removeSource(root.id);
        setSelected(new Set());
      },
      `Source removed from TheDoPixel; originals were not deleted`
    );
  }
  function startScan(root: SourceRoot, fullVerify: boolean) {
    setScanProgress((current) => ({
      ...current,
      [root.id]: {
        root_id: root.id,
        phase: "enumerating",
        processed: 0,
        total: 0,
        examined: 0,
        discovered: 0,
        skipped: 0,
        cached: 0,
        hashed: 0,
        full_verify: fullVerify,
        complete: false,
        failed: false
      }
    }));
    void act(
      async () => {
        const result = await api.scanSource(root.id, fullVerify);
        if (result.skipped.length) {
          setScanProgress((current) => ({
            ...current,
            [root.id]: { ...current[root.id], issues: result.skipped }
          }));
        }
        return result;
      },
      (result) => result.skipped.length
        ? `Scan complete with ${result.skipped.length} issue${result.skipped.length === 1 ? "" : "s"}: ${root.name}`
        : `Scan complete: ${root.name} · ${result.stats.cached} cached · ${result.stats.hashed} hashed`
    );
  }
  return (
    <>
      <div className="page-heading"><div><div className="page-kicker">MEDIA SOURCES</div><h1>Sources <span>& imports</span></h1><p>Browse folders on the server, or upload media from this browser client.</p></div><button className="primary" onClick={() => fileInput.current?.click()}><Icons.upload /> Upload media</button><input ref={fileInput} hidden type="file" accept="image/*,video/*,.dng,.cr2,.cr3,.nef,.nrw,.arw,.srf,.sr2,.raf,.orf,.rw2,.pef,.x3f,.3fr,.erf,.mef,.mos,.mrw,.raw,.rwl,.iiq" multiple onChange={(event) => { const uploads = [...(event.target.files || [])]; void act(async () => { for (const file of uploads) await api.upload(file); }, `${uploads.length} original${uploads.length === 1 ? "" : "s"} imported`); event.target.value = ""; }} /></div>
      <div className="source-grid">
        <section className="panel">
          <div className="panel-head"><div><span className="panel-kicker">ALLOWLISTED ROOTS</span><h2>Host & network folders</h2></div></div>
          <div className="root-list">
            {roots.map((root) => {
              const progress = scanProgress[root.id];
              const percent = progress?.total
                ? Math.min(100, (progress.processed / progress.total) * 100)
                : 0;
              const scanning = Boolean(progress && !progress.complete);
              return <div className="root-row" key={root.id}>
                <span className={`root-icon ${root.available ? "" : "offline"}`}><Icons.source /></span>
                <div className="root-details">
                  <strong>{root.name}</strong>
                  <small title={root.path}>{root.path}</small>
                  {root.issue && <small className="root-issue"><Icons.warning /> <span>{root.issue}</span></small>}
                </div>
                <span className={`availability ${root.available ? "" : "bad"}`}>
                  {root.available ? "AVAILABLE" : root.issue_code === "permission_denied" ? "ACCESS BLOCKED" : "OFFLINE"}
                </span>
                <div className="root-scan-control">
                  <div className="scan-actions">
                    <button
                      className="secondary small"
                      disabled={!root.available || busy}
                      onClick={() => startScan(root, false)}
                    >
                      {scanning
                        ? progress.phase === "saving"
                          ? "Saving…"
                          : progress.total
                            ? `${progress.full_verify ? "Verifying" : "Scanning"} ${percent.toFixed(1)}%`
                            : "Enumerating…"
                        : "Scan now"}
                    </button>
                    <button
                      className="secondary small scan-full"
                      disabled={!root.available || busy}
                      title="Recalculate SHA-256 for every media file"
                      onClick={() => startScan(root, true)}
                    >
                      Full verify
                    </button>
                  </div>
                  {progress && <div className={`scan-progress ${progress.complete ? "complete" : ""} ${progress.failed ? "failed" : ""}`}>
                    <div className={`scan-progress-track ${!progress.total && !progress.complete ? "indeterminate" : ""}`}>
                      <i style={{ width: `${percent}%` }} />
                    </div>
                    <small>
                      {progress.failed
                        ? progress.message || "Scan failed"
                        : progress.complete
                          ? `${progress.discovered.toLocaleString()} media · ${(progress.cached ?? 0).toLocaleString()} cached · ${(progress.hashed ?? 0).toLocaleString()} hashed · ${progress.skipped.toLocaleString()} issues`
                          : progress.total
                            ? `${progress.processed.toLocaleString()} / ${progress.total.toLocaleString()} · ${(progress.cached ?? 0).toLocaleString()} cached · ${(progress.hashed ?? 0).toLocaleString()} hashed`
                            : `${(progress.examined ?? 0).toLocaleString()} files examined · reading folder tree…`}
                    </small>
                    {!progress.complete && progress.current_name && <em title={progress.current_name}>{progress.current_name}</em>}
                    {progress.issues?.slice(0, 3).map((issue) => <em className="scan-issue" title={issue.path} key={`${issue.path}:${issue.reason}`}>{issue.reason} · {issue.path}</em>)}
                    {progress.skipped > (progress.issues?.length ?? 0) && <em className="scan-issue">+{progress.skipped - (progress.issues?.length ?? 0)} more issues</em>}
                  </div>}
                </div>
                <button className="danger small" disabled={busy} onClick={() => void removeRoot(root)}>Remove</button>
              </div>;
            })}
            {!roots.length && <Empty icon="＋" title="No source roots" text="Add a local drive or mounted network folder below." />}
          </div>
          <form className="inline-form" onSubmit={(event) => { event.preventDefault(); void act(async () => { await api.createSource(name.trim() || pathBaseName(path), path); setName(""); setPath(""); }, "Source root added"); }}>
            <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder={pathBaseName(path) || "Family archive"} /></label>
            <label className="grow">Absolute path<div className="path-picker"><input value={path} onChange={(e) => setPath(e.target.value)} placeholder={"E:\\Photos or /Volumes/NAS/Photos"} required /><button type="button" className="secondary small" onClick={() => setServerBrowserOpen(true)}>Browse server…</button></div></label>
            <button className="secondary" disabled={busy}>Add root</button>
          </form>
        </section>
        <section className="panel files-panel">
          <div className="panel-head file-panel-head"><div><span className="panel-kicker">DISCOVERED / NOT IN FLIGHT</span><h2>Ready for a batch</h2></div><span className="selection-count">{selected.size.toLocaleString()} selected · {bytes(selectedBytes)}</span></div>
          <div className="batch-source-picker">
            <div className="batch-source-heading">
              <div><strong>Batch sources</strong><small>Choose which roots are eligible for this batch.</small></div>
              <span>{includedSourceCount} of {availableSourceCount} available included</span>
            </div>
            <div className="batch-source-options">
              {roots.map((root) => {
                const included = root.available && !excludedBatchSources.has(root.id);
                const readyCount = files.filter((file) => file.root_id === root.id).length;
                return <label className={`${included ? "included" : ""} ${root.available ? "" : "unavailable"}`} key={root.id} title={root.issue || undefined}>
                  <input type="checkbox" checked={included} disabled={!root.available} onChange={() => toggleBatchSource(root.id)} />
                  <span><strong>{root.name}</strong><small>{root.issue || `${readyCount.toLocaleString()} ready`}</small></span>
                </label>;
              })}
              {!roots.length && <small className="batch-source-empty">Add or upload to a source before creating a batch.</small>}
            </div>
            {roots.length > 1 && <div className="batch-source-actions">
              <button type="button" disabled={includedSourceCount === availableSourceCount} onClick={() => { setExcludedBatchSources(new Set()); setVisibleFileLimit(sourceFileRenderChunk); }}>Include all available</button>
              <button type="button" disabled={includedSourceCount === 0} onClick={() => { setExcludedBatchSources(new Set(roots.map((root) => root.id))); setSelected(new Set()); setVisibleFileLimit(sourceFileRenderChunk); }}>Include none</button>
            </div>}
          </div>
          <div className="history-filter" role="group" aria-label="Transfer history">
            <span>History</span>
            <button type="button" className={historyFilter === "all" ? "active" : ""} onClick={() => { setHistoryFilter("all"); setVisibleFileLimit(sourceFileRenderChunk); }}>All <b>{mediaEligibleFiles.length.toLocaleString()}</b></button>
            <button type="button" className={historyFilter === "new" ? "active" : ""} onClick={() => { setHistoryFilter("new"); setVisibleFileLimit(sourceFileRenderChunk); }}>Never batched <b>{newCount.toLocaleString()}</b></button>
            <button type="button" className={historyFilter === "processed" ? "active" : ""} onClick={() => { setHistoryFilter("processed"); setVisibleFileLimit(sourceFileRenderChunk); }}>Previously processed <b>{processedCount.toLocaleString()}</b></button>
          </div>
          <div className="media-tabs" role="group" aria-label="Media type">
            <button className={mediaFilter === "all" ? "active" : ""} onClick={() => { setMediaFilter("all"); setVisibleFileLimit(sourceFileRenderChunk); }}>All <b>{historyEligibleFiles.length.toLocaleString()}</b></button>
            <button className={mediaFilter === "photo" ? "active" : ""} onClick={() => { setMediaFilter("photo"); setVisibleFileLimit(sourceFileRenderChunk); }}>Photos <b>{photoCount.toLocaleString()}</b></button>
            <button className={mediaFilter === "raw" ? "active" : ""} onClick={() => { setMediaFilter("raw"); setVisibleFileLimit(sourceFileRenderChunk); }}>RAW <b>{rawCount.toLocaleString()}</b></button>
            <button className={mediaFilter === "video" ? "active" : ""} onClick={() => { setMediaFilter("video"); setVisibleFileLimit(sourceFileRenderChunk); }}>Videos <b>{videoCount.toLocaleString()}</b></button>
            <span className="selection-actions">
              <button type="button" disabled={!visibleFiles.length || allVisibleSelected} onClick={() => setSelected((previous) => new Set([...previous, ...visibleFiles.map((file) => file.id)]))}>{mediaFilter === "all" ? "Select all" : mediaFilter === "raw" ? "Select all RAW" : `Select all ${mediaFilter}s`}</button>
              <button type="button" disabled={!selected.size} onClick={() => setSelected(new Set())}>Clear</button>
            </span>
          </div>
          <div className="file-list">
            {renderedFiles.map((file) => <label className="file-row" key={file.id}><input type="checkbox" checked={selected.has(file.id)} onChange={() => setSelected((previous) => { const next = new Set(previous); next.has(file.id) ? next.delete(file.id) : next.add(file.id); return next; })} /><span className={`file-type ${isRaw(file) ? "raw" : file.media_kind}`}>{isRaw(file) ? "RAW" : file.media_kind === "photo" ? "PHOTO" : "VIDEO"}</span><div><strong title={file.path}>{shortPath(file.path)}</strong><small>{file.root_name} · {file.extension.slice(1).toUpperCase()} · {bytes(file.size)}{file.duplicate_content ? " · Duplicate content" : ""}{file.previously_purged ? " · Previously purged" : file.previously_confirmed ? " · Previously confirmed" : file.previous_batch_count ? ` · In ${file.previous_batch_count} prior batch${file.previous_batch_count === 1 ? "" : "es"}` : ""}</small></div></label>)}
            {remainingVisibleFiles > 0 && <div className="file-list-more"><span>Showing {renderedFiles.length.toLocaleString()} of {visibleFiles.length.toLocaleString()}</span><button type="button" className="secondary small" onClick={() => setVisibleFileLimit((current) => current + sourceFileRenderChunk)}>Show next {Math.min(sourceFileRenderChunk, remainingVisibleFiles).toLocaleString()}</button></div>}
            {!visibleFiles.length && <Empty icon="○" title={`No ${mediaFilter === "all" ? "media" : `${mediaFilter}s`} available`} text={includedSourceCount ? "Scan an included source or upload media to begin." : "Choose at least one batch source above."} />}
          </div>
          {(planning || batchPlan || planError) && <section className={`batch-plan ${planError ? "error" : ""}`}>
            <div className="batch-plan-head">
              <div><span className="panel-kicker">PREFLIGHT</span><strong>{planning ? "Calculating batch plan…" : planError ? "Plan needs attention" : `${batchPlan?.batch_count} batch part${batchPlan?.batch_count === 1 ? "" : "s"} planned`}</strong></div>
              {batchPlan?.estimated_seconds != null && <span>Estimated {duration(batchPlan.estimated_seconds)}</span>}
            </div>
            {planError && <p>{planError}</p>}
            {batchPlan && !planning && <>
              <div className="batch-plan-stats">
                <span>Unique media<b>{batchPlan.unique_content_count.toLocaleString()}</b></span>
                <span>Total size<b>{bytes(batchPlan.total_bytes)}</b></span>
                <span>Per-part limit<b>{bytes(batchPlan.batch_byte_limit)}</b></span>
                <span>Pixel reserve<b>{bytes(batchPlan.storage_reserve_bytes)}</b></span>
              </div>
              {(batchPlan.duplicate_selection_count > 0 || batchPlan.previously_processed_count > 0) && <div className="batch-plan-warning">
                {batchPlan.duplicate_selection_count > 0 && <span>{batchPlan.duplicate_selection_count} duplicate selection{batchPlan.duplicate_selection_count === 1 ? "" : "s"} will be collapsed by content hash.</span>}
                {batchPlan.previously_processed_count > 0 && <span>{batchPlan.previously_processed_count} file{batchPlan.previously_processed_count === 1 ? "" : "s"} appeared in an earlier batch; {batchPlan.previously_purged_count} were previously purged.</span>}
              </div>}
              <div className="batch-plan-parts">
                {batchPlan.parts.map((part, index) => <span key={`${part.folder}-${index}`}><b>{index + 1}</b><span><strong>{part.name}</strong><small>{part.file_count.toLocaleString()} files · {bytes(part.total_bytes)} · {part.photo_count} photos · {part.raw_count} RAW · {part.video_count} videos</small></span></span>)}
              </div>
            </>}
          </section>}
          <form className="batch-builder" onSubmit={(event) => { event.preventDefault(); void act(async () => { const batches = await api.createBatch(batchName.trim() || undefined, [...selected]); setBatchName(""); setSelected(new Set()); return batches; }, (batches) => batches.length > 1 ? `Selection split into ${batches.length} balanced batches` : "Batch created and queued"); }}>
            <input value={batchName} onChange={(e) => setBatchName(e.target.value)} placeholder={`Archive name · ${defaultArchiveName}`} />
            <button type="button" className="secondary" disabled={!batchName} onClick={() => setBatchName("")}>Use folder {selectedFolderNames.length === 1 ? "name" : "names"}</button>
            <button className="primary" disabled={!selected.size || busy || planning || Boolean(planError)}>Create batch <span>→</span></button>
          </form>
        </section>
      </div>
      {serverBrowserOpen && <ServerDirectoryBrowser
        title="Choose a source folder"
        initialPath={path}
        close={() => setServerBrowserOpen(false)}
        select={(selectedPath) => {
          setPath(selectedPath);
          if (!name.trim()) setName(pathBaseName(selectedPath));
          setServerBrowserOpen(false);
        }}
      />}
    </>
  );
}

function Audit() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [logs, setLogs] = useState<SystemLog[]>([]);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextEntries, nextLogs] = await Promise.all([api.audit(), api.logs()]);
      setEntries(nextEntries);
      setLogs(nextLogs);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return <>
    <div className="page-heading"><div><div className="page-kicker">APPEND-ONLY HISTORY</div><h1>Audit <span>log</span></h1><p>Operator actions and detailed structured diagnostics.</p></div><button className="secondary" disabled={loading} onClick={() => void load()}><Icons.refresh /> {loading ? "Refreshing…" : "Refresh logs"}</button></div>
    <section className="panel audit-list">
      <div className="panel-head"><div><span className="panel-kicker">OPERATOR ACTIONS</span><h2>Safety audit</h2></div></div>
      {entries.map((entry) => <div className="audit-row" key={entry.id}><span className="audit-id">#{entry.id}</span><span className="audit-symbol">·</span><div><strong>{label(entry.action.replaceAll(".", "_"))}</strong><small>{entry.username || "System"} · {entry.target_type}{entry.target_id ? ` / ${entry.target_id.slice(0, 12)}` : ""}</small></div><time>{dateTime(entry.created_at)}</time></div>)}
      {!entries.length && <Empty icon="≡" title="No audit events yet" text="Safety-sensitive activity will appear here." />}
    </section>
    <section className="panel audit-list system-log">
      <div className="panel-head"><div><span className="panel-kicker">SERVICE OUTPUT</span><h2>Operational log</h2></div></div>
      {logs.map((entry, index) => <div className="audit-row log-row" key={`${entry.timestamp}-${index}`}><span className={`log-level ${entry.level.toLowerCase()}`}>{entry.level}</span><span className="audit-symbol">·</span><div><strong>{entry.message}</strong><small>{entry.logger}</small>{entry.context && <pre className="log-context">{JSON.stringify(entry.context, null, 2)}</pre>}{entry.exception && <pre>{entry.exception}</pre>}</div><time>{dateTime(entry.timestamp)}</time></div>)}
      {!logs.length && <Empty icon="~" title="No service messages" text="Runtime diagnostics will appear here." />}
    </section>
  </>;
}

function Settings({ report }: { report: (message: string, type?: Notice["type"]) => void }) {
  const [settings, setSettings] = useState<RelaySettings | null>(null);
  const [draft, setDraft] = useState<RelaySettings | null>(null);
  const [ftpPassword, setFtpPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [storageOptions, setStorageOptions] = useState<StorageOptions | null>(null);
  const [storageRefreshing, setStorageRefreshing] = useState(false);
  const [serverBrowserOpen, setServerBrowserOpen] = useState(false);
  const [speedTesting, setSpeedTesting] = useState(false);
  const [speedTest, setSpeedTest] = useState<AdbSpeedTestResult | null>(null);
  const [ftpSpeedTesting, setFtpSpeedTesting] = useState(false);
  const [ftpSpeedTest, setFtpSpeedTest] = useState<FtpSpeedTestResult | null>(null);
  useEffect(() => {
    void api.settings().then((value) => {
      const normalized = {
        ...value,
        ftp_host: value.ftp_host || "192.168.1.35",
        ftp_port: value.ftp_port || 21,
        ftp_username: value.ftp_username ?? "anonymous",
        ftp_password_configured: value.ftp_password_configured ?? false,
        ftp_destination_root: value.ftp_destination_root || "/DCIM/Camera/PixelRelay",
        reserve_bytes: value.pixel_internal_storage_bytes
          ? Math.min(value.reserve_bytes, value.pixel_internal_storage_bytes)
          : value.reserve_bytes
      };
      setSettings(normalized);
      setDraft(normalized);
    });
    // Fetch this separately: a detailed ADB probe may wait behind a background
    // adoption, but the rest of Settings (including job status) should render.
    void api.storageOptions(true).then(setStorageOptions).catch(() => undefined);
  }, []);
  if (!settings) return <SectionLoader />;
  if (!draft) return <SectionLoader />;
  const form = draft;
  const savedConnectionMode = settings.connection_mode;
  const ftpConfigDirty = Boolean(
    ftpPassword
    || form.ftp_host !== settings.ftp_host
    || form.ftp_port !== settings.ftp_port
    || form.ftp_username !== settings.ftp_username
    || form.ftp_destination_root !== settings.ftp_destination_root
  );
  const ftpTestSettings = () => ({
    ftp_host: form.ftp_host,
    ftp_port: form.ftp_port,
    ftp_username: form.ftp_username,
    ...(ftpPassword ? { ftp_password: ftpPassword } : {}),
    ftp_destination_root: form.ftp_destination_root
  });
  const update = <K extends keyof RelaySettings>(key: K, value: RelaySettings[K]) =>
    setDraft((current) => current ? { ...current, [key]: value } : current);
  async function refreshStorageOptions() {
    setStorageRefreshing(true);
    try {
      await api.refreshDevice();
      setStorageOptions(await api.storageOptions(true));
      report("Pixel storage list refreshed");
    } catch (error) {
      report(error instanceof Error ? error.message : "Storage list could not be refreshed", "bad");
    } finally {
      setStorageRefreshing(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      if (
        form.connection_mode === "ftp"
        && (savedConnectionMode !== "ftp" || ftpConfigDirty)
      ) {
        await api.ftpConnectionTest(ftpTestSettings());
      }
      const saved = await api.updateSettings({
        device_serial: form.device_serial,
        expected_primary_uuid: form.expected_primary_uuid,
        connection_mode: form.connection_mode,
        ftp_host: form.ftp_host,
        ftp_port: form.ftp_port,
        ftp_username: form.ftp_username,
        ...(ftpPassword ? { ftp_password: ftpPassword } : {}),
        ftp_destination_root: form.ftp_destination_root,
        destination_root: form.destination_root,
        import_root: form.import_root || "",
        max_batch_files: form.max_batch_files,
        max_batch_bytes: form.max_batch_bytes,
        reserve_bytes: form.reserve_bytes,
        pause_temperature_c: form.pause_temperature_c,
        resume_temperature_c: form.resume_temperature_c
      });
      const merged = { ...form, ...saved };
      setSettings(merged);
      setDraft(merged);
      void api.storageOptions().then(setStorageOptions).catch(() => undefined);
      setFtpPassword("");
      report("Settings saved");
    } catch (error) {
      report(error instanceof Error ? error.message : "Settings could not be saved", "bad");
    } finally {
      setSaving(false);
    }
  }
  async function runAdbSpeedTest() {
    setSpeedTesting(true);
    try {
      const result = await api.adbSpeedTest();
      setSpeedTest(result);
      report(`ADB transfer test: ${bytes(result.bytes_per_second)}/s`);
    } catch (error) {
      report(error instanceof Error ? error.message : "ADB speed test failed", "bad");
    } finally {
      setSpeedTesting(false);
    }
  }
  async function runFtpSpeedTest() {
    setFtpSpeedTesting(true);
    try {
      const result = await api.ftpSpeedTest(ftpTestSettings());
      setFtpSpeedTest(result);
      report(`FTP upload test: ${bytes(result.upload_bytes_per_second)}/s`);
    } catch (error) {
      report(error instanceof Error ? error.message : "FTP speed test failed", "bad");
    } finally {
      setFtpSpeedTesting(false);
    }
  }
  return <>
    <form className="settings-form" onSubmit={save}>
      <div className="page-heading"><div><div className="page-kicker">APPLIANCE CONFIGURATION</div><h1>Safety <span>& settings</span></h1><p>Change runtime controls here; each save is recorded in the audit log.</p></div><button className="primary" disabled={saving}>{saving ? "Saving…" : "Save settings"}<span>→</span></button></div>
      <div className="settings-grid">
        <section className="panel setting-card editable-card">
          <span className="panel-kicker">PIXEL TARGET</span>
          <h2>How TheDoPixel connects</h2>
          <label>Connection method
            <select value={form.connection_mode} onChange={(event) => update("connection_mode", event.target.value as "network" | "usb" | "ftp")}>
              <option value="usb">USB · first authorized device</option>
              <option value="network">Network ADB · use Network settings</option>
              <option value="ftp">FTP transfers · Network ADB controls</option>
            </select>
          </label>
          {(form.connection_mode === "network" || form.connection_mode === "ftp") && <label>Network ADB serial<input value={form.device_serial} onChange={(event) => update("device_serial", event.target.value)} placeholder="192.168.1.35:5555" /></label>}
          {form.connection_mode === "ftp" && <div className="ftp-fields">
            <div className="editable-grid">
              <label>FTP host<input value={form.ftp_host} onChange={(event) => update("ftp_host", event.target.value)} placeholder="192.168.1.35" /></label>
              <label>FTP port<input
                type="number"
                min={1}
                max={65535}
                step={1}
                inputMode="numeric"
                value={form.ftp_port}
                onChange={(event) => {
                  const port = event.currentTarget.valueAsNumber;
                  if (Number.isInteger(port)) update("ftp_port", port);
                }}
              /></label>
              <label>Username<input value={form.ftp_username} onChange={(event) => update("ftp_username", event.target.value)} /></label>
              <label>Password<input type="password" value={ftpPassword} onChange={(event) => setFtpPassword(event.target.value)} placeholder={form.ftp_password_configured ? "Configured · leave blank to keep" : "Optional"} /></label>
            </div>
            <label>FTP destination<input value={form.ftp_destination_root} onChange={(event) => update("ftp_destination_root", event.target.value)} placeholder="/DCIM/Camera/PixelRelay" /></label>
            <div className="connection-speed-test ftp-speed-test">
              <div><strong>FTP connectivity and speed</strong><small>Tests the values currently entered above without saving them. It uploads a disposable 32 MiB non-media file, downloads it for SHA-256 verification, and removes both copies.</small></div>
              <button type="button" className="secondary small" disabled={ftpSpeedTesting || saving} onClick={() => void runFtpSpeedTest()}>{ftpSpeedTesting ? "Testing…" : "Test FTP server"}</button>
              {ftpSpeedTest && <div className="speed-test-result ftp-speed-test-result">
                <span><small>FTP upload</small><b>{bytes(ftpSpeedTest.upload_bytes_per_second)}/s</b></span>
                <span><small>Equivalent bit rate</small><b>{ftpSpeedTest.upload_megabits_per_second.toFixed(1)} Mbps</b></span>
                <span><small>Verification read-back</small><b>{bytes(ftpSpeedTest.verification_bytes_per_second)}/s</b></span>
                <span><small>Verified effective rate</small><b>{bytes(ftpSpeedTest.verified_bytes_per_second)}/s</b></span>
                <span><small>Test payload</small><b>{bytes(ftpSpeedTest.size_bytes)} · {ftpSpeedTest.upload_duration_seconds.toFixed(2)}s up + {ftpSpeedTest.verification_duration_seconds.toFixed(2)}s verify</b></span>
                <span><small>Integrity</small><b>{ftpSpeedTest.checksum_verified && ftpSpeedTest.temporary_files_removed ? "Verified · cleaned" : "Needs attention"}</b></span>
              </div>}
            </div>
          </div>}
          <small className="field-hint">{form.connection_mode === "ftp" ? "FTP handles file copies while network ADB remains connected for storage detection, telemetry, MediaStore scans, checks, and device controls." : form.connection_mode === "network" ? "Use Enable ADB over IP in Overview → Pixel Network to configure and verify this automatically." : "USB is the default and works even when Ethernet routing is unavailable."}</small>
          <div className="connection-speed-test">
            <div><strong>ADB transfer speed</strong><small>Uploads and verifies a disposable 32 MiB non-media file over the saved {settings.connection_mode === "usb" ? "USB ADB" : "network ADB"} control connection, then removes it.</small></div>
            <button type="button" className="secondary small" disabled={speedTesting} onClick={() => void runAdbSpeedTest()}>{speedTesting ? "Testing…" : "Run speed test"}</button>
            {speedTest && <div className="speed-test-result">
              <span><small>Throughput</small><b>{bytes(speedTest.bytes_per_second)}/s</b></span>
              <span><small>Equivalent bit rate</small><b>{speedTest.megabits_per_second.toFixed(1)} Mbps</b></span>
              <span><small>Test payload</small><b>{bytes(speedTest.size_bytes)} in {speedTest.duration_seconds.toFixed(2)}s</b></span>
              <span><small>Integrity</small><b>{speedTest.checksum_verified && speedTest.temporary_files_removed ? "Verified · cleaned" : "Needs attention"}</b></span>
            </div>}
          </div>
        </section>
        <section className="panel setting-card editable-card"><span className="panel-kicker">STORAGE PATHS</span><h2>Pixel and source locations</h2><label>Folder within selected Pixel storage<input value={form.destination_root} onChange={(event) => update("destination_root", event.target.value)} /></label><small className="field-hint">Keep this beneath <code>/sdcard</code>. The storage-medium selector changes what Android mounts at <code>/sdcard</code>, so this folder path does not need to change when switching between phone storage and an adopted drive.</small><div className="storage-buffer-control"><ByteSlider label="Pixel free-space buffer" value={form.reserve_bytes} minimum={0} maximum={form.pixel_internal_storage_bytes || Math.max(1, form.reserve_bytes)} disabled={!form.pixel_internal_storage_bytes} onChange={(value) => update("reserve_bytes", value)} /><small className="field-hint">{form.pixel_internal_storage_bytes ? <>Transfers pause before available Pixel storage falls below this buffer. Maximum: {bytes(form.pixel_internal_storage_bytes)} measured internal storage. The default is 10 GiB.</> : <>Connect and refresh the Pixel to measure internal storage before adjusting this buffer.</>}</small></div><label>TheDoPixel import root<div className="path-picker"><input value={form.import_root || ""} onChange={(event) => update("import_root", event.target.value)} placeholder="Absolute local or network path for browser imports" /><button type="button" className="secondary small" onClick={() => setServerBrowserOpen(true)}>Browse server…</button></div></label><small className="field-hint">This is a folder visible to the Pixel Relay host. Files selected with Upload media come from the browser client and are copied here.</small></section>
        <section className="panel setting-card editable-card"><span className="panel-kicker">BATCH GUARDRAILS</span><h2>Transfer limits</h2><div className="editable-grid"><NumericSlider label="Maximum files per batch" value={form.max_batch_files} min={1} max={100000} step={1} format={(value) => value.toLocaleString()} onChange={(value) => update("max_batch_files", value)} /><ByteSlider label="Maximum bytes" value={form.max_batch_bytes} minimum={1} onChange={(value) => update("max_batch_bytes", value)} /></div><small className="field-hint">Pixel Relay also considers current Pixel free space and the safety reserve. Oversized selections are balanced into sequential parts. Once a part is fully staged, the next part can start without Google Photos confirmation if its whole payload fits; otherwise it waits for space. Files from different source folders remain separate batches.</small></section>
        <section className="panel setting-card editable-card"><span className="panel-kicker">THERMAL SAFETY</span><h2>Temperature thresholds</h2><div className="editable-grid"><NumericSlider label="Pause at" value={form.pause_temperature_c} min={30} max={80} step={0.1} format={(value) => `${value.toFixed(1)} °C`} onChange={(value) => update("pause_temperature_c", value)} /><NumericSlider label="Resume below" value={form.resume_temperature_c} min={20} max={75} step={0.1} format={(value) => `${value.toFixed(1)} °C`} onChange={(value) => update("resume_temperature_c", value)} /></div><small className="field-hint">Resume must remain below the pause threshold.</small></section>
        <StorageSelector
          choices={storageOptions}
          value={form.expected_primary_uuid}
          refreshing={storageRefreshing}
          refresh={refreshStorageOptions}
          select={(uuid) => update("expected_primary_uuid", uuid)}
          report={report}
        />
        <PixelStorageManager report={report} />
      </div>
    </form>
    {serverBrowserOpen && <ServerDirectoryBrowser
      title="Choose the import folder"
      initialPath={form.import_root || ""}
      close={() => setServerBrowserOpen(false)}
      select={(selectedPath) => {
        update("import_root", selectedPath);
        setServerBrowserOpen(false);
      }}
    />}
  </>;
}

function StorageSelector({
  choices,
  value,
  refreshing,
  refresh,
  select,
  report
}: {
  choices: StorageOptions | null;
  value: string;
  refreshing: boolean;
  refresh: () => Promise<void>;
  select: (uuid: string) => void;
  report: (message: string, type?: Notice["type"]) => void;
}) {
  const [adoptionMedium, setAdoptionMedium] = useState<StorageMedium | null>(null);
  const [forceAdoptable, setForceAdoptable] = useState(false);
  const [migratePrimary, setMigratePrimary] = useState(false);
  const [submittingAdoption, setSubmittingAdoption] = useState(false);
  const [adoptionOperation, setAdoptionOperation] = useState<StorageAdoptionOperation | null>(null);
  const [primarySwitchOperation, setPrimarySwitchOperation] = useState<StoragePrimarySwitchOperation | null>(null);
  const [unmountingDiskId, setUnmountingDiskId] = useState<string | null>(null);
  const [adoptionElapsed, setAdoptionElapsed] = useState(0);
  const [primarySwitchElapsed, setPrimarySwitchElapsed] = useState(0);
  const handledAdoption = useRef<string | null>(null);
  const handledPrimarySwitch = useRef<string | null>(null);
  const adopting = adoptionOperation?.status === "running";
  const switchingPrimary = primarySwitchOperation?.status === "running";
  const adoptionProgress = adoptionOperation?.progress || null;
  const primarySwitchProgress = primarySwitchOperation?.progress || null;

  useEffect(() => {
    void api.storageAdoption()
      .then(({ operation }) => setAdoptionOperation(operation))
      .catch(() => undefined);
    void api.storagePrimarySwitch()
      .then(({ operation }) => setPrimarySwitchOperation(operation))
      .catch(() => undefined);
    const stream = new EventSource("/api/v1/events");
    const onStorage = (event: Event) => {
      try {
        const envelope = JSON.parse((event as MessageEvent<string>).data) as {
          data: StorageAdoptionProgress | StoragePrimarySwitchProgress;
          created_at: string;
        };
        const progress = envelope.data;
        if (progress.action === "adoption_progress") {
          setAdoptionOperation((current) => current?.operation_id === progress.operation_id
            ? {
                ...current,
                status: progress.failed ? "failed" : progress.complete ? "completed" : "running",
                progress
              }
            : current);
          if (progress.complete || progress.failed) {
            void api.storageAdoption()
              .then(({ operation }) => setAdoptionOperation(operation))
              .catch(() => undefined);
          }
        } else if (progress.action === "primary_switch_progress") {
          setPrimarySwitchOperation((current) =>
            current?.operation_id === progress.operation_id
              ? {
                  ...current,
                  status: progress.failed
                    ? "failed"
                    : progress.complete
                      ? "completed"
                      : "running",
                  progress
                }
              : current
          );
          if (progress.complete || progress.failed) {
            void api.storagePrimarySwitch()
              .then(({ operation }) => setPrimarySwitchOperation(operation))
              .catch(() => undefined);
          }
        }
      } catch {
        // Keep the last valid adoption stage when an event is malformed.
      }
    };
    stream.addEventListener("storage", onStorage);
    return () => {
      stream.removeEventListener("storage", onStorage);
      stream.close();
    };
  }, []);

  useEffect(() => {
    if (!adopting) return;
    const startedAt = new Date(adoptionOperation.started_at).getTime();
    const updateElapsed = () => setAdoptionElapsed(
      Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
    );
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [adopting, adoptionOperation?.started_at]);

  useEffect(() => {
    if (!switchingPrimary) return;
    const startedAt = new Date(primarySwitchOperation.started_at).getTime();
    const updateElapsed = () => setPrimarySwitchElapsed(
      Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
    );
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [switchingPrimary, primarySwitchOperation?.started_at]);

  useEffect(() => {
    if (
      !adoptionOperation
      || adoptionOperation.status === "running"
      || handledAdoption.current === adoptionOperation.operation_id
    ) return;
    handledAdoption.current = adoptionOperation.operation_id;
    const finish = async () => {
      await refresh();
      if (adoptionOperation.status === "failed") {
        report(
          adoptionOperation.error || adoptionOperation.progress.message || "Storage adoption failed",
          "bad"
        );
        return;
      }
      const result = adoptionOperation.result;
      if (result?.migrated_primary) select(result.adopted_uuid);
      const message = result?.migration_error
        ? `Storage was adopted, but migration did not finish: ${result.migration_error}`
        : result?.migrated_primary
          ? "Storage adopted, migrated, and selected as Android primary"
          : "Storage adopted. Use Android Settings to migrate it before selecting it.";
      report(message, result?.migration_error ? "bad" : "good");
    };
    void finish();
  }, [adoptionOperation, refresh, report, select]);

  useEffect(() => {
    if (
      !primarySwitchOperation
      || primarySwitchOperation.status === "running"
      || handledPrimarySwitch.current === primarySwitchOperation.operation_id
    ) return;
    handledPrimarySwitch.current = primarySwitchOperation.operation_id;
    const finish = async () => {
      await refresh();
      if (primarySwitchOperation.status === "failed") {
        report(
          primarySwitchOperation.error
            || primarySwitchOperation.progress.message
            || "Android primary-storage migration failed",
          "bad"
        );
        return;
      }
      const targetUuid = primarySwitchOperation.result?.target_uuid
        ?? primarySwitchOperation.target_uuid;
      select(targetUuid);
      report(
        targetUuid
          ? "Android /sdcard and Pixel Relay now use the selected adopted drive"
          : "Android /sdcard and Pixel Relay now use phone internal storage"
      );
    };
    void finish();
  }, [primarySwitchOperation, refresh, report, select]);

  function closeAdoption() {
    setAdoptionMedium(null);
    setForceAdoptable(false);
    setMigratePrimary(false);
  }

  async function adopt() {
    if (!adoptionMedium) return;
    setAdoptionElapsed(0);
    setSubmittingAdoption(true);
    try {
      const operation = await api.adoptStorage(
        adoptionMedium.disk_id,
        forceAdoptable,
        migratePrimary
      );
      handledAdoption.current = null;
      setAdoptionOperation(operation);
      closeAdoption();
      report("Drive adoption started in the background. You can leave this page.");
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : "Storage adoption failed";
      const message = /^(?:adoption failed[,:]?\s*)?adb command failed/i.test(rawMessage)
        ? "Android rejected the drive-adoption command without diagnostic output. Check the SSD and hub power, keep the Pixel unlocked with USB debugging authorized, then refresh the storage list before retrying because the drive may have been partially repartitioned."
        : rawMessage;
      report(message, "bad");
    } finally {
      setSubmittingAdoption(false);
    }
  }

  async function dismissAdoption() {
    try {
      await api.dismissStorageAdoption();
      setAdoptionOperation(null);
      setAdoptionElapsed(0);
    } catch (error) {
      report(error instanceof Error ? error.message : "Could not dismiss adoption status", "bad");
    }
  }

  async function choosePrimaryStorage(targetUuid: string) {
    if (
      switchingPrimary
      || adopting
      || (
        targetUuid === value
        && targetUuid === (choices?.current_primary_uuid || "")
      )
    ) return;
    const changesAndroid = targetUuid !== (choices?.current_primary_uuid || "");
    const option = choices?.options.find((candidate) => candidate.uuid === targetUuid);
    if (
      changesAndroid
      && !window.confirm(
        targetUuid
          ? `Move Android /sdcard data to “${option?.label || targetUuid}”?\n\nThis runs in the background. Keep the Pixel and drive connected until it completes.`
          : "Move Android /sdcard data back to phone internal storage?\n\nThis runs in the background. Keep the Pixel and drive connected until it completes."
      )
    ) return;
    try {
      const operation = await api.switchPrimaryStorage(targetUuid);
      handledPrimarySwitch.current = null;
      setPrimarySwitchOperation(operation);
      setPrimarySwitchElapsed(0);
      report(
        changesAndroid
          ? "Android primary-storage migration started in the background"
          : "Pixel Relay is updating its storage safety lock"
      );
    } catch (error) {
      report(
        error instanceof Error ? error.message : "Could not change Android primary storage",
        "bad"
      );
    }
  }

  async function dismissPrimarySwitch() {
    try {
      await api.dismissStoragePrimarySwitch();
      setPrimarySwitchOperation(null);
      setPrimarySwitchElapsed(0);
    } catch (error) {
      report(
        error instanceof Error ? error.message : "Could not dismiss storage migration status",
        "bad"
      );
    }
  }

  async function unmount(medium: StorageMedium) {
    const mountedVolumes = medium.volumes.filter(
      (volume) => ["public", "private", "stub"].includes(volume.volume_type || "")
        && (volume.state || "").startsWith("mounted")
    );
    if (!mountedVolumes.length) return;
    const isCurrentPrimary = medium.volumes.some(
      (volume) => Boolean(choices?.current_primary_uuid)
        && volume.fs_uuid === choices?.current_primary_uuid
        && ["private", "emulated"].includes(volume.volume_type || "")
    );
    const warning = isCurrentPrimary
      ? "This drive is Android’s current primary storage. Unmounting it will make /sdcard and its Google Photos media unavailable until the drive is mounted again.\n\nUnmount it now?"
      : "Unmount this drive from Android? Wait for Pixel Relay to report success before disconnecting it.";
    if (!window.confirm(warning)) return;
    setUnmountingDiskId(medium.disk_id);
    try {
      const result = await api.unmountStorage(medium.disk_id);
      await refresh();
      report(
        `${result.unmounted_volume_ids.length} volume${result.unmounted_volume_ids.length === 1 ? "" : "s"} unmounted. The drive can now be disconnected.`,
        "good"
      );
    } catch (error) {
      report(error instanceof Error ? error.message : "Storage unmount failed", "bad");
    } finally {
      setUnmountingDiskId(null);
    }
  }
  const adoptionDisplaySeconds = adoptionOperation
    ? (
        adoptionOperation.status === "running"
          ? adoptionElapsed
          : Math.max(
              0,
              Math.floor(
                (
                  new Date(adoptionOperation.finished_at || adoptionOperation.started_at).getTime()
                  - new Date(adoptionOperation.started_at).getTime()
                ) / 1000
              )
            )
      )
    : 0;
  const primarySwitchDisplaySeconds = primarySwitchOperation
    ? (
        primarySwitchOperation.status === "running"
          ? primarySwitchElapsed
          : Math.max(
              0,
              Math.floor(
                (
                  new Date(
                    primarySwitchOperation.finished_at || primarySwitchOperation.started_at
                  ).getTime()
                  - new Date(primarySwitchOperation.started_at).getTime()
                ) / 1000
              )
            )
      )
    : 0;
  const displayedStorageUuid = switchingPrimary
    ? primarySwitchOperation.target_uuid
    : choices?.current_primary_uuid ?? value;
  const storageLockMismatch = Boolean(
    choices
    && !switchingPrimary
    && choices.current_primary_uuid !== value
  );
  return <section className="panel setting-card wide-card editable-card storage-selector">
    <div className="storage-selector-head">
      <div>
        <span className="panel-kicker">PIXEL STORAGE TARGET</span>
        <h2>Select Android primary storage</h2>
      </div>
      <button type="button" className="secondary small" disabled={refreshing} onClick={refresh}><Icons.refresh className={refreshing ? "spin" : ""} /> {refreshing ? "Refreshing…" : "Refresh list"}</button>
    </div>
    <p>Pixel Relay always writes beneath <code>/sdcard</code>. Choosing another medium migrates Android’s primary shared storage in the background, then updates Pixel Relay’s UUID safety lock. The configured phone destination remains the same because Android remaps <code>/sdcard</code>.</p>
    {adoptionOperation && adoptionProgress && <div className={`adoption-progress background-adoption ${adoptionProgress.failed ? "failed" : adoptionProgress.complete ? "complete" : "active"}`} role="status" aria-live="polite">
      <div className="adoption-progress-head">
        <strong>{adoptionProgress.failed ? "Background adoption failed" : adoptionProgress.complete ? "Background adoption complete" : "Drive adoption running in background"}</strong>
        <span>{adoptionDisplaySeconds < 60 ? `${adoptionDisplaySeconds}s` : `${Math.floor(adoptionDisplaySeconds / 60)}m ${adoptionDisplaySeconds % 60}s`}</span>
      </div>
      <div className="adoption-progress-track"><i style={{ width: `${Math.max(1, Math.min(100, adoptionProgress.percent))}%` }} /></div>
      <p>{adoptionProgress.message}</p>
      {adoptionProgress.failed && <CopyButton text={adoptionOperation.error || adoptionProgress.message} />}
      <small><code>{adoptionOperation.disk_id}</code> · Operation <code>{adoptionOperation.operation_id.slice(0, 8)}</code>{adopting ? " · You may navigate elsewhere in Pixel Relay, but keep the Pixel, hub, and drive connected." : ""}</small>
      {!adopting && <div className="adoption-actions"><button type="button" className="secondary small" onClick={() => void dismissAdoption()}>Dismiss status</button></div>}
    </div>}
    {primarySwitchOperation && primarySwitchProgress && <div className={`adoption-progress background-adoption ${primarySwitchProgress.failed ? "failed" : primarySwitchProgress.complete ? "complete" : "active"}`} role="status" aria-live="polite">
      <div className="adoption-progress-head">
        <strong>{primarySwitchProgress.failed ? "Storage migration failed" : primarySwitchProgress.complete ? "Storage migration complete" : "Changing Android primary storage"}</strong>
        <span>{primarySwitchDisplaySeconds < 60 ? `${primarySwitchDisplaySeconds}s` : `${Math.floor(primarySwitchDisplaySeconds / 60)}m ${primarySwitchDisplaySeconds % 60}s`}</span>
      </div>
      <div className="adoption-progress-track"><i style={{ width: `${Math.max(1, Math.min(100, primarySwitchProgress.percent))}%` }} /></div>
      <p>{primarySwitchProgress.message}</p>
      {primarySwitchProgress.failed && <CopyButton text={primarySwitchOperation.error || primarySwitchProgress.message} />}
      <small>Target: <code>{primarySwitchOperation.target_uuid || "phone internal storage"}</code> · Destination remains <code>/sdcard/…</code>{switchingPrimary ? " · Keep the Pixel and selected drive connected." : ""}</small>
      {!switchingPrimary && <div className="adoption-actions"><button type="button" className="secondary small" onClick={() => void dismissPrimarySwitch()}>Dismiss status</button></div>}
    </div>}
    {choices && <>
      <div className="storage-quick-choice">
        <label>
          <span>Store Pixel Relay uploads on</span>
          <select value={displayedStorageUuid} disabled={switchingPrimary || adopting} onChange={(event) => void choosePrimaryStorage(event.target.value)}>
            {choices.options
              .filter((option) => option.selectable || option.uuid === value)
              .map((option) => <option key={option.id} value={option.uuid}>
                {option.kind === "internal" ? "Phone internal storage" : option.label}
                {option.total_bytes ? ` · ${bytes(option.total_bytes)}` : ""}
                {option.disk_id ? ` · ${option.disk_id}` : ""}
              </option>)}
          </select>
        </label>
        <small>Choosing a different medium also moves Android’s <code>/sdcard</code> data there. Portable drives appear below and must be adopted first. The migration continues if you leave this page.</small>
      </div>
      {storageLockMismatch && <div className="storage-selector-notice">
        <Icons.warning />
        <span>
          <strong>Pixel Relay’s saved storage lock does not match the Pixel.</strong>{" "}
          Android currently uses <code>{choices.current_primary_uuid || "phone internal storage"}</code>,
          while Pixel Relay expects <code>{value || "phone internal storage"}</code>.
          <button type="button" className="secondary small" onClick={() => void choosePrimaryStorage(choices.current_primary_uuid)}>Use the Pixel’s current storage</button>
        </span>
      </div>}
      <div className="storage-choice-list">
        {choices.options.map((option) => {
          const checked = displayedStorageUuid === option.uuid;
          const utilization = storageUtilization(option.total_bytes, option.free_bytes);
          const capacity = utilization
            ? `${bytes(utilization.freeBytes)} free of ${bytes(utilization.totalBytes)} · ${utilization.utilizedPercent.toFixed(1)}% utilized`
            : "Capacity available only for the current primary";
          return <label key={option.id} className={`storage-choice ${checked ? "selected" : ""} ${!option.selectable ? "unavailable" : ""}`}>
            <input type="radio" name="pixel-storage-uuid" value={option.uuid} checked={checked} disabled={!option.selectable || switchingPrimary || adopting} onChange={() => void choosePrimaryStorage(option.uuid)} />
            <span className={`storage-choice-icon ${option.kind}`}>{option.kind === "internal" ? "PHONE" : option.kind === "adopted" ? "PRIVATE" : option.kind === "portable" ? "USB" : "MISSING"}</span>
            <span className="storage-choice-body">
              <span className="storage-choice-title"><strong>{option.label}</strong>{option.current && <b className="choice-badge current">ACTIVE ON PIXEL</b>}{option.configured && <b className="choice-badge configured">RELAY LOCK</b>}<b className={`choice-badge state ${option.state === "mounted" ? "mounted" : ""}`}>{option.state.toUpperCase()}</b></span>
              <small>{option.description}</small>
              <code>{option.uuid || "No UUID · Android internal/default"}</code>
              <em>{capacity}{option.volume_ids.length ? ` · ${option.volume_ids.join(", ")}` : ""}</em>
            </span>
          </label>;
        })}
      </div>
      <small className="field-hint">Portable volumes are shown for diagnosis but cannot be selected. Adopted private storage must be mounted, and its UUID must be Android’s current primary before transfers can run.</small>
      <div className="storage-observation"><span>Android primary</span><code>{choices.current_primary_uuid || "Internal / no UUID"}</code><span>Detected disks</span><code>{choices.disks.length ? choices.disks.join(", ") : "None reported"}</code><span>Observed</span><code>{relativeTime(choices.observed_at)}</code></div>
      <div className="adoption-section">
        <div className="adoption-section-head"><div><span className="panel-kicker">PHYSICAL MEDIA</span><h3>USB and removable storage</h3></div><small>{choices.details_supported ? "Android media details available" : "Limited Android details"}</small></div>
        {choices.media_error && <div className="storage-selector-notice"><Icons.warning /><span>{choices.media_error}</span></div>}
        <div className="storage-media-list">
          {choices.media.map((medium) => {
            const alreadyAdopted = medium.volumes.some((volume) => ["private", "emulated"].includes(volume.volume_type || "") && Boolean(volume.fs_uuid));
            const incompleteAdoption = medium.volumes.some((volume) => volume.volume_type === "private" && volume.state === "unmountable" && !volume.fs_uuid);
            const hasMountedPhysicalVolume = medium.volumes.some((volume) => ["public", "private", "stub"].includes(volume.volume_type || "") && (volume.state || "").startsWith("mounted"));
            const mountPoints = [...new Set(medium.volumes.map((volume) => volume.path).filter((path): path is string => Boolean(path)))];
            const backsSdcard = Boolean(
              choices.current_primary_uuid
              && medium.volumes.some((volume) =>
                volume.fs_uuid === choices.current_primary_uuid
                && ["private", "emulated"].includes(volume.volume_type || "")
              )
            );
            const unmounting = unmountingDiskId === medium.disk_id;
            return <div className="storage-medium" key={medium.disk_id}>
            <div className="storage-medium-head">
              <span className={`storage-medium-icon ${medium.usb ? "usb" : medium.sd ? "sd" : ""}`}>{medium.usb ? "USB" : medium.sd ? "SD" : "DISK"}</span>
              <div><strong>{medium.label || (medium.usb ? "USB storage medium" : medium.sd ? "SD storage medium" : "Removable storage medium")}</strong><code>{medium.disk_id}</code></div>
              <b className={`choice-badge ${medium.adoptable ? "mounted" : ""}`}>{medium.adoptable ? "ADOPTABLE" : "PORTABLE"}</b>
            </div>
            <div className="storage-medium-facts">
              <span><small>Capacity</small><b>{bytes(medium.size_bytes)}</b></span>
              <span><small>Volumes</small><b>{medium.volumes.length}</b></span>
              <span><small>Connection</small><b>{medium.usb ? "USB" : medium.sd ? "SD" : "Other"}</b></span>
              <span><small>Default primary</small><b>{medium.default_primary ? "Yes" : "No"}</b></span>
            </div>
            <div className="storage-medium-path">
              <span>{backsSdcard ? "Pixel Relay media path" : "External drive mount point"}</span>
              <code>{backsSdcard ? "/sdcard (Android primary storage)" : mountPoints.length ? mountPoints.join(" · ") : "Not mounted or not reported by Android"}</code>
            </div>
            {medium.volumes.length > 0 && <div className="medium-volume-list">
              {medium.volumes.map((volume) => <div key={volume.volume_id}><code>{volume.volume_id}</code><span>{(volume.volume_type || "unknown").toUpperCase()} · {(volume.state || "unknown").toUpperCase()}</span><span>{volume.fs_label || "No label"} · {volume.fs_type || "unknown FS"}</span><code>{volume.fs_uuid || "No UUID"}</code>{volume.path && <code className="volume-mount">Mounted at {volume.path}</code>}</div>)}
            </div>}
            <div className="storage-medium-path"><span>Android path</span><code>{medium.sys_path || "Not reported"}</code></div>
            {incompleteAdoption && <div className="storage-selector-notice"><Icons.warning /><span>Android reports an incomplete adopted partition with no filesystem UUID. Reset or reformat this drive as portable storage, reconnect it, and refresh before adopting again.</span></div>}
            <div className="storage-medium-actions">
              <button type="button" className={alreadyAdopted || incompleteAdoption ? "secondary" : "danger"} disabled={adopting || Boolean(unmountingDiskId) || alreadyAdopted || incompleteAdoption} onClick={() => { setAdoptionMedium(medium); setForceAdoptable(false); }}>{alreadyAdopted ? "Already adopted" : incompleteAdoption ? "Reset required" : "Adopt this medium…"}</button>
              <button type="button" className="secondary" disabled={adopting || Boolean(unmountingDiskId) || !hasMountedPhysicalVolume} onClick={() => void unmount(medium)}>{unmounting ? "Unmounting…" : hasMountedPhysicalVolume ? "Unmount drive" : "Unmounted"}</button>
            </div>
          </div>;
          })}
          {!choices.media.length && <Empty icon="□" title="No removable media detected" text="Connect the SSD to the Pixel, then refresh the storage list." />}
        </div>
        {choices.ignored_media?.map((medium) => <div className="storage-selector-notice" key={`ignored-${medium.disk_id}`}>
          <Icons.storage />
          <span>
            <strong>Empty USB reader or bridge ignored ({medium.disk_id}).</strong>{" "}
            Android keeps this hardware slot registered even when no drive is inserted.
            It is not phone storage and cannot be selected or adopted.
            {medium.sys_path ? <><br /><code>{medium.sys_path}</code></> : null}
          </span>
        </div>)}
      </div>
    </>}
    {!choices && <SectionLoader />}
    {adoptionMedium && <div className="adoption-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && closeAdoption()}>
      <section className="adoption-dialog" role="dialog" aria-modal="true" aria-labelledby="adoption-title">
        <button type="button" className="drawer-close" disabled={submittingAdoption} onClick={() => closeAdoption()}><Icons.close /></button>
        <span className="panel-kicker">DESTRUCTIVE AND IRREVERSIBLE</span>
        <h2 id="adoption-title">Adopt {adoptionMedium.label || adoptionMedium.disk_id}?</h2>
        <div className="adoption-warning"><Icons.warning /><div><strong>Every file and partition on this medium will be erased.</strong><small>Android will repartition and encrypt it for this Pixel. Other computers will no longer be able to mount it, and disconnecting adopted USB storage can cause corruption.</small></div></div>
        <dl>
          <div><dt>Disk ID</dt><dd><code>{adoptionMedium.disk_id}</code></dd></div>
          <div><dt>Reported capacity</dt><dd>{bytes(adoptionMedium.size_bytes)}</dd></div>
          <div><dt>Label</dt><dd>{adoptionMedium.label || "Not reported"}</dd></div>
          <div><dt>Existing volumes</dt><dd>{adoptionMedium.volumes.map((volume) => volume.fs_uuid || volume.volume_id).join(", ") || "None reported"}</dd></div>
        </dl>
        {!adoptionMedium.adoptable && <label className="adoption-check"><input type="checkbox" checked={forceAdoptable} disabled={submittingAdoption} onChange={(event) => setForceAdoptable(event.target.checked)} /><span><strong>Force Android USB adoption</strong><small>Android does not currently flag this medium as adoptable. This enables Android’s force-adoptable debug setting before partitioning.</small></span></label>}
        <label className="adoption-check"><input type="checkbox" checked={migratePrimary} disabled={submittingAdoption} onChange={(event) => setMigratePrimary(event.target.checked)} /><span><strong>Migrate shared storage and make this Android primary</strong><small>Moves current <code>/sdcard</code> data after adoption. This can take a long time; keep the Pixel powered and do not disconnect the hub.</small></span></label>
        <small className="field-hint">After the server accepts this operation, it will continue in the background. You may close this dialog or navigate elsewhere in Pixel Relay.</small>
        <div className="adoption-actions"><button type="button" className="secondary" disabled={submittingAdoption} onClick={() => closeAdoption()}>Cancel</button><button type="button" className="danger" disabled={submittingAdoption || adopting || (!adoptionMedium.adoptable && !forceAdoptable)} onClick={() => void adopt()}>{submittingAdoption ? "Starting background adoption…" : "Erase and adopt in background"}</button></div>
      </section>
    </div>}
  </section>;
}

function NumericSlider({
  label: sliderLabel,
  value,
  min,
  max,
  step,
  onChange,
  format = (next) => next.toLocaleString()
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  format?: (value: number) => string;
}) {
  return (
    <label className="slider-setting">
      <span>{sliderLabel}<output>{format(value)}</output></span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <small><i>{format(min)}</i><i>{format(max)}</i></small>
    </label>
  );
}

function ByteSlider({
  label: sliderLabel,
  value,
  minimum,
  maximum = 10 * 1024 ** 4,
  disabled = false,
  onChange
}: {
  label: string;
  value: number;
  minimum: 0 | 1;
  maximum?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const oneMiB = 1024 ** 2;
  const logarithmicMinimum = 1;
  const effective = Math.min(maximum, Math.max(logarithmicMinimum, value));
  const logarithmicSpan = Math.max(
    1,
    Math.log(maximum / logarithmicMinimum)
  );
  const position = value === 0
    ? 0
    : 1 + (
      Math.log(effective / logarithmicMinimum)
      / logarithmicSpan
    ) * 999;
  function updatePosition(nextPosition: number) {
    if (minimum === 0 && nextPosition === 0) {
      onChange(0);
      return;
    }
    const normalized = Math.max(0, (nextPosition - 1) / 999);
    const raw = logarithmicMinimum
      * Math.pow(maximum / logarithmicMinimum, normalized);
    const rounded = raw < oneMiB
      ? Math.round(raw)
      : Math.round(raw / oneMiB) * oneMiB;
    onChange(Math.min(maximum, Math.max(minimum, rounded)));
  }
  return (
    <label className="slider-setting">
      <span>{sliderLabel}<output>{bytes(value)}</output></span>
      <input
        type="range"
        min={minimum === 0 ? 0 : 1}
        max={1000}
        step={1}
        value={Math.round(position)}
        disabled={disabled}
        onChange={(event) => updatePosition(Number(event.target.value))}
      />
      <small><i>{minimum === 0 ? "0 B" : "1 B"}</i><i>{bytes(maximum)}</i></small>
    </label>
  );
}

function ServerDirectoryBrowser({
  title,
  initialPath,
  close,
  select
}: {
  title: string;
  initialPath: string;
  close: () => void;
  select: (path: string) => void;
}) {
  const [listing, setListing] = useState<ServerDirectoryListing | null>(null);
  const [pathInput, setPathInput] = useState(initialPath);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async (nextPath: string) => {
    setLoading(true);
    setError("");
    try {
      const next = await api.serverDirectories(nextPath);
      setListing(next);
      setPathInput(next.path);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Server folder could not be read");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load(initialPath);
  }, [initialPath, load]);
  return (
    <div className="drawer-backdrop directory-browser-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}>
      <section className="directory-browser" role="dialog" aria-modal="true" aria-label={title}>
        <div className="directory-browser-head">
          <div><span className="page-kicker">SERVER FILESYSTEM</span><h2>{title}</h2></div>
          <button type="button" className="drawer-close" onClick={close}><Icons.close /></button>
        </div>
        <p>Browse folders and drives visible to the Pixel Relay service on this host.</p>
        <form className="directory-path" onSubmit={(event) => { event.preventDefault(); void load(pathInput); }}>
          <input value={pathInput} onChange={(event) => setPathInput(event.target.value)} aria-label="Server directory path" />
          <button className="secondary" disabled={loading}>Go</button>
        </form>
        {listing && <div className="directory-shortcuts">
          {listing.shortcuts.map((shortcut) => <button type="button" key={shortcut.path} onClick={() => void load(shortcut.path)}>{shortcut.name}</button>)}
        </div>}
        {error && <div className="alert-strip"><Icons.warning /><div><strong>Folder unavailable</strong><span>{error}</span></div></div>}
        <div className="directory-list">
          {listing?.parent && <button type="button" onClick={() => void load(listing.parent!)}><span>↰</span><div><strong>Parent folder</strong><small>{listing.parent}</small></div></button>}
          {listing?.entries.map((entry) => <button type="button" key={entry.path} onClick={() => void load(entry.path)}><span><Icons.source /></span><div><strong>{entry.name}</strong><small>{entry.path}</small></div></button>)}
          {!loading && listing && !listing.entries.length && !listing.parent && <Empty icon="□" title="No subfolders" text="Select this folder or enter another server path." />}
          {loading && <SectionLoader />}
        </div>
        <div className="directory-browser-actions">
          <small>{listing ? `Current folder: ${listing.path}` : "Choose a readable server folder"}</small>
          <button type="button" className="primary" disabled={!listing || loading} onClick={() => listing && select(listing.path)}>Use this folder <span>→</span></button>
        </div>
      </section>
    </div>
  );
}

function PixelStorageManager({ report }: { report: (message: string, type?: Notice["type"]) => void }) {
  const [storage, setStorage] = useState<PixelStorage | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  async function load() {
    setLoading(true);
    try {
      setStorage(await api.pixelStorage());
      setSelected(new Set());
    } catch (error) {
      setStorage(null);
      report(error instanceof Error ? error.message : "Pixel storage could not be read", "bad");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { void load(); }, []);
  async function purgeOrphans() {
    if (!window.confirm(`Delete ${selected.size} selected orphaned Pixel ${selected.size === 1 ? "copy" : "copies"}?`)) return;
    setLoading(true);
    try {
      const result = await api.purgeStorageOrphans([...selected]);
      report(`${result.deleted_count} orphaned Pixel file${result.deleted_count === 1 ? "" : "s"} deleted`);
      await load();
    } catch (error) {
      report(error instanceof Error ? error.message : "Orphan cleanup failed", "bad");
      setLoading(false);
    }
  }
  async function freePixelSpace() {
    if (!window.confirm("Free up safe space on the Pixel? This removes untracked Pixel Relay files, prunes empty relay folders, and trims disposable Android app caches. It will not clear app data, delete tracked batch copies, or remove personal media.")) return;
    setLoading(true);
    try {
      const result = await api.freePixelSpace();
      const reclaimed = result.reclaimed_bytes == null ? "" : ` · ${bytes(result.reclaimed_bytes)} reclaimed`;
      const cacheNote = result.cache_trim_supported ? "" : " · app-cache trim requires ADB";
      report(`Pixel cleanup finished${reclaimed}${cacheNote}`);
      await load();
    } catch (error) {
      report(error instanceof Error ? error.message : "Pixel space cleanup failed", "bad");
      setLoading(false);
    }
  }
  async function cleanSlate() {
    const confirmation = window.prompt(
      `This permanently deletes everything beneath ${storage?.destination_root || "the configured Pixel Relay tree"}. `
      + "Source originals, Google Photos copies, unrelated drive files, and confirmed backup history are preserved. "
      + "Unconfirmed batches are cancelled. Type DELETE PIXEL RELAY TREE to continue."
    );
    if (confirmation == null) return;
    if (confirmation !== "DELETE PIXEL RELAY TREE") {
      report("Clean slate cancelled: confirmation text did not match", "bad");
      return;
    }
    setLoading(true);
    try {
      const result = await api.cleanSlateStorage();
      report(
        `Pixel Relay tree reset · ${result.known_files_deleted.toLocaleString()} known files removed · `
        + `${result.confirmed_batches_purged.toLocaleString()} confirmed batches purged · `
        + `${result.unconfirmed_batches_cancelled.toLocaleString()} unconfirmed batches cancelled`
      );
      await load();
    } catch (error) {
      report(error instanceof Error ? error.message : "Pixel Relay clean slate failed", "bad");
      setLoading(false);
    }
  }
  const utilization = storageUtilization(
    storage?.storage_total_bytes,
    storage?.storage_free_bytes
  );
  return <section className="panel setting-card wide-card storage-manager">
    <div className="storage-manager-head"><div><span className="panel-kicker">PIXEL STORAGE MANAGER</span><h2>TheDoPixel files</h2></div><button type="button" className="secondary small" disabled={loading} onClick={() => void load()}><Icons.refresh /> Refresh</button></div>
    {loading && !storage ? <SectionLoader /> : storage ? <>
      <p><code>{storage.destination_root}</code> · {storage.connection_mode.toUpperCase()}</p>
      <div className="target-capacity">
        <div>
          <span>Target medium capacity</span>
          <strong>{utilization ? `${bytes(utilization.freeBytes)} free / ${bytes(utilization.totalBytes)} total` : "Capacity unavailable"}</strong>
          <small>{utilization ? `${bytes(utilization.usedBytes)} used on the active /sdcard medium` : "Connect and refresh the active target medium to measure it."}</small>
        </div>
        <b>{utilization ? `${utilization.utilizedPercent.toFixed(1)}% utilized` : "—"}</b>
        <div className="target-capacity-track" role="meter" aria-label="Target storage utilization" aria-valuemin={0} aria-valuemax={100} aria-valuenow={utilization?.utilizedPercent}>
          <i style={{ width: `${utilization?.utilizedPercent ?? 0}%` }} />
        </div>
      </div>
      <div className="storage-summary">
        <span><b>{bytes(storage.relay_allocated_bytes)}</b><small>Relay allocation</small></span>
        <span><b>{bytes(storage.storage_free_bytes)}</b><small>Target free</small></span>
        <span><b>{storage.tracked_count}</b><small>Tracked files</small></span>
        <span className={storage.orphan_count ? "warning" : ""}><b>{storage.orphan_count}</b><small>Orphaned files</small></span>
      </div>
      <div className="storage-file-list">
        {storage.files.map((file) => <label className={`storage-file ${file.tracked ? "" : "orphan"}`} key={file.path}>
          <input type="checkbox" disabled={file.tracked} checked={selected.has(file.path)} onChange={() => setSelected((previous) => { const next = new Set(previous); next.has(file.path) ? next.delete(file.path) : next.add(file.path); return next; })} />
          <div><strong title={file.path}>{shortPath(file.path)}</strong><small>{file.tracked ? `${file.batch_name} · ${label(file.state || "")}` : "ORPHAN · Not present in the TheDoPixel database"}</small></div>
          <b>{bytes(file.allocated_bytes)}</b>
        </label>)}
        {!storage.files.length && <Empty icon="✓" title="TheDoPixel storage is empty" text="No staged files were found beneath the configured destination." />}
      </div>
      {storage.orphan_count > 0 && <div className="orphan-cleanup"><div><strong>Delete selected orphaned copies</strong><small>Tracked files must be purged through their batch. This removes only the selected untracked Pixel copies.</small></div><button type="button" className="danger" disabled={loading || !selected.size} onClick={() => void purgeOrphans()}>Delete {selected.size || ""} orphan{selected.size === 1 ? "" : "s"}</button></div>}
      <div className="general-cleanup"><div><strong>Free up Pixel space</strong><small>Removes untracked Relay files, prunes empty Relay folders, and trims disposable Android app caches over ADB. Personal media, app data, and tracked batch copies are kept.</small></div><button type="button" className="secondary" disabled={loading} onClick={() => void freePixelSpace()}>Free up safe space</button></div>
      <div className="general-cleanup clean-slate"><div><strong>Clean slate</strong><small>Deletes every tracked and orphaned file beneath this exact Pixel Relay destination, then recreates it empty. Confirmed backup history is preserved; unfinished batches are cancelled.</small></div><button type="button" className="danger" disabled={loading} onClick={() => void cleanSlate()}>Clean Pixel Relay tree</button></div>
    </> : <p>Connect the Pixel and refresh to inspect TheDoPixel storage.</p>}
  </section>;
}

function Empty({ icon, title, text }: { icon: string; title: string; text: string }) {
  return <div className="empty"><span>{icon}</span><strong>{title}</strong><small>{text}</small></div>;
}

function SectionLoader() {
  return <div className="section-loader"><i /><i /><i /><span>Loading appliance state…</span></div>;
}

function timeGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "morning";
  if (hour < 18) return "afternoon";
  return "evening";
}

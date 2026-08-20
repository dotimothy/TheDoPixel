import { describe, expect, it } from "vitest";
import {
  batchProgress,
  bytes,
  duration,
  googlePhotosDateSearch,
  googlePhotosBatchDateSearch,
  label,
  mediaScanProgress,
  parentFolderName,
  pathBaseName,
  shortPath,
  storageUtilization
} from "./format";

describe("dashboard formatting", () => {
  it("formats byte counts", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(null)).toBe("—");
    expect(bytes(1024)).toBe("1 KB");
    expect(bytes(5 * 1024 ** 3)).toBe("5.0 GB");
  });

  it("calculates target storage utilization without trusting missing values", () => {
    expect(storageUtilization(500, 400)).toEqual({
      totalBytes: 500,
      freeBytes: 400,
      usedBytes: 100,
      utilizedPercent: 20
    });
    expect(storageUtilization(100, 120)?.utilizedPercent).toBe(0);
    expect(storageUtilization(100, null, 25)?.freeBytes).toBe(75);
    expect(storageUtilization(null, null)).toBeNull();
    expect(storageUtilization(0, 0)).toBeNull();
  });

  it("turns states into readable labels", () => {
    expect(label("awaiting_backup_confirmation")).toBe("Awaiting Backup Confirmation");
  });

  it("formats processing ETAs", () => {
    expect(duration(null)).toBe("Calculating…");
    expect(duration(42)).toBe("<1 min");
    expect(duration(3_661)).toBe("1 hr 2 min");
    expect(duration(90_000)).toBe("1 d 1 hr");
  });

  it("shortens long source paths", () => {
    expect(shortPath("/Volumes/NAS/archive/2024/photo.jpg")).toBe("…/archive/2024/photo.jpg");
    expect(shortPath("E:\\Camera Roll\\archive\\2024\\photo.jpg")).toBe("…/archive/2024/photo.jpg");
  });

  it("uses the containing folder as the default archive name", () => {
    expect(parentFolderName("/Volumes/NAS/archive/2024/photo.jpg")).toBe("2024");
    expect(parentFolderName("C:\\Photos\\Birthday\\photo.jpg")).toBe("Birthday");
  });

  it("uses a selected source folder as its default display name", () => {
    expect(pathBaseName("/Volumes/NAS/Family archive/")).toBe("Family archive");
  });

  it("builds a Google Photos search for a completed batch date", () => {
    const timestamp = new Date(2024, 6, 25, 12).getTime() * 1_000_000;
    expect(googlePhotosDateSearch([{ mtime_ns: timestamp }])).toEqual({
      href: "https://photos.google.com/search/July%2025%2C%202024",
      dateLabel: "July 25, 2024"
    });
  });

  it("builds a Google Photos search for a completed batch date range", () => {
    const first = new Date(2024, 6, 25, 12).getTime() * 1_000_000;
    const last = new Date(2024, 7, 2, 12).getTime() * 1_000_000;
    expect(googlePhotosDateSearch([{ mtime_ns: last }, { mtime_ns: first }])).toEqual({
      href: "https://photos.google.com/search/from%20July%2025%2C%202024%20to%20August%202%2C%202024",
      dateLabel: "July 25, 2024–August 2, 2024"
    });
  });

  it("limits a split-batch search to that part's photo dates", () => {
    const ownFirst = new Date(2024, 6, 25, 12).getTime() * 1_000_000;
    const ownLast = new Date(2024, 6, 27, 12).getTime() * 1_000_000;
    const otherPart = new Date(2010, 0, 1, 12).getTime() * 1_000_000;
    const ownVideo = new Date(2030, 0, 1, 12).getTime() * 1_000_000;

    expect(googlePhotosBatchDateSearch("part-2", [
      { batch_id: "part-1", media_kind: "photo", mtime_ns: otherPart },
      { batch_id: "part-2", media_kind: "photo", mtime_ns: ownLast },
      { batch_id: "part-2", media_kind: "photo", mtime_ns: ownFirst },
      { batch_id: "part-2", media_kind: "video", mtime_ns: ownVideo }
    ])).toEqual({
      href: "https://photos.google.com/search/from%20July%2025%2C%202024%20to%20July%2027%2C%202024",
      dateLabel: "July 25, 2024–July 27, 2024"
    });
  });

  it("uses this part's video dates when it contains no photos", () => {
    const timestamp = new Date(2025, 2, 4, 12).getTime() * 1_000_000;

    expect(googlePhotosBatchDateSearch("videos", [
      { batch_id: "other", media_kind: "photo", mtime_ns: timestamp - 1_000_000 },
      { batch_id: "videos", media_kind: "video", mtime_ns: timestamp }
    ])).toEqual({
      href: "https://photos.google.com/search/March%204%2C%202025",
      dateLabel: "March 4, 2025"
    });
  });

  it("reports granular workflow progress by item phase", () => {
    const progress = batchProgress({
      queued: 1,
      transferring: 1,
      staged_on_pixel: 1,
      awaiting_backup_confirmation: 1
    });
    expect(progress.percent).toBe(55);
    expect(progress.ready).toBe(1);
    expect(progress.segments.map((segment) => segment.kind)).toEqual([
      "queued",
      "transferring",
      "staged",
      "verification"
    ]);
  });

  it("reports MediaStore scan progress separately from transfer progress", () => {
    const progress = mediaScanProgress({
      queued: 1,
      transferring: 1,
      staged_on_pixel: 1,
      awaiting_backup_confirmation: 2,
      media_scan_failed: 1
    });
    expect(progress.completed).toBe(3);
    expect(progress.scanning).toBe(1);
    expect(progress.total).toBe(6);
    expect(progress.percent).toBe(50);
  });
});

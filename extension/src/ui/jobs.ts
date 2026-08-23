import type { Job, JobStatus } from "../api/contracts";

export const JOB_FILTERS = ["all", "processing", "completed", "failed"] as const;

export type JobFilter = (typeof JOB_FILTERS)[number];

export type BatchLine = {
  lineNumber: number;
  raw: string;
  valid: boolean;
  reason?: string;
};

export type BatchInput = {
  lines: readonly BatchLine[];
  validCount: number;
  invalidCount: number;
  overLimit: boolean;
};

const PROCESSING_STATUSES = new Set<JobStatus>([
  "queued",
  "downloading",
  "extracting",
  "transcribing",
  "diarizing",
  "segmenting",
  "translating",
  "exporting",
  "cancelling",
]);

const FAILED_STATUSES = new Set<JobStatus>(["failed", "cancelled"]);

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "等待中",
  downloading: "正在下载",
  extracting: "正在提取音频",
  transcribing: "正在转写",
  diarizing: "正在识别说话人",
  segmenting: "正在整理句段",
  translating: "正在翻译",
  exporting: "正在导出",
  completed: "已完成",
  failed: "失败",
  cancelling: "正在取消",
  cancelled: "已取消",
};

export function parseBatchInput(value: string): BatchInput {
  const lines: BatchLine[] = [];
  for (const [index, source] of value.split(/\r?\n/u).entries()) {
    const raw = source.trim();
    if (raw.length === 0) {
      continue;
    }
    const reason = validateUrlSyntax(raw);
    lines.push({
      lineNumber: index + 1,
      raw,
      valid: reason === undefined,
      ...(reason === undefined ? {} : { reason }),
    });
  }
  const validCount = lines.filter((line) => line.valid).length;
  return {
    lines,
    validCount,
    invalidCount: lines.length - validCount,
    overLimit: lines.length > 100,
  };
}

export function submittedUrls(batch: BatchInput): string[] {
  if (batch.overLimit) {
    return [];
  }
  return batch.lines.filter((line) => line.valid).map((line) => line.raw);
}

export function retainedInputAfterSubmission(
  batch: BatchInput,
  rejectedUrls: readonly string[],
): string {
  const remainingRejected = new Map<string, number>();
  for (const url of rejectedUrls) {
    remainingRejected.set(url, (remainingRejected.get(url) ?? 0) + 1);
  }
  return batch.lines
    .filter((line) => {
      if (!line.valid) {
        return true;
      }
      const remaining = remainingRejected.get(line.raw) ?? 0;
      if (remaining === 0) {
        return false;
      }
      remainingRejected.set(line.raw, remaining - 1);
      return true;
    })
    .map((line) => line.raw)
    .join("\n");
}

export function filterJobs(jobs: readonly Job[], filter: JobFilter): Job[] {
  if (filter === "all") {
    return [...jobs];
  }
  if (filter === "processing") {
    return jobs.filter((job) => PROCESSING_STATUSES.has(job.status));
  }
  if (filter === "completed") {
    return jobs.filter((job) => job.status === "completed");
  }
  return jobs.filter((job) => FAILED_STATUSES.has(job.status));
}

export function jobStatusLabel(status: JobStatus): string {
  return STATUS_LABELS[status];
}

export function jobDisplayTitle(job: Job): string {
  return job.title.trim().length > 0 ? job.title : job.sanitizedDisplayUrl;
}

export function formatDuration(durationMs: number | null): string {
  if (durationMs === null) {
    return "--";
  }
  const totalSeconds = Math.floor(durationMs / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${String(hours)}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes)}:${String(seconds).padStart(2, "0")}`;
}

function validateUrlSyntax(value: string): string | undefined {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "URL 格式无效";
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return "仅支持 HTTP 或 HTTPS";
  }
  return undefined;
}

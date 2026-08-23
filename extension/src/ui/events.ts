import {
  ERROR_CODES,
  JOB_STATUSES,
  type ErrorCode,
  type JobEvent,
  type JobEventType,
  type JobStatus,
} from "../api/contracts";
import { jobStatusLabel } from "./jobs";

const JOB_STATUS_SET = new Set<string>(JOB_STATUSES);
const ERROR_CODE_SET = new Set<string>(ERROR_CODES);

const EVENT_LABELS: Partial<Record<JobEventType, string>> = {
  created: "任务已创建",
  claimed: "任务已开始",
  stage_changed: "阶段已变化",
  progress: "进度已更新",
  checkpoint_published: "检查点已保存",
  automatic_requeued: "任务等待自动重试",
  manual_retry: "已手工重试",
  cancel_requested: "已请求取消",
  interrupted: "任务被中断",
  artifact_unavailable: "产物不可用",
};

const REASON_LABELS: Record<string, string> = {
  startup_recovery: "启动恢复",
  automatic_requeue_budget_exhausted: "自动重试次数已用尽",
};

export function mergeEventPages(
  existing: readonly JobEvent[],
  incoming: readonly JobEvent[],
): JobEvent[] {
  const byId = new Map(existing.map((event) => [event.id, event]));
  for (const event of incoming) {
    if (!byId.has(event.id)) {
      byId.set(event.id, event);
    }
  }
  return [...byId.values()].sort((left, right) => left.id - right.id);
}

export function eventStatusLabel(status: JobEventType): string {
  if (JOB_STATUS_SET.has(status)) {
    return jobStatusLabel(status as JobStatus);
  }
  return EVENT_LABELS[status] ?? "任务事件";
}

export function eventMessageSummary(message: string | null): string {
  if (message === null) {
    return "";
  }
  let value: unknown;
  try {
    value = JSON.parse(message);
  } catch {
    return "";
  }
  if (!isRecord(value)) {
    return "";
  }
  const parts: string[] = [];
  if (typeof value.from_status === "string" && JOB_STATUS_SET.has(value.from_status)) {
    parts.push(`来自 ${jobStatusLabel(value.from_status as JobStatus)}`);
  }
  if (typeof value.resume_stage === "string" && JOB_STATUS_SET.has(value.resume_stage)) {
    parts.push(`恢复至 ${jobStatusLabel(value.resume_stage as JobStatus)}`);
  }
  if (typeof value.error_code === "string" && ERROR_CODE_SET.has(value.error_code)) {
    parts.push(`错误 ${value.error_code as ErrorCode}`);
  }
  if (typeof value.reason === "string") {
    const reason = REASON_LABELS[value.reason];
    if (reason !== undefined) {
      parts.push(`原因 ${reason}`);
    }
  }
  return parts.join(" · ");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

import type { JobStatus } from "../api/contracts";
import { ApiClientError } from "../api/errors";

export type JobAction = "cancel" | "retry" | "delete";

export type JobActionAvailability = Record<JobAction, boolean>;

export type ActionFailure = {
  message: string;
  refresh: boolean;
};

const CANCELLABLE = new Set<JobStatus>([
  "queued",
  "downloading",
  "extracting",
  "transcribing",
  "diarizing",
  "segmenting",
  "translating",
  "exporting",
]);

export function jobActionAvailability(status: JobStatus): JobActionAvailability {
  return {
    cancel: CANCELLABLE.has(status),
    retry: status === "failed" || status === "cancelled",
    delete: status === "completed" || status === "failed" || status === "cancelled",
  };
}

export function describeActionFailure(error: unknown): ActionFailure {
  if (!(error instanceof ApiClientError)) {
    return { message: "操作结果未知，请刷新任务确认；不会自动重试", refresh: false };
  }
  if (error.kind === "unreachable") {
    return { message: "操作结果未知，请刷新任务确认；不会自动重试", refresh: false };
  }
  return {
    message: error.message,
    refresh: error.kind === "conflict" || error.kind === "notFound",
  };
}

export class JobActionGate {
  private readonly busyJobs = new Set<string>();

  isBusy(jobId: string): boolean {
    return this.busyJobs.has(jobId);
  }

  run<T>(jobId: string, operation: () => Promise<T>): Promise<T> | undefined {
    if (this.busyJobs.has(jobId)) {
      return undefined;
    }
    this.busyJobs.add(jobId);
    let result: Promise<T>;
    try {
      result = operation();
    } catch (error) {
      this.busyJobs.delete(jobId);
      throw error;
    }
    return result.finally(() => {
      this.busyJobs.delete(jobId);
    });
  }
}

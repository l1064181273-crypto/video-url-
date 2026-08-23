import { describe, expect, it, vi } from "vitest";

import { JOB_STATUSES, type JobEvent, type JobStatus } from "../../src/api/contracts";
import { ApiClientError } from "../../src/api/errors";
import {
  describeActionFailure,
  JobActionGate,
  jobActionAvailability,
} from "../../src/ui/job-actions";
import { eventMessageSummary, eventStatusLabel, mergeEventPages } from "../../src/ui/events";

describe("job action policy", () => {
  it.each([
    ["queued", true, false, false],
    ["downloading", true, false, false],
    ["extracting", true, false, false],
    ["transcribing", true, false, false],
    ["diarizing", true, false, false],
    ["segmenting", true, false, false],
    ["translating", true, false, false],
    ["exporting", true, false, false],
    ["completed", false, false, true],
    ["failed", false, true, true],
    ["cancelling", false, false, false],
    ["cancelled", false, true, true],
  ] satisfies readonly [JobStatus, boolean, boolean, boolean][])(
    "maps %s to cancel=%s retry=%s delete=%s",
    (status, cancel, retry, deleteAction) => {
      expect(jobActionAvailability(status)).toEqual({
        cancel,
        retry,
        delete: deleteAction,
      });
    },
  );

  it("covers every frozen Job status exactly once", () => {
    expect(JOB_STATUSES).toHaveLength(12);
  });

  it("allows only one in-flight write per Job while leaving other Jobs independent", async () => {
    const gate = new JobActionGate();
    let resolveFirst!: () => void;
    const firstOperation = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const sameJobSecondOperation = vi.fn(() => Promise.resolve());
    const otherJobOperation = vi.fn(() => Promise.resolve());

    const first = gate.run("job-a", firstOperation);
    const duplicate = gate.run("job-a", sameJobSecondOperation);
    const independent = gate.run("job-b", otherJobOperation);

    expect(first).toBeDefined();
    expect(duplicate).toBeUndefined();
    expect(independent).toBeDefined();
    expect(firstOperation).toHaveBeenCalledTimes(1);
    expect(sameJobSecondOperation).not.toHaveBeenCalled();
    expect(otherJobOperation).toHaveBeenCalledTimes(1);
    expect(gate.isBusy("job-a")).toBe(true);

    resolveFirst();
    await Promise.all([first, independent]);
    expect(gate.isBusy("job-a")).toBe(false);
  });

  it.each([
    [new ApiClientError("notFound", "请求的本地资源不存在", 404), true],
    [new ApiClientError("conflict", "任务状态已变化，请刷新后重试", 409), true],
    [new ApiClientError("server", "本地服务处理失败，请稍后重试", 503), false],
  ])("classifies HTTP action failure %#", (error, refresh) => {
    expect(describeActionFailure(error)).toEqual({ message: error.message, refresh });
  });

  it("reports an unknown network result without retrying or claiming a local state change", () => {
    const failure = describeActionFailure(
      new ApiClientError("unreachable", "本地服务未启动，请先启动 Local Video Transcriber"),
    );

    expect(failure).toEqual({
      message: "操作结果未知，请刷新任务确认；不会自动重试",
      refresh: false,
    });
  });

  it("does not retry a rejected write or mutate caller-owned Job state", async () => {
    const gate = new JobActionGate();
    const state = { status: "failed" as JobStatus };
    const operation = vi.fn(() =>
      Promise.reject(new ApiClientError("unreachable", "network result unknown")),
    );

    const request = gate.run("job-a", operation);

    await expect(request).rejects.toMatchObject({ kind: "unreachable" });
    expect(operation).toHaveBeenCalledTimes(1);
    expect(state.status).toBe("failed");
    expect(gate.isBusy("job-a")).toBe(false);
  });
});

describe("event timeline", () => {
  it("stably sorts pages by ID and removes overlap without replacing prior events", () => {
    const first = [event(3), event(1), event(2)];
    const overlap = [event(2, "failed"), event(5), event(4)];

    const merged = mergeEventPages(first, overlap);

    expect(merged.map(({ id }) => id)).toEqual([1, 2, 3, 4, 5]);
    expect(merged[1]?.status).toBe("queued");
  });

  it("extracts only allowlisted structured message fields", () => {
    const secret = "ValidationSecretToken123";
    const summary = eventMessageSummary(
      JSON.stringify({
        from_status: "failed",
        resume_stage: "downloading",
        error_code: "DOWNLOAD_FAILED",
        reason: "startup_recovery",
        input: `<img src=x onerror=alert('${secret}')>`,
        ctx: { secret },
        error_message: secret,
        run_id: secret,
      }),
    );

    expect(summary).toBe("来自 失败 · 恢复至 正在下载 · 错误 DOWNLOAD_FAILED · 原因 启动恢复");
    expect(summary).not.toContain(secret);
    expect(summary).not.toContain("img");
    expect(eventMessageSummary("not-json")).toBe("");
    expect(eventMessageSummary('{"reason":"private_reason"}')).toBe("");
  });

  it("uses fixed labels for event and stage statuses", () => {
    expect(eventStatusLabel("manual_retry")).toBe("已手工重试");
    expect(eventStatusLabel("transcribing")).toBe("正在转写");
  });
});

function event(id: number, status: JobEvent["status"] = "queued"): JobEvent {
  return {
    id,
    jobId: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
    status,
    message: null,
    createdAt: "2026-08-23T10:00:00+00:00",
  };
}

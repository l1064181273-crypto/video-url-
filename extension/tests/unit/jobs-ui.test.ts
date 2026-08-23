import { describe, expect, it } from "vitest";

import { JOB_STATUSES, type Job, type JobStatus } from "../../src/api/contracts";
import {
  filterJobs,
  formatDuration,
  jobDisplayTitle,
  jobStatusLabel,
  parseBatchInput,
  retainedInputAfterSubmission,
  submittedUrls,
} from "../../src/ui/jobs";

describe("batch URL input", () => {
  it.each([1, 100])("accepts %i syntactically valid URLs in order", (count) => {
    const input = Array.from({ length: count }, (_, index) => {
      return `  https://example.test/video/${String(index + 1)}  `;
    }).join("\n");

    const batch = parseBatchInput(input);

    expect(batch).toMatchObject({
      validCount: count,
      invalidCount: 0,
      overLimit: false,
    });
    expect(submittedUrls(batch)).toHaveLength(count);
    expect(submittedUrls(batch)[0]).toBe("https://example.test/video/1");
    expect(submittedUrls(batch).at(-1)).toBe(`https://example.test/video/${String(count)}`);
  });

  it("blocks 101 non-empty lines without silently truncating the request", () => {
    const input = Array.from(
      { length: 101 },
      (_, index) => `https://example.test/${String(index)}`,
    ).join("\n");
    const batch = parseBatchInput(input);

    expect(batch).toMatchObject({
      validCount: 101,
      invalidCount: 0,
      overLimit: true,
    });
    expect(submittedUrls(batch)).toEqual([]);
  });

  it("ignores blank lines and reports line-specific syntax failures", () => {
    const batch = parseBatchInput(
      [
        "",
        " https://example.test/first ",
        "ftp://example.test/file",
        "not a URL",
        "http://example.test/last",
        "   ",
      ].join("\n"),
    );

    expect(batch).toMatchObject({ validCount: 2, invalidCount: 2, overLimit: false });
    expect(batch.lines).toEqual([
      { lineNumber: 2, raw: "https://example.test/first", valid: true },
      {
        lineNumber: 3,
        raw: "ftp://example.test/file",
        valid: false,
        reason: "仅支持 HTTP 或 HTTPS",
      },
      { lineNumber: 4, raw: "not a URL", valid: false, reason: "URL 格式无效" },
      { lineNumber: 5, raw: "http://example.test/last", valid: true },
    ]);
  });

  it("clears accepted lines while retaining client-invalid and backend-rejected lines in order", () => {
    const batch = parseBatchInput(
      [
        "https://example.test/accepted",
        "not a URL",
        "http://127.0.0.1/private",
        "https://example.test/accepted-again",
        "http://127.0.0.1/private",
      ].join("\n"),
    );

    expect(
      retainedInputAfterSubmission(batch, ["http://127.0.0.1/private", "http://127.0.0.1/private"]),
    ).toBe(["not a URL", "http://127.0.0.1/private", "http://127.0.0.1/private"].join("\n"));
  });
});

describe("job list presentation", () => {
  it("maps every frozen status to its fixed Chinese label and filter group", () => {
    const jobs = JOB_STATUSES.map((status, index) => job(status, index));

    expect(jobs.map(({ status }) => jobStatusLabel(status))).toEqual([
      "等待中",
      "正在下载",
      "正在提取音频",
      "正在转写",
      "正在识别说话人",
      "正在整理句段",
      "正在翻译",
      "正在导出",
      "已完成",
      "失败",
      "正在取消",
      "已取消",
    ]);
    expect(filterJobs(jobs, "all")).toHaveLength(12);
    expect(filterJobs(jobs, "processing").map(({ status }) => status)).toEqual([
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
    expect(filterJobs(jobs, "completed").map(({ status }) => status)).toEqual(["completed"]);
    expect(filterJobs(jobs, "failed").map(({ status }) => status)).toEqual(["failed", "cancelled"]);
  });

  it("uses backend title, URL fallback, duration, and progress values without derivation", () => {
    const item = {
      ...job("transcribing", 1),
      title: "中文标题 Пример",
      stageProgress: 73,
      overallProgress: 41,
      durationMs: 3_723_000,
    };

    expect(jobDisplayTitle(item)).toBe("中文标题 Пример");
    expect(jobDisplayTitle({ ...item, title: "" })).toBe(item.sanitizedDisplayUrl);
    expect(formatDuration(item.durationMs)).toBe("1:02:03");
    expect(formatDuration(null)).toBe("--");
    expect(item.stageProgress).toBe(73);
    expect(item.overallProgress).toBe(41);
  });
});

function job(status: JobStatus, index: number): Job {
  return {
    uuid: `4c50ff38-9cca-4f91-bae0-f3fe4bc18b${index.toString(16).padStart(2, "0")}`,
    sanitizedDisplayUrl: `https://example.test/video/${String(index)}`,
    title: "",
    status,
    stageProgress: index,
    overallProgress: index + 1,
    detectedLanguage: null,
    attempts: 0,
    errorCode: null,
    errorMessage: null,
    createdAt: "2026-08-23T10:00:00+00:00",
    updatedAt: "2026-08-23T10:00:00+00:00",
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    options: {
      asrModel: "mlx-community/whisper-small-mlx",
      translateTo: "zh-CN",
      diarization: true,
    },
    executionCountTotal: 0,
    retryCycle: 0,
    automaticRequeueCountInCycle: 0,
    nextAttemptAt: null,
    cancelRequestedAt: null,
  };
}

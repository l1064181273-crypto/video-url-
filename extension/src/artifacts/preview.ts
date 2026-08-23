import { ApiClientError } from "../api/errors";

export const MAX_PREVIEW_BYTES = 5 * 1024 * 1024;
export const MAX_PREVIEW_SEGMENTS = 2_000;

export type PreviewLanguage = "source" | "zh-CN";

export type PreviewSegment = {
  id: number;
  startMs: number;
  endMs: number;
  speaker: string;
  text: string;
};

export type TranscriptPreview = {
  language: PreviewLanguage;
  segments: PreviewSegment[];
};

export class PreviewTooLargeError extends Error {
  override readonly name = "PreviewTooLargeError";

  constructor() {
    super("文件过大，请下载后查看");
  }
}

export class PreviewSegmentLimitError extends Error {
  override readonly name = "PreviewSegmentLimitError";

  constructor() {
    super("段落过多，请下载后查看");
  }
}

export async function readBoundedJsonResponse(
  response: Response,
  maximumBytes = MAX_PREVIEW_BYTES,
  parseJson: (text: string) => unknown = JSON.parse,
  signal?: AbortSignal,
): Promise<unknown> {
  signal?.throwIfAborted();
  const contentType = response.headers.get("Content-Type")?.toLowerCase();
  if (
    contentType === undefined ||
    (!contentType.includes("application/json") && !contentType.includes("application/octet-stream"))
  ) {
    throw invalidPreview();
  }
  const declaredLength = parseContentLength(response.headers.get("Content-Length"));
  if (declaredLength !== undefined && declaredLength > maximumBytes) {
    try {
      await response.body?.cancel();
    } catch {
      // The preview limit remains authoritative even if stream cleanup fails.
    }
    throw new PreviewTooLargeError();
  }
  const reader = response.body?.getReader();
  if (reader === undefined) {
    throw invalidPreview();
  }
  const cancelReader = (): void => {
    void reader.cancel().catch(() => undefined);
  };
  signal?.addEventListener("abort", cancelReader, { once: true });
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) {
        break;
      }
      totalBytes += result.value.byteLength;
      if (totalBytes > maximumBytes) {
        chunks.length = 0;
        try {
          await reader.cancel();
        } catch {
          // Preserve the stable size-limit result.
        }
        throw new PreviewTooLargeError();
      }
      chunks.push(result.value);
    }
    signal?.throwIfAborted();
  } finally {
    signal?.removeEventListener("abort", cancelReader);
    reader.releaseLock();
  }
  if (totalBytes === 0) {
    throw invalidPreview();
  }
  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  chunks.length = 0;
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw invalidPreview();
  }
  try {
    return parseJson(text);
  } catch {
    throw invalidPreview();
  }
}

export function parseTranscriptPreview(
  value: unknown,
  language: PreviewLanguage,
  expectedJobId: string,
): TranscriptPreview {
  const transcript = expectRecord(value);
  expectExactKeys(transcript, [
    "schema_version",
    "job_id",
    "source_url",
    "title",
    "duration_ms",
    "detected_language",
    "engine_versions",
    "processing_options",
    "segments",
    "warnings",
  ]);
  const durationMs = transcript.duration_ms;
  if (
    transcript.schema_version !== "1.0" ||
    transcript.job_id !== expectedJobId ||
    typeof transcript.source_url !== "string" ||
    typeof transcript.title !== "string" ||
    !isPositiveInteger(durationMs) ||
    typeof transcript.detected_language !== "string" ||
    !isRecord(transcript.engine_versions) ||
    !isRecord(transcript.processing_options) ||
    !Array.isArray(transcript.warnings) ||
    !transcript.warnings.every((warning) => typeof warning === "string") ||
    !Array.isArray(transcript.segments)
  ) {
    throw invalidPreview();
  }
  if (transcript.segments.length > MAX_PREVIEW_SEGMENTS) {
    throw new PreviewSegmentLimitError();
  }
  let previousStart = -1;
  const segments = transcript.segments.map((value, index) => {
    const segment = expectRecord(value);
    expectExactKeys(segment, [
      "id",
      "start_ms",
      "end_ms",
      "speaker",
      "source_language",
      "source_text",
      "translated_text",
      "metadata",
    ]);
    const expectedId = index + 1;
    if (
      segment.id !== expectedId ||
      !isNonNegativeInteger(segment.start_ms) ||
      !isPositiveInteger(segment.end_ms) ||
      segment.start_ms >= segment.end_ms ||
      segment.end_ms > durationMs ||
      segment.start_ms < previousStart ||
      typeof segment.speaker !== "string" ||
      !/^Speaker [1-9]\d*$/u.test(segment.speaker) ||
      typeof segment.source_language !== "string" ||
      segment.source_language.length < 2 ||
      segment.source_language.length > 16 ||
      typeof segment.source_text !== "string" ||
      segment.source_text.length === 0 ||
      typeof segment.translated_text !== "string" ||
      !isRecord(segment.metadata)
    ) {
      throw invalidPreview();
    }
    const text = language === "source" ? segment.source_text : segment.translated_text;
    if (text.length === 0) {
      throw invalidPreview();
    }
    previousStart = segment.start_ms;
    return {
      id: expectedId,
      startMs: segment.start_ms,
      endMs: segment.end_ms,
      speaker: segment.speaker,
      text,
    };
  });
  return { language, segments };
}

function parseContentLength(value: string | null): number | undefined {
  if (value === null || !/^\d+$/u.test(value)) {
    return undefined;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function expectRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw invalidPreview();
  }
  return value;
}

function expectExactKeys(value: Record<string, unknown>, expected: readonly string[]): void {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  if (
    actual.length !== sortedExpected.length ||
    actual.some((key, index) => key !== sortedExpected[index])
  ) {
    throw invalidPreview();
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function invalidPreview(): ApiClientError {
  return new ApiClientError("invalidResponse", "预览文件格式异常，请下载后查看");
}

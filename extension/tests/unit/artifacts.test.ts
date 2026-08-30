import { describe, expect, it, vi } from "vitest";

import { ArtifactDownloadService, safeDownloadDirectory } from "../../src/artifacts/download";
import {
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_SEGMENTS,
  parseTranscriptPreview,
  PreviewSegmentLimitError,
  PreviewTooLargeError,
  readBoundedJsonResponse,
} from "../../src/artifacts/preview";
import { type ConnectionSource, LocalApiClient } from "../../src/api/client";
import {
  ARTIFACT_KINDS,
  artifactDownloadPath,
  type JobArtifact,
  parseJobArtifactsResponse,
} from "../../src/api/contracts";

const JOB_ID = "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f";
const ARTIFACT_ID = "25274279-9f3f-45f1-8352-79bc38f11cf6";

describe("artifact contracts", () => {
  it("accepts exactly eight unique canonical artifacts and binds them to the requested Job", () => {
    const artifacts = parseJobArtifactsResponse(artifactPayload(), JOB_ID);

    expect(artifacts.map(({ kind }) => kind)).toEqual(ARTIFACT_KINDS);
    expect(new Set(artifacts.map(({ id }) => id)).size).toBe(8);
    expect(artifacts.every(({ jobId }) => jobId === JOB_ID)).toBe(true);
    expect(artifacts[0]?.downloadPath).toBe(`/api/v1/artifacts/${artifacts[0]?.id ?? ""}/download`);
  });

  it.each([
    "https://evil.test/api/v1/artifacts/id/download",
    "//evil.test/api/v1/artifacts/id/download",
    "http://127.0.0.1:9999/api/v1/artifacts/id/download",
    "http://localhost/api/v1/artifacts/id/download",
    "/api/v1/artifacts/../secret/download",
    "/api/v1/artifacts/%2e%2e/download",
  ])("rejects untrusted download_url %s", (downloadUrl) => {
    const payload = artifactPayload();
    const first = payload.items[0];
    if (first === undefined) {
      throw new Error("artifact fixture is empty");
    }
    first.download_url = downloadUrl;

    expect(() => parseJobArtifactsResponse(payload, JOB_ID)).toThrow(
      "must match the fixed artifact route",
    );
  });

  it.each([
    "../secret",
    "25274279-9f3f-45f1-8352-79bc38f11cf6?x=1",
    "25274279-9f3f-45f1-8352-79bc38f11cf6#x",
    "25274279-9F3F-45F1-8352-79BC38F11CF6",
    "25274279-9f3f-45f1-8352-79bc38f11cf6\u0000",
  ])("rejects malicious or non-canonical artifact ID %s", (artifactId) => {
    expect(() => artifactDownloadPath(artifactId)).toThrow("canonical lowercase UUID");
  });

  it("rejects missing, duplicate, or unknown artifact kinds", () => {
    const missing = artifactPayload();
    missing.items.pop();
    expect(() => parseJobArtifactsResponse(missing, JOB_ID)).toThrow(
      "expected exactly eight artifacts",
    );

    const duplicate = artifactPayload();
    const last = duplicate.items[7];
    if (last === undefined) {
      throw new Error("artifact fixture is incomplete");
    }
    last.kind = "source.txt";
    expect(() => parseJobArtifactsResponse(duplicate, JOB_ID)).toThrow(
      "artifact IDs and kinds must be unique",
    );
  });
});

describe("bounded transcript preview", () => {
  it("rejects declared Content-Length above 5 MiB before reading or parsing", async () => {
    const stream = controlledStream([new Uint8Array([123])]);
    const parseJson = vi.fn<(text: string) => unknown>();

    await expect(
      readBoundedJsonResponse(
        responseWithStream(stream.body, String(MAX_PREVIEW_BYTES + 1)),
        MAX_PREVIEW_BYTES,
        parseJson,
      ),
    ).rejects.toBeInstanceOf(PreviewTooLargeError);

    expect(stream.cancel).toHaveBeenCalledTimes(1);
    expect(parseJson).not.toHaveBeenCalled();
  });

  it.each([
    ["missing", null],
    ["invalid", "not-a-number"],
    ["underreported", "1"],
  ])("limits actual streamed bytes when Content-Length is %s", async (_label, length) => {
    const stream = controlledStream([new Uint8Array([1, 2, 3]), new Uint8Array([4, 5, 6])]);
    const parseJson = vi.fn<(text: string) => unknown>();
    const response = responseWithStream(stream.body, length);

    await expect(readBoundedJsonResponse(response, 5, parseJson)).rejects.toBeInstanceOf(
      PreviewTooLargeError,
    );

    expect(stream.cancel).toHaveBeenCalledTimes(1);
    expect(response.body?.locked).toBe(false);
    expect(parseJson).not.toHaveBeenCalled();
  });

  it("enforces the fixed 5 MiB limit against actual streamed bytes", async () => {
    const stream = controlledStream([new Uint8Array(MAX_PREVIEW_BYTES + 1)]);

    await expect(
      readBoundedJsonResponse(responseWithStream(stream.body, null)),
    ).rejects.toBeInstanceOf(PreviewTooLargeError);
    expect(stream.cancel).toHaveBeenCalledTimes(1);
  });

  it("accepts an exact-limit stream and strictly decodes JSON", async () => {
    const bytes = new TextEncoder().encode('{"ok":true}');
    const response = responseWithStream(controlledStream([bytes]).body, String(bytes.byteLength));

    await expect(readBoundedJsonResponse(response, bytes.byteLength)).resolves.toEqual({
      ok: true,
    });
  });

  it("rejects invalid UTF-8 and malformed JSON without exposing content", async () => {
    await expect(
      readBoundedJsonResponse(
        responseWithStream(controlledStream([new Uint8Array([0xff])]).body, null),
      ),
    ).rejects.toMatchObject({ kind: "invalidResponse" });
    await expect(
      readBoundedJsonResponse(
        responseWithStream(controlledStream([new TextEncoder().encode("{not-json}")]).body, null),
      ),
    ).rejects.toMatchObject({ kind: "invalidResponse" });
  });

  it("cancels the active reader when preview loading is aborted", async () => {
    const cancel = vi.fn();
    const response = responseWithStream(
      new ReadableStream<Uint8Array>({
        pull: () => new Promise(() => undefined),
        cancel,
      }),
      null,
    );
    const controller = new AbortController();

    const pending = readBoundedJsonResponse(response, 100, JSON.parse, controller.signal);
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("preserves segment order, speaker, timestamps, and selected language text", () => {
    const transcript = transcriptPayload();

    expect(parseTranscriptPreview(transcript, "source", JOB_ID).segments).toEqual([
      { id: 1, startMs: 125, endMs: 2_100, speaker: "Speaker 1", text: "Hello." },
      { id: 2, startMs: 2_200, endMs: 4_999, speaker: "Speaker 2", text: "Private." },
    ]);
    expect(
      parseTranscriptPreview(transcript, "zh-CN", JOB_ID).segments.map(({ text }) => text),
    ).toEqual(["你好。", "私密。"]);
  });

  it("applies the segment rendering limit only after JSON parsing", () => {
    const transcript = transcriptPayload();
    transcript.segments = Array.from({ length: MAX_PREVIEW_SEGMENTS + 1 }, (_, index) => ({
      id: index + 1,
      start_ms: index * 2,
      end_ms: index * 2 + 1,
      speaker: "Speaker 1",
      source_language: "en",
      source_text: "source",
      translated_text: "中文",
      metadata: {},
    }));

    expect(() => parseTranscriptPreview(transcript, "source", JOB_ID)).toThrow(
      PreviewSegmentLimitError,
    );
  });

  it("rejects a transcript attributed to another Job", () => {
    const transcript = transcriptPayload();
    transcript.job_id = ARTIFACT_ID;

    expect(() => parseTranscriptPreview(transcript, "source", JOB_ID)).toThrow("预览文件格式异常");
  });
});

describe("artifact download lifecycle", () => {
  it("asks Chrome to show the save dialog when location prompting is enabled", async () => {
    const artifact = parsedArtifact();
    const client = {
      getArtifactBlob: vi.fn(() => Promise.resolve(new Blob(["artifact"]))),
    } as unknown as LocalApiClient;
    const download = vi.fn(() => Promise.resolve(23));
    const service = new ArtifactDownloadService(
      client,
      { getConnection: () => Promise.resolve({ port: 9123, token: "token" }) },
      { download },
      { createObjectURL: () => "blob:save-as", revokeObjectURL: vi.fn() },
    );

    await expect(
      service.download(JOB_ID, "Choose location", artifact, { saveAs: true }),
    ).resolves.toBe(23);
    expect(download).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        filename: `Choose location--${JOB_ID.slice(0, 8)}/source.txt`,
        saveAs: true,
      }),
    );
  });

  it.each(["success", "failure", "cancel"] as const)(
    "revokes the Blob URL after %s",
    async (outcome) => {
      const artifact = parsedArtifact();
      const client = {
        getArtifactBlob: vi.fn(() => Promise.resolve(new Blob(["artifact"]))),
      } as unknown as LocalApiClient;
      const connectionSource: ConnectionSource = {
        getConnection: vi.fn(() => Promise.resolve({ port: 9123, token: "CurrentSecretToken" })),
      };
      const download = vi.fn<(options: chrome.downloads.DownloadOptions) => Promise<number>>(() => {
        if (outcome === "success") {
          return Promise.resolve(19);
        }
        if (outcome === "cancel") {
          return Promise.reject(new DOMException("cancelled", "AbortError"));
        }
        return Promise.reject(new Error("private browser failure"));
      });
      const objectUrls = {
        createObjectURL: vi.fn(() => "blob:local-artifact"),
        revokeObjectURL: vi.fn(),
      };
      const service = new ArtifactDownloadService(
        client,
        connectionSource,
        { download },
        objectUrls,
      );

      const result = service.download(JOB_ID, "Title CurrentSecretToken", artifact);
      if (outcome === "success") {
        await expect(result).resolves.toBe(19);
      } else {
        await expect(result).rejects.toBeDefined();
      }

      expect(objectUrls.revokeObjectURL).toHaveBeenCalledExactlyOnceWith("blob:local-artifact");
      expect(download).toHaveBeenCalledTimes(1);
      const firstCall = download.mock.calls[0];
      if (firstCall === undefined) {
        throw new Error("download fixture was not called");
      }
      const filename = firstCall[0].filename ?? "";
      expect(filename).toBe(`local-video--${JOB_ID.slice(0, 8)}/source.txt`);
      expect(filename).not.toContain("CurrentSecretToken");
      expect(firstCall[0].saveAs).toBe(false);
    },
  );

  it("keeps normal download available after a preview limit failure", async () => {
    const artifact = parsedArtifact();
    const client = {
      getArtifactBlob: vi.fn(() => Promise.resolve(new Blob(["download remains available"]))),
    } as unknown as LocalApiClient;
    const service = new ArtifactDownloadService(
      client,
      { getConnection: () => Promise.resolve({ port: 9123, token: "token" }) },
      { download: () => Promise.resolve(7) },
      { createObjectURL: () => "blob:download", revokeObjectURL: vi.fn() },
    );

    await expect(
      readBoundedJsonResponse(
        responseWithStream(controlledStream([new Uint8Array(6)]).body, null),
        5,
      ),
    ).rejects.toBeInstanceOf(PreviewTooLargeError);
    await expect(service.download(JOB_ID, "Safe title", artifact)).resolves.toBe(7);
  });

  it("does not retain private Blob URL or storage errors", async () => {
    const artifact = parsedArtifact();
    const secret = "PrivateDownloadSecret";
    const client = {
      getArtifactBlob: vi.fn(() => Promise.resolve(new Blob(["artifact"]))),
    } as unknown as LocalApiClient;
    const storageFailure = new ArtifactDownloadService(
      client,
      {
        getConnection: () => Promise.reject(new Error(secret)),
      },
      { download: vi.fn(() => Promise.resolve(1)) },
      { createObjectURL: () => "blob:unused", revokeObjectURL: vi.fn() },
    );

    await expect(storageFailure.download(JOB_ID, "Title", artifact)).rejects.toMatchObject({
      kind: "notConfigured",
    });

    const blobFailure = new ArtifactDownloadService(
      client,
      { getConnection: () => Promise.resolve({ port: 9123, token: "token" }) },
      { download: vi.fn(() => Promise.resolve(1)) },
      {
        createObjectURL: () => {
          throw new Error(secret);
        },
        revokeObjectURL: vi.fn(),
      },
    );
    let caught: unknown;
    try {
      await blobFailure.download(JOB_ID, "Title", artifact);
    } catch (error) {
      caught = error;
    }
    expect(caught).toMatchObject({ kind: "server" });
    expect(JSON.stringify(caught)).not.toContain(secret);
  });

  it("sanitizes path separators and unsafe filename characters", () => {
    expect(safeDownloadDirectory('A/B:C?D*|"E"', JOB_ID, null)).toBe(
      `A_B_C_D_E_--${JOB_ID.slice(0, 8)}`,
    );
  });
});

function artifactPayload(): { items: Record<string, unknown>[] } {
  return {
    items: ARTIFACT_KINDS.map((kind, index) => {
      const id = `25274279-9f3f-45f1-8352-${(index + 1).toString(16).padStart(12, "0")}`;
      return {
        id,
        kind,
        created_at: "2026-08-23T10:00:00+00:00",
        download_url: `/api/v1/artifacts/${id}/download`,
      };
    }),
  };
}

function parsedArtifact(): JobArtifact {
  return {
    id: ARTIFACT_ID,
    jobId: JOB_ID,
    kind: "source.txt",
    createdAt: "2026-08-23T10:00:00+00:00",
    downloadPath: `/api/v1/artifacts/${ARTIFACT_ID}/download`,
  };
}

function transcriptPayload(): Record<string, unknown> & { segments: Record<string, unknown>[] } {
  return {
    schema_version: "1.0",
    job_id: JOB_ID,
    source_url: "https://example.test/video",
    title: "Example",
    duration_ms: 5_000,
    detected_language: "en",
    engine_versions: {},
    processing_options: {},
    segments: [
      {
        id: 1,
        start_ms: 125,
        end_ms: 2_100,
        speaker: "Speaker 1",
        source_language: "en",
        source_text: "Hello.",
        translated_text: "你好。",
        metadata: {},
      },
      {
        id: 2,
        start_ms: 2_200,
        end_ms: 4_999,
        speaker: "Speaker 2",
        source_language: "en",
        source_text: "Private.",
        translated_text: "私密。",
        metadata: {},
      },
    ],
    warnings: [],
  };
}

function controlledStream(chunks: readonly Uint8Array[]): {
  body: ReadableStream<Uint8Array>;
  cancel: ReturnType<typeof vi.fn>;
} {
  let index = 0;
  const cancel = vi.fn();
  return {
    body: new ReadableStream<Uint8Array>({
      pull(controller) {
        const chunk = chunks[index];
        index += 1;
        if (chunk === undefined) {
          controller.close();
        } else {
          controller.enqueue(chunk);
        }
      },
      cancel,
    }),
    cancel,
  };
}

function responseWithStream(
  body: ReadableStream<Uint8Array>,
  contentLength: string | null,
): Response {
  const headers = new Headers({ "Content-Type": "application/octet-stream" });
  if (contentLength !== null) {
    headers.set("Content-Length", contentLength);
  }
  return new Response(body, { headers });
}

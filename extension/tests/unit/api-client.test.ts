import { describe, expect, it, vi } from "vitest";

import {
  LocalApiClient,
  LocalApiTransport,
  buildLocalApiUrl,
  type ConnectionSource,
} from "../../src/api/client";
import { ARTIFACT_KINDS, type JobArtifact } from "../../src/api/contracts";
import { ApiClientError, normalizeHttpError } from "../../src/api/errors";

const TOKEN = "ClientHeaderToken123";
const PORT = 9123;
const CHECKED_AT = "2026-08-23T10:00:00+00:00";

class MutableConnectionSource implements ConnectionSource {
  port = PORT;
  token: string | null = TOKEN;
  reads = 0;

  getConnection() {
    this.reads += 1;
    return Promise.resolve({ port: this.port, token: this.token });
  }
}

describe("local API transport security", () => {
  it("constructs only the validated loopback origin", () => {
    expect(buildLocalApiUrl(PORT, "/api/v1/jobs").href).toBe("http://127.0.0.1:9123/api/v1/jobs");
    expect(buildLocalApiUrl(PORT, "/api/v1/jobs?offset=0#details").href).toBe(
      "http://127.0.0.1:9123/api/v1/jobs?offset=0#details",
    );
    expect(() => buildLocalApiUrl(0, "/health")).toThrow();
    expect(() => buildLocalApiUrl(PORT, "http://evil.test/jobs")).toThrow();
    expect(() => buildLocalApiUrl(PORT, "//evil.test/jobs")).toThrow();
    expect(() => buildLocalApiUrl(PORT, "/../secret")).toThrow();
  });

  it.each([
    ["/api/%2e%2e/health", "/health"],
    ["/api/%2E%2E/health", "/health"],
    ["/api/%2e./health", "/health"],
    ["/api/.%2e/health", "/health"],
    ["/api/%2e/health", "/api/health"],
    ["/api/./health", "/api/health"],
  ])("rejects encoded or literal dot path segment %s before fetch", async (path, normalized) => {
    const fetcher = vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({ status: "healthy" })));
    const transport = new LocalApiTransport(new MutableConnectionSource(), fetcher);

    await expect(transport.requestJson(path, "health")).rejects.toMatchObject({
      kind: "invalidResponse",
    });

    expect(fetcher).not.toHaveBeenCalled();
    expect(new URL(path, "http://127.0.0.1:9123/").pathname).toBe(normalized);
  });

  it.each([
    "/api/%/health",
    "/api/%2/health",
    "/api/%GG/health",
    "/api/v1/jobs?cursor=%GG",
    "/api/v1/jobs#view-%2",
  ])("rejects malformed percent encoding %s before fetch", async (path) => {
    const fetcher = vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({ status: "healthy" })));
    const transport = new LocalApiTransport(new MutableConnectionSource(), fetcher);

    await expect(transport.requestJson(path, "health")).rejects.toMatchObject({
      kind: "invalidResponse",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps percent encoding in query and fragment without treating it as a path segment", () => {
    const url = buildLocalApiUrl(PORT, "/api/v1/jobs?cursor=%2e%2e#view-%2E");

    expect(url.pathname).toBe("/api/v1/jobs");
    expect(url.search).toBe("?cursor=%2e%2e");
    expect(url.hash).toBe("#view-%2E");
  });

  it("reads the current token for every request and never places it in the URL", async () => {
    const connection = new MutableConnectionSource();
    const requests: { url: string; init: RequestInit }[] = [];
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      requests.push({ url: requestUrl(input), init: init ?? {} });
      return Promise.resolve(jsonResponse([]));
    });
    const transport = new LocalApiTransport(connection, fetcher);

    await transport.requestJson("/api/v1/jobs", "jobs");
    connection.token = "ReplacementToken456";
    await transport.requestJson("/api/v1/jobs", "jobs");

    expect(connection.reads).toBe(2);
    expect(requests.map(({ url }) => url)).toEqual([
      "http://127.0.0.1:9123/api/v1/jobs",
      "http://127.0.0.1:9123/api/v1/jobs",
    ]);
    expect(requests[0]?.init).toMatchObject({ redirect: "error" });
    expect(new Headers(requests[0]?.init.headers).get("X-LVT-Token")).toBe(TOKEN);
    expect(new Headers(requests[1]?.init.headers).get("X-LVT-Token")).toBe("ReplacementToken456");
    expect(JSON.stringify(requests)).not.toContain(`token=${TOKEN}`);
  });

  it("keeps health unauthenticated but requires a token for protected routes", async () => {
    const connection = new MutableConnectionSource();
    connection.token = null;
    const requests: RequestInit[] = [];
    const fetcher = vi.fn<typeof fetch>((_input, init) => {
      requests.push(init ?? {});
      return Promise.resolve(jsonResponse({ status: "healthy", version: "0.1.0" }));
    });
    const transport = new LocalApiTransport(connection, fetcher);

    await transport.requestJson("/health", "health", { authenticated: false });
    await expect(transport.requestJson("/api/v1/jobs", "jobs")).rejects.toMatchObject({
      kind: "notConfigured",
    });

    expect(new Headers(requests[0]?.headers).has("X-LVT-Token")).toBe(false);
  });

  it("invokes browser fetch without binding the transport as its receiver", async () => {
    const receivers: unknown[] = [];
    const fetcher: typeof fetch = function (this: unknown) {
      receivers.push(this);
      return Promise.resolve(jsonResponse([]));
    };
    const transport = new LocalApiTransport(new MutableConnectionSource(), fetcher);

    await transport.requestJson("/api/v1/jobs", "jobs");

    expect(receivers).toEqual([undefined]);
  });

  it("handles 204 and binary responses without JSON coercion", async () => {
    const connection = new MutableConnectionSource();
    const responses = [
      new Response(null, { status: 204 }),
      new Response(new Uint8Array([1, 2, 3]), {
        headers: { "Content-Type": "application/octet-stream" },
      }),
    ];
    const fetcher = vi.fn<typeof fetch>(() => {
      const response = responses.shift();
      return response === undefined
        ? Promise.reject(new Error("missing response fixture"))
        : Promise.resolve(response);
    });
    const transport = new LocalApiTransport(connection, fetcher);

    await expect(
      transport.requestNoContent("/api/v1/jobs/id?confirm=true"),
    ).resolves.toBeUndefined();
    const binary = await transport.requestBinary("/api/v1/artifacts/id/download");
    await expect(binary.arrayBuffer()).resolves.toEqual(new Uint8Array([1, 2, 3]).buffer);
  });
});

describe("typed API client", () => {
  it("submits batch URLs and JobOptions as authenticated JSON", async () => {
    const requests: { init: RequestInit; url: string }[] = [];
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      requests.push({ url: requestUrl(input), init: init ?? {} });
      return Promise.resolve(
        jsonResponse({
          accepted: [],
          rejected: [
            {
              url: "http://127.0.0.1/private",
              error_code: "INVALID_URL",
              message: "local targets are not allowed",
            },
          ],
        }),
      );
    });
    const client = new LocalApiClient(
      new LocalApiTransport(new MutableConnectionSource(), fetcher),
    );

    const result = await client.createJobs(
      ["https://example.test/video", "http://127.0.0.1/private"],
      {
        asrModel: "mlx-community/whisper-small-mlx",
        translateTo: "zh-CN",
        diarization: false,
      },
    );

    expect(result.rejected[0]?.url).toBe("http://127.0.0.1/private");
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe("http://127.0.0.1:9123/api/v1/jobs");
    expect(requests[0]?.init.method).toBe("POST");
    expect(new Headers(requests[0]?.init.headers).get("Content-Type")).toBe("application/json");
    const requestBody = requests[0]?.init.body;
    expect(typeof requestBody).toBe("string");
    expect(JSON.parse(requestBody as string)).toEqual({
      urls: ["https://example.test/video", "http://127.0.0.1/private"],
      options: {
        asr_model: "mlx-community/whisper-small-mlx",
        translate_to: "zh-CN",
        diarization: false,
      },
    });
  });

  it("uses fixed Job control and event routes with strict response parsing", async () => {
    const jobId = "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f";
    const requests: { method: string; url: string }[] = [];
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      const method = init?.method ?? "GET";
      requests.push({ method, url });
      if (method === "DELETE") {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (url.includes("/events?")) {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                id: 1,
                job_id: jobId,
                status: "queued",
                message: null,
                created_at: CHECKED_AT,
              },
            ],
            offset: 0,
            limit: 50,
            total: 1,
          }),
        );
      }
      return Promise.resolve(jsonResponse(jobPayload(jobId)));
    });
    const client = new LocalApiClient(
      new LocalApiTransport(new MutableConnectionSource(), fetcher),
    );

    await expect(client.getJob(jobId)).resolves.toMatchObject({ uuid: jobId });
    await expect(client.retryJob(jobId)).resolves.toMatchObject({ uuid: jobId });
    await expect(client.cancelJob(jobId)).resolves.toMatchObject({ uuid: jobId });
    await expect(client.getJobEvents(jobId, 0)).resolves.toMatchObject({
      offset: 0,
      limit: 50,
      total: 1,
    });
    await expect(client.deleteJob(jobId)).resolves.toBeUndefined();

    expect(requests).toEqual([
      { method: "GET", url: `http://127.0.0.1:9123/api/v1/jobs/${jobId}` },
      { method: "POST", url: `http://127.0.0.1:9123/api/v1/jobs/${jobId}/retry` },
      { method: "POST", url: `http://127.0.0.1:9123/api/v1/jobs/${jobId}/cancel` },
      {
        method: "GET",
        url: `http://127.0.0.1:9123/api/v1/jobs/${jobId}/events?offset=0&limit=50`,
      },
      { method: "DELETE", url: `http://127.0.0.1:9123/api/v1/jobs/${jobId}?confirm=true` },
    ]);
  });

  it("rejects non-UUID Job route input before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new LocalApiClient(
      new LocalApiTransport(new MutableConnectionSource(), fetcher),
    );

    await expect(client.cancelJob("../health")).rejects.toMatchObject({
      kind: "invalidResponse",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects events attributed to a different Job", async () => {
    const jobId = "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f";
    const fetcher = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        jsonResponse({
          items: [
            {
              id: 1,
              job_id: "25274279-9f3f-45f1-8352-79bc38f11cf6",
              status: "queued",
              message: null,
              created_at: CHECKED_AT,
            },
          ],
          offset: 0,
          limit: 50,
          total: 1,
        }),
      ),
    );
    const client = new LocalApiClient(
      new LocalApiTransport(new MutableConnectionSource(), fetcher),
    );

    await expect(client.getJobEvents(jobId, 0)).rejects.toMatchObject({
      kind: "invalidResponse",
    });
  });

  it("lists artifacts and downloads only through fixed authenticated loopback routes", async () => {
    const jobId = "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f";
    const requests: { headers: Headers; redirect: RequestRedirect | undefined; url: string }[] = [];
    const connection = new MutableConnectionSource();
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      requests.push({
        url,
        headers: new Headers(init?.headers),
        redirect: init?.redirect,
      });
      if (url.endsWith("/artifacts")) {
        return Promise.resolve(jsonResponse(artifactListPayload()));
      }
      return Promise.resolve(
        new Response("artifact bytes", {
          headers: { "Content-Type": "application/octet-stream" },
        }),
      );
    });
    const client = new LocalApiClient(new LocalApiTransport(connection, fetcher));

    const artifacts = await client.getJobArtifacts(jobId);
    const firstArtifact = artifacts[0];
    const secondArtifact = artifacts[1];
    if (firstArtifact === undefined || secondArtifact === undefined) {
      throw new Error("artifact fixture did not return eight items");
    }
    connection.token = "RotatedCurrentToken";
    const response = await client.getArtifactResponse(jobId, firstArtifact);
    const blob = await client.getArtifactBlob(jobId, secondArtifact);

    expect(artifacts).toHaveLength(8);
    expect(await response.text()).toBe("artifact bytes");
    expect(await blob.text()).toBe("artifact bytes");
    expect(requests.map(({ url }) => url)).toEqual([
      `http://127.0.0.1:9123/api/v1/jobs/${jobId}/artifacts`,
      `http://127.0.0.1:9123/api/v1/artifacts/${artifacts[0]?.id ?? ""}/download`,
      `http://127.0.0.1:9123/api/v1/artifacts/${artifacts[1]?.id ?? ""}/download`,
    ]);
    for (const request of requests.slice(1)) {
      expect(request.headers.get("X-LVT-Token")).toBe("RotatedCurrentToken");
      expect(request.headers.get("Accept")).toBe("application/octet-stream");
      expect(request.redirect).toBe("error");
      expect(request.url).not.toContain("RotatedCurrentToken");
    }
  });

  it("rejects a cross-Job or forged artifact before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new LocalApiClient(
      new LocalApiTransport(new MutableConnectionSource(), fetcher),
    );
    const artifact: JobArtifact = {
      id: "25274279-9f3f-45f1-8352-79bc38f11cf6",
      jobId: "25274279-9f3f-45f1-8352-79bc38f11cf6",
      kind: "source.txt",
      createdAt: CHECKED_AT,
      downloadPath: "/api/v1/artifacts/25274279-9f3f-45f1-8352-79bc38f11cf6/download",
    };

    await expect(
      client.getArtifactBlob("4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f", artifact),
    ).rejects.toMatchObject({ kind: "invalidResponse" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("parses a complete connection snapshot from frozen contracts", async () => {
    const connection = new MutableConnectionSource();
    const payloads: Record<string, unknown> = {
      "/health": { status: "healthy", version: "0.1.0" },
      "/api/v1/settings": {
        worker_concurrency: 1,
        runtime_effect: "new_claims_only",
      },
      "/api/v1/capabilities": capabilities(),
      "/api/v1/jobs": [],
    };
    const fetcher = vi.fn<typeof fetch>((input) => {
      const path = new URL(requestUrl(input)).pathname;
      return Promise.resolve(jsonResponse(payloads[path]));
    });
    const client = new LocalApiClient(new LocalApiTransport(connection, fetcher));

    const snapshot = await client.loadConnectionSnapshot();

    expect(snapshot.health.status).toBe("healthy");
    expect(snapshot.settings.workerConcurrency).toBe(1);
    expect(snapshot.capabilities.ttlSeconds).toBe(5);
    expect(snapshot.jobs).toEqual([]);
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it("classifies invalid JSON and malformed success payloads", async () => {
    const connection = new MutableConnectionSource();
    const invalidJson = new LocalApiClient(
      new LocalApiTransport(
        connection,
        vi.fn<typeof fetch>(() =>
          Promise.resolve(new Response("not-json", { headers: { "Content-Type": "text/plain" } })),
        ),
      ),
    );
    const malformed = new LocalApiClient(
      new LocalApiTransport(
        connection,
        vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({ status: "ready" }))),
      ),
    );

    await expect(invalidJson.getHealth()).rejects.toMatchObject({ kind: "invalidResponse" });
    await expect(malformed.getHealth()).rejects.toMatchObject({ kind: "invalidResponse" });
  });

  it("aborts sibling snapshot requests when one parallel request fails", async () => {
    const connection = new MutableConnectionSource();
    const abortedPaths = new Set<string>();
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const path = new URL(requestUrl(input)).pathname;
      if (path === "/health") {
        return Promise.resolve(jsonResponse({ status: "healthy", version: "0.1.0" }));
      }
      if (path === "/api/v1/settings") {
        return Promise.resolve(
          jsonResponse({ detail: { error_code: "SETTINGS_APPLY_FAILED", message: "unsafe" } }, 500),
        );
      }
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => {
            abortedPaths.add(path);
            reject(new DOMException("aborted", "AbortError"));
          },
          { once: true },
        );
      });
    });
    const client = new LocalApiClient(new LocalApiTransport(connection, fetcher));

    await expect(client.loadConnectionSnapshot()).rejects.toMatchObject({ kind: "server" });

    expect([...abortedPaths].sort()).toEqual(["/api/v1/capabilities", "/api/v1/jobs"]);
  });
});

describe("safe HTTP error normalization", () => {
  it.each([
    [401, "unauthorized"],
    [404, "notFound"],
    [409, "conflict"],
    [500, "server"],
    [503, "server"],
  ] as const)("maps HTTP %i to %s", async (status, kind) => {
    const error = await normalizeHttpError(
      jsonResponse(
        { detail: { error_code: "INTERNAL_ERROR", message: "unsafe backend detail" } },
        status,
      ),
      "jobs",
    );

    expect(error).toMatchObject({ kind, status });
    expect(error.message).not.toContain("unsafe backend detail");
  });

  it("distinguishes unhealthy health responses from other server failures", async () => {
    const error = await normalizeHttpError(
      jsonResponse({ status: "unhealthy", version: "0.1.0" }, 503),
      "health",
    );

    expect(error).toMatchObject({ kind: "backendUnhealthy", status: 503 });
  });

  it.each([
    ["jobs", ["body", "urls"], "提交内容格式不正确，请检查 URL 数量和任务选项"],
    ["settings", ["body", "worker_concurrency"], "并发数只能为 1 或 2"],
    ["events", ["query", "limit"], "事件分页参数无效，请重新加载时间线"],
  ] as const)("sanitizes real FastAPI 422 details for %s", async (route, loc, message) => {
    const secret = "ValidationSecretToken123";
    const url = `https://example.test/video?token=${secret}`;
    const response = jsonResponse(
      {
        detail: [
          {
            type: "greater_than_equal",
            loc,
            msg: `Value from ${url} is invalid`,
            input: { url, token: secret },
            ctx: { secret },
          },
        ],
      },
      422,
    );

    const error = await normalizeHttpError(response, route);

    expect(error).toMatchObject({ kind: "validation", message });
    expect(JSON.stringify(error)).not.toContain(secret);
    expect(JSON.stringify(error)).not.toContain(url);
    expect(JSON.stringify(error)).not.toContain("ctx");
    expect(JSON.stringify(error)).not.toContain("input");
  });

  it("rejects malformed 422 detail without reflecting its body", async () => {
    const error = await normalizeHttpError(
      jsonResponse({ detail: { input: "Secret123" } }, 422),
      "jobs",
    );

    expect(error).toMatchObject({ kind: "invalidResponse" });
    expect(JSON.stringify(error)).not.toContain("Secret123");
  });

  it("classifies network failures without retaining causes or request data", async () => {
    const secret = "NetworkSecret123";
    const connection = new MutableConnectionSource();
    connection.token = secret;
    const transport = new LocalApiTransport(
      connection,
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError(`fetch failed for token ${secret}`))),
    );

    let caught: unknown;
    try {
      await transport.requestJson("/api/v1/jobs", "jobs");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ApiClientError);
    expect(caught).toMatchObject({ kind: "unreachable" });
    expect(JSON.stringify(caught)).not.toContain(secret);
  });
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestUrl(input: URL | RequestInfo): string {
  if (typeof input === "string") {
    return input;
  }
  return input instanceof URL ? input.href : input.url;
}

function jobPayload(uuid: string): Record<string, unknown> {
  return {
    uuid,
    sanitized_display_url: "https://example.test/video",
    title: "",
    status: "queued",
    stage_progress: 0,
    overall_progress: 0,
    detected_language: null,
    attempts: 0,
    error_code: null,
    error_message: null,
    created_at: CHECKED_AT,
    updated_at: CHECKED_AT,
    started_at: null,
    finished_at: null,
    duration_ms: null,
    options: {
      asr_model: "mlx-community/whisper-small-mlx",
      translate_to: "zh-CN",
      diarization: true,
    },
    execution_count_total: 0,
    retry_cycle: 0,
    automatic_requeue_count_in_cycle: 0,
    next_attempt_at: null,
    cancel_requested_at: null,
  };
}

function artifactListPayload(): Record<string, unknown> {
  return {
    items: ARTIFACT_KINDS.map((kind, index) => {
      const id = `25274279-9f3f-45f1-8352-${(index + 1).toString(16).padStart(12, "0")}`;
      return {
        id,
        kind,
        created_at: CHECKED_AT,
        download_url: `/api/v1/artifacts/${id}/download`,
      };
    }),
  };
}

function capabilities(): Record<string, unknown> {
  const component = { status: "available", checked_at: CHECKED_AT };
  return {
    checked_at: CHECKED_AT,
    ttl_seconds: 5,
    ffmpeg: { ...component },
    ollama: { ...component },
    asr_package: { ...component },
    asr_model: { ...component, model: "mlx-community/whisper-small-mlx" },
    diarization: { ...component },
    translation_primary: { ...component, model: "hy-mt2:1.8b-q4km-fixed" },
    translation_fallback: { ...component, model: "qwen2.5:1.5b" },
  };
}

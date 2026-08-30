import {
  ARTIFACT_KINDS,
  artifactDownloadPath,
  type CapabilitiesResponse,
  type CreateJobsResponse,
  type HealthResponse,
  type Job,
  type JobArtifact,
  type JobEventsResponse,
  type JobOptions,
  type SettingsResponse,
  parseCapabilitiesResponse,
  parseCreateJobsResponse,
  parseHealthResponse,
  parseJobArtifactsResponse,
  parseJobEventsResponse,
  parseJobResponse,
  parseJobsResponse,
  parseSettingsResponse,
} from "./contracts";
import {
  ApiClientError,
  type ErrorRoute,
  normalizeHttpError,
  normalizeRequestFailure,
} from "./errors";

export type ConnectionValue = {
  port: number;
  token: string | null;
};

export type ConnectionSource = {
  getConnection(): Promise<ConnectionValue>;
};

export type ConnectionSnapshot = {
  health: HealthResponse;
  settings: SettingsResponse;
  capabilities: CapabilitiesResponse;
  jobs: Job[];
};

type RequestOptions = {
  accept?: string;
  authenticated?: boolean;
  body?: unknown;
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  pairing?: boolean;
  signal?: AbortSignal | undefined;
};

export class LocalApiTransport {
  private readonly fetcher: typeof fetch;

  constructor(
    private readonly connectionSource: ConnectionSource,
    fetcher: typeof fetch = globalThis.fetch,
  ) {
    this.fetcher = (input, init) => fetcher(input, init);
  }

  async requestJson(
    path: string,
    route: ErrorRoute,
    options: RequestOptions = {},
  ): Promise<unknown> {
    const response = await this.request(path, route, options);
    if (!isJsonResponse(response)) {
      throw invalidResponse();
    }
    try {
      return await response.json();
    } catch {
      throw invalidResponse();
    }
  }

  async requestNoContent(
    path: string,
    route: ErrorRoute = "other",
    options: RequestOptions = {},
  ): Promise<void> {
    const response = await this.request(path, route, options);
    if (response.status !== 204) {
      throw invalidResponse();
    }
  }

  async requestBinary(
    path: string,
    route: ErrorRoute = "other",
    options: RequestOptions = {},
  ): Promise<Blob> {
    const response = await this.request(path, route, options);
    const contentType = response.headers
      .get("Content-Type")
      ?.split(";", 1)[0]
      ?.trim()
      .toLowerCase();
    if (contentType !== "application/octet-stream") {
      throw invalidResponse();
    }
    const blob = await response.blob();
    if (blob.size === 0) {
      throw invalidResponse();
    }
    return blob;
  }

  async requestResponse(
    path: string,
    route: ErrorRoute = "other",
    options: RequestOptions = {},
  ): Promise<Response> {
    return this.request(path, route, options);
  }

  private async request(
    path: string,
    route: ErrorRoute,
    options: RequestOptions,
  ): Promise<Response> {
    const connection = await this.connectionSource.getConnection();
    const url = buildLocalApiUrl(connection.port, path);
    assertValidatedLocalTarget(url, connection.port);
    const authenticated = options.authenticated ?? true;
    const headers = new Headers({ Accept: options.accept ?? "application/json" });
    if (authenticated) {
      if (connection.token === null) {
        throw new ApiClientError("notConfigured", "尚未自动配对，请确认本地服务已启动");
      }
      headers.set("X-LVT-Token", connection.token);
    }
    if (options.pairing === true) {
      headers.set("X-LVT-Pairing", "1");
    }
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    let response: Response;
    try {
      const requestInit: RequestInit = {
        method: options.method ?? "GET",
        headers,
        redirect: "error",
      };
      if (options.body !== undefined) {
        requestInit.body = JSON.stringify(options.body);
      }
      if (options.signal !== undefined) {
        requestInit.signal = options.signal;
      }
      response = await this.fetcher(url, requestInit);
    } catch (error) {
      if (options.signal?.aborted === true) {
        throw new DOMException("Request aborted", "AbortError");
      }
      throw normalizeRequestFailure(error);
    }
    if (!response.ok) {
      throw await normalizeHttpError(response, route);
    }
    return response;
  }
}

export class LocalApiClient {
  constructor(private readonly transport: LocalApiTransport) {}

  async pair(signal?: AbortSignal): Promise<string> {
    const value = await this.transport.requestJson("/api/v1/pairing", "other", {
      authenticated: false,
      method: "POST",
      pairing: true,
      signal,
    });
    if (!isRecord(value) || Object.keys(value).length !== 1) {
      throw invalidResponse();
    }
    const token = value.token;
    if (
      typeof token !== "string" ||
      token.length < 32 ||
      token.length > 256 ||
      !/^[A-Za-z0-9_-]+$/u.test(token)
    ) {
      throw invalidResponse();
    }
    return token;
  }

  async getHealth(signal?: AbortSignal): Promise<HealthResponse> {
    return parseContract(
      await this.transport.requestJson("/health", "health", {
        authenticated: false,
        signal,
      }),
      parseHealthResponse,
    );
  }

  async getSettings(signal?: AbortSignal): Promise<SettingsResponse> {
    return parseContract(
      await this.transport.requestJson("/api/v1/settings", "settings", { signal }),
      parseSettingsResponse,
    );
  }

  async updateSettings(workerConcurrency: 1 | 2, signal?: AbortSignal): Promise<SettingsResponse> {
    return parseContract(
      await this.transport.requestJson("/api/v1/settings", "settings", {
        method: "PATCH",
        body: { worker_concurrency: workerConcurrency },
        signal,
      }),
      parseSettingsResponse,
    );
  }

  async getCapabilities(signal?: AbortSignal): Promise<CapabilitiesResponse> {
    return parseContract(
      await this.transport.requestJson("/api/v1/capabilities", "capabilities", { signal }),
      parseCapabilitiesResponse,
    );
  }

  async getJobs(signal?: AbortSignal): Promise<Job[]> {
    return parseContract(
      await this.transport.requestJson("/api/v1/jobs", "jobs", { signal }),
      parseJobsResponse,
    );
  }

  async createJobs(
    urls: readonly string[],
    options: JobOptions,
    signal?: AbortSignal,
  ): Promise<CreateJobsResponse> {
    return parseContract(
      await this.transport.requestJson("/api/v1/jobs", "jobs", {
        method: "POST",
        body: {
          urls,
          options: {
            asr_model: options.asrModel,
            translate_to: options.translateTo,
            diarization: options.diarization,
          },
        },
        signal,
      }),
      parseCreateJobsResponse,
    );
  }

  async getJob(jobId: string, signal?: AbortSignal): Promise<Job> {
    return parseContract(
      await this.transport.requestJson(jobPath(jobId), "jobs", { signal }),
      parseJobResponse,
    );
  }

  async retryJob(jobId: string, signal?: AbortSignal): Promise<Job> {
    return parseContract(
      await this.transport.requestJson(`${jobPath(jobId)}/retry`, "jobs", {
        method: "POST",
        signal,
      }),
      parseJobResponse,
    );
  }

  async cancelJob(jobId: string, signal?: AbortSignal): Promise<Job> {
    return parseContract(
      await this.transport.requestJson(`${jobPath(jobId)}/cancel`, "jobs", {
        method: "POST",
        signal,
      }),
      parseJobResponse,
    );
  }

  async deleteJob(jobId: string, signal?: AbortSignal): Promise<void> {
    await this.transport.requestNoContent(`${jobPath(jobId)}?confirm=true`, "jobs", {
      method: "DELETE",
      signal,
    });
  }

  async getJobEvents(
    jobId: string,
    offset: number,
    limit = 50,
    signal?: AbortSignal,
  ): Promise<JobEventsResponse> {
    if (
      !Number.isInteger(offset) ||
      offset < 0 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 100
    ) {
      throw invalidResponse();
    }
    const response = parseContract(
      await this.transport.requestJson(
        `${jobPath(jobId)}/events?offset=${String(offset)}&limit=${String(limit)}`,
        "events",
        { signal },
      ),
      parseJobEventsResponse,
    );
    if (response.items.some((event) => event.jobId !== jobId)) {
      throw invalidResponse();
    }
    return response;
  }

  async getJobArtifacts(jobId: string, signal?: AbortSignal): Promise<JobArtifact[]> {
    return parseContract(
      await this.transport.requestJson(`${jobPath(jobId)}/artifacts`, "other", { signal }),
      (value) => parseJobArtifactsResponse(value, jobId),
    );
  }

  async getArtifactResponse(
    jobId: string,
    artifact: JobArtifact,
    signal?: AbortSignal,
  ): Promise<Response> {
    return this.transport.requestResponse(artifactPathForJob(jobId, artifact), "other", {
      accept: "application/octet-stream",
      signal,
    });
  }

  async getArtifactBlob(jobId: string, artifact: JobArtifact, signal?: AbortSignal): Promise<Blob> {
    return this.transport.requestBinary(artifactPathForJob(jobId, artifact), "other", {
      accept: "application/octet-stream",
      signal,
    });
  }

  async loadConnectionSnapshot(signal?: AbortSignal): Promise<ConnectionSnapshot> {
    const batch = new AbortController();
    const abortBatch = () => batch.abort();
    if (signal?.aborted === true) {
      abortBatch();
    } else {
      signal?.addEventListener("abort", abortBatch, { once: true });
    }
    try {
      const health = await this.getHealth(batch.signal);
      try {
        const [settings, capabilities, jobs] = await Promise.all([
          this.getSettings(batch.signal),
          this.getCapabilities(batch.signal),
          this.getJobs(batch.signal),
        ]);
        return { health, settings, capabilities, jobs };
      } catch (error) {
        abortBatch();
        throw error;
      }
    } finally {
      signal?.removeEventListener("abort", abortBatch);
    }
  }
}

export function buildLocalApiUrl(port: number, path: string): URL {
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new ApiClientError("notConfigured", "本地端口必须是 1 到 65535 的整数");
  }
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    throw invalidApiPath();
  }
  assertSafePathSegments(path);
  const baseUrl = new URL("http://127.0.0.1/");
  baseUrl.port = String(port);
  const origin = baseUrl.origin;
  const url = new URL(path, baseUrl);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.port !== String(port) ||
    url.origin !== origin
  ) {
    throw new ApiClientError("invalidResponse", "本地 API 地址无效");
  }
  return url;
}

function assertSafePathSegments(path: string): void {
  try {
    decodeURIComponent(path);
  } catch {
    throw invalidApiPath();
  }
  const suffixIndex = path.search(/[?#]/u);
  const pathname = suffixIndex === -1 ? path : path.slice(0, suffixIndex);
  for (const segment of pathname.split("/")) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      throw invalidApiPath();
    }
    if (segment === "." || segment === ".." || decoded === "." || decoded === "..") {
      throw invalidApiPath();
    }
  }
}

function invalidApiPath(): ApiClientError {
  return new ApiClientError("invalidResponse", "本地 API 路径无效");
}

function jobPath(jobId: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(jobId)) {
    throw invalidApiPath();
  }
  return `/api/v1/jobs/${encodeURIComponent(jobId)}`;
}

function artifactPathForJob(jobId: string, artifact: JobArtifact): string {
  jobPath(jobId);
  const expectedPath = artifactDownloadPath(artifact.id);
  if (
    artifact.jobId !== jobId ||
    artifact.downloadPath !== expectedPath ||
    !ARTIFACT_KINDS.includes(artifact.kind)
  ) {
    throw invalidResponse();
  }
  return expectedPath;
}

function assertValidatedLocalTarget(url: URL, port: number): void {
  const expected = new URL("http://127.0.0.1/");
  expected.port = String(port);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.port !== String(port) ||
    url.origin !== expected.origin
  ) {
    throw invalidResponse();
  }
}

function isJsonResponse(response: Response): boolean {
  return response.headers.get("Content-Type")?.toLowerCase().includes("application/json") === true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseContract<T>(value: unknown, parser: (input: unknown) => T): T {
  try {
    return parser(value);
  } catch {
    throw invalidResponse();
  }
}

function invalidResponse(): ApiClientError {
  return new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
}

import {
  type CapabilitiesResponse,
  type HealthResponse,
  type Job,
  type SettingsResponse,
  parseCapabilitiesResponse,
  parseHealthResponse,
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
  authenticated?: boolean;
  method?: "GET" | "POST" | "PATCH" | "DELETE";
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
    const contentType = response.headers.get("Content-Type");
    if (contentType === null || contentType.toLowerCase().includes("application/json")) {
      throw invalidResponse();
    }
    const blob = await response.blob();
    if (blob.size === 0) {
      throw invalidResponse();
    }
    return blob;
  }

  private async request(
    path: string,
    route: ErrorRoute,
    options: RequestOptions,
  ): Promise<Response> {
    const connection = await this.connectionSource.getConnection();
    const url = buildLocalApiUrl(connection.port, path);
    const authenticated = options.authenticated ?? true;
    const headers = new Headers({ Accept: "application/json" });
    if (authenticated) {
      if (connection.token === null) {
        throw new ApiClientError("notConfigured", "请先设置本地端口和配对 Token");
      }
      headers.set("X-LVT-Token", connection.token);
    }
    let response: Response;
    try {
      const requestInit: RequestInit = {
        method: options.method ?? "GET",
        headers,
        redirect: "error",
      };
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

function isJsonResponse(response: Response): boolean {
  return response.headers.get("Content-Type")?.toLowerCase().includes("application/json") === true;
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

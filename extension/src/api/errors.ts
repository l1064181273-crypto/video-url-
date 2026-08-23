export const API_ERROR_KINDS = [
  "notConfigured",
  "unreachable",
  "unauthorized",
  "backendUnhealthy",
  "validation",
  "conflict",
  "notFound",
  "server",
  "invalidResponse",
] as const;

export type ApiErrorKind = (typeof API_ERROR_KINDS)[number];
export type ValidationRoute = "jobs" | "settings" | "events";
export type ErrorRoute = ValidationRoute | "health" | "capabilities" | "other";

const VALIDATION_MESSAGES: Record<ValidationRoute, string> = {
  jobs: "提交内容格式不正确，请检查 URL 数量和任务选项",
  settings: "并发数只能为 1 或 2",
  events: "事件分页参数无效，请重新加载时间线",
};

const VALIDATION_FIELDS: Record<ValidationRoute, ReadonlySet<string>> = {
  jobs: new Set(["urls", "options", "asr_model", "translate_to", "diarization"]),
  settings: new Set(["worker_concurrency"]),
  events: new Set(["offset", "limit"]),
};

export class ApiClientError extends Error {
  override readonly name = "ApiClientError";

  constructor(
    readonly kind: ApiErrorKind,
    message: string,
    readonly status?: number,
    readonly validationFields: readonly string[] = [],
  ) {
    super(message);
  }

  toJSON(): Record<string, unknown> {
    const value: Record<string, unknown> = {
      name: this.name,
      kind: this.kind,
      message: this.message,
      validationFields: this.validationFields,
    };
    if (this.status !== undefined) {
      value.status = this.status;
    }
    return value;
  }
}

export async function normalizeHttpError(
  response: Response,
  route: ErrorRoute,
): Promise<ApiClientError> {
  if (response.status === 422) {
    return normalizeValidationError(response, route);
  }
  if (response.status === 401) {
    return new ApiClientError("unauthorized", "配对 Token 无效，请重新输入", 401);
  }
  if (response.status === 404) {
    return new ApiClientError("notFound", "请求的本地资源不存在", 404);
  }
  if (response.status === 409) {
    return new ApiClientError("conflict", "任务状态已变化，请刷新后重试", 409);
  }
  if (response.status === 503 && route === "health") {
    return new ApiClientError(
      "backendUnhealthy",
      "服务已连接，但 worker 当前异常，请检查本地服务",
      503,
    );
  }
  if (response.status >= 500) {
    return new ApiClientError("server", "本地服务处理失败，请稍后重试", response.status);
  }
  return new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
}

export function normalizeRequestFailure(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) {
    return error;
  }
  return new ApiClientError("unreachable", "本地服务未启动，请先启动 Local Video Transcriber");
}

function isValidationRoute(route: ErrorRoute): route is ValidationRoute {
  return route === "jobs" || route === "settings" || route === "events";
}

async function normalizeValidationError(
  response: Response,
  route: ErrorRoute,
): Promise<ApiClientError> {
  if (!isValidationRoute(route)) {
    return new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
  }
  const body = await readJsonSafely(response);
  if (!isRecord(body) || !Array.isArray(body.detail)) {
    return new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
  }
  const fields = new Set<string>();
  for (const item of body.detail) {
    if (!isRecord(item) || !Array.isArray(item.loc)) {
      return new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
    }
    for (const part of item.loc) {
      if (typeof part === "string" && VALIDATION_FIELDS[route].has(part)) {
        fields.add(part);
      }
    }
  }
  return new ApiClientError("validation", VALIDATION_MESSAGES[route], 422, [...fields].sort());
}

async function readJsonSafely(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

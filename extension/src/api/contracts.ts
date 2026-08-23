export const CONTRACT_VERSION = "phase-3-checkpoint-5a";

export const JOB_STATUSES = [
  "queued",
  "downloading",
  "extracting",
  "transcribing",
  "diarizing",
  "segmenting",
  "translating",
  "exporting",
  "completed",
  "failed",
  "cancelling",
  "cancelled",
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export const CAPABILITY_STATUSES = ["available", "missing", "unavailable", "unchecked"] as const;

export type CapabilityStatus = (typeof CAPABILITY_STATUSES)[number];

export const ERROR_CODES = [
  "INVALID_URL",
  "DOWNLOAD_UNSUPPORTED",
  "DOWNLOAD_FAILED",
  "FFMPEG_NOT_FOUND",
  "MEDIA_INVALID",
  "ASR_MODEL_MISSING",
  "TRANSCRIPTION_FAILED",
  "DIARIZATION_TOKEN_REQUIRED",
  "DIARIZATION_MODEL_MISSING",
  "DIARIZATION_FAILED",
  "UNSUPPORTED_SOURCE_LANGUAGE",
  "OLLAMA_UNAVAILABLE",
  "TRANSLATION_MODEL_MISSING",
  "TRANSLATION_INVALID_RESPONSE",
  "TRANSLATION_FAILED",
  "TRANSLATION_ALL_MODELS_FAILED",
  "EXPORT_FAILED",
  "DISK_SPACE_LOW",
  "CANCELLED_BY_USER",
  "INTERNAL_ERROR",
  "UNAUTHORIZED",
  "CAPABILITIES_UNAVAILABLE",
  "JOB_NOT_FOUND",
  "JOB_STATE_CONFLICT",
  "RETRY_NOT_ALLOWED",
  "DELETE_CONFIRMATION_REQUIRED",
  "UNSAFE_JOB_PATH",
  "DELETE_FAILED",
  "DELETE_CLEANUP_PENDING",
  "ARTIFACT_NOT_FOUND",
  "SETTINGS_APPLY_FAILED",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

export type WorkerHealth = {
  status: "healthy" | "unhealthy";
  configuredWorkers: number;
  liveWorkers: number;
  fatalCount: number;
};

export type HealthResponse = {
  status: "healthy" | "unhealthy";
  version: string;
  worker?: WorkerHealth;
};

export type SettingsResponse = {
  workerConcurrency: 1 | 2;
  runtimeEffect: "new_claims_only" | "persisted_for_next_worker_start";
};

export type JobOptions = {
  asrModel: string;
  translateTo: string;
  diarization: boolean;
};

export type Job = {
  uuid: string;
  sanitizedDisplayUrl: string;
  title: string;
  status: JobStatus;
  stageProgress: number;
  overallProgress: number;
  detectedLanguage: string | null;
  attempts: number;
  errorCode: ErrorCode | null;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  options: JobOptions;
  executionCountTotal: number;
  retryCycle: number;
  automaticRequeueCountInCycle: number;
  nextAttemptAt: string | null;
  cancelRequestedAt: string | null;
};

export type RejectedJob = {
  url: string;
  errorCode: "INVALID_URL";
  message: string;
};

export type CreateJobsResponse = {
  accepted: Job[];
  rejected: RejectedJob[];
};

export const JOB_EVENT_TYPES = [
  "created",
  "claimed",
  "stage_changed",
  "progress",
  "checkpoint_published",
  "automatic_requeued",
  "manual_retry",
  "cancel_requested",
  "interrupted",
  "completed",
  "failed",
  "cancelled",
  "artifact_unavailable",
] as const;

export type JobEventType = (typeof JOB_EVENT_TYPES)[number] | JobStatus;

export type JobEvent = {
  id: number;
  jobId: string;
  status: JobEventType;
  message: string | null;
  createdAt: string;
};

export type JobEventsResponse = {
  items: JobEvent[];
  offset: number;
  limit: number;
  total: number;
};

export const ARTIFACT_KINDS = [
  "source.txt",
  "source.srt",
  "source.vtt",
  "source.json",
  "zh-CN.txt",
  "zh-CN.srt",
  "zh-CN.vtt",
  "zh-CN.json",
] as const;

export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export type JobArtifact = {
  id: string;
  jobId: string;
  kind: ArtifactKind;
  createdAt: string;
  downloadPath: string;
};

export const CAPABILITY_COMPONENTS = [
  "ffmpeg",
  "ollama",
  "asr_package",
  "asr_model",
  "diarization",
  "translation_primary",
  "translation_fallback",
] as const;

export type CapabilityComponentName = (typeof CAPABILITY_COMPONENTS)[number];

export type CapabilityComponent = {
  status: CapabilityStatus;
  checkedAt: string;
  model?: string;
};

export type CapabilitiesResponse = {
  checkedAt: string;
  ttlSeconds: 5;
  components: Record<CapabilityComponentName, CapabilityComponent>;
};

export type ApiErrorResponse = {
  errorCode: ErrorCode;
  message: string;
};

export class ContractError extends Error {
  override readonly name = "ContractError";

  constructor(
    readonly path: string,
    message: string,
  ) {
    super(`${path}: ${message}`);
  }
}

const JOB_STATUS_SET = new Set<string>(JOB_STATUSES);
const JOB_EVENT_TYPE_SET = new Set<string>([...JOB_EVENT_TYPES, ...JOB_STATUSES]);
const ARTIFACT_KIND_SET = new Set<string>(ARTIFACT_KINDS);
const CAPABILITY_STATUS_SET = new Set<string>(CAPABILITY_STATUSES);
const ERROR_CODE_SET = new Set<string>(ERROR_CODES);
const MODEL_COMPONENTS = new Set<CapabilityComponentName>([
  "asr_model",
  "translation_primary",
  "translation_fallback",
]);
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function parseCapabilitiesResponse(value: unknown): CapabilitiesResponse {
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["checked_at", "ttl_seconds", ...CAPABILITY_COMPONENTS]);
  const checkedAt = expectTimestamp(root.checked_at, "$.checked_at");
  if (root.ttl_seconds !== 5) {
    throw new ContractError("$.ttl_seconds", "expected fixed value 5");
  }

  const components = {} as Record<CapabilityComponentName, CapabilityComponent>;
  for (const name of CAPABILITY_COMPONENTS) {
    const raw = expectRecord(root[name], `$.${name}`);
    const permitsModel = MODEL_COMPONENTS.has(name);
    expectExactKeys(
      raw,
      `$.${name}`,
      permitsModel ? ["status", "checked_at", "model"] : ["status", "checked_at"],
    );
    const componentCheckedAt = expectTimestamp(raw.checked_at, `$.${name}.checked_at`);
    if (componentCheckedAt !== checkedAt) {
      throw new ContractError(`$.${name}.checked_at`, "must match top-level checked_at");
    }
    const component: CapabilityComponent = {
      status: expectEnum(raw.status, CAPABILITY_STATUS_SET, `$.${name}.status`) as CapabilityStatus,
      checkedAt: componentCheckedAt,
    };
    if (permitsModel) {
      component.model = expectNonEmptyString(raw.model, `$.${name}.model`);
    }
    components[name] = component;
  }

  return { checkedAt, ttlSeconds: 5, components };
}

export function parseHealthResponse(value: unknown): HealthResponse {
  const root = expectRecord(value, "$");
  expectAllowedKeys(root, "$", ["status", "version", "worker"]);
  const status = expectEnum(
    root.status,
    new Set(["healthy", "unhealthy"]),
    "$.status",
  ) as HealthResponse["status"];
  const response: HealthResponse = {
    status,
    version: expectNonEmptyString(root.version, "$.version"),
  };
  if (root.worker !== undefined) {
    const worker = expectRecord(root.worker, "$.worker");
    expectExactKeys(worker, "$.worker", [
      "status",
      "configured_workers",
      "live_workers",
      "fatal_count",
    ]);
    response.worker = {
      status: expectEnum(
        worker.status,
        new Set(["healthy", "unhealthy"]),
        "$.worker.status",
      ) as WorkerHealth["status"],
      configuredWorkers: expectInteger(worker.configured_workers, "$.worker.configured_workers", 0),
      liveWorkers: expectInteger(worker.live_workers, "$.worker.live_workers", 0),
      fatalCount: expectInteger(worker.fatal_count, "$.worker.fatal_count", 0),
    };
  }
  return response;
}

export function parseSettingsResponse(value: unknown): SettingsResponse {
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["worker_concurrency", "runtime_effect"]);
  if (root.worker_concurrency !== 1 && root.worker_concurrency !== 2) {
    throw new ContractError("$.worker_concurrency", "expected 1 or 2");
  }
  const runtimeEffect = expectEnum(
    root.runtime_effect,
    new Set(["new_claims_only", "persisted_for_next_worker_start"]),
    "$.runtime_effect",
  ) as SettingsResponse["runtimeEffect"];
  return {
    workerConcurrency: root.worker_concurrency,
    runtimeEffect,
  };
}

export function parseJobsResponse(value: unknown): Job[] {
  if (!Array.isArray(value)) {
    throw new ContractError("$", "expected an array");
  }
  return value.map((job, index) => parseJob(job, `$[${String(index)}]`));
}

export function parseJobResponse(value: unknown): Job {
  return parseJob(value, "$");
}

export function parseCreateJobsResponse(value: unknown): CreateJobsResponse {
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["accepted", "rejected"]);
  if (!Array.isArray(root.accepted) || !Array.isArray(root.rejected)) {
    throw new ContractError("$", "expected accepted and rejected arrays");
  }
  return {
    accepted: root.accepted.map((job, index) => parseJob(job, `$.accepted[${String(index)}]`)),
    rejected: root.rejected.map((value, index) => {
      const path = `$.rejected[${String(index)}]`;
      const rejected = expectRecord(value, path);
      expectExactKeys(rejected, path, ["url", "error_code", "message"]);
      if (rejected.error_code !== "INVALID_URL") {
        throw new ContractError(`${path}.error_code`, "expected INVALID_URL");
      }
      return {
        url: expectNonEmptyString(rejected.url, `${path}.url`),
        errorCode: "INVALID_URL",
        message: expectNonEmptyString(rejected.message, `${path}.message`),
      };
    }),
  };
}

export function parseApiErrorResponse(value: unknown): ApiErrorResponse {
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["detail"]);
  const detail = expectRecord(root.detail, "$.detail");
  expectExactKeys(detail, "$.detail", ["error_code", "message"]);
  return {
    errorCode: expectEnum(detail.error_code, ERROR_CODE_SET, "$.detail.error_code") as ErrorCode,
    message: expectNonEmptyString(detail.message, "$.detail.message"),
  };
}

export function parseJobEventsResponse(value: unknown): JobEventsResponse {
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["items", "offset", "limit", "total"]);
  if (!Array.isArray(root.items)) {
    throw new ContractError("$.items", "expected an array");
  }
  const offset = expectInteger(root.offset, "$.offset", 0);
  const limit = expectInteger(root.limit, "$.limit", 1, 100);
  const total = expectInteger(root.total, "$.total", 0);
  const items = root.items.map((value, index) => {
    const path = `$.items[${String(index)}]`;
    const event = expectRecord(value, path);
    expectExactKeys(event, path, ["id", "job_id", "status", "message", "created_at"]);
    return {
      id: expectInteger(event.id, `${path}.id`, 1),
      jobId: expectUuid(event.job_id, `${path}.job_id`),
      status: expectEnum(event.status, JOB_EVENT_TYPE_SET, `${path}.status`) as JobEventType,
      message: expectNullableString(event.message, `${path}.message`),
      createdAt: expectTimestamp(event.created_at, `${path}.created_at`),
    };
  });
  if (items.some((event) => event.jobId !== items[0]?.jobId)) {
    throw new ContractError("$.items", "all events must belong to one job");
  }
  if (offset + items.length > total) {
    throw new ContractError("$.total", "cannot be smaller than the returned page");
  }
  return { items, offset, limit, total };
}

export function parseJobArtifactsResponse(value: unknown, jobId: string): JobArtifact[] {
  const canonicalJobId = expectUuid(jobId, "$jobId");
  const root = expectRecord(value, "$");
  expectExactKeys(root, "$", ["items"]);
  if (!Array.isArray(root.items) || root.items.length !== ARTIFACT_KINDS.length) {
    throw new ContractError("$.items", "expected exactly eight artifacts");
  }
  const seenIds = new Set<string>();
  const seenKinds = new Set<ArtifactKind>();
  const artifacts = root.items.map((value, index) => {
    const path = `$.items[${String(index)}]`;
    const item = expectRecord(value, path);
    expectExactKeys(item, path, ["id", "kind", "created_at", "download_url"]);
    const id = expectArtifactId(item.id, `${path}.id`);
    const kind = expectEnum(item.kind, ARTIFACT_KIND_SET, `${path}.kind`) as ArtifactKind;
    const downloadPath = artifactDownloadPath(id);
    if (item.download_url !== downloadPath) {
      throw new ContractError(`${path}.download_url`, "must match the fixed artifact route");
    }
    if (seenIds.has(id) || seenKinds.has(kind)) {
      throw new ContractError(path, "artifact IDs and kinds must be unique");
    }
    seenIds.add(id);
    seenKinds.add(kind);
    return {
      id,
      jobId: canonicalJobId,
      kind,
      createdAt: expectTimestamp(item.created_at, `${path}.created_at`),
      downloadPath,
    };
  });
  if (ARTIFACT_KINDS.some((kind) => !seenKinds.has(kind))) {
    throw new ContractError("$.items", "artifact kinds do not match the required set");
  }
  return artifacts;
}

export function artifactDownloadPath(artifactId: string): string {
  const id = expectArtifactId(artifactId, "$artifactId");
  return `/api/v1/artifacts/${encodeURIComponent(id)}/download`;
}

function parseJob(value: unknown, path: string): Job {
  const job = expectRecord(value, path);
  return {
    uuid: expectUuid(job.uuid, `${path}.uuid`),
    sanitizedDisplayUrl: expectNonEmptyString(
      job.sanitized_display_url,
      `${path}.sanitized_display_url`,
    ),
    title: expectString(job.title, `${path}.title`),
    status: expectEnum(job.status, JOB_STATUS_SET, `${path}.status`) as JobStatus,
    stageProgress: expectInteger(job.stage_progress, `${path}.stage_progress`, 0, 100),
    overallProgress: expectInteger(job.overall_progress, `${path}.overall_progress`, 0, 100),
    detectedLanguage: expectNullableString(job.detected_language, `${path}.detected_language`),
    attempts: expectInteger(job.attempts, `${path}.attempts`, 0),
    errorCode:
      job.error_code === null
        ? null
        : (expectEnum(job.error_code, ERROR_CODE_SET, `${path}.error_code`) as ErrorCode),
    errorMessage: expectNullableString(job.error_message, `${path}.error_message`),
    createdAt: expectTimestamp(job.created_at, `${path}.created_at`),
    updatedAt: expectTimestamp(job.updated_at, `${path}.updated_at`),
    startedAt: expectNullableTimestamp(job.started_at, `${path}.started_at`),
    finishedAt: expectNullableTimestamp(job.finished_at, `${path}.finished_at`),
    durationMs: expectNullableInteger(job.duration_ms, `${path}.duration_ms`, 0),
    options: parseJobOptions(job.options, `${path}.options`),
    executionCountTotal: expectInteger(
      job.execution_count_total,
      `${path}.execution_count_total`,
      0,
    ),
    retryCycle: expectInteger(job.retry_cycle, `${path}.retry_cycle`, 0),
    automaticRequeueCountInCycle: expectInteger(
      job.automatic_requeue_count_in_cycle,
      `${path}.automatic_requeue_count_in_cycle`,
      0,
    ),
    nextAttemptAt: expectNullableTimestamp(job.next_attempt_at, `${path}.next_attempt_at`),
    cancelRequestedAt: expectNullableTimestamp(
      job.cancel_requested_at,
      `${path}.cancel_requested_at`,
    ),
  };
}

function expectArtifactId(value: unknown, path: string): string {
  const id = expectNonEmptyString(value, path);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(id)) {
    throw new ContractError(path, "expected a canonical lowercase UUID");
  }
  return id;
}

function parseJobOptions(value: unknown, path: string): JobOptions {
  const options = expectRecord(value, path);
  expectExactKeys(options, path, ["asr_model", "translate_to", "diarization"]);
  return {
    asrModel: expectNonEmptyString(options.asr_model, `${path}.asr_model`),
    translateTo: expectNonEmptyString(options.translate_to, `${path}.translate_to`),
    diarization: expectBoolean(options.diarization, `${path}.diarization`),
  };
}

function expectRecord(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractError(path, "expected an object");
  }
  return value as Record<string, unknown>;
}

function expectExactKeys(
  value: Record<string, unknown>,
  path: string,
  keys: readonly string[],
): void {
  const expected = new Set(keys);
  for (const key of keys) {
    if (!(key in value)) {
      throw new ContractError(`${path}.${key}`, "missing required field");
    }
  }
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) {
      throw new ContractError(`${path}.${key}`, "unexpected field");
    }
  }
}

function expectAllowedKeys(
  value: Record<string, unknown>,
  path: string,
  keys: readonly string[],
): void {
  const allowed = new Set(keys);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new ContractError(`${path}.${key}`, "unexpected field");
    }
  }
  for (const key of ["status", "version"]) {
    if (!(key in value)) {
      throw new ContractError(`${path}.${key}`, "missing required field");
    }
  }
}

function expectString(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw new ContractError(path, "expected a string");
  }
  return value;
}

function expectNonEmptyString(value: unknown, path: string): string {
  const result = expectString(value, path);
  if (result.length === 0) {
    throw new ContractError(path, "expected a non-empty string");
  }
  return result;
}

function expectBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw new ContractError(path, "expected a boolean");
  }
  return value;
}

function expectEnum(value: unknown, allowed: ReadonlySet<string>, path: string): string {
  const result = expectString(value, path);
  if (!allowed.has(result)) {
    throw new ContractError(path, "unexpected enum value");
  }
  return result;
}

function expectInteger(
  value: unknown,
  path: string,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (!Number.isInteger(value) || typeof value !== "number") {
    throw new ContractError(path, "expected an integer");
  }
  if (value < minimum || value > maximum) {
    throw new ContractError(path, "integer is outside the accepted range");
  }
  return value;
}

function expectNullableInteger(value: unknown, path: string, minimum: number): number | null {
  return value === null ? null : expectInteger(value, path, minimum);
}

function expectNullableString(value: unknown, path: string): string | null {
  return value === null ? null : expectString(value, path);
}

function expectTimestamp(value: unknown, path: string): string {
  const timestamp = expectString(value, path);
  if (!ISO_TIMESTAMP.test(timestamp) || Number.isNaN(Date.parse(timestamp))) {
    throw new ContractError(path, "expected an ISO 8601 timestamp with timezone");
  }
  return timestamp;
}

function expectNullableTimestamp(value: unknown, path: string): string | null {
  return value === null ? null : expectTimestamp(value, path);
}

function expectUuid(value: unknown, path: string): string {
  const uuid = expectString(value, path);
  if (!UUID.test(uuid)) {
    throw new ContractError(path, "expected a UUID");
  }
  return uuid;
}

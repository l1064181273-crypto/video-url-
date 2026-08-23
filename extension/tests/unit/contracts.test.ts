import { describe, expect, it } from "vitest";

import {
  CAPABILITY_COMPONENTS,
  CAPABILITY_STATUSES,
  ContractError,
  ERROR_CODES,
  JOB_STATUSES,
  parseApiErrorResponse,
  parseCapabilitiesResponse,
  parseCreateJobsResponse,
  parseHealthResponse,
  parseJobEventsResponse,
  parseJobsResponse,
  parseSettingsResponse,
} from "../../src/api/contracts";

const CHECKED_AT = "2026-08-23T10:00:00+00:00";

describe("capabilities contract", () => {
  it.each(CAPABILITY_STATUSES)("accepts all seven components in state %s", (status) => {
    const parsed = parseCapabilitiesResponse(capabilities(status));

    expect(Object.keys(parsed.components)).toEqual(CAPABILITY_COMPONENTS);
    expect(parsed.ttlSeconds).toBe(5);
    for (const name of CAPABILITY_COMPONENTS) {
      expect(parsed.components[name].status).toBe(status);
      expect(parsed.components[name].checkedAt).toBe(CHECKED_AT);
    }
  });

  it("allows model only on the three configured model components", () => {
    const parsed = parseCapabilitiesResponse(capabilities("available"));

    expect(parsed.components.asr_model.model).toBe("mlx-community/whisper-small-mlx");
    expect(parsed.components.translation_primary.model).toBe("hy-mt2:1.8b-q4km-fixed");
    expect(parsed.components.translation_fallback.model).toBe("qwen2.5:1.5b");
    expect(parsed.components.ffmpeg.model).toBeUndefined();
    expect(parsed.components.ollama.model).toBeUndefined();
    expect(parsed.components.asr_package.model).toBeUndefined();
    expect(parsed.components.diarization.model).toBeUndefined();
  });

  it.each([
    ["unknown state", () => mutateCapabilities("ffmpeg", "status", "ready")],
    ["wrong TTL", () => ({ ...capabilities("available"), ttl_seconds: 10 })],
    ["missing component", () => withoutKey(capabilities("available"), "ollama")],
    ["version metadata", () => mutateCapabilities("ffmpeg", "version", "7.0")],
    ["model on service", () => mutateCapabilities("ollama", "model", "secret")],
    ["missing configured model", () => withoutComponentKey("asr_model", "model")],
    [
      "mismatched checked_at",
      () => mutateCapabilities("diarization", "checked_at", "2026-08-23T10:00:01+00:00"),
    ],
  ])("rejects %s", (_label, buildInvalid) => {
    expect(() => parseCapabilitiesResponse(buildInvalid())).toThrow(ContractError);
  });
});

describe("frozen public DTOs", () => {
  it("accepts health and settings responses", () => {
    expect(
      parseHealthResponse({
        status: "healthy",
        version: "0.1.0",
        worker: {
          status: "healthy",
          configured_workers: 2,
          live_workers: 2,
          fatal_count: 0,
        },
      }),
    ).toEqual({
      status: "healthy",
      version: "0.1.0",
      worker: {
        status: "healthy",
        configuredWorkers: 2,
        liveWorkers: 2,
        fatalCount: 0,
      },
    });
    expect(
      parseSettingsResponse({
        worker_concurrency: 1,
        runtime_effect: "new_claims_only",
      }),
    ).toEqual({
      workerConcurrency: 1,
      runtimeEffect: "new_claims_only",
    });
  });

  it("accepts a real job shape while discarding unsafe extra fields", () => {
    const parsed = parseJobsResponse([
      {
        uuid: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
        original_url: "https://example.test/video?token=private",
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
        active_run_id: null,
        execution_count_total: 0,
        retry_cycle: 0,
        automatic_requeue_count_in_cycle: 0,
        next_attempt_at: CHECKED_AT,
        cancel_requested_at: null,
      },
    ]);

    expect(parsed).toHaveLength(1);
    expect(parsed[0]?.sanitizedDisplayUrl).toBe("https://example.test/video");
    expect(parsed[0]).not.toHaveProperty("original_url");
    expect(parsed[0]).not.toHaveProperty("active_run_id");
  });

  it("parses accepted and rejected batch submission results", () => {
    const parsed = parseCreateJobsResponse({
      accepted: [validJob()],
      rejected: [
        {
          url: "http://127.0.0.1/private",
          error_code: "INVALID_URL",
          message: "local targets are not allowed",
        },
      ],
    });

    expect(parsed.accepted[0]?.uuid).toBe("4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f");
    expect(parsed.rejected).toEqual([
      {
        url: "http://127.0.0.1/private",
        errorCode: "INVALID_URL",
        message: "local targets are not allowed",
      },
    ]);
  });

  it("parses paginated job events", () => {
    const parsed = parseJobEventsResponse({
      items: [
        {
          id: 7,
          job_id: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
          status: "manual_retry",
          message: '{"from_status":"failed"}',
          created_at: CHECKED_AT,
        },
      ],
      offset: 0,
      limit: 50,
      total: 1,
    });

    expect(parsed).toEqual({
      items: [
        {
          id: 7,
          jobId: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
          status: "manual_retry",
          message: '{"from_status":"failed"}',
          createdAt: CHECKED_AT,
        },
      ],
      offset: 0,
      limit: 50,
      total: 1,
    });
  });

  it("locks every Job status and public error code", () => {
    expect(JOB_STATUSES).toHaveLength(12);
    expect(new Set(JOB_STATUSES).size).toBe(JOB_STATUSES.length);
    expect(ERROR_CODES).toContain("INVALID_URL");
    expect(ERROR_CODES).toContain("CAPABILITIES_UNAVAILABLE");
    expect(ERROR_CODES).toContain("SETTINGS_APPLY_FAILED");
    expect(new Set(ERROR_CODES).size).toBe(ERROR_CODES.length);

    expect(
      parseApiErrorResponse({
        detail: {
          error_code: "UNAUTHORIZED",
          message: "配对 Token 无效",
        },
      }),
    ).toEqual({
      errorCode: "UNAUTHORIZED",
      message: "配对 Token 无效",
    });
  });

  it.each([
    ["bad health", () => parseHealthResponse({ status: "ready", version: "0.1.0" })],
    [
      "bad settings",
      () =>
        parseSettingsResponse({
          worker_concurrency: 3,
          runtime_effect: "new_claims_only",
        }),
    ],
    [
      "bad job status",
      () =>
        parseJobsResponse([
          {
            ...validJob(),
            status: "running",
          },
        ]),
    ],
    [
      "unknown error",
      () =>
        parseApiErrorResponse({
          detail: {
            error_code: "SECRET_BACKEND_ERROR",
            message: "bad",
          },
        }),
    ],
    [
      "unknown rejected code",
      () =>
        parseCreateJobsResponse({
          accepted: [],
          rejected: [{ url: "https://example.test", error_code: "INTERNAL_ERROR", message: "x" }],
        }),
    ],
    [
      "unknown event status",
      () =>
        parseJobEventsResponse({
          items: [
            {
              id: 1,
              job_id: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
              status: "private_internal_event",
              message: null,
              created_at: CHECKED_AT,
            },
          ],
          offset: 0,
          limit: 50,
          total: 1,
        }),
    ],
  ])("rejects %s", (_label, parseInvalid) => {
    expect(parseInvalid).toThrow(ContractError);
  });
});

function capabilities(status: (typeof CAPABILITY_STATUSES)[number]): Record<string, unknown> {
  const component = { status, checked_at: CHECKED_AT };
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

function mutateCapabilities(
  component: string,
  field: string,
  value: unknown,
): Record<string, unknown> {
  const payload = capabilities("available");
  payload[component] = {
    ...(payload[component] as Record<string, unknown>),
    [field]: value,
  };
  return payload;
}

function withoutComponentKey(component: string, key: string): Record<string, unknown> {
  const payload = capabilities("available");
  const updated = Object.fromEntries(
    Object.entries(payload[component] as Record<string, unknown>).filter(
      ([entryKey]) => entryKey !== key,
    ),
  );
  payload[component] = updated;
  return payload;
}

function withoutKey(value: Record<string, unknown>, key: string): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([entryKey]) => entryKey !== key));
}

function validJob(): Record<string, unknown> {
  return {
    uuid: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
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
    next_attempt_at: CHECKED_AT,
    cancel_requested_at: null,
  };
}

import { describe, expect, it } from "vitest";

import {
  CAPABILITY_COMPONENTS,
  CAPABILITY_STATUSES,
  ERROR_CODES,
  type CapabilitiesResponse,
  type CapabilityStatus,
} from "../../src/api/contracts";
import { ApiClientError } from "../../src/api/errors";
import {
  capabilityAdvice,
  capabilityPresentations,
  jobErrorAdvice,
  settingsErrorMessage,
  SettingsUpdateGate,
} from "../../src/ui/diagnostics";

const CHECKED_AT = "2026-08-24T01:02:03+00:00";

describe("capability diagnostics", () => {
  it.each(CAPABILITY_STATUSES)("renders all seven components with distinct %s text", (status) => {
    const rows = capabilityPresentations(capabilities(status));

    expect(rows.map(({ name }) => name)).toEqual(CAPABILITY_COMPONENTS);
    expect(new Set(rows.map(({ statusLabel }) => statusLabel)).size).toBe(1);
    expect(rows.every((row) => row.status === status && row.advice.length > 0)).toBe(true);
  });

  it("uses four distinct Chinese status labels", () => {
    const labels = CAPABILITY_STATUSES.map(
      (status) => capabilityPresentations(capabilities(status))[0]?.statusLabel,
    );

    expect(new Set(labels).size).toBe(4);
    expect(labels).toEqual(["可用", "缺失", "不可用", "未检查"]);
  });

  it("distinguishes service, package, model, and diarization recovery advice", () => {
    const advice = {
      ollamaDown: capabilityAdvice("ollama", "unavailable"),
      ollamaMissing: capabilityAdvice("ollama", "missing"),
      asrPackage: capabilityAdvice("asr_package", "missing"),
      asrModel: capabilityAdvice("asr_model", "missing"),
      diarization: capabilityAdvice("diarization", "missing"),
      primaryModel: capabilityAdvice("translation_primary", "missing"),
      fallbackUnchecked: capabilityAdvice("translation_fallback", "unchecked"),
    };

    expect(new Set(Object.values(advice)).size).toBe(Object.keys(advice).length);
    expect(advice.ollamaDown).toContain("启动 Ollama");
    expect(advice.asrPackage).toContain("Python 包");
    expect(advice.asrModel).toContain("ASR 模型");
    expect(advice.diarization).toContain("说话人模型");
    expect(advice.primaryModel).toContain("主翻译模型");
    expect(advice.fallbackUnchecked).toContain("等待 Ollama");
  });

  it("never reflects capability metadata into advice", () => {
    const secret = "CapabilitySecret123";
    const value = capabilities("missing");
    value.components.asr_model.model = `/Users/private/${secret}`;
    value.components.translation_primary.model = `env:${secret}`;

    const serialized = JSON.stringify(capabilityPresentations(value));

    expect(serialized).not.toContain(secret);
    expect(serialized).not.toContain("/Users/private");
    expect(serialized).not.toContain("env:");
  });
});

describe("job error advice", () => {
  it("provides a fixed Chinese next step for every frozen error code", () => {
    for (const errorCode of ERROR_CODES) {
      const advice = jobErrorAdvice(errorCode);
      expect(advice.length, errorCode).toBeGreaterThan(0);
      expect(advice, errorCode).not.toMatch(/(?:\/Users\/|\/home\/|\/var\/|[A-Za-z]:\\)/u);
      expect(advice, errorCode).not.toContain("traceback");
    }
  });
});

describe("settings update gate", () => {
  it("keeps the confirmed value unchanged until the server resolves", async () => {
    const gate = new SettingsUpdateGate();
    let resolveUpdate!: (value: {
      workerConcurrency: 1 | 2;
      runtimeEffect: "new_claims_only";
    }) => void;
    const confirmed = { workerConcurrency: 1 as const, runtimeEffect: "new_claims_only" as const };
    const operation = () =>
      new Promise<{
        workerConcurrency: 1 | 2;
        runtimeEffect: "new_claims_only";
      }>((resolve) => {
        resolveUpdate = resolve;
      });

    const request = gate.run(operation);
    const duplicate = gate.run(() =>
      Promise.resolve({ workerConcurrency: 2, runtimeEffect: "new_claims_only" }),
    );

    expect(gate.isBusy()).toBe(true);
    expect(confirmed.workerConcurrency).toBe(1);
    expect(duplicate).toBeUndefined();
    resolveUpdate({ workerConcurrency: 2, runtimeEffect: "new_claims_only" });
    await expect(request).resolves.toMatchObject({ workerConcurrency: 2 });
    expect(gate.isBusy()).toBe(false);
  });

  it("releases the gate without changing caller state after failure", async () => {
    const gate = new SettingsUpdateGate();
    const confirmed = { workerConcurrency: 1 as const };

    await expect(
      gate.run(() => Promise.reject(new Error("private settings failure"))),
    ).rejects.toThrow();
    expect(confirmed.workerConcurrency).toBe(1);
    expect(gate.isBusy()).toBe(false);
  });
});

describe("settings errors", () => {
  it.each([
    [
      new ApiClientError("validation", "Pydantic input=/Users/private/TokenSecret", 422),
      "并发数只能为 1 或 2",
    ],
    [
      new ApiClientError("conflict", "raw conflict TokenSecret", 409),
      "后端设置已变化，正在刷新，请重试",
    ],
    [
      new ApiClientError("server", "Traceback /Users/private/TokenSecret", 503),
      "无法应用并发设置，请检查本地 worker 后重试",
    ],
    [
      new ApiClientError("unreachable", "本地服务未启动，请先启动 Local Video Transcriber"),
      "本地服务未启动，请先启动 Local Video Transcriber",
    ],
    [new Error("private settings failure"), "运行设置更新失败，请稍后重试"],
  ])("returns a fixed safe message for settings failure %#", (error, expected) => {
    const message = settingsErrorMessage(error);

    expect(message).toBe(expected);
    expect(message).not.toContain("TokenSecret");
    expect(message).not.toContain("/Users/private");
    expect(message.toLowerCase()).not.toContain("traceback");
  });
});

function capabilities(status: CapabilityStatus): CapabilitiesResponse {
  const component = { status, checkedAt: CHECKED_AT };
  return {
    checkedAt: CHECKED_AT,
    ttlSeconds: 5,
    components: {
      ffmpeg: { ...component },
      ollama: { ...component },
      asr_package: { ...component },
      asr_model: { ...component, model: "asr-model" },
      diarization: { ...component },
      translation_primary: { ...component, model: "primary-model" },
      translation_fallback: { ...component, model: "fallback-model" },
    },
  };
}

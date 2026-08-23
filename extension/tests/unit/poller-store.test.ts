import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionSnapshot } from "../../src/api/client";
import { ApiClientError } from "../../src/api/errors";
import { ConnectionStore } from "../../src/state/store";
import { VisibilityPoller } from "../../src/state/poller";

type Deferred<T> = {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
};

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("VisibilityPoller", () => {
  it("polls immediately, every second when visible, and every five seconds when hidden", async () => {
    const load = vi.fn(() => Promise.resolve("snapshot"));
    const onData = vi.fn();
    const poller = new VisibilityPoller({ load, onData, onError: vi.fn(), visible: true });

    poller.start();
    await settle();
    expect(load).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(999);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(2);

    poller.setVisible(false);
    await vi.advanceTimersByTimeAsync(4_999);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(3);
    poller.stop();
  });

  it("uses deterministic 1, 2, 5, and 10 second failure backoff capped at ten seconds", async () => {
    const load = vi.fn(() => Promise.reject(new ApiClientError("unreachable", "本地服务未启动")));
    const poller = new VisibilityPoller({ load, onData: vi.fn(), onError: vi.fn() });

    poller.start();
    await settle();
    expect(load).toHaveBeenCalledTimes(1);

    for (const [delay, calls] of [
      [1_000, 2],
      [2_000, 3],
      [5_000, 4],
      [10_000, 5],
      [10_000, 6],
    ] as const) {
      await vi.advanceTimersByTimeAsync(delay);
      expect(load).toHaveBeenCalledTimes(calls);
    }
    poller.stop();
  });

  it("returns to the normal interval after the backend recovers", async () => {
    const load = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new ApiClientError("unreachable", "本地服务未启动"))
      .mockResolvedValue("recovered");
    const onData = vi.fn();
    const poller = new VisibilityPoller({ load, onData, onError: vi.fn() });

    poller.start();
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(onData).toHaveBeenCalledWith("recovered", 1);

    await vi.advanceTimersByTimeAsync(999);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(3);
    poller.stop();
  });

  it("never overlaps requests while a previous request is unresolved", async () => {
    const first = deferred<string>();
    const load = vi.fn(() => first.promise);
    const onData = vi.fn();
    const poller = new VisibilityPoller({ load, onData, onError: vi.fn() });

    poller.start();
    await settle();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(load).toHaveBeenCalledTimes(1);

    first.resolve("first");
    await settle();
    expect(onData).toHaveBeenCalledWith("first", 1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(load).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("aborts replaced generations and discards stale responses", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const signals: AbortSignal[] = [];
    const load = vi
      .fn<(signal: AbortSignal) => Promise<string>>()
      .mockImplementationOnce((signal) => {
        signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((signal) => {
        signals.push(signal);
        return second.promise;
      });
    const onData = vi.fn();
    const poller = new VisibilityPoller({ load, onData, onError: vi.fn() });

    expect(poller.start()).toBe(1);
    await settle();
    expect(poller.restart()).toBe(2);
    await settle();
    expect(signals[0]?.aborted).toBe(true);

    second.resolve("new");
    await settle();
    first.resolve("old");
    await settle();

    expect(onData).toHaveBeenCalledTimes(1);
    expect(onData).toHaveBeenCalledWith("new", 2);
    poller.stop();
  });

  it("aborts the active request and stops scheduling", async () => {
    const pending = deferred<string>();
    let signal: AbortSignal | undefined;
    const load = vi.fn((currentSignal: AbortSignal) => {
      signal = currentSignal;
      return pending.promise;
    });
    const poller = new VisibilityPoller({ load, onData: vi.fn(), onError: vi.fn() });

    poller.start();
    await settle();
    poller.stop();

    expect(signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(load).toHaveBeenCalledTimes(1);
  });
});

describe("ConnectionStore", () => {
  it("discards stale generations and preserves the last jobs snapshot on failure", () => {
    const store = new ConnectionStore();
    const snapshot = connectionSnapshot();

    store.beginGeneration(1);
    store.applySnapshot(1, snapshot);
    store.beginGeneration(2);
    store.applySnapshot(1, { ...snapshot, jobs: [] });
    store.applyError(2, new ApiClientError("unreachable", "本地服务未启动"));

    expect(store.getState()).toMatchObject({
      generation: 2,
      connection: { status: "unreachable" },
      jobs: snapshot.jobs,
    });
  });

  it("returns to not configured without retaining connection secrets", () => {
    const store = new ConnectionStore();
    store.beginGeneration(1);

    store.markNotConfigured(2);

    expect(store.getState()).toMatchObject({
      generation: 2,
      connection: { status: "notConfigured" },
    });
    expect(JSON.stringify(store.getState())).not.toContain("token");
  });
});

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return {
    promise,
    resolve: resolvePromise,
    reject: rejectPromise,
  };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function connectionSnapshot(): ConnectionSnapshot {
  return {
    health: { status: "healthy", version: "0.1.0" },
    settings: {
      workerConcurrency: 1,
      runtimeEffect: "new_claims_only",
    },
    capabilities: {
      checkedAt: CHECKED_AT,
      ttlSeconds: 5,
      components: {
        ffmpeg: component(),
        ollama: component(),
        asr_package: component(),
        asr_model: { ...component(), model: "asr-model" },
        diarization: component(),
        translation_primary: { ...component(), model: "primary-model" },
        translation_fallback: { ...component(), model: "fallback-model" },
      },
    },
    jobs: [
      {
        uuid: "4c50ff38-9cca-4f91-bae0-f3fe4bc18b6f",
        sanitizedDisplayUrl: "https://example.test/video",
        title: "",
        status: "queued",
        stageProgress: 0,
        overallProgress: 0,
        detectedLanguage: null,
        attempts: 0,
        errorCode: null,
        errorMessage: null,
        createdAt: CHECKED_AT,
        updatedAt: CHECKED_AT,
        startedAt: null,
        finishedAt: null,
        durationMs: null,
        options: {
          asrModel: "asr-model",
          translateTo: "zh-CN",
          diarization: true,
        },
        executionCountTotal: 0,
        retryCycle: 0,
        automaticRequeueCountInCycle: 0,
        nextAttemptAt: null,
        cancelRequestedAt: null,
      },
    ],
  };
}

const CHECKED_AT = "2026-08-23T10:00:00+00:00";

function component() {
  return {
    status: "available" as const,
    checkedAt: CHECKED_AT,
  };
}

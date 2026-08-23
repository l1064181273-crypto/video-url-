import { afterEach, describe, expect, it, vi } from "vitest";

import { cleanupE2eResources, type StoppableProcess, stopProcess } from "../support/e2e-resources";

type ExitListener = (code: number | null, signal: NodeJS.Signals | null) => void;

class FakeProcess implements StoppableProcess {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  readonly signals: NodeJS.Signals[] = [];
  private readonly listeners = new Set<ExitListener>();

  constructor(private readonly exitOnSignal?: NodeJS.Signals) {}

  kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    if (signal === this.exitOnSignal) {
      this.signalCode = signal;
      this.emitExit(null, signal);
    }
    return true;
  }

  off(_event: "exit", listener: ExitListener): this {
    this.listeners.delete(listener);
    return this;
  }

  once(_event: "exit", listener: ExitListener): this {
    this.listeners.add(listener);
    return this;
  }

  private emitExit(code: number | null, signal: NodeJS.Signals | null): void {
    const current = [...this.listeners];
    this.listeners.clear();
    for (const listener of current) {
      listener(code, signal);
    }
  }
}

afterEach(() => {
  vi.useRealTimers();
});

describe("E2E resource cleanup", () => {
  it("accepts beforeAll early failure with no resources", async () => {
    await expect(cleanupE2eResources({})).resolves.toBeUndefined();
  });

  it("does not signal an already exited or signal-terminated process", async () => {
    const exited = new FakeProcess();
    exited.exitCode = 1;
    const signaled = new FakeProcess();
    signaled.signalCode = "SIGTERM";

    await stopProcess(undefined, 1);
    await stopProcess(exited, 1);
    await stopProcess(signaled, 1);

    expect(exited.signals).toEqual([]);
    expect(signaled.signals).toEqual([]);
  });

  it("stops a running process with TERM when it exits promptly", async () => {
    const child = new FakeProcess("SIGTERM");

    await stopProcess(child, 100);

    expect(child.signals).toEqual(["SIGTERM"]);
  });

  it("escalates deterministically from TERM to KILL", async () => {
    vi.useFakeTimers();
    const child = new FakeProcess("SIGKILL");

    const stopping = stopProcess(child, 100);
    await vi.advanceTimersByTimeAsync(100);
    await stopping;

    expect(child.signals).toEqual(["SIGTERM", "SIGKILL"]);
  });

  it("removes an optional data root and supports repeated cleanup", async () => {
    const child = new FakeProcess("SIGTERM");
    const removed: string[] = [];
    const resources = { backend: child, dataRoot: "/tmp/lvt-e2e-controlled" };
    const options = {
      processTimeoutMs: 100,
      removeDirectory: (path: string) => {
        removed.push(path);
        return Promise.resolve();
      },
    };

    await cleanupE2eResources(resources, options);
    await cleanupE2eResources(resources, options);

    expect(child.signals).toEqual(["SIGTERM"]);
    expect(removed).toEqual(["/tmp/lvt-e2e-controlled", "/tmp/lvt-e2e-controlled"]);
  });
});

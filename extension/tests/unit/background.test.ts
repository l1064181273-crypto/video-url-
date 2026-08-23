import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Listener = () => void;

describe("MV3 service worker initialization", () => {
  const installedListeners: Listener[] = [];
  const startupListeners: Listener[] = [];
  const setAccessLevel = vi.fn(() => Promise.resolve());
  const setPanelBehavior = vi.fn(() => Promise.resolve());

  beforeEach(() => {
    installedListeners.length = 0;
    startupListeners.length = 0;
    setAccessLevel.mockClear();
    setPanelBehavior.mockClear();
    vi.resetModules();
    vi.stubGlobal("chrome", {
      runtime: {
        onInstalled: {
          addListener: (listener: Listener) => installedListeners.push(listener),
        },
        onStartup: {
          addListener: (listener: Listener) => startupListeners.push(listener),
        },
      },
      storage: {
        local: {
          setAccessLevel,
        },
      },
      sidePanel: {
        setPanelBehavior,
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("registers listeners synchronously and configures one action-driven panel", async () => {
    await import("../../src/background");
    await flushPromises();

    expect(installedListeners).toHaveLength(1);
    expect(startupListeners).toHaveLength(1);
    expect(setAccessLevel).toHaveBeenCalledExactlyOnceWith({
      accessLevel: "TRUSTED_CONTEXTS",
    });
    expect(setPanelBehavior).toHaveBeenCalledExactlyOnceWith({
      openPanelOnActionClick: true,
    });
  });

  it("reapplies idempotent browser configuration on install and startup", async () => {
    await import("../../src/background");
    await flushPromises();

    installedListeners[0]?.();
    startupListeners[0]?.();
    await flushPromises();

    expect(setAccessLevel).toHaveBeenCalledTimes(3);
    expect(setPanelBehavior).toHaveBeenCalledTimes(3);
    for (const call of setPanelBehavior.mock.calls) {
      expect(call).toEqual([{ openPanelOnActionClick: true }]);
    }
  });
});

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

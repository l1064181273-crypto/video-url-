import { describe, expect, it } from "vitest";

import {
  ConnectionSettingsStorage,
  DEFAULT_BACKEND_PORT,
  DownloadPreferenceStorage,
  type TrustedStorageArea,
} from "../../src/storage/settings";

class FakeStorageArea implements TrustedStorageArea {
  readonly values: Record<string, unknown> = {};
  getCalls = 0;
  setCalls = 0;

  get(key: string): Promise<Record<string, unknown>> {
    this.getCalls += 1;
    return Promise.resolve({ [key]: this.values[key] });
  }

  set(items: Record<string, unknown>): Promise<void> {
    this.setCalls += 1;
    Object.assign(this.values, items);
    return Promise.resolve();
  }
}

describe("ConnectionSettingsStorage", () => {
  it("defaults to the fixed local port without claiming a token exists", async () => {
    const storage = new ConnectionSettingsStorage(new FakeStorageArea());

    await expect(storage.getSummary()).resolves.toEqual({
      port: DEFAULT_BACKEND_PORT,
      tokenConfigured: false,
    });
    await expect(storage.getCredentials()).resolves.toBeNull();
  });

  it("persists the port and token while exposing only a token flag", async () => {
    const area = new FakeStorageArea();
    const storage = new ConnectionSettingsStorage(area);

    await storage.saveConnection(9123, "NeverRenderThisToken123");

    await expect(storage.getSummary()).resolves.toEqual({
      port: 9123,
      tokenConfigured: true,
    });
    await expect(storage.getCredentials()).resolves.toEqual({
      port: 9123,
      token: "NeverRenderThisToken123",
    });
  });

  it("updates the port without replacing an existing token", async () => {
    const area = new FakeStorageArea();
    const storage = new ConnectionSettingsStorage(area);
    await storage.saveConnection(8765, "stable-token");

    await storage.saveConnection(9000);

    await expect(storage.getCredentials()).resolves.toEqual({
      port: 9000,
      token: "stable-token",
    });
  });

  it("clears only the token and remains idempotent", async () => {
    const area = new FakeStorageArea();
    const storage = new ConnectionSettingsStorage(area);
    await storage.saveConnection(9001, "remove-me");

    await storage.clearToken();
    await storage.clearToken();

    await expect(storage.getSummary()).resolves.toEqual({
      port: 9001,
      tokenConfigured: false,
    });
    await expect(storage.getCredentials()).resolves.toBeNull();
  });

  it.each([0, 65_536, 1.5, Number.NaN])("rejects invalid port %s", async (port) => {
    const storage = new ConnectionSettingsStorage(new FakeStorageArea());

    await expect(storage.saveConnection(port, "token")).rejects.toThrow("port");
  });

  it("does not reflect malformed persisted data or secrets", async () => {
    const area = new FakeStorageArea();
    area.values.lvtConnection = {
      port: "http://evil.test/?token=LeakedToken123",
      token: { secret: "LeakedToken123" },
    };
    const storage = new ConnectionSettingsStorage(area);

    await expect(storage.getSummary()).resolves.toEqual({
      port: DEFAULT_BACKEND_PORT,
      tokenConfigured: false,
    });
    await expect(storage.getCredentials()).resolves.toBeNull();
  });
});

describe("DownloadPreferenceStorage", () => {
  it("defaults to automatic downloads and persists a request to choose each location", async () => {
    const area = new FakeStorageArea();
    const storage = new DownloadPreferenceStorage(area);

    await expect(storage.getMode()).resolves.toBe("automatic");
    await storage.saveMode("prompt");
    await expect(storage.getMode()).resolves.toBe("prompt");
  });

  it("fails closed to automatic downloads for malformed persisted values", async () => {
    const area = new FakeStorageArea();
    area.values.lvtDownloadPreference = { mode: "arbitrary-system-path" };
    const storage = new DownloadPreferenceStorage(area);

    await expect(storage.getMode()).resolves.toBe("automatic");
  });
});

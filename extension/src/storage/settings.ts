export const DEFAULT_BACKEND_PORT = 8765;
const CONNECTION_KEY = "lvtConnection";
const DOWNLOAD_PREFERENCE_KEY = "lvtDownloadPreference";

export type DownloadMode = "automatic" | "prompt";

export type ConnectionSummary = {
  port: number;
  tokenConfigured: boolean;
};

export type ConnectionCredentials = {
  port: number;
  token: string;
};

export type ConnectionValue = {
  port: number;
  token: string | null;
};

export type TrustedStorageArea = {
  get(key: string): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
};

type PersistedConnection = {
  port: number;
  token?: string;
};

export class ConnectionSettingsStorage {
  constructor(private readonly area: TrustedStorageArea = chrome.storage.local) {}

  async getSummary(): Promise<ConnectionSummary> {
    const connection = await this.getConnection();
    return {
      port: connection.port,
      tokenConfigured: connection.token !== null,
    };
  }

  async getConnection(): Promise<ConnectionValue> {
    const stored = await this.area.get(CONNECTION_KEY);
    return parsePersistedConnection(stored[CONNECTION_KEY]);
  }

  async getCredentials(): Promise<ConnectionCredentials | null> {
    const connection = await this.getConnection();
    if (connection.token === null) {
      return null;
    }
    return {
      port: connection.port,
      token: connection.token,
    };
  }

  async saveConnection(port: number, token?: string): Promise<ConnectionSummary> {
    assertValidPort(port);
    const current = await this.getConnection();
    const nextToken = token === undefined ? current.token : validateToken(token);
    const persisted: PersistedConnection = { port };
    if (nextToken !== null) {
      persisted.token = nextToken;
    }
    await this.area.set({ [CONNECTION_KEY]: persisted });
    return {
      port,
      tokenConfigured: nextToken !== null,
    };
  }

  async clearToken(): Promise<ConnectionSummary> {
    const current = await this.getConnection();
    await this.area.set({ [CONNECTION_KEY]: { port: current.port } });
    return {
      port: current.port,
      tokenConfigured: false,
    };
  }
}

export class DownloadPreferenceStorage {
  constructor(private readonly area: TrustedStorageArea = chrome.storage.local) {}

  async getMode(): Promise<DownloadMode> {
    const stored = await this.area.get(DOWNLOAD_PREFERENCE_KEY);
    return parseDownloadMode(stored[DOWNLOAD_PREFERENCE_KEY]);
  }

  async saveMode(mode: DownloadMode): Promise<void> {
    await this.area.set({ [DOWNLOAD_PREFERENCE_KEY]: { mode } });
  }
}

export function assertValidPort(port: number): void {
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new RangeError("port must be an integer from 1 through 65535");
  }
}

function parsePersistedConnection(value: unknown): ConnectionValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { port: DEFAULT_BACKEND_PORT, token: null };
  }
  const stored = value as Record<string, unknown>;
  if (!isValidPort(stored.port)) {
    return { port: DEFAULT_BACKEND_PORT, token: null };
  }
  const token = typeof stored.token === "string" && stored.token.length > 0 ? stored.token : null;
  return { port: stored.port, token };
}

function isValidPort(port: unknown): port is number {
  return typeof port === "number" && Number.isInteger(port) && port >= 1 && port <= 65_535;
}

function parseDownloadMode(value: unknown): DownloadMode {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return "automatic";
  }
  return (value as Record<string, unknown>).mode === "prompt" ? "prompt" : "automatic";
}

function validateToken(token: string): string {
  if (token.length === 0) {
    throw new TypeError("token must not be empty");
  }
  return token;
}

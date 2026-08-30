import type { CapabilitiesResponse, HealthResponse, Job, SettingsResponse } from "../api/contracts";
import type { ConnectionSnapshot } from "../api/client";
import { ApiClientError, type ApiErrorKind } from "../api/errors";

export type ConnectionStatus = "notConfigured" | "connecting" | "healthy" | ApiErrorKind;

export type ConnectionState = {
  status: ConnectionStatus;
  message: string;
};

export type AppState = {
  generation: number;
  connection: ConnectionState;
  health: HealthResponse | null;
  settings: SettingsResponse | null;
  capabilities: CapabilitiesResponse | null;
  jobs: readonly Job[];
};

type StoreListener = (state: Readonly<AppState>) => void;

const INITIAL_STATE: AppState = {
  generation: 0,
  connection: {
    status: "notConfigured",
    message: "本地服务未启动，请先双击启动文件",
  },
  health: null,
  settings: null,
  capabilities: null,
  jobs: [],
};

export class ConnectionStore {
  private state: AppState = structuredClone(INITIAL_STATE);
  private readonly listeners = new Set<StoreListener>();

  getState(): Readonly<AppState> {
    return this.state;
  }

  subscribe(listener: StoreListener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  beginGeneration(generation: number): void {
    if (generation < this.state.generation) {
      return;
    }
    this.update({
      ...this.state,
      generation,
      connection: {
        status: "connecting",
        message: "正在连接本地服务",
      },
    });
  }

  applySnapshot(generation: number, snapshot: ConnectionSnapshot): void {
    if (generation !== this.state.generation) {
      return;
    }
    this.update({
      generation,
      connection: {
        status: "healthy",
        message: "本地服务连接正常",
      },
      health: snapshot.health,
      settings: snapshot.settings,
      capabilities: snapshot.capabilities,
      jobs: snapshot.jobs,
    });
  }

  applyError(generation: number, error: unknown): void {
    if (generation !== this.state.generation) {
      return;
    }
    const normalized =
      error instanceof ApiClientError
        ? error
        : new ApiClientError("invalidResponse", "后端响应格式异常，请确认前后端版本一致");
    this.update({
      ...this.state,
      generation,
      connection: {
        status: normalized.kind,
        message: normalized.message,
      },
    });
  }

  markNotConfigured(generation: number): void {
    if (generation < this.state.generation) {
      return;
    }
    this.update({
      ...this.state,
      generation,
      connection: {
        status: "notConfigured",
        message: "本地服务未启动，请先双击启动文件",
      },
    });
  }

  private update(next: AppState): void {
    this.state = next;
    for (const listener of this.listeners) {
      listener(this.state);
    }
  }
}

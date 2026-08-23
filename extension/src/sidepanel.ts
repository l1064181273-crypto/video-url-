import { LocalApiClient, LocalApiTransport } from "./api/client";
import { ApiClientError } from "./api/errors";
import { VisibilityPoller } from "./state/poller";
import { ConnectionStore } from "./state/store";
import { ConnectionSettingsStorage, type ConnectionSummary } from "./storage/settings";
import { CONTRACT_VERSION } from "./api/contracts";

document.documentElement.dataset.contractVersion = CONTRACT_VERSION;

const status = requireElement("#connection-status", HTMLParagraphElement);
const detail = requireElement("#connection-detail", HTMLParagraphElement);
const tokenState = requireElement("#token-state", HTMLSpanElement);
const form = requireElement("#connection-form", HTMLFormElement);
const portInput = requireElement("#connection-port", HTMLInputElement);
const tokenInput = requireElement("#connection-token", HTMLInputElement);
const saveButton = requireElement("#save-connection", HTMLButtonElement);
const reconnectButton = requireElement("#reconnect", HTMLButtonElement);
const clearButton = requireElement("#clear-token", HTMLButtonElement);
let tokenConfigured = false;

const connectionStorage = new ConnectionSettingsStorage();
const apiClient = new LocalApiClient(new LocalApiTransport(connectionStorage));
const store = new ConnectionStore();
const poller = new VisibilityPoller({
  load: (signal) => apiClient.loadConnectionSnapshot(signal),
  onData: (snapshot, generation) => store.applySnapshot(generation, snapshot),
  onError: (error, generation) => store.applyError(generation, error),
  visible: document.visibilityState === "visible",
});

const unsubscribe = store.subscribe((state) => {
  status.textContent = state.connection.message;
  status.dataset.status = state.connection.status;
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveAndConnect();
});

reconnectButton.addEventListener("click", () => {
  void reconnect();
});

clearButton.addEventListener("click", () => {
  void clearToken();
});

document.addEventListener("visibilitychange", () => {
  poller.setVisible(document.visibilityState === "visible");
});

window.addEventListener("pagehide", () => {
  poller.stop();
  unsubscribe();
});

void initialize();

async function initialize(): Promise<void> {
  try {
    const summary = await connectionStorage.getSummary();
    renderSummary(summary);
    if (summary.tokenConfigured) {
      startPolling();
    } else {
      store.markNotConfigured(1);
    }
  } catch {
    renderLocalFailure();
  }
}

async function saveAndConnect(): Promise<void> {
  setBusy(true);
  const candidateToken = tokenInput.value;
  try {
    const port = parsePort(portInput.value);
    const current = await connectionStorage.getSummary();
    if (candidateToken.length === 0 && !current.tokenConfigured) {
      detail.textContent = "请输入配对 Token";
      return;
    }
    const summary = await connectionStorage.saveConnection(
      port,
      candidateToken.length > 0 ? candidateToken : undefined,
    );
    renderSummary(summary);
    startPolling();
  } catch (error) {
    detail.textContent =
      error instanceof RangeError ? "端口必须是 1 到 65535 的整数" : "无法保存本地连接设置，请重试";
  } finally {
    tokenInput.value = "";
    setBusy(false);
  }
}

async function reconnect(): Promise<void> {
  setBusy(true);
  try {
    const summary = await connectionStorage.getSummary();
    renderSummary(summary);
    if (!summary.tokenConfigured) {
      store.markNotConfigured(poller.stop());
      return;
    }
    startPolling();
  } catch {
    renderLocalFailure();
  } finally {
    setBusy(false);
  }
}

async function clearToken(): Promise<void> {
  setBusy(true);
  try {
    const summary = await connectionStorage.clearToken();
    tokenInput.value = "";
    renderSummary(summary);
    store.markNotConfigured(poller.stop());
  } catch {
    renderLocalFailure();
  } finally {
    setBusy(false);
  }
}

function startPolling(): void {
  detail.textContent = "";
  const generation = poller.restart();
  store.beginGeneration(generation);
}

function renderSummary(summary: ConnectionSummary): void {
  tokenConfigured = summary.tokenConfigured;
  portInput.value = String(summary.port);
  tokenState.textContent = summary.tokenConfigured ? "Token 已保存" : "未保存 Token";
  reconnectButton.disabled = !summary.tokenConfigured;
  clearButton.disabled = !summary.tokenConfigured;
}

function setBusy(busy: boolean): void {
  saveButton.disabled = busy;
  reconnectButton.disabled = busy || !tokenConfigured;
  clearButton.disabled = busy || !tokenConfigured;
  portInput.disabled = busy;
  tokenInput.disabled = busy;
}

function renderLocalFailure(): void {
  const error = new ApiClientError("invalidResponse", "无法读取本地扩展设置，请重新打开面板");
  status.textContent = error.message;
  status.dataset.status = error.kind;
}

function parsePort(value: string): number {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new RangeError("invalid port");
  }
  return port;
}

function requireElement<T extends Element>(selector: string, constructor: new () => T): T {
  const element = document.querySelector(selector);
  if (!(element instanceof constructor)) {
    throw new Error(`Missing required element: ${selector}`);
  }
  return element;
}

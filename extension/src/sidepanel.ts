import { LocalApiClient, LocalApiTransport } from "./api/client";
import type { Job, JobOptions } from "./api/contracts";
import { ApiClientError } from "./api/errors";
import { VisibilityPoller } from "./state/poller";
import { ConnectionStore, type ConnectionStatus } from "./state/store";
import { ConnectionSettingsStorage, type ConnectionSummary } from "./storage/settings";
import { CONTRACT_VERSION } from "./api/contracts";
import {
  filterJobs,
  formatDuration,
  type JobFilter,
  jobDisplayTitle,
  jobStatusLabel,
  parseBatchInput,
  retainedInputAfterSubmission,
  submittedUrls,
} from "./ui/jobs";

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
const jobForm = requireElement("#job-form", HTMLFormElement);
const urlsInput = requireElement("#job-urls", HTMLTextAreaElement);
const urlCount = requireElement("#url-count", HTMLSpanElement);
const urlLimit = requireElement("#url-limit", HTMLParagraphElement);
const invalidUrls = requireElement("#invalid-urls", HTMLOListElement);
const asrModelInput = requireElement("#asr-model", HTMLInputElement);
const diarizationInput = requireElement("#diarization", HTMLInputElement);
const submitButton = requireElement("#submit-jobs", HTMLButtonElement);
const submissionResult = requireElement("#submission-result", HTMLParagraphElement);
const serverRejections = requireElement("#server-rejections", HTMLOListElement);
const filterTabs = requireElement("#job-filters", HTMLDivElement);
const jobCount = requireElement("#job-count", HTMLSpanElement);
const jobsEmpty = requireElement("#jobs-empty", HTMLParagraphElement);
const jobList = requireElement("#job-list", HTMLDivElement);
let tokenConfigured = false;
let connected = false;
let submissionBusy = false;
let currentFilter: JobFilter = "all";
let currentBatch = parseBatchInput("");
const jobRows = new Map<string, HTMLElement>();

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
  connected = state.connection.status === "healthy";
  renderJobs(state.jobs, state.connection.status);
  updateSubmitState();
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

urlsInput.addEventListener("input", () => {
  submissionResult.textContent = "";
  serverRejections.replaceChildren();
  renderBatchInput();
});

asrModelInput.addEventListener("input", updateSubmitState);

jobForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void submitJobs();
});

filterTabs.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) {
    return;
  }
  const filter = target.dataset.filter;
  if (!isJobFilter(filter)) {
    return;
  }
  currentFilter = filter;
  for (const button of filterTabs.querySelectorAll<HTMLButtonElement>("[data-filter]")) {
    button.setAttribute("aria-selected", String(button === target));
  }
  const state = store.getState();
  renderJobs(state.jobs, state.connection.status);
});

document.addEventListener("visibilitychange", () => {
  poller.setVisible(document.visibilityState === "visible");
});

window.addEventListener("pagehide", () => {
  poller.stop();
  unsubscribe();
});

void initialize();
renderBatchInput();

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

async function submitJobs(): Promise<void> {
  renderBatchInput();
  const urls = submittedUrls(currentBatch);
  if (urls.length === 0 || submissionBusy || !connected) {
    return;
  }
  const asrModel = asrModelInput.value.trim();
  if (asrModel.length === 0) {
    submissionResult.textContent = "请输入 ASR 模型";
    return;
  }
  const options: JobOptions = {
    asrModel,
    translateTo: "zh-CN",
    diarization: diarizationInput.checked,
  };
  setSubmissionBusy(true);
  serverRejections.replaceChildren();
  try {
    const result = await apiClient.createJobs(urls, options);
    urlsInput.value = retainedInputAfterSubmission(
      currentBatch,
      result.rejected.map(({ url }) => url),
    );
    submissionResult.textContent = `已创建 ${String(result.accepted.length)} 个，后端拒绝 ${String(result.rejected.length)} 个`;
    renderServerRejections(result.rejected);
    renderBatchInput();
    startPolling();
  } catch (error) {
    submissionResult.textContent =
      error instanceof ApiClientError ? error.message : "任务提交失败，请稍后重试";
  } finally {
    setSubmissionBusy(false);
  }
}

function renderBatchInput(): void {
  currentBatch = parseBatchInput(urlsInput.value);
  urlCount.textContent = `有效 ${String(currentBatch.validCount)} / 无效 ${String(currentBatch.invalidCount)}`;
  urlLimit.textContent = currentBatch.overLimit ? "一次最多提交 100 条 URL" : "";
  const items = currentBatch.lines
    .filter((line) => !line.valid)
    .map((line) => {
      const item = document.createElement("li");
      item.textContent = `第 ${String(line.lineNumber)} 行：${line.reason ?? "URL 无效"}`;
      return item;
    });
  invalidUrls.replaceChildren(...items);
  updateSubmitState();
}

function renderServerRejections(rejected: readonly { message: string; url: string }[]): void {
  serverRejections.replaceChildren(
    ...rejected.map(({ message, url }) => {
      const item = document.createElement("li");
      item.textContent = `${url}：${message}`;
      return item;
    }),
  );
}

function setSubmissionBusy(busy: boolean): void {
  submissionBusy = busy;
  urlsInput.disabled = busy;
  asrModelInput.disabled = busy;
  diarizationInput.disabled = busy;
  submitButton.textContent = busy ? "提交中..." : "提交任务";
  updateSubmitState();
}

function updateSubmitState(): void {
  submitButton.disabled =
    submissionBusy ||
    !connected ||
    currentBatch.overLimit ||
    currentBatch.validCount === 0 ||
    asrModelInput.value.trim().length === 0;
}

function renderJobs(jobs: readonly Job[], connectionStatus: ConnectionStatus): void {
  const knownIds = new Set(jobs.map(({ uuid }) => uuid));
  for (const [uuid, row] of jobRows) {
    if (!knownIds.has(uuid)) {
      row.remove();
      jobRows.delete(uuid);
    }
  }

  const visibleJobs = filterJobs(jobs, currentFilter);
  const visibleIds = new Set(visibleJobs.map(({ uuid }) => uuid));
  for (const job of jobs) {
    const row = jobRows.get(job.uuid) ?? createJobRow(job.uuid);
    updateJobRow(row, job);
    row.hidden = !visibleIds.has(job.uuid);
    jobList.append(row);
  }

  jobCount.textContent = `${String(jobs.length)} 个`;
  jobsEmpty.hidden = visibleJobs.length > 0;
  jobList.hidden = visibleJobs.length === 0;
  if (visibleJobs.length === 0) {
    jobsEmpty.textContent = emptyJobsMessage(jobs, connectionStatus);
  }
}

function createJobRow(uuid: string): HTMLElement {
  const row = document.createElement("article");
  row.className = "job-row";
  row.dataset.jobId = uuid;

  const header = document.createElement("div");
  header.className = "job-row-header";
  header.append(
    fieldElement("h3", "job-title", "title"),
    fieldElement("span", "job-status", "status"),
  );
  row.append(header);
  row.append(fieldElement("p", "job-url", "url"));

  const waiting = fieldElement("p", "job-waiting", "waiting");
  waiting.textContent = "等待调度";
  row.append(waiting);

  const progress = document.createElement("div");
  progress.className = "job-progress";
  progress.dataset.field = "progress";
  progress.append(
    progressBlock("总进度", "overall-progress", "overall-value"),
    progressBlock("阶段进度", "stage-progress", "stage-value"),
  );
  row.append(progress);

  const meta = document.createElement("div");
  meta.className = "job-meta";
  meta.append(
    fieldElement("span", "", "language"),
    fieldElement("span", "", "duration"),
    fieldElement("span", "", "started"),
    fieldElement("span", "", "finished"),
  );
  row.append(meta);
  jobRows.set(uuid, row);
  return row;
}

function progressBlock(label: string, progressField: string, valueField: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const heading = document.createElement("div");
  heading.className = "job-progress-heading";
  const labelElement = document.createElement("span");
  labelElement.textContent = label;
  heading.append(labelElement, fieldElement("span", "", valueField));
  const progress = document.createElement("progress");
  progress.max = 100;
  progress.dataset.field = progressField;
  fragment.append(heading, progress);
  return fragment;
}

function updateJobRow(row: HTMLElement, job: Job): void {
  row.dataset.status = job.status;
  row.dataset.overallProgress = String(job.overallProgress);
  row.dataset.stageProgress = String(job.stageProgress);
  const displayTitle = jobDisplayTitle(job);
  const title = jobField(row, "title");
  title.textContent = displayTitle;
  title.title = displayTitle;
  const statusElement = jobField(row, "status");
  statusElement.textContent = jobStatusLabel(job.status);
  statusElement.dataset.status = job.status;
  jobField(row, "url").textContent = job.sanitizedDisplayUrl;

  const waiting = jobField(row, "waiting");
  const progress = jobField(row, "progress");
  const queued = job.status === "queued";
  waiting.hidden = !queued;
  progress.hidden = queued;
  const overall = jobProgress(row, "overall-progress");
  const stage = jobProgress(row, "stage-progress");
  overall.value = job.overallProgress;
  stage.value = job.stageProgress;
  jobField(row, "overall-value").textContent = `${String(job.overallProgress)}%`;
  jobField(row, "stage-value").textContent = `${String(job.stageProgress)}%`;

  jobField(row, "language").textContent = `语言 ${job.detectedLanguage ?? "--"}`;
  jobField(row, "duration").textContent = `时长 ${formatDuration(job.durationMs)}`;
  jobField(row, "started").textContent = `开始 ${formatTimestamp(job.startedAt)}`;
  jobField(row, "finished").textContent = `完成 ${formatTimestamp(job.finishedAt)}`;
}

function fieldElement<K extends keyof HTMLElementTagNameMap>(
  tagName: K,
  className: string,
  field: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tagName);
  element.className = className;
  element.dataset.field = field;
  return element;
}

function jobField(row: HTMLElement, field: string): HTMLElement {
  const element = row.querySelector<HTMLElement>(`[data-field="${field}"]`);
  if (element === null) {
    throw new Error(`Missing job row field: ${field}`);
  }
  return element;
}

function jobProgress(row: HTMLElement, field: string): HTMLProgressElement {
  const element = row.querySelector<HTMLProgressElement>(`progress[data-field="${field}"]`);
  if (element === null) {
    throw new Error(`Missing job progress field: ${field}`);
  }
  return element;
}

function emptyJobsMessage(jobs: readonly Job[], connectionStatus: ConnectionStatus): string {
  if (jobs.length > 0) {
    return "当前筛选没有任务";
  }
  if (
    connectionStatus === "unreachable" ||
    connectionStatus === "server" ||
    connectionStatus === "backendUnhealthy"
  ) {
    return "后端不可达，保留上次任务数据";
  }
  if (connectionStatus === "notConfigured") {
    return "连接后显示任务";
  }
  return "尚未提交任务";
}

function formatTimestamp(value: string | null): string {
  if (value === null) {
    return "--";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function isJobFilter(value: string | undefined): value is JobFilter {
  return value === "all" || value === "processing" || value === "completed" || value === "failed";
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

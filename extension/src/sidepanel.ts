import { LocalApiClient, LocalApiTransport } from "./api/client";
import type { Job, JobEvent, JobOptions } from "./api/contracts";
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
import {
  describeActionFailure,
  type JobAction,
  JobActionGate,
  jobActionAvailability,
} from "./ui/job-actions";
import { eventMessageSummary, eventStatusLabel, mergeEventPages } from "./ui/events";

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
const submissionSection = requireElement(".submission-section", HTMLElement);
const jobsSection = requireElement(".jobs-section", HTMLElement);
const jobDetail = requireElement("#job-detail", HTMLElement);
const detailBack = requireElement("#detail-back", HTMLButtonElement);
const detailJobTitle = requireElement("#detail-job-title", HTMLHeadingElement);
const detailJobUrl = requireElement("#detail-job-url", HTMLParagraphElement);
const detailStatus = requireElement("#detail-status", HTMLElement);
const detailOverallProgress = requireElement("#detail-overall-progress", HTMLElement);
const detailStageProgress = requireElement("#detail-stage-progress", HTMLElement);
const detailLanguage = requireElement("#detail-language", HTMLElement);
const detailDuration = requireElement("#detail-duration", HTMLElement);
const detailExecutionCount = requireElement("#detail-execution-count", HTMLElement);
const detailRetryCycle = requireElement("#detail-retry-cycle", HTMLElement);
const detailAsrModel = requireElement("#detail-asr-model", HTMLElement);
const detailTranslateTo = requireElement("#detail-translate-to", HTMLElement);
const detailDiarization = requireElement("#detail-diarization", HTMLElement);
const detailActions = requireElement("#detail-actions", HTMLDivElement);
const detailActionMessage = requireElement("#detail-action-message", HTMLParagraphElement);
const eventCount = requireElement("#event-count", HTMLSpanElement);
const timelineMessage = requireElement("#timeline-message", HTMLParagraphElement);
const eventList = requireElement("#event-list", HTMLOListElement);
const loadMoreEvents = requireElement("#load-more-events", HTMLButtonElement);
const deleteDialog = requireElement("#delete-dialog", HTMLDialogElement);
const deleteJobTitle = requireElement("#delete-job-title", HTMLParagraphElement);
const deleteCancel = requireElement("#delete-cancel", HTMLButtonElement);
const deleteConfirm = requireElement("#delete-confirm", HTMLButtonElement);
const deleteError = requireElement("#delete-error", HTMLParagraphElement);
let tokenConfigured = false;
let connected = false;
let submissionBusy = false;
let currentFilter: JobFilter = "all";
let currentBatch = parseBatchInput("");
let selectedJobId: string | null = null;
let timelineEvents: JobEvent[] = [];
let timelineTotal = 0;
let timelineNextOffset = 0;
let timelineLoading = false;
let eventRequestGeneration = 0;
let eventAbort: AbortController | undefined;
let deleteTargetJobId: string | null = null;
let deleteReturnFocus: HTMLButtonElement | null = null;
const jobRows = new Map<string, HTMLElement>();
const actionGate = new JobActionGate();

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
  if (selectedJobId !== null) {
    const selected = state.jobs.find((job) => job.uuid === selectedJobId);
    if (selected === undefined) {
      closeJobDetail();
    } else {
      renderJobDetail(selected);
    }
  }
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

jobList.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  const button = target.closest<HTMLButtonElement>("button[data-target-job-id][data-action]");
  if (button === null) {
    return;
  }
  const jobId = button.dataset.targetJobId;
  const action = button.dataset.action;
  if (jobId === undefined) {
    return;
  }
  if (action === "details") {
    openJobDetail(jobId);
    return;
  }
  if (!isJobAction(action)) {
    return;
  }
  if (action === "delete") {
    openDeleteDialog(jobId, button);
    return;
  }
  void runJobAction(action, jobId);
});

detailActions.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement) || selectedJobId === null) {
    return;
  }
  const action = target.dataset.action;
  if (!isJobAction(action)) {
    return;
  }
  if (action === "delete") {
    openDeleteDialog(selectedJobId, target);
    return;
  }
  void runJobAction(action, selectedJobId);
});

detailBack.addEventListener("click", closeJobDetail);

loadMoreEvents.addEventListener("click", () => {
  void loadEvents(false);
});

deleteCancel.addEventListener("click", () => {
  deleteDialog.close("cancel");
});

deleteConfirm.addEventListener("click", () => {
  if (deleteTargetJobId !== null) {
    void runJobAction("delete", deleteTargetJobId);
  }
});

deleteDialog.addEventListener("cancel", (event) => {
  if (deleteTargetJobId !== null && actionGate.isBusy(deleteTargetJobId)) {
    event.preventDefault();
  }
});

deleteDialog.addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || deleteCancel.disabled || deleteConfirm.disabled) {
    return;
  }
  if (event.shiftKey && document.activeElement === deleteCancel) {
    event.preventDefault();
    deleteConfirm.focus();
    return;
  }
  if (!event.shiftKey && document.activeElement === deleteConfirm) {
    event.preventDefault();
    deleteCancel.focus();
    return;
  }
  if (!deleteDialog.contains(document.activeElement)) {
    event.preventDefault();
    deleteCancel.focus();
  }
});

deleteDialog.addEventListener("close", () => {
  deleteError.textContent = "";
  deleteConfirm.disabled = false;
  deleteCancel.disabled = false;
  deleteTargetJobId = null;
  if (deleteReturnFocus?.isConnected === true) {
    deleteReturnFocus.focus();
  }
  deleteReturnFocus = null;
});

document.addEventListener("visibilitychange", () => {
  poller.setVisible(document.visibilityState === "visible");
});

window.addEventListener("pagehide", () => {
  poller.stop();
  eventAbort?.abort();
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
  const actions = document.createElement("div");
  actions.className = "job-actions";
  actions.dataset.field = "actions";
  actions.append(
    createActionButton("details", uuid, "查看详情", "secondary"),
    createActionButton("cancel", uuid, "取消任务", "secondary"),
    createActionButton("retry", uuid, "重试", "secondary"),
    createActionButton("delete", uuid, "删除", "danger"),
  );
  row.append(actions, fieldElement("p", "action-message", "action-message"));
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
  updateActionButtons(jobField(row, "actions"), job);
}

function createActionButton(
  action: JobAction | "details",
  jobId: string,
  label: string,
  className: string,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.dataset.action = action;
  button.dataset.targetJobId = jobId;
  button.textContent = label;
  return button;
}

function updateActionButtons(container: HTMLElement, job: Job): void {
  const availability = jobActionAvailability(job.status);
  const busy = actionGate.isBusy(job.uuid);
  const title = jobDisplayTitle(job);
  for (const button of container.querySelectorAll<HTMLButtonElement>("button[data-action]")) {
    const action = button.dataset.action;
    if (action === "details") {
      button.hidden = false;
      button.disabled = busy;
      button.ariaLabel = `查看详情 ${title}`;
      continue;
    }
    if (!isJobAction(action)) {
      continue;
    }
    button.hidden = !availability[action];
    button.disabled = busy;
    button.ariaLabel = `${actionLabel(action)} ${title}`;
  }
}

function openJobDetail(jobId: string): void {
  const job = store.getState().jobs.find((candidate) => candidate.uuid === jobId);
  if (job === undefined) {
    return;
  }
  selectedJobId = jobId;
  submissionSection.hidden = true;
  jobsSection.hidden = true;
  jobDetail.hidden = false;
  detailActionMessage.textContent = "";
  renderJobDetail(job);
  detailBack.focus();
  void loadEvents(true);
}

function closeJobDetail(): void {
  eventAbort?.abort();
  eventAbort = undefined;
  eventRequestGeneration += 1;
  selectedJobId = null;
  timelineEvents = [];
  timelineTotal = 0;
  timelineNextOffset = 0;
  timelineLoading = false;
  eventList.replaceChildren();
  timelineMessage.textContent = "";
  jobDetail.hidden = true;
  submissionSection.hidden = false;
  jobsSection.hidden = false;
}

function renderJobDetail(job: Job): void {
  const title = jobDisplayTitle(job);
  detailJobTitle.textContent = title;
  detailJobTitle.title = title;
  detailJobUrl.textContent = job.sanitizedDisplayUrl;
  detailStatus.textContent = jobStatusLabel(job.status);
  detailOverallProgress.textContent = `${String(job.overallProgress)}%`;
  detailStageProgress.textContent = `${String(job.stageProgress)}%`;
  detailLanguage.textContent = job.detectedLanguage ?? "--";
  detailDuration.textContent = formatDuration(job.durationMs);
  detailExecutionCount.textContent = String(job.executionCountTotal);
  detailRetryCycle.textContent = String(job.retryCycle);
  detailAsrModel.textContent = job.options.asrModel;
  detailTranslateTo.textContent = job.options.translateTo;
  detailDiarization.textContent = job.options.diarization ? "已启用" : "未启用";

  const availability = jobActionAvailability(job.status);
  const buttons = (["cancel", "retry", "delete"] as const)
    .filter((action) => availability[action])
    .map((action) =>
      createActionButton(action, job.uuid, actionLabel(action), actionClass(action)),
    );
  detailActions.replaceChildren(...buttons);
  updateActionButtons(detailActions, job);
}

async function runJobAction(action: JobAction, jobId: string): Promise<void> {
  const job = store.getState().jobs.find((candidate) => candidate.uuid === jobId);
  if (job === undefined || !jobActionAvailability(job.status)[action]) {
    return;
  }
  const request = actionGate.run<Job | null>(jobId, () => {
    if (action === "cancel") {
      return apiClient.cancelJob(jobId);
    }
    if (action === "retry") {
      return apiClient.retryJob(jobId);
    }
    return apiClient.deleteJob(jobId).then(() => null);
  });
  if (request === undefined) {
    return;
  }

  setJobActionBusy(jobId, true);
  setActionMessage(jobId, "");
  try {
    await request;
    if (action === "delete") {
      if (deleteDialog.open) {
        deleteDialog.close("deleted");
      }
      if (selectedJobId === jobId) {
        closeJobDetail();
      }
    } else if (selectedJobId === jobId) {
      void loadEvents(true);
    }
    setActionMessage(jobId, "操作成功，正在刷新");
    startPolling();
  } catch (error) {
    const failure = describeActionFailure(error);
    setActionMessage(jobId, failure.message);
    if (action === "delete" && deleteDialog.open) {
      deleteError.textContent = failure.message;
    }
    if (failure.refresh) {
      startPolling();
    }
  } finally {
    setJobActionBusy(jobId, false);
  }
}

function setJobActionBusy(jobId: string, busy: boolean): void {
  const job = store.getState().jobs.find((candidate) => candidate.uuid === jobId);
  const row = jobRows.get(jobId);
  if (job !== undefined && row !== undefined) {
    updateActionButtons(jobField(row, "actions"), job);
  }
  if (job !== undefined && selectedJobId === jobId) {
    updateActionButtons(detailActions, job);
  }
  if (deleteTargetJobId === jobId && deleteDialog.open) {
    deleteConfirm.disabled = busy;
    deleteCancel.disabled = busy;
  }
}

function setActionMessage(jobId: string, message: string): void {
  const row = jobRows.get(jobId);
  if (row !== undefined) {
    jobField(row, "action-message").textContent = message;
  }
  if (selectedJobId === jobId) {
    detailActionMessage.textContent = message;
  }
}

function openDeleteDialog(jobId: string, trigger: HTMLButtonElement): void {
  const job = store.getState().jobs.find((candidate) => candidate.uuid === jobId);
  if (
    job === undefined ||
    !jobActionAvailability(job.status).delete ||
    actionGate.isBusy(jobId) ||
    deleteDialog.open
  ) {
    return;
  }
  deleteTargetJobId = jobId;
  deleteReturnFocus = trigger;
  deleteJobTitle.textContent = jobDisplayTitle(job);
  deleteError.textContent = "";
  deleteDialog.showModal();
  deleteCancel.focus();
}

async function loadEvents(reset: boolean): Promise<void> {
  const jobId = selectedJobId;
  if (jobId === null) {
    return;
  }
  if (reset) {
    eventAbort?.abort();
    timelineLoading = false;
    timelineEvents = [];
    timelineTotal = 0;
    timelineNextOffset = 0;
    eventRequestGeneration += 1;
  } else if (timelineLoading) {
    return;
  }
  const generation = eventRequestGeneration;
  const controller = new AbortController();
  eventAbort = controller;
  timelineLoading = true;
  timelineMessage.textContent = "正在加载事件";
  renderTimeline();
  try {
    const page = await apiClient.getJobEvents(
      jobId,
      reset ? 0 : timelineNextOffset,
      50,
      controller.signal,
    );
    if (
      controller.signal.aborted ||
      selectedJobId !== jobId ||
      generation !== eventRequestGeneration
    ) {
      return;
    }
    timelineEvents = mergeEventPages(reset ? [] : timelineEvents, page.items);
    timelineTotal = page.total;
    timelineNextOffset = Math.max(timelineNextOffset, page.offset + page.items.length);
    timelineMessage.textContent = timelineEvents.length === 0 ? "暂无事件" : "";
  } catch (error) {
    if (!controller.signal.aborted && selectedJobId === jobId) {
      timelineMessage.textContent =
        error instanceof ApiClientError ? error.message : "事件加载失败，请稍后重试";
    }
  } finally {
    if (selectedJobId === jobId && generation === eventRequestGeneration) {
      timelineLoading = false;
      eventAbort = undefined;
      renderTimeline();
    }
  }
}

function renderTimeline(): void {
  eventCount.textContent = `${String(timelineEvents.length)} / ${String(timelineTotal)} 条`;
  eventList.replaceChildren(
    ...timelineEvents.map((event) => {
      const item = document.createElement("li");
      item.className = "event-item";
      item.dataset.eventId = String(event.id);
      const heading = document.createElement("div");
      heading.className = "event-heading";
      const eventStatus = document.createElement("span");
      eventStatus.className = "event-status";
      eventStatus.textContent = eventStatusLabel(event.status);
      const time = document.createElement("time");
      time.className = "event-time";
      time.dateTime = event.createdAt;
      time.textContent = formatTimestamp(event.createdAt);
      heading.append(eventStatus, time);
      item.append(heading);
      const summary = eventMessageSummary(event.message);
      if (summary.length > 0) {
        const message = document.createElement("p");
        message.className = "event-summary";
        message.textContent = summary;
        item.append(message);
      }
      return item;
    }),
  );
  loadMoreEvents.hidden = timelineLoading || timelineNextOffset >= timelineTotal;
  loadMoreEvents.disabled = timelineLoading;
}

function actionLabel(action: JobAction): string {
  if (action === "cancel") {
    return "取消任务";
  }
  if (action === "retry") {
    return "重试";
  }
  return "删除";
}

function actionClass(action: JobAction): string {
  return action === "delete" ? "danger" : "secondary";
}

function isJobAction(value: string | undefined): value is JobAction {
  return value === "cancel" || value === "retry" || value === "delete";
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

import { execFile, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { promisify } from "node:util";

import { chromium, expect, test, type BrowserContext, type Worker } from "@playwright/test";

import {
  parseApiErrorResponse,
  parseCapabilitiesResponse,
  parseCreateJobsResponse,
  parseHealthResponse,
  parseJobsResponse,
  parseSettingsResponse,
} from "../../src/api/contracts";
import { cleanupE2eResources } from "../support/e2e-resources";

const PROJECT_ROOT = resolve(import.meta.dirname, "../../..");
const BACKEND_ROOT = resolve(PROJECT_ROOT, "backend");
const PYTHON = resolve(PROJECT_ROOT, ".venv-smoke/bin/python");
const EXTENSION_PATH = resolve(PROJECT_ROOT, "extension/dist");
const TOKEN = "CheckpointOneRuntimeToken";
const execFileAsync = promisify(execFile);

let backend: ChildProcessWithoutNullStreams | undefined;
let dataRoot: string | undefined;
let baseUrl: string;

test.beforeAll(async () => {
  const port = await reservePort();
  dataRoot = await mkdtemp(resolve(tmpdir(), "lvt-extension-e2e-"));
  baseUrl = `http://127.0.0.1:${String(port)}`;
  backend = spawn(
    PYTHON,
    ["-m", "uvicorn", "lvt.main:app", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: BACKEND_ROOT,
      env: {
        ...process.env,
        LVT_DATA_ROOT: dataRoot,
        LVT_TOKEN: TOKEN,
        PYTHONUNBUFFERED: "1",
      },
      stdio: "pipe",
    },
  );
  await waitForOutput(backend, "Application startup complete.");
});

test.afterAll(async () => {
  await cleanupE2eResources({ backend, dataRoot });
});

test("unpacked extension reads frozen local API contracts without leaking its token", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-chromium-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
    });
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    const requests: { headers: Record<string, string>; url: string }[] = [];
    const consoleMessages: string[] = [];
    context.on("request", (request) => {
      if (request.url().startsWith(baseUrl)) {
        requests.push({ headers: request.headers(), url: request.url() });
      }
    });

    const page = await context.newPage();
    page.on("console", (message) => consoleMessages.push(message.text()));
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(page.getByRole("heading", { name: "Local Video Transcriber" })).toBeVisible();
    await expect(page.locator("#connection-status")).toHaveText("请先设置本地端口和配对 Token");

    const panelBehavior = await worker.evaluate(async () => chrome.sidePanel.getPanelBehavior());
    const panelOptions = await worker.evaluate(async () => chrome.sidePanel.getOptions({}));
    expect(panelBehavior).toEqual({ openPanelOnActionClick: true });
    expect(panelOptions.path).toBe("sidepanel.html");
    expect(panelOptions.enabled).not.toBe(false);

    await worker.evaluate(
      async (token) => chrome.storage.local.set({ checkpointOneToken: token }),
      TOKEN,
    );
    const responses = await page.evaluate(async (origin) => {
      const stored = await chrome.storage.local.get("checkpointOneToken");
      const token = stored.checkpointOneToken;
      if (typeof token !== "string") {
        throw new Error("test token was not available in trusted extension storage");
      }
      const request = async (path: string, authenticated: boolean) => {
        const response = await fetch(`${origin}${path}`, {
          headers: authenticated ? { "X-LVT-Token": token } : {},
          redirect: "error",
        });
        const body: unknown = await response.json();
        return {
          body,
          status: response.status,
          url: response.url,
        };
      };
      const unauthorizedResponse = await fetch(`${origin}/api/v1/jobs`, {
        headers: { "X-LVT-Token": "wrong-token" },
        redirect: "error",
      });
      const unauthorizedBody: unknown = await unauthorizedResponse.json();
      return {
        health: await request("/health", false),
        settings: await request("/api/v1/settings", true),
        jobs: await request("/api/v1/jobs", true),
        capabilities: await request("/api/v1/capabilities", true),
        unauthorized: {
          body: unauthorizedBody,
          status: unauthorizedResponse.status,
          url: unauthorizedResponse.url,
        },
      };
    }, baseUrl);
    await worker.evaluate(async () => chrome.storage.local.remove("checkpointOneToken"));

    expect(responses.health.status).toBe(200);
    expect(responses.settings.status).toBe(200);
    expect(responses.jobs.status).toBe(200);
    expect(responses.capabilities.status).toBe(200);
    expect(responses.unauthorized.status).toBe(401);
    expect(parseHealthResponse(responses.health.body).status).toBe("healthy");
    expect(parseSettingsResponse(responses.settings.body).workerConcurrency).toBe(1);
    expect(parseJobsResponse(responses.jobs.body)).toEqual([]);
    const capabilities = parseCapabilitiesResponse(responses.capabilities.body);
    expect(Object.keys(capabilities.components)).toHaveLength(7);
    expect(parseApiErrorResponse(responses.unauthorized.body).errorCode).toBe("UNAUTHORIZED");

    expect(requests).toHaveLength(5);
    expect(requests.filter((request) => request.headers["x-lvt-token"] === TOKEN)).toHaveLength(3);
    expect(requests.every((request) => !request.url.includes(TOKEN))).toBe(true);
    expect(Object.values(responses).every((response) => !response.url.includes(TOKEN))).toBe(true);
    expect(await page.locator("body").innerText()).not.toContain(TOKEN);
    expect(consoleMessages.join("\n")).not.toContain(TOKEN);
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

test("side panel restores connection summary and clears its trusted token", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-chromium-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
    });
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    await worker.evaluate(
      async ({ port, token }) =>
        chrome.storage.local.set({
          lvtConnection: { port, token },
        }),
      { port: Number(new URL(baseUrl).port), token: TOKEN },
    );
    const authenticatedPaths = new Set<string>();
    context.on("request", (request) => {
      if (request.url().startsWith(baseUrl) && request.headers()["x-lvt-token"] === TOKEN) {
        authenticatedPaths.add(new URL(request.url()).pathname);
      }
    });

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);

    await expect(page.locator("#connection-port")).toHaveValue(new URL(baseUrl).port);
    await expect(page.locator("#connection-token")).toHaveValue("");
    await expect(page.locator("#token-state")).toHaveText("Token 已保存");
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");
    await expect
      .poll(() => [...authenticatedPaths].sort())
      .toEqual(["/api/v1/capabilities", "/api/v1/jobs", "/api/v1/settings"]);
    await expect(page.locator("body")).not.toContainText(TOKEN);

    await page.getByRole("button", { name: "清除 Token" }).click();
    await expect(page.locator("#token-state")).toHaveText("未保存 Token");
    await expect(page.locator("#connection-status")).toHaveText("请先设置本地端口和配对 Token");
    const persisted = await worker.evaluate(async () => chrome.storage.local.get("lvtConnection"));
    expect(persisted).toEqual({
      lvtConnection: { port: Number(new URL(baseUrl).port) },
    });
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

test("side panel submits a mixed batch and restores the real backend job list", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-chromium-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
    });
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    await worker.evaluate(
      async ({ port, token }) =>
        chrome.storage.local.set({
          lvtConnection: { port, token },
        }),
      { port: Number(new URL(baseUrl).port), token: TOKEN },
    );
    const postRequests: { headers: Record<string, string>; url: string }[] = [];
    context.on("request", (request) => {
      if (request.method() === "POST" && request.url() === `${baseUrl}/api/v1/jobs`) {
        postRequests.push({ headers: request.headers(), url: request.url() });
      }
    });

    const acceptedUrl =
      "https://example.test/media/a-very-long-video-title-for-layout-check/中文/пример";
    const rejectedUrl = "https://user:password@example.test/private";
    const clientInvalid = "not a URL";
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");

    await page.locator("#job-urls").fill([acceptedUrl, "", rejectedUrl, clientInvalid].join("\n"));
    await expect(page.locator("#url-count")).toHaveText("有效 2 / 无效 1");
    await expect(page.locator("#invalid-urls")).toContainText("第 4 行：URL 格式无效");
    await page.getByRole("button", { name: "提交任务" }).click();

    await expect(page.locator("#submission-result")).toHaveText("已创建 1 个，后端拒绝 1 个");
    await expect(page.locator("#job-urls")).toHaveValue([rejectedUrl, clientInvalid].join("\n"));
    await expect(page.locator("#server-rejections")).toContainText(rejectedUrl);
    await expect(page.locator(".job-row")).toHaveCount(1);
    await expect(page.locator("#jobs-empty")).toBeHidden();
    await expect(page.locator(".job-url")).toContainText("https://example.test/media/");
    await expect(page.locator(".job-title")).toHaveAttribute("title", /https:\/\/example\.test/);

    expect(postRequests).toHaveLength(1);
    expect(postRequests[0]?.headers["x-lvt-token"]).toBe(TOKEN);
    expect(postRequests[0]?.url).not.toContain(TOKEN);

    const jobs = await page.evaluate(async (origin) => {
      const connection = (await chrome.storage.local.get("lvtConnection")).lvtConnection as {
        token?: string;
      };
      const response = await fetch(`${origin}/api/v1/jobs`, {
        headers: { "X-LVT-Token": connection.token ?? "" },
      });
      return response.json() as Promise<unknown>;
    }, baseUrl);
    const parsedJobs = parseJobsResponse(jobs);
    const submitted = parsedJobs.find((job) =>
      job.sanitizedDisplayUrl.includes("a-very-long-video-title-for-layout-check"),
    );
    expect(submitted).toBeDefined();
    const submittedId = submitted?.uuid;
    if (submittedId === undefined) {
      throw new Error("submitted job was not returned by the backend");
    }
    const row = page.locator(`[data-job-id="${submittedId}"]`);
    await expect
      .poll(() =>
        page.evaluate(
          async ({ id, origin }) => {
            const connection = (await chrome.storage.local.get("lvtConnection")).lvtConnection as {
              token?: string;
            };
            const response = await fetch(`${origin}/api/v1/jobs`, {
              headers: { "X-LVT-Token": connection.token ?? "" },
            });
            const backendJobs = (await response.json()) as {
              uuid?: string;
              status?: string;
              overall_progress?: number;
              stage_progress?: number;
            }[];
            const backendJob = backendJobs.find((job) => job.uuid === id);
            const element = document.querySelector<HTMLElement>(`[data-job-id="${id}"]`);
            if (backendJob === undefined || element === null) {
              return false;
            }
            return (
              element.dataset.status === backendJob.status &&
              element.dataset.overallProgress === String(backendJob.overall_progress) &&
              element.dataset.stageProgress === String(backendJob.stage_progress)
            );
          },
          { id: submittedId, origin: baseUrl },
        ),
      )
      .toBe(true);
    expect(await row.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);

    const longTitle =
      "这是一个用于窄侧边栏布局验证的很长中文视频标题 Пример длинного русского заголовка";
    await row.evaluate((element) => {
      element.dataset.identityMarker = "preserved";
    });
    await seedCompletedJobForLayout(submittedId, longTitle);
    await expect(page.locator(".job-title")).toHaveText(longTitle);
    await expect(row).toHaveAttribute("data-identity-marker", "preserved");
    await expect(page.locator(".job-title")).toHaveAttribute("title", longTitle);
    await expect(page.locator(".job-status")).toHaveText("已完成");
    await expect(page.locator('[data-field="overall-value"]')).toHaveText("100%");
    await page.getByRole("tab", { name: "已完成" }).click();
    await expect(row).toBeVisible();
    await page.getByRole("tab", { name: "失败", exact: true }).click();
    await expect(page.locator("#jobs-empty")).toHaveText("当前筛选没有任务");
    await expect(page.locator("#job-list")).toBeHidden();
    await page.getByRole("tab", { name: "全部" }).click();
    await page.setViewportSize({ width: 320, height: 800 });
    await expect(row).toHaveScreenshot("checkpoint-3-job-row-320.png", {
      animations: "disabled",
    });
    await page.setViewportSize({ width: 420, height: 800 });
    await expect(row).toHaveScreenshot("checkpoint-3-job-row-420.png", {
      animations: "disabled",
    });

    await page.close();
    const reopened = await context.newPage();
    await reopened.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(reopened.locator("#connection-status")).toHaveText("本地服务连接正常");
    await expect(reopened.locator(`[data-job-id="${submittedId}"]`)).toBeVisible();
    await expect(reopened.locator("body")).not.toContainText(TOKEN);
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

test("job controls, confirmed delete, and paginated events use the real backend", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-chromium-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
    });
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    await worker.evaluate(
      async ({ port, token }) =>
        chrome.storage.local.set({
          lvtConnection: { port, token },
        }),
      { port: Number(new URL(baseUrl).port), token: TOKEN },
    );
    const writes: string[] = [];
    context.on("request", (request) => {
      if (
        request.url().startsWith(`${baseUrl}/api/v1/jobs/`) &&
        (request.method() === "POST" || request.method() === "DELETE")
      ) {
        writes.push(`${request.method()} ${request.url()}`);
      }
    });

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");
    const created = parseCreateJobsResponse(
      await page.evaluate(
        async ({ origin, token }) => {
          const response = await fetch(`${origin}/api/v1/jobs`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-LVT-Token": token,
            },
            body: JSON.stringify({
              urls: [
                "https://example.test/control-cancel",
                "https://example.test/control-retry",
                "https://example.test/control-delete",
              ],
            }),
          });
          return response.json() as Promise<unknown>;
        },
        { origin: baseUrl, token: TOKEN },
      ),
    );
    expect(created.accepted).toHaveLength(3);
    const [cancelJob, retryJob, deleteJob] = created.accepted;
    if (cancelJob === undefined || retryJob === undefined || deleteJob === undefined) {
      throw new Error("control test jobs were not created");
    }
    await seedControlJobs(cancelJob.uuid, retryJob.uuid, deleteJob.uuid);

    const cancelRow = page.locator(`[data-job-id="${cancelJob.uuid}"]`);
    const retryRow = page.locator(`[data-job-id="${retryJob.uuid}"]`);
    const deleteRow = page.locator(`[data-job-id="${deleteJob.uuid}"]`);
    await expect(cancelRow).toHaveAttribute("data-status", "queued");
    await expect(retryRow).toHaveAttribute("data-status", "failed");
    await expect(deleteRow).toHaveAttribute("data-status", "completed");

    await cancelRow.getByRole("button", { name: "取消任务 Cancel Target" }).dblclick();
    await expect
      .poll(() => writes.filter((request) => request.endsWith(`/${cancelJob.uuid}/cancel`)).length)
      .toBe(1);
    await expect(cancelRow).toHaveAttribute("data-status", "cancelled");

    await retryRow.getByRole("button", { name: "查看详情 Retry Target" }).click();
    await expect(page.locator("#detail-job-title")).toHaveText("Retry Target");
    await expect(page.locator("#detail-status")).toHaveText("失败");
    await expect(page.locator("#detail-asr-model")).toHaveText("mlx-community/whisper-small-mlx");
    await expect(page.locator("#event-count")).toHaveText("50 / 55 条");
    await page.getByRole("button", { name: "加载更多" }).click();
    await expect(page.locator("#event-count")).toHaveText("55 / 55 条");
    const eventIds = await page
      .locator(".event-item")
      .evaluateAll((items) => items.map((item) => Number((item as HTMLElement).dataset.eventId)));
    expect(eventIds).toEqual([...new Set(eventIds)].sort((left, right) => left - right));
    await expect(page.locator("#event-list")).toContainText(
      "来自 失败 · 恢复至 正在下载 · 错误 DOWNLOAD_FAILED · 原因 启动恢复",
    );
    await expect(page.locator("body")).not.toContainText("TimelineSecret");
    await expect(page.locator("#event-list img")).toHaveCount(0);

    const retryUrl = `${baseUrl}/api/v1/jobs/${retryJob.uuid}/retry`;
    let abortedRetryRequests = 0;
    await page.route(retryUrl, async (route) => {
      abortedRetryRequests += 1;
      await route.abort("failed");
    });
    const retryButton = page.getByRole("button", { name: "重试 Retry Target" });
    await retryButton.click();
    await expect(page.locator("#detail-action-message")).toHaveText(
      "操作结果未知，请刷新任务确认；不会自动重试",
    );
    await expect(page.locator("#detail-status")).toHaveText("失败");
    expect(abortedRetryRequests).toBe(1);
    await page.unroute(retryUrl);

    const retryWritesBeforeSuccess = writes.filter((request) =>
      request.endsWith(`/${retryJob.uuid}/retry`),
    ).length;
    await retryButton.dblclick();
    await expect
      .poll(() => writes.filter((request) => request.endsWith(`/${retryJob.uuid}/retry`)).length)
      .toBe(retryWritesBeforeSuccess + 1);
    await expect(page.locator("#detail-status")).not.toHaveText("失败");
    await page.getByRole("button", { name: "返回任务" }).click();

    const deleteButton = deleteRow.getByRole("button", { name: "删除 Delete Target" });
    await deleteButton.click();
    await expect(page.locator("#delete-dialog")).toBeVisible();
    await expect(page.locator("#delete-job-title")).toHaveText("Delete Target");
    await expect(page.locator("#delete-cancel")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.locator("#delete-confirm")).toBeFocused();
    await page.keyboard.press("Tab");
    expect(
      await page.locator("#delete-dialog").evaluate((dialog) => {
        return dialog.contains(document.activeElement);
      }),
    ).toBe(true);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.locator("#delete-dialog")).toBeHidden();
    await expect(deleteButton).toBeFocused();
    await deleteButton.click();
    await page.getByRole("button", { name: "确认删除" }).dblclick();
    await expect
      .poll(
        () =>
          writes.filter((request) =>
            request.startsWith(`DELETE ${baseUrl}/api/v1/jobs/${deleteJob.uuid}?confirm=true`),
          ).length,
      )
      .toBe(1);
    await expect(deleteRow).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText(TOKEN);
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

test("completed Job previews and downloads eight artifacts through authenticated fetch", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-chromium-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
    });
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    await worker.evaluate(
      async ({ port, token }) =>
        chrome.storage.local.set({
          lvtConnection: { port, token },
        }),
      { port: Number(new URL(baseUrl).port), token: TOKEN },
    );
    const artifactRequests: { headers: Record<string, string>; url: string }[] = [];
    context.on("request", (request) => {
      if (request.url().includes("/api/v1/artifacts/") && request.url().endsWith("/download")) {
        artifactRequests.push({ headers: request.headers(), url: request.url() });
      }
    });

    const page = await context.newPage();
    await page.addInitScript(() => {
      const active = new Set<string>();
      const downloadFilenames: string[] = [];
      const createObjectURL = URL.createObjectURL.bind(URL);
      const revokeObjectURL = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = (blob) => {
        const url = createObjectURL(blob);
        active.add(url);
        return url;
      };
      URL.revokeObjectURL = (url) => {
        active.delete(url);
        revokeObjectURL(url);
      };
      Object.defineProperty(window, "__activeArtifactBlobUrls", {
        get: () => active.size,
      });
      Object.defineProperty(window, "__artifactDownloadFilenames", {
        get: () => [...downloadFilenames],
      });
      chrome.downloads.download = (options) => {
        downloadFilenames.push(options.filename ?? "");
        return Promise.resolve(downloadFilenames.length);
      };
    });
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");
    const created = parseCreateJobsResponse(
      await page.evaluate(
        async ({ origin, token }) => {
          const response = await fetch(`${origin}/api/v1/jobs`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-LVT-Token": token,
            },
            body: JSON.stringify({ urls: ["https://example.test/artifact-preview"] }),
          });
          return response.json() as Promise<unknown>;
        },
        { origin: baseUrl, token: TOKEN },
      ),
    );
    const job = created.accepted[0];
    if (job === undefined) {
      throw new Error("artifact test job was not created");
    }
    await seedArtifactJob(job.uuid);

    const row = page.locator(`[data-job-id="${job.uuid}"]`);
    await expect(row).toHaveAttribute("data-status", "completed");
    await row.getByRole("button", { name: "查看详情 Artifact Target" }).click();
    await expect(page.locator("#artifact-count")).toHaveText("8 / 8");
    await expect(page.locator(".artifact-row")).toHaveCount(8);
    expect(await page.locator(".artifact-kind").allTextContents()).toEqual([
      "source.txt",
      "source.srt",
      "source.vtt",
      "source.json",
      "zh-CN.txt",
      "zh-CN.srt",
      "zh-CN.vtt",
      "zh-CN.json",
    ]);

    await page.getByRole("button", { name: "预览 source.json" }).click();
    await expect(page.locator(".preview-segment")).toHaveCount(2);
    expect(
      await page
        .locator(".preview-segment")
        .evaluateAll((segments) =>
          segments.map((segment) => (segment as HTMLElement).dataset.segmentId),
        ),
    ).toEqual(["1", "2"]);
    await expect(page.locator(".preview-segment").nth(0)).toContainText(
      "#100:00:00.125 → 00:00:02.100Speaker 1Hello <img src=x>",
    );
    await expect(page.locator("#preview-segments img")).toHaveCount(0);
    await page.getByRole("tab", { name: "中文" }).click();
    await expect(page.locator(".preview-segment").nth(0)).toContainText("你好 <script>");
    await expect(page.locator("#preview-segments script")).toHaveCount(0);
    await page.setViewportSize({ width: 360, height: 900 });
    await expect(page.locator("#artifact-section")).toHaveScreenshot(
      "checkpoint-5a-artifact-preview-360.png",
      { animations: "disabled" },
    );

    for (const kind of [
      "source.txt",
      "source.srt",
      "source.vtt",
      "source.json",
      "zh-CN.txt",
      "zh-CN.srt",
      "zh-CN.vtt",
      "zh-CN.json",
    ]) {
      await page.getByRole("button", { name: `下载 ${kind}` }).click();
      await expect(page.locator("#artifact-message")).toHaveText(`已开始下载 ${kind}`);
    }

    expect(
      await page.evaluate(
        () =>
          (
            window as unknown as {
              __artifactDownloadFilenames?: string[];
            }
          ).__artifactDownloadFilenames ?? [],
      ),
    ).toEqual([
      `Artifact Target--${job.uuid.slice(0, 8)}/source.txt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/source.srt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/source.vtt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/source.json`,
      `Artifact Target--${job.uuid.slice(0, 8)}/zh-CN.txt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/zh-CN.srt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/zh-CN.vtt`,
      `Artifact Target--${job.uuid.slice(0, 8)}/zh-CN.json`,
    ]);
    expect(
      await page.evaluate(
        () =>
          (window as unknown as { __activeArtifactBlobUrls?: number }).__activeArtifactBlobUrls ??
          -1,
      ),
    ).toBe(0);
    expect(artifactRequests).toHaveLength(10);
    for (const request of artifactRequests) {
      expect(request.url).toMatch(
        new RegExp(`^${baseUrl.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")}/api/v1/artifacts/`),
      );
      expect(request.url).not.toContain(TOKEN);
      expect(request.headers["x-lvt-token"]).toBe(TOKEN);
    }
    await expect(page.locator("body")).not.toContainText(TOKEN);
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

async function extensionWorker(context: BrowserContext): Promise<Worker> {
  const existing = context.serviceWorkers()[0];
  return existing ?? context.waitForEvent("serviceworker");
}

async function seedCompletedJobForLayout(jobId: string, title: string): Promise<void> {
  if (dataRoot === undefined) {
    throw new Error("E2E data root was not initialized");
  }
  const database = resolve(dataRoot, "db/lvt.sqlite3");
  await execFileAsync(PYTHON, [
    "-c",
    [
      "import sqlite3, sys",
      "connection = sqlite3.connect(sys.argv[1])",
      [
        "connection.execute(",
        "\"UPDATE jobs SET title = ?, status = 'completed', stage_progress = 100, \"",
        "\"overall_progress = 100, detected_language = 'ru', duration_ms = 123000, \"",
        "\"started_at = '2026-08-23T10:00:00+00:00', \"",
        "\"finished_at = '2026-08-23T10:02:03+00:00' WHERE uuid = ?\",",
        "(sys.argv[3], sys.argv[2]))",
      ].join(""),
      "connection.commit()",
      "connection.close()",
    ].join("; "),
    database,
    jobId,
    title,
  ]);
}

async function seedControlJobs(
  cancelJobId: string,
  retryJobId: string,
  deleteJobId: string,
): Promise<void> {
  if (dataRoot === undefined) {
    throw new Error("E2E data root was not initialized");
  }
  const database = resolve(dataRoot, "db/lvt.sqlite3");
  await execFileAsync(PYTHON, [
    "-c",
    [
      "import json, sqlite3, sys",
      "connection = sqlite3.connect(sys.argv[1])",
      [
        "connection.execute(",
        "\"UPDATE jobs SET title = 'Cancel Target', status = 'queued', stage_progress = 0, \"",
        '"overall_progress = 0, active_run_id = NULL, "',
        "\"next_attempt_at = '2099-01-01T00:00:00+00:00' WHERE uuid = ?\",",
        "(sys.argv[2],))",
      ].join(""),
      [
        "connection.execute(",
        "\"UPDATE jobs SET title = 'Retry Target', status = 'failed', \"",
        "\"error_code = 'DOWNLOAD_FAILED', error_message = 'download failed', \"",
        "\"active_run_id = NULL, finished_at = '2026-08-23T10:02:03+00:00' WHERE uuid = ?\",",
        "(sys.argv[3],))",
      ].join(""),
      [
        "connection.execute(",
        "\"UPDATE jobs SET title = 'Delete Target', status = 'completed', \"",
        '"stage_progress = 100, overall_progress = 100, active_run_id = NULL, "',
        "\"finished_at = '2026-08-23T10:02:03+00:00' WHERE uuid = ?\",",
        "(sys.argv[4],))",
      ].join(""),
      "connection.execute('DELETE FROM job_events WHERE job_id = ?', (sys.argv[3],))",
      [
        "message = json.dumps({",
        "'from_status': 'failed', 'resume_stage': 'downloading', ",
        "'error_code': 'DOWNLOAD_FAILED', 'reason': 'startup_recovery', ",
        "'input': '<img src=x onerror=TimelineSecret>', ",
        "'ctx': {'secret': 'TimelineSecret'}",
        "}, sort_keys=True)",
      ].join(""),
      [
        "events = [(sys.argv[3], 'progress', message, ",
        "f'2026-08-23T10:00:{index:02d}+00:00') for index in range(55)]",
      ].join(""),
      [
        "connection.executemany(",
        "'INSERT INTO job_events (job_id, status, message, created_at) VALUES (?, ?, ?, ?)', ",
        "events)",
      ].join(""),
      "connection.commit()",
      "connection.close()",
    ].join("; "),
    database,
    cancelJobId,
    retryJobId,
    deleteJobId,
  ]);
}

async function seedArtifactJob(jobId: string): Promise<void> {
  if (dataRoot === undefined) {
    throw new Error("E2E data root was not initialized");
  }
  const database = resolve(dataRoot, "db/lvt.sqlite3");
  const workRoot = resolve(dataRoot, "work");
  await execFileAsync(PYTHON, [
    "-c",
    [
      "import hashlib, json, pathlib, sqlite3, sys, uuid",
      "database, work_root, job_id = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]",
      "run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'e2e-run:{job_id}'))",
      "stage = work_root / job_id / 'runs' / run_id / 'export_manifest'",
      "exports = stage / 'exports'",
      "exports.mkdir(parents=True, exist_ok=True)",
      [
        "segments = [",
        "{'id': 1, 'start_ms': 125, 'end_ms': 2100, 'speaker': 'Speaker 1', ",
        "'source_language': 'en', 'source_text': 'Hello <img src=x>', ",
        "'translated_text': '你好 <script>', 'metadata': {}}, ",
        "{'id': 2, 'start_ms': 2200, 'end_ms': 4999, 'speaker': 'Speaker 2', ",
        "'source_language': 'en', 'source_text': 'Second line.', ",
        "'translated_text': '第二行。', 'metadata': {}}]",
      ].join(""),
      [
        "base = {'schema_version': '1.0', 'job_id': job_id, ",
        "'source_url': 'https://example.test/artifact-preview', 'title': 'Artifact Target', ",
        "'duration_ms': 5000, 'detected_language': 'en', 'engine_versions': {}, ",
        "'processing_options': {}, 'segments': segments, 'warnings': []}",
      ].join(""),
      "source = json.loads(json.dumps(base))",
      "[segment.update({'translated_text': ''}) for segment in source['segments']]",
      [
        "files = {",
        "'source.txt': 'Hello <img src=x>\\nSecond line.\\n', ",
        "'source.srt': '1\\n00:00:00,125 --> 00:00:02,100\\nHello <img src=x>\\n', ",
        "'source.vtt': 'WEBVTT\\n\\n00:00:00.125 --> 00:00:02.100\\nHello <img src=x>\\n', ",
        "'source.json': json.dumps(source, ensure_ascii=False, indent=2) + '\\n', ",
        "'zh-CN.txt': '你好 <script>\\n第二行。\\n', ",
        "'zh-CN.srt': '1\\n00:00:00,125 --> 00:00:02,100\\n你好 <script>\\n', ",
        "'zh-CN.vtt': 'WEBVTT\\n\\n00:00:00.125 --> 00:00:02.100\\n你好 <script>\\n', ",
        "'zh-CN.json': json.dumps(base, ensure_ascii=False, indent=2) + '\\n'}",
      ].join(""),
      [
        "[(exports / kind).write_text(content, encoding='utf-8') ",
        "for kind, content in files.items()]",
      ].join(""),
      [
        "outputs = [{'kind': kind, ",
        "'relative_path': (exports / kind).relative_to(work_root).as_posix(), ",
        "'byte_size': (exports / kind).stat().st_size, ",
        "'sha256': hashlib.sha256((exports / kind).read_bytes()).hexdigest()} ",
        "for kind in sorted(files)]",
      ].join(""),
      [
        "(stage / 'manifest.json').write_text(json.dumps({",
        "'job_id': job_id, 'run_id': run_id, 'stage': 'export_manifest', 'outputs': outputs",
        "}), encoding='utf-8')",
      ].join(""),
      "(stage / '.published').write_text('published', encoding='utf-8')",
      "connection = sqlite3.connect(database)",
      "connection.execute('DELETE FROM artifacts WHERE job_id = ?', (job_id,))",
      [
        "connection.executemany(",
        "'INSERT INTO artifacts (id, job_id, kind, path, created_at) VALUES (?, ?, ?, ?, ?)', ",
        "[(str(uuid.uuid5(uuid.NAMESPACE_URL, f'e2e-artifact:{job_id}:{kind}')), job_id, kind, ",
        "(exports / kind).relative_to(work_root).as_posix(), '2026-08-23T10:02:03+00:00') ",
        "for kind in sorted(files)])",
      ].join(""),
      [
        "connection.execute(",
        "\"UPDATE jobs SET title = 'Artifact Target', status = 'completed', \"",
        '"stage_progress = 100, overall_progress = 100, active_run_id = NULL, "',
        "\"detected_language = 'en', duration_ms = 5000, \"",
        "\"checkpoint_pointer = ?, finished_at = '2026-08-23T10:02:03+00:00' \"",
        '"WHERE uuid = ?", ',
        "((stage / 'manifest.json').relative_to(work_root).as_posix(), job_id))",
      ].join(""),
      "connection.commit()",
      "connection.close()",
    ].join("; "),
    database,
    workRoot,
    jobId,
  ]);
}

async function reservePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    server.close();
    throw new Error("failed to reserve an IPv4 test port");
  }
  await new Promise<void>((resolvePromise, reject) => {
    server.close((error) => (error === undefined ? resolvePromise() : reject(error)));
  });
  return address.port;
}

async function waitForOutput(child: ChildProcessWithoutNullStreams, marker: string): Promise<void> {
  await new Promise<void>((resolvePromise, reject) => {
    let output = "";
    const deadline = setTimeout(() => {
      cleanup();
      reject(new Error(`Uvicorn startup timed out:\n${output}`));
    }, 30_000);
    const onData = (chunk: Buffer) => {
      output += chunk.toString();
      if (output.includes(marker)) {
        cleanup();
        resolvePromise();
      }
    };
    const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
      cleanup();
      reject(
        new Error(`Uvicorn exited before startup: code=${String(code)} signal=${String(signal)}`),
      );
    };
    const cleanup = () => {
      clearTimeout(deadline);
      child.stdout.off("data", onData);
      child.stderr.off("data", onData);
      child.off("exit", onExit);
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.once("exit", onExit);
  });
}

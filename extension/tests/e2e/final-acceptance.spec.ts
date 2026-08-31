import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, resolve } from "node:path";

import {
  chromium,
  expect,
  test,
  type BrowserContext,
  type CDPSession,
  type Route,
  type Worker,
} from "@playwright/test";

import { parseJobsResponse } from "../../src/api/contracts";
import { cleanupE2eResources, stopProcess } from "../support/e2e-resources";

const PROJECT_ROOT = resolve(import.meta.dirname, "../../..");
const BACKEND_ROOT = resolve(PROJECT_ROOT, "backend");
const PYTHON =
  process.env.LVT_E2E_PYTHON ??
  (process.platform === "win32"
    ? resolve(BACKEND_ROOT, ".venv/Scripts/python.exe")
    : resolve(PROJECT_ROOT, ".venv-smoke/bin/python"));
const EXTENSION_PATH = resolve(PROJECT_ROOT, "extension/dist");
const TOKEN = "PhaseThreeFinalAcceptanceToken";
const LIFECYCLE_PING = "lvt.lifecycle.ping";
const LIFECYCLE_READY = "lvt.lifecycle.ready";

test("real backend outage converges without losing the last Job snapshot", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-final-profile-"));
  const dataRoot = await mkdtemp(resolve(tmpdir(), "lvt-final-data-"));
  const port = await reservePort();
  const baseUrl = `http://127.0.0.1:${String(port)}`;
  let backend: ChildProcessWithoutNullStreams | undefined;
  let context: BrowserContext | undefined;
  try {
    context = await launchExtension(profile);
    const worker = await extensionWorker(context);
    const extensionId = new URL(worker.url()).host;
    await setConnection(worker, port, TOKEN);

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(page.locator("#connection-status")).toHaveText(
      "本地服务未启动，请先启动 Local Video Transcriber",
    );

    backend = startBackend(port, dataRoot);
    await waitForOutput(backend, "Application startup complete.");
    await page.getByRole("button", { name: "重新连接" }).click();
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");

    await page.locator("#job-urls").fill("https://example.test/final-acceptance");
    await page.getByRole("button", { name: "提交任务" }).click();
    await expect(page.locator("#submission-result")).toHaveText("已创建 1 个，后端拒绝 0 个");
    const jobs = parseJobsResponse(
      await page.evaluate(
        async ({ origin, token }) => {
          const response = await fetch(`${origin}/api/v1/jobs`, {
            headers: { "X-LVT-Token": token },
            redirect: "error",
          });
          return response.json() as Promise<unknown>;
        },
        { origin: baseUrl, token: TOKEN },
      ),
    );
    const jobId = jobs[0]?.uuid;
    if (jobId === undefined) {
      throw new Error("final acceptance Job was not returned by the backend");
    }
    const jobRow = page.locator(`[data-job-id="${jobId}"]`);
    await expect(jobRow).toBeVisible();

    let releaseJobs!: () => void;
    let markJobsStarted!: () => void;
    const jobsRelease = new Promise<void>((resolvePromise) => {
      releaseJobs = resolvePromise;
    });
    const jobsStarted = new Promise<void>((resolvePromise) => {
      markJobsStarted = resolvePromise;
    });
    const delayedJobs = async (route: Route): Promise<void> => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      markJobsStarted();
      await jobsRelease;
      await route.continue();
    };
    await page.route(`${baseUrl}/api/v1/jobs`, delayedJobs);
    await page.getByRole("button", { name: "重新连接" }).click();
    await jobsStarted;
    await stopProcess(backend);
    backend = undefined;
    releaseJobs();

    await expect(page.locator("#connection-status")).toHaveText(
      "本地服务未启动，请先启动 Local Video Transcriber",
    );
    await expect(jobRow).toBeVisible();
    await page.unroute(`${baseUrl}/api/v1/jobs`, delayedJobs);

    backend = startBackend(port, dataRoot);
    await waitForOutput(backend, "Application startup complete.");
    await page.getByRole("button", { name: "重新连接" }).click();
    await expect(page.locator("#connection-status")).toHaveText("本地服务连接正常");
    await expect(jobRow).toBeVisible();

    await page.close();
    const reopened = await context.newPage();
    await reopened.goto(`chrome-extension://${extensionId}/sidepanel.html`);
    await expect(reopened.locator("#connection-status")).toHaveText("本地服务连接正常");
    await expect(reopened.locator(`[data-job-id="${jobId}"]`)).toBeVisible();
    await expect(reopened.locator("#connection-token")).toHaveValue("");
    await expect(reopened.locator("body")).not.toContainText(TOKEN);
  } finally {
    await context?.close();
    await cleanupE2eResources({ backend, dataRoot });
    await rm(profile, { force: true, recursive: true });
  }
});

test("service worker revives once through action and runtime message", async () => {
  const profile = await mkdtemp(resolve(tmpdir(), "lvt-worker-profile-"));
  let context: BrowserContext | undefined;
  try {
    context = await launchExtension(profile);
    const initialWorker = await extensionWorker(context);
    const extensionId = new URL(initialWorker.url()).host;
    const port = await reservePort();
    await setConnection(initialWorker, port, TOKEN);

    const hostPage = await context.newPage();
    await hostPage.goto("data:text/html,<title>action-target</title>");
    const browser = context.browser();
    if (browser === null) {
      throw new Error("persistent Chromium context did not expose its browser");
    }
    const browserCdp = await browser.newBrowserCDPSession();
    const workerCdp = await context.newCDPSession(hostPage);
    const hostTargetId = await targetIdForUrl(browserCdp, hostPage.url());

    await stopExtensionWorker(workerCdp, browserCdp, extensionId);
    await browserCdp.send("Extensions.triggerAction", { id: extensionId, targetId: hostTargetId });
    await expect
      .poll(() => extensionWorkerTargetExists(browserCdp, extensionId), {
        message: "action-revived service worker",
      })
      .toBe(true);
    await expect
      .poll(() => sidePanelTargetCount(browserCdp, extensionId), {
        message: "one action-opened side panel",
      })
      .toBe(1);

    await browserCdp.send("Extensions.triggerAction", { id: extensionId, targetId: hostTargetId });
    await hostPage.waitForTimeout(250);
    const repeatedActionCount = await sidePanelTargetCount(browserCdp, extensionId);
    expect(repeatedActionCount).toBeLessThanOrEqual(1);
    if (repeatedActionCount === 0) {
      await browserCdp.send("Extensions.triggerAction", {
        id: extensionId,
        targetId: hostTargetId,
      });
      await expect
        .poll(() => sidePanelTargetCount(browserCdp, extensionId), {
          message: "side panel reopened after platform toggle",
        })
        .toBe(1);
    }

    const messagePage = await context.newPage();
    await messagePage.goto(extensionSidePanelUrl(extensionId));
    expect(await messagePage.evaluate(async () => chrome.sidePanel.getPanelBehavior())).toEqual({
      openPanelOnActionClick: true,
    });

    await stopExtensionWorker(workerCdp, browserCdp, extensionId);
    const responsePromise: Promise<unknown> = messagePage.evaluate(
      async (type): Promise<unknown> => {
        const response: unknown = await chrome.runtime.sendMessage({ type });
        return response;
      },
      LIFECYCLE_PING,
    );
    const response = await responsePromise;
    await expect
      .poll(() => extensionWorkerTargetExists(browserCdp, extensionId), {
        message: "message-revived service worker",
      })
      .toBe(true);
    expect(response).toEqual({ type: LIFECYCLE_READY });

    const persisted = await messagePage.evaluate(
      async () => (await chrome.storage.local.get("lvtConnection")).lvtConnection,
    );
    expect(persisted).toEqual({ port, token: TOKEN });
    await expect(messagePage.locator("#connection-token")).toHaveValue("");
    await expect(messagePage.locator("body")).not.toContainText(TOKEN);
  } finally {
    await context?.close();
    await rm(profile, { force: true, recursive: true });
  }
});

async function launchExtension(profile: string): Promise<BrowserContext> {
  return chromium.launchPersistentContext(profile, {
    channel: "chromium",
    headless: true,
    args: [`--disable-extensions-except=${EXTENSION_PATH}`, `--load-extension=${EXTENSION_PATH}`],
  });
}

async function setConnection(worker: Worker, port: number, token: string): Promise<void> {
  await worker.evaluate(
    async (connection) => chrome.storage.local.set({ lvtConnection: connection }),
    { port, token },
  );
}

function startBackend(port: number, dataRoot: string): ChildProcessWithoutNullStreams {
  return spawn(
    PYTHON,
    ["-m", "uvicorn", "lvt.main:app", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: BACKEND_ROOT,
      env: {
        ...process.env,
        LVT_DATA_ROOT: dataRoot,
        LVT_TOKEN: TOKEN,
        PYTHONPATH: [resolve(BACKEND_ROOT, "src"), process.env.PYTHONPATH]
          .filter((value): value is string => value !== undefined && value.length > 0)
          .join(delimiter),
        PYTHONUNBUFFERED: "1",
      },
      stdio: "pipe",
    },
  );
}

async function extensionWorker(context: BrowserContext): Promise<Worker> {
  const existing = context.serviceWorkers()[0];
  return existing ?? context.waitForEvent("serviceworker");
}

async function stopExtensionWorker(
  workerCdp: CDPSession,
  browserCdp: CDPSession,
  extensionId: string,
): Promise<void> {
  const workerUrl = `chrome-extension://${extensionId}/background.js`;
  const versions = new Map<string, string>();
  const onVersions = (event: { versions: { scriptURL: string; versionId: string }[] }) => {
    for (const version of event.versions) {
      versions.set(version.scriptURL, version.versionId);
    }
  };
  await workerCdp.send("ServiceWorker.disable");
  workerCdp.on("ServiceWorker.workerVersionUpdated", onVersions);
  await workerCdp.send("ServiceWorker.enable");
  await expect
    .poll(() => versions.get(workerUrl), { message: "service worker version" })
    .toBeTruthy();
  const versionId = versions.get(workerUrl);
  if (versionId === undefined) {
    throw new Error("extension service worker version was not reported");
  }
  await workerCdp.send("ServiceWorker.stopWorker", { versionId });
  await expect
    .poll(async () => {
      const { targetInfos } = await browserCdp.send("Target.getTargets");
      return targetInfos.some(
        (target) => target.type === "service_worker" && target.url === workerUrl,
      );
    })
    .toBe(false);
  workerCdp.off("ServiceWorker.workerVersionUpdated", onVersions);
}

async function targetIdForUrl(cdp: CDPSession, url: string): Promise<string> {
  const { targetInfos } = await cdp.send("Target.getTargets", { filter: [{}] });
  const targetId = targetInfos.find(
    (target) => target.type === "tab" && target.url === url,
  )?.targetId;
  if (targetId === undefined) {
    throw new Error(`tab target not found for ${url}`);
  }
  return targetId;
}

async function sidePanelTargetCount(cdp: CDPSession, extensionId: string): Promise<number> {
  const { targetInfos } = await cdp.send("Target.getTargets");
  const url = extensionSidePanelUrl(extensionId);
  return targetInfos.filter((target) => target.url === url).length;
}

async function extensionWorkerTargetExists(cdp: CDPSession, extensionId: string): Promise<boolean> {
  const { targetInfos } = await cdp.send("Target.getTargets");
  const url = `chrome-extension://${extensionId}/background.js`;
  return targetInfos.some((target) => target.type === "service_worker" && target.url === url);
}

function extensionSidePanelUrl(extensionId: string): string {
  return `chrome-extension://${extensionId}/sidepanel.html`;
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
        new Error(
          `Uvicorn exited before startup: code=${String(code)} signal=${String(signal)}\n${output}`,
        ),
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

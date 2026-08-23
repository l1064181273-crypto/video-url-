import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { chromium, expect, test, type BrowserContext, type Worker } from "@playwright/test";

import {
  parseApiErrorResponse,
  parseCapabilitiesResponse,
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

async function extensionWorker(context: BrowserContext): Promise<Worker> {
  const existing = context.serviceWorkers()[0];
  return existing ?? context.waitForEvent("serviceworker");
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

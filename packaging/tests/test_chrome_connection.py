from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "tools"))

from publish_install import chrome_connection_instructions  # noqa: E402


def test_chrome_instructions_use_stable_extension_path_without_token(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "Library/Application Support/LocalVideoTranscriber"
    secret = "LVT_SECRET_" + "x" * 48
    token = data_root / "config/api-token"
    token.parent.mkdir(parents=True)
    token.write_text(secret, encoding="ascii")
    token.chmod(0o600)

    instructions = chrome_connection_instructions(data_root)
    rendered = "\n".join(instructions)

    assert str(data_root / "extension") in rendered
    assert "extension.next" not in rendered
    assert "releases/" not in rendered
    assert secret not in rendered
    assert "chrome://extensions" in rendered


def test_stable_extension_contract_exposes_existing_seven_capabilities() -> None:
    manifest = json.loads((ROOT / "extension/dist/manifest.json").read_text(encoding="utf-8"))
    contracts = (ROOT / "extension/src/api/contracts.ts").read_text(encoding="utf-8")
    diagnostics = (ROOT / "extension/src/ui/diagnostics.ts").read_text(encoding="utf-8")

    assert manifest["host_permissions"] == ["http://127.0.0.1/*"]
    for identifier in (
        "ffmpeg",
        "ollama",
        "asr_package",
        "asr_model",
        "diarization",
        "translation_primary",
        "translation_fallback",
    ):
        assert identifier in contracts or identifier in diagnostics


def test_real_chromium_loads_stable_extension_and_renders_seven_capabilities(
    tmp_path: Path,
) -> None:
    playwright = ROOT / "extension/node_modules/@playwright/test"
    browser_cache = Path.home() / "Library/Caches/ms-playwright"
    if not playwright.is_dir() or not browser_cache.is_dir():
        pytest.skip("Playwright Chromium is not installed")

    stable_extension = tmp_path / "LocalVideoTranscriber/extension"
    shutil.copytree(ROOT / "extension/dist", stable_extension)
    profile = tmp_path / "chromium-profile"
    script = textwrap.dedent(
        """
        import { randomBytes } from "node:crypto";
        import { rm } from "node:fs/promises";
        import { chromium } from "@playwright/test";

        const stablePath = process.argv[1];
        const profile = process.argv[2];
        const token = randomBytes(32).toString("hex");
        const origin = "http://127.0.0.1:8765";
        const names = [
          "ffmpeg",
          "ollama",
          "asr_package",
          "asr_model",
          "diarization",
          "translation_primary",
          "translation_fallback",
        ];
        const checkedAt = "2026-08-26T00:00:00+00:00";
        const component = (name) => ({
          status: "available",
          checked_at: checkedAt,
          ...(
            ["asr_model", "translation_primary", "translation_fallback"].includes(name)
              ? { model: `fixture:${name}` }
              : {}
          ),
        });
        const capabilities = {
          checked_at: checkedAt,
          ttl_seconds: 5,
          ...Object.fromEntries(names.map((name) => [name, component(name)])),
        };
        const responses = {
          "/health": { status: "healthy", version: "0.1.0" },
          "/api/v1/settings": {
            worker_concurrency: 1,
            runtime_effect: "new_claims_only",
          },
          "/api/v1/capabilities": capabilities,
          "/api/v1/jobs": [],
        };

        let context;
        try {
          context = await chromium.launchPersistentContext(profile, {
            channel: "chromium",
            headless: true,
            args: [
              `--disable-extensions-except=${stablePath}`,
              `--load-extension=${stablePath}`,
            ],
          });
          const worker =
            context.serviceWorkers()[0] ??
            await context.waitForEvent("serviceworker");
          await worker.evaluate(
            async (connection) =>
              chrome.storage.local.set({ lvtConnection: connection }),
            { port: 8765, token },
          );

          const authenticatedPaths = new Set();
          await context.route(`${origin}/**`, async (route) => {
            const request = route.request();
            const pathname = new URL(request.url()).pathname;
            if (pathname !== "/health") {
              if (request.headers()["x-lvt-token"] !== token) {
                throw new Error(`missing authentication for ${pathname}`);
              }
              authenticatedPaths.add(pathname);
            }
            const body = responses[pathname];
            if (body === undefined) {
              throw new Error(`unexpected request ${pathname}`);
            }
            await route.fulfill({
              status: 200,
              contentType: "application/json",
              body: JSON.stringify(body),
            });
          });

          const consoleMessages = [];
          const page = await context.newPage();
          page.on("console", (message) => consoleMessages.push(message.text()));
          const extensionId = new URL(worker.url()).host;
          await page.goto(`chrome-extension://${extensionId}/sidepanel.html`);
          await page.waitForFunction(
            () =>
              document.querySelector("#connection-status")?.textContent ===
              "本地服务连接正常",
          );

          const rows = await page
            .locator(".capability-row")
            .evaluateAll((elements) =>
              elements.map((element) => element.getAttribute("data-capability")),
            );
          if (JSON.stringify(rows) !== JSON.stringify(names)) {
            throw new Error(`unexpected capability rows: ${JSON.stringify(rows)}`);
          }
          const requiredPaths = [
            "/api/v1/capabilities",
            "/api/v1/jobs",
            "/api/v1/settings",
          ];
          if (!requiredPaths.every((path) => authenticatedPaths.has(path))) {
            throw new Error("stable extension did not call every protected API");
          }
          if (page.url().includes(token)) {
            throw new Error("token leaked into extension URL");
          }
          if ((await page.locator("body").innerText()).includes(token)) {
            throw new Error("token leaked into extension DOM");
          }
          if (consoleMessages.join("\\n").includes(token)) {
            throw new Error("token leaked into browser console");
          }
          if ((await page.locator("#connection-token").inputValue()) !== "") {
            throw new Error("token remained in the visible input");
          }
        } finally {
          await context?.close();
          await rm(profile, { force: true, recursive: true });
        }
        """
    )

    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(stable_extension),
            str(profile),
        ],
        cwd=ROOT / "extension",
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr

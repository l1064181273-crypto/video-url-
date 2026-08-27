from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "tools"))
sys.path.insert(0, str(ROOT / "backend" / "src"))

from process_state import (  # noqa: E402
    SystemServiceOperations,
    _group_snapshots,
    _signal_group_members,
    _signal_snapshot,
    _snapshot,
    _token_is_live,
)
from publish_install import (  # noqa: E402
    FirstInstallPublisher,
    SystemPublicationServices,
    chrome_connection_instructions,
)


class ExtensionPublicationFixtureServices:
    def validate_candidate(self, phase: str) -> bool:
        return True

    def start_precommit(self) -> object:
        return object()

    def runtime_full(self) -> bool:
        return True

    def activate(self, handle: object) -> None:
        return

    def healthy(self) -> bool:
        return True

    def stop_candidate(self) -> None:
        return

    def copy_token(self, token_path: Path) -> None:
        assert token_path.stat().st_mode & 0o777 == 0o600


def _cleanup_chrome_group(profile: Path) -> None:
    marker = profile / "browser-process.json"
    if not marker.is_file():
        return
    payload = json.loads(marker.read_text(encoding="ascii"))
    anchor = _snapshot(payload["pid"])
    if anchor is None:
        return
    assert anchor.pgid == payload["pgid"]
    group = _group_snapshots(anchor.pgid)
    assert group is not None
    assert any(
        member.pid == anchor.pid and member.signal_token == anchor.signal_token for member in group
    )
    assert _signal_group_members(group, anchor.pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    completion = threading.Event()
    while any(_token_is_live(member) for member in group):
        assert time.monotonic() < deadline
        completion.wait(0.02)


def _prepare_production_backend_fixture(data_root: Path, release: Path) -> None:
    (release / ".venv").symlink_to(ROOT / "backend/.venv", target_is_directory=True)
    shutil.copytree(ROOT / "backend/src", release / "backend/src")
    shutil.copytree(ROOT / "packaging/tools", release / "packaging/tools")
    dependencies = {
        "schema_version": 1,
        "artifacts": [
            {
                "id": "ollama",
                "version": "fixture",
            }
        ],
        "ollama_models": [],
    }
    dependencies_path = release / "packaging/dependencies.json"
    dependencies_path.write_text(json.dumps(dependencies), encoding="utf-8")

    ollama = data_root / "app/tools/ollama/fixture/bin/ollama"
    ollama.parent.mkdir(parents=True)
    ollama.write_text(
        f"""#!{ROOT / "backend/.venv/bin/python"}
import http.server
import json
import os
import pathlib
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        payload = (
            {{"version": "fixture"}}
            if self.path == "/api/version"
            else {{"models": [
                {{"name": "hy-mt2:1.8b-q4km-fixed"}},
                {{"name": "qwen2.5:1.5b"}},
            ]}}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *_args):
        return


record = pathlib.Path(os.environ["OLLAMA_MODELS"]).parents[1] / "runtime/ollama.pid"
ready = threading.Event()
while not record.is_file():
    ready.wait(0.01)
http.server.HTTPServer(("127.0.0.1", 11435), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    ollama.chmod(0o700)

    ffmpeg_dir = data_root / "app/tools/ffmpeg/fixture/bin"
    ffmpeg_dir.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        executable = ffmpeg_dir / name
        executable.write_text(
            f"#!/bin/sh\nprintf '{name} version fixture\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    digests = {
        name: hashlib.sha256((ffmpeg_dir / name).read_bytes()).hexdigest()
        for name in ("ffmpeg", "ffprobe")
    }
    install_state = data_root / "runtime/install-state.json"
    install_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg": {
                    "version": "fixture",
                    "directory": "tools/ffmpeg/fixture/bin",
                    "sha256": digests,
                },
            }
        ),
        encoding="utf-8",
    )


def _cleanup_production_services(
    services: SystemPublicationServices,
    data_root: Path,
    release: Path,
) -> None:
    try:
        services.stop_candidate()
    except Exception:
        operations = SystemServiceOperations(data_root, release)
        operations.reconcile()
        if any(operations.state(kind) != "absent" for kind in ("backend", "ollama")):
            raise


def _published_production_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str, FirstInstallPublisher]:
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.0"
    shutil.copytree(ROOT / "extension/dist", release / "extension")
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    token_path = data_root / "config/api-token"
    token_path.parent.mkdir(parents=True)
    secret = "LVT_SENTINEL_" + "7" * 48
    token_path.write_text(secret, encoding="ascii")
    token_path.chmod(0o600)
    (data_root / "runtime").mkdir()
    _prepare_production_backend_fixture(data_root, release)
    publisher = FirstInstallPublisher(
        data_root,
        release,
        services=ExtensionPublicationFixtureServices(),
    )
    publisher.publish(lock_held=True)
    return data_root, release, token_path, secret, publisher


def _require_production_acceptance_ports() -> None:
    for port in (8765, 11435):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                pytest.skip(f"production acceptance port {port} is occupied")


def _read_production_capabilities(secret: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
    try:
        connection.request(
            "GET",
            "/api/v1/capabilities",
            headers={"X-LVT-Token": secret},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
    assert response.status == 200
    assert isinstance(payload, dict)
    return payload


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_system_publication_services_starts_installed_lvt_main_with_real_probes(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    _require_production_acceptance_ports()
    data_root, release, _token_path, secret, _publisher = _published_production_fixture(tmp_path)
    services = SystemPublicationServices(data_root, release)
    handle = services.start_precommit()
    request.addfinalizer(lambda: _cleanup_production_services(services, data_root, release))
    services.activate(handle)

    backend_record = json.loads((data_root / "runtime/backend.pid").read_text(encoding="utf-8"))
    backend_command = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(backend_record["service"]["pid"])],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    capabilities = _read_production_capabilities(secret)

    assert backend_command.returncode == 0
    assert "-m lvt.main" in backend_command.stdout
    assert capabilities["ffmpeg"].get("status") == "available"
    assert capabilities["ollama"].get("status") == "available"
    assert capabilities["checked_at"] != "2026-08-26T00:00:00+00:00"
    _cleanup_production_services(services, data_root, release)
    operations = SystemServiceOperations(data_root, release)
    assert operations.state("backend") == "absent"
    assert operations.state("ollama") == "absent"


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
    request: pytest.FixtureRequest,
) -> None:
    playwright = ROOT / "extension/node_modules/@playwright/test"
    chrome = Path("/Applications/Google Chrome.app")
    node = shutil.which("node")
    local_node = Path.home() / ".local/bin/node/bin/node"
    if node is None and local_node.is_file():
        node = str(local_node)
    if not playwright.is_dir() or not chrome.is_dir() or node is None:
        pytest.skip("Node, Playwright, or stable Google Chrome is not installed")

    _require_production_acceptance_ports()
    data_root, release, token_path, secret, publisher = _published_production_fixture(tmp_path)
    stable_extension = data_root / "extension"

    production_services = SystemPublicationServices(data_root, release)
    activation_handle = production_services.start_precommit()
    request.addfinalizer(
        lambda: _cleanup_production_services(production_services, data_root, release)
    )
    production_services.activate(activation_handle)
    assert production_services.healthy()
    backend_record = json.loads((data_root / "runtime/backend.pid").read_text(encoding="utf-8"))
    backend_command = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(backend_record["service"]["pid"])],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert backend_command.returncode == 0
    assert "-m lvt.main" in backend_command.stdout
    production_capabilities = _read_production_capabilities(secret)
    assert production_capabilities["ffmpeg"].get("status") == "available", production_capabilities
    assert production_capabilities["ollama"].get("status") == "available", production_capabilities
    assert production_capabilities["checked_at"] != "2026-08-26T00:00:00+00:00"
    profile = tmp_path / "chromium-profile"
    script = textwrap.dedent(
        """
        import { execFileSync, spawn } from "node:child_process";
        import { closeSync, openSync } from "node:fs";
        import { mkdir, readFile, watch, writeFile } from "node:fs/promises";
        import { chromium } from "@playwright/test";

        const stablePath = process.argv[1];
        const profile = process.argv[2];
        const tokenPath = process.argv[3];
        const chromePath = process.argv[4];
        const token = (await readFile(tokenPath, "utf8")).trim();
        const mark = (stage) =>
          writeFile(`${profile}/acceptance-progress`, stage, {
            encoding: "ascii",
            mode: 0o600,
          });
        const names = [
          "ffmpeg",
          "ollama",
          "asr_package",
          "asr_model",
          "diarization",
          "translation_primary",
          "translation_fallback",
        ];
        let context;
        let browser;
        let browserCdp;
        let browserPid;
        let browserPgid;
        let chromeProcess;
        let chromeLog;
        try {
          await mkdir(profile, { recursive: true });
          chromeLog = openSync(`${profile}/chrome.log`, "a", 0o600);
          chromeProcess = spawn(chromePath, [
            `--user-data-dir=${profile}`,
            "--remote-debugging-port=0",
            "--enable-unsafe-extension-debugging",
            "--disable-crash-reporter",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
          ], {
            detached: true,
            stdio: ["ignore", chromeLog, chromeLog],
          });
          const portFile = `${profile}/DevToolsActivePort`;
          let portContents;
          try {
            portContents = await readFile(portFile, "utf8");
          } catch {
            const portWatcher = watch(profile);
            try {
              for await (const event of portWatcher) {
                if (event.filename !== "DevToolsActivePort") continue;
                try {
                  portContents = await readFile(portFile, "utf8");
                  break;
                } catch {
                  continue;
                }
              }
            } finally {
              await portWatcher.return();
            }
          }
          if (!portContents) {
            throw new Error("Chrome did not publish DevToolsActivePort");
          }
          const port = portContents.split("\\n", 1)[0];
          browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
          context = browser.contexts()[0];
          browserCdp = await browser.newBrowserCDPSession();
          await browserCdp.send("Target.setDiscoverTargets", {
            discover: true,
          });
          const processRows = execFileSync(
            "/bin/ps",
            ["-axo", "pid=,pgid=,command="],
            { encoding: "utf8" },
          ).split("\\n").filter(
            (row) =>
              row.includes(chromePath) &&
              row.includes(`--user-data-dir=${profile}`) &&
              !row.includes("--type="),
          );
          if (processRows.length !== 1) {
            throw new Error(`browser process unavailable: ${processRows.length}`);
          }
          [browserPid, browserPgid] = processRows[0].trim().split(/\\s+/, 2).map(Number);
          await writeFile(
            `${profile}/browser-process.json`,
            JSON.stringify({ pid: browserPid, pgid: browserPgid }),
            { encoding: "ascii", mode: 0o600 },
          );
          let seedPage = context.pages()[0];
          const loaded = await browserCdp.send("Extensions.loadUnpacked", {
            path: stablePath,
          });
          await mark("loaded");
          const workerUrl = `chrome-extension://${loaded.id}/background.js`;
          const existingWorker = context.serviceWorkers().find(
            (worker) => worker.url() === workerUrl,
          );
          const workerCreated = existingWorker
            ? Promise.resolve(existingWorker)
            : context.waitForEvent("serviceworker", {
                predicate: (worker) => worker.url() === workerUrl,
              });
          await seedPage.close();
          seedPage = await context.newPage();
          await seedPage.goto("http://127.0.0.1:8765/health");
          const tabs = await browserCdp.send("Target.getTargets", {
            filter: [{ type: "tab" }],
          });
          const hostTab = tabs.targetInfos.find(
            (target) => target.url === "http://127.0.0.1:8765/health",
          );
          if (!hostTab) {
            throw new Error(`unexpected tab targets: ${JSON.stringify(tabs)}`);
          }
          const extensionUrl = `chrome-extension://${loaded.id}/sidepanel.html`;
          let resolveSidePanel;
          let rejectSidePanel;
          const sidePanelCreated = new Promise((resolve, reject) => {
            resolveSidePanel = resolve;
            rejectSidePanel = reject;
          });
          const observeTarget = ({ targetInfo }) => {
            if (targetInfo.url === extensionUrl) {
              resolveSidePanel(targetInfo);
            }
          };
          browserCdp.on("Target.targetCreated", observeTarget);
          browserCdp.on("Target.targetInfoChanged", observeTarget);
          void browserCdp.send("Extensions.triggerAction", {
            id: loaded.id,
            targetId: hostTab.targetId,
          }).catch((error) => {
            rejectSidePanel(error);
          });
          await mark("triggered");
          const worker = await workerCreated;
          await worker.evaluate(
            async ({ port, token }) => {
              await chrome.storage.local.set({
                lvtConnection: { port, token },
              });
            },
            { port: 8765, token },
          );
          void browserCdp.send("Extensions.triggerAction", {
            id: loaded.id,
            targetId: hostTab.targetId,
          }).catch((error) => {
            rejectSidePanel(error);
          });
          const sidePanel = await sidePanelCreated;
          await mark("panel");
          const attached = await browserCdp.send("Target.attachToTarget", {
            targetId: sidePanel.targetId,
            flatten: false,
          });
          let commandId = 0;
          const commands = new Map();
          const eventWaiters = new Map();
          const authenticatedPaths = new Set();
          const consoleMessages = [];
          browserCdp.on("Target.receivedMessageFromTarget", (event) => {
            if (event.sessionId !== attached.sessionId) return;
            const message = JSON.parse(event.message);
            if (message.id !== undefined) {
              const command = commands.get(message.id);
              if (!command) return;
              commands.delete(message.id);
              if (message.error) command.reject(new Error(message.error.message));
              else command.resolve(message.result);
              return;
            }
            if (message.method === "Runtime.consoleAPICalled") {
              consoleMessages.push(
                message.params.args.map((argument) => argument.value ?? "").join(" "),
              );
            }
            if (message.method === "Network.requestWillBeSent") {
              const request = message.params.request;
              const url = new URL(request.url);
              if (url.hostname === "127.0.0.1" && url.pathname !== "/health") {
                const header = Object.entries(request.headers).find(
                  ([name]) => name.toLowerCase() === "x-lvt-token",
                );
                if (header?.[1] !== token) {
                  throw new Error(`missing authentication for ${url.pathname}`);
                }
                authenticatedPaths.add(url.pathname);
              }
              if (request.url.includes(token)) {
                throw new Error("token leaked into request URL");
              }
            }
            const waiters = eventWaiters.get(message.method) ?? [];
            eventWaiters.delete(message.method);
            for (const resolve of waiters) {
              resolve(message.params);
            }
          });
          const sendTarget = (method, params = {}) =>
            new Promise((resolve, reject) => {
              const id = ++commandId;
              commands.set(id, { resolve, reject });
              browserCdp.send("Target.sendMessageToTarget", {
                sessionId: attached.sessionId,
                message: JSON.stringify({ id, method, params }),
              }).catch(reject);
            });
          const waitForEvent = (method) =>
            new Promise((resolve) => {
              const waiters = eventWaiters.get(method) ?? [];
              waiters.push(resolve);
              eventWaiters.set(method, waiters);
            });
          await mark("storage");
          const evaluate = async (expression) => {
            const result = await sendTarget("Runtime.evaluate", {
              expression,
              awaitPromise: true,
              returnByValue: true,
            });
            if (result.exceptionDetails) {
              throw new Error(
                result.exceptionDetails.exception?.description ??
                JSON.stringify(result.exceptionDetails),
              );
            }
            return result.result.value;
          };
          await sendTarget("Runtime.enable");
          await sendTarget("Network.enable");
          await sendTarget("Page.enable");
          const loadedPage = waitForEvent("Page.loadEventFired");
          await sendTarget("Page.reload");
          await loadedPage;
          await evaluate(`new Promise((resolve) => {
            const ready = () =>
              document.querySelector("#connection-status")?.textContent ===
              "本地服务连接正常";
            if (ready()) {
              resolve();
              return;
            }
            const observer = new MutationObserver(() => {
              if (ready()) {
                observer.disconnect();
                resolve();
              }
            });
            observer.observe(document.documentElement, {
              childList: true,
              subtree: true,
              characterData: true,
            });
          })`);
          await mark("ready");

          const rows = await evaluate(
            `[...document.querySelectorAll(".capability-row")]
              .map((element) => element.getAttribute("data-capability"))`,
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
          const pageUrl = await evaluate("location.href");
          if (pageUrl.includes(token)) {
            throw new Error("token leaked into extension URL");
          }
          if ((await evaluate("document.body.innerText")).includes(token)) {
            throw new Error("token leaked into extension DOM");
          }
          if (consoleMessages.join("\\n").includes(token)) {
            throw new Error("token leaked into browser console");
          }
          if ((await evaluate(
            'document.querySelector("#connection-token").value',
          )) !== "") {
            throw new Error("token remained in the visible input");
          }
          await writeFile(
            `${profile}/acceptance-result.json`,
            JSON.stringify({ ok: true }),
            { encoding: "ascii", mode: 0o600 },
          );
        } finally {
          void browser?.close().catch(() => {});
          if (chromeLog !== undefined) closeSync(chromeLog);
        }
        process.exit(0);
        """
    )

    browser_test = subprocess.Popen(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(stable_extension),
            str(profile),
            str(token_path),
            str(chrome / "Contents/MacOS/Google Chrome"),
        ],
        cwd=ROOT / "extension",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    node_snapshot = _snapshot(browser_test.pid)
    result_marker = profile / "acceptance-result.json"
    deadline = time.monotonic() + 60
    completion = threading.Event()
    process_transcript = ""
    try:
        while browser_test.poll() is None and not result_marker.is_file():
            if time.monotonic() >= deadline:
                raise AssertionError("Chrome acceptance did not reach its result marker")
            completion.wait(0.02)
        if result_marker.is_file() and browser_test.poll() is None:
            browser_identity = json.loads(
                (profile / "browser-process.json").read_text(encoding="ascii")
            )
            process_audit = subprocess.run(
                [
                    "/bin/ps",
                    "-eww",
                    "-o",
                    "command=",
                    "-p",
                    f"{browser_test.pid},{browser_identity['pid']}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            process_transcript = process_audit.stdout + process_audit.stderr
            assert node_snapshot is not None
            if browser_test.poll() is None and not _signal_snapshot(
                node_snapshot,
                signal.SIGKILL,
            ):
                assert browser_test.poll() is not None
    finally:
        if browser_test.poll() is None:
            assert node_snapshot is not None
            assert _signal_snapshot(node_snapshot, signal.SIGKILL)
        stdout, stderr = browser_test.communicate(timeout=10)
        _cleanup_chrome_group(profile)
        _cleanup_production_services(production_services, data_root, release)

    transcript = stdout + stderr
    assert result_marker.is_file(), transcript
    result = json.loads(result_marker.read_text(encoding="ascii"))
    assert result == {"ok": True}
    assert browser_test.returncode in {0, -signal.SIGKILL}, transcript
    operations = SystemServiceOperations(data_root, release)
    assert operations.state("backend") == "absent"
    assert operations.state("ollama") == "absent"
    assert secret not in transcript
    assert secret not in process_transcript
    assert secret not in (profile / "chrome.log").read_text(encoding="utf-8", errors="replace")
    assert secret not in "\n".join(
        path.read_text(encoding="utf-8") for path in publisher.journal.root.glob("slot-*.json")
    )
    assert all(
        secret.encode("ascii") not in path.read_bytes()
        for path in stable_extension.rglob("*")
        if path.is_file()
    )
    assert all(
        path == token_path or secret.encode("ascii") not in path.read_bytes()
        for path in data_root.rglob("*")
        if path.is_file()
    )
    assert token_path.stat().st_mode & 0o777 == 0o600
    process = subprocess.run(
        ["/bin/ps", "-eww", "-o", "command=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert secret not in process.stdout + process.stderr

#!/usr/bin/env python3
"""Run the Phase 2 final acceptance against a real Uvicorn worker service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

import srt
import webvtt

ACTIVE_STATUSES = {
    "downloading",
    "extracting",
    "transcribing",
    "diarizing",
    "segmenting",
    "translating",
    "exporting",
    "cancelling",
}
EXPECTED_ARTIFACT_KINDS = {
    "source.json",
    "source.srt",
    "source.txt",
    "source.vtt",
    "zh-CN.json",
    "zh-CN.srt",
    "zh-CN.txt",
    "zh-CN.vtt",
}
IMMUTABLE_SEGMENT_FIELDS = (
    "id",
    "start_ms",
    "end_ms",
    "speaker",
    "source_language",
    "source_text",
    "metadata",
)
SAMPLES = (
    ("english", "English Single.mp4"),
    ("russian", "Русский single.mp4"),
    ("two_speakers", "中文 双人 video.mp4"),
    ("same_title_a", "same-a/Same Title.mp4"),
    ("same_title_b", "same-b/Same Title.mp4"),
)
JOB_OPTIONS = {
    "asr_model": "mlx-community/whisper-tiny",
    "translate_to": "zh-CN",
    "diarization": True,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class DownloadGate:
    source: Path
    started: threading.Event
    release: threading.Event


class MediaState:
    def __init__(self, assets_root: Path) -> None:
        self.assets_root = assets_root
        self.gates: dict[str, DownloadGate] = {}
        self.flaky_enabled = threading.Event()

    def add_gate(self, path: str, source: Path) -> DownloadGate:
        gate = DownloadGate(
            source=source,
            started=threading.Event(),
            release=threading.Event(),
        )
        self.gates[path] = gate
        return gate

    def resolve_static(self, request_path: str) -> Path | None:
        prefix = "/media/"
        if not request_path.startswith(prefix):
            return None
        relative = Path(unquote(request_path.removeprefix(prefix)))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = self.assets_root.joinpath(*relative.parts)
        try:
            candidate.relative_to(self.assets_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


class ControlledMediaServer:
    def __init__(self, state: MediaState) -> None:
        self.state = state
        self.server = self._build_server()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="phase2-acceptance-media",
            daemon=False,
        )

    def _build_server(self) -> ThreadingHTTPServer:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:
                self._dispatch(send_body=False)

            def do_GET(self) -> None:
                self._dispatch(send_body=True)

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _dispatch(self, *, send_body: bool) -> None:
                request_path = urlsplit(self.path).path
                if request_path == "/controlled/flaky.mp4":
                    if not state.flaky_enabled.is_set():
                        self.send_error(503, "controlled retry failure")
                        return
                    self._serve_file(
                        state.assets_root / "English Single.mp4",
                        send_body=send_body,
                    )
                    return
                gate = state.gates.get(request_path)
                if gate is not None:
                    if not send_body:
                        self._serve_file(gate.source, send_body=False)
                        return
                    self._send_file_headers(gate.source)
                    gate.started.set()
                    if not gate.release.wait(timeout=120):
                        return
                    self._write_file(gate.source)
                    return
                source = state.resolve_static(request_path)
                if source is None:
                    self.send_error(404)
                    return
                self._serve_file(source, send_body=send_body)

            def _serve_file(self, source: Path, *, send_body: bool) -> None:
                self._send_file_headers(source)
                if send_body:
                    self._write_file(source)

            def _send_file_headers(self, source: Path) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(source.stat().st_size))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()

            def _write_file(self, source: Path) -> None:
                try:
                    with source.open("rb") as handle:
                        while chunk := handle.read(64 * 1024):
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        for gate in self.state.gates.values():
            gate.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("media server did not stop")


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-LVT-Token": token}

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, bytes, dict[str, str]]:
        data = None
        headers = dict(self.headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                body = response.read()
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            status = exc.code
            body = exc.read()
            response_headers = dict(exc.headers.items())
        if status not in expected:
            raise AssertionError(
                f"{method} {path} returned {status}, expected {expected}: "
                f"{body.decode('utf-8', errors='replace')}"
            )
        return status, body, response_headers

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any] | list[dict[str, Any]]:
        _, body, _ = self.request(method, path, payload=payload, expected=expected)
        return json.loads(body)

    def submit(self, urls: list[str]) -> list[dict[str, Any]]:
        response = self.json(
            "POST",
            "/api/v1/jobs",
            payload={"urls": urls, "options": JOB_OPTIONS},
        )
        assert isinstance(response, dict)
        assert response["rejected"] == []
        accepted = response["accepted"]
        assert isinstance(accepted, list) and len(accepted) == len(urls)
        return accepted

    def job(self, job_id: str) -> dict[str, Any]:
        response = self.json("GET", f"/api/v1/jobs/{job_id}")
        assert isinstance(response, dict)
        return response

    def events(self, job_id: str) -> list[dict[str, Any]]:
        response = self.json("GET", f"/api/v1/jobs/{job_id}/events?limit=100")
        assert isinstance(response, dict)
        items = response["items"]
        assert isinstance(items, list)
        return items

    def wait_for_status(
        self,
        job_id: str,
        expected: set[str],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.job(job_id)
            if last["status"] in expected:
                return last
            time.sleep(0.2)
        raise TimeoutError(f"job {job_id} did not reach {expected}; last={last}")


class UvicornService:
    def __init__(
        self,
        *,
        python: Path,
        backend_root: Path,
        data_root: Path,
        token: str,
        port: int,
        logs_root: Path,
    ) -> None:
        self.python = python
        self.backend_root = backend_root
        self.data_root = data_root
        self.token = token
        self.port = port
        self.logs_root = logs_root
        self.base_url = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self.starts: list[dict[str, Any]] = []

    def start(self) -> ApiClient:
        if self.process is not None:
            raise RuntimeError("Uvicorn service is already running")
        sequence = len(self.starts) + 1
        log_path = self.logs_root / f"uvicorn-{sequence}.log"
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._log_handle = log_path.open("wb")
        env = os.environ.copy()
        env.update(
            {
                "LVT_DATA_ROOT": os.fspath(self.data_root),
                "LVT_HOST": "127.0.0.1",
                "LVT_PORT": str(self.port),
                "LVT_TOKEN": self.token,
                "LVT_WORKER_CONCURRENCY": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = [os.fspath(self.python), "-m", "lvt.main"]
        started_at = utc_now()
        self.process = subprocess.Popen(
            command,
            cwd=self.backend_root,
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        client = ApiClient(self.base_url, self.token)
        deadline = time.monotonic() + 60
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                status, _, _ = client.request("GET", "/health", expected=(200,))
                if status == 200:
                    self.starts.append(
                        {
                            "sequence": sequence,
                            "pid": self.process.pid,
                            "started_at": started_at,
                            "command": command,
                            "log": log_path.name,
                        }
                    )
                    return client
            except (AssertionError, URLError) as exc:
                last_error = exc
            time.sleep(0.2)
        return_code = self.process.poll()
        self.stop()
        raise RuntimeError(
            f"Uvicorn failed to become healthy; return_code={return_code}; last_error={last_error}"
        )

    def stop(self) -> int | None:
        process = self.process
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        return_code = process.returncode
        if self.starts:
            self.starts[-1]["stopped_at"] = utc_now()
            self.starts[-1]["return_code"] = return_code
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return return_code


def media_url(base_url: str, relative_path: str) -> str:
    encoded = "/".join(quote(part) for part in relative_path.split("/"))
    return f"{base_url}/media/{encoded}"


def assert_active_count(client: ApiClient, job_ids: list[str], expected: int) -> list[str]:
    active = [job_id for job_id in job_ids if client.job(job_id)["status"] in ACTIVE_STATUSES]
    assert len(active) == expected, active
    return active


def cancel_jobs(client: ApiClient, job_ids: list[str]) -> list[dict[str, Any]]:
    results = []
    for job_id in job_ids:
        response = client.json("POST", f"/api/v1/jobs/{job_id}/cancel")
        assert isinstance(response, dict)
        results.append(response)
    return [client.wait_for_status(job_id, {"cancelled"}, timeout=30) for job_id in job_ids]


def verify_artifacts(
    client: ApiClient,
    *,
    job: dict[str, Any],
    label: str,
    downloads_root: Path,
) -> dict[str, Any]:
    job_id = str(job["uuid"])
    response = client.json("GET", f"/api/v1/jobs/{job_id}/artifacts")
    assert isinstance(response, dict)
    items = response["items"]
    assert isinstance(items, list)
    kinds = {str(item["kind"]) for item in items}
    assert kinds == EXPECTED_ARTIFACT_KINDS

    output_dir = downloads_root / label
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts: list[dict[str, Any]] = []
    for item in items:
        kind = str(item["kind"])
        _, content, headers = client.request("GET", str(item["download_url"]))
        assert content
        destination = output_dir / kind
        destination.write_bytes(content)
        artifacts.append(
            {
                "id": item["id"],
                "kind": kind,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_disposition": headers.get("Content-Disposition"),
            }
        )

    source = json.loads((output_dir / "source.json").read_text(encoding="utf-8"))
    translated = json.loads((output_dir / "zh-CN.json").read_text(encoding="utf-8"))
    source_segments = source["segments"]
    translated_segments = translated["segments"]
    assert len(source_segments) == len(translated_segments) > 0
    assert [segment["id"] for segment in source_segments] == list(
        range(1, len(source_segments) + 1)
    )
    assert [segment["start_ms"] for segment in source_segments] == sorted(
        segment["start_ms"] for segment in source_segments
    )
    for source_segment, translated_segment in zip(
        source_segments,
        translated_segments,
        strict=True,
    ):
        assert all(
            source_segment[field] == translated_segment[field] for field in IMMUTABLE_SEGMENT_FIELDS
        )
        assert source_segment["start_ms"] < source_segment["end_ms"]
        assert source_segment["end_ms"] <= source["duration_ms"]
        assert source_segment["speaker"].startswith("Speaker ")
        assert source_segment["source_language"] == source["detected_language"]
        assert source_segment["translated_text"] == ""
        assert translated_segment["translated_text"].strip()

    source_srt = list(srt.parse((output_dir / "source.srt").read_text(encoding="utf-8")))
    translated_srt = list(srt.parse((output_dir / "zh-CN.srt").read_text(encoding="utf-8")))
    assert len(source_srt) == len(translated_srt) == len(source_segments)
    assert [(cue.index, cue.start, cue.end) for cue in source_srt] == [
        (cue.index, cue.start, cue.end) for cue in translated_srt
    ]
    assert [cue.content.split(":", 1)[0] for cue in source_srt] == [
        cue.content.split(":", 1)[0] for cue in translated_srt
    ]

    source_vtt = webvtt.read(os.fspath(output_dir / "source.vtt"))
    translated_vtt = webvtt.read(os.fspath(output_dir / "zh-CN.vtt"))
    assert len(source_vtt.captions) == len(translated_vtt.captions) == len(source_segments)
    assert [(cue.start, cue.end) for cue in source_vtt.captions] == [
        (cue.start, cue.end) for cue in translated_vtt.captions
    ]
    assert (output_dir / "source.txt").read_text(encoding="utf-8").strip()
    assert (output_dir / "zh-CN.txt").read_text(encoding="utf-8").strip()

    events = client.events(job_id)
    return {
        "label": label,
        "job_id": job_id,
        "title": source["title"],
        "duration_ms": source["duration_ms"],
        "detected_language": source["detected_language"],
        "segment_count": len(source_segments),
        "speakers": sorted({segment["speaker"] for segment in source_segments}),
        "engine_versions": source["engine_versions"],
        "warnings": source["warnings"],
        "execution_count_total": job["execution_count_total"],
        "retry_cycle": job["retry_cycle"],
        "automatic_requeue_count_in_cycle": job["automatic_requeue_count_in_cycle"],
        "events": events,
        "artifacts": sorted(artifacts, key=lambda item: str(item["kind"])),
        "invariants": {
            "segment_count_equal": True,
            "ids_contiguous": True,
            "timestamps_equal": True,
            "speakers_equal": True,
            "source_language_equal": True,
            "source_text_equal": True,
            "metadata_equal": True,
            "order_equal": True,
            "source_text_preserved": True,
        },
    }


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise FileExistsError(f"acceptance output already exists: {output_root}")
    output_root.mkdir(parents=True)
    data_root = output_root / "data"
    downloads_root = output_root / "downloads"
    assets_root = project_root / "test-assets" / "generated"
    python = project_root / ".venv-smoke" / "bin" / "python"
    backend_root = project_root / "backend"
    token = "phase2-checkpoint8-local-token"

    media_state = MediaState(assets_root)
    media_server = ControlledMediaServer(media_state)
    uvicorn = UvicornService(
        python=python,
        backend_root=backend_root,
        data_root=data_root,
        token=token,
        port=free_port(),
        logs_root=output_root / "logs",
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "checkpoint": "phase-2-checkpoint-8",
        "started_at": utc_now(),
        "status": "running",
        "environment": {
            "python": sys.version,
            "platform": sys.platform,
            "worker_process_model": "single-uvicorn-process",
            "asr_model": JOB_OPTIONS["asr_model"],
            "ollama_url": args.ollama_url,
        },
        "scenarios": {},
        "samples": [],
    }
    client: ApiClient | None = None
    try:
        with urlopen(f"{args.ollama_url.rstrip('/')}/api/tags", timeout=10) as response:
            ollama_tags = json.loads(response.read())
        model_names = {model["name"] for model in ollama_tags["models"]}
        assert {"hy-mt2:1.8b-q4km-fixed", "qwen2.5:1.5b"} <= model_names
        report["environment"]["ollama_models"] = sorted(model_names)

        media_server.start()
        client = uvicorn.start()

        one_a = media_state.add_gate(
            "/controlled/concurrency-one-a.mp4",
            assets_root / "English Single.mp4",
        )
        one_b = media_state.add_gate(
            "/controlled/concurrency-one-b.mp4",
            assets_root / "English Single.mp4",
        )
        concurrency_one_jobs = client.submit(
            [
                f"{media_server.base_url}/controlled/concurrency-one-a.mp4",
                f"{media_server.base_url}/controlled/concurrency-one-b.mp4",
            ]
        )
        concurrency_one_ids = [str(job["uuid"]) for job in concurrency_one_jobs]
        assert one_a.started.wait(timeout=30) or one_b.started.wait(timeout=30)
        active_one = assert_active_count(client, concurrency_one_ids, 1)
        assert sum(client.job(job_id)["status"] == "queued" for job_id in concurrency_one_ids) == 1
        cancelled_one = cancel_jobs(client, concurrency_one_ids)
        one_a.release.set()
        one_b.release.set()
        report["scenarios"]["concurrency_1"] = {
            "configured": 1,
            "observed_active": len(active_one),
            "job_ids": concurrency_one_ids,
            "final_statuses": [job["status"] for job in cancelled_one],
        }

        settings_two = client.json(
            "PATCH",
            "/api/v1/settings",
            payload={"worker_concurrency": 2},
        )
        two_a = media_state.add_gate(
            "/controlled/concurrency-two-a.mp4",
            assets_root / "English Single.mp4",
        )
        two_b = media_state.add_gate(
            "/controlled/concurrency-two-b.mp4",
            assets_root / "English Single.mp4",
        )
        concurrency_two_jobs = client.submit(
            [
                f"{media_server.base_url}/controlled/concurrency-two-a.mp4",
                f"{media_server.base_url}/controlled/concurrency-two-b.mp4",
            ]
        )
        concurrency_two_ids = [str(job["uuid"]) for job in concurrency_two_jobs]
        assert two_a.started.wait(timeout=30)
        assert two_b.started.wait(timeout=30)
        active_two = assert_active_count(client, concurrency_two_ids, 2)
        cancelled_two = cancel_jobs(client, concurrency_two_ids)
        two_a.release.set()
        two_b.release.set()
        report["scenarios"]["concurrency_2"] = {
            "settings_response": settings_two,
            "configured": 2,
            "observed_active": len(active_two),
            "job_ids": concurrency_two_ids,
            "final_statuses": [job["status"] for job in cancelled_two],
        }
        report["scenarios"]["cancel"] = {
            "job_ids": concurrency_one_ids + concurrency_two_ids,
            "all_cancelled": True,
            "events": {
                job_id: client.events(job_id)
                for job_id in concurrency_one_ids + concurrency_two_ids
            },
        }

        client.json(
            "PATCH",
            "/api/v1/settings",
            payload={"worker_concurrency": 1},
        )
        recovery_gate = media_state.add_gate(
            "/controlled/recovery.mp4",
            assets_root / "English Single.mp4",
        )
        recovery_job = client.submit([f"{media_server.base_url}/controlled/recovery.mp4"])[0]
        recovery_id = str(recovery_job["uuid"])
        assert recovery_gate.started.wait(timeout=30)
        before_restart = client.wait_for_status(
            recovery_id,
            ACTIVE_STATUSES,
            timeout=30,
        )
        first_stop_code = uvicorn.stop()
        recovery_gate.release.set()
        client = uvicorn.start()
        recovered = client.wait_for_status(recovery_id, {"completed"}, timeout=600)
        recovery_events = client.events(recovery_id)
        interrupted_events = [
            event for event in recovery_events if event["status"] == "interrupted"
        ]
        assert len(interrupted_events) == 1
        assert recovered["execution_count_total"] == 2
        report["scenarios"]["recovery"] = {
            "job_id": recovery_id,
            "before_restart_status": before_restart["status"],
            "first_service_stop_return_code": first_stop_code,
            "final_status": recovered["status"],
            "execution_count_total": recovered["execution_count_total"],
            "interrupted_event_count": len(interrupted_events),
            "events": recovery_events,
        }

        client.json(
            "PATCH",
            "/api/v1/settings",
            payload={"worker_concurrency": 2},
        )
        english_url = media_url(media_server.base_url, "English Single.mp4")
        flaky_url = f"{media_server.base_url}/controlled/flaky.mp4"
        batch_jobs = client.submit([english_url, flaky_url])
        english_id = str(batch_jobs[0]["uuid"])
        flaky_id = str(batch_jobs[1]["uuid"])
        english_job = client.wait_for_status(english_id, {"completed"}, timeout=600)
        failed_job = client.wait_for_status(flaky_id, {"failed"}, timeout=180)
        assert failed_job["execution_count_total"] == 3
        assert failed_job["automatic_requeue_count_in_cycle"] == 2
        assert failed_job["error_code"] == "DOWNLOAD_FAILED"
        media_state.flaky_enabled.set()
        retry_response = client.json("POST", f"/api/v1/jobs/{flaky_id}/retry")
        assert isinstance(retry_response, dict)
        retried_job = client.wait_for_status(flaky_id, {"completed"}, timeout=600)
        assert retried_job["retry_cycle"] == 1
        assert retried_job["execution_count_total"] == 4
        report["scenarios"]["batch_retry"] = {
            "submitted_together": [english_id, flaky_id],
            "initial_results": {
                english_id: english_job["status"],
                flaky_id: failed_job["status"],
            },
            "failure_error_code": failed_job["error_code"],
            "automatic_requeues": failed_job["automatic_requeue_count_in_cycle"],
            "execution_count_before_manual_retry": failed_job["execution_count_total"],
            "manual_retry_response_status": retry_response["status"],
            "final_status": retried_job["status"],
            "final_retry_cycle": retried_job["retry_cycle"],
            "final_execution_count_total": retried_job["execution_count_total"],
            "events": client.events(flaky_id),
        }

        client.json(
            "PATCH",
            "/api/v1/settings",
            payload={"worker_concurrency": 1},
        )
        sample_jobs: dict[str, dict[str, Any]] = {"english": english_job}
        remaining_urls = [
            media_url(media_server.base_url, relative_path)
            for label, relative_path in SAMPLES
            if label != "english"
        ]
        remaining = client.submit(remaining_urls)
        for (label, _), submitted in zip(SAMPLES[1:], remaining, strict=True):
            sample_jobs[label] = client.wait_for_status(
                str(submitted["uuid"]),
                {"completed"},
                timeout=900,
            )

        for label, _ in SAMPLES:
            report["samples"].append(
                verify_artifacts(
                    client,
                    job=sample_jobs[label],
                    label=label,
                    downloads_root=downloads_root,
                )
            )

        same_a = sample_jobs["same_title_a"]
        same_b = sample_jobs["same_title_b"]
        assert same_a["title"] == same_b["title"] == "Same Title"
        relative_roots = [
            f"work/{same_a['uuid']}",
            f"work/{same_b['uuid']}",
        ]
        assert relative_roots[0] != relative_roots[1]
        assert all((data_root / root).is_dir() for root in relative_roots)
        report["scenarios"]["same_title_isolation"] = {
            "title": "Same Title",
            "job_ids": [same_a["uuid"], same_b["uuid"]],
            "relative_job_roots": relative_roots,
            "isolated": True,
        }

        assert len(report["samples"]) == 5
        artifact_count = sum(len(sample["artifacts"]) for sample in report["samples"])
        assert artifact_count == 40
        report["summary"] = {
            "real_api_worker_samples_completed": 5,
            "artifacts_downloaded_and_read_back": artifact_count,
            "all_segment_invariants_passed": True,
            "concurrency_values_verified": [1, 2],
            "automatic_retry_budget_verified": 2,
            "manual_retry_verified": True,
            "cancel_verified": True,
            "restart_recovery_verified": True,
            "batch_mixed_outcome_then_retry_verified": True,
        }
        report["status"] = "passed"
        return report
    finally:
        stop_code = uvicorn.stop()
        if stop_code is not None:
            report["final_service_stop_return_code"] = stop_code
        report["uvicorn_starts"] = uvicorn.starts
        media_server.stop()
        report["finished_at"] = utc_now()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    report: dict[str, Any]
    exit_code = 0
    try:
        report = run_acceptance(args)
    except BaseException as exc:
        report = {
            "schema_version": 1,
            "checkpoint": "phase-2-checkpoint-8",
            "status": "failed",
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        exit_code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

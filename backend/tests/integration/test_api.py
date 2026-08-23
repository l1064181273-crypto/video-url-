from pathlib import Path

from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.models import DEFAULT_ASR_MODEL


def test_batch_create_accepts_valid_and_rejects_invalid_urls(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "lvt.sqlite3", api_token="test-token")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/jobs").status_code == 401

    response = client.post(
        "/api/v1/jobs",
        headers={"X-LVT-Token": "test-token"},
        json={
            "urls": [
                "https://example.test/video?private=value",
                "file:///tmp/video.mp4",
            ],
            "options": {
                "asr_model": "small",
                "translate_to": "zh-CN",
                "diarization": False,
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["accepted"]) == 1
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["error_code"] == "INVALID_URL"
    assert body["accepted"][0]["sanitized_display_url"] == "https://example.test/video"

    jobs = client.get("/api/v1/jobs", headers={"X-LVT-Token": "test-token"}).json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["options"] == {
        "asr_model": "small",
        "translate_to": "zh-CN",
        "diarization": False,
    }

    restarted = TestClient(create_app(db_path=tmp_path / "lvt.sqlite3", api_token="test-token"))
    restored = restarted.get(
        f"/api/v1/jobs/{jobs[0]['uuid']}",
        headers={"X-LVT-Token": "test-token"},
    ).json()
    assert restored["options"] == jobs[0]["options"]


def test_api_default_job_persists_canonical_asr_model(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "default.sqlite3", api_token="test-token"))

    response = client.post(
        "/api/v1/jobs",
        headers={"X-LVT-Token": "test-token"},
        json={"urls": ["https://example.test/default"]},
    )

    assert response.status_code == 200
    assert response.json()["accepted"][0]["options"]["asr_model"] == DEFAULT_ASR_MODEL

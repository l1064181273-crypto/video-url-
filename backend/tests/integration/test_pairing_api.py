from pathlib import Path

from fastapi.testclient import TestClient

from lvt.api.app import PACKAGED_EXTENSION_ORIGIN, create_app


def test_packaged_extension_can_pair_without_rendering_the_token(tmp_path: Path) -> None:
    token = "pairing-secret-" + "x" * 48
    app = create_app(db_path=tmp_path / "lvt.sqlite3", api_token=token)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/pairing",
            headers={
                "Origin": PACKAGED_EXTENSION_ORIGIN,
                "X-LVT-Pairing": "1",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"token": token}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "Origin"


def test_pairing_rejects_web_origins_missing_marker_and_preflight(tmp_path: Path) -> None:
    token = "pairing-secret-" + "y" * 48
    app = create_app(db_path=tmp_path / "lvt.sqlite3", api_token=token)

    with TestClient(app) as client:
        attempts = [
            client.post(
                "/api/v1/pairing",
                headers={"Origin": "https://evil.test", "X-LVT-Pairing": "1"},
            ),
            client.post(
                "/api/v1/pairing",
                headers={"Origin": PACKAGED_EXTENSION_ORIGIN},
            ),
            client.post(
                "/api/v1/pairing",
                headers={
                    "Origin": f"{PACKAGED_EXTENSION_ORIGIN}.evil.test",
                    "X-LVT-Pairing": "1",
                },
            ),
            client.options(
                "/api/v1/pairing",
                headers={
                    "Origin": "https://evil.test",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "X-LVT-Pairing",
                },
            ),
        ]

    assert [response.status_code for response in attempts] == [403, 403, 403, 405]
    assert token not in "".join(response.text for response in attempts)
    assert all("access-control-allow-origin" not in response.headers for response in attempts)

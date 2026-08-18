from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.for_data_dir(Path(tmp_path) / "runtime")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def test_subjects_expose_only_person_identity_and_active_state(client):
    response = client.get("/api/subjects")
    assert response.status_code == 200
    subjects = response.json()["subjects"]
    assert [row["display_name"] for row in subjects] == ["木野内栄治", "大川智宏", "江守哲", "千竈 鉄平"]
    assert all(set(row) == {"id", "key", "display_name", "is_active"} for row in subjects)


def test_read_routes_reject_query_expansion_and_noncanonical_ids(client):
    assert client.get("/api/subjects?include=private").status_code == 422
    assert client.get("/api/jobs/01").status_code in {400, 404, 422}


def test_health_declares_loopback_boundary(client):
    assert client.get("/api/health").json() == {
        "status": "ok", "bind_boundary": "127.0.0.1", "authentication": "none"
    }

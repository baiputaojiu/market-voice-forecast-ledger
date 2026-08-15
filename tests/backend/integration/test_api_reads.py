from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    JobKind,
    JobStage,
    PolicyKind,
)
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.services.corrections import (
    ChannelPolicyChange,
    ChannelPolicyCorrectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.for_data_dir(tmp_path / "runtime")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as value:
        yield value


def _create_failed_job(
    settings: Settings, error_code: str = "SYNTHETIC_FAILURE"
) -> int:
    conn = open_database(settings.database_path)
    try:
        service = JobStateService(conn)
        job_id = service.create(
            JobManifest.build(
                JobKind.VIDEO_PIPELINE,
                (
                    ManifestUnit(
                        unit_key="private_input_text_body",
                        stage=JobStage.VIDEO_METADATA,
                        ordinal=1,
                        declared_input_hash="prompt_body_private_hash",
                        dependency_keys=(),
                        execution_contract_hash="local_path_private_contract",
                    ),
                ),
            )
        )
        service.begin_unit(job_id, "private_input_text_body")
        service.fail_unit(job_id, "private_input_text_body", error_code)
        return job_id
    finally:
        conn.close()


def test_health_is_exact_and_interactive_docs_are_disabled(client: TestClient):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "bind_boundary": "127.0.0.1",
        "authentication": "none",
    }
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404

    preflight = client.options(
        "/api/subjects",
        headers={
            "Origin": "https://untrusted.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers


def test_subjects_return_only_stable_public_configuration(client: TestClient):
    response = client.get("/api/subjects")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"subjects"}
    subjects = payload["subjects"]
    assert len(subjects) == 4
    assert [item["id"] for item in subjects] == sorted(
        item["id"] for item in subjects
    )
    assert {item["display_name"] for item in subjects} == {
        "木野内栄治",
        "暁投資顧問",
        "江守哲",
        "大川智宏",
    }
    assert all(
        set(item)
        == {
            "id",
            "key",
            "display_name",
            "subject_kind",
            "is_active",
            "policy_kind",
            "configuration_status",
            "youtube_channel_id",
        }
        for item in subjects
    )
    assert client.get("/api/subjects?ignored=1").status_code == 422


def test_subjects_support_fixed_policy_awaiting_configuration(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        subject_id = int(
            conn.execute(
                "SELECT id FROM analysis_subjects ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        )
        ChannelPolicyCorrectionService(conn).change(
            ChannelPolicyChange(
                subject_id=subject_id,
                policy_kind=PolicyKind.FIXED_CHANNEL,
                configuration_status=(
                    ConfigurationStatus.CONFIGURATION_REQUIRED
                ),
                youtube_channel_id=None,
                channel_display_name=None,
                actor="user",
                reason="API read boundary test",
            )
        )
    finally:
        conn.close()

    response = client.get("/api/subjects")

    assert response.status_code == 200
    subject = next(
        item for item in response.json()["subjects"]
        if item["id"] == subject_id
    )
    assert subject["policy_kind"] == "fixed_channel"
    assert subject["configuration_status"] == "configuration_required"
    assert subject["youtube_channel_id"] is None


def test_subjects_fail_closed_when_a_policy_row_is_missing(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "DELETE FROM subject_channel_policies "
            "WHERE subject_id=(SELECT MAX(id) FROM analysis_subjects)"
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/subjects")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


def test_empty_heatmap_returns_four_subjects_by_four_assets(client: TestClient):
    response = client.get(
        "/api/heatmaps?cutoff=2026-08-14&granularity=week"
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"cutoff", "granularity", "rows"}
    assert payload["cutoff"] == "2026-08-14"
    assert payload["granularity"] == "week"
    assert len(payload["rows"]) == 16
    assert all(row["cells"] == [] for row in payload["rows"])
    assert Counter(row["asset"] for row in payload["rows"]) == {
        "nikkei_225": 4,
        "topix": 4,
        "sp500": 4,
        "xau_usd": 4,
    }
    assert all(
        set(row)
        == {
            "subject_id",
            "subject_key",
            "scope_id",
            "scope_status",
            "stale_reason",
            "asset",
            "cells",
        }
        for row in payload["rows"]
    )


@pytest.mark.parametrize(
    "query",
    (
        "cutoff=2026-8-14&granularity=week",
        "cutoff=2026-02-30&granularity=week",
        "cutoff=2026-08-14T00:00:00Z&granularity=week",
        "cutoff=2026-08-14&granularity=day",
        "cutoff=2026-08-14&granularity=week&unknown=value",
    ),
)
def test_heatmap_query_is_canonical_and_forbids_unknown_fields(
    client: TestClient, query: str
):
    response = client.get(f"/api/heatmaps?{query}")

    assert response.status_code == 422
    assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"


def test_job_read_exposes_progress_and_safe_error_without_private_hashes(
    client: TestClient, settings: Settings
):
    job_id = _create_failed_job(settings)

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "job_id": job_id,
        "kind": "video_pipeline",
        "status": "failed",
        "completed": 0,
        "total": 1,
        "stages": [
            {"stage": "video_metadata", "completed": 0, "total": 1},
            {"stage": "audio_acquisition", "completed": 0, "total": 0},
            {"stage": "transcription", "completed": 0, "total": 0},
            {"stage": "speaker_assignment", "completed": 0, "total": 0},
            {
                "stage": "analysis_input_extraction",
                "completed": 0,
                "total": 0,
            },
            {"stage": "codex_analysis", "completed": 0, "total": 0},
            {"stage": "asset_mapping", "completed": 0, "total": 0},
            {"stage": "heatmap_update", "completed": 0, "total": 0},
        ],
        "units": [
            {
                "stage": "video_metadata",
                "status": "failed",
                "ordinal": 1,
                "error_code": "SYNTHETIC_FAILURE",
            }
        ],
    }
    serialized = response.text
    for private_value in (
        "private_input_text_body",
        "prompt_body_private_hash",
        "local_path_private_contract",
    ):
        assert private_value not in serialized


def test_job_read_accepts_the_domain_safe_error_code_alphabet(
    client: TestClient, settings: Settings
):
    job_id = _create_failed_job(settings, "provider.timeout:v1")

    response = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["units"][0]["error_code"] == "provider.timeout:v1"


@pytest.mark.parametrize(
    "path_id",
    ("0", "-1", "01", "+1", "1.0", "true", "99999999999999999999"),
)
def test_job_path_ids_require_canonical_positive_decimal(
    client: TestClient, path_id: str
):
    assert client.get(f"/api/jobs/{path_id}").status_code == 422


def test_missing_job_is_a_safe_not_found(client: TestClient):
    response = client.get("/api/jobs/999999")

    assert response.status_code == 404
    assert response.json() == {"error": "JOB_NOT_FOUND"}

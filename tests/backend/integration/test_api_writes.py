from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.api.dependencies import PublicReadAdapter
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.domain.enums import AssignmentKind
from tests.backend.integration.test_review_application import (
    _accepted_low_mapping,
    _accepted_unknown,
)
from tests.backend.integration.test_speaker_corrections import _speaker_fixture
from tests.backend.integration.test_text_deletion import _retained_forecast


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.for_data_dir(tmp_path / "runtime")


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as value:
        yield value


def test_mapping_review_uses_atomic_review_application_service(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        private_evidence = "transcript_full_api_mapping_sentinel"
        prepared, _, scope_id = _accepted_low_mapping(conn, private_evidence)
        mapping_id = prepared.mapping_ids[0]
        fixture_subject_id = conn.execute(
            "SELECT subject_id FROM analysis_scopes WHERE id=?",
            (scope_id,),
        ).fetchone()["subject_id"]
        conn.execute(
            "UPDATE analysis_subjects SET is_active=0 "
            "WHERE id=(SELECT MIN(id) FROM analysis_subjects WHERE id!=?)",
            (fixture_subject_id,),
        )
        conn.commit()
        cutoff = conn.execute(
            "SELECT cutoff_day_jst FROM analysis_scopes WHERE id=?",
            (scope_id,),
        ).fetchone()["cutoff_day_jst"]
        before = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
            )
        )
    finally:
        conn.close()

    get_response = client.get(f"/api/mappings/{mapping_id}/reviews")
    assert get_response.status_code == 405
    conn = open_database(settings.database_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM mapping_reviews"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    response = client.post(
        f"/api/mappings/{mapping_id}/reviews",
        json={"decision": "approve", "reason": "Synthetic API approval"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mapping_id"] == mapping_id
    assert payload["applied_to_current"] is True
    assert payload["rebuilt_cell_count"] > 0
    assert payload["current"]["scope_id"] == scope_id
    assert payload["current"]["forecast_count"] == 1
    assert private_evidence not in response.text

    heatmap = client.get(
        f"/api/heatmaps?cutoff={cutoff}&granularity=week"
    )
    assert heatmap.status_code == 200
    populated = [
        cell
        for row in heatmap.json()["rows"]
        if row["scope_id"] == scope_id
        for cell in row["cells"]
    ]
    assert len(populated) == 1
    assert set(populated[0]) == {
        "scope_id",
        "source_run_id",
        "projection_batch_id",
        "period_key",
        "slot_start",
        "slot_end",
        "unknown_period",
        "condition_kind",
        "condition_texts",
        "primary_direction",
        "directions",
        "view_relation",
        "selected_published_at",
        "selected_forecast_basis",
        "mapping_kind",
        "confidence",
        "evidence_count",
        "supporting_statement_ids",
        "counterevidence_statement_ids",
        "source_forecast_ids",
    }
    assert private_evidence not in heatmap.text

    conn = open_database(settings.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM heatmap_cells WHERE scope_id=?", (scope_id,)
        ).fetchone()[0] == payload["rebuilt_cell_count"]
        after = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
            )
        )
        assert after != before
    finally:
        conn.close()


def test_period_review_uses_atomic_review_application_service(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        prepared, _, scope_id = _accepted_unknown(conn, "api-period")
        period_id = prepared.period_ids[0]
    finally:
        conn.close()

    response = client.post(
        f"/api/periods/{period_id}/reviews",
        json={
            "decision": "approve_unknown",
            "reason": "Synthetic API unknown-period approval",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period_id"] == period_id
    assert payload["applied_to_current"] is True
    assert payload["rebuilt_cell_count"] == 2
    assert payload["current"]["scope_id"] == scope_id
    conn = open_database(settings.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM period_reviews").fetchone()[0] == 1
    finally:
        conn.close()


def test_speaker_correction_calls_real_service_and_leaves_results_stale(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        fixture = _speaker_fixture(conn, AssignmentKind.SUBJECT)
    finally:
        conn.close()

    response = client.post(
        f"/api/speakers/{fixture.segment_id}/corrections",
        json={
            "assignment_kind": "hold",
            "assigned_subject_id": None,
            "reason": "Synthetic API correction",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "segment_id": fixture.segment_id,
        "assignment_kind": "hold",
        "assigned_subject_id": None,
        "assignment_origin": "manual",
        "applied": True,
        "stale_scope_count": 1,
    }
    conn = open_database(settings.database_path)
    try:
        assignment = conn.execute(
            "SELECT assignment_kind, assignment_origin FROM speaker_assignments "
            "WHERE segment_id=?",
            (fixture.segment_id,),
        ).fetchone()
        assert tuple(assignment) == ("hold", "manual")
        scope = conn.execute(
            "SELECT status, stale_reason FROM analysis_scopes WHERE id=?",
            (fixture.scope_id,),
        ).fetchone()
        assert tuple(scope) == ("stale", "SPEAKER_ASSIGNMENT_CHANGED")
    finally:
        conn.close()


def test_speaker_correction_read_failure_does_not_escape_atomic_write(
    client: TestClient, settings: Settings, monkeypatch
):
    conn = open_database(settings.database_path)
    try:
        fixture = _speaker_fixture(conn, AssignmentKind.SUBJECT)
        assignment_before = tuple(
            conn.execute(
                "SELECT * FROM speaker_assignments WHERE segment_id=?",
                (fixture.segment_id,),
            ).fetchone()
        )
        scope_before = tuple(
            conn.execute(
                "SELECT * FROM analysis_scopes WHERE id=?",
                (fixture.scope_id,),
            ).fetchone()
        )
        audit_before = conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
    finally:
        conn.close()

    def fail_count(_self, _segment_id):
        raise sqlite3.OperationalError(
            "C:/private/ledger.sqlite3 stale count failure"
        )

    monkeypatch.setattr(
        PublicReadAdapter, "stale_scope_count_for_segment", fail_count
    )

    response = client.post(
        f"/api/speakers/{fixture.segment_id}/corrections",
        json={
            "assignment_kind": "hold",
            "assigned_subject_id": None,
            "reason": "Synthetic rollback check",
        },
    )

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    conn = open_database(settings.database_path)
    try:
        assert tuple(
            conn.execute(
                "SELECT * FROM speaker_assignments WHERE segment_id=?",
                (fixture.segment_id,),
            ).fetchone()
        ) == assignment_before
        assert tuple(
            conn.execute(
                "SELECT * FROM analysis_scopes WHERE id=?",
                (fixture.scope_id,),
            ).fetchone()
        ) == scope_before
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0] == audit_before
    finally:
        conn.close()


def test_retention_requires_exact_preview_token_and_preserves_public_results(
    client: TestClient, settings: Settings, tmp_path
):
    conn = open_database(settings.database_path)
    try:
        fixture = _retained_forecast(conn, tmp_path)
        before = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_forecasts "
                "WHERE scope_id=? ORDER BY analysis_forecast_id",
                (fixture.scope_id,),
            )
        )
    finally:
        conn.close()

    cutoff = "2028-08-16T12:00:00.000000Z"
    preview = client.post("/api/retention/preview", json={"cutoff": cutoff})
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["affected_video_count"] == 1
    assert preview_payload["affected_transcript_count"] == 1
    assert preview_payload["affected_analysis_input_count"] == 1
    assert preview_payload["full_reproduction_will_be_lost"] is True
    assert len(preview_payload["preview_token"]) == 64

    deletion = client.post(
        "/api/retention/delete",
        json={
            "cutoff": cutoff,
            "preview_token": preview_payload["preview_token"],
        },
    )
    assert deletion.status_code == 200
    assert deletion.json()["deleted_transcript_count"] == 1
    assert deletion.json()["deleted_analysis_input_count"] == 1

    replay = client.post(
        "/api/retention/delete",
        json={
            "cutoff": cutoff,
            "preview_token": preview_payload["preview_token"],
        },
    )
    assert replay.status_code == 409
    assert replay.json() == {"error": "DELETION_PREVIEW_NOT_CURRENT"}
    assert preview_payload["preview_token"] not in replay.text

    conn = open_database(settings.database_path)
    try:
        assert conn.execute(
            "SELECT text_body FROM transcript_segments WHERE id=?",
            (fixture.segment_id,),
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT input_text FROM analysis_input_snapshots WHERE run_id=?",
            (fixture.run_id,),
        ).fetchone()[0] is None
        after = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_forecasts "
                "WHERE scope_id=? ORDER BY analysis_forecast_id",
                (fixture.scope_id,),
            )
        )
        assert after == before
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("path", "body"),
    (
        (
            "/api/mappings/1/reviews",
            {"decision": "approve", "reason": "\u3000"},
        ),
        (
            "/api/mappings/1/reviews",
            {
                "decision": "approve",
                "reason": "valid reason",
                "corrected_asset": "topix",
            },
        ),
        (
            "/api/mappings/1/reviews",
            {
                "decision": "correct",
                "reason": "valid reason",
                "corrected_asset": None,
            },
        ),
        (
            "/api/mappings/1/reviews",
            {"decision": "approve", "reason": "valid", "actor": "system"},
        ),
        (
            "/api/mappings/1/reviews?unknown=private",
            {"decision": "approve", "reason": "valid reason"},
        ),
        (
            "/api/mappings/1/reviews",
            {"decision": "approve", "reason": "x" * 257},
        ),
        (
            "/api/periods/1/reviews",
            {"decision": "approve", "reason": "valid reason"},
        ),
        (
            "/api/speakers/1/corrections",
            {
                "assignment_kind": "subject",
                "assigned_subject_id": True,
                "reason": "valid reason",
            },
        ),
        (
            "/api/speakers/1/corrections",
            {
                "assignment_kind": "hold",
                "assigned_subject_id": 1,
                "reason": "valid reason",
            },
        ),
        (
            "/api/speakers/1/corrections",
            {
                "assignment_kind": "subject",
                "assigned_subject_id": "1",
                "reason": "valid reason",
            },
        ),
        (
            "/api/speakers/1/corrections",
            {
                "assignment_kind": "subject",
                "assigned_subject_id": 1.0,
                "reason": "valid reason",
            },
        ),
        (
            "/api/retention/preview",
            {"cutoff": "2028-08-16T12:00:00Z"},
        ),
        (
            "/api/retention/delete",
            {
                "cutoff": "2028-08-16T12:00:00.000000Z",
                "preview_token": 7,
            },
        ),
        (
            "/api/retention/preview",
            {
                "cutoff": "2028-08-16T12:00:00.000000Z",
                "unknown": {"nested": "value"},
            },
        ),
    ),
)
def test_write_models_are_strict_shape_checked_and_actor_is_server_fixed(
    client: TestClient, path: str, body: dict[str, object]
):
    response = client.post(path, json=body)

    assert response.status_code == 422
    assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"


def test_mapping_review_rollback_is_atomic_and_storage_error_is_private(
    client: TestClient, settings: Settings
):
    conn = open_database(settings.database_path)
    try:
        prepared, _, scope_id = _accepted_low_mapping(conn, "api-rollback")
        mapping_id = prepared.mapping_ids[0]
        current_before = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
            )
        )
        heatmap_before = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM heatmap_cells WHERE scope_id=? ORDER BY id", (scope_id,)
            )
        )
        conn.execute(
            """
            CREATE TRIGGER api_force_private_audit_failure
            BEFORE INSERT ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'C:/private/ledger.sqlite3 raw sqlite failure');
            END
            """
        )
    finally:
        conn.close()

    response = client.post(
        f"/api/mappings/{mapping_id}/reviews",
        json={"decision": "approve", "reason": "private reason sentinel"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    assert "private" not in response.text.lower()
    assert "sqlite" not in response.text.lower()
    conn = open_database(settings.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0
        assert tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
            )
        ) == current_before
        assert tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM heatmap_cells WHERE scope_id=? ORDER BY id", (scope_id,)
            )
        ) == heatmap_before
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("path", "body", "error_code"),
    (
        (
            "/api/mappings/999999/reviews",
            {"decision": "approve", "reason": "Missing mapping"},
            "ASSET_MAPPING_NOT_FOUND",
        ),
        (
            "/api/periods/999999/reviews",
            {"decision": "reject", "reason": "Missing period"},
            "PERIOD_NOT_FOUND",
        ),
        (
            "/api/speakers/999999/corrections",
            {
                "assignment_kind": "hold",
                "assigned_subject_id": None,
                "reason": "Missing segment",
            },
            "SPEAKER_CORRECTION_SEGMENT_NOT_FOUND",
        ),
    ),
)
def test_write_routes_classify_missing_entities_as_safe_not_found(
    client: TestClient,
    path: str,
    body: dict[str, object],
    error_code: str,
):
    response = client.post(path, json=body)

    assert response.status_code == 404
    assert response.json() == {"error": error_code}

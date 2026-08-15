import json
import sqlite3
from dataclasses import dataclass

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.codex_contract import (
    CodexContractService,
    CodexRunReceipt,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
    _create_job_for_input,
    _create_subject,
    _prepare_personal_analysis,
)


CODEX_UNIT_KEY = "codex:batch:1"


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class StartedRun:
    id: int
    job_id: int
    running_codex_unit_key: str
    segment_id: int


@pytest.fixture
def started_run(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).begin_unit(prepared.job_id, CODEX_UNIT_KEY)
    segment_id = AnalysisRepository(db).get_input_segments(run.id)[0].segment_id
    return StartedRun(run.id, prepared.job_id, CODEX_UNIT_KEY, segment_id)


@pytest.fixture
def valid_output_payload(started_run):
    return {
        "run_id": started_run.id,
        "batch_key": CODEX_UNIT_KEY,
        "statements": [
            {
                "statement_type": "future_forecast",
                "forecast_basis": "direct",
                "condition_kind": "unconditional",
                "condition_text": None,
                "direction_kind": "up",
                "turning_point_kind": None,
                "target_expression": "Synthetic equity benchmark",
                "period_expression": "Synthetic future period",
                "codex_asset_hints": [],
                "evidence": [
                    {
                        "segment_id": started_run.segment_id,
                        "excerpt": "Synthetic subject evidence.",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def valid_output_json(valid_output_payload):
    return json.dumps(valid_output_payload, ensure_ascii=False, indent=2)


def _valid_receipt() -> CodexRunReceipt:
    return CodexRunReceipt(
        "gpt-5.6-sol", "max", 0, "stored_statements_only"
    )


def test_valid_batch_output_and_unit_success_commit_together(
    db, started_run, valid_output_payload, valid_output_json
):
    result = CodexContractService(db).validate_and_store(
        started_run.id,
        started_run.running_codex_unit_key,
        valid_output_json,
        _valid_receipt(),
    )

    expected_canonical = canonical_json(valid_output_payload)
    stored = db.execute(
        "SELECT * FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()
    assert result.unit_key == started_run.running_codex_unit_key
    assert result.batch_ordinal == 2
    assert result.canonical_output_json == expected_canonical
    assert result.output_sha256 == sha256_text(expected_canonical)
    assert stored["job_id"] == started_run.job_id
    assert stored["unit_key"] == CODEX_UNIT_KEY
    assert stored["batch_ordinal"] == 2
    assert stored["canonical_output_json"] == expected_canonical
    assert stored["output_sha256"] == sha256_text(expected_canonical)
    assert stored["receipt_model"] == "gpt-5.6-sol"
    assert stored["receipt_reasoning_effort"] == "max"
    assert stored["receipt_tool_call_count"] == 0
    assert stored["receipt_boundary_mode"] == "stored_statements_only"
    assert JobStateService(db).unit(
        started_run.job_id,
        started_run.running_codex_unit_key,
    ).status is UnitStatus.SUCCESS
    assert AnalysisRepository(db).get_effective_run_status(
        started_run.id
    ) is AnalysisRunStatus.TRANSPORT_VALIDATED
    assert db.execute(
        """
        SELECT COUNT(*)
        FROM analysis_run_events
        WHERE run_id=? AND status='accepted'
        """,
        (started_run.id,),
    ).fetchone()[0] == 0


def test_output_row_is_one_per_run_unit_and_append_only(
    db, started_run, valid_output_json
):
    CodexContractService(db).validate_and_store(
        started_run.id, CODEX_UNIT_KEY, valid_output_json, _valid_receipt()
    )
    row_id = db.execute(
        "SELECT id FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE analysis_run_outputs SET output_sha256=output_sha256 WHERE id=?",
            (row_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM analysis_run_outputs WHERE id=?", (row_id,))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            """
            INSERT INTO analysis_run_outputs(
                run_id,
                job_id,
                unit_key,
                batch_ordinal,
                canonical_output_json,
                output_sha256,
                receipt_model,
                receipt_reasoning_effort,
                receipt_tool_call_count,
                receipt_boundary_mode,
                created_at
            )
            SELECT
                run_id,
                job_id,
                unit_key,
                batch_ordinal,
                canonical_output_json,
                output_sha256,
                receipt_model,
                receipt_reasoning_effort,
                receipt_tool_call_count,
                receipt_boundary_mode,
                created_at
            FROM analysis_run_outputs
            WHERE id=?
            """,
            (row_id,),
        )


def test_insert_or_replace_cannot_bypass_output_append_only_trigger(
    db, started_run, valid_output_json
):
    CodexContractService(db).validate_and_store(
        started_run.id, CODEX_UNIT_KEY, valid_output_json, _valid_receipt()
    )
    before = dict(
        db.execute(
            "SELECT * FROM analysis_run_outputs WHERE run_id=?",
            (started_run.id,),
        ).fetchone()
    )
    replacement = before | {
        "canonical_output_json": '{"replaced":true}',
        "output_sha256": "0" * 64,
        "created_at": "2099-01-01T00:00:00.000000Z",
    }

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_run_outputs(
                id,
                run_id,
                job_id,
                unit_key,
                batch_ordinal,
                canonical_output_json,
                output_sha256,
                receipt_model,
                receipt_reasoning_effort,
                receipt_tool_call_count,
                receipt_boundary_mode,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(replacement.values()),
        )

    after = dict(
        db.execute(
            "SELECT * FROM analysis_run_outputs WHERE run_id=?",
            (started_run.id,),
        ).fetchone()
    )
    assert after == before


@pytest.mark.parametrize(
    ("foreign", "unit_key", "batch_ordinal"),
    [
        (False, "analysis-input:freeze", 1),
        (True, CODEX_UNIT_KEY, 2),
    ],
    ids=("non-codex-unit", "foreign-job-unit"),
)
def test_raw_output_row_rejects_non_codex_or_foreign_job_unit(
    db, started_run, valid_output_payload, foreign, unit_key, batch_ordinal
):
    job_id = started_run.job_id
    if foreign:
        subject_id = _create_subject(
            db,
            "Synthetic Foreign Output Person",
            SubjectKind.PERSON,
            channel_index=41,
        )
        job_id = _create_job_for_input(db, subject_id).job_id
    body = canonical_json(valid_output_payload)

    with pytest.raises(
        sqlite3.IntegrityError, match="ANALYSIS_CODEX_OUTPUT_UNIT_MISMATCH"
    ):
        db.execute(
            """
            INSERT INTO analysis_run_outputs(
                run_id,
                job_id,
                unit_key,
                batch_ordinal,
                canonical_output_json,
                output_sha256,
                receipt_model,
                receipt_reasoning_effort,
                receipt_tool_call_count,
                receipt_boundary_mode,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_run.id,
                job_id,
                unit_key,
                batch_ordinal,
                body,
                sha256_text(body),
                "gpt-5.6-sol",
                "max",
                0,
                "stored_statements_only",
                "2026-08-15T00:00:00.000000Z",
            ),
        )

    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_outputs"
    ).fetchone()[0] == 0


def test_failed_attempt_can_resume_and_later_append_transport_validated(
    db, started_run, valid_output_json
):
    with pytest.raises(DomainError) as failed:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            valid_output_json,
            CodexRunReceipt(
                "gpt-5.6-sol", "max", 1, "stored_statements_only"
            ),
        )
    assert failed.value.code == "CODEX_TOOL_CALL_DETECTED"

    input_hash = db.execute(
        """
        SELECT output_hash
        FROM job_units
        WHERE job_id=? AND unit_key='analysis-input:freeze'
        """,
        (started_run.job_id,),
    ).fetchone()[0]
    plan = JobStateService(db).resume(
        started_run.job_id, {"analysis-input:freeze": input_hash}
    )
    assert CODEX_UNIT_KEY in plan.pending_unit_keys
    JobStateService(db).begin_unit(started_run.job_id, CODEX_UNIT_KEY)

    CodexContractService(db).validate_and_store(
        started_run.id, CODEX_UNIT_KEY, valid_output_json, _valid_receipt()
    )

    history = tuple(
        (row["status"], row["safe_error_code"])
        for row in db.execute(
            """
            SELECT status, safe_error_code
            FROM analysis_run_events
            WHERE run_id=?
            ORDER BY id
            """,
            (started_run.id,),
        )
    )
    assert history == (
        ("started", None),
        ("failed", "CODEX_TOOL_CALL_DETECTED"),
        ("transport_validated", None),
    )
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()[0] == 1


def test_success_transaction_rolls_back_output_and_event_if_unit_cannot_complete(
    db, started_run, valid_output_json
):
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_codex_success
        BEFORE UPDATE OF status ON job_units
        WHEN NEW.job_id = OLD.job_id
            AND NEW.unit_key = 'codex:batch:1'
            AND NEW.status = 'success'
        BEGIN
            SELECT RAISE(ABORT, 'SYNTHETIC_SUCCESS_FAILURE');
        END
        """
    )

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id, CODEX_UNIT_KEY, valid_output_json, _valid_receipt()
        )

    assert error.value.code == "CODEX_OUTPUT_STORAGE_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()[0] == 0
    assert db.execute(
        """
        SELECT COUNT(*)
        FROM analysis_run_events
        WHERE run_id=? AND status='transport_validated'
        """,
        (started_run.id,),
    ).fetchone()[0] == 0
    assert AnalysisRepository(db).get_effective_run_status(
        started_run.id
    ) is AnalysisRunStatus.FAILED
    assert JobStateService(db).unit(
        started_run.job_id, CODEX_UNIT_KEY
    ).status is UnitStatus.FAILED

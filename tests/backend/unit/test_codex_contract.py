import copy
import json
from dataclasses import dataclass

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.codex_contract import (
    CodexContractService,
    CodexRunReceipt,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
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
                "codex_asset_hints": [
                    {
                        "expression": "Synthetic equity benchmark",
                        "suggested_asset": "nikkei_225",
                        "confidence": "high",
                    }
                ],
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


@pytest.mark.parametrize(
    ("receipt", "code"),
    [
        (
            CodexRunReceipt(
                "lower-model", "max", 0, "stored_statements_only"
            ),
            "CODEX_MODEL_MISMATCH",
        ),
        (
            CodexRunReceipt(
                "gpt-5.6-sol", "high", 0, "stored_statements_only"
            ),
            "CODEX_REASONING_MISMATCH",
        ),
        (
            CodexRunReceipt(
                "gpt-5.6-sol", "max", 1, "stored_statements_only"
            ),
            "CODEX_TOOL_CALL_DETECTED",
        ),
        (
            CodexRunReceipt("gpt-5.6-sol", "max", 0, "augmented"),
            "CODEX_BOUNDARY_MISMATCH",
        ),
    ],
)
def test_invalid_receipt_fails_the_running_batch_without_storing_output(
    db, started_run, valid_output_json, receipt, code
):
    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            started_run.running_codex_unit_key,
            valid_output_json,
            receipt,
        )

    assert error.value.code == code
    assert JobStateService(db).unit(
        started_run.job_id,
        started_run.running_codex_unit_key,
    ).status is UnitStatus.FAILED
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()[0] == 0
    failure = db.execute(
        """
        SELECT status, safe_error_code
        FROM analysis_run_events
        WHERE run_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (started_run.id,),
    ).fetchone()
    assert tuple(failure) == ("failed", code)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unknown_envelope_field": True}),
        lambda payload: payload["statements"][0].update(
            {"unknown_statement_field": True}
        ),
        lambda payload: payload["statements"][0]["evidence"][0].update(
            {"unknown_evidence_field": True}
        ),
        lambda payload: payload["statements"][0]["codex_asset_hints"][
            0
        ].update({"unknown_hint_field": True}),
    ],
    ids=("envelope", "statement", "evidence", "asset-hint"),
)
def test_output_contract_rejects_unknown_fields(
    db, started_run, valid_output_payload, mutate
):
    payload = copy.deepcopy(valid_output_payload)
    mutate(payload)

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"run_id": str(payload["run_id"])}),
        lambda payload: payload["statements"][0]["evidence"][0].update(
            {
                "segment_id": str(
                    payload["statements"][0]["evidence"][0]["segment_id"]
                )
            }
        ),
        lambda payload: payload["statements"][0].update(
            {"statement_type": "forecast_like"}
        ),
    ],
    ids=("run-id-coercion", "segment-id-coercion", "unknown-enum"),
)
def test_output_contract_rejects_coercion_and_unknown_enums(
    db, started_run, valid_output_payload, mutate
):
    payload = copy.deepcopy(valid_output_payload)
    mutate(payload)

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_OUTPUT_SCHEMA_INVALID"


def test_malformed_json_fails_closed(db, started_run):
    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            '{"run_id":',
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_OUTPUT_SCHEMA_INVALID"


def test_envelope_run_id_must_match_requested_run(
    db, started_run, valid_output_payload
):
    valid_output_payload["run_id"] = started_run.id + 1

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(valid_output_payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_RUN_ID_MISMATCH"


def test_envelope_requires_the_exact_manifest_batch_key(
    db, started_run, valid_output_payload
):
    valid_output_payload["batch_key"] = "codex:batch:2"

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(valid_output_payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_BATCH_KEY_MISMATCH"


def test_output_rejects_evidence_segment_not_frozen_into_the_run(
    db, started_run, valid_output_payload
):
    valid_output_payload["statements"][0]["evidence"][0][
        "segment_id"
    ] = started_run.segment_id + 10_000

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(valid_output_payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_EVIDENCE_SEGMENT_NOT_IN_RUN"


def test_evidence_excerpt_length_is_bounded(db, started_run, valid_output_payload):
    valid_output_payload["statements"][0]["evidence"][0]["excerpt"] = (
        "x" * 301
    )

    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            CODEX_UNIT_KEY,
            json.dumps(valid_output_payload),
            _valid_receipt(),
        )

    assert error.value.code == "CODEX_OUTPUT_SCHEMA_INVALID"

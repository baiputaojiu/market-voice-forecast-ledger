import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
)
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    DirectionKind,
    StatementType,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.statements import (
    StatementRepository,
)
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.services.codex_contract import (
    CodexContractService,
    CodexRunReceipt,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.speaker_assignment import (
    SpeakerAssignmentService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_analysis_input_boundaries import (
    _add_video_with_segments,
    _begin,
    _create_job_for_input,
    _create_subject,
    _save_assignment,
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
class PreparedOutput:
    run_id: int
    job_id: int
    subject_id: int
    video_id: int
    segment_ids: tuple[int, ...]


def _valid_receipt() -> CodexRunReceipt:
    return CodexRunReceipt(
        "gpt-5.6-sol", "max", 0, "stored_statements_only"
    )


def _statement(
    evidence,
    *,
    statement_type="future_forecast",
    forecast_basis="direct",
    condition_kind="unconditional",
    condition_text=None,
    direction_kind="up",
    turning_point_kind=None,
    target_expression="Synthetic equity benchmark",
    period_expression="Synthetic future period",
):
    return {
        "statement_type": statement_type,
        "forecast_basis": forecast_basis,
        "condition_kind": condition_kind,
        "condition_text": condition_text,
        "direction_kind": direction_kind,
        "turning_point_kind": turning_point_kind,
        "target_expression": target_expression,
        "period_expression": period_expression,
        "codex_asset_hints": [],
        "evidence": list(evidence),
    }


def _prepare_output(
    db,
    texts: tuple[str, ...],
    build_statements,
    *,
    subject_kind: SubjectKind = SubjectKind.PERSON,
    start_normalization: bool = True,
) -> PreparedOutput:
    subject_id = _create_subject(
        db,
        f"Synthetic Statement {subject_kind.value}",
        subject_kind,
        channel_index=71,
    )
    video_id, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id=f"synthetic-statement-{subject_kind.value}",
        published_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        texts=texts,
        channel_index=71,
    )
    if subject_kind is SubjectKind.ORGANIZATION:
        SpeakerAssignmentService(db).assign_organization_video(
            subject_id, video_id
        )
    else:
        for ordinal, segment_id in enumerate(segment_ids, start=1):
            _save_assignment(
                db,
                segment_id=segment_id,
                kind=AssignmentKind.SUBJECT,
                subject_id=subject_id,
                evidence_hash=f"statement-subject-evidence-{ordinal}",
            )

    prepared = _create_job_for_input(db, subject_id)
    run = _begin(db, prepared)
    frozen_segment_ids = tuple(
        row.segment_id
        for row in AnalysisRepository(db).get_input_segments(run.id)
    )
    assert frozen_segment_ids == segment_ids

    jobs = JobStateService(db)
    jobs.begin_unit(prepared.job_id, CODEX_UNIT_KEY)
    payload = {
        "run_id": run.id,
        "batch_key": CODEX_UNIT_KEY,
        "statements": build_statements(segment_ids),
    }
    CodexContractService(db).validate_and_store(
        run.id,
        CODEX_UNIT_KEY,
        json.dumps(payload, ensure_ascii=False),
        _valid_receipt(),
    )
    if start_normalization:
        jobs.begin_unit(prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    return PreparedOutput(
        run.id,
        prepared.job_id,
        subject_id,
        video_id,
        segment_ids,
    )


def _one_forecast(segment_ids):
    return [
        _statement(
            (
                {
                    "segment_id": segment_ids[0],
                    "excerpt": "Synthetic first subject evidence.",
                },
            )
        )
    ]


def _unit_failure(db, job_id: int) -> tuple[str, str | None]:
    row = db.execute(
        """
        SELECT status, error_code
        FROM job_units
        WHERE job_id=? AND unit_key=?
        """,
        (job_id, STATEMENT_NORMALIZATION_UNIT_KEY),
    ).fetchone()
    return row["status"], row["error_code"]


def test_four_statement_types_are_distinct_and_only_future_is_candidate(db):
    def statements(segment_ids):
        evidence = (
            {
                "segment_id": segment_ids[0],
                "excerpt": "Synthetic first subject evidence.",
            },
        )
        return [
            _statement(evidence),
            _statement(
                evidence,
                statement_type="current_analysis",
                forecast_basis=None,
                direction_kind="flat",
            ),
            _statement(
                evidence,
                statement_type="past_result_analysis",
                forecast_basis=None,
                direction_kind="unknown",
            ),
            _statement(
                evidence,
                statement_type="general_statement",
                forecast_basis=None,
                direction_kind=None,
            ),
        ]

    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), statements
    )

    rows = StatementService(db).normalize_and_store(prepared.run_id)

    assert tuple(row.statement_type for row in rows) == (
        StatementType.FUTURE_FORECAST,
        StatementType.CURRENT_ANALYSIS,
        StatementType.PAST_RESULT_ANALYSIS,
        StatementType.GENERAL_STATEMENT,
    )
    assert tuple(row.heatmap_candidate for row in rows) == (
        True,
        False,
        False,
        False,
    )
    assert rows[1].direction_kind is DirectionKind.FLAT
    assert rows[2].direction_kind is DirectionKind.UNKNOWN
    assert rows[3].direction_kind is None
    assert StatementRepository(db).list_run_statements(prepared.run_id) == rows


def test_turning_point_flat_and_unknown_are_not_coerced(db):
    def statements(segment_ids):
        evidence = (
            {
                "segment_id": segment_ids[0],
                "excerpt": "Synthetic first subject evidence.",
            },
        )
        return [
            _statement(
                evidence,
                direction_kind="turning_point",
                turning_point_kind="bottom",
            ),
            _statement(evidence, direction_kind="flat"),
            _statement(evidence, direction_kind="unknown"),
        ]

    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), statements
    )

    rows = StatementService(db).normalize_and_store(prepared.run_id)

    assert tuple(row.direction_kind for row in rows) == (
        DirectionKind.TURNING_POINT,
        DirectionKind.FLAT,
        DirectionKind.UNKNOWN,
    )
    assert rows[0].turning_point_kind.value == "bottom"
    assert all(row.heatmap_candidate for row in rows)


def test_one_statement_can_link_ordered_subject_segments(db):
    def statements(segment_ids):
        return [
            _statement(
                (
                    {
                        "segment_id": segment_ids[1],
                        "excerpt": "Synthetic second subject evidence.",
                    },
                    {
                        "segment_id": segment_ids[0],
                        "excerpt": "first subject",
                    },
                )
            )
        ]

    prepared = _prepare_output(
        db,
        (
            "Synthetic first subject evidence.",
            "Synthetic second subject evidence.",
        ),
        statements,
    )

    row = StatementService(db).normalize_and_store(prepared.run_id)[0]

    assert [link.segment_id for link in row.evidence_links] == [
        prepared.segment_ids[1],
        prepared.segment_ids[0],
    ]
    assert [link.excerpt for link in row.evidence_links] == [
        "Synthetic second subject evidence.",
        "first subject",
    ]
    assert [link.start_ms for link in row.evidence_links] == [10_000, 0]
    assert [link.end_ms for link in row.evidence_links] == [20_000, 10_000]
    assert row.source_video_id == prepared.video_id


def test_free_summary_not_present_in_transcript_is_rejected_atomically(db):
    def statements(segment_ids):
        return [
            _statement(
                (
                    {
                        "segment_id": segment_ids[0],
                        "excerpt": "Invented free summary.",
                    },
                )
            )
        ]

    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), statements
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "EVIDENCE_NOT_CONTIGUOUS_SOURCE_TEXT"
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_statement_evidence_links"
    ).fetchone()[0] == 0
    assert _unit_failure(db, prepared.job_id) == (
        "failed",
        "EVIDENCE_NOT_CONTIGUOUS_SOURCE_TEXT",
    )
    assert JobStateService(db).unit(
        prepared.job_id, CODEX_UNIT_KEY
    ).status is UnitStatus.SUCCESS


def test_deleted_current_transcript_text_cannot_be_new_evidence(db):
    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), _one_forecast
    )
    db.execute(
        """
        UPDATE transcript_segments
        SET text_body=NULL, text_deleted_at=?
        WHERE id=?
        """,
        ("2026-08-15T00:00:00.000000Z", prepared.segment_ids[0]),
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "EVIDENCE_SOURCE_TEXT_UNAVAILABLE"
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0


def test_person_evidence_must_still_be_assigned_to_the_run_subject(db):
    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), _one_forecast
    )
    _save_assignment(
        db,
        segment_id=prepared.segment_ids[0],
        kind=AssignmentKind.HOLD,
        subject_id=None,
        evidence_hash="statement-drifted-to-hold",
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "EVIDENCE_SUBJECT_ASSIGNMENT_REQUIRED"
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0


def test_organization_evidence_must_still_be_channel_organization_assigned(db):
    prepared = _prepare_output(
        db,
        ("Synthetic first subject evidence.",),
        _one_forecast,
        subject_kind=SubjectKind.ORGANIZATION,
    )
    _save_assignment(
        db,
        segment_id=prepared.segment_ids[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=prepared.subject_id,
        evidence_hash="statement-drifted-to-manual",
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "EVIDENCE_ORGANIZATION_ASSIGNMENT_REQUIRED"
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0


def test_normalization_requires_one_output_for_every_successful_codex_batch(db):
    subject_id = _create_subject(
        db,
        "Synthetic Missing Codex Output Person",
        SubjectKind.PERSON,
        channel_index=72,
    )
    _, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-missing-codex-output",
        published_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        texts=("Synthetic first subject evidence.",),
        channel_index=72,
    )
    _save_assignment(
        db,
        segment_id=segment_ids[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=subject_id,
        evidence_hash="missing-output-subject-evidence",
    )
    prepared = _create_job_for_input(db, subject_id)
    run = _begin(db, prepared)
    jobs = JobStateService(db)
    jobs.begin_unit(prepared.job_id, CODEX_UNIT_KEY)
    with transaction(db):
        jobs.complete_unit_in_transaction(
            prepared.job_id, CODEX_UNIT_KEY, "synthetic-orphan-output"
        )
    jobs.begin_unit(prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY)

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(run.id)

    assert error.value.code == "CODEX_BATCH_OUTPUT_COUNT_INVALID"
    assert _unit_failure(db, prepared.job_id) == (
        "failed",
        "CODEX_BATCH_OUTPUT_COUNT_INVALID",
    )
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0


def test_normalization_unit_must_be_running(db):
    prepared = _prepare_output(
        db,
        ("Synthetic first subject evidence.",),
        _one_forecast,
        start_normalization=False,
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "STATEMENT_NORMALIZATION_UNIT_NOT_RUNNING"
    assert _unit_failure(db, prepared.job_id) == ("pending", None)


def test_rows_links_hash_and_unit_success_commit_together_and_are_reused(db):
    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), _one_forecast
    )

    first = StatementService(db).normalize_and_store(prepared.run_id)
    unit = JobStateService(db).unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    )
    expected_payload = [
        {
            "batch_ordinal": 2,
            "condition_kind": "unconditional",
            "condition_text": None,
            "direction_kind": "up",
            "evidence": [
                {
                    "end_ms": 10_000,
                    "excerpt": "Synthetic first subject evidence.",
                    "ordinal": 1,
                    "run_segment_id": first[0].evidence_links[0].run_segment_id,
                    "segment_id": prepared.segment_ids[0],
                    "start_ms": 0,
                }
            ],
            "forecast_basis": "direct",
            "heatmap_candidate": True,
            "ordinal": 1,
            "period_expression": "Synthetic future period",
            "proposal_ordinal": 1,
            "source_video_id": prepared.video_id,
            "statement_type": "future_forecast",
            "target_expression": "Synthetic equity benchmark",
            "turning_point_kind": None,
        }
    ]
    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == sha256_text(canonical_json(expected_payload))
    before_counts = (
        db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0],
        db.execute(
            "SELECT COUNT(*) FROM analysis_statement_evidence_links"
        ).fetchone()[0],
    )

    second = StatementService(db).normalize_and_store(prepared.run_id)

    assert second == first
    assert (
        db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0],
        db.execute(
            "SELECT COUNT(*) FROM analysis_statement_evidence_links"
        ).fetchone()[0],
    ) == before_counts
    assert JobStateService(db).unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).attempt_count == 1


def test_storage_failure_rolls_back_all_rows_and_retry_restarts_unit(db):
    def statements(segment_ids):
        return [
            _statement(
                (
                    {
                        "segment_id": segment_ids[0],
                        "excerpt": "Synthetic first subject evidence.",
                    },
                    {
                        "segment_id": segment_ids[1],
                        "excerpt": "Synthetic second subject evidence.",
                    },
                )
            )
        ]

    prepared = _prepare_output(
        db,
        (
            "Synthetic first subject evidence.",
            "Synthetic second subject evidence.",
        ),
        statements,
    )
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_second_statement_evidence
        BEFORE INSERT ON analysis_statement_evidence_links
        WHEN NEW.ordinal=2
        BEGIN
            SELECT RAISE(ABORT, 'SYNTHETIC_EVIDENCE_FAILURE');
        END
        """
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "STATEMENT_STORAGE_FAILED"
    assert db.execute("SELECT COUNT(*) FROM analysis_statements").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_statement_evidence_links"
    ).fetchone()[0] == 0
    assert _unit_failure(db, prepared.job_id) == (
        "failed",
        "STATEMENT_STORAGE_FAILED",
    )

    db.execute("DROP TRIGGER synthetic_reject_second_statement_evidence")
    artifacts = {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            """
            SELECT unit_key, output_hash
            FROM job_units
            WHERE job_id=? AND status='success'
            """,
            (prepared.job_id,),
        )
    }
    plan = JobStateService(db).resume(prepared.job_id, artifacts)
    assert plan.next_unit_key == STATEMENT_NORMALIZATION_UNIT_KEY
    JobStateService(db).begin_unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    )

    rows = StatementService(db).normalize_and_store(prepared.run_id)

    assert len(rows) == 1
    assert len(rows[0].evidence_links) == 2
    assert JobStateService(db).unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).status is UnitStatus.SUCCESS


def test_statement_and_evidence_rows_are_append_only(db):
    prepared = _prepare_output(
        db, ("Synthetic first subject evidence.",), _one_forecast
    )
    row = StatementService(db).normalize_and_store(prepared.run_id)[0]

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE analysis_statements SET statement_type=statement_type WHERE id=?",
            (row.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "DELETE FROM analysis_statements WHERE id=?",
            (row.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            UPDATE analysis_statement_evidence_links
            SET excerpt=excerpt
            WHERE statement_id=? AND ordinal=1
            """,
            (row.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            DELETE FROM analysis_statement_evidence_links
            WHERE statement_id=? AND ordinal=1
            """,
            (row.id,),
        )


def test_exactly_300_unicode_code_points_are_retained(db):
    excerpt = "予" * 300

    def statements(segment_ids):
        return [
            _statement(
                ({"segment_id": segment_ids[0], "excerpt": excerpt},)
            )
        ]

    prepared = _prepare_output(db, (f"x{excerpt}y",), statements)

    row = StatementService(db).normalize_and_store(prepared.run_id)[0]

    assert row.evidence_links[0].excerpt == excerpt
    assert len(row.evidence_links[0].excerpt) == 300

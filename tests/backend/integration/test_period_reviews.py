import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
)
from market_voice_forecast_ledger.domain.enums import (
    PeriodReviewDecision,
    TimeBasis,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.audit import AuditRepository
from market_voice_forecast_ledger.repositories.periods import PeriodRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.periods import (
    PeriodReviewService,
    PeriodService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_statement_evidence import (
    _prepare_output,
    _statement,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _prepare_statements(db, expressions):
    def statements(segment_ids):
        evidence = (
            {
                "segment_id": segment_ids[0],
                "excerpt": "Synthetic period evidence.",
            },
        )
        return [
            _statement(
                evidence,
                target_expression=f"Synthetic period target {ordinal}",
                period_expression=expression,
            )
            for ordinal, expression in enumerate(expressions, start=1)
        ]

    prepared = _prepare_output(
        db,
        ("Synthetic period evidence.",),
        statements,
    )
    StatementService(db).normalize_and_store(prepared.run_id)
    return prepared


def _normalize(db, expressions):
    prepared = _prepare_statements(db, expressions)
    JobStateService(db).begin_unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    )
    periods = PeriodService(db).normalize_run(prepared.run_id)
    return prepared, periods


@pytest.fixture
def unknown_period(db):
    _, periods = _normalize(db, ("当面",))
    return periods[0]


def _unit_state(db, job_id):
    row = db.execute(
        """
        SELECT status, error_code
        FROM job_units
        WHERE job_id=? AND unit_key=?
        """,
        (job_id, PERIOD_NORMALIZATION_UNIT_KEY),
    ).fetchone()
    return row["status"], row["error_code"]


def test_normalize_run_requires_caller_to_start_period_unit(db):
    prepared = _prepare_statements(db, ("来週",))

    with pytest.raises(DomainError) as error:
        PeriodService(db).normalize_run(prepared.run_id)

    assert error.value.code == "PERIOD_NORMALIZATION_UNIT_NOT_RUNNING"
    assert _unit_state(db, prepared.job_id) == ("pending", None)
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_statement_periods"
    ).fetchone()[0] == 0


def test_normalize_run_stores_explicit_relative_and_unknown_periods(db):
    prepared, periods = _normalize(db, ("2027年", "来週", "当面"))

    assert tuple(period.statement_id for period in periods) == tuple(
        row["id"]
        for row in db.execute(
            "SELECT id FROM analysis_statements WHERE run_id=? ORDER BY ordinal",
            (prepared.run_id,),
        )
    )
    assert periods[0].start_date == date(2027, 1, 1)
    assert periods[0].end_date == date(2027, 12, 31)
    assert periods[0].time_basis is TimeBasis.EXPLICIT_STATEMENT
    assert periods[0].basis_published_at is None
    assert periods[1].start_date == date(2026, 8, 17)
    assert periods[1].end_date == date(2026, 8, 23)
    assert periods[1].time_basis is TimeBasis.PUBLISHED_AT
    assert periods[1].basis_published_at == datetime(
        2026, 8, 14, 12, tzinfo=timezone.utc
    )
    assert periods[2].is_unknown is True
    assert periods[2].start_date is None
    assert periods[2].end_date is None
    assert periods[2].time_basis is None
    assert periods[2].basis_published_at is None
    assert PeriodRepository(db).list_run_periods(prepared.run_id) == periods


def test_period_rows_hash_and_unit_success_commit_together_and_are_reused(db):
    prepared, first = _normalize(db, ("2027年", "来週", "当面"))
    expected_payload = [
        {
            "basis_published_at": None,
            "end_date": "2027-12-31",
            "is_unknown": False,
            "source_expression": "2027年",
            "start_date": "2027-01-01",
            "statement_id": first[0].statement_id,
            "time_basis": "explicit_statement",
        },
        {
            "basis_published_at": "2026-08-14T12:00:00.000000Z",
            "end_date": "2026-08-23",
            "is_unknown": False,
            "source_expression": "来週",
            "start_date": "2026-08-17",
            "statement_id": first[1].statement_id,
            "time_basis": "published_at",
        },
        {
            "basis_published_at": None,
            "end_date": None,
            "is_unknown": True,
            "source_expression": "当面",
            "start_date": None,
            "statement_id": first[2].statement_id,
            "time_basis": None,
        },
    ]
    unit = JobStateService(db).unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    )

    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == sha256_text(canonical_json(expected_payload))

    second = PeriodService(db).normalize_run(prepared.run_id)

    assert second == first
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_statement_periods"
    ).fetchone()[0] == 3
    assert JobStateService(db).unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    ).attempt_count == 1


def test_storage_failure_rolls_back_all_periods_and_retry_reuses_statements(db):
    prepared = _prepare_statements(db, ("2027年", "来週"))
    statement_ids = tuple(
        row["id"]
        for row in db.execute(
            "SELECT id FROM analysis_statements WHERE run_id=? ORDER BY ordinal",
            (prepared.run_id,),
        )
    )
    JobStateService(db).begin_unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    )
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_relative_period
        BEFORE INSERT ON analysis_statement_periods
        WHEN NEW.source_expression='来週'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PERIOD_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as error:
        PeriodService(db).normalize_run(prepared.run_id)

    assert error.value.code == "PERIOD_STORAGE_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_statement_periods"
    ).fetchone()[0] == 0
    assert _unit_state(db, prepared.job_id) == (
        "failed",
        "PERIOD_STORAGE_FAILED",
    )
    assert JobStateService(db).unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).status is UnitStatus.SUCCESS
    assert tuple(
        row["id"]
        for row in db.execute(
            "SELECT id FROM analysis_statements WHERE run_id=? ORDER BY ordinal",
            (prepared.run_id,),
        )
    ) == statement_ids

    db.execute("DROP TRIGGER synthetic_reject_relative_period")
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
    assert plan.next_unit_key == PERIOD_NORMALIZATION_UNIT_KEY
    JobStateService(db).begin_unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    )

    periods = PeriodService(db).normalize_run(prepared.run_id)

    assert len(periods) == 2
    assert _unit_state(db, prepared.job_id) == ("success", None)
    assert tuple(period.statement_id for period in periods) == statement_ids


def test_unknown_period_is_not_eligible_until_approved(db, unknown_period):
    reviews = PeriodReviewService(db)

    assert reviews.effective(unknown_period.id) is None

    review_id = reviews.review(
        unknown_period.id,
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "時期不明列で表示",
    )
    effective = reviews.effective(unknown_period.id)

    assert effective.id == review_id
    assert effective.approved_for_unknown_column is True
    assert effective.excluded is False
    stored = PeriodRepository(db).get(unknown_period.id)
    assert stored == unknown_period
    assert stored.is_unknown is True
    assert stored.start_date is None
    assert stored.end_date is None


def test_latest_review_id_is_effective_and_reject_excludes(db, unknown_period):
    service = PeriodReviewService(db)
    approved_id = service.review(
        unknown_period.id,
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "時期不明列へ",
    )
    rejected_id = service.review(
        unknown_period.id,
        PeriodReviewDecision.REJECT,
        "user",
        "比較から除外",
    )

    effective = service.effective(unknown_period.id)

    assert rejected_id > approved_id
    assert effective.id == rejected_id
    assert effective.approved_for_unknown_column is False
    assert effective.excluded is True
    assert db.execute(
        "SELECT COUNT(*) FROM period_reviews WHERE period_id=?",
        (unknown_period.id,),
    ).fetchone()[0] == 2


def test_approve_unknown_cannot_route_a_known_period_to_unknown_column(db):
    _, periods = _normalize(db, ("2027年",))

    with pytest.raises(DomainError) as error:
        PeriodReviewService(db).review(
            periods[0].id,
            PeriodReviewDecision.APPROVE_UNKNOWN,
            "user",
            "誤った専用列承認",
        )

    assert error.value.code == "PERIOD_REVIEW_INVALID"
    assert db.execute("SELECT COUNT(*) FROM period_reviews").fetchone()[0] == 0
    assert AuditRepository(db).list_for_entity(
        "analysis_statement_period", str(periods[0].id)
    ) == ()


def test_period_review_requires_non_empty_actor_and_reason(db, unknown_period):
    service = PeriodReviewService(db)

    for actor, reason in (("", "reason"), ("user", "")):
        with pytest.raises(DomainError) as error:
            service.review(
                unknown_period.id,
                PeriodReviewDecision.REJECT,
                actor,
                reason,
            )
        assert error.value.code == "PERIOD_REVIEW_INVALID"

    assert db.execute("SELECT COUNT(*) FROM period_reviews").fetchone()[0] == 0


def test_review_and_safe_audit_event_commit_together(db, unknown_period):
    PeriodReviewService(db).review(
        unknown_period.id,
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "時期不明列で表示",
    )

    events = AuditRepository(db).list_for_entity(
        "analysis_statement_period", str(unknown_period.id)
    )

    assert len(events) == 1
    assert events[0].operation == "review"
    assert events[0].actor_kind == "user"
    assert events[0].reason_code == "approve_unknown"
    assert events[0].reason_text == "時期不明列で表示"
    assert events[0].before is None
    assert events[0].after == {
        "actor": "user",
        "decision": "approve_unknown",
        "period_id": unknown_period.id,
        "reason": "時期不明列で表示",
    }
    serialized = canonical_json(events[0].after)
    assert "text_body" not in serialized
    assert "input_text" not in serialized
    assert "path" not in serialized


def test_audit_insert_failure_rolls_back_period_review(db, unknown_period):
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_period_review_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.entity_type='analysis_statement_period'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_AUDIT_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as error:
        PeriodReviewService(db).review(
            unknown_period.id,
            PeriodReviewDecision.REJECT,
            "user",
            "合成監査失敗",
        )

    assert error.value.code == "PERIOD_REVIEW_STORAGE_FAILED"
    assert db.execute("SELECT COUNT(*) FROM period_reviews").fetchone()[0] == 0
    assert AuditRepository(db).list_for_entity(
        "analysis_statement_period", str(unknown_period.id)
    ) == ()


def test_period_and_review_rows_reject_raw_update_delete_and_replace(
    db, unknown_period
):
    review_id = PeriodReviewService(db).review(
        unknown_period.id,
        PeriodReviewDecision.REJECT,
        "user",
        "合成除外",
    )

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            UPDATE analysis_statement_periods
            SET source_expression=source_expression
            WHERE id=?
            """,
            (unknown_period.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "DELETE FROM analysis_statement_periods WHERE id=?",
            (unknown_period.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_statement_periods(
                id, statement_id, source_expression, start_date, end_date,
                time_basis, basis_published_at, is_unknown
            )
            SELECT id, statement_id, source_expression, start_date, end_date,
                   time_basis, basis_published_at, is_unknown
            FROM analysis_statement_periods
            WHERE id=?
            """,
            (unknown_period.id,),
        )

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE period_reviews SET reason=reason WHERE id=?",
            (review_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM period_reviews WHERE id=?", (review_id,))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO period_reviews(
                id, period_id, decision, actor, reason, created_at
            )
            SELECT id, period_id, decision, actor, reason, created_at
            FROM period_reviews
            WHERE id=?
            """,
            (review_id,),
        )


def test_review_rows_store_only_safe_fields(db, unknown_period):
    PeriodReviewService(db).review(
        unknown_period.id,
        PeriodReviewDecision.REJECT,
        "user",
        "合成除外",
    )

    row = db.execute("SELECT * FROM period_reviews").fetchone()

    assert set(row.keys()) == {
        "id",
        "period_id",
        "decision",
        "actor",
        "reason",
        "created_at",
    }
    assert json.loads(
        db.execute(
            """
            SELECT after_json
            FROM audit_events
            WHERE entity_type='analysis_statement_period'
            """
        ).fetchone()[0]
    ) == {
        "actor": "user",
        "decision": "reject",
        "period_id": unknown_period.id,
        "reason": "合成除外",
    }

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    AssignmentKind,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    MappingKind,
    MappingReviewDecision,
    PeriodReviewDecision,
    StatementType,
    UnitStatus,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.domain.jobs import (
    ASSET_MAPPING_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.forecasts import ForecastRepository
from market_voice_forecast_ledger.services.asset_mapping import AssetMappingService
from market_voice_forecast_ledger.services.codex_contract import (
    CodexContractService,
)
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from market_voice_forecast_ledger.services.periods import (
    PeriodReviewService,
    PeriodService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_analysis_input_boundaries import (
    _add_video_with_segments,
    _begin,
    _create_job_for_input,
    _create_subject,
    _save_assignment,
)
from tests.backend.integration.test_statement_evidence import _valid_receipt
from market_voice_forecast_ledger.domain.enums import SubjectKind


UTC = timezone.utc
OLDER = datetime(2026, 8, 10, 3, tzinfo=UTC)
NEWER = datetime(2026, 8, 11, 3, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class StatementSpec:
    video_id: str
    published_at: datetime
    direction: DirectionKind = DirectionKind.UP
    statement_type: StatementType = StatementType.FUTURE_FORECAST
    forecast_basis: ForecastBasis | None = ForecastBasis.DIRECT
    condition_kind: ConditionKind = ConditionKind.UNCONDITIONAL
    condition_text: str | None = None
    period_expression: str | None = "来週"
    target_expression: str = "日経平均"
    confidence: Confidence = Confidence.HIGH
    extra_segment_count: int = 0


@dataclass(frozen=True)
class PreparedProjection:
    run_id: int
    job_id: int
    statement_ids: tuple[int, ...]
    period_ids: tuple[int, ...]
    mapping_ids: tuple[int, ...]
    video_ids: tuple[int, ...]


def _prepare_upstream(db, specs: tuple[StatementSpec, ...]) -> PreparedProjection:
    subject_id = _create_subject(
        db,
        f"Synthetic projection subject {db.execute('SELECT COUNT(*) FROM analysis_subjects').fetchone()[0]}",
        SubjectKind.PERSON,
        channel_index=81,
    )
    segment_ids: list[int] = []
    video_ids: list[int] = []
    for ordinal, spec in enumerate(specs, start=1):
        text = f"Synthetic projection evidence {ordinal}."
        texts = (text,) + tuple(
            f"Synthetic projection context {ordinal}-{index}."
            for index in range(1, spec.extra_segment_count + 1)
        )
        video_id, created_segments = _add_video_with_segments(
            db,
            subject_id=subject_id,
            youtube_video_id=spec.video_id,
            published_at=spec.published_at,
            texts=texts,
            channel_index=81,
        )
        for segment_index, created_segment_id in enumerate(
            created_segments, start=1
        ):
            _save_assignment(
                db,
                segment_id=created_segment_id,
                kind=AssignmentKind.SUBJECT,
                subject_id=subject_id,
                evidence_hash=(
                    f"projection-assignment-{ordinal}-{segment_index}"
                ),
            )
        video_ids.append(video_id)
        segment_ids.append(created_segments[0])

    prepared = _create_job_for_input(db, subject_id)
    run = _begin(db, prepared)
    proposals = []
    for ordinal, (spec, segment_id) in enumerate(
        zip(specs, segment_ids, strict=True), start=1
    ):
        expression = spec.target_expression
        proposals.append(
            {
                "statement_type": spec.statement_type.value,
                "forecast_basis": (
                    None if spec.forecast_basis is None else spec.forecast_basis.value
                ),
                "condition_kind": spec.condition_kind.value,
                "condition_text": spec.condition_text,
                "direction_kind": spec.direction.value,
                "turning_point_kind": (
                    "bottom" if spec.direction is DirectionKind.TURNING_POINT else None
                ),
                "target_expression": expression,
                "period_expression": spec.period_expression,
                "codex_asset_hints": [
                    {
                        "expression": expression,
                        "suggested_asset": Asset.NIKKEI_225.value,
                        "confidence": spec.confidence.value,
                    }
                ],
                "evidence": [
                    {
                        "segment_id": segment_id,
                        "excerpt": f"Synthetic projection evidence {ordinal}.",
                    }
                ],
            }
        )

    jobs = JobStateService(db)
    jobs.begin_unit(prepared.job_id, "codex:batch:1")
    CodexContractService(db).validate_and_store(
        run.id,
        "codex:batch:1",
        json.dumps(
            {
                "run_id": run.id,
                "batch_key": "codex:batch:1",
                "statements": proposals,
            }
        ),
        _valid_receipt(),
    )
    jobs.begin_unit(prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    statements = StatementService(db).normalize_and_store(run.id)
    jobs.begin_unit(prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY)
    periods = PeriodService(db).normalize_run(run.id)
    jobs.begin_unit(prepared.job_id, ASSET_MAPPING_UNIT_KEY)
    mappings = AssetMappingService(db).map_run(run.id)
    return PreparedProjection(
        run_id=run.id,
        job_id=prepared.job_id,
        statement_ids=tuple(row.id for row in statements),
        period_ids=tuple(row.id for row in periods),
        mapping_ids=tuple(row.id for row in mappings),
        video_ids=tuple(video_ids),
    )


def _project(db, prepared: PreparedProjection):
    service = ForecastProjectionService(db)
    review_hash = service.effective_review_state_hash(prepared.run_id)
    JobStateService(db).begin_unit(
        prepared.job_id,
        FORECAST_PROJECTION_UNIT_KEY,
        review_hash,
    )
    return service.project_run(prepared.run_id, ProjectionTrigger.INITIAL)


def _successful_artifacts(db, job_id: int) -> dict[str, str]:
    return {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            """
            SELECT unit_key, output_hash
            FROM job_units
            WHERE job_id=? AND status='success'
            """,
            (job_id,),
        )
    }


def test_only_future_with_effective_mapping_and_period_enters_projection(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("future", NEWER),
            StatementSpec(
                "current",
                NEWER,
                statement_type=StatementType.CURRENT_ANALYSIS,
                forecast_basis=None,
            ),
            StatementSpec(
                "past",
                NEWER,
                statement_type=StatementType.PAST_RESULT_ANALYSIS,
                forecast_basis=None,
            ),
            StatementSpec(
                "general",
                NEWER,
                statement_type=StatementType.GENERAL_STATEMENT,
                forecast_basis=None,
            ),
        ),
    )

    batch = _project(db, prepared)
    forecasts = ForecastRepository(db).list_batch_forecasts(batch.id)

    assert len(forecasts) == 1
    assert forecasts[0].supporting_statement_ids == (prepared.statement_ids[0],)
    assert forecasts[0].primary_direction is DirectionKind.UP
    assert {row.asset for row in forecasts} == {Asset.NIKKEI_225}


def test_same_exact_condition_conflicts_but_distinct_conditions_stay_separate(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "same-a",
                NEWER,
                DirectionKind.UP,
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="if synthetic threshold A",
            ),
            StatementSpec(
                "same-b",
                NEWER,
                DirectionKind.DOWN,
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="if synthetic threshold A",
            ),
            StatementSpec(
                "different",
                NEWER,
                DirectionKind.DOWN,
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="if synthetic threshold B",
            ),
            StatementSpec("unconditional", NEWER, DirectionKind.UP),
        ),
    )

    batch = _project(db, prepared)
    forecasts = ForecastRepository(db).list_batch_forecasts(batch.id)

    assert len(forecasts) == 3
    by_condition = {row.condition_text: row for row in forecasts}
    assert by_condition["if synthetic threshold A"].view_relation is ViewRelation.DISAGREEMENT
    assert set(by_condition["if synthetic threshold A"].directions) == {
        DirectionKind.UP,
        DirectionKind.DOWN,
    }
    assert by_condition["if synthetic threshold B"].view_relation is ViewRelation.CURRENT
    assert by_condition[None].condition_kind is ConditionKind.UNCONDITIONAL
    assert by_condition[None].view_relation is ViewRelation.CURRENT


def test_unknown_and_low_mapping_require_latest_effective_approvals(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "reviewed",
                NEWER,
                period_expression="当面",
                confidence=Confidence.LOW,
            ),
        ),
    )
    assert _project(db, prepared).forecasts == ()

    # Reprojection after reviews uses the caller-owned internal primitive.
    PeriodReviewService(db).review(
        prepared.period_ids[0],
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "Synthetic unknown approval",
    )
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.APPROVE,
            "user",
            "Synthetic mapping approval",
            None,
        )
    )
    with transaction(db):
        reviewed = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )

    assert len(reviewed.forecasts) == 1
    assert reviewed.forecasts[0].unknown_period is True


def test_projection_uses_frozen_run_publication_timestamp(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("old-down", OLDER, DirectionKind.DOWN),
            StatementSpec("new-up", NEWER, DirectionKind.UP),
        ),
    )
    db.execute(
        "UPDATE videos SET published_at=? WHERE id=?",
        ("2030-01-01T00:00:00.000000Z", prepared.video_ids[0]),
    )

    batch = _project(db, prepared)

    assert batch.forecasts[0].primary_direction is DirectionKind.UP
    assert batch.forecasts[0].view_relation is ViewRelation.CHANGED
    assert batch.forecasts[0].selected_published_at == NEWER


def test_projection_uses_frozen_youtube_id_in_result_and_artifact_hash(db):
    prepared = _prepare_upstream(
        db, (StatementSpec("frozen-youtube-id", NEWER),)
    )
    db.execute(
        "UPDATE videos SET youtube_video_id=? WHERE id=?",
        ("mutable-current-youtube-id", prepared.video_ids[0]),
    )

    batch = _project(db, prepared)
    repository = ForecastRepository(db)
    artifact = repository.batch_artifact(batch.id)

    expected_key = (
        f"frozen-youtube-id:{prepared.statement_ids[0]:020d}"
    )
    assert batch.forecasts[0].stable_selection_key == expected_key
    assert artifact["forecasts"][0]["stable_selection_key"] == expected_key
    assert (
        _successful_artifacts(db, prepared.job_id)[
            FORECAST_PROJECTION_UNIT_KEY
        ]
        == repository.batch_artifact_hash(batch.id)
    )


@pytest.mark.parametrize(
    "malformation",
    ["missing", "duplicate", "conflicting_youtube_id", "mismatched_time"],
)
def test_projection_rejects_malformed_frozen_source_metadata(
    db, malformation: str
):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "malformed-source-metadata",
                NEWER,
                extra_segment_count=(
                    1 if malformation == "conflicting_youtube_id" else 0
                ),
            ),
        ),
    )
    row = db.execute(
        "SELECT metadata_json FROM analysis_input_snapshots WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    segments = metadata["segments"]
    if malformation == "missing":
        segments.pop()
    elif malformation == "duplicate":
        segments.append(dict(segments[0]))
    elif malformation == "conflicting_youtube_id":
        segments[1]["youtube_video_id"] = "conflicting-frozen-youtube-id"
    else:
        segments[0]["published_at"] = "2030-01-01T00:00:00.000000Z"
    db.execute("DROP TRIGGER analysis_input_snapshots_limited_update")
    db.execute(
        "UPDATE analysis_input_snapshots SET metadata_json=? WHERE run_id=?",
        (
            json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            prepared.run_id,
        ),
    )

    with pytest.raises(DomainError) as error:
        _project(db, prepared)

    assert error.value.code == "FORECAST_SOURCE_SNAPSHOT_INVALID"


def test_projection_rejects_consistently_rewritten_frozen_youtube_id(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "contract-bound-youtube-id",
                NEWER,
                extra_segment_count=1,
            ),
        ),
    )
    row = db.execute(
        "SELECT metadata_json FROM analysis_input_snapshots WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    matching_segments = [
        segment
        for segment in metadata["segments"]
        if segment["video_id"] == prepared.video_ids[0]
    ]
    assert len(matching_segments) == 2
    for segment in matching_segments:
        assert segment["youtube_video_id"] == "contract-bound-youtube-id"
        segment["youtube_video_id"] = "consistently-forged-youtube-id"
    db.execute("DROP TRIGGER analysis_input_snapshots_limited_update")
    db.execute(
        "UPDATE analysis_input_snapshots SET metadata_json=? WHERE run_id=?",
        (
            json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            prepared.run_id,
        ),
    )

    try:
        accepted = _project(db, prepared)
    except DomainError as error:
        assert error.code == "FORECAST_SOURCE_SNAPSHOT_INVALID"
    else:
        pytest.fail(
            "projection accepted rewritten frozen source identity: "
            f"{accepted.forecasts[0].stable_selection_key}"
        )


@pytest.mark.parametrize(
    "delete_input_text",
    [False, True],
    ids=["untouched", "input-text-deleted"],
)
def test_projection_source_contract_is_independent_of_snapshot_body_retention(
    db, delete_input_text: bool
):
    prepared = _prepare_upstream(
        db, (StatementSpec("body-retention-source", NEWER),)
    )
    before = db.execute(
        """
        SELECT input_text, metadata_json
        FROM analysis_input_snapshots
        WHERE run_id=?
        """,
        (prepared.run_id,),
    ).fetchone()
    if delete_input_text:
        db.execute(
            """
            UPDATE analysis_input_snapshots
            SET input_text=NULL, text_deleted_at=?
            WHERE run_id=?
            """,
            ("2026-08-15T00:00:00.000000Z", prepared.run_id),
        )
    after = db.execute(
        """
        SELECT input_text, metadata_json
        FROM analysis_input_snapshots
        WHERE run_id=?
        """,
        (prepared.run_id,),
    ).fetchone()

    batch = _project(db, prepared)

    assert after["metadata_json"] == before["metadata_json"]
    assert (after["input_text"] is None) is delete_input_text
    assert batch.forecasts[0].stable_selection_key == (
        f"body-retention-source:{prepared.statement_ids[0]:020d}"
    )


def test_internal_projection_requires_outer_transaction(db):
    prepared = _prepare_upstream(db, (StatementSpec("one", NEWER),))

    with pytest.raises(DomainError) as error:
        ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.PERIOD_REVIEW
        )

    assert error.value.code == "FORECAST_PROJECTION_TRANSACTION_REQUIRED"


def test_mapping_correction_routes_projection_to_effective_asset(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "corrected-mapping",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.CORRECT,
            "user",
            "Synthetic correction to TOPIX",
            Asset.TOPIX,
        )
    )

    batch = _project(db, prepared)

    assert len(batch.forecasts) == 1
    assert batch.forecasts[0].asset is Asset.TOPIX
    assert batch.forecasts[0].mapping_kind is MappingKind.DIRECT
    assert batch.forecasts[0].confidence is Confidence.LOW


def test_review_state_hash_is_canonical_and_batch_records_latest_review_heads(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "review-state",
                NEWER,
                period_expression="当面",
                confidence=Confidence.LOW,
            ),
        ),
    )
    service = ForecastProjectionService(db)
    original = service.effective_review_state_hash(prepared.run_id)
    assert service.effective_review_state_hash(prepared.run_id) == original

    period_review_id = PeriodReviewService(db).review(
        prepared.period_ids[0],
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "Synthetic hash period review",
    )
    after_period = service.effective_review_state_hash(prepared.run_id)
    assert after_period != original
    mapping_review_id = MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.CORRECT,
            "user",
            "Synthetic hash mapping correction",
            Asset.TOPIX,
        )
    )
    final_hash = service.effective_review_state_hash(prepared.run_id)
    assert final_hash not in {original, after_period}
    assert service.effective_review_state_hash(prepared.run_id) == final_hash

    batch = _project(db, prepared)

    assert batch.latest_mapping_review_id == mapping_review_id
    assert batch.latest_period_review_id == period_review_id
    assert batch.forecasts[0].asset is Asset.TOPIX
    assert batch.forecasts[0].unknown_period is True


def test_period_review_guard_rejects_nonpositive_explicit_id(db):
    prepared = _prepare_upstream(
        db, (StatementSpec("nonpositive-period-review", NEWER),)
    )

    with pytest.raises(sqlite3.IntegrityError, match="PERIOD_REVIEW_INVALID"):
        db.execute(
            """
            INSERT INTO period_reviews(
                id, period_id, decision, actor, reason, created_at
            ) VALUES (0, ?, 'reject', 'user', ?, ?)
            """,
            (
                prepared.period_ids[0],
                "Synthetic invalid nonpositive review",
                "2026-08-11T03:00:00.000000Z",
            ),
        )


def test_period_review_guard_rejects_id_older_than_existing_for_period(db):
    prepared = _prepare_upstream(
        db, (StatementSpec("out-of-order-period-review", NEWER),)
    )
    values = (
        prepared.period_ids[0],
        "Synthetic valid review",
        "2026-08-11T03:00:00.000000Z",
    )
    db.execute(
        """
        INSERT INTO period_reviews(
            id, period_id, decision, actor, reason, created_at
        ) VALUES (10, ?, 'reject', 'user', ?, ?)
        """,
        values,
    )

    with pytest.raises(sqlite3.IntegrityError, match="PERIOD_REVIEW_INVALID"):
        db.execute(
            """
            INSERT INTO period_reviews(
                id, period_id, decision, actor, reason, created_at
            ) VALUES (5, ?, 'reject', 'user', ?, ?)
            """,
            values,
        )


@pytest.mark.parametrize(
    (
        "malformation",
        "decision",
        "reason",
        "created_at",
    ),
    [
        (
            "blank_reason",
            "reject",
            "   ",
            "2026-08-11T03:00:00.000000Z",
        ),
        (
            "invalid_decision",
            "synthetic_invalid",
            "Synthetic invalid decision",
            "2026-08-11T03:00:00.000000Z",
        ),
        (
            "invalid_time",
            "reject",
            "Synthetic invalid time",
            "2026-02-30T03:00:00.000000Z",
        ),
        (
            "approve_known",
            "approve_unknown",
            "Synthetic invalid known approval",
            "2026-08-11T03:00:00.000000Z",
        ),
    ],
)
def test_review_state_rejects_malformed_older_period_history(
    db,
    malformation: str,
    decision: str,
    reason: str,
    created_at: str,
):
    prepared = _prepare_upstream(
        db, (StatementSpec(f"malformed-period-{malformation}", NEWER),)
    )
    if malformation == "invalid_decision":
        db.execute("PRAGMA ignore_check_constraints = ON")
    if malformation == "approve_known":
        db.execute("DROP TRIGGER period_reviews_approve_requires_unknown")
    db.execute(
        """
        INSERT INTO period_reviews(
            id, period_id, decision, actor, reason, created_at
        ) VALUES (10, ?, ?, 'user', ?, ?)
        """,
        (prepared.period_ids[0], decision, reason, created_at),
    )
    db.execute(
        """
        INSERT INTO period_reviews(
            id, period_id, decision, actor, reason, created_at
        ) VALUES (11, ?, 'reject', 'user', ?, ?)
        """,
        (
            prepared.period_ids[0],
            "Synthetic valid latest review",
            "2026-08-11T04:00:00.000000Z",
        ),
    )

    with pytest.raises(DomainError) as error:
        ForecastProjectionService(db).effective_review_state_hash(
            prepared.run_id
        )

    assert error.value.code == "FORECAST_PERIOD_REVIEW_STORED_INVALID"


def test_latest_period_reject_excludes_known_and_previously_approved_unknown(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("known-rejected", NEWER),
            StatementSpec(
                "unknown-rejected",
                NEWER,
                period_expression="当面",
            ),
        ),
    )
    reviews = PeriodReviewService(db)
    reviews.review(
        prepared.period_ids[0],
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic known rejection",
    )
    reviews.review(
        prepared.period_ids[1],
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "Synthetic unknown approval",
    )
    reviews.review(
        prepared.period_ids[1],
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic latest unknown rejection",
    )

    assert _project(db, prepared).forecasts == ()


def test_distinct_reposts_keep_support_and_counterevidence_links(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("original-down", OLDER, DirectionKind.DOWN),
            StatementSpec(
                "later-up",
                OLDER.replace(hour=4),
                DirectionKind.UP,
            ),
            StatementSpec("up-repost", NEWER, DirectionKind.STRONG_UP),
        ),
    )

    forecast = _project(db, prepared).forecasts[0]

    assert forecast.view_relation is ViewRelation.CHANGED
    assert forecast.supporting_statement_ids == prepared.statement_ids[1:]
    assert forecast.counterevidence_statement_ids == prepared.statement_ids[:1]
    assert forecast.evidence_count == 2


def test_review_state_drift_fails_bound_unit_and_requires_successor(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "drifting-review",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    service = ForecastProjectionService(db)
    original_hash = service.effective_review_state_hash(prepared.run_id)
    jobs = JobStateService(db)
    jobs.begin_unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY, original_hash
    )
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.APPROVE,
            "user",
            "Synthetic review drift",
            None,
        )
    )
    changed_hash = service.effective_review_state_hash(prepared.run_id)
    assert changed_hash != original_hash

    with pytest.raises(DomainError) as error:
        service.project_run(prepared.run_id, ProjectionTrigger.INITIAL)

    assert error.value.code == "UNIT_INPUT_CHANGED"
    assert jobs.unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY
    ).status is UnitStatus.FAILED
    assert db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches"
    ).fetchone()[0] == 0

    jobs.resume(prepared.job_id, _successful_artifacts(db, prepared.job_id))
    with pytest.raises(DomainError) as rebound:
        jobs.begin_unit(
            prepared.job_id, FORECAST_PROJECTION_UNIT_KEY, changed_hash
        )
    assert rebound.value.code == "UNIT_INPUT_CHANGED"
    assert jobs.unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY
    ).status is UnitStatus.PENDING


def test_projection_failure_rolls_back_artifact_and_retry_reuses_upstream(db):
    prepared = _prepare_upstream(db, (StatementSpec("atomic-retry", NEWER),))
    service = ForecastProjectionService(db)
    review_hash = service.effective_review_state_hash(prepared.run_id)
    jobs = JobStateService(db)
    jobs.begin_unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY, review_hash
    )
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_forecast
        BEFORE INSERT ON analysis_forecasts
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_FORECAST_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as error:
        service.project_run(prepared.run_id, ProjectionTrigger.INITIAL)

    assert error.value.code == "FORECAST_PROJECTION_STORAGE_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches"
    ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM analysis_forecasts").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_forecast_statement_links"
    ).fetchone()[0] == 0
    failed = jobs.unit(prepared.job_id, FORECAST_PROJECTION_UNIT_KEY)
    assert failed.status is UnitStatus.FAILED
    assert db.execute(
        """
        SELECT error_code FROM job_units
        WHERE job_id=? AND unit_key=?
        """,
        (prepared.job_id, FORECAST_PROJECTION_UNIT_KEY),
    ).fetchone()[0] == "FORECAST_PROJECTION_STORAGE_FAILED"

    db.execute("DROP TRIGGER synthetic_reject_forecast")
    plan = jobs.resume(
        prepared.job_id, _successful_artifacts(db, prepared.job_id)
    )
    assert plan.next_unit_key == FORECAST_PROJECTION_UNIT_KEY
    jobs.begin_unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY, review_hash
    )
    retried = service.project_run(prepared.run_id, ProjectionTrigger.INITIAL)

    assert len(retried.forecasts) == 1
    assert jobs.unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).attempt_count == 1
    assert jobs.unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    ).attempt_count == 1
    assert jobs.unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    ).attempt_count == 1


def test_success_reuse_verifies_complete_batch_hash_without_duplicate_rows(db):
    prepared = _prepare_upstream(db, (StatementSpec("hash-sealed", NEWER),))
    first = _project(db, prepared)
    jobs = JobStateService(db)
    unit = jobs.unit(prepared.job_id, FORECAST_PROJECTION_UNIT_KEY)

    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == ForecastRepository(db).batch_artifact_hash(first.id)
    second = ForecastProjectionService(db).project_run(
        prepared.run_id, ProjectionTrigger.INITIAL
    )
    assert second == first
    assert db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()[0] == 1
    assert jobs.unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY
    ).attempt_count == 1

    db.execute(
        """
        UPDATE job_units SET output_hash=?
        WHERE job_id=? AND unit_key=?
        """,
        ("f" * 64, prepared.job_id, FORECAST_PROJECTION_UNIT_KEY),
    )
    with pytest.raises(DomainError) as mismatch:
        ForecastProjectionService(db).project_run(
            prepared.run_id, ProjectionTrigger.INITIAL
        )
    assert mismatch.value.code == "FORECAST_PROJECTION_OUTPUT_HASH_MISMATCH"


def test_batch_artifact_seals_exact_statement_link_records(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("sealed-counter", OLDER, DirectionKind.DOWN),
            StatementSpec("sealed-support", NEWER, DirectionKind.UP),
        ),
    )
    batch = _project(db, prepared)
    repository = ForecastRepository(db)

    artifact = repository.batch_artifact(batch.id)
    reloaded = repository.get_batch(batch.id)

    assert artifact["forecasts"][0]["statement_links"] == [
        {
            "statement_id": prepared.statement_ids[1],
            "relation_kind": "supporting",
            "ordinal": 1,
        },
        {
            "statement_id": prepared.statement_ids[0],
            "relation_kind": "counterevidence",
            "ordinal": 1,
        },
    ]
    assert reloaded.forecasts[0].source_forecast_ids == (
        reloaded.forecasts[0].id,
    )


def test_success_reuse_rejects_direct_counter_link_ordinal_gap(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec("gap-support", NEWER),
            StatementSpec(
                "gap-unused",
                NEWER,
                statement_type=StatementType.CURRENT_ANALYSIS,
                forecast_basis=None,
            ),
        ),
    )
    batch = _project(db, prepared)
    db.execute(
        """
        INSERT INTO analysis_forecast_statement_links(
            forecast_id, statement_id, relation_kind, ordinal
        ) VALUES (?, ?, 'counterevidence', 2)
        """,
        (batch.forecasts[0].id, prepared.statement_ids[1]),
    )

    with pytest.raises(DomainError) as error:
        ForecastProjectionService(db).project_run(
            prepared.run_id, ProjectionTrigger.INITIAL
        )

    assert error.value.code == "FORECAST_ARTIFACT_INVALID"


def test_link_ordinal_mutation_cannot_retain_the_sealed_hash(db):
    prepared = _prepare_upstream(
        db, (StatementSpec("mutated-link-ordinal", NEWER),)
    )
    batch = _project(db, prepared)
    repository = ForecastRepository(db)
    sealed_hash = _successful_artifacts(db, prepared.job_id)[
        FORECAST_PROJECTION_UNIT_KEY
    ]
    assert repository.batch_artifact_hash(batch.id) == sealed_hash
    db.execute("DROP TRIGGER analysis_forecast_statement_links_no_update")
    db.execute(
        """
        UPDATE analysis_forecast_statement_links
        SET ordinal=2
        WHERE forecast_id=? AND relation_kind='supporting'
        """,
        (batch.forecasts[0].id,),
    )

    with pytest.raises(DomainError) as error:
        repository.batch_artifact_hash(batch.id)

    assert error.value.code == "FORECAST_ARTIFACT_INVALID"
    assert _successful_artifacts(db, prepared.job_id)[
        FORECAST_PROJECTION_UNIT_KEY
    ] == sealed_hash


def test_internal_review_projection_appends_without_changing_successful_unit(db):
    prepared = _prepare_upstream(db, (StatementSpec("internal-review", NEWER),))
    initial = _project(db, prepared)
    jobs = JobStateService(db)
    unit_before = jobs.unit(prepared.job_id, FORECAST_PROJECTION_UNIT_KEY)
    status_before = jobs.status(prepared.job_id)

    with transaction(db):
        reviewed = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.PERIOD_REVIEW
        )

    assert reviewed.id != initial.id
    assert reviewed.trigger_kind is ProjectionTrigger.PERIOD_REVIEW
    assert jobs.unit(
        prepared.job_id, FORECAST_PROJECTION_UNIT_KEY
    ) == unit_before
    assert jobs.status(prepared.job_id) is status_before

    batch_count = db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()[0]
    with pytest.raises(RuntimeError, match="caller rollback"):
        with transaction(db):
            ForecastProjectionService(db)._project_run_in_transaction(
                prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
            )
            raise RuntimeError("caller rollback")
    assert db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()[0] == batch_count


def test_projection_tables_are_append_only_and_cross_run_links_fail(db):
    first = _prepare_upstream(db, (StatementSpec("first", NEWER),))
    first_batch = _project(db, first)
    second = _prepare_upstream(db, (StatementSpec("second", NEWER),))
    second_batch = _project(db, second)
    forecast_id = first_batch.forecasts[0].id

    for sql in (
        "UPDATE forecast_projection_batches SET created_at=created_at WHERE id=?",
        "DELETE FROM forecast_projection_batches WHERE id=?",
        "UPDATE analysis_forecasts SET evidence_count=evidence_count WHERE id=?",
        "DELETE FROM analysis_forecasts WHERE id=?",
    ):
        target = first_batch.id if "batches" in sql else forecast_id
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            db.execute(sql, (target,))

    with pytest.raises(sqlite3.IntegrityError, match="FORECAST_LINK_OWNERSHIP_MISMATCH"):
        db.execute(
            """
            INSERT INTO analysis_forecast_statement_links(
                forecast_id, statement_id, relation_kind, ordinal
            ) VALUES (?, ?, 'supporting', 99)
            """,
            (second_batch.forecasts[0].id, first.statement_ids[0]),
        )

    link = db.execute(
        """
        SELECT * FROM analysis_forecast_statement_links
        WHERE forecast_id=? ORDER BY relation_kind, ordinal LIMIT 1
        """,
        (forecast_id,),
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            UPDATE analysis_forecast_statement_links
            SET ordinal=ordinal
            WHERE forecast_id=? AND statement_id=?
            """,
            (forecast_id, link["statement_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            DELETE FROM analysis_forecast_statement_links
            WHERE forecast_id=? AND statement_id=?
            """,
            (forecast_id, link["statement_id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_forecast_statement_links(
                forecast_id, statement_id, relation_kind, ordinal
            ) VALUES (?, ?, ?, ?)
            """,
            (
                forecast_id,
                link["statement_id"],
                link["relation_kind"],
                link["ordinal"],
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO forecast_projection_batches(
                id, run_id, trigger_kind, latest_mapping_review_id,
                latest_period_review_id, created_at
            )
            SELECT id, run_id, trigger_kind, latest_mapping_review_id,
                   latest_period_review_id, created_at
            FROM forecast_projection_batches WHERE id=?
            """,
            (first_batch.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_forecasts
            SELECT * FROM analysis_forecasts WHERE id=?
            """,
            (forecast_id,),
        )

    with pytest.raises(sqlite3.IntegrityError, match="FORECAST_BATCH_RUN_MISMATCH"):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                ?, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key || ':cross-batch',
                heatmap_eligible, exclusion_reason
            FROM analysis_forecasts WHERE id=?
            """,
            (second_batch.id, forecast_id),
        )


def test_schema_rejects_impossible_calendar_dates(db):
    prepared = _prepare_upstream(db, (StatementSpec("invalid-date", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, 'xau_usd', mapping_kind,
                '2026-02-30', '2026-02-30', 0,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, 3,
                stable_selection_key || ':invalid-date', 1, NULL
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (batch.id,),
        )


def test_schema_rejects_impossible_selected_publication_timestamp(db):
    prepared = _prepare_upstream(db, (StatementSpec("invalid-timestamp", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, 'xau_usd', mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, '2026-02-30T03:00:00.000000Z',
                selected_forecast_basis, period_specificity,
                stable_selection_key || ':invalid-timestamp', 1, NULL
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (batch.id,),
        )


def test_schema_requires_opposite_direction_arrays_to_be_disagreement(db):
    prepared = _prepare_upstream(db, (StatementSpec("invalid-relation", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError, match="FORECAST_DIRECTIONS_INVALID"):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, 'xau_usd', mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, 'current',
                'up', '["up","down"]', confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key || ':invalid-relation', 1, NULL
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (batch.id,),
        )


@pytest.mark.parametrize(
    (
        "period_start",
        "period_end",
        "unknown_period",
        "condition_kind",
        "condition_text",
        "specificity",
    ),
    [
        ("2026-08-17", "2026-08-23", 1, "unconditional", None, 0),
        (None, None, 0, "unconditional", None, 3),
        ("2026-08-17", "2026-08-23", 0, "conditional", None, 3),
        ("2026-08-17", "2026-08-23", 0, "unconditional", "text", 3),
        ("2026-08-17", "2026-08-23", 0, "unconditional", None, 2),
    ],
)
def test_schema_rejects_inconsistent_period_and_condition_shapes(
    db,
    period_start,
    period_end,
    unknown_period,
    condition_kind,
    condition_text,
    specificity,
):
    prepared = _prepare_upstream(db, (StatementSpec("invalid-shape", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, 'xau_usd', mapping_kind,
                ?, ?, ?, ?, ?, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, ?,
                stable_selection_key || ':invalid-shape', 1, NULL
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (
                period_start,
                period_end,
                unknown_period,
                condition_kind,
                condition_text,
                specificity,
                batch.id,
            ),
        )


def test_schema_prevents_duplicate_comparison_group_in_one_batch(db):
    prepared = _prepare_upstream(db, (StatementSpec("duplicate-group", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key || ':duplicate',
                heatmap_eligible, exclusion_reason
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (batch.id,),
        )


@pytest.mark.parametrize(
    ("directions_json", "view_relation", "primary_direction"),
    [
        ('[ "up" ]', "current", "up"),
        ('["up","up"]', "current", "up"),
        ('["down"]', "current", "up"),
        ('["up"]', "disagreement", "up"),
        ('["sideways"]', "current", "sideways"),
    ],
)
def test_schema_rejects_noncanonical_or_inconsistent_direction_json(
    db, directions_json, view_relation, primary_direction
):
    prepared = _prepare_upstream(db, (StatementSpec("invalid-json", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id, run_id, asset, mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, view_relation,
                primary_direction, directions_json, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key, heatmap_eligible, exclusion_reason
            )
            SELECT
                projection_batch_id, run_id, 'xau_usd', mapping_kind,
                period_start, period_end, unknown_period,
                condition_kind, condition_text, ?,
                ?, ?, confidence,
                evidence_count, selected_published_at,
                selected_forecast_basis, period_specificity,
                stable_selection_key || ':invalid-json', 1, NULL
            FROM analysis_forecasts
            WHERE projection_batch_id=?
            """,
            (
                view_relation,
                primary_direction,
                directions_json,
                batch.id,
            ),
        )

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    PeriodReviewDecision,
    StatementType,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import (
    ForecastCandidate,
    ForecastProjectionBatch,
    ProjectedForecast,
    ProjectionTrigger,
    select_current,
)
from market_voice_forecast_ledger.domain.jobs import (
    ASSET_MAPPING_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.domain.mappings import AssetMapping
from market_voice_forecast_ledger.domain.periods import (
    EffectivePeriodReview,
    NormalizedPeriod,
)
from market_voice_forecast_ledger.domain.statements import NormalizedStatement
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.forecasts import ForecastRepository
from market_voice_forecast_ledger.repositories.mappings import MappingRepository
from market_voice_forecast_ledger.repositories.periods import PeriodRepository
from market_voice_forecast_ledger.repositories.statements import StatementRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewService,
)


_ASSET_ORDER = {asset: ordinal for ordinal, asset in enumerate(Asset)}
_FROZEN_METADATA_KEYS = frozenset(
    {
        "cutoff_day_jst",
        "cutoff_exclusive_utc",
        "input_sha256",
        "interviewer_market_context",
        "segments",
        "settings",
        "subject_id",
    }
)
_FROZEN_SEGMENT_KEYS = frozenset(
    {
        "assignment_evidence_hash",
        "assignment_kind",
        "assignment_origin",
        "assignment_updated_at",
        "assigned_subject_id",
        "channel_display_name",
        "end_ms",
        "metadata_snapshot_hash",
        "metadata_snapshot_id",
        "presence_decision_hash",
        "presence_decision_id",
        "published_at",
        "segment_id",
        "segment_no",
        "speaker_assignment_id",
        "start_ms",
        "text_sha256",
        "title",
        "video_id",
        "youtube_channel_id",
        "youtube_video_id",
    }
)


@dataclass(frozen=True, slots=True)
class _ReviewState:
    digest: str
    latest_mapping_review_id: int | None
    latest_period_review_id: int | None


@dataclass(frozen=True, slots=True)
class _ProjectionGroup:
    subject_id: int
    asset: Asset
    period_start: date | None
    period_end: date | None
    unknown_period: bool
    condition_kind: ConditionKind
    condition_text: str | None
    candidates: tuple[ForecastCandidate, ...]


class ForecastProjectionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._statements = StatementRepository(conn)
        self._periods = PeriodRepository(conn)
        self._mappings = MappingRepository(conn)
        self._mapping_reviews = MappingReviewService(conn)
        self._forecasts = ForecastRepository(conn)
        self._job_state = JobStateService(conn, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def effective_review_state_hash(self, run_id: int) -> str:
        return self._review_state(run_id).digest

    def project_run(
        self, run_id: int, trigger_kind: ProjectionTrigger
    ) -> ForecastProjectionBatch:
        if trigger_kind is not ProjectionTrigger.INITIAL:
            raise DomainError(
                "FORECAST_PROJECTION_TRIGGER_INVALID",
                "public projection accepts only the initial trigger",
            )
        run = self._analysis.get_run(run_id)
        unit = self._job_state.unit(
            run.active_job_id, FORECAST_PROJECTION_UNIT_KEY
        )
        if unit.status is UnitStatus.SUCCESS:
            batch = self._forecasts.initial_batch(run_id)
            artifact_hash = self._forecasts.batch_artifact_hash(batch.id)
            if unit.output_hash != artifact_hash:
                raise DomainError(
                    "FORECAST_PROJECTION_OUTPUT_HASH_MISMATCH",
                    "stored forecasts do not match the successful unit",
                )
            return batch
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "FORECAST_PROJECTION_UNIT_NOT_RUNNING",
                "initial projection requires a running unit",
            )

        try:
            self._require_initial_contract(run_id, run.active_job_id)
            with transaction(self._conn):
                current_run = self._analysis.get_run(run_id)
                if current_run.active_job_id != run.active_job_id:
                    raise DomainError(
                        "FORECAST_PROJECTION_UNIT_NOT_OWNED",
                        "projection unit must belong to the active run attempt",
                    )
                self._require_initial_contract(run_id, current_run.active_job_id)
                batch = self._project_run_in_transaction(
                    run_id, ProjectionTrigger.INITIAL
                )
                output_hash = self._forecasts.batch_artifact_hash(batch.id)
                self._job_state.complete_unit_in_transaction(
                    current_run.active_job_id,
                    FORECAST_PROJECTION_UNIT_KEY,
                    output_hash,
                )
        except DomainError as error:
            self._record_failure(run.active_job_id, error.code)
            raise
        except (sqlite3.DatabaseError, RuntimeError) as cause:
            error = DomainError(
                "FORECAST_PROJECTION_STORAGE_FAILED",
                "forecast projection could not be stored",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause
        return self._forecasts.get_batch(batch.id)

    def _project_run_in_transaction(
        self, run_id: int, trigger_kind: ProjectionTrigger
    ) -> ForecastProjectionBatch:
        if not self._conn.in_transaction:
            raise DomainError(
                "FORECAST_PROJECTION_TRANSACTION_REQUIRED",
                "internal projection requires a caller-owned transaction",
            )
        if type(trigger_kind) is not ProjectionTrigger:
            raise DomainError(
                "FORECAST_PROJECTION_TRIGGER_INVALID",
                "projection trigger is invalid",
            )
        run = self._analysis.get_run(run_id)
        scope = self._analysis.get_scope(run.scope_id)
        review_state = self._review_state(run_id)
        created_at = self._clock()
        groups = self._eligible_groups(run_id, scope.subject_id)
        batch_id = self._forecasts.insert_batch(
            run_id,
            trigger_kind,
            review_state.latest_mapping_review_id,
            review_state.latest_period_review_id,
            created_at,
        )
        for group in groups:
            resolved = select_current(group.candidates)
            forecast = ProjectedForecast(
                id=None,
                projection_batch_id=batch_id,
                run_id=run_id,
                subject_id=group.subject_id,
                asset=group.asset,
                mapping_kind=resolved.mapping_kind,
                period_start=group.period_start,
                period_end=group.period_end,
                unknown_period=group.unknown_period,
                condition_kind=group.condition_kind,
                condition_text=group.condition_text,
                view_relation=resolved.view_relation,
                primary_direction=resolved.primary_direction,
                directions=resolved.directions,
                confidence=resolved.confidence,
                evidence_count=resolved.evidence_count,
                selected_published_at=resolved.selected_published_at,
                selected_forecast_basis=resolved.selected_forecast_basis,
                period_specificity=resolved.period_specificity,
                stable_selection_key=resolved.stable_selection_key,
                heatmap_eligible=True,
                exclusion_reason=None,
                supporting_statement_ids=resolved.supporting_statement_ids,
                counterevidence_statement_ids=(
                    resolved.counterevidence_statement_ids
                ),
            )
            self._forecasts.insert_forecast(forecast)
        return self._forecasts.get_batch(batch_id)

    def _require_initial_contract(self, run_id: int, job_id: int) -> None:
        current_hash = self.effective_review_state_hash(run_id)
        unit = self._job_state.unit(job_id, FORECAST_PROJECTION_UNIT_KEY)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "FORECAST_PROJECTION_UNIT_NOT_RUNNING",
                "initial projection requires a running unit",
            )
        if unit.external_input_hash != current_hash:
            raise DomainError(
                "UNIT_INPUT_CHANGED",
                "projection review state changed after the unit was bound",
            )
        for unit_key in (
            STATEMENT_NORMALIZATION_UNIT_KEY,
            PERIOD_NORMALIZATION_UNIT_KEY,
            ASSET_MAPPING_UNIT_KEY,
        ):
            upstream = self._job_state.unit(job_id, unit_key)
            if (
                upstream.status is not UnitStatus.SUCCESS
                or upstream.output_hash is None
            ):
                raise DomainError(
                    "FORECAST_PROJECTION_UPSTREAM_INCOMPLETE",
                    "projection requires successful statement, period, and mapping units",
                )
        self._job_state.require_upstream_success(
            job_id, FORECAST_PROJECTION_UNIT_KEY
        )

    def _eligible_groups(
        self, run_id: int, subject_id: int
    ) -> tuple[_ProjectionGroup, ...]:
        statements = self._statements.list_run_statements(run_id)
        periods = {
            period.statement_id: period
            for period in self._periods.list_run_periods(run_id)
        }
        mappings_by_statement: dict[int, list[AssetMapping]] = {}
        for mapping in self._mappings.list_run_mappings(run_id):
            mappings_by_statement.setdefault(mapping.statement_id, []).append(
                mapping
            )
        source_metadata = self._frozen_source_metadata(run_id)

        grouped: dict[
            tuple[
                Asset,
                date | None,
                date | None,
                bool,
                ConditionKind,
                str | None,
            ],
            list[ForecastCandidate],
        ] = {}
        for statement in statements:
            if (
                statement.statement_type is not StatementType.FUTURE_FORECAST
                or not statement.heatmap_candidate
            ):
                continue
            if statement.forecast_basis is None or statement.direction_kind is None:
                raise DomainError(
                    "FORECAST_STATEMENT_INVALID",
                    "future forecast is missing its basis or direction",
                )
            condition_text = self._condition_text(statement)
            period = periods.get(statement.id)
            if period is None:
                raise DomainError(
                    "FORECAST_PERIOD_MISSING",
                    "future forecast does not have a normalized period",
                )
            if not self._period_is_eligible(period):
                continue
            published_at, youtube_video_id = source_metadata.get(
                statement.source_video_id, (None, None)
            )
            if published_at is None or youtube_video_id is None:
                raise DomainError(
                    "FORECAST_SOURCE_MISSING",
                    "forecast source is not present in the frozen run",
                )
            for mapping in mappings_by_statement.get(statement.id, ()):
                effective = self._mapping_reviews.effective(mapping.id)
                if not effective.heatmap_eligible:
                    continue
                key = (
                    effective.asset,
                    period.start_date,
                    period.end_date,
                    period.is_unknown,
                    statement.condition_kind,
                    condition_text,
                )
                grouped.setdefault(key, []).append(
                    ForecastCandidate(
                        statement_id=statement.id,
                        youtube_video_id=youtube_video_id,
                        published_at=published_at,
                        direction=statement.direction_kind,
                        forecast_basis=statement.forecast_basis,
                        period_specificity=_period_specificity(period),
                        mapping_kind=mapping.mapping_kind,
                        confidence=mapping.final_confidence,
                    )
                )

        return tuple(
            _ProjectionGroup(
                subject_id=subject_id,
                asset=key[0],
                period_start=key[1],
                period_end=key[2],
                unknown_period=key[3],
                condition_kind=key[4],
                condition_text=key[5],
                candidates=tuple(candidates),
            )
            for key, candidates in sorted(
                grouped.items(), key=lambda item: _group_sort_key(item[0])
            )
        )

    def _frozen_source_metadata(
        self, run_id: int
    ) -> dict[int, tuple[datetime, str]]:
        try:
            snapshot = self._analysis.get_snapshot(run_id)
            run = self._analysis.get_run(run_id)
            raw_metadata = snapshot.metadata_json
            if (
                not isinstance(raw_metadata, str)
                or sha256_text(raw_metadata) != run.input_contract_hash
            ):
                raise ValueError
            metadata = json.loads(raw_metadata)
            if (
                not isinstance(metadata, dict)
                or set(metadata) != _FROZEN_METADATA_KEYS
                or not isinstance(metadata["segments"], list)
            ):
                raise ValueError
            run_segments = {
                segment.segment_id: segment
                for segment in self._analysis.get_input_segments(run_id)
            }
            raw_segments = metadata["segments"]
            if len(run_segments) != len(raw_segments):
                raise ValueError

            seen_segment_ids: set[int] = set()
            frozen_by_video: dict[int, tuple[datetime, str]] = {}
            video_by_youtube_id: dict[str, int] = {}
            for item in raw_segments:
                if (
                    not isinstance(item, dict)
                    or set(item) != _FROZEN_SEGMENT_KEYS
                ):
                    raise ValueError
                segment_id = item["segment_id"]
                video_id = item["video_id"]
                youtube_video_id = item["youtube_video_id"]
                if (
                    not _is_positive_int(segment_id)
                    or segment_id in seen_segment_ids
                    or not _is_positive_int(video_id)
                    or not isinstance(youtube_video_id, str)
                    or not youtube_video_id
                    or youtube_video_id.strip() != youtube_video_id
                ):
                    raise ValueError
                published_at = _parse_exact_utc(item["published_at"])
                run_segment = run_segments.get(segment_id)
                if (
                    run_segment is None
                    or run_segment.video_id != video_id
                    or run_segment.published_at != published_at
                ):
                    raise ValueError

                existing = frozen_by_video.setdefault(
                    video_id, (published_at, youtube_video_id)
                )
                if existing != (published_at, youtube_video_id):
                    raise ValueError
                existing_video_id = video_by_youtube_id.setdefault(
                    youtube_video_id, video_id
                )
                if existing_video_id != video_id:
                    raise ValueError
                seen_segment_ids.add(segment_id)
            if seen_segment_ids != set(run_segments):
                raise ValueError
            return frozen_by_video
        except (json.JSONDecodeError, KeyError, LookupError, TypeError, ValueError) as cause:
            raise DomainError(
                "FORECAST_SOURCE_SNAPSHOT_INVALID",
                "frozen source metadata does not match the analysis run",
            ) from cause

    def _period_is_eligible(
        self,
        period: NormalizedPeriod,
        history: tuple[EffectivePeriodReview, ...] | None = None,
    ) -> bool:
        reviews = (
            self._validated_period_review_history(period)
            if history is None
            else history
        )
        review = reviews[-1] if reviews else None
        if period.is_unknown:
            return (
                review is not None
                and review.decision is PeriodReviewDecision.APPROVE_UNKNOWN
            )
        return review is None or review.decision is not PeriodReviewDecision.REJECT

    def _validated_period_review_history(
        self, period: NormalizedPeriod
    ) -> tuple[EffectivePeriodReview, ...]:
        try:
            if not _is_positive_int(period.id):
                raise ValueError
            rows = self._conn.execute(
                """
                SELECT id, period_id, decision, actor, reason, created_at
                FROM period_reviews
                WHERE period_id=?
                ORDER BY id
                """,
                (period.id,),
            ).fetchall()
            reviews: list[EffectivePeriodReview] = []
            previous_id = 0
            for row in rows:
                review_id = row["id"]
                period_id = row["period_id"]
                actor = row["actor"]
                reason = row["reason"]
                if (
                    not _is_positive_int(review_id)
                    or review_id <= previous_id
                    or period_id != period.id
                    or not isinstance(actor, str)
                    or not actor.strip()
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    raise ValueError
                decision = PeriodReviewDecision(row["decision"])
                created_at = _parse_exact_utc(row["created_at"])
                if (
                    decision is PeriodReviewDecision.APPROVE_UNKNOWN
                    and not period.is_unknown
                ):
                    raise ValueError
                reviews.append(
                    EffectivePeriodReview(
                        id=review_id,
                        period_id=period_id,
                        decision=decision,
                        actor=actor,
                        reason=reason,
                        created_at=created_at,
                        period_is_unknown=period.is_unknown,
                    )
                )
                previous_id = review_id
            return tuple(reviews)
        except (KeyError, sqlite3.DatabaseError, TypeError, ValueError) as cause:
            raise DomainError(
                "FORECAST_PERIOD_REVIEW_STORED_INVALID",
                "stored period review history is invalid for projection",
            ) from cause

    @staticmethod
    def _condition_text(statement: NormalizedStatement) -> str | None:
        if statement.condition_kind is ConditionKind.UNCONDITIONAL:
            if statement.condition_text is not None:
                raise DomainError(
                    "FORECAST_CONDITION_INVALID",
                    "unconditional forecast cannot carry condition text",
                )
            return None
        if not statement.condition_text:
            raise DomainError(
                "FORECAST_CONDITION_INVALID",
                "conditional forecast requires condition text",
            )
        return statement.condition_text

    def _review_state(self, run_id: int) -> _ReviewState:
        self._analysis.get_run(run_id)
        period_payload: list[dict[str, object]] = []
        latest_period_ids: list[int] = []
        for period in self._periods.list_run_periods(run_id):
            history = self._validated_period_review_history(period)
            review = history[-1] if history else None
            if review is not None:
                latest_period_ids.append(review.id)
            period_payload.append(
                {
                    "period_id": period.id,
                    "statement_id": period.statement_id,
                    "latest_review_id": None if review is None else review.id,
                    "latest_decision": (
                        None if review is None else review.decision.value
                    ),
                    "effective_start": (
                        None
                        if period.start_date is None
                        else period.start_date.isoformat()
                    ),
                    "effective_end": (
                        None
                        if period.end_date is None
                        else period.end_date.isoformat()
                    ),
                    "effective_unknown": period.is_unknown,
                    "effective_eligible": self._period_is_eligible(
                        period, history
                    ),
                }
            )

        mapping_payload: list[dict[str, object]] = []
        latest_mapping_ids: list[int] = []
        for mapping in self._mappings.list_run_mappings(run_id):
            row = self._conn.execute(
                """
                SELECT id, decision
                FROM mapping_reviews
                WHERE mapping_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (mapping.id,),
            ).fetchone()
            effective = self._mapping_reviews.effective(mapping.id)
            if row is not None:
                latest_mapping_ids.append(row["id"])
            mapping_payload.append(
                {
                    "mapping_id": mapping.id,
                    "statement_id": mapping.statement_id,
                    "latest_review_id": None if row is None else row["id"],
                    "latest_decision": None if row is None else row["decision"],
                    "effective_asset": effective.asset.value,
                    "effective_eligible": effective.heatmap_eligible,
                }
            )
        payload = {"mappings": mapping_payload, "periods": period_payload}
        return _ReviewState(
            digest=sha256_text(canonical_json(payload)),
            latest_mapping_review_id=(
                max(latest_mapping_ids) if latest_mapping_ids else None
            ),
            latest_period_review_id=(
                max(latest_period_ids) if latest_period_ids else None
            ),
        )

    def _record_failure(self, job_id: int, error_code: str) -> None:
        unit = self._job_state.unit(job_id, FORECAST_PROJECTION_UNIT_KEY)
        if unit.status is UnitStatus.RUNNING:
            self._job_state.fail_unit(
                job_id, FORECAST_PROJECTION_UNIT_KEY, error_code
            )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_exact_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if utc_iso(parsed) != value:
        raise ValueError
    return parsed


def _period_specificity(period: NormalizedPeriod) -> int:
    if period.is_unknown:
        return 0
    if period.start_date is None or period.end_date is None:
        raise DomainError(
            "FORECAST_PERIOD_INVALID",
            "known forecast period requires both dates",
        )
    days = (period.end_date - period.start_date).days + 1
    if days <= 0:
        raise DomainError(
            "FORECAST_PERIOD_INVALID", "forecast period dates are reversed"
        )
    if days <= 7:
        return 3
    if days <= 31:
        return 2
    return 1


def _group_sort_key(
    key: tuple[
        Asset,
        date | None,
        date | None,
        bool,
        ConditionKind,
        str | None,
    ],
) -> tuple[int, int, str, str, str, str]:
    asset, start, end, unknown, condition_kind, condition_text = key
    return (
        _ASSET_ORDER[asset],
        int(unknown),
        "" if start is None else start.isoformat(),
        "" if end is None else end.isoformat(),
        condition_kind.value,
        "" if condition_text is None else condition_text,
    )

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    PeriodReviewDecision,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.domain.periods import (
    EffectivePeriodReview,
    NormalizedPeriod,
    normalize_period,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.periods import PeriodRepository
from market_voice_forecast_ledger.repositories.statements import (
    StatementRepository,
)
from market_voice_forecast_ledger.services.job_state import JobStateService


@dataclass(frozen=True, slots=True)
class _ResolvedPeriod:
    statement_id: int
    period: NormalizedPeriod


class PeriodService:
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
        self._job_state = JobStateService(conn, clock=clock)

    def normalize_run(self, run_id: int) -> tuple[NormalizedPeriod, ...]:
        run = self._analysis.get_run(run_id)
        unit = self._job_state.unit(
            run.active_job_id, PERIOD_NORMALIZATION_UNIT_KEY
        )
        if unit.status is UnitStatus.SUCCESS:
            return self._periods.list_run_periods(run_id)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "PERIOD_NORMALIZATION_UNIT_NOT_RUNNING",
                "period normalization requires a running unit",
            )

        try:
            with transaction(self._conn):
                current_run = self._analysis.get_run(run_id)
                if current_run.active_job_id != run.active_job_id:
                    raise DomainError(
                        "PERIOD_NORMALIZATION_UNIT_NOT_OWNED",
                        "period unit must belong to the active run attempt",
                    )
                current_unit = self._job_state.unit(
                    current_run.active_job_id,
                    PERIOD_NORMALIZATION_UNIT_KEY,
                )
                if current_unit.status is not UnitStatus.RUNNING:
                    raise DomainError(
                        "PERIOD_NORMALIZATION_UNIT_NOT_RUNNING",
                        "period normalization requires a running unit",
                    )
                statement_unit = self._job_state.unit(
                    current_run.active_job_id,
                    STATEMENT_NORMALIZATION_UNIT_KEY,
                )
                if (
                    statement_unit.status is not UnitStatus.SUCCESS
                    or statement_unit.output_hash is None
                ):
                    raise DomainError(
                        "STATEMENT_NORMALIZATION_NOT_SUCCESSFUL",
                        "period normalization requires successful statements",
                    )

                resolved = self._resolve_periods(run_id)
                for item in resolved:
                    self._periods.insert(item.statement_id, item.period)
                output_hash = sha256_text(
                    canonical_json(self._artifact_payload(resolved))
                )
                self._job_state.complete_unit_in_transaction(
                    current_run.active_job_id,
                    PERIOD_NORMALIZATION_UNIT_KEY,
                    output_hash,
                )
        except DomainError as error:
            self._record_failure(run.active_job_id, error.code)
            raise
        except (OverflowError, ValueError) as cause:
            error = DomainError(
                "PERIOD_NORMALIZATION_FAILED",
                "period expressions could not be normalized",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause
        except (sqlite3.DatabaseError, RuntimeError) as cause:
            error = DomainError(
                "PERIOD_STORAGE_FAILED",
                "normalized periods could not be stored",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause

        return self._periods.list_run_periods(run_id)

    def _resolve_periods(self, run_id: int) -> tuple[_ResolvedPeriod, ...]:
        published_by_video: dict[int, datetime] = {}
        for segment in self._analysis.get_input_segments(run_id):
            existing = published_by_video.setdefault(
                segment.video_id, segment.published_at
            )
            if existing != segment.published_at:
                raise DomainError(
                    "PERIOD_SOURCE_TIMESTAMP_CONFLICT",
                    "one source video has conflicting frozen timestamps",
                )

        resolved: list[_ResolvedPeriod] = []
        for statement in self._statements.list_run_statements(run_id):
            published_at = published_by_video.get(statement.source_video_id)
            if published_at is None:
                raise DomainError(
                    "PERIOD_SOURCE_TIMESTAMP_MISSING",
                    "statement source timestamp is unavailable",
                )
            resolved.append(
                _ResolvedPeriod(
                    statement.id,
                    normalize_period(statement.period_expression, published_at),
                )
            )
        return tuple(resolved)

    @staticmethod
    def _artifact_payload(
        periods: tuple[_ResolvedPeriod, ...]
    ) -> list[dict[str, object]]:
        return [
            {
                "statement_id": item.statement_id,
                "source_expression": item.period.source_expression,
                "start_date": (
                    None
                    if item.period.start_date is None
                    else item.period.start_date.isoformat()
                ),
                "end_date": (
                    None
                    if item.period.end_date is None
                    else item.period.end_date.isoformat()
                ),
                "time_basis": (
                    None
                    if item.period.time_basis is None
                    else item.period.time_basis.value
                ),
                "basis_published_at": (
                    None
                    if item.period.basis_published_at is None
                    else utc_iso(item.period.basis_published_at)
                ),
                "is_unknown": item.period.is_unknown,
            }
            for item in periods
        ]

    def _record_failure(self, job_id: int, error_code: str) -> None:
        unit = self._job_state.unit(job_id, PERIOD_NORMALIZATION_UNIT_KEY)
        if unit.status is UnitStatus.RUNNING:
            self._job_state.fail_unit(
                job_id, PERIOD_NORMALIZATION_UNIT_KEY, error_code
            )


class PeriodReviewService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._periods = PeriodRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def review(
        self,
        period_id: int,
        decision: PeriodReviewDecision,
        actor: str,
        reason: str,
    ) -> int:
        if (
            not isinstance(decision, PeriodReviewDecision)
            or not isinstance(actor, str)
            or not actor.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise DomainError(
                "PERIOD_REVIEW_INVALID",
                "period review requires a decision, actor, and reason",
            )
        period = self._periods.get(period_id)
        if (
            decision is PeriodReviewDecision.APPROVE_UNKNOWN
            and not period.is_unknown
        ):
            raise DomainError(
                "PERIOD_REVIEW_INVALID",
                "only an unknown period can use the unknown column",
            )

        try:
            with transaction(self._conn):
                current_period = self._periods.get(period_id)
                if (
                    decision is PeriodReviewDecision.APPROVE_UNKNOWN
                    and not current_period.is_unknown
                ):
                    raise DomainError(
                        "PERIOD_REVIEW_INVALID",
                        "only an unknown period can use the unknown column",
                    )
                created_at = self._clock()
                review_id = self._periods.insert_review(
                    period_id,
                    decision,
                    actor,
                    reason,
                    created_at,
                )
                self._audit.append(
                    AuditEventInput(
                        entity_type="analysis_statement_period",
                        entity_id=str(period_id),
                        scope_id=self._periods.scope_id(period_id),
                        operation="review",
                        actor_kind=(
                            actor if actor in {"user", "system"} else "user"
                        ),
                        reason_code=decision.value,
                        reason_text=reason,
                        before=None,
                        after={
                            "period_id": period_id,
                            "decision": decision.value,
                            "actor": actor,
                            "reason": reason,
                        },
                        created_at=created_at,
                    )
                )
                return review_id
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError) as cause:
            raise DomainError(
                "PERIOD_REVIEW_STORAGE_FAILED",
                "period review could not be stored",
            ) from cause

    def effective(
        self, period_id: int
    ) -> EffectivePeriodReview | None:
        return self._periods.latest_review(period_id)

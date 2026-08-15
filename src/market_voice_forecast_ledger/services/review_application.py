import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.enums import PeriodReviewDecision
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.repositories.mappings import MappingRepository
from market_voice_forecast_ledger.repositories.periods import PeriodRepository
from market_voice_forecast_ledger.services.current_results import (
    CurrentResultService,
    CurrentResultSummary,
)
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from market_voice_forecast_ledger.services.periods import PeriodReviewService


@dataclass(frozen=True, slots=True)
class ReviewApplicationResult:
    applied_to_current: bool
    current_summary: CurrentResultSummary | None
    rebuilt_cell_count: int


class ReviewApplicationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mappings = MappingRepository(conn)
        self._periods = PeriodRepository(conn)
        self._mapping_reviews = MappingReviewService(conn, clock=self._clock)
        self._period_reviews = PeriodReviewService(conn, clock=self._clock)
        self._projection = ForecastProjectionService(conn, clock=self._clock)
        self._current = CurrentResultService(conn, clock=self._clock)
        self._heatmap = HeatmapService(conn)

    def apply_mapping(
        self, command: MappingReviewCommand
    ) -> ReviewApplicationResult:
        MappingReviewService._validate_command(command)
        mapping = self._mappings.get(command.mapping_id)
        header = self._current_header(mapping.run_id)
        if header is None:
            self._mapping_reviews.review(command)
            return ReviewApplicationResult(
                applied_to_current=False,
                current_summary=None,
                rebuilt_cell_count=0,
            )
        try:
            with transaction(self._conn):
                header = self._require_same_header(mapping.run_id, header)
                before = self._current.get_scope(header["scope_id"])
                self._mapping_reviews._review_in_transaction(command)
                batch = self._projection._project_run_in_transaction(
                    mapping.run_id, ProjectionTrigger.MAPPING_REVIEW
                )
                delta = self._current._replace_accepted_review_rows_in_transaction(
                    mapping.run_id,
                    batch.id,
                    header["projection_batch_id"],
                    ProjectionTrigger.MAPPING_REVIEW,
                )
                if delta.before != before:
                    raise DomainError(
                        "REVIEW_APPLICATION_CONFLICT",
                        "current mapping review target changed concurrently",
                    )
                cell_count = self._heatmap._rebuild_scope_in_transaction(
                    header["scope_id"]
                )
                self._current._append_result_replacement_audit(
                    header["scope_id"],
                    delta.before,
                    delta.after,
                    cell_count,
                    "mapping_review",
                    self._clock(),
                )
                return ReviewApplicationResult(
                    applied_to_current=True,
                    current_summary=delta.after,
                    rebuilt_cell_count=cell_count,
                )
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "REVIEW_APPLICATION_FAILED",
                "mapping review application could not be committed",
            ) from cause

    def apply_period(
        self,
        period_id: int,
        decision: PeriodReviewDecision,
        actor: str,
        reason: str,
    ) -> ReviewApplicationResult:
        PeriodReviewService._validate_command(
            period_id, decision, actor, reason
        )
        self._periods.get(period_id)
        run_id = self._period_run_id(period_id)
        header = self._current_header(run_id)
        if header is None:
            self._period_reviews.review(
                period_id, decision, actor, reason
            )
            return ReviewApplicationResult(
                applied_to_current=False,
                current_summary=None,
                rebuilt_cell_count=0,
            )
        try:
            with transaction(self._conn):
                header = self._require_same_header(run_id, header)
                before = self._current.get_scope(header["scope_id"])
                self._period_reviews._review_in_transaction(
                    period_id, decision, actor, reason
                )
                batch = self._projection._project_run_in_transaction(
                    run_id, ProjectionTrigger.PERIOD_REVIEW
                )
                delta = self._current._replace_accepted_review_rows_in_transaction(
                    run_id,
                    batch.id,
                    header["projection_batch_id"],
                    ProjectionTrigger.PERIOD_REVIEW,
                )
                if delta.before != before:
                    raise DomainError(
                        "REVIEW_APPLICATION_CONFLICT",
                        "current period review target changed concurrently",
                    )
                cell_count = self._heatmap._rebuild_scope_in_transaction(
                    header["scope_id"]
                )
                self._current._append_result_replacement_audit(
                    header["scope_id"],
                    delta.before,
                    delta.after,
                    cell_count,
                    "period_review",
                    self._clock(),
                )
                return ReviewApplicationResult(
                    applied_to_current=True,
                    current_summary=delta.after,
                    rebuilt_cell_count=cell_count,
                )
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "REVIEW_APPLICATION_FAILED",
                "period review application could not be committed",
            ) from cause

    def _current_header(self, run_id: int) -> sqlite3.Row | None:
        rows = self._conn.execute(
            """
            SELECT scope_id, source_run_id, projection_batch_id
            FROM current_result_sets
            WHERE source_run_id=?
            """,
            (run_id,),
        ).fetchall()
        if len(rows) > 1:
            raise DomainError(
                "REVIEW_APPLICATION_CONFLICT",
                "review target has multiple current result headers",
            )
        return None if not rows else rows[0]

    def _require_same_header(
        self, run_id: int, expected: sqlite3.Row
    ) -> sqlite3.Row:
        current = self._current_header(run_id)
        if current is None or tuple(current) != tuple(expected):
            raise DomainError(
                "REVIEW_APPLICATION_CONFLICT",
                "review target changed before application",
            )
        return current

    def _period_run_id(self, period_id: int) -> int:
        row = self._conn.execute(
            """
            SELECT statement.run_id
            FROM analysis_statement_periods AS period
            JOIN analysis_statements AS statement
                ON statement.id=period.statement_id
            WHERE period.id=?
            """,
            (period_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "PERIOD_REVIEW_INVALID",
                "period review requires an existing period",
            )
        return row["run_id"]

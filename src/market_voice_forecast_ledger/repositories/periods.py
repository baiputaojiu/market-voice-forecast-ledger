import sqlite3
from datetime import date, datetime

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import (
    PeriodReviewDecision,
    TimeBasis,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.periods import (
    EffectivePeriodReview,
    NormalizedPeriod,
)


class PeriodRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, statement_id: int, period: NormalizedPeriod) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_statement_periods(
                statement_id,
                source_expression,
                start_date,
                end_date,
                time_basis,
                basis_published_at,
                is_unknown
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement_id,
                period.source_expression,
                _date_or_none(period.start_date),
                _date_or_none(period.end_date),
                (
                    None
                    if period.time_basis is None
                    else period.time_basis.value
                ),
                (
                    None
                    if period.basis_published_at is None
                    else utc_iso(period.basis_published_at)
                ),
                int(period.is_unknown),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("period insert did not return an id")
        return cursor.lastrowid

    def get(self, period_id: int) -> NormalizedPeriod:
        row = self._conn.execute(
            "SELECT * FROM analysis_statement_periods WHERE id=?",
            (period_id,),
        ).fetchone()
        if row is None:
            raise DomainError("PERIOD_NOT_FOUND", "period does not exist")
        return _period_from_row(row)

    def list_run_periods(self, run_id: int) -> tuple[NormalizedPeriod, ...]:
        rows = self._conn.execute(
            """
            SELECT period.*
            FROM analysis_statement_periods AS period
            JOIN analysis_statements AS statement
                ON statement.id=period.statement_id
            WHERE statement.run_id=?
            ORDER BY statement.ordinal
            """,
            (run_id,),
        ).fetchall()
        return tuple(_period_from_row(row) for row in rows)

    def scope_id(self, period_id: int) -> int:
        row = self._conn.execute(
            """
            SELECT run.scope_id
            FROM analysis_statement_periods AS period
            JOIN analysis_statements AS statement
                ON statement.id=period.statement_id
            JOIN analysis_runs AS run ON run.id=statement.run_id
            WHERE period.id=?
            """,
            (period_id,),
        ).fetchone()
        if row is None:
            raise DomainError("PERIOD_NOT_FOUND", "period does not exist")
        return row["scope_id"]

    def insert_review(
        self,
        period_id: int,
        decision: PeriodReviewDecision,
        actor: str,
        reason: str,
        created_at: datetime,
    ) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO period_reviews(
                period_id, decision, actor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                period_id,
                decision.value,
                actor,
                reason,
                utc_iso(created_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("period review insert did not return an id")
        return cursor.lastrowid

    def latest_review(
        self, period_id: int
    ) -> EffectivePeriodReview | None:
        period = self.get(period_id)
        row = self._conn.execute(
            """
            SELECT *
            FROM period_reviews
            WHERE period_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (period_id,),
        ).fetchone()
        if row is None:
            return None
        return EffectivePeriodReview(
            id=row["id"],
            period_id=row["period_id"],
            decision=PeriodReviewDecision(row["decision"]),
            actor=row["actor"],
            reason=row["reason"],
            created_at=_parse_utc(row["created_at"]),
            period_is_unknown=period.is_unknown,
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "PERIOD_TRANSACTION_REQUIRED",
                "period mutation requires an active caller transaction",
            )


def _period_from_row(row: sqlite3.Row) -> NormalizedPeriod:
    return NormalizedPeriod(
        start_date=_parse_date(row["start_date"]),
        end_date=_parse_date(row["end_date"]),
        time_basis=(
            None
            if row["time_basis"] is None
            else TimeBasis(row["time_basis"])
        ),
        source_expression=row["source_expression"],
        is_unknown=bool(row["is_unknown"]),
        basis_published_at=(
            None
            if row["basis_published_at"] is None
            else _parse_utc(row["basis_published_at"])
        ),
        id=row["id"],
        statement_id=row["statement_id"],
    )


def _date_or_none(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

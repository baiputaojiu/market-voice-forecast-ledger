import sqlite3
from collections.abc import Sequence
from datetime import datetime

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import (
    ConditionKind,
    DirectionKind,
    ForecastBasis,
    StatementType,
    TurningPointKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.statements import (
    EvidenceLink,
    NormalizedStatement,
)


class StatementRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_statement(
        self,
        *,
        run_id: int,
        ordinal: int,
        batch_ordinal: int,
        proposal_ordinal: int,
        source_video_id: int,
        statement_type: StatementType,
        forecast_basis: ForecastBasis | None,
        condition_kind: ConditionKind,
        condition_text: str | None,
        direction_kind: DirectionKind | None,
        turning_point_kind: TurningPointKind | None,
        target_expression: str,
        period_expression: str | None,
        heatmap_candidate: bool,
        created_at: datetime,
    ) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_statements(
                run_id,
                ordinal,
                batch_ordinal,
                proposal_ordinal,
                source_video_id,
                statement_type,
                forecast_basis,
                condition_kind,
                condition_text,
                direction_kind,
                turning_point_kind,
                original_target_expression,
                original_period_expression,
                heatmap_candidate,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ordinal,
                batch_ordinal,
                proposal_ordinal,
                source_video_id,
                statement_type.value,
                None if forecast_basis is None else forecast_basis.value,
                condition_kind.value,
                condition_text,
                None if direction_kind is None else direction_kind.value,
                (
                    None
                    if turning_point_kind is None
                    else turning_point_kind.value
                ),
                target_expression,
                period_expression,
                int(heatmap_candidate),
                utc_iso(created_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("statement insert did not return an id")
        return cursor.lastrowid

    def insert_evidence_links(
        self, links: Sequence[EvidenceLink]
    ) -> None:
        self._require_transaction()
        self._conn.executemany(
            """
            INSERT INTO analysis_statement_evidence_links(
                statement_id,
                ordinal,
                run_segment_id,
                excerpt,
                start_ms,
                end_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    link.statement_id,
                    link.ordinal,
                    link.run_segment_id,
                    link.excerpt,
                    link.start_ms,
                    link.end_ms,
                )
                for link in links
            ),
        )

    def list_run_statements(
        self, run_id: int
    ) -> tuple[NormalizedStatement, ...]:
        statement_rows = self._conn.execute(
            """
            SELECT *
            FROM analysis_statements
            WHERE run_id=?
            ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        if not statement_rows:
            return ()
        statement_ids = tuple(row["id"] for row in statement_rows)
        evidence_rows = self._conn.execute(
            """
            SELECT
                link.statement_id,
                link.ordinal,
                link.run_segment_id,
                run_segment.segment_id,
                link.excerpt,
                link.start_ms,
                link.end_ms
            FROM analysis_statement_evidence_links AS link
            JOIN analysis_statements AS statement
                ON statement.id = link.statement_id
            JOIN analysis_run_segments AS run_segment
                ON run_segment.id = link.run_segment_id
            WHERE statement.run_id = ?
            ORDER BY statement.ordinal, link.ordinal
            """,
            (run_id,),
        ).fetchall()
        links_by_statement: dict[int, list[EvidenceLink]] = {
            statement_id: [] for statement_id in statement_ids
        }
        for row in evidence_rows:
            links_by_statement[row["statement_id"]].append(
                EvidenceLink(
                    statement_id=row["statement_id"],
                    ordinal=row["ordinal"],
                    run_segment_id=row["run_segment_id"],
                    segment_id=row["segment_id"],
                    excerpt=row["excerpt"],
                    start_ms=row["start_ms"],
                    end_ms=row["end_ms"],
                )
            )
        return tuple(
            _statement_from_row(
                row, tuple(links_by_statement[row["id"]])
            )
            for row in statement_rows
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "STATEMENT_TRANSACTION_REQUIRED",
                "statement mutation requires an active caller transaction",
            )


def _statement_from_row(
    row: sqlite3.Row, evidence_links: tuple[EvidenceLink, ...]
) -> NormalizedStatement:
    return NormalizedStatement(
        id=row["id"],
        run_id=row["run_id"],
        ordinal=row["ordinal"],
        batch_ordinal=row["batch_ordinal"],
        proposal_ordinal=row["proposal_ordinal"],
        source_video_id=row["source_video_id"],
        statement_type=StatementType(row["statement_type"]),
        forecast_basis=(
            None
            if row["forecast_basis"] is None
            else ForecastBasis(row["forecast_basis"])
        ),
        condition_kind=ConditionKind(row["condition_kind"]),
        condition_text=row["condition_text"],
        direction_kind=(
            None
            if row["direction_kind"] is None
            else DirectionKind(row["direction_kind"])
        ),
        turning_point_kind=(
            None
            if row["turning_point_kind"] is None
            else TurningPointKind(row["turning_point_kind"])
        ),
        target_expression=row["original_target_expression"],
        period_expression=row["original_period_expression"],
        heatmap_candidate=bool(row["heatmap_candidate"]),
        evidence_links=evidence_links,
        created_at=_parse_utc(row["created_at"]),
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

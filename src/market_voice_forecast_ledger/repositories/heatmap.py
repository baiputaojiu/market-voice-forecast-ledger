import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    HeatmapGranularity,
    MappingKind,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError


@dataclass(frozen=True, slots=True)
class HeatmapCellWrite:
    scope_id: int
    subject_id: int
    source_run_id: int
    projection_batch_id: int
    granularity: HeatmapGranularity
    period_key: str
    slot_start: date | None
    slot_end: date | None
    unknown_period: bool
    asset: Asset
    condition_kind: ConditionKind
    condition_texts: tuple[str, ...]
    primary_direction: DirectionKind
    directions: tuple[DirectionKind, ...]
    view_relation: ViewRelation
    selected_published_at: datetime
    selected_forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence
    evidence_count: int
    supporting_statement_ids: tuple[int, ...]
    counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]


class HeatmapRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def delete_scope(self, scope_id: int) -> None:
        self._require_transaction()
        self._conn.execute(
            "DELETE FROM heatmap_cells WHERE scope_id=?", (scope_id,)
        )

    def insert_cell(self, cell: HeatmapCellWrite) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO heatmap_cells(
                scope_id, subject_id, source_run_id, projection_batch_id,
                granularity, period_key, slot_start, slot_end, unknown_period,
                asset, condition_kind, condition_texts_json,
                primary_direction, directions_json, view_relation,
                selected_published_at, selected_forecast_basis,
                period_specificity, mapping_kind, confidence, evidence_count,
                supporting_statement_ids_json,
                counterevidence_statement_ids_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                cell.scope_id,
                cell.subject_id,
                cell.source_run_id,
                cell.projection_batch_id,
                cell.granularity.value,
                cell.period_key,
                None if cell.slot_start is None else cell.slot_start.isoformat(),
                None if cell.slot_end is None else cell.slot_end.isoformat(),
                int(cell.unknown_period),
                cell.asset.value,
                cell.condition_kind.value,
                canonical_json(cell.condition_texts),
                cell.primary_direction.value,
                canonical_json(tuple(item.value for item in cell.directions)),
                cell.view_relation.value,
                utc_iso(cell.selected_published_at),
                cell.selected_forecast_basis.value,
                cell.period_specificity,
                cell.mapping_kind.value,
                cell.confidence.value,
                cell.evidence_count,
                canonical_json(cell.supporting_statement_ids),
                canonical_json(cell.counterevidence_statement_ids),
            ),
        )
        cell_id = cursor.lastrowid
        if not isinstance(cell_id, int) or cell_id <= 0:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "heatmap cell insert did not return a valid identifier",
            )
        return cell_id

    def insert_links(self, cell_id: int, cell: HeatmapCellWrite) -> None:
        self._require_transaction()
        self._conn.executemany(
            """
            INSERT INTO heatmap_cell_forecasts(
                heatmap_cell_id, scope_id, source_run_id,
                projection_batch_id, source_forecast_id, ordinal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    cell_id,
                    cell.scope_id,
                    cell.source_run_id,
                    cell.projection_batch_id,
                    source_forecast_id,
                    ordinal,
                )
                for ordinal, source_forecast_id in enumerate(
                    cell.source_forecast_ids, start=1
                )
            ),
        )

    def list_cells(
        self, scope_id: int, granularity: HeatmapGranularity
    ) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                """
                SELECT *
                FROM heatmap_cells
                WHERE scope_id=? AND granularity=?
                ORDER BY
                    subject_id,
                    CASE asset
                        WHEN 'nikkei_225' THEN 1
                        WHEN 'topix' THEN 2
                        WHEN 'sp500' THEN 3
                        ELSE 4
                    END,
                    CASE WHEN period_key='unknown' THEN 1 ELSE 0 END,
                    period_key,
                    CASE condition_kind
                        WHEN 'unconditional' THEN 1 ELSE 2
                    END,
                    id
                """,
                (scope_id, granularity.value),
            )
        )

    def list_links(self, scope_id: int) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                """
                SELECT link.*,
                       cell.id AS parent_cell_id,
                       cell.scope_id AS parent_scope_id,
                       cell.source_run_id AS parent_source_run_id,
                       cell.projection_batch_id AS parent_projection_batch_id,
                       cell.granularity AS parent_granularity,
                       current.analysis_forecast_id AS parent_forecast_id
                FROM heatmap_cell_forecasts AS link
                LEFT JOIN heatmap_cells AS cell
                    ON cell.id=link.heatmap_cell_id
                LEFT JOIN current_forecasts AS current
                    ON current.scope_id=link.scope_id
                    AND current.analysis_forecast_id=link.source_forecast_id
                    AND current.source_run_id=link.source_run_id
                    AND current.projection_batch_id=link.projection_batch_id
                WHERE link.scope_id=? OR cell.scope_id=?
                ORDER BY link.heatmap_cell_id, link.ordinal
                """,
                (scope_id, scope_id),
            )
        )

    def list_active_subjects(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                """
                SELECT id, canonical_name
                FROM analysis_subjects
                WHERE is_active=1
                ORDER BY id
                """
            )
        )

    def get_subject(self, subject_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT id, canonical_name FROM analysis_subjects WHERE id=?",
            (subject_id,),
        ).fetchone()

    def find_scope(self, subject_id: int, cutoff_day: date) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT id, status, stale_reason
            FROM analysis_scopes
            WHERE subject_id=? AND cutoff_day_jst=?
            """,
            (subject_id, cutoff_day.isoformat()),
        ).fetchone()

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "HEATMAP_TRANSACTION_REQUIRED",
                "heatmap cache writes require a caller-owned transaction",
            )

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    HeatmapGranularity,
    MappingKind,
    ScopeStatus,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import (
    ProjectedForecast,
    PublicationCandidate,
    resolve_publication_groups,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.forecasts import ForecastRepository
from market_voice_forecast_ledger.repositories.heatmap import (
    HeatmapCellWrite,
    HeatmapRepository,
)


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    scope_id: int
    subject_id: int
    source_run_id: int
    projection_batch_id: int
    asset: Asset
    granularity: HeatmapGranularity
    period_key: str
    slot_start: date | None
    slot_end: date | None
    unknown_period: bool
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


@dataclass(frozen=True, slots=True)
class HeatmapRow:
    subject_id: int
    subject_key: str
    scope_id: int | None
    scope_status: ScopeStatus | None
    stale_reason: str | None
    asset: Asset
    cells: tuple[HeatmapCell, ...]


@dataclass(frozen=True, slots=True)
class HeatmapView:
    scope_id: int | None
    cutoff_day: date
    granularity: HeatmapGranularity
    scope_status: ScopeStatus | None
    stale_reason: str | None
    rows: tuple[HeatmapRow, ...]

    def cell(
        self,
        subject_key: str,
        asset: Asset,
        period_key: str,
        condition_kind: ConditionKind = ConditionKind.UNCONDITIONAL,
    ) -> HeatmapCell:
        if (
            type(subject_key) is not str
            or not subject_key
            or type(asset) is not Asset
            or type(period_key) is not str
            or not period_key
            or type(condition_kind) is not ConditionKind
        ):
            raise DomainError(
                "HEATMAP_CELL_LOOKUP_INVALID",
                "heatmap cell lookup values are invalid",
            )
        matches = tuple(
            cell
            for row in self.rows
            if row.subject_key == subject_key and row.asset is asset
            for cell in row.cells
            if cell.period_key == period_key
            and cell.condition_kind is condition_kind
        )
        if not matches:
            raise DomainError(
                "HEATMAP_CELL_NOT_FOUND",
                "requested heatmap cell does not exist",
            )
        if len(matches) != 1:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "heatmap view contains duplicate cell keys",
            )
        return matches[0]


_ASSET_ORDER = {asset: index for index, asset in enumerate(Asset)}
_GRANULARITY_ORDER = {
    granularity: index for index, granularity in enumerate(HeatmapGranularity)
}
_CONDITION_ORDER = {
    condition: index for index, condition in enumerate(ConditionKind)
}
_DIRECTION_ORDER = {
    direction: index for index, direction in enumerate(DirectionKind)
}


class HeatmapService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._forecasts = ForecastRepository(conn)
        self._repository = HeatmapRepository(conn)

    def rebuild_scope(self, scope_id: int) -> int:
        self._validate_scope_id(scope_id)
        with transaction(self._conn):
            return self._rebuild_scope_in_transaction(scope_id)

    def _rebuild_scope_in_transaction(self, scope_id: int) -> int:
        self._validate_scope_id(scope_id)
        if not self._conn.in_transaction:
            raise DomainError(
                "HEATMAP_TRANSACTION_REQUIRED",
                "heatmap cache writes require a caller-owned transaction",
            )
        scope, header, forecasts = self._current_context(scope_id)
        self._validated_raw_links(scope_id)
        self._repository.delete_scope(scope_id)
        if header is None:
            return 0
        cells = self._build_cells(
            scope.id,
            scope.subject_id,
            header["source_run_id"],
            header["projection_batch_id"],
            forecasts,
        )
        self._insert_cells(cells)
        return len(cells)

    def _insert_cells(self, cells: tuple[HeatmapCellWrite, ...]) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "HEATMAP_TRANSACTION_REQUIRED",
                "heatmap cache writes require a caller-owned transaction",
            )
        for cell in cells:
            cell_id = self._repository.insert_cell(cell)
            self._repository.insert_links(cell_id, cell)

    def read_scope(
        self, scope_id: int, granularity: HeatmapGranularity
    ) -> HeatmapView:
        self._validate_scope_id(scope_id)
        self._validate_granularity(granularity)
        scope = self._analysis.get_scope(scope_id)
        subject = self._repository.get_subject(scope.subject_id)
        if subject is None:
            self._invalid("heatmap subject does not exist")
        rows = self._read_rows(scope_id, granularity, subject)
        return HeatmapView(
            scope_id=scope.id,
            cutoff_day=scope.cutoff_day_jst,
            granularity=granularity,
            scope_status=scope.status,
            stale_reason=scope.stale_reason,
            rows=rows,
        )

    def read_cutoff(
        self, cutoff_day: date, granularity: HeatmapGranularity
    ) -> HeatmapView:
        if not isinstance(cutoff_day, date) or isinstance(cutoff_day, datetime):
            raise DomainError(
                "HEATMAP_READ_INVALID", "cutoff day must be a date"
            )
        self._validate_granularity(granularity)
        subjects = self._repository.list_active_subjects()
        if len(subjects) != 4:
            raise DomainError(
                "HEATMAP_ACTIVE_SUBJECT_SET_INVALID",
                "heatmap cutoff reads require exactly four active subjects",
            )
        rows: list[HeatmapRow] = []
        for subject in subjects:
            scope = self._repository.find_scope(subject["id"], cutoff_day)
            if scope is None:
                rows.extend(self._empty_rows(subject))
            else:
                rows.extend(
                    self._read_rows(scope["id"], granularity, subject)
                )
        if len(rows) != 16:
            self._invalid("heatmap cutoff row count is invalid")
        return HeatmapView(
            scope_id=None,
            cutoff_day=cutoff_day,
            granularity=granularity,
            scope_status=None,
            stale_reason=None,
            rows=tuple(rows),
        )

    def _artifact_payload(self, scope_id: int) -> dict[str, object]:
        self._validate_scope_id(scope_id)
        views = tuple(
            self.read_scope(scope_id, granularity)
            for granularity in HeatmapGranularity
        )
        return {
            "scope_id": scope_id,
            "granularities": [
                {
                    "granularity": view.granularity.value,
                    "rows": [
                        {
                            "subject_id": row.subject_id,
                            "subject_key": row.subject_key,
                            "scope_id": row.scope_id,
                            "scope_status": (
                                None
                                if row.scope_status is None
                                else row.scope_status.value
                            ),
                            "stale_reason": row.stale_reason,
                            "asset": row.asset.value,
                            "cells": [
                                {
                                    "period_key": cell.period_key,
                                    "source_run_id": cell.source_run_id,
                                    "projection_batch_id": (
                                        cell.projection_batch_id
                                    ),
                                    "slot_start": (
                                        None
                                        if cell.slot_start is None
                                        else cell.slot_start.isoformat()
                                    ),
                                    "slot_end": (
                                        None
                                        if cell.slot_end is None
                                        else cell.slot_end.isoformat()
                                    ),
                                    "unknown_period": cell.unknown_period,
                                    "condition_kind": cell.condition_kind.value,
                                    "condition_texts": list(cell.condition_texts),
                                    "primary_direction": (
                                        cell.primary_direction.value
                                    ),
                                    "directions": [
                                        item.value for item in cell.directions
                                    ],
                                    "view_relation": cell.view_relation.value,
                                    "selected_published_at": utc_iso(
                                        cell.selected_published_at
                                    ),
                                    "selected_forecast_basis": (
                                        cell.selected_forecast_basis.value
                                    ),
                                    "period_specificity": cell.period_specificity,
                                    "mapping_kind": cell.mapping_kind.value,
                                    "confidence": cell.confidence.value,
                                    "evidence_count": cell.evidence_count,
                                    "supporting_statement_ids": list(
                                        cell.supporting_statement_ids
                                    ),
                                    "counterevidence_statement_ids": list(
                                        cell.counterevidence_statement_ids
                                    ),
                                    "source_forecast_ids": list(
                                        cell.source_forecast_ids
                                    ),
                                }
                                for cell in row.cells
                            ],
                        }
                        for row in view.rows
                    ],
                }
                for view in views
            ],
        }

    def _read_rows(
        self,
        scope_id: int,
        granularity: HeatmapGranularity,
        subject: sqlite3.Row,
    ) -> tuple[HeatmapRow, ...]:
        scope, header, forecasts = self._current_context(scope_id)
        if scope.subject_id != subject["id"]:
            self._invalid("heatmap scope subject does not match its row")
        expected = (
            ()
            if header is None
            else tuple(
                cell
                for cell in self._build_cells(
                    scope.id,
                    scope.subject_id,
                    header["source_run_id"],
                    header["projection_batch_id"],
                    forecasts,
                )
                if cell.granularity is granularity
            )
        )
        stored = self._read_stored_cells(scope_id, granularity)
        if len(stored) != len(expected):
            self._invalid("heatmap cache cell coverage is incomplete")
        for actual, wanted in zip(stored, expected, strict=True):
            if self._cell_signature(actual) != self._write_signature(wanted):
                self._invalid("heatmap cache content does not match current forecasts")

        cells_by_asset: dict[Asset, list[HeatmapCell]] = defaultdict(list)
        for cell in stored:
            cells_by_asset[cell.asset].append(cell)
        return tuple(
            HeatmapRow(
                subject_id=subject["id"],
                subject_key=subject["canonical_name"],
                scope_id=scope.id,
                scope_status=scope.status,
                stale_reason=scope.stale_reason,
                asset=asset,
                cells=tuple(cells_by_asset.get(asset, ())),
            )
            for asset in Asset
        )

    def _empty_rows(self, subject: sqlite3.Row) -> tuple[HeatmapRow, ...]:
        return tuple(
            HeatmapRow(
                subject_id=subject["id"],
                subject_key=subject["canonical_name"],
                scope_id=None,
                scope_status=None,
                stale_reason=None,
                asset=asset,
                cells=(),
            )
            for asset in Asset
        )

    def _current_context(self, scope_id: int):
        scope = self._analysis.get_scope(scope_id)
        try:
            from market_voice_forecast_ledger.services.current_results import (
                CurrentResultService,
            )

            current_results = CurrentResultService(self._conn)
            summary = current_results.get_scope(scope_id)
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "current result state is invalid for heatmap use",
            ) from cause
        header = self._conn.execute(
            "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if summary.source_run_id is None:
            if header is not None or summary.projection_batch_id is not None:
                self._invalid("empty current result summary has a header")
            return scope, None, ()
        if (
            header is None
            or header["source_run_id"] != summary.source_run_id
            or header["projection_batch_id"] != summary.projection_batch_id
        ):
            self._invalid("current result header changed during heatmap validation")
        try:
            self._validate_raw_projection_storage(
                header["projection_batch_id"], summary.forecast_ids
            )
            run = self._analysis.get_run(header["source_run_id"])
            batch = self._forecasts.get_batch(header["projection_batch_id"])
            current_results._validate_batch_contents(run, batch)
        except (
            AttributeError,
            DomainError,
            KeyError,
            LookupError,
            TypeError,
            ValueError,
        ) as cause:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "current forecast batch content is invalid",
            ) from cause
        if (
            batch.run_id != header["source_run_id"]
            or tuple(
                forecast.id for forecast in batch.forecasts
            )
            != summary.forecast_ids
        ):
            self._invalid("current forecast batch ownership is invalid")
        for forecast in batch.forecasts:
            self._validate_forecast_shape(forecast)
        if any(not forecast.heatmap_eligible for forecast in batch.forecasts):
            self._invalid("current forecast coverage is invalid")
        return scope, header, batch.forecasts

    def _validate_raw_projection_storage(
        self, batch_id: int, expected_forecast_ids: tuple[int, ...]
    ) -> None:
        batch_row = self._conn.execute(
            "SELECT created_at FROM forecast_projection_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        rows = tuple(
            self._conn.execute(
                """
                SELECT id, period_start, period_end, unknown_period,
                       selected_published_at, heatmap_eligible
                FROM analysis_forecasts
                WHERE projection_batch_id=?
                ORDER BY id
                """,
                (batch_id,),
            )
        )
        if (
            batch_row is None
            or tuple(row["id"] for row in rows) != expected_forecast_ids
        ):
            self._invalid("raw current forecast coverage is invalid")
        try:
            self._parse_utc(batch_row["created_at"])
            for row in rows:
                self._parse_date(row["period_start"])
                self._parse_date(row["period_end"])
                self._parse_bool(row["unknown_period"])
                self._parse_utc(row["selected_published_at"])
                self._parse_bool(row["heatmap_eligible"])
        except (AttributeError, TypeError, ValueError) as cause:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "raw current forecast storage is noncanonical",
            ) from cause

    def _validate_forecast_shape(self, forecast: ProjectedForecast) -> None:
        directions = forecast.directions
        supporting = forecast.supporting_statement_ids
        counterevidence = forecast.counterevidence_statement_ids
        if (
            not self._positive_int(forecast.id)
            or not self._positive_int(forecast.projection_batch_id)
            or not self._positive_int(forecast.run_id)
            or not self._positive_int(forecast.subject_id)
            or type(forecast.asset) is not Asset
            or type(forecast.mapping_kind) is not MappingKind
            or type(forecast.condition_kind) is not ConditionKind
            or type(forecast.view_relation) is not ViewRelation
            or type(forecast.primary_direction) is not DirectionKind
            or not isinstance(directions, tuple)
            or not directions
            or any(type(item) is not DirectionKind for item in directions)
            or len(set(directions)) != len(directions)
            or directions
            != tuple(sorted(directions, key=_DIRECTION_ORDER.__getitem__))
            or forecast.primary_direction not in directions
            or type(forecast.confidence) is not Confidence
            or type(forecast.selected_forecast_basis) is not ForecastBasis
            or not isinstance(forecast.selected_published_at, datetime)
            or forecast.selected_published_at.tzinfo is None
            or forecast.selected_published_at.utcoffset() is None
            or not isinstance(forecast.period_specificity, int)
            or isinstance(forecast.period_specificity, bool)
            or forecast.period_specificity not in range(4)
            or not self._positive_int(forecast.evidence_count)
            or not isinstance(supporting, tuple)
            or not supporting
            or any(not self._positive_int(item) for item in supporting)
            or supporting != tuple(sorted(set(supporting)))
            or not isinstance(counterevidence, tuple)
            or any(not self._positive_int(item) for item in counterevidence)
            or counterevidence != tuple(sorted(set(counterevidence)))
            or set(supporting) & set(counterevidence)
            or forecast.evidence_count != len(supporting)
            or not isinstance(forecast.stable_selection_key, str)
            or not forecast.stable_selection_key
            or forecast.heatmap_eligible is not True
            or forecast.exclusion_reason is not None
            or forecast.source_forecast_ids != (forecast.id,)
        ):
            self._invalid("stored current forecast shape is invalid")
        has_up = bool(
            set(directions) & {DirectionKind.UP, DirectionKind.STRONG_UP}
        )
        has_down = bool(
            set(directions)
            & {DirectionKind.DOWN, DirectionKind.STRONG_DOWN}
        )
        if (forecast.view_relation is ViewRelation.DISAGREEMENT) != (
            has_up and has_down
        ):
            self._invalid("stored forecast direction relation is invalid")
        if forecast.condition_kind is ConditionKind.UNCONDITIONAL:
            if forecast.condition_text is not None:
                self._invalid("stored forecast condition is invalid")
        elif (
            not isinstance(forecast.condition_text, str)
            or not forecast.condition_text
        ):
            self._invalid("stored forecast condition is invalid")
        if forecast.unknown_period:
            if (
                forecast.period_start is not None
                or forecast.period_end is not None
                or forecast.period_specificity != 0
            ):
                self._invalid("stored unknown forecast period is invalid")
            return
        if (
            type(forecast.period_start) is not date
            or type(forecast.period_end) is not date
            or forecast.period_start > forecast.period_end
        ):
            self._invalid("stored known forecast period is invalid")
        day_count = (forecast.period_end - forecast.period_start).days + 1
        expected_specificity = 3 if day_count <= 7 else 2 if day_count <= 31 else 1
        if forecast.period_specificity != expected_specificity:
            self._invalid("stored forecast period specificity is invalid")

    def _build_cells(
        self,
        scope_id: int,
        subject_id: int,
        source_run_id: int,
        projection_batch_id: int,
        forecasts: tuple[ProjectedForecast, ...],
    ) -> tuple[HeatmapCellWrite, ...]:
        groups: dict[
            tuple[Asset, HeatmapGranularity, str, ConditionKind],
            list[tuple[ProjectedForecast, date | None, date | None]],
        ] = defaultdict(list)
        supporting_by_forecast: dict[
            int, dict[DirectionKind, tuple[int, ...]]
        ] = {}
        for forecast in forecasts:
            if (
                forecast.id is None
                or forecast.run_id != source_run_id
                or forecast.projection_batch_id != projection_batch_id
                or forecast.subject_id != subject_id
                or not forecast.heatmap_eligible
            ):
                self._invalid("forecast ownership is invalid for heatmap rebuild")
            supporting_by_forecast[forecast.id] = self._support_by_direction(
                forecast
            )
            for granularity in HeatmapGranularity:
                for period_key, slot_start, slot_end in self._slots(
                    forecast, granularity
                ):
                    groups[
                        (
                            forecast.asset,
                            granularity,
                            period_key,
                            forecast.condition_kind,
                        )
                    ].append((forecast, slot_start, slot_end))

        cells: list[HeatmapCellWrite] = []
        for key in sorted(groups, key=self._group_sort_key):
            asset, granularity, period_key, condition_kind = key
            members = tuple(groups[key])
            candidates: list[PublicationCandidate] = []
            for forecast, _, _ in members:
                for direction_ordinal, direction in enumerate(
                    forecast.directions, start=1
                ):
                    candidates.append(
                        PublicationCandidate(
                            published_at=forecast.selected_published_at,
                            direction=direction,
                            forecast_basis=forecast.selected_forecast_basis,
                            period_specificity=forecast.period_specificity,
                            mapping_kind=forecast.mapping_kind,
                            confidence=forecast.confidence,
                            inherited_view_relation=forecast.view_relation,
                            evidence_statement_ids=(
                                supporting_by_forecast[forecast.id][direction]
                            ),
                            inherited_counterevidence_statement_ids=(
                                forecast.counterevidence_statement_ids
                            ),
                            source_forecast_ids=(forecast.id,),
                            stable_order_key=(
                                f"{forecast.stable_selection_key}:"
                                f"{forecast.id:020d}:"
                                f"{0 if direction is forecast.primary_direction else 1}:"
                                f"{direction_ordinal:02d}"
                            ),
                        )
                    )
            resolved = resolve_publication_groups(candidates)
            condition_texts = tuple(
                sorted(
                    {
                        forecast.condition_text
                        for forecast, _, _ in members
                        if forecast.condition_text is not None
                    }
                )
            )
            slot_start = members[0][1]
            slot_end = members[0][2]
            if any(
                (member_start, member_end) != (slot_start, slot_end)
                for _, member_start, member_end in members
            ):
                self._invalid("heatmap slot grouping is inconsistent")
            cells.append(
                HeatmapCellWrite(
                    scope_id=scope_id,
                    subject_id=subject_id,
                    source_run_id=source_run_id,
                    projection_batch_id=projection_batch_id,
                    granularity=granularity,
                    period_key=period_key,
                    slot_start=slot_start,
                    slot_end=slot_end,
                    unknown_period=period_key == "unknown",
                    asset=asset,
                    condition_kind=condition_kind,
                    condition_texts=condition_texts,
                    primary_direction=resolved.primary_direction,
                    directions=resolved.directions,
                    view_relation=resolved.view_relation,
                    selected_published_at=resolved.selected_published_at,
                    selected_forecast_basis=resolved.selected_forecast_basis,
                    period_specificity=resolved.period_specificity,
                    mapping_kind=resolved.mapping_kind,
                    confidence=resolved.confidence,
                    evidence_count=resolved.evidence_count,
                    supporting_statement_ids=(
                        resolved.supporting_statement_ids
                    ),
                    counterevidence_statement_ids=(
                        resolved.counterevidence_statement_ids
                    ),
                    source_forecast_ids=resolved.source_forecast_ids,
                )
            )
        return tuple(cells)

    def _support_by_direction(
        self, forecast: ProjectedForecast
    ) -> dict[DirectionKind, tuple[int, ...]]:
        rows = tuple(
            self._conn.execute(
                """
                SELECT link.statement_id AS id,
                       statement.run_id,
                       statement.direction_kind
                FROM analysis_forecast_statement_links AS link
                LEFT JOIN analysis_statements AS statement
                    ON statement.id=link.statement_id
                WHERE link.forecast_id=? AND link.relation_kind='supporting'
                ORDER BY link.ordinal
                """,
                (forecast.id,),
            )
        )
        try:
            statement_directions = tuple(
                (row["id"], DirectionKind(row["direction_kind"]))
                for row in rows
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "HEATMAP_CACHE_INVALID",
                "stored forecast support directions are invalid",
            ) from cause
        if (
            tuple(row["id"] for row in rows)
            != forecast.supporting_statement_ids
            or any(row["run_id"] != forecast.run_id for row in rows)
        ):
            self._invalid("stored forecast support ownership is invalid")

        result = {
            direction: tuple(
                statement_id
                for statement_id, statement_direction in statement_directions
                if self._same_direction_family(direction, statement_direction)
            )
            for direction in forecast.directions
        }
        if any(not statement_ids for statement_ids in result.values()) or any(
            not any(
                self._same_direction_family(direction, statement_direction)
                for direction in forecast.directions
            )
            for _, statement_direction in statement_directions
        ):
            self._invalid("stored forecast support does not match its directions")
        return result

    @staticmethod
    def _same_direction_family(
        left: DirectionKind, right: DirectionKind
    ) -> bool:
        upward = {DirectionKind.UP, DirectionKind.STRONG_UP}
        downward = {DirectionKind.DOWN, DirectionKind.STRONG_DOWN}
        if left in upward:
            return right in upward
        if left in downward:
            return right in downward
        return left is right

    def _slots(
        self, forecast: ProjectedForecast, granularity: HeatmapGranularity
    ) -> tuple[tuple[str, date | None, date | None], ...]:
        if forecast.unknown_period:
            if forecast.period_start is not None or forecast.period_end is not None:
                self._invalid("unknown forecast has stored dates")
            return (("unknown", None, None),)
        if forecast.period_start is None or forecast.period_end is None:
            self._invalid("known forecast is missing stored dates")
        start = forecast.period_start
        end = forecast.period_end
        if start > end:
            self._invalid("forecast date range is invalid")
        slots: list[tuple[str, date, date]] = []
        if granularity is HeatmapGranularity.WEEK:
            cursor = start - timedelta(days=start.weekday())
            while cursor <= end:
                slot_end = cursor + timedelta(days=6)
                slots.append(
                    (f"{cursor.isoformat()}/{slot_end.isoformat()}", cursor, slot_end)
                )
                cursor += timedelta(days=7)
        else:
            cursor = date(start.year, start.month, 1)
            while cursor <= end:
                next_month = (
                    date(cursor.year + 1, 1, 1)
                    if cursor.month == 12
                    else date(cursor.year, cursor.month + 1, 1)
                )
                slot_end = next_month - timedelta(days=1)
                slots.append((cursor.strftime("%Y-%m"), cursor, slot_end))
                cursor = next_month
        return tuple(slots)

    def _read_stored_cells(
        self, scope_id: int, granularity: HeatmapGranularity
    ) -> tuple[HeatmapCell, ...]:
        rows = self._repository.list_cells(scope_id, granularity)
        links = tuple(
            link
            for link in self._validated_raw_links(scope_id)
            if link["parent_granularity"] == granularity.value
        )
        links_by_cell: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for link in links:
            links_by_cell[link["heatmap_cell_id"]].append(link)
        cells: list[HeatmapCell] = []
        try:
            for row in rows:
                cell_links = tuple(links_by_cell.pop(row["id"], ()))
                source_ids = tuple(
                    link["source_forecast_id"] for link in cell_links
                )
                if (
                    not source_ids
                    or tuple(link["ordinal"] for link in cell_links)
                    != tuple(range(1, len(cell_links) + 1))
                    or len(set(source_ids)) != len(source_ids)
                    or any(
                        link["scope_id"] != row["scope_id"]
                        or link["source_run_id"] != row["source_run_id"]
                        or link["projection_batch_id"]
                        != row["projection_batch_id"]
                        for link in cell_links
                    )
                ):
                    raise ValueError
                condition_texts = self._parse_text_tuple(
                    row["condition_texts_json"], allow_empty=True
                )
                directions = tuple(
                    DirectionKind(item)
                    for item in self._parse_text_tuple(
                        row["directions_json"], allow_empty=False
                    )
                )
                supporting = self._parse_id_tuple(
                    row["supporting_statement_ids_json"], allow_empty=False
                )
                counterevidence = self._parse_id_tuple(
                    row["counterevidence_statement_ids_json"], allow_empty=True
                )
                selected_at = self._parse_utc(row["selected_published_at"])
                slot_start = self._parse_date(row["slot_start"])
                slot_end = self._parse_date(row["slot_end"])
                cell = HeatmapCell(
                    scope_id=row["scope_id"],
                    subject_id=row["subject_id"],
                    source_run_id=row["source_run_id"],
                    projection_batch_id=row["projection_batch_id"],
                    asset=Asset(row["asset"]),
                    granularity=HeatmapGranularity(row["granularity"]),
                    period_key=row["period_key"],
                    slot_start=slot_start,
                    slot_end=slot_end,
                    unknown_period=self._parse_bool(row["unknown_period"]),
                    condition_kind=ConditionKind(row["condition_kind"]),
                    condition_texts=condition_texts,
                    primary_direction=DirectionKind(row["primary_direction"]),
                    directions=directions,
                    view_relation=ViewRelation(row["view_relation"]),
                    selected_published_at=selected_at,
                    selected_forecast_basis=ForecastBasis(
                        row["selected_forecast_basis"]
                    ),
                    period_specificity=row["period_specificity"],
                    mapping_kind=MappingKind(row["mapping_kind"]),
                    confidence=Confidence(row["confidence"]),
                    evidence_count=row["evidence_count"],
                    supporting_statement_ids=supporting,
                    counterevidence_statement_ids=counterevidence,
                    source_forecast_ids=source_ids,
                )
                self._validate_parsed_cell(cell, row)
                cells.append(cell)
            if links_by_cell:
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError) as cause:
            raise DomainError(
                "HEATMAP_CACHE_INVALID", "stored heatmap cache is invalid"
            ) from cause
        return tuple(cells)

    def _validated_raw_links(self, scope_id: int) -> tuple[sqlite3.Row, ...]:
        links = self._repository.list_links(scope_id)
        valid_granularities = {
            granularity.value for granularity in HeatmapGranularity
        }
        if any(
            link["parent_cell_id"] is None
            or link["parent_forecast_id"] is None
            or link["parent_scope_id"] != scope_id
            or link["parent_scope_id"] != link["scope_id"]
            or link["parent_source_run_id"] != link["source_run_id"]
            or link["parent_projection_batch_id"]
            != link["projection_batch_id"]
            or link["parent_granularity"] not in valid_granularities
            for link in links
        ):
            self._invalid("heatmap source link ownership is invalid")
        return links

    def _validate_parsed_cell(self, cell: HeatmapCell, row: sqlite3.Row) -> None:
        if (
            not self._positive_int(cell.scope_id)
            or not self._positive_int(cell.subject_id)
            or not self._positive_int(cell.source_run_id)
            or not self._positive_int(cell.projection_batch_id)
            or cell.granularity.value != row["granularity"]
            or cell.primary_direction not in cell.directions
            or len(set(cell.directions)) != len(cell.directions)
            or cell.condition_texts != tuple(sorted(set(cell.condition_texts)))
            or cell.supporting_statement_ids
            != tuple(sorted(set(cell.supporting_statement_ids)))
            or cell.counterevidence_statement_ids
            != tuple(sorted(set(cell.counterevidence_statement_ids)))
            or set(cell.supporting_statement_ids)
            & set(cell.counterevidence_statement_ids)
            or cell.evidence_count != len(cell.supporting_statement_ids)
            or cell.source_forecast_ids
            != tuple(sorted(set(cell.source_forecast_ids)))
        ):
            raise ValueError
        up = bool(
            set(cell.directions) & {DirectionKind.UP, DirectionKind.STRONG_UP}
        )
        down = bool(
            set(cell.directions)
            & {DirectionKind.DOWN, DirectionKind.STRONG_DOWN}
        )
        if (cell.view_relation is ViewRelation.DISAGREEMENT) != (up and down):
            raise ValueError
        if cell.condition_kind is ConditionKind.UNCONDITIONAL:
            if cell.condition_texts:
                raise ValueError
        elif not cell.condition_texts:
            raise ValueError
        if cell.unknown_period:
            if (
                cell.period_key != "unknown"
                or cell.slot_start is not None
                or cell.slot_end is not None
                or cell.period_specificity != 0
            ):
                raise ValueError
        elif (
            cell.slot_start is None
            or cell.slot_end is None
            or cell.slot_start > cell.slot_end
        ):
            raise ValueError

    def _cell_signature(self, cell: HeatmapCell) -> tuple[object, ...]:
        return (
            cell.scope_id,
            cell.subject_id,
            cell.source_run_id,
            cell.projection_batch_id,
            cell.asset,
            cell.granularity,
            cell.period_key,
            cell.slot_start,
            cell.slot_end,
            cell.unknown_period,
            cell.condition_kind,
            cell.condition_texts,
            cell.primary_direction,
            cell.directions,
            cell.view_relation,
            cell.selected_published_at,
            cell.selected_forecast_basis,
            cell.period_specificity,
            cell.mapping_kind,
            cell.confidence,
            cell.evidence_count,
            cell.supporting_statement_ids,
            cell.counterevidence_statement_ids,
            cell.source_forecast_ids,
        )

    def _write_signature(self, cell: HeatmapCellWrite) -> tuple[object, ...]:
        return (
            cell.scope_id,
            cell.subject_id,
            cell.source_run_id,
            cell.projection_batch_id,
            cell.asset,
            cell.granularity,
            cell.period_key,
            cell.slot_start,
            cell.slot_end,
            cell.unknown_period,
            cell.condition_kind,
            cell.condition_texts,
            cell.primary_direction,
            cell.directions,
            cell.view_relation,
            cell.selected_published_at.astimezone(timezone.utc),
            cell.selected_forecast_basis,
            cell.period_specificity,
            cell.mapping_kind,
            cell.confidence,
            cell.evidence_count,
            cell.supporting_statement_ids,
            cell.counterevidence_statement_ids,
            cell.source_forecast_ids,
        )

    def _group_sort_key(self, key):
        asset, granularity, period_key, condition_kind = key
        return (
            _GRANULARITY_ORDER[granularity],
            _ASSET_ORDER[asset],
            period_key == "unknown",
            period_key,
            _CONDITION_ORDER[condition_kind],
        )

    def _parse_text_tuple(self, value: str, *, allow_empty: bool) -> tuple[str, ...]:
        payload = json.loads(value)
        if (
            not isinstance(payload, list)
            or (not allow_empty and not payload)
            or any(not isinstance(item, str) or not item for item in payload)
            or canonical_json(payload) != value
            or len(set(payload)) != len(payload)
        ):
            raise ValueError
        return tuple(payload)

    def _parse_id_tuple(self, value: str, *, allow_empty: bool) -> tuple[int, ...]:
        payload = json.loads(value)
        if (
            not isinstance(payload, list)
            or (not allow_empty and not payload)
            or any(not self._positive_int(item) for item in payload)
            or canonical_json(payload) != value
            or len(set(payload)) != len(payload)
        ):
            raise ValueError
        return tuple(payload)

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or utc_iso(parsed) != value:
            raise ValueError
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        if value is None:
            return None
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed

    @staticmethod
    def _parse_bool(value: object) -> bool:
        if value not in (0, 1) or isinstance(value, bool):
            raise ValueError
        return bool(value)

    @staticmethod
    def _positive_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _validate_scope_id(self, scope_id: int) -> None:
        if not self._positive_int(scope_id):
            raise DomainError(
                "HEATMAP_READ_INVALID", "scope identifier must be positive"
            )

    @staticmethod
    def _validate_granularity(granularity: HeatmapGranularity) -> None:
        if not isinstance(granularity, HeatmapGranularity):
            raise DomainError(
                "HEATMAP_READ_INVALID",
                "heatmap granularity must be an enum value",
            )

    @staticmethod
    def _invalid(message: str):
        raise DomainError("HEATMAP_CACHE_INVALID", message)

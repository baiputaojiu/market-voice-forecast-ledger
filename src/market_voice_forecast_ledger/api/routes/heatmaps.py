import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from market_voice_forecast_ledger.api.dependencies import get_connection
from market_voice_forecast_ledger.api.models import (
    HeatmapCellResponse,
    HeatmapQuery,
    HeatmapResponse,
    HeatmapRowResponse,
    parse_canonical_date,
)
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import HeatmapGranularity
from market_voice_forecast_ledger.services.heatmap import HeatmapService


router = APIRouter()


@router.get("/heatmaps", response_model=HeatmapResponse)
async def heatmaps(
    query: Annotated[HeatmapQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HeatmapResponse:
    view = HeatmapService(conn).read_cutoff(
        parse_canonical_date(query.cutoff),
        HeatmapGranularity(query.granularity),
    )
    rows = tuple(
        HeatmapRowResponse(
            subject_id=row.subject_id,
            subject_key=row.subject_key,
            scope_id=row.scope_id,
            scope_status=(None if row.scope_status is None else row.scope_status.value),
            stale_reason=row.stale_reason,
            asset=row.asset.value,
            cells=tuple(
                HeatmapCellResponse(
                    scope_id=cell.scope_id,
                    source_run_id=cell.source_run_id,
                    projection_batch_id=cell.projection_batch_id,
                    period_key=cell.period_key,
                    slot_start=(None if cell.slot_start is None else cell.slot_start.isoformat()),
                    slot_end=(None if cell.slot_end is None else cell.slot_end.isoformat()),
                    unknown_period=cell.unknown_period,
                    condition_kind=cell.condition_kind.value,
                    condition_texts=cell.condition_texts,
                    primary_direction=cell.primary_direction.value,
                    directions=tuple(direction.value for direction in cell.directions),
                    view_relation=cell.view_relation.value,
                    selected_published_at=utc_iso(cell.selected_published_at),
                    selected_forecast_basis=cell.selected_forecast_basis.value,
                    mapping_kind=cell.mapping_kind.value,
                    confidence=cell.confidence.value,
                    evidence_count=cell.evidence_count,
                    supporting_statement_ids=cell.supporting_statement_ids,
                    counterevidence_statement_ids=cell.counterevidence_statement_ids,
                    source_forecast_ids=cell.source_forecast_ids,
                )
                for cell in row.cells
            ),
        )
        for row in view.rows
    )
    return HeatmapResponse(
        cutoff=view.cutoff_day.isoformat(),
        granularity=view.granularity.value,
        rows=rows,
    )

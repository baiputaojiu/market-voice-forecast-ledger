import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query

from market_voice_forecast_ledger.api.dependencies import get_connection
from market_voice_forecast_ledger.api.models import (
    MappingReviewRequest,
    MappingReviewResponse,
    NoQuery,
    PeriodReviewRequest,
    PeriodReviewResponse,
    current_summary_response,
    parse_positive_path_id,
)
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    MappingReviewDecision,
    PeriodReviewDecision,
)
from market_voice_forecast_ledger.services.mapping_review import MappingReviewCommand
from market_voice_forecast_ledger.services.review_application import ReviewApplicationService


router = APIRouter()


@router.post("/mappings/{mapping_id}/reviews", response_model=MappingReviewResponse)
async def review_mapping(
    mapping_id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,17}$")],
    body: Annotated[MappingReviewRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> MappingReviewResponse:
    identifier = parse_positive_path_id(mapping_id)
    result = ReviewApplicationService(conn).apply_mapping(
        MappingReviewCommand(
            mapping_id=identifier,
            decision=MappingReviewDecision(body.decision),
            actor="user",
            reason=body.reason,
            corrected_asset=(
                None
                if body.corrected_asset is None
                else Asset(body.corrected_asset)
            ),
        )
    )
    return MappingReviewResponse(
        mapping_id=identifier,
        applied_to_current=result.applied_to_current,
        rebuilt_cell_count=result.rebuilt_cell_count,
        current=current_summary_response(result.current_summary),
    )


@router.post("/periods/{period_id}/reviews", response_model=PeriodReviewResponse)
async def review_period(
    period_id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,17}$")],
    body: Annotated[PeriodReviewRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> PeriodReviewResponse:
    identifier = parse_positive_path_id(period_id)
    result = ReviewApplicationService(conn).apply_period(
        identifier,
        PeriodReviewDecision(body.decision),
        "user",
        body.reason,
    )
    return PeriodReviewResponse(
        period_id=identifier,
        applied_to_current=result.applied_to_current,
        rebuilt_cell_count=result.rebuilt_cell_count,
        current=current_summary_response(result.current_summary),
    )

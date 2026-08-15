from typing import Annotated

from fastapi import APIRouter, Query

from market_voice_forecast_ledger.api.models import HealthResponse, NoQuery


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(_query: Annotated[NoQuery, Query()]) -> HealthResponse:
    return HealthResponse(
        status="ok", bind_boundary="127.0.0.1", authentication="none"
    )

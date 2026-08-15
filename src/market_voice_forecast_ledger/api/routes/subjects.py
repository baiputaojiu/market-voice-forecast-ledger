import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from market_voice_forecast_ledger.api.dependencies import (
    PublicReadAdapter,
    get_connection,
)
from market_voice_forecast_ledger.api.models import NoQuery, SubjectsResponse


router = APIRouter()


@router.get("/subjects", response_model=SubjectsResponse)
async def subjects(
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> SubjectsResponse:
    return SubjectsResponse(subjects=PublicReadAdapter(conn).list_subjects())

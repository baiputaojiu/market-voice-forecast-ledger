import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from market_voice_forecast_ledger.api.dependencies import (
    PublicReadAdapter,
    get_connection,
)
from market_voice_forecast_ledger.api.models import (
    JobResponse,
    NoQuery,
    parse_positive_path_id,
)


router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def job(
    job_id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,17}$")],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> JobResponse:
    return PublicReadAdapter(conn).read_job(parse_positive_path_id(job_id))

import sqlite3
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError

from market_voice_forecast_ledger.api.dependencies import (
    PublicReadAdapter,
    get_connection,
    get_task_wake_adapter,
)
from market_voice_forecast_ledger.api.models import (
    NoQuery,
    YouTubeManualCandidateRequest,
    YouTubeManualCandidateResponse,
    YouTubeSyncRequestResponse,
    YouTubeSyncStatusResponse,
    parse_positive_path_id,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.windows.task_scheduler import TaskWakeAdapter


router = APIRouter()


@router.post(
    "/youtube-syncs",
    response_model=YouTubeSyncRequestResponse,
    status_code=202,
)
async def request_youtube_sync(
    _body: Annotated[NoQuery, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    wake: Annotated[TaskWakeAdapter, Depends(get_task_wake_adapter)],
) -> YouTubeSyncRequestResponse:
    result = YouTubeSyncService(conn).request_full_sync(
        datetime.now(timezone.utc)
    )
    _request_wake(wake)
    return YouTubeSyncRequestResponse(
        job_id=result.job_id,
        status=result.status.value,
        reused=result.reused,
    )


@router.get("/youtube-syncs/{job_id}", response_model=YouTubeSyncStatusResponse)
async def youtube_sync_status(
    request: Request,
    job_id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,17}$")],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    _body: Annotated[NoQuery | None, Body()] = None,
) -> YouTubeSyncStatusResponse:
    if _body is None and (await request.body()).strip():
        raise RequestValidationError(
            [{"type": "model_type", "loc": ("body",)}]
        )
    return PublicReadAdapter(conn).read_youtube_sync_status(
        parse_positive_path_id(job_id)
    )


@router.post(
    "/youtube-manual-candidates",
    response_model=YouTubeManualCandidateResponse,
    status_code=202,
)
async def request_youtube_manual_candidate(
    body: Annotated[YouTubeManualCandidateRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    wake: Annotated[TaskWakeAdapter, Depends(get_task_wake_adapter)],
) -> YouTubeManualCandidateResponse:
    result = YouTubeSyncService(conn).request_manual_candidate(
        body.subject_id,
        body.url,
        datetime.now(timezone.utc),
    )
    _request_wake(wake)
    return YouTubeManualCandidateResponse(
        request_id=result.request_id,
        job_id=result.job_id,
        status=result.status.value,
        reused=result.reused,
    )


def _request_wake(wake: TaskWakeAdapter) -> None:
    try:
        wake.request_start()
    except Exception:
        raise DomainError(
            "YOUTUBE_SYNC_UNAVAILABLE", "YouTube sync wake is unavailable"
        ) from None

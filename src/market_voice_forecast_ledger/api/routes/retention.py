import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query

from market_voice_forecast_ledger.api.dependencies import (
    get_connection,
    get_settings,
)
from market_voice_forecast_ledger.api.models import (
    NoQuery,
    RetentionDeleteRequest,
    RetentionDeleteResponse,
    RetentionPreviewRequest,
    RetentionPreviewResponse,
    parse_canonical_utc,
)
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.services.retention import (
    DeleteTextCommand,
    RetentionService,
)


router = APIRouter()


@router.post("/retention/preview", response_model=RetentionPreviewResponse)
async def preview_retention(
    body: Annotated[RetentionPreviewRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RetentionPreviewResponse:
    result = RetentionService(conn, settings).preview_text_deletion(
        DeleteTextCommand(cutoff=parse_canonical_utc(body.cutoff))
    )
    return RetentionPreviewResponse(
        affected_video_count=result.affected_video_count,
        affected_transcript_count=result.affected_transcript_count,
        affected_analysis_input_count=result.affected_analysis_input_count,
        full_reproduction_will_be_lost=result.full_reproduction_will_be_lost,
        preview_token=result.token,
        expires_at=utc_iso(result.expires_at),
    )


@router.post("/retention/delete", response_model=RetentionDeleteResponse)
async def delete_retention(
    body: Annotated[RetentionDeleteRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RetentionDeleteResponse:
    result = RetentionService(conn, settings).delete_text(
        DeleteTextCommand(
            cutoff=parse_canonical_utc(body.cutoff),
            preview_token=body.preview_token,
        )
    )
    return RetentionDeleteResponse(
        affected_video_count=result.affected_video_count,
        deleted_transcript_count=result.deleted_transcript_count,
        deleted_analysis_input_count=result.deleted_analysis_input_count,
        deleted_at=(None if result.deleted_at is None else utc_iso(result.deleted_at)),
    )

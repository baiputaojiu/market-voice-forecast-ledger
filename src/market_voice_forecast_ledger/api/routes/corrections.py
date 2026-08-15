import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query

from market_voice_forecast_ledger.api.dependencies import (
    PublicReadAdapter,
    get_connection,
)
from market_voice_forecast_ledger.api.models import (
    NoQuery,
    SpeakerCorrectionRequest,
    SpeakerCorrectionResponse,
    parse_positive_path_id,
)
from market_voice_forecast_ledger.domain.enums import AssignmentKind
from market_voice_forecast_ledger.services.corrections import (
    SpeakerCorrection,
    SpeakerCorrectionService,
)


router = APIRouter()


@router.post(
    "/speakers/{segment_id}/corrections",
    response_model=SpeakerCorrectionResponse,
)
async def correct_speaker(
    segment_id: Annotated[str, Path(pattern=r"^[1-9][0-9]{0,17}$")],
    body: Annotated[SpeakerCorrectionRequest, Body()],
    _query: Annotated[NoQuery, Query()],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> SpeakerCorrectionResponse:
    identifier = parse_positive_path_id(segment_id)
    assignment_kind = AssignmentKind(body.assignment_kind)
    stale_scope_count = PublicReadAdapter(
        conn
    ).stale_scope_count_for_segment(
        identifier,
        body.assigned_subject_id
        if assignment_kind is AssignmentKind.SUBJECT
        else None,
    )
    result = SpeakerCorrectionService(conn).correct(
        SpeakerCorrection(
            segment_id=identifier,
            assignment_kind=assignment_kind,
            assigned_subject_id=body.assigned_subject_id,
            actor="user",
            reason=body.reason,
        )
    )
    return SpeakerCorrectionResponse(
        segment_id=result.segment_id,
        assignment_kind=result.assignment_kind.value,
        assigned_subject_id=result.assigned_subject_id,
        assignment_origin="manual",
        applied=True,
        stale_scope_count=stale_scope_count,
    )

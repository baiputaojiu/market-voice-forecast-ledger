from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.current_results import CurrentResultSummary


_CANONICAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANONICAL_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_PREVIEW_TOKEN = re.compile(r"^[0-9a-f]{64}$")


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NoQuery(StrictApiModel):
    pass


class HeatmapQuery(StrictApiModel):
    cutoff: str = Field(min_length=10, max_length=10)
    granularity: Literal["week", "month"]

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: str) -> str:
        parse_canonical_date(value)
        return value


class MappingReviewRequest(StrictApiModel):
    decision: Literal["approve", "correct", "reject"]
    reason: str = Field(min_length=1, max_length=256)
    corrected_asset: Literal["nikkei_225", "topix", "sp500", "xau_usd"] | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return practical_reason(value)

    @model_validator(mode="after")
    def validate_correction_shape(self):
        if (self.decision == "correct") != (self.corrected_asset is not None):
            raise ValueError("corrected asset shape is invalid")
        return self


class PeriodReviewRequest(StrictApiModel):
    decision: Literal["approve_unknown", "reject"]
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return practical_reason(value)


class SpeakerCorrectionRequest(StrictApiModel):
    assignment_kind: Literal["subject", "interviewer", "hold"]
    assigned_subject_id: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return practical_reason(value)

    @model_validator(mode="after")
    def validate_assignment_shape(self):
        if (self.assignment_kind == "subject") != (
            self.assigned_subject_id is not None
        ):
            raise ValueError("speaker assignment shape is invalid")
        return self


class RetentionPreviewRequest(StrictApiModel):
    cutoff: str = Field(min_length=27, max_length=27)

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: str) -> str:
        parse_canonical_utc(value)
        return value


class RetentionDeleteRequest(RetentionPreviewRequest):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("preview_token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not _PREVIEW_TOKEN.fullmatch(value):
            raise ValueError("preview token shape is invalid")
        return value


class HealthResponse(StrictApiModel):
    status: Literal["ok"]
    bind_boundary: Literal["127.0.0.1"]
    authentication: Literal["none"]


class SubjectResponse(StrictApiModel):
    id: int = Field(gt=0)
    key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    is_active: bool

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return public_subject_text(value)


class SubjectsResponse(StrictApiModel):
    subjects: tuple[SubjectResponse, ...]


class HeatmapCellResponse(StrictApiModel):
    scope_id: int = Field(gt=0)
    source_run_id: int = Field(gt=0)
    projection_batch_id: int = Field(gt=0)
    period_key: str = Field(min_length=1, max_length=32)
    slot_start: str | None = Field(default=None, min_length=10, max_length=10)
    slot_end: str | None = Field(default=None, min_length=10, max_length=10)
    unknown_period: bool
    condition_kind: Literal["unconditional", "conditional"]
    condition_texts: tuple[str, ...]
    primary_direction: Literal[
        "strong_up", "up", "flat", "down", "strong_down", "turning_point", "unknown"
    ]
    directions: tuple[
        Literal[
            "strong_up", "up", "flat", "down", "strong_down", "turning_point", "unknown"
        ],
        ...,
    ]
    view_relation: Literal["current", "changed", "disagreement"]
    selected_published_at: str = Field(min_length=27, max_length=27)
    selected_forecast_basis: Literal["direct", "inferred_from_subject_statements"]
    mapping_kind: Literal["direct", "inferred"]
    confidence: Literal["high", "medium", "low", "unresolved"]
    evidence_count: int = Field(ge=0)
    supporting_statement_ids: tuple[int, ...]
    counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]


class HeatmapRowResponse(StrictApiModel):
    subject_id: int = Field(gt=0)
    subject_key: str = Field(min_length=1, max_length=200)
    scope_id: int | None = Field(default=None, gt=0)
    scope_status: Literal["ready", "running", "current", "stale", "failed"] | None
    stale_reason: str | None = Field(default=None, max_length=64)
    asset: Literal["nikkei_225", "topix", "sp500", "xau_usd"]
    cells: tuple[HeatmapCellResponse, ...]

    @field_validator("subject_key")
    @classmethod
    def validate_subject_key(cls, value: str) -> str:
        return public_subject_text(value)


class HeatmapResponse(StrictApiModel):
    cutoff: str = Field(min_length=10, max_length=10)
    granularity: Literal["week", "month"]
    rows: tuple[HeatmapRowResponse, ...]


class StageProgressResponse(StrictApiModel):
    stage: Literal[
        "video_metadata",
        "audio_acquisition",
        "transcription",
        "speaker_assignment",
        "analysis_input_extraction",
        "codex_analysis",
        "asset_mapping",
        "heatmap_update",
    ]
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


class JobUnitResponse(StrictApiModel):
    stage: Literal[
        "video_metadata",
        "audio_acquisition",
        "transcription",
        "speaker_assignment",
        "analysis_input_extraction",
        "codex_analysis",
        "asset_mapping",
        "heatmap_update",
    ]
    status: Literal["pending", "running", "success", "failed"]
    ordinal: int = Field(gt=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


class JobResponse(StrictApiModel):
    job_id: int = Field(gt=0)
    kind: Literal["video_pipeline", "analysis_scope"]
    status: Literal[
        "queued",
        "running",
        "pause_requested",
        "paused",
        "cancel_requested",
        "stopped",
        "failed",
        "retrying",
        "succeeded",
    ]
    completed: int = Field(ge=0)
    total: int = Field(gt=0)
    stages: tuple[StageProgressResponse, ...]
    units: tuple[JobUnitResponse, ...]


class CurrentSummaryResponse(StrictApiModel):
    scope_id: int = Field(gt=0)
    source_run_id: int | None = Field(default=None, gt=0)
    projection_batch_id: int | None = Field(default=None, gt=0)
    statement_count: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    eligible_mapping_count: int = Field(ge=0)
    forecast_count: int = Field(ge=0)


class MappingReviewResponse(StrictApiModel):
    mapping_id: int = Field(gt=0)
    applied_to_current: bool
    rebuilt_cell_count: int = Field(ge=0)
    current: CurrentSummaryResponse | None


class PeriodReviewResponse(StrictApiModel):
    period_id: int = Field(gt=0)
    applied_to_current: bool
    rebuilt_cell_count: int = Field(ge=0)
    current: CurrentSummaryResponse | None


class SpeakerCorrectionResponse(StrictApiModel):
    segment_id: int = Field(gt=0)
    assignment_kind: Literal["subject", "interviewer", "hold"]
    assigned_subject_id: int | None = Field(default=None, gt=0)
    assignment_origin: Literal["manual"]
    applied: Literal[True]
    stale_scope_count: int = Field(ge=0)


class RetentionPreviewResponse(StrictApiModel):
    affected_video_count: int = Field(ge=0)
    affected_transcript_count: int = Field(ge=0)
    affected_analysis_input_count: int = Field(ge=0)
    full_reproduction_will_be_lost: bool
    preview_token: str = Field(min_length=64, max_length=64)
    expires_at: str = Field(min_length=27, max_length=27)


class RetentionDeleteResponse(StrictApiModel):
    affected_video_count: int = Field(ge=0)
    deleted_transcript_count: int = Field(ge=0)
    deleted_analysis_input_count: int = Field(ge=0)
    deleted_at: str | None = Field(default=None, min_length=27, max_length=27)


def practical_reason(value: str) -> str:
    if not any(not character.isspace() for character in value):
        raise ValueError("reason must contain practical text")
    return value


def public_subject_text(value: str) -> str:
    if (
        not any(not character.isspace() for character in value)
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("public subject text is invalid")
    return value


def parse_canonical_date(value: str):
    from datetime import date

    if not _CANONICAL_DATE.fullmatch(value):
        raise ValueError("date must be canonical")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical")
    return parsed


def parse_canonical_utc(value: str) -> datetime:
    if not _CANONICAL_UTC.fullmatch(value):
        raise ValueError("timestamp must be canonical UTC")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    if utc_iso(parsed) != value:
        raise ValueError("timestamp must be canonical UTC")
    return parsed


def parse_positive_path_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise DomainError("PATH_ID_INVALID", "path id must be canonical")
    return int(value)


def current_summary_response(
    summary: CurrentResultSummary | None,
) -> CurrentSummaryResponse | None:
    if summary is None:
        return None
    return CurrentSummaryResponse(
        scope_id=summary.scope_id,
        source_run_id=summary.source_run_id,
        projection_batch_id=summary.projection_batch_id,
        statement_count=summary.statement_count,
        mapping_count=summary.mapping_count,
        eligible_mapping_count=summary.eligible_mapping_count,
        forecast_count=summary.forecast_count,
    )

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal

from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
)


@dataclass(frozen=True, slots=True)
class ScoreRule:
    operator: Literal["gte", "lte"]
    boundary: float

    def __post_init__(self) -> None:
        if self.operator not in ("gte", "lte"):
            raise ValueError(f"unsupported score operator: {self.operator}")
        if not isfinite(self.boundary):
            raise ValueError("score boundary must be finite")

    def matches(self, raw_score: float) -> bool:
        if not isfinite(raw_score):
            raise ValueError("raw score must be finite")
        if self.operator == "gte":
            return raw_score >= self.boundary
        return raw_score <= self.boundary


@dataclass(frozen=True, slots=True)
class SpeakerThresholdConfig:
    version: str
    model_name: str
    model_version: str
    subject_rule: ScoreRule
    interviewer_rule: ScoreRule

    def __post_init__(self) -> None:
        if self.subject_rule.operator != "gte":
            raise ValueError("subject score rule must use gte")
        if self.interviewer_rule.operator != "lte":
            raise ValueError("interviewer score rule must use lte")
        if self.subject_rule.boundary <= self.interviewer_rule.boundary:
            raise ValueError("speaker thresholds must leave a hold band")


def classify_raw_score(
    raw_score: float, config: SpeakerThresholdConfig
) -> AssignmentKind:
    if config.subject_rule.matches(raw_score):
        return AssignmentKind.SUBJECT
    if config.interviewer_rule.matches(raw_score):
        return AssignmentKind.INTERVIEWER
    return AssignmentKind.HOLD


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    id: int
    video_id: int
    chunk_id: int
    segment_no: int
    start_ms: int
    end_ms: int
    text_body: str | None
    text_sha256: str
    anonymous_speaker_id: str
    transcript_created_at: datetime
    expires_at: datetime | None
    text_deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class SpeakerAssignment:
    segment_id: int
    assignment_kind: AssignmentKind
    assigned_subject_id: int | None
    assignment_origin: AssignmentOrigin
    raw_match_score: float | None
    model_name: str | None
    model_version: str | None
    threshold_config_version: str | None
    evidence_hash: str
    assigned_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class PersonalAssignmentCommand:
    segment_id: int
    subject_id: int
    raw_match_score: float
    model_name: str
    model_version: str
    threshold_config_version: str
    evidence_hash: str
    assigned_at: datetime | None = None

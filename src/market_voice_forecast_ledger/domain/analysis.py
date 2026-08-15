from dataclasses import asdict, dataclass
from datetime import date, datetime

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    AssignmentKind,
    ScopeStatus,
)


@dataclass(frozen=True, slots=True)
class AnalysisRunSettings:
    model: str
    reasoning_effort: str
    prompt_version: str
    schema_version: str
    information_boundary_version: str

    @classmethod
    def required(cls) -> "AnalysisRunSettings":
        return cls(
            model="gpt-5.6-sol",
            reasoning_effort="max",
            prompt_version="m2-core-prompt-contract-v1",
            schema_version="m2-analysis-output-v1",
            information_boundary_version="stored-statements-only-v1",
        )

    def contract_metadata(self) -> dict[str, str]:
        return asdict(self)

    def codex_execution_contract_hash(self) -> str:
        return sha256_text(canonical_json(self.contract_metadata()))


@dataclass(frozen=True, slots=True)
class BeginAnalysisRun:
    subject_id: int
    cutoff_day: date
    job_id: int
    settings: AnalysisRunSettings


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    id: int
    subject_id: int
    cutoff_day_jst: date
    cutoff_exclusive_utc: datetime
    status: ScopeStatus
    stale_reason: str | None

    @property
    def cutoff_day(self) -> date:
        return self.cutoff_day_jst


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    id: int
    scope_id: int
    model: str
    reasoning_effort: str
    prompt_version: str
    schema_version: str
    information_boundary_version: str
    input_hash: str
    input_contract_hash: str
    started_at: datetime
    active_job_id: int

    @property
    def settings(self) -> AnalysisRunSettings:
        return AnalysisRunSettings(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            information_boundary_version=self.information_boundary_version,
        )


@dataclass(frozen=True, slots=True)
class AnalysisRunJobAttempt:
    id: int
    run_id: int
    job_id: int
    attempt_ordinal: int
    source_job_id: int | None
    attached_at: datetime


@dataclass(frozen=True, slots=True)
class RunSegment:
    id: int
    run_id: int
    segment_id: int
    ordinal: int
    video_id: int
    published_at: datetime
    policy_id: int
    policy_hash: str
    assignment_kind: AssignmentKind
    assigned_subject_id: int | None
    assignment_updated_at: datetime
    assignment_evidence_hash: str


@dataclass(frozen=True, slots=True)
class AnalysisInputSnapshot:
    id: int
    run_id: int
    input_text: str | None
    metadata_json: str
    input_sha256: str
    snapshot_created_at: datetime
    expires_at: datetime | None
    text_deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class SelectedInputSegment:
    segment_id: int
    video_id: int
    youtube_video_id: str
    video_title: str
    youtube_channel_id: str | None
    channel_display_name: str
    published_at: datetime
    segment_no: int
    start_ms: int
    end_ms: int
    text_body: str
    text_sha256: str
    policy_id: int
    policy_hash: str
    assignment_kind: AssignmentKind
    assignment_origin: str
    assigned_subject_id: int | None
    assignment_updated_at: datetime
    assignment_evidence_hash: str


@dataclass(frozen=True, slots=True)
class FrozenAnalysisInput:
    input_text: str
    metadata_json: str
    input_sha256: str
    input_contract_hash: str
    segments: tuple[SelectedInputSegment, ...]


@dataclass(frozen=True, slots=True)
class AnalysisRunEvent:
    id: int
    run_id: int
    status: AnalysisRunStatus
    safe_error_code: str | None
    created_at: datetime

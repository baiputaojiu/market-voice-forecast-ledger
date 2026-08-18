from enum import StrEnum


class AssignmentKind(StrEnum):
    SUBJECT = "subject"
    INTERVIEWER = "interviewer"
    HOLD = "hold"


class AssignmentOrigin(StrEnum):
    AUTO_VOICE = "auto_voice"
    MANUAL = "manual"


class JobKind(StrEnum):
    VIDEO_PIPELINE = "video_pipeline"
    ANALYSIS_SCOPE = "analysis_scope"
    YOUTUBE_SYNC = "youtube_sync"


class JobStage(StrEnum):
    VIDEO_METADATA = "video_metadata"
    AUDIO_ACQUISITION = "audio_acquisition"
    TRANSCRIPTION = "transcription"
    SPEAKER_ASSIGNMENT = "speaker_assignment"
    ANALYSIS_INPUT_EXTRACTION = "analysis_input_extraction"
    CODEX_ANALYSIS = "codex_analysis"
    ASSET_MAPPING = "asset_mapping"
    HEATMAP_UPDATE = "heatmap_update"
    YOUTUBE_SEED_DISCOVERY = "youtube_seed_discovery"
    YOUTUBE_SEARCH_DISCOVERY = "youtube_search_discovery"
    YOUTUBE_MANUAL_DISCOVERY = "youtube_manual_discovery"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    STOPPED = "stopped"
    FAILED = "failed"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"


class UnitStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class AnalysisRunStatus(StrEnum):
    STARTED = "started"
    TRANSPORT_VALIDATED = "transport_validated"
    FAILED = "failed"
    ACCEPTED = "accepted"


class ScopeStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"


class StatementType(StrEnum):
    FUTURE_FORECAST = "future_forecast"
    CURRENT_ANALYSIS = "current_analysis"
    PAST_RESULT_ANALYSIS = "past_result_analysis"
    GENERAL_STATEMENT = "general_statement"


class ForecastBasis(StrEnum):
    DIRECT = "direct"
    INFERRED = "inferred_from_subject_statements"


class ConditionKind(StrEnum):
    UNCONDITIONAL = "unconditional"
    CONDITIONAL = "conditional"


class DirectionKind(StrEnum):
    STRONG_UP = "strong_up"
    UP = "up"
    FLAT = "flat"
    DOWN = "down"
    STRONG_DOWN = "strong_down"
    TURNING_POINT = "turning_point"
    UNKNOWN = "unknown"


class TurningPointKind(StrEnum):
    BOTTOM = "bottom"
    TOP = "top"
    OTHER = "other"


class TimeBasis(StrEnum):
    EXPLICIT_STATEMENT = "explicit_statement"
    PUBLISHED_AT = "published_at"


class PeriodReviewDecision(StrEnum):
    APPROVE_UNKNOWN = "approve_unknown"
    REJECT = "reject"


class Asset(StrEnum):
    NIKKEI_225 = "nikkei_225"
    TOPIX = "topix"
    SP500 = "sp500"
    XAU_USD = "xau_usd"


class MappingKind(StrEnum):
    DIRECT = "direct"
    INFERRED = "inferred"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class MappingReviewDecision(StrEnum):
    APPROVE = "approve"
    CORRECT = "correct"
    REJECT = "reject"


class ViewRelation(StrEnum):
    CURRENT = "current"
    CHANGED = "changed"
    DISAGREEMENT = "disagreement"


class HeatmapGranularity(StrEnum):
    WEEK = "week"
    MONTH = "month"

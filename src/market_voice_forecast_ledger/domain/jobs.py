import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError


ANALYSIS_INPUT_UNIT_KEY = "analysis-input:freeze"
STATEMENT_NORMALIZATION_UNIT_KEY = "analysis:normalize-statements"
PERIOD_NORMALIZATION_UNIT_KEY = "analysis:normalize-periods"
ASSET_MAPPING_UNIT_KEY = "analysis:map-assets"
FORECAST_PROJECTION_UNIT_KEY = "analysis:project-forecasts"
FINAL_PROMOTION_UNIT_KEY = "heatmap:promote-current"

VIDEO_PIPELINE_STAGES = frozenset(
    {
        JobStage.VIDEO_METADATA,
        JobStage.AUDIO_ACQUISITION,
        JobStage.TRANSCRIPTION,
        JobStage.SPEAKER_ASSIGNMENT,
    }
)
ANALYSIS_SCOPE_STAGES = frozenset(
    {
        JobStage.ANALYSIS_INPUT_EXTRACTION,
        JobStage.CODEX_ANALYSIS,
        JobStage.ASSET_MAPPING,
        JobStage.HEATMAP_UPDATE,
    }
)

STAGE_ORDER = (
    JobStage.VIDEO_METADATA,
    JobStage.AUDIO_ACQUISITION,
    JobStage.TRANSCRIPTION,
    JobStage.SPEAKER_ASSIGNMENT,
    JobStage.ANALYSIS_INPUT_EXTRACTION,
    JobStage.CODEX_ANALYSIS,
    JobStage.ASSET_MAPPING,
    JobStage.HEATMAP_UPDATE,
)

LEGAL_TRANSITIONS = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.STOPPED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.PAUSE_REQUESTED,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.FAILED,
            JobStatus.SUCCEEDED,
        }
    ),
    JobStatus.PAUSE_REQUESTED: frozenset(
        {JobStatus.PAUSED, JobStatus.FAILED}
    ),
    JobStatus.PAUSED: frozenset({JobStatus.RUNNING, JobStatus.STOPPED}),
    JobStatus.CANCEL_REQUESTED: frozenset({JobStatus.STOPPED}),
    JobStatus.FAILED: frozenset({JobStatus.RETRYING}),
    JobStatus.RETRYING: frozenset({JobStatus.RUNNING, JobStatus.FAILED}),
}

_SAFE_UNIT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class ManifestUnit:
    unit_key: str
    stage: JobStage
    ordinal: int
    declared_input_hash: str | None
    dependency_keys: tuple[str, ...]
    execution_contract_hash: str


@dataclass(frozen=True, slots=True)
class JobManifest:
    kind: JobKind
    units: tuple[ManifestUnit, ...]
    manifest_hash: str

    @classmethod
    def build(
        cls, kind: JobKind, units: Sequence[ManifestUnit]
    ) -> "JobManifest":
        ordered = tuple(sorted(units, key=lambda item: item.ordinal))
        ordinals = [item.ordinal for item in ordered]
        if not ordered or ordinals != list(range(1, len(ordered) + 1)):
            raise DomainError(
                "INVALID_MANIFEST_ORDINALS",
                "unit ordinals must be contiguous from one",
            )
        if len({item.unit_key for item in ordered}) != len(ordered):
            raise DomainError("DUPLICATE_UNIT_KEY", "unit keys must be unique")
        if any(not _SAFE_UNIT_KEY.fullmatch(item.unit_key) for item in ordered):
            raise DomainError(
                "INVALID_UNIT_KEY", "unit keys must be safe logical identifiers"
            )

        earlier: set[str] = set()
        for item in ordered:
            if len(set(item.dependency_keys)) != len(item.dependency_keys) or any(
                key not in earlier for key in item.dependency_keys
            ):
                raise DomainError(
                    "INVALID_UNIT_DEPENDENCY",
                    "dependencies must be unique earlier units",
                )
            earlier.add(item.unit_key)

        final_count = sum(
            item.unit_key == FINAL_PROMOTION_UNIT_KEY for item in ordered
        )
        input_count = sum(
            item.unit_key == ANALYSIS_INPUT_UNIT_KEY for item in ordered
        )
        if kind is JobKind.ANALYSIS_SCOPE and (
            input_count != 1
            or ordered[0].unit_key != ANALYSIS_INPUT_UNIT_KEY
            or ordered[0].stage is not JobStage.ANALYSIS_INPUT_EXTRACTION
            or final_count != 1
            or ordered[-1].unit_key != FINAL_PROMOTION_UNIT_KEY
        ):
            raise DomainError(
                "INVALID_ANALYSIS_MANIFEST",
                "analysis manifest requires reserved first and final units",
            )
        if kind is JobKind.VIDEO_PIPELINE and (input_count or final_count):
            raise DomainError(
                "INVALID_VIDEO_MANIFEST",
                "video manifest cannot contain analysis-reserved units",
            )

        allowed = (
            ANALYSIS_SCOPE_STAGES
            if kind is JobKind.ANALYSIS_SCOPE
            else VIDEO_PIPELINE_STAGES
        )
        if any(item.stage not in allowed for item in ordered):
            raise DomainError(
                "INVALID_JOB_STAGE", "unit stage does not belong to job kind"
            )
        if (
            kind is JobKind.ANALYSIS_SCOPE
            and ordered[-1].stage is not JobStage.HEATMAP_UPDATE
        ):
            raise DomainError(
                "INVALID_PROMOTION_STAGE",
                "promotion unit must use heatmap_update stage",
            )

        payload = {"kind": kind.value, "units": [asdict(item) for item in ordered]}
        return cls(kind, ordered, sha256_text(canonical_json(payload)))


@dataclass(frozen=True, slots=True)
class JobUnit:
    job_id: int
    unit_key: str
    stage: JobStage
    ordinal: int
    status: UnitStatus
    declared_input_hash: str | None
    dependency_keys: tuple[str, ...]
    execution_contract_hash: str
    external_input_hash: str | None
    bound_input_hash: str | None
    output_hash: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ResumePlan:
    reused_unit_keys: tuple[str, ...]
    pending_unit_keys: tuple[str, ...]
    next_unit_key: str | None


@dataclass(frozen=True, slots=True)
class StageProgress:
    stage: JobStage
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class JobProgress:
    stages: tuple[StageProgress, ...]
    completed: int
    total: int

    def stage(self, stage: JobStage) -> StageProgress:
        for progress in self.stages:
            if progress.stage is stage:
                return progress
        raise KeyError(stage)


def effective_input_hash(
    declared_input_hash: str | None,
    dependency_outputs: Sequence[str],
    external_input_hash: str | None,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "declared_input_hash": declared_input_hash,
                "dependency_outputs": list(dependency_outputs),
                "external_input_hash": external_input_hash,
            }
        )
    )

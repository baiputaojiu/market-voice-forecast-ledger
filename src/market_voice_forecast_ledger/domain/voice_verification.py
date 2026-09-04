from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from typing import Final, Literal

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerThresholdConfig,
)


PRESENCE_VAD_CONTRACT_VERSION: Final = "vad-v2"


class VoiceProposal(StrEnum):
    LIKELY_PRESENT = "likely_present"
    LIKELY_ABSENT = "likely_absent"
    NEEDS_REVIEW = "needs_review"


class ReviewAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class ReferenceClipCommand:
    subject_id: int
    video_id: int
    start_ms: int
    end_ms: int
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    subject_id: int
    sample_kind: Literal["held_out_positive", "negative"]
    score: float


@dataclass(frozen=True, slots=True)
class VoiceCalibration:
    subject_boundary: float
    interviewer_boundary: float
    margin: float

    def to_threshold_config(
        self,
        *,
        version: str,
        model_name: str,
        model_version: str,
    ) -> SpeakerThresholdConfig:
        return SpeakerThresholdConfig(
            version=version,
            model_name=model_name,
            model_version=model_version,
            subject_rule=ScoreRule("gte", self.subject_boundary),
            interviewer_rule=ScoreRule("lte", self.interviewer_boundary),
        )


@dataclass(frozen=True, slots=True)
class VoiceManifestSnapshot:
    candidate_id: int
    video_id: int
    profile_id: int
    presence_decision_id: int
    presence_decision_hash: str
    reference_profile_id: int
    reference_feature_hash: str
    threshold_config_version: str
    model_name: str
    model_version: str
    adapter_version: str
    vad_contract_version: str
    selection_contract_version: str


@dataclass(frozen=True, slots=True)
class VoiceSegmentScore:
    ordinal: int
    start_ms: int
    end_ms: int
    raw_match_score: float
    evidence_hash: str


@dataclass(frozen=True, slots=True)
class VoiceRunResult:
    job_id: int
    candidate_id: int
    input_hash: str
    output_hash: str
    proposal: VoiceProposal
    result_code: str


PRESENCE_UNITS = (
    ("video:validate", JobStage.VIDEO_METADATA),
    ("audio:acquire", JobStage.AUDIO_ACQUISITION),
    ("audio:normalize", JobStage.AUDIO_ACQUISITION),
    ("voice:vad", JobStage.SPEAKER_ASSIGNMENT),
    ("voice:score", JobStage.SPEAKER_ASSIGNMENT),
    ("voice:proposal", JobStage.SPEAKER_ASSIGNMENT),
    ("audio:cleanup", JobStage.SPEAKER_ASSIGNMENT),
)


def calibrate_thresholds(samples: Sequence[CalibrationSample]) -> VoiceCalibration:
    positives = tuple(
        item.score for item in samples if item.sample_kind == "held_out_positive"
    )
    negatives = tuple(
        item.score for item in samples if item.sample_kind == "negative"
    )
    if (
        not positives
        or not negatives
        or any(
            not isfinite(value) or not -1.0 <= value <= 1.0
            for value in (*positives, *negatives)
        )
    ):
        raise DomainError(
            "VOICE_CALIBRATION_INVALID", "calibration samples are invalid"
        )
    minimum_positive = min(positives)
    maximum_negative = max(negatives)
    if minimum_positive <= maximum_negative:
        raise DomainError(
            "VOICE_MODEL_NOT_SEPARABLE", "voice model is not separable"
        )
    return VoiceCalibration(
        subject_boundary=minimum_positive,
        interviewer_boundary=maximum_negative,
        margin=minimum_positive - maximum_negative,
    )


def classify_presence_score(
    score: float, calibration: VoiceCalibration
) -> VoiceProposal:
    if not isfinite(score) or not -1.0 <= score <= 1.0:
        raise DomainError("VOICE_SCORE_INVALID", "voice score is invalid")
    if ScoreRule("gte", calibration.subject_boundary).matches(score):
        return VoiceProposal.LIKELY_PRESENT
    if ScoreRule("lte", calibration.interviewer_boundary).matches(score):
        return VoiceProposal.LIKELY_ABSENT
    return VoiceProposal.NEEDS_REVIEW


def build_presence_job_manifest(snapshot: VoiceManifestSnapshot) -> JobManifest:
    declared_input_hash = sha256_text(canonical_json(asdict(snapshot)))
    units: list[ManifestUnit] = []
    previous_key: str | None = None
    for ordinal, (unit_key, stage) in enumerate(PRESENCE_UNITS, start=1):
        execution_contract_hash = sha256_text(
            canonical_json(
                {
                    "adapter_version": snapshot.adapter_version,
                    "model_name": snapshot.model_name,
                    "model_version": snapshot.model_version,
                    "selection_contract_version": snapshot.selection_contract_version,
                    "unit_key": unit_key,
                    "vad_contract_version": snapshot.vad_contract_version,
                }
            )
        )
        units.append(
            ManifestUnit(
                unit_key=unit_key,
                stage=stage,
                ordinal=ordinal,
                declared_input_hash=declared_input_hash,
                dependency_keys=() if previous_key is None else (previous_key,),
                execution_contract_hash=execution_contract_hash,
            )
        )
        previous_key = unit_key
    return JobManifest.build(JobKind.VIDEO_PIPELINE, units)

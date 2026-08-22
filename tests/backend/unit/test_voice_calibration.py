from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from math import nan

import pytest

from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import (
    CalibrationSample,
    ReferenceClipCommand,
    ReviewAction,
    VoiceManifestSnapshot,
    VoiceProposal,
    VoiceRunResult,
    VoiceSegmentScore,
    build_presence_job_manifest,
    calibrate_thresholds,
    classify_presence_score,
)
from market_voice_forecast_ledger.services.voice_reference import (
    ApprovedReferenceClip,
    canonical_reference_approval_hash,
)


def _snapshot() -> VoiceManifestSnapshot:
    return VoiceManifestSnapshot(
        candidate_id=101,
        video_id=202,
        profile_id=303,
        presence_decision_id=404,
        presence_decision_hash="a" * 64,
        reference_profile_id=505,
        reference_feature_hash="b" * 64,
        threshold_config_version="threshold-v1",
        model_name="speaker-model.onnx",
        model_version="model-v1",
        adapter_version="adapter-v1",
        vad_contract_version="vad-v1",
        selection_contract_version="selection-v1",
    )


def test_calibration_uses_global_extrema_and_classifies_closed_boundaries() -> None:
    calibration = calibrate_thresholds(
        (
            CalibrationSample(1, "held_out_positive", 0.81),
            CalibrationSample(1, "negative", 0.22),
            CalibrationSample(2, "held_out_positive", 0.73),
            CalibrationSample(2, "negative", 0.31),
        )
    )

    assert calibration.subject_boundary == 0.73
    assert calibration.interviewer_boundary == 0.31
    assert calibration.margin == pytest.approx(0.42)
    threshold_config = calibration.to_threshold_config(
        version="threshold-v1",
        model_name="speaker-model.onnx",
        model_version="model-v1",
    )
    assert threshold_config.subject_rule.operator == "gte"
    assert threshold_config.subject_rule.boundary == 0.73
    assert threshold_config.interviewer_rule.operator == "lte"
    assert threshold_config.interviewer_rule.boundary == 0.31
    assert classify_presence_score(0.73, calibration) is VoiceProposal.LIKELY_PRESENT
    assert classify_presence_score(0.31, calibration) is VoiceProposal.LIKELY_ABSENT
    assert classify_presence_score(0.50, calibration) is VoiceProposal.NEEDS_REVIEW


def test_calibration_requires_global_separation() -> None:
    samples = (
        CalibrationSample(
            subject_id=1,
            sample_kind="held_out_positive",
            score=0.71,
        ),
        CalibrationSample(subject_id=1, sample_kind="negative", score=0.72),
    )
    with pytest.raises(DomainError) as caught:
        calibrate_thresholds(samples)

    assert caught.value.code == "VOICE_MODEL_NOT_SEPARABLE"


@pytest.mark.parametrize(
    "samples",
    (
        (CalibrationSample(1, "held_out_positive", 0.71),),
        (CalibrationSample(1, "negative", 0.21),),
        (
            CalibrationSample(1, "held_out_positive", nan),
            CalibrationSample(1, "negative", 0.21),
        ),
    ),
)
def test_calibration_rejects_missing_bands_and_nonfinite_scores(samples) -> None:
    with pytest.raises(DomainError) as caught:
        calibrate_thresholds(samples)

    assert caught.value.code == "VOICE_CALIBRATION_INVALID"


def test_classification_rejects_nonfinite_scores() -> None:
    calibration = calibrate_thresholds(
        (
            CalibrationSample(1, "held_out_positive", 0.71),
            CalibrationSample(1, "negative", 0.21),
        )
    )

    with pytest.raises(DomainError) as caught:
        classify_presence_score(nan, calibration)

    assert caught.value.code == "VOICE_SCORE_INVALID"


def test_presence_manifest_has_exact_contiguous_dependency_chain_and_hashes() -> None:
    manifest = build_presence_job_manifest(_snapshot())

    assert manifest.kind is JobKind.VIDEO_PIPELINE
    assert tuple(
        (unit.unit_key, unit.stage, unit.ordinal, unit.dependency_keys)
        for unit in manifest.units
    ) == (
        ("video:validate", JobStage.VIDEO_METADATA, 1, ()),
        ("audio:acquire", JobStage.AUDIO_ACQUISITION, 2, ("video:validate",)),
        ("audio:normalize", JobStage.AUDIO_ACQUISITION, 3, ("audio:acquire",)),
        ("voice:vad", JobStage.SPEAKER_ASSIGNMENT, 4, ("audio:normalize",)),
        ("voice:score", JobStage.SPEAKER_ASSIGNMENT, 5, ("voice:vad",)),
        ("voice:proposal", JobStage.SPEAKER_ASSIGNMENT, 6, ("voice:score",)),
        ("audio:cleanup", JobStage.SPEAKER_ASSIGNMENT, 7, ("voice:proposal",)),
    )
    assert {unit.declared_input_hash for unit in manifest.units} == {
        "e36deeb0d7b2c3fe6b4e1f822abde10428889d24c3bb993c9917fa1781a2305b"
    }
    assert tuple(unit.execution_contract_hash for unit in manifest.units) == (
        "fbf0acc0cbf9ab031eba6d4977a00910c778340e464ab0d4cc5eef5017fb9fa7",
        "0ed6e4eeb32f8757979b668bd111815cbc405aa80332182743507c754f4fbab4",
        "bdfff8aa31e0a84072075ab539e1d6a16b00039a0525384b9e0405ae220af779",
        "0e7805ae273aeb6931d7a3a1492cffc0a90236331572d41b02280f4c9bbbd953",
        "34e18705ae636cb1a419a2732c33525d8ec6f461278223203b145649eb203f53",
        "5f29be9898f28e6c6bdb03272e816fdbb2d61471f31dd8be6e7a1ab84ac3cd3a",
        "757598aa2a87e02f77147b650162ff71730a3105a4f6929e81c0d87f6a12fdbf",
    )


def test_voice_commands_and_results_are_immutable_proposal_only_records() -> None:
    command = ReferenceClipCommand(
        subject_id=1,
        video_id=2,
        start_ms=1_000,
        end_ms=3_000,
        actor="local_user",
        reason="clear solo speech",
    )
    segment = VoiceSegmentScore(1, 1_000, 3_000, 0.75, "c" * 64)
    result = VoiceRunResult(
        job_id=10,
        candidate_id=20,
        input_hash="d" * 64,
        output_hash="e" * 64,
        proposal=VoiceProposal.LIKELY_PRESENT,
        result_code="VOICE_PROPOSAL_READY",
    )

    assert command.actor == "local_user"
    assert segment.raw_match_score == 0.75
    assert result.proposal is VoiceProposal.LIKELY_PRESENT
    assert tuple(action.value for action in ReviewAction) == (
        "confirm",
        "reject",
        "hold",
    )
    with pytest.raises(FrozenInstanceError):
        result.proposal = VoiceProposal.LIKELY_ABSENT


def test_reference_approval_hash_binds_public_approval_and_record_is_immutable() -> None:
    approved_at = datetime(2026, 8, 22, 1, 2, 3, tzinfo=timezone.utc)
    approval_hash = canonical_reference_approval_hash(
        subject_id=1,
        video_id=2,
        start_ms=1_000,
        end_ms=16_000,
        ordinal=1,
        clip_kind="enrollment",
        actor="local_user",
        reason="clear solo speech",
        approved_at=approved_at,
    )
    approval = ApprovedReferenceClip(
        subject_id=1,
        video_id=2,
        start_ms=1_000,
        end_ms=16_000,
        ordinal=1,
        clip_kind="enrollment",
        actor="local_user",
        reason="clear solo speech",
        approved_at=approved_at,
        approval_hash=approval_hash,
    )

    assert approval_hash == canonical_reference_approval_hash(
        subject_id=approval.subject_id,
        video_id=approval.video_id,
        start_ms=approval.start_ms,
        end_ms=approval.end_ms,
        ordinal=approval.ordinal,
        clip_kind=approval.clip_kind,
        actor=approval.actor,
        reason=approval.reason,
        approved_at=approval.approved_at,
    )
    assert approval_hash != canonical_reference_approval_hash(
        subject_id=approval.subject_id,
        video_id=approval.video_id,
        start_ms=approval.start_ms,
        end_ms=approval.end_ms + 1,
        ordinal=approval.ordinal,
        clip_kind=approval.clip_kind,
        actor=approval.actor,
        reason=approval.reason,
        approved_at=approval.approved_at,
    )
    with pytest.raises(FrozenInstanceError):
        approval.reason = "changed"

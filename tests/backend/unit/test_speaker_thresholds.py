from market_voice_forecast_ledger.domain.enums import AssignmentKind
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerThresholdConfig,
    classify_raw_score,
)


def test_raw_score_is_not_normalized_and_border_band_is_hold():
    config = SpeakerThresholdConfig(
        version="synthetic-threshold-v1",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.50),
        interviewer_rule=ScoreRule("lte", 0.50),
    )

    assert classify_raw_score(1.73, config) is AssignmentKind.SUBJECT
    assert classify_raw_score(0.90, config) is AssignmentKind.HOLD
    assert classify_raw_score(0.25, config) is AssignmentKind.INTERVIEWER


def test_exact_configured_boundaries_are_inclusive():
    config = SpeakerThresholdConfig(
        version="synthetic-threshold-v1",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.50),
        interviewer_rule=ScoreRule("lte", 0.50),
    )

    assert classify_raw_score(1.50, config) is AssignmentKind.SUBJECT
    assert classify_raw_score(0.50, config) is AssignmentKind.INTERVIEWER

from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    EligibilityStatus,
    PolicyKind,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy
from market_voice_forecast_ledger.services.channel_policy import evaluate_policy


def test_configuration_required_blocks_before_channel_resolution():
    decision = evaluate_policy(
        ChannelPolicy(
            policy_kind=PolicyKind.FIXED_CHANNEL,
            configuration_status=ConfigurationStatus.CONFIGURATION_REQUIRED,
        ),
        None,
    )

    assert decision.status is EligibilityStatus.CONFIGURATION_REQUIRED
    assert decision.may_download_audio is False
    assert decision.may_analyze is False
    assert decision.reason == "CHANNEL_CONFIGURATION_REQUIRED"


def test_unresolved_video_channel_fails_closed_for_all_channels_policy():
    decision = evaluate_policy(
        ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        ),
        None,
    )

    assert decision.status is EligibilityStatus.CHANNEL_UNRESOLVED
    assert decision.may_download_audio is False
    assert decision.may_analyze is False
    assert decision.reason == "VIDEO_CHANNEL_UNRESOLVED"


def test_resolved_video_is_eligible_for_all_channels_policy():
    decision = evaluate_policy(
        ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        ),
        "UC1111111111111111111111",
    )

    assert decision.status is EligibilityStatus.ELIGIBLE
    assert decision.may_download_audio is True
    assert decision.may_analyze is True
    assert decision.reason == "ALL_CHANNELS"


def test_fixed_channel_compares_authoritative_id_not_display_name():
    policy = ChannelPolicy(
        policy_kind=PolicyKind.FIXED_CHANNEL,
        configuration_status=ConfigurationStatus.CONFIGURED,
        youtube_channel_id="UC2222222222222222222222",
        channel_display_name="Same Display Name",
    )

    mismatch = evaluate_policy(policy, "UC3333333333333333333333")
    match = evaluate_policy(policy, "UC2222222222222222222222")

    assert mismatch.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE
    assert mismatch.may_download_audio is False
    assert mismatch.may_analyze is False
    assert mismatch.reason == "FIXED_CHANNEL_MISMATCH"
    assert match.status is EligibilityStatus.ELIGIBLE
    assert match.may_download_audio is True
    assert match.may_analyze is True
    assert match.reason == "FIXED_CHANNEL_MATCH"

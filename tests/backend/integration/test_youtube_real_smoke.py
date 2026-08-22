from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.credentials.windows import (
    WindowsCredentialManager,
)
from market_voice_forecast_ledger.youtube.client import (
    UrllibYouTubeTransport,
    YouTubeClient,
)


SMOKE_SKIP_REASON = "real YouTube operational acceptance not requested"
APPROVED_SMOKE_CHANNEL_ID = "UCXvjRTXoDa8tKwdkTaukGug"
SMOKE_CONFIGURATION_FAILURE = "opt-in YouTube smoke configuration is incomplete"
SMOKE_RESPONSE_FAILURE = "YouTube smoke response shape is invalid"
SMOKE_PRIVACY_CONTROL_FAILURE = "YouTube smoke privacy control failed"
PROVIDER_BODY_SENTINEL = "private-provider-body-sentinel"
PROVIDER_HEADER_SENTINEL = "private-provider-header-sentinel"


def _smoke_clock() -> datetime:
    return datetime.now(timezone.utc)


def _require_safe_smoke_shape(channels, videos, video_id: str) -> None:
    try:
        channel = channels[0] if len(channels) == 1 else None
        video = videos[0] if len(videos) == 1 else None
        valid = (
            channel is not None
            and channel.channel_id == APPROVED_SMOKE_CHANNEL_ID
            and type(channel.uploads_playlist_id) is str
            and channel.uploads_playlist_id.startswith("UU")
            and len(channel.uploads_playlist_id) == 24
            and type(video) is dict
            and video.get("id") == video_id
            and type(video.get("snippet")) is dict
            and type(video.get("contentDetails")) is dict
            and type(video.get("status")) is dict
        )
    except Exception:
        valid = False
    if valid is not True:
        pytest.fail("YouTube smoke response shape is invalid", pytrace=False)


def test_real_youtube_read_only_operational_smoke_is_opt_in():
    if os.environ.get("MVFL_RUN_YOUTUBE_SMOKE") != "1":
        pytest.skip(SMOKE_SKIP_REASON)

    video_id = os.environ.get("MVFL_YOUTUBE_SMOKE_VIDEO_ID")
    if video_id is None:
        pytest.fail(
            "opt-in YouTube smoke configuration is incomplete",
            pytrace=False,
        )

    client = YouTubeClient(
        transport=UrllibYouTubeTransport(),
        credential_store=WindowsCredentialManager(),
        reserve_attempt=lambda _endpoint, _attempt, _attempted_at: None,
        sleeper=lambda _seconds: None,
        clock=_smoke_clock,
    )
    channels = client.channels_uploads((APPROVED_SMOKE_CHANNEL_ID,))
    videos = client.videos((video_id,))

    _require_safe_smoke_shape(channels, videos, video_id)


def test_fake_malformed_smoke_shapes_never_disclose_provider_sentinels():
    class SentinelChannel:
        def __init__(self, value: str) -> None:
            self.channel_id = value
            self.uploads_playlist_id = value

        def __repr__(self) -> str:
            return self.channel_id

    valid_channel = SentinelChannel(APPROVED_SMOKE_CHANNEL_ID)
    valid_channel.uploads_playlist_id = "UU" + "A" * 22
    valid_video = {
        "id": "safevideo01",
        "snippet": {},
        "contentDetails": {},
        "status": {},
    }
    malformed_cases = (
        (
            PROVIDER_BODY_SENTINEL,
            (valid_channel,),
            ({"id": "safevideo01", "snippet": PROVIDER_BODY_SENTINEL},),
        ),
        (
            PROVIDER_HEADER_SENTINEL,
            (SentinelChannel(PROVIDER_HEADER_SENTINEL),),
            (valid_video,),
        ),
    )
    for sentinel, channels, videos in malformed_cases:
        with pytest.raises(pytest.fail.Exception) as caught:
            _require_safe_smoke_shape(channels, videos, "safevideo01")
        rendered = f"{caught.value!s} {caught.value!r}"
        if (
            str(caught.value) != SMOKE_RESPONSE_FAILURE
            or sentinel in rendered
        ):
            pytest.fail(SMOKE_PRIVACY_CONTROL_FAILURE, pytrace=False)


def test_real_smoke_has_no_rewritten_assertions_or_dynamic_failure_messages():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    targets = tuple(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {
            "_require_safe_smoke_shape",
            "test_real_youtube_read_only_operational_smoke_is_opt_in",
        }
    )
    assert not tuple(
        node
        for target in targets
        for node in ast.walk(target)
        if isinstance(node, ast.Assert)
    )
    fail_calls = tuple(
        node
        for target in targets
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "fail"
    )
    assert fail_calls
    assert all(
        call.args
        and isinstance(call.args[0], ast.Constant)
        and type(call.args[0].value) is str
        and any(
            keyword.arg == "pytrace"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
        for call in fail_calls
    )

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    SearchWindow,
    canonical_profile_hash,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.youtube.client import (
    ChannelUploads,
    YouTubePage,
)
from market_voice_forecast_ledger.youtube.discovery import (
    CrossChannelSearchDiscoverer,
    DiscoveredIdPage,
    ManualUrlDiscoverer,
    SeedUploadsDiscoverer,
    extract_youtube_video_id,
)
from tests.backend.youtube_fakes import (
    FIXED_NOW,
    FakeYouTubeClient,
    synthetic_video_item,
)


CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
OTHER_CHANNEL_ID = "UCbcdefghijklmnopqrstuvw"
UPLOADS_PLAYLIST_ID = "UUabcdefghijklmnopqrstuv"
OTHER_UPLOADS_PLAYLIST_ID = "UUbcdefghijklmnopqrstuvw"
VIDEO_ID = "abcdefghijk"
OTHER_VIDEO_ID = "bcdefghijkl"
THIRD_VIDEO_ID = "cdefghijklm"


def _profile(
    *,
    profile_id: int = 17,
    subject_id: int = 23,
    seed_channel_ids: tuple[str, ...] = (CHANNEL_ID,),
    search_terms: tuple[str, ...] = ("Synthetic Person",),
) -> DiscoveryProfileVersion:
    return DiscoveryProfileVersion(
        id=31,
        profile_id=profile_id,
        subject_id=subject_id,
        config_hash=canonical_profile_hash(seed_channel_ids, search_terms),
        seed_channel_ids=seed_channel_ids,
        search_terms=search_terms,
        created_at=FIXED_NOW,
    )


def _window(
    *,
    lower_bound: datetime = datetime(2023, 8, 18, tzinfo=timezone.utc),
    upper_bound: datetime = datetime(2026, 8, 18, tzinfo=timezone.utc),
) -> SearchWindow:
    return SearchWindow(
        id=41,
        job_id=43,
        unit_key="youtube:profile:17:search",
        ordinal=1,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        next_page_token=None,
        page_count=0,
        split_parent_id=None,
        completed_at=None,
        window_hash="f" * 64,
    )


def _playlist_item(
    video_id: str,
    *,
    resource_video_id: str | None = None,
    title: str = "Synthetic macro discussion without a person name",
) -> dict[str, object]:
    return {
        "contentDetails": {"videoId": video_id},
        "snippet": {
            "playlistId": UPLOADS_PLAYLIST_ID,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": resource_video_id or video_id,
            },
            "title": title,
        },
    }


def _search_item(video_id: str) -> dict[str, object]:
    return {"id": {"kind": "youtube#video", "videoId": video_id}}


def _assert_discovery_invalid(callable_) -> None:
    with pytest.raises(DomainError) as captured:
        callable_()
    assert captured.value.code == "YOUTUBE_DISCOVERY_INVALID"
    assert str(captured.value) == "YouTube discovery response is invalid"


def _assert_url_invalid(value: object) -> None:
    with pytest.raises(DomainError) as captured:
        extract_youtube_video_id(value)  # type: ignore[arg-type]
    assert captured.value.code == "INVALID_YOUTUBE_URL"
    assert str(captured.value) == "YouTube URL is invalid"


@pytest.mark.parametrize(
    "url",
    (
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://m.youtube.com/shorts/{VIDEO_ID}",
        f"https://youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://m.youtube.com/live/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
    ),
)
def test_url_parser_accepts_only_the_four_approved_https_form_families(url: str):
    assert extract_youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    (
        "",
        "arbitrary text",
        f"watch this {VIDEO_ID}",
        f"watch https://youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID} trailing",
        f"http://youtube.com/watch?v={VIDEO_ID}",
        f"ftp://youtube.com/watch?v={VIDEO_ID}",
        f"//youtube.com/watch?v={VIDEO_ID}",
        f"HTTPS://youtube.com/watch?v={VIDEO_ID}",
        f"https://YOUTUBE.COM/watch?v={VIDEO_ID}",
        f"https://youtube.com./watch?v={VIDEO_ID}",
        f"https://youtube.com:443/watch?v={VIDEO_ID}",
        f"https://user@youtube.com/watch?v={VIDEO_ID}",
        f"https://user:pass@youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com.evil.invalid/watch?v={VIDEO_ID}",
        f"https://www.youtube.com.evil.invalid/watch?v={VIDEO_ID}",
        f"https://evil.invalid/youtube.com/watch?v={VIDEO_ID}",
        f"https://www.yоutube.com/watch?v={VIDEO_ID}",
        f"https://ｗｗｗ.youtube.com/watch?v={VIDEO_ID}",
        f"https://xn--yutube-wqf.com/watch?v={VIDEO_ID}",
        f"https://youtube。com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}#fragment",
        f"https://youtube.com/watch?v={VIDEO_ID}#",
        f"https://youtu.be/{VIDEO_ID}#fragment",
        f"https://youtube.com/watch?v={VIDEO_ID}&feature=share",
        f"https://youtube.com/watch?feature=share&v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}&v={OTHER_VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}",
        "https://youtube.com/watch?v=",
        f"https://youtube.com/watch?V={VIDEO_ID}",
        f"https://youtube.com/watch/{VIDEO_ID}",
        f"https://youtube.com//watch?v={VIDEO_ID}",
        f"https://youtube.com/embed/{VIDEO_ID}",
        f"https://youtube.com/v/{VIDEO_ID}",
        f"https://youtube.com/playlist?list={VIDEO_ID}",
        f"https://youtube.com/channel/{CHANNEL_ID}",
        f"https://youtube.com/@synthetic?v={VIDEO_ID}",
        f"https://youtube.com/shorts/{VIDEO_ID}/",
        f"https://youtube.com/shorts/{VIDEO_ID}?",
        f"https://youtube.com/shorts/{VIDEO_ID}?feature=share",
        f"https://youtube.com/live/{VIDEO_ID}?",
        f"https://youtube.com/live/{VIDEO_ID}?feature=share",
        f"https://youtu.be/{VIDEO_ID}/",
        f"https://youtu.be/{VIDEO_ID}?",
        f"https://youtu.be/{VIDEO_ID}?si=tracking",
        f"https://youtu.be/{VIDEO_ID}?v={OTHER_VIDEO_ID}",
        f"https://youtube.com/watch?v=%61bcdefghij",
        f"https://youtube.com/%77atch?v={VIDEO_ID}",
        f"https://youtu.be/%61bcdefghij",
        "https://youtube.com/watch?v=https%3A%2F%2Fyoutu.be%2Fabcdefghijk",
        "https://youtube.com/watch?v=https://youtu.be/abcdefghijk",
        "https://youtube.com/watch?v=abcdefghij",
        "https://youtube.com/watch?v=abcdefghijkl",
        "https://youtube.com/watch?v=abcdefghij!",
        f" https://youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID} ",
        f"https://youtube.com/watch?v={VIDEO_ID}\n",
        f"https://you\ttube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}\x00",
        f"https://youtube.com/watch?v={VIDEO_ID}\u00a0",
    ),
)
def test_url_parser_rejects_spoofing_ambiguity_tracking_and_noncanonical_text(
    url: str,
):
    _assert_url_invalid(url)


@pytest.mark.parametrize("value", (None, True, 7, b"https://youtu.be/abcdefghijk", []))
def test_url_parser_requires_an_exact_string(value: object):
    _assert_url_invalid(value)


def test_seed_resolves_exactly_one_matching_uploads_playlist():
    client = FakeYouTubeClient(
        channel_responses=((ChannelUploads(CHANNEL_ID, UPLOADS_PLAYLIST_ID),),)
    )

    playlist_id = SeedUploadsDiscoverer(client).resolve_uploads_playlist(
        CHANNEL_ID
    )

    assert playlist_id == UPLOADS_PLAYLIST_ID
    assert client.channel_calls == [(CHANNEL_ID,)]


@pytest.mark.parametrize(
    "response",
    (
        (),
        [],
        (ChannelUploads(OTHER_CHANNEL_ID, UPLOADS_PLAYLIST_ID),),
        (ChannelUploads(CHANNEL_ID, OTHER_UPLOADS_PLAYLIST_ID),) * 2,
        (True,),
    ),
)
def test_seed_playlist_resolution_fails_closed_on_missing_duplicate_or_mismatch(
    response: object,
):
    client = FakeYouTubeClient(channel_responses=(response,))
    _assert_discovery_invalid(
        lambda: SeedUploadsDiscoverer(client).resolve_uploads_playlist(CHANNEL_ID)
    )


@pytest.mark.parametrize("channel_id", (True, "UCshort", "", [], None))
def test_seed_playlist_resolution_rejects_unsafe_channel_id_before_client(
    channel_id: object,
):
    client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: SeedUploadsDiscoverer(client).resolve_uploads_playlist(channel_id)  # type: ignore[arg-type]
    )
    assert client.channel_calls == []


def test_seed_page_returns_every_playlist_video_without_name_filtering():
    items = (
        _playlist_item(VIDEO_ID, title="Weekly synthetic market wrap"),
        _playlist_item(OTHER_VIDEO_ID, title="An unrelated synthetic title"),
    )
    client = FakeYouTubeClient(
        playlist_responses=(YouTubePage(items=items, next_page_token="TOKEN_2"),)
    )

    page = SeedUploadsDiscoverer(client).page_video_ids(
        UPLOADS_PLAYLIST_ID, "TOKEN_1"
    )

    assert page == DiscoveredIdPage(
        video_ids=(VIDEO_ID, OTHER_VIDEO_ID), next_page_token="TOKEN_2"
    )
    assert client.playlist_calls == [(UPLOADS_PLAYLIST_ID, "TOKEN_1")]


@pytest.mark.parametrize(
    "page",
    (
        YouTubePage(items=(_playlist_item(VIDEO_ID), _playlist_item(VIDEO_ID)), next_page_token=None),
        YouTubePage(items=(_playlist_item(VIDEO_ID, resource_video_id=OTHER_VIDEO_ID),), next_page_token=None),
        YouTubePage(items=({"contentDetails": [], "snippet": {}},), next_page_token=None),
        YouTubePage(items=(_playlist_item(VIDEO_ID),), next_page_token=True),
        YouTubePage(items=[_playlist_item(VIDEO_ID)], next_page_token=None),
        {"items": [_playlist_item(VIDEO_ID)]},
    ),
)
def test_seed_page_fails_closed_on_duplicate_mismatched_or_invalid_page_shape(
    page: object,
):
    client = FakeYouTubeClient(playlist_responses=(page,))
    _assert_discovery_invalid(
        lambda: SeedUploadsDiscoverer(client).page_video_ids(
            UPLOADS_PLAYLIST_ID, None
        )
    )


def test_search_joins_only_ordered_profile_terms_with_pipe_and_exact_window():
    profile = _profile(search_terms=("千竈鉄平", "千竃鉄平"))
    window = _window()
    items = (_search_item(VIDEO_ID), _search_item(OTHER_VIDEO_ID))
    client = FakeYouTubeClient(
        search_responses=(YouTubePage(items=items, next_page_token="TOKEN_2"),)
    )

    page = CrossChannelSearchDiscoverer(client).page_video_ids(
        profile, window, "TOKEN_1"
    )

    assert page == DiscoveredIdPage(
        video_ids=(VIDEO_ID, OTHER_VIDEO_ID), next_page_token="TOKEN_2"
    )
    assert client.search_calls == [(
        "千竈鉄平|千竃鉄平",
        "2023-08-18T00:00:00.000000Z",
        "2026-08-18T00:00:00.000000Z",
        "TOKEN_1",
    )]


def test_search_preserves_term_order_and_ignores_subject_identity_and_seed_values():
    items = (_search_item(VIDEO_ID),)
    first_profile = _profile(
        profile_id=1,
        subject_id=101,
        seed_channel_ids=(CHANNEL_ID,),
        search_terms=("term-two", "term-one"),
    )
    second_profile = _profile(
        profile_id=999,
        subject_id=777,
        seed_channel_ids=(),
        search_terms=("term-two", "term-one"),
    )
    first_client = FakeYouTubeClient(
        search_responses=(YouTubePage(items=items, next_page_token=None),)
    )
    second_client = FakeYouTubeClient(
        search_responses=(YouTubePage(items=items, next_page_token=None),)
    )

    CrossChannelSearchDiscoverer(first_client).page_video_ids(
        first_profile, _window(), None
    )
    CrossChannelSearchDiscoverer(second_client).page_video_ids(
        second_profile, _window(), None
    )

    assert first_client.search_calls == second_client.search_calls
    assert first_client.search_calls[0][0] == "term-two|term-one"


@pytest.mark.parametrize(
    "profile",
    (
        replace(_profile(), search_terms=()),
        replace(_profile(), search_terms=("term", "term")),
        replace(_profile(), search_terms=["term"]),  # type: ignore[arg-type]
        replace(_profile(), search_terms=("term\nnext",)),
        True,
    ),
)
def test_search_rejects_invalid_profile_terms_before_client(profile: object):
    client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: CrossChannelSearchDiscoverer(client).page_video_ids(
            profile, _window(), None  # type: ignore[arg-type]
        )
    )
    assert client.search_calls == []


@pytest.mark.parametrize(
    "window",
    (
        replace(_window(), lower_bound=datetime(2023, 8, 18)),
        replace(
            _window(),
            upper_bound=datetime(
                2026, 8, 18, 9, tzinfo=timezone(timedelta(hours=9))
            ),
        ),
        replace(
            _window(),
            lower_bound=datetime(2026, 8, 18, tzinfo=timezone.utc),
        ),
        replace(
            _window(),
            lower_bound=datetime(2027, 8, 18, tzinfo=timezone.utc),
        ),
        True,
    ),
)
def test_search_rejects_nonexact_or_nonincreasing_search_window_before_client(
    window: object,
):
    client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: CrossChannelSearchDiscoverer(client).page_video_ids(
            _profile(), window, None  # type: ignore[arg-type]
        )
    )
    assert client.search_calls == []


@pytest.mark.parametrize("token", (True, "", "bad token", "a" * 513, [], {}))
def test_discoverers_reject_noncanonical_input_page_tokens_before_client(
    token: object,
):
    seed_client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: SeedUploadsDiscoverer(seed_client).page_video_ids(
            UPLOADS_PLAYLIST_ID, token  # type: ignore[arg-type]
        )
    )
    assert seed_client.playlist_calls == []

    search_client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: CrossChannelSearchDiscoverer(search_client).page_video_ids(
            _profile(), _window(), token  # type: ignore[arg-type]
        )
    )
    assert search_client.search_calls == []


@pytest.mark.parametrize(
    "page",
    (
        YouTubePage(items=(_search_item(VIDEO_ID), _search_item(VIDEO_ID)), next_page_token=None),
        YouTubePage(items=({"id": {"kind": "youtube#channel", "videoId": VIDEO_ID}},), next_page_token=None),
        YouTubePage(items=({"id": {"kind": "youtube#video", "videoId": True}},), next_page_token=None),
        YouTubePage(items=(_search_item(VIDEO_ID),), next_page_token="bad token"),
        YouTubePage(items=[_search_item(VIDEO_ID)], next_page_token=None),
        {"items": [_search_item(VIDEO_ID)]},
    ),
)
def test_search_page_fails_closed_on_duplicate_mismatched_or_invalid_shape(
    page: object,
):
    client = FakeYouTubeClient(search_responses=(page,))
    _assert_discovery_invalid(
        lambda: CrossChannelSearchDiscoverer(client).page_video_ids(
            _profile(), _window(), None
        )
    )


def test_manual_discoverer_receives_parsed_id_and_always_uses_videos_list():
    item = synthetic_video_item(
        video_id=VIDEO_ID,
        title="Synthetic manual candidate",
    )
    client = FakeYouTubeClient(video_responses=((item,),))

    result = ManualUrlDiscoverer(client, clock=lambda: FIXED_NOW).fetch(VIDEO_ID)

    assert client.video_calls == [(VIDEO_ID,)]
    assert len(result) == 1
    assert result[0].youtube_video_id == VIDEO_ID
    assert result[0].title == "Synthetic manual candidate"
    assert result[0].fetched_at == FIXED_NOW


def test_manual_missing_private_or_deleted_video_returns_safe_unavailable_tuple():
    private = synthetic_video_item(video_id=VIDEO_ID, privacy_status="private")
    deleted = synthetic_video_item(video_id=VIDEO_ID, upload_status="deleted")
    for response in ((), (private,), (deleted,)):
        client = FakeYouTubeClient(video_responses=(response,))
        result = ManualUrlDiscoverer(client, clock=lambda: FIXED_NOW).fetch(VIDEO_ID)
        assert result == ()
        assert client.video_calls == [(VIDEO_ID,)]


@pytest.mark.parametrize(
    "response",
    (
        [synthetic_video_item(video_id=VIDEO_ID)],
        (synthetic_video_item(video_id=OTHER_VIDEO_ID),),
        (
            synthetic_video_item(video_id=VIDEO_ID),
            synthetic_video_item(video_id=VIDEO_ID),
        ),
        (True,),
    ),
)
def test_manual_fails_closed_on_duplicate_mismatched_or_invalid_video_results(
    response: object,
):
    client = FakeYouTubeClient(video_responses=(response,))
    _assert_discovery_invalid(
        lambda: ManualUrlDiscoverer(client, clock=lambda: FIXED_NOW).fetch(
            VIDEO_ID
        )
    )


@pytest.mark.parametrize(
    "video_id",
    (
        f"https://youtu.be/{VIDEO_ID}",
        "short",
        "abcdefghij!",
        True,
        [],
    ),
)
def test_manual_requires_an_already_parsed_canonical_video_id_before_client(
    video_id: object,
):
    client = FakeYouTubeClient()
    _assert_discovery_invalid(
        lambda: ManualUrlDiscoverer(client, clock=lambda: FIXED_NOW).fetch(video_id)  # type: ignore[arg-type]
    )
    assert client.video_calls == []


@pytest.mark.parametrize(
    "now",
    (
        datetime(2026, 8, 18, 2, 3, 4),
        datetime(
            2026, 8, 18, 11, 3, 4, tzinfo=timezone(timedelta(hours=9))
        ),
        True,
    ),
)
def test_manual_requires_exact_utc_clock_before_canonicalizing_result(now: object):
    client = FakeYouTubeClient(
        video_responses=((synthetic_video_item(video_id=VIDEO_ID),),)
    )
    _assert_discovery_invalid(
        lambda: ManualUrlDiscoverer(client, clock=lambda: now).fetch(VIDEO_ID)  # type: ignore[arg-type]
    )
    assert client.video_calls == [(VIDEO_ID,)]

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.youtube.client import (
    ENDPOINT_COSTS,
    MAX_RESPONSE_BYTES,
    QUOTA_CONTRACT_VERSION,
    QUOTA_REFERENCE_URL,
    READ_UNIT_DAILY_LIMIT,
    SEARCH_CALL_DAILY_LIMIT,
    ChannelUploads,
    EndpointClass,
    SafeTransportFailure,
    UrllibYouTubeTransport,
    YouTubeClient,
    YouTubePage,
    YouTubeProviderFailure,
)
from tests.backend.youtube_fakes import (
    FIXED_NOW,
    FakeCredentialStore,
    FakeUrlResponse,
    FakeYouTubeTransport,
    RecordingReservation,
    RecordingSleeper,
    fixed_clock,
    missing_credential_error,
)


CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
OTHER_CHANNEL_ID = "UCbcdefghijklmnopqrstuvw"
UPLOADS_PLAYLIST_ID = "UUabcdefghijklmnopqrstuv"
OTHER_UPLOADS_PLAYLIST_ID = "UUbcdefghijklmnopqrstuvw"
VIDEO_ID = "video000001"
OTHER_VIDEO_ID = "video000002"
PUBLISHED_AFTER = "2023-08-17T23:59:59.000000Z"
PUBLISHED_BEFORE = "2026-08-18T00:00:00.000000Z"
SECRET = "synthetic-youtube-key-000001"
RAW_PROVIDER_SENTINEL = "raw-provider-body-private-sentinel"
FULL_URL_SENTINEL = f"https://provider.invalid/path?key={SECRET}"


def _client(
    transport: FakeYouTubeTransport,
    *,
    credential_store: FakeCredentialStore | None = None,
    reservation: RecordingReservation | None = None,
    sleeper: RecordingSleeper | None = None,
) -> tuple[YouTubeClient, RecordingReservation, RecordingSleeper]:
    actual_reservation = reservation or RecordingReservation()
    actual_sleeper = sleeper or RecordingSleeper()
    return (
        YouTubeClient(
            transport=transport,
            credential_store=credential_store or FakeCredentialStore(SECRET),
            reserve_attempt=actual_reservation,
            sleeper=actual_sleeper,
            clock=fixed_clock,
        ),
        actual_reservation,
        actual_sleeper,
    )


def _channel_item(
    channel_id: str = CHANNEL_ID,
    uploads_playlist_id: str = UPLOADS_PLAYLIST_ID,
) -> dict[str, object]:
    return {
        "id": channel_id,
        "contentDetails": {
            "relatedPlaylists": {"uploads": uploads_playlist_id},
        },
    }


def _playlist_item(
    video_id: str = VIDEO_ID,
    playlist_id: object = UPLOADS_PLAYLIST_ID,
) -> dict[str, object]:
    return {
        "contentDetails": {"videoId": video_id},
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"videoId": video_id},
        },
    }


def _search_item(video_id: str = VIDEO_ID) -> dict[str, object]:
    return {
        "id": {"kind": "youtube#video", "videoId": video_id},
    }


def _video_item(video_id: str = VIDEO_ID) -> dict[str, object]:
    return {
        "id": video_id,
        "snippet": {},
        "contentDetails": {},
        "liveStreamingDetails": {},
        "status": {},
    }


def _assert_one_reservation(
    reservation: RecordingReservation,
    endpoint_class: EndpointClass,
) -> None:
    assert reservation.calls == [(endpoint_class, 1, FIXED_NOW)]


def test_quota_contract_constants_and_costs_are_exact():
    assert QUOTA_CONTRACT_VERSION == "youtube-data-api-2026-06-01"
    assert QUOTA_REFERENCE_URL == (
        "https://developers.google.com/youtube/v3/determine_quota_cost"
    )
    assert SEARCH_CALL_DAILY_LIMIT == 100
    assert READ_UNIT_DAILY_LIMIT == 10_000
    assert ENDPOINT_COSTS == {
        EndpointClass.SEARCH_LIST: 1,
        EndpointClass.CHANNELS_LIST: 1,
        EndpointClass.PLAYLIST_ITEMS_LIST: 1,
        EndpointClass.VIDEOS_LIST: 1,
    }


def test_search_uses_one_logical_query_and_exact_provider_parameters():
    transport = FakeYouTubeTransport(page={"items": [], "nextPageToken": None})
    client, reservation, _sleeper = _client(transport)

    page = client.search_videos(
        query="千竈鉄平|千竃鉄平",
        published_after=PUBLISHED_AFTER,
        published_before=PUBLISHED_BEFORE,
        page_token=None,
    )

    assert page == YouTubePage(items=(), next_page_token=None)
    assert transport.safe_requests == [{
        "endpoint": "search",
        "part": "id",
        "type": "video",
        "order": "date",
        "maxResults": "50",
        "q": "千竈鉄平|千竃鉄平",
        "publishedAfter": PUBLISHED_AFTER,
        "publishedBefore": PUBLISHED_BEFORE,
    }]
    _assert_one_reservation(reservation, EndpointClass.SEARCH_LIST)


def test_channels_uses_exact_parameters_and_returns_uploads_identity():
    transport = FakeYouTubeTransport(page={"items": [_channel_item()]})
    client, reservation, _sleeper = _client(transport)

    result = client.channels_uploads((CHANNEL_ID,))

    assert result == (ChannelUploads(CHANNEL_ID, UPLOADS_PLAYLIST_ID),)
    assert transport.safe_requests == [{
        "endpoint": "channels",
        "part": "contentDetails",
        "id": CHANNEL_ID,
    }]
    _assert_one_reservation(reservation, EndpointClass.CHANNELS_LIST)


def test_playlist_items_uses_exact_parameters_and_page_token():
    transport = FakeYouTubeTransport(
        page={"items": [_playlist_item()], "nextPageToken": "TOKEN_2"}
    )
    client, reservation, _sleeper = _client(transport)

    page = client.playlist_items(UPLOADS_PLAYLIST_ID, "TOKEN_1")

    assert page == YouTubePage(
        items=(_playlist_item(),), next_page_token="TOKEN_2"
    )
    assert transport.safe_requests == [{
        "endpoint": "playlistItems",
        "part": "contentDetails,snippet",
        "maxResults": "50",
        "playlistId": UPLOADS_PLAYLIST_ID,
        "pageToken": "TOKEN_1",
    }]
    _assert_one_reservation(reservation, EndpointClass.PLAYLIST_ITEMS_LIST)


def test_videos_uses_exact_parameters_and_comma_joined_ids():
    transport = FakeYouTubeTransport(
        page={"items": [_video_item(VIDEO_ID), _video_item(OTHER_VIDEO_ID)]}
    )
    client, reservation, _sleeper = _client(transport)

    items = client.videos((VIDEO_ID, OTHER_VIDEO_ID))

    assert items == (_video_item(VIDEO_ID), _video_item(OTHER_VIDEO_ID))
    assert transport.safe_requests == [{
        "endpoint": "videos",
        "part": "snippet,contentDetails,liveStreamingDetails,status",
        "id": f"{VIDEO_ID},{OTHER_VIDEO_ID}",
    }]
    _assert_one_reservation(reservation, EndpointClass.VIDEOS_LIST)


@pytest.mark.parametrize("method_name", ("channels", "videos"))
def test_empty_identity_batch_returns_without_credential_reservation_or_network(method_name):
    credential = FakeCredentialStore(SECRET)
    transport = FakeYouTubeTransport()
    client, reservation, _sleeper = _client(
        transport, credential_store=credential
    )

    result = (
        client.channels_uploads(())
        if method_name == "channels"
        else client.videos(())
    )

    assert result == ()
    assert credential.read_count == 0
    assert reservation.calls == []
    assert transport.safe_requests == []


@pytest.mark.parametrize(
    ("method_name", "value"),
    (
        ("channels", [CHANNEL_ID]),
        ("channels", (True,)),
        ("channels", ("UCshort",)),
        ("channels", tuple(CHANNEL_ID for _ in range(51))),
        ("channels", (CHANNEL_ID, CHANNEL_ID)),
        ("videos", [VIDEO_ID]),
        ("videos", (True,)),
        ("videos", ("video/00001",)),
        ("videos", tuple(f"vid{i:08d}" for i in range(51))),
        ("videos", (VIDEO_ID, VIDEO_ID)),
    ),
)
def test_unsafe_or_oversized_identity_batches_fail_before_any_side_effect(
    method_name,
    value,
):
    credential = FakeCredentialStore(SECRET)
    transport = FakeYouTubeTransport()
    client, reservation, _sleeper = _client(
        transport, credential_store=credential
    )

    with pytest.raises(DomainError) as caught:
        if method_name == "channels":
            client.channels_uploads(value)
        else:
            client.videos(value)

    assert caught.value.code == "YOUTUBE_REQUEST_INVALID"
    assert credential.read_count == 0
    assert reservation.calls == []
    assert transport.safe_requests == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("query", ""),
        ("query", "query\nwith-control"),
        ("query", "x" * 501),
        ("published_after", "2023-08-17T23:59:59Z"),
        ("published_after", "2027-08-17T23:59:59.000000Z"),
        ("published_before", "not-a-date"),
        ("page_token", ""),
        ("page_token", "token with spaces"),
        ("page_token", "x" * 513),
        ("page_token", True),
    ),
)
def test_search_rejects_unsafe_query_date_and_token_before_any_side_effect(
    field,
    value,
):
    credential = FakeCredentialStore(SECRET)
    transport = FakeYouTubeTransport()
    client, reservation, _sleeper = _client(
        transport, credential_store=credential
    )
    values = {
        "query": "Synthetic analyst",
        "published_after": PUBLISHED_AFTER,
        "published_before": PUBLISHED_BEFORE,
        "page_token": None,
    }
    values[field] = value

    with pytest.raises(DomainError) as caught:
        client.search_videos(**values)

    assert caught.value.code == "YOUTUBE_REQUEST_INVALID"
    assert credential.read_count == 0
    assert reservation.calls == []
    assert transport.safe_requests == []


@pytest.mark.parametrize(
    ("playlist_id", "page_token"),
    (
        ("PLnot-uploads-playlist0000", None),
        (UPLOADS_PLAYLIST_ID, "token with spaces"),
        (True, None),
    ),
)
def test_playlist_request_validation_precedes_credential_reservation_and_network(
    playlist_id,
    page_token,
):
    credential = FakeCredentialStore(SECRET)
    transport = FakeYouTubeTransport()
    client, reservation, _sleeper = _client(
        transport, credential_store=credential
    )

    with pytest.raises(DomainError) as caught:
        client.playlist_items(playlist_id, page_token)

    assert caught.value.code == "YOUTUBE_REQUEST_INVALID"
    assert credential.read_count == 0
    assert reservation.calls == []
    assert transport.safe_requests == []


@pytest.mark.parametrize(
    "page",
    (
        [],
        {"nextPageToken": None},
        {"items": "not-a-list"},
        {"items": [True]},
        {"items": [], "nextPageToken": True},
        {"items": [], "nextPageToken": "token with spaces"},
        {"items": [], "nextPageToken": "x" * 513},
    ),
)
def test_invalid_envelope_item_list_or_next_token_fails_with_safe_permanent_error(page):
    transport = FakeYouTubeTransport(page=page)  # type: ignore[arg-type]
    client, reservation, sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.search_videos(
            query="Synthetic analyst",
            published_after=PUBLISHED_AFTER,
            published_before=PUBLISHED_BEFORE,
            page_token=None,
        )

    assert caught.value.code == "YOUTUBE_RESPONSE_INVALID"
    assert caught.value.category == "permanent"
    assert str(caught.value) == "YouTube provider request failed."
    _assert_one_reservation(reservation, EndpointClass.SEARCH_LIST)
    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("method_name", "endpoint_class"),
    (
        ("search", EndpointClass.SEARCH_LIST),
        ("playlist", EndpointClass.PLAYLIST_ITEMS_LIST),
    ),
)
@pytest.mark.parametrize(("item_count", "accepted"), ((50, True), (51, False)))
def test_page_item_count_enforces_provider_maximum_without_retry_or_private_leak(
    method_name,
    endpoint_class,
    item_count,
    accepted,
):
    video_ids = tuple(f"video{index:06d}" for index in range(item_count))
    items = [
        _search_item(video_id)
        if method_name == "search"
        else _playlist_item(video_id)
        for video_id in video_ids
    ]
    if items:
        items[-1]["private"] = RAW_PROVIDER_SENTINEL
    transport = FakeYouTubeTransport(page={"items": items})
    client, reservation, sleeper = _client(transport)

    if accepted:
        page = (
            client.search_videos(
                "Synthetic analyst",
                PUBLISHED_AFTER,
                PUBLISHED_BEFORE,
                None,
            )
            if method_name == "search"
            else client.playlist_items(UPLOADS_PLAYLIST_ID, None)
        )
        assert len(page.items) == 50
    else:
        with pytest.raises(YouTubeProviderFailure) as caught:
            if method_name == "search":
                client.search_videos(
                    "Synthetic analyst",
                    PUBLISHED_AFTER,
                    PUBLISHED_BEFORE,
                    None,
                )
            else:
                client.playlist_items(UPLOADS_PLAYLIST_ID, None)

        assert caught.value.code == "YOUTUBE_RESPONSE_INVALID"
        assert caught.value.category == "permanent"
        assert str(caught.value) == "YouTube provider request failed."
        assert RAW_PROVIDER_SENTINEL not in repr(caught.value)

    _assert_one_reservation(reservation, endpoint_class)
    assert len(transport.safe_requests) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    "snippet",
    (
        {
            "playlistId": OTHER_UPLOADS_PLAYLIST_ID,
            "resourceId": {"videoId": VIDEO_ID},
            "private": RAW_PROVIDER_SENTINEL,
        },
        {
            "resourceId": {"videoId": VIDEO_ID},
            "private": RAW_PROVIDER_SENTINEL,
        },
        {
            "playlistId": True,
            "resourceId": {"videoId": VIDEO_ID},
            "private": RAW_PROVIDER_SENTINEL,
        },
    ),
)
def test_playlist_item_requires_exact_requested_playlist_identity(snippet):
    transport = FakeYouTubeTransport(page={
        "items": [{
            "contentDetails": {"videoId": VIDEO_ID},
            "snippet": snippet,
        }]
    })
    client, reservation, sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.playlist_items(UPLOADS_PLAYLIST_ID, None)

    assert caught.value.code == "YOUTUBE_RESPONSE_INVALID"
    assert caught.value.category == "permanent"
    assert str(caught.value) == "YouTube provider request failed."
    assert RAW_PROVIDER_SENTINEL not in repr(caught.value)
    _assert_one_reservation(reservation, EndpointClass.PLAYLIST_ITEMS_LIST)
    assert len(transport.safe_requests) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("method_name", "item"),
    (
        ("channels", {"id": CHANNEL_ID}),
        ("channels", _channel_item(OTHER_CHANNEL_ID)),
        ("channels", _channel_item(CHANNEL_ID, "PLabcdefghijklmnopqrstuv")),
        ("playlist", {"contentDetails": {"videoId": VIDEO_ID}}),
        (
            "playlist",
            {
                "contentDetails": {"videoId": VIDEO_ID},
                "snippet": {"resourceId": {"videoId": OTHER_VIDEO_ID}},
            },
        ),
        ("search", {"id": {"kind": "youtube#channel", "videoId": VIDEO_ID}}),
        ("search", {"id": {"kind": "youtube#video", "videoId": "bad"}}),
        ("videos", {"id": VIDEO_ID, "snippet": {}, "contentDetails": {}}),
        ("videos", _video_item(OTHER_VIDEO_ID)),
    ),
)
def test_endpoint_specific_item_shape_and_identity_mismatch_fail_closed(
    method_name,
    item,
):
    transport = FakeYouTubeTransport(page={"items": [item]})
    client, reservation, _sleeper = _client(transport)
    expected_endpoint = {
        "channels": EndpointClass.CHANNELS_LIST,
        "playlist": EndpointClass.PLAYLIST_ITEMS_LIST,
        "search": EndpointClass.SEARCH_LIST,
        "videos": EndpointClass.VIDEOS_LIST,
    }[method_name]

    with pytest.raises(YouTubeProviderFailure) as caught:
        if method_name == "channels":
            client.channels_uploads((CHANNEL_ID,))
        elif method_name == "playlist":
            client.playlist_items(UPLOADS_PLAYLIST_ID, None)
        elif method_name == "search":
            client.search_videos(
                "Synthetic analyst",
                PUBLISHED_AFTER,
                PUBLISHED_BEFORE,
                None,
            )
        else:
            client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_RESPONSE_INVALID"
    assert caught.value.category == "permanent"
    _assert_one_reservation(reservation, expected_endpoint)


def test_duplicate_provider_items_fail_closed_instead_of_double_counting():
    transport = FakeYouTubeTransport(
        page={"items": [_video_item(), _video_item()]}
    )
    client, _reservation, _sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("failure", "expected_sleeps"),
    (
        (SafeTransportFailure(kind="network"), (1, 4, 16)),
        (SafeTransportFailure(kind="http", status_code=429), (1, 4, 16)),
        (SafeTransportFailure(kind="http", status_code=500), (1, 4, 16)),
        (SafeTransportFailure(kind="http", status_code=503), (1, 4, 16)),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_seconds=0
            ),
            (1, 4, 16),
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_seconds=10
            ),
            (10, 10, 16),
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_seconds=60
            ),
            (60, 60, 60),
        ),
    ),
)
def test_transient_failures_retry_at_most_four_attempts_with_exact_waits(
    failure,
    expected_sleeps,
):
    transport = FakeYouTubeTransport(responses=(failure, failure, failure, failure))
    client, reservation, sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_PROVIDER_TRANSIENT"
    assert caught.value.category == "transient"
    assert [call[1] for call in reservation.calls] == [1, 2, 3, 4]
    assert all(call[0] is EndpointClass.VIDEOS_LIST for call in reservation.calls)
    assert sleeper.delays == list(expected_sleeps)
    assert len(transport.safe_requests) == 4


def test_retry_stops_after_success_and_credential_is_read_only_once():
    failure = SafeTransportFailure(kind="network")
    transport = FakeYouTubeTransport(
        responses=(failure, failure, {"items": [_video_item()]})
    )
    credential = FakeCredentialStore(SECRET)
    client, reservation, sleeper = _client(
        transport, credential_store=credential
    )

    result = client.videos((VIDEO_ID,))

    assert result == (_video_item(),)
    assert credential.read_count == 1
    assert [call[1] for call in reservation.calls] == [1, 2, 3]
    assert sleeper.delays == [1, 4]


@pytest.mark.parametrize(
    ("failure", "code", "category", "retry_after"),
    (
        (
            SafeTransportFailure(
                kind="http", status_code=429, provider_signal="quota"
            ),
            "YOUTUBE_QUOTA_EXHAUSTED",
            "quota",
            None,
        ),
        (
            SafeTransportFailure(
                kind="http",
                status_code=429,
                retry_after_seconds=10,
                provider_signal="quota",
            ),
            "YOUTUBE_QUOTA_EXHAUSTED",
            "quota",
            None,
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=400, provider_signal="invalid_page_token"
            ),
            "YOUTUBE_INVALID_PAGE_TOKEN",
            "invalid_page_token",
            None,
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_seconds=61
            ),
            "YOUTUBE_PROVIDER_DEFERRED",
            "defer",
            61,
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_seconds=86_400
            ),
            "YOUTUBE_PROVIDER_DEFERRED",
            "defer",
            86_400,
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=503, retry_after_invalid=True
            ),
            "YOUTUBE_PROVIDER_DEFERRED",
            "defer",
            86_400,
        ),
        (
            SafeTransportFailure(kind="http", status_code=404),
            "YOUTUBE_PROVIDER_REQUEST_FAILED",
            "permanent",
            None,
        ),
        (
            SafeTransportFailure(kind="response"),
            "YOUTUBE_RESPONSE_INVALID",
            "permanent",
            None,
        ),
    ),
)
def test_nonretry_categories_stop_after_one_reservation_without_sleep(
    failure,
    code,
    category,
    retry_after,
):
    transport = FakeYouTubeTransport(responses=(failure,))
    client, reservation, sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == code
    assert caught.value.category == category
    assert caught.value.retry_after_seconds == retry_after
    _assert_one_reservation(reservation, EndpointClass.VIDEOS_LIST)
    assert sleeper.delays == []


def test_missing_credential_stops_before_reservation_or_network():
    credential = FakeCredentialStore(read_error=missing_credential_error())
    transport = FakeYouTubeTransport()
    client, reservation, sleeper = _client(
        transport, credential_store=credential
    )

    with pytest.raises(DomainError) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_CREDENTIAL_NOT_CONFIGURED"
    assert reservation.calls == []
    assert sleeper.delays == []
    assert transport.safe_requests == []


@pytest.mark.parametrize(
    "read_result",
    ("", "not ascii 日本語 token", "synthetic key token 000001", True),
)
def test_invalid_credential_store_result_is_reduced_to_a_safe_code(read_result):
    credential = FakeCredentialStore(read_result)  # type: ignore[arg-type]
    transport = FakeYouTubeTransport()
    client, reservation, _sleeper = _client(
        transport, credential_store=credential
    )

    with pytest.raises(DomainError) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_CREDENTIAL_INVALID"
    assert str(caught.value) == "YouTube credential is invalid"
    assert reservation.calls == []
    assert transport.safe_requests == []


def test_unexpected_credential_exception_does_not_leak_native_text():
    credential = FakeCredentialStore(
        read_error=RuntimeError(f"native {SECRET} {RAW_PROVIDER_SENTINEL}")
    )
    transport = FakeYouTubeTransport()
    client, _reservation, _sleeper = _client(
        transport, credential_store=credential
    )

    with pytest.raises(DomainError) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert SECRET not in str(caught.value)
    assert RAW_PROVIDER_SENTINEL not in str(caught.value)


def test_reservation_happens_before_each_transport_call():
    events: list[str] = []
    failure = SafeTransportFailure(kind="network")
    transport = FakeYouTubeTransport(
        responses=(failure, {"items": [_video_item()]}), events=events
    )
    reservation = RecordingReservation(events)
    client, _reservation, _sleeper = _client(
        transport, reservation=reservation
    )

    client.videos((VIDEO_ID,))

    assert events == ["reserve:1", "transport", "reserve:2", "transport"]


def test_reservation_failure_prevents_transport_and_is_not_reclassified():
    class FailingReservation:
        def __call__(self, *_args):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "quota reservation is invalid",
            )

    transport = FakeYouTubeTransport(page={"items": [_video_item()]})
    client = YouTubeClient(
        transport=transport,
        credential_store=FakeCredentialStore(SECRET),
        reserve_attempt=FailingReservation(),
        sleeper=RecordingSleeper(),
        clock=fixed_clock,
    )

    with pytest.raises(DomainError) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert transport.safe_requests == []


def test_unexpected_transport_exception_is_safe_and_not_retried(capsys):
    transport = FakeYouTubeTransport(
        responses=(
            RuntimeError(
                f"{RAW_PROVIDER_SENTINEL} {FULL_URL_SENTINEL} {SECRET}"
            ),
        )
    )
    client, reservation, sleeper = _client(transport)

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.videos((VIDEO_ID,))

    rendered = str(caught.value)
    assert caught.value.code == "YOUTUBE_PROVIDER_REQUEST_FAILED"
    assert caught.value.category == "permanent"
    assert rendered == "YouTube provider request failed."
    for sentinel in (RAW_PROVIDER_SENTINEL, FULL_URL_SENTINEL, SECRET):
        assert sentinel not in rendered
        assert sentinel not in repr(caught.value)
        assert sentinel not in capsys.readouterr().out
    _assert_one_reservation(reservation, EndpointClass.VIDEOS_LIST)
    assert sleeper.delays == []


def test_urllib_transport_uses_fixed_https_get_timeout_and_does_not_retain_secret():
    captured: dict[str, object] = {}
    response = FakeUrlResponse(b'{"items":[]}')

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    transport = UrllibYouTubeTransport(opener=opener)

    result = transport.get_json("search", {"part": "id"}, SECRET)

    request = captured["request"]
    assert result == {"items": []}
    assert request.method == "GET"
    assert request.get_header("Accept") == "application/json"
    assert captured["timeout"] == 30
    split = urlsplit(request.full_url)
    assert split.scheme == "https"
    assert split.netloc == "www.googleapis.com"
    assert split.path == "/youtube/v3/search"
    assert parse_qs(split.query) == {"part": ["id"], "key": [SECRET]}
    assert response.closed is True
    assert SECRET not in repr(transport)
    assert SECRET not in repr(vars(transport))


@pytest.mark.parametrize(
    "body",
    (
        b"\xff",
        b"not-json",
        b"[]",
        b"null",
    ),
)
def test_urllib_transport_rejects_invalid_utf8_json_and_nonobject_envelopes(body):
    transport = UrllibYouTubeTransport(opener=lambda *_args, **_kwargs: FakeUrlResponse(body))

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json("videos", {"part": "snippet"}, SECRET)

    assert caught.value.kind == "response"
    assert str(caught.value) == "YouTube transport failed safely."
    decoded = body.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in str(caught.value)


def test_urllib_transport_rejects_response_larger_than_fixed_ceiling():
    body = b"{" + (b" " * MAX_RESPONSE_BYTES) + b"}"
    transport = UrllibYouTubeTransport(opener=lambda *_args, **_kwargs: FakeUrlResponse(body))

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json("videos", {"part": "snippet"}, SECRET)

    assert caught.value.kind == "response"


def test_urllib_transport_safely_extracts_known_provider_signal_and_retry_after():
    provider_body = json.dumps({
        "error": {
            "code": 429,
            "message": RAW_PROVIDER_SENTINEL,
            "errors": [{"reason": "quotaExceeded", "message": SECRET}],
        }
    }).encode("utf-8")
    headers = Message()
    headers["Retry-After"] = "61"

    def opener(_request, timeout):
        assert timeout == 30
        raise HTTPError(
            FULL_URL_SENTINEL,
            429,
            RAW_PROVIDER_SENTINEL,
            headers,
            io.BytesIO(provider_body),
        )

    transport = UrllibYouTubeTransport(opener=opener)

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json("search", {"part": "id"}, SECRET)

    assert caught.value.kind == "http"
    assert caught.value.status_code == 429
    assert caught.value.provider_signal == "quota"
    assert caught.value.retry_after_seconds == 61
    assert caught.value.retry_after_invalid is False
    for sentinel in (RAW_PROVIDER_SENTINEL, FULL_URL_SENTINEL, SECRET):
        assert sentinel not in str(caught.value)
        assert sentinel not in repr(caught.value)


@pytest.mark.parametrize(
    ("header", "seconds", "invalid"),
    (
        ("0", 0, False),
        ("60", 60, False),
        ("86400", 86_400, False),
        ("86401", None, True),
        ("061", None, True),
        ("-1", None, True),
        ("tomorrow", None, True),
    ),
)
def test_urllib_transport_parses_retry_after_only_at_canonical_boundaries(
    header,
    seconds,
    invalid,
):
    headers = Message()
    headers["Retry-After"] = header

    def opener(_request, timeout):
        assert timeout == 30
        raise HTTPError(
            FULL_URL_SENTINEL,
            503,
            RAW_PROVIDER_SENTINEL,
            headers,
            io.BytesIO(b"{}"),
        )

    transport = UrllibYouTubeTransport(opener=opener)

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json("videos", {"part": "snippet"}, SECRET)

    assert caught.value.retry_after_seconds == seconds
    assert caught.value.retry_after_invalid is invalid


def test_urllib_transport_converts_native_network_error_without_leaking_it():
    def opener(_request, timeout):
        assert timeout == 30
        raise URLError(f"native {RAW_PROVIDER_SENTINEL} {SECRET}")

    transport = UrllibYouTubeTransport(opener=opener)

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json("videos", {"part": "snippet"}, SECRET)

    assert caught.value.kind == "network"
    assert RAW_PROVIDER_SENTINEL not in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("endpoint", "params", "api_key"),
    (
        ("https://attacker.invalid", {"part": "id"}, SECRET),
        ("search?leak=1", {"part": "id"}, SECRET),
        ("search", {"part": True}, SECRET),
        ("search", {"part": "id"}, ""),
    ),
)
def test_urllib_transport_rejects_unsafe_direct_inputs_without_opening(
    endpoint,
    params,
    api_key,
):
    opened = False

    def opener(_request, _timeout):
        nonlocal opened
        opened = True
        raise AssertionError("unsafe transport input reached opener")

    transport = UrllibYouTubeTransport(opener=opener)

    with pytest.raises(SafeTransportFailure) as caught:
        transport.get_json(endpoint, params, api_key)

    assert caught.value.kind == "response"
    assert opened is False


def test_clock_must_produce_exact_utc_before_reservation_or_transport():
    transport = FakeYouTubeTransport(page={"items": [_video_item()]})
    reservation = RecordingReservation()
    client = YouTubeClient(
        transport=transport,
        credential_store=FakeCredentialStore(SECRET),
        reserve_attempt=reservation,
        sleeper=RecordingSleeper(),
        clock=lambda: datetime(2026, 8, 18, 2, 3, 4),
    )

    with pytest.raises(DomainError) as caught:
        client.videos((VIDEO_ID,))

    assert caught.value.code == "YOUTUBE_ATTEMPT_TIME_INVALID"
    assert reservation.calls == []
    assert transport.safe_requests == []

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.youtube.client import (
    ChannelUploads,
    YouTubePage,
)


FIXED_NOW = datetime(2026, 8, 18, 2, 3, 4, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED_NOW


class FakeCredentialStore:
    def __init__(
        self,
        secret: str = "synthetic-youtube-key-000001",
        *,
        read_error: BaseException | None = None,
    ) -> None:
        self._secret = secret
        self._read_error = read_error
        self.read_count = 0

    def set_api_key(self, secret: str) -> None:
        self._secret = secret

    def has_api_key(self) -> bool:
        return self._read_error is None

    def read_api_key(self) -> str:
        self.read_count += 1
        if self._read_error is not None:
            raise self._read_error
        return self._secret

    def delete_api_key(self) -> bool:
        return True


class FakeYouTubeTransport:
    def __init__(
        self,
        *,
        page: Mapping[str, object] | None = None,
        responses: tuple[Mapping[str, object] | BaseException, ...] | None = None,
        events: list[str] | None = None,
    ) -> None:
        default_page: Mapping[str, object] = {"items": [], "nextPageToken": None}
        self._responses = list(
            responses if responses is not None else (
                page if page is not None else default_page,
            )
        )
        self.safe_requests: list[dict[str, str]] = []
        self.api_key_was_supplied: list[bool] = []
        self.events = events

    def get_json(
        self,
        endpoint: str,
        params: Mapping[str, str],
        api_key: str,
    ) -> Mapping[str, object]:
        request = {"endpoint": endpoint, **dict(params)}
        self.safe_requests.append(request)
        self.api_key_was_supplied.append(bool(api_key))
        if self.events is not None:
            self.events.append("transport")
        if not self._responses:
            raise AssertionError("fake transport received an unexpected request")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeYouTubeClient:
    def __init__(
        self,
        *,
        channel_responses: tuple[object, ...] = (),
        playlist_responses: tuple[object, ...] = (),
        search_responses: tuple[object, ...] = (),
        video_responses: tuple[object, ...] = (),
    ) -> None:
        self._channel_responses = list(channel_responses)
        self._playlist_responses = list(playlist_responses)
        self._search_responses = list(search_responses)
        self._video_responses = list(video_responses)
        self.channel_calls: list[tuple[str, ...]] = []
        self.playlist_calls: list[tuple[str, str | None]] = []
        self.search_calls: list[tuple[str, str, str, str | None]] = []
        self.video_calls: list[tuple[str, ...]] = []

    def channels_uploads(
        self, channel_ids: tuple[str, ...]
    ) -> tuple[ChannelUploads, ...]:
        self.channel_calls.append(channel_ids)
        return self._next(self._channel_responses, "channels_uploads")  # type: ignore[return-value]

    def playlist_items(
        self, playlist_id: str, page_token: str | None
    ) -> YouTubePage:
        self.playlist_calls.append((playlist_id, page_token))
        return self._next(self._playlist_responses, "playlist_items")  # type: ignore[return-value]

    def search_videos(
        self,
        query: str,
        published_after: str,
        published_before: str,
        page_token: str | None,
    ) -> YouTubePage:
        self.search_calls.append(
            (query, published_after, published_before, page_token)
        )
        return self._next(self._search_responses, "search_videos")  # type: ignore[return-value]

    def videos(
        self, video_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, object], ...]:
        self.video_calls.append(video_ids)
        return self._next(self._video_responses, "videos")  # type: ignore[return-value]

    @staticmethod
    def _next(responses: list[object], method: str) -> object:
        if not responses:
            raise AssertionError(f"fake client received unexpected {method} call")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def synthetic_video_item(
    *,
    video_id: str = "video000001",
    channel_id: str = "UCabcdefghijklmnopqrstuv",
    channel_title: str = "Synthetic Channel",
    title: str = "Synthetic market discussion",
    description: str = "Synthetic description.\nSecond line.",
    snippet_published_at: str = "2026-08-10T01:00:00Z",
    actual_start_time: str | None = None,
    duration: str = "PT10M",
    live_broadcast_content: str = "none",
    privacy_status: str = "public",
    upload_status: str = "processed",
) -> dict[str, object]:
    live_details: dict[str, object] = {
        "scheduledStartTime": "2026-08-10T02:00:00Z",
    }
    if actual_start_time is not None:
        live_details["actualStartTime"] = actual_start_time
    return {
        "etag": "provider-only-etag",
        "id": video_id,
        "kind": "youtube#video",
        "snippet": {
            "publishedAt": snippet_published_at,
            "channelId": channel_id,
            "channelTitle": channel_title,
            "title": title,
            "description": description,
            "liveBroadcastContent": live_broadcast_content,
            "tags": ["synthetic", "market"],
            "thumbnails": {"default": {"url": "https://provider.invalid/image"}},
        },
        "contentDetails": {
            "duration": duration,
            "definition": "hd",
            "licensedContent": True,
        },
        "liveStreamingDetails": live_details,
        "status": {
            "privacyStatus": privacy_status,
            "uploadStatus": upload_status,
            "embeddable": True,
            "publicStatsViewable": True,
        },
        "statistics": {"viewCount": "123"},
        "topicDetails": {"topicCategories": ["synthetic"]},
    }


def synthetic_playlist_item(
    video_id: str,
    playlist_id: str,
) -> dict[str, object]:
    return {
        "kind": "youtube#playlistItem",
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        },
        "contentDetails": {"videoId": video_id},
    }


class RecordingReservation:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[object, int, datetime]] = []
        self.events = events

    def __call__(
        self,
        endpoint_class: object,
        attempt_no: int,
        attempted_at: datetime,
    ) -> None:
        self.calls.append((endpoint_class, attempt_no, attempted_at))
        if self.events is not None:
            self.events.append(f"reserve:{attempt_no}")


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeUrlResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.closed = False

    def __enter__(self) -> "FakeUrlResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            return self._body
        return self._body[:limit]


def missing_credential_error() -> DomainError:
    return DomainError(
        "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        "YouTube credential is not configured",
    )

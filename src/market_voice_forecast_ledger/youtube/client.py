from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol

from market_voice_forecast_ledger.credentials import CredentialStore
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError


QUOTA_CONTRACT_VERSION = "youtube-data-api-2026-06-01"
QUOTA_REFERENCE_URL = "https://developers.google.com/youtube/v3/determine_quota_cost"
SEARCH_CALL_DAILY_LIMIT = 100
READ_UNIT_DAILY_LIMIT = 10_000
MAX_RESPONSE_BYTES = 1_048_576
_TIMEOUT_SECONDS = 30
_MAX_BATCH_SIZE = 50
_MAX_PAGE_TOKEN_LENGTH = 512
_MAX_QUERY_LENGTH = 500
_SAFE_PROVIDER_MESSAGE = "YouTube provider request failed."
_SAFE_TRANSPORT_MESSAGE = "YouTube transport failed safely."
_DEFER_FALLBACK_SECONDS = 86_400
_RETRY_WAITS = (1, 4, 16)

_YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_UPLOADS_PLAYLIST_ID = re.compile(r"^UU[A-Za-z0-9_-]{22}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SUPPORTED_ENDPOINTS = frozenset({"search", "channels", "playlistItems", "videos"})
_QUOTA_PROVIDER_REASONS = frozenset({
    "quotaExceeded",
    "dailyLimitExceeded",
    "dailyLimitExceededUnreg",
    "rateLimitExceeded",
    "userRateLimitExceeded",
})
_INVALID_PAGE_TOKEN_REASONS = frozenset({"invalidPageToken"})
_CREDENTIAL_ERROR_MESSAGES = {
    "YOUTUBE_CREDENTIAL_NOT_CONFIGURED": "YouTube credential is not configured",
    "YOUTUBE_CREDENTIAL_INVALID": "YouTube credential is invalid",
    "YOUTUBE_CREDENTIAL_STORAGE_FAILED": "YouTube credential storage failed",
}


class EndpointClass(StrEnum):
    SEARCH_LIST = "search_list"
    CHANNELS_LIST = "channels_list"
    PLAYLIST_ITEMS_LIST = "playlist_items_list"
    VIDEOS_LIST = "videos_list"


ENDPOINT_COSTS = MappingProxyType({endpoint: 1 for endpoint in EndpointClass})


class YouTubeTransport(Protocol):
    def get_json(
        self,
        endpoint: str,
        params: Mapping[str, str],
        api_key: str,
    ) -> Mapping[str, object]: ...


class AttemptReservation(Protocol):
    def __call__(
        self,
        endpoint_class: EndpointClass,
        attempt_no: int,
        attempted_at: datetime,
    ) -> None: ...


FailureCategory = Literal[
    "quota", "transient", "defer", "invalid_page_token", "permanent"
]
TransportKind = Literal["network", "http", "response"]
ProviderSignal = Literal["quota", "invalid_page_token"]


class SafeTransportFailure(Exception):
    __slots__ = (
        "kind",
        "status_code",
        "retry_after_seconds",
        "retry_after_invalid",
        "provider_signal",
    )

    def __init__(
        self,
        *,
        kind: TransportKind,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        retry_after_invalid: bool = False,
        provider_signal: ProviderSignal | None = None,
    ) -> None:
        super().__init__(_SAFE_TRANSPORT_MESSAGE)
        self.kind = kind
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_invalid = retry_after_invalid
        self.provider_signal = provider_signal

    def __repr__(self) -> str:
        return "SafeTransportFailure('YouTube transport failed safely.')"


class YouTubeProviderFailure(Exception):
    __slots__ = ("code", "category", "retry_after_seconds")

    def __init__(
        self,
        code: str,
        category: FailureCategory,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(_SAFE_PROVIDER_MESSAGE)
        self.code = code
        self.category = category
        self.retry_after_seconds = retry_after_seconds

    def __repr__(self) -> str:
        return "YouTubeProviderFailure('YouTube provider request failed.')"


@dataclass(frozen=True, slots=True)
class YouTubePage:
    items: tuple[Mapping[str, object], ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class ChannelUploads:
    channel_id: str
    uploads_playlist_id: str


class UrllibYouTubeTransport:
    BASE_URL = "https://www.googleapis.com/youtube/v3/"

    def __init__(
        self,
        *,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen

    def get_json(
        self,
        endpoint: str,
        params: Mapping[str, str],
        api_key: str,
    ) -> Mapping[str, object]:
        if not _safe_transport_arguments(endpoint, params, api_key):
            raise SafeTransportFailure(kind="response")
        query = urllib.parse.urlencode({**dict(params), "key": api_key})
        request = urllib.request.Request(
            f"{self.BASE_URL}{endpoint}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as cause:
            raise _safe_http_failure(cause) from None
        except SafeTransportFailure:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SafeTransportFailure(kind="network") from None
        except Exception:
            raise SafeTransportFailure(kind="network") from None
        return _decode_json_object(body)


class YouTubeClient:
    def __init__(
        self,
        *,
        transport: YouTubeTransport,
        credential_store: CredentialStore,
        reserve_attempt: AttemptReservation,
        sleeper: Callable[[float], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._transport = transport
        self._credential_store = credential_store
        self._reserve_attempt = reserve_attempt
        self._sleeper = sleeper
        self._clock = clock

    def channels_uploads(
        self, channel_ids: tuple[str, ...]
    ) -> tuple[ChannelUploads, ...]:
        _validate_identity_batch(channel_ids, _YOUTUBE_CHANNEL_ID)
        if not channel_ids:
            return ()
        response = self._request(
            EndpointClass.CHANNELS_LIST,
            "channels",
            {"part": "contentDetails", "id": ",".join(channel_ids)},
        )
        page = _validated_page(response)
        requested = frozenset(channel_ids)
        result: list[ChannelUploads] = []
        seen: set[str] = set()
        try:
            for item in page.items:
                channel_id = item["id"]
                content_details = item["contentDetails"]
                related = content_details["relatedPlaylists"]
                uploads = related["uploads"]
                if (
                    type(channel_id) is not str
                    or channel_id not in requested
                    or channel_id in seen
                    or type(content_details) is not dict
                    or type(related) is not dict
                    or type(uploads) is not str
                    or _YOUTUBE_UPLOADS_PLAYLIST_ID.fullmatch(uploads) is None
                ):
                    raise _response_invalid()
                seen.add(channel_id)
                result.append(ChannelUploads(channel_id, uploads))
        except YouTubeProviderFailure:
            raise
        except Exception:
            raise _response_invalid() from None
        return tuple(result)

    def playlist_items(
        self, playlist_id: str, page_token: str | None
    ) -> YouTubePage:
        if (
            type(playlist_id) is not str
            or _YOUTUBE_UPLOADS_PLAYLIST_ID.fullmatch(playlist_id) is None
        ):
            _raise_request_invalid()
        _validate_page_token(page_token)
        params = {
            "part": "contentDetails,snippet",
            "maxResults": "50",
            "playlistId": playlist_id,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        page = _validated_page(
            self._request(
                EndpointClass.PLAYLIST_ITEMS_LIST,
                "playlistItems",
                params,
            )
        )
        seen: set[str] = set()
        try:
            for item in page.items:
                content_details = item["contentDetails"]
                snippet = item["snippet"]
                resource_id = snippet["resourceId"]
                content_video_id = content_details["videoId"]
                snippet_playlist_id = snippet["playlistId"]
                snippet_video_id = resource_id["videoId"]
                if (
                    type(content_details) is not dict
                    or type(snippet) is not dict
                    or type(resource_id) is not dict
                    or type(content_video_id) is not str
                    or _YOUTUBE_VIDEO_ID.fullmatch(content_video_id) is None
                    or type(snippet_playlist_id) is not str
                    or snippet_playlist_id != playlist_id
                    or snippet_video_id != content_video_id
                    or content_video_id in seen
                ):
                    raise _response_invalid()
                seen.add(content_video_id)
        except YouTubeProviderFailure:
            raise
        except Exception:
            raise _response_invalid() from None
        return page

    def search_videos(
        self,
        query: str,
        published_after: str,
        published_before: str,
        page_token: str | None,
    ) -> YouTubePage:
        if (
            type(query) is not str
            or not query.strip()
            or len(query) > _MAX_QUERY_LENGTH
            or _contains_control(query)
        ):
            _raise_request_invalid()
        after = _parse_request_utc(published_after)
        before = _parse_request_utc(published_before)
        if after >= before:
            _raise_request_invalid()
        _validate_page_token(page_token)
        params = {
            "part": "id",
            "type": "video",
            "order": "date",
            "maxResults": "50",
            "q": query,
            "publishedAfter": published_after,
            "publishedBefore": published_before,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        page = _validated_page(
            self._request(EndpointClass.SEARCH_LIST, "search", params)
        )
        seen: set[str] = set()
        try:
            for item in page.items:
                identity = item["id"]
                video_id = identity["videoId"]
                if (
                    type(identity) is not dict
                    or identity.get("kind") != "youtube#video"
                    or type(video_id) is not str
                    or _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None
                    or video_id in seen
                ):
                    raise _response_invalid()
                seen.add(video_id)
        except YouTubeProviderFailure:
            raise
        except Exception:
            raise _response_invalid() from None
        return page

    def videos(
        self, video_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, object], ...]:
        _validate_identity_batch(video_ids, _YOUTUBE_VIDEO_ID)
        if not video_ids:
            return ()
        response = self._request(
            EndpointClass.VIDEOS_LIST,
            "videos",
            {
                "part": "snippet,contentDetails,liveStreamingDetails,status",
                "id": ",".join(video_ids),
            },
        )
        page = _validated_page(response)
        requested = frozenset(video_ids)
        seen: set[str] = set()
        try:
            for item in page.items:
                video_id = item["id"]
                live = item.get("liveStreamingDetails")
                if (
                    type(video_id) is not str
                    or video_id not in requested
                    or video_id in seen
                    or type(item["snippet"]) is not dict
                    or type(item["contentDetails"]) is not dict
                    or type(item["status"]) is not dict
                    or (live is not None and type(live) is not dict)
                ):
                    raise _response_invalid()
                seen.add(video_id)
        except YouTubeProviderFailure:
            raise
        except Exception:
            raise _response_invalid() from None
        return page.items

    def _request(
        self,
        endpoint_class: EndpointClass,
        endpoint: str,
        params: Mapping[str, str],
    ) -> Mapping[str, object]:
        credential = self._read_credential()
        for attempt_no in range(1, 5):
            attempted_at = self._clock()
            if type(attempted_at) is not datetime or attempted_at.tzinfo is not timezone.utc:
                raise DomainError(
                    "YOUTUBE_ATTEMPT_TIME_INVALID",
                    "YouTube attempt time is invalid",
                )
            self._reserve_attempt(endpoint_class, attempt_no, attempted_at)
            try:
                return self._transport.get_json(endpoint, params, credential)
            except SafeTransportFailure as cause:
                failure = _classify_provider_failure(cause)
            except Exception:
                raise YouTubeProviderFailure(
                    "YOUTUBE_PROVIDER_REQUEST_FAILED", "permanent"
                ) from None
            if failure.category != "transient" or attempt_no == 4:
                raise failure
            retry_after = failure.retry_after_seconds or 0
            self._sleeper(max(_RETRY_WAITS[attempt_no - 1], retry_after))
        raise AssertionError("unreachable YouTube retry state")

    def _read_credential(self) -> str:
        try:
            value = self._credential_store.read_api_key()
        except DomainError as cause:
            code = (
                cause.code
                if cause.code in _CREDENTIAL_ERROR_MESSAGES
                else "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
            )
            raise DomainError(code, _CREDENTIAL_ERROR_MESSAGES[code]) from None
        except Exception:
            raise DomainError(
                "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
                _CREDENTIAL_ERROR_MESSAGES["YOUTUBE_CREDENTIAL_STORAGE_FAILED"],
            ) from None
        if (
            type(value) is not str
            or not 20 <= len(value) <= 200
            or not value.isascii()
            or any(not 0x21 <= ord(character) <= 0x7E for character in value)
        ):
            raise DomainError(
                "YOUTUBE_CREDENTIAL_INVALID",
                _CREDENTIAL_ERROR_MESSAGES["YOUTUBE_CREDENTIAL_INVALID"],
            )
        return value


def _validated_page(response: object) -> YouTubePage:
    try:
        if not isinstance(response, Mapping) or "items" not in response:
            raise _response_invalid()
        items = response["items"]
        token = response.get("nextPageToken")
        if (
            type(items) is not list
            or len(items) > 50
            or any(type(item) is not dict for item in items)
        ):
            raise _response_invalid()
        if token is not None and (
            type(token) is not str or _PAGE_TOKEN.fullmatch(token) is None
        ):
            raise _response_invalid()
        return YouTubePage(items=tuple(items), next_page_token=token)
    except YouTubeProviderFailure:
        raise
    except Exception:
        raise _response_invalid() from None


def _classify_provider_failure(
    failure: SafeTransportFailure,
) -> YouTubeProviderFailure:
    if failure.provider_signal == "quota":
        return YouTubeProviderFailure("YOUTUBE_QUOTA_EXHAUSTED", "quota")
    if failure.provider_signal == "invalid_page_token":
        return YouTubeProviderFailure(
            "YOUTUBE_INVALID_PAGE_TOKEN", "invalid_page_token"
        )
    if failure.kind == "response":
        return YouTubeProviderFailure("YOUTUBE_RESPONSE_INVALID", "permanent")
    if failure.kind == "network":
        return YouTubeProviderFailure("YOUTUBE_PROVIDER_TRANSIENT", "transient")
    status = failure.status_code
    if status != 429 and not (type(status) is int and 500 <= status <= 599):
        return YouTubeProviderFailure(
            "YOUTUBE_PROVIDER_REQUEST_FAILED", "permanent"
        )
    if failure.retry_after_invalid:
        return YouTubeProviderFailure(
            "YOUTUBE_PROVIDER_DEFERRED", "defer", _DEFER_FALLBACK_SECONDS
        )
    if failure.retry_after_seconds is not None and failure.retry_after_seconds > 60:
        return YouTubeProviderFailure(
            "YOUTUBE_PROVIDER_DEFERRED",
            "defer",
            failure.retry_after_seconds,
        )
    return YouTubeProviderFailure(
        "YOUTUBE_PROVIDER_TRANSIENT",
        "transient",
        failure.retry_after_seconds,
    )


def _safe_transport_arguments(
    endpoint: object,
    params: object,
    api_key: object,
) -> bool:
    return (
        type(endpoint) is str
        and endpoint in _SUPPORTED_ENDPOINTS
        and isinstance(params, Mapping)
        and "key" not in params
        and all(
            type(key) is str
            and type(value) is str
            and key
            and len(key) <= 100
            and len(value) <= 2_048
            and not _contains_control(key)
            and not _contains_control(value)
            for key, value in params.items()
        )
        and type(api_key) is str
        and bool(api_key)
        and api_key.isascii()
        and not _contains_control(api_key)
    )


def _decode_json_object(body: object) -> Mapping[str, object]:
    try:
        if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("invalid response bytes")
        text = body.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_unique_json_object)
        if type(value) is not dict:
            raise ValueError("provider envelope must be an object")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise SafeTransportFailure(kind="response") from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_http_failure(error: urllib.error.HTTPError) -> SafeTransportFailure:
    try:
        body = error.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        body = b""
    finally:
        try:
            error.close()
        except Exception:
            pass
    signal = _known_provider_signal(body)
    retry_after = None
    try:
        retry_after = error.headers.get("Retry-After")
    except Exception:
        retry_after = None
    seconds, invalid = _parse_retry_after(retry_after)
    status = error.code if type(error.code) is int else None
    return SafeTransportFailure(
        kind="http",
        status_code=status,
        retry_after_seconds=seconds,
        retry_after_invalid=invalid,
        provider_signal=signal,
    )


def _known_provider_signal(body: object) -> ProviderSignal | None:
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
        )
        error = value.get("error")
        entries = error.get("errors")
        if type(error) is not dict or type(entries) is not list:
            return None
        reasons = {
            entry.get("reason")
            for entry in entries
            if type(entry) is dict and type(entry.get("reason")) is str
        }
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if reasons & _QUOTA_PROVIDER_REASONS:
        return "quota"
    if reasons & _INVALID_PAGE_TOKEN_REASONS:
        return "invalid_page_token"
    return None


def _parse_retry_after(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, False
    if (
        type(value) is not str
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        return None, True
    seconds = int(value)
    if seconds > _DEFER_FALLBACK_SECONDS:
        return None, True
    return seconds, False


def _validate_identity_batch(value: object, pattern: re.Pattern[str]) -> None:
    if (
        type(value) is not tuple
        or len(value) > _MAX_BATCH_SIZE
        or any(type(item) is not str or pattern.fullmatch(item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        _raise_request_invalid()


def _validate_page_token(value: object) -> None:
    if value is not None and (
        type(value) is not str or _PAGE_TOKEN.fullmatch(value) is None
    ):
        _raise_request_invalid()


def _parse_request_utc(value: object) -> datetime:
    if type(value) is not str or _UTC_TEXT.fullmatch(value) is None:
        _raise_request_invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise_request_invalid()
    if parsed.tzinfo is not timezone.utc or utc_iso(parsed) != value:
        _raise_request_invalid()
    return parsed


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _raise_request_invalid() -> None:
    raise DomainError("YOUTUBE_REQUEST_INVALID", "YouTube request is invalid")


def _response_invalid() -> YouTubeProviderFailure:
    return YouTubeProviderFailure("YOUTUBE_RESPONSE_INVALID", "permanent")


__all__ = [
    "AttemptReservation",
    "ChannelUploads",
    "ENDPOINT_COSTS",
    "EndpointClass",
    "MAX_RESPONSE_BYTES",
    "QUOTA_CONTRACT_VERSION",
    "QUOTA_REFERENCE_URL",
    "READ_UNIT_DAILY_LIMIT",
    "SEARCH_CALL_DAILY_LIMIT",
    "SafeTransportFailure",
    "UrllibYouTubeTransport",
    "YouTubeClient",
    "YouTubePage",
    "YouTubeProviderFailure",
    "YouTubeTransport",
]

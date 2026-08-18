from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoveryProfileVersion,
    SearchWindow,
    canonical_profile_hash,
    validate_profile_configuration,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.youtube.client import (
    ChannelUploads,
    YouTubeClient,
    YouTubePage,
)
from market_voice_forecast_ledger.youtube.metadata import normalize_video_item


_YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_UPLOADS_PLAYLIST_ID = re.compile(r"^UU[A-Za-z0-9_-]{22}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com"}
)


@dataclass(frozen=True, slots=True)
class DiscoveredIdPage:
    video_ids: tuple[str, ...]
    next_page_token: str | None


def extract_youtube_video_id(url: str) -> str:
    if (
        type(url) is not str
        or not url.startswith("https://")
        or not url.isascii()
        or "%" in url
        or "#" in url
        or "\\" in url
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
    ):
        _raise_url_invalid()
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        _raise_url_invalid()
    if parsed.scheme != "https" or parsed.fragment:
        _raise_url_invalid()
    host = parsed.netloc
    if host == "youtu.be":
        if parsed.query or "?" in url:
            _raise_url_invalid()
        parts = parsed.path.split("/")
        if len(parts) != 2:
            _raise_url_invalid()
        return _validated_url_video_id(parts[1])
    if host not in _YOUTUBE_HOSTS:
        _raise_url_invalid()
    if parsed.path == "/watch":
        prefix = "v="
        if not parsed.query.startswith(prefix) or parsed.query.count("=") != 1:
            _raise_url_invalid()
        video_id = parsed.query[len(prefix):]
        if parsed.query != f"v={video_id}":
            _raise_url_invalid()
        return _validated_url_video_id(video_id)
    if parsed.query or "?" in url:
        _raise_url_invalid()
    parts = parsed.path.split("/")
    if len(parts) != 3 or parts[1] not in {"shorts", "live"}:
        _raise_url_invalid()
    return _validated_url_video_id(parts[2])


class SeedUploadsDiscoverer:
    def __init__(self, client: YouTubeClient) -> None:
        self._client = client

    def resolve_uploads_playlist(self, channel_id: str) -> str:
        if not _valid_channel_id(channel_id):
            _raise_discovery_invalid()
        result = self._client.channels_uploads((channel_id,))
        if type(result) is not tuple or len(result) != 1:
            _raise_discovery_invalid()
        value = result[0]
        if (
            type(value) is not ChannelUploads
            or value.channel_id != channel_id
            or not _valid_uploads_playlist_id(value.uploads_playlist_id)
        ):
            _raise_discovery_invalid()
        return value.uploads_playlist_id

    def page_video_ids(
        self, playlist_id: str, page_token: str | None
    ) -> DiscoveredIdPage:
        if not _valid_uploads_playlist_id(playlist_id):
            _raise_discovery_invalid()
        _validate_page_token(page_token)
        page = _validated_page(self._client.playlist_items(playlist_id, page_token))
        result: list[str] = []
        seen: set[str] = set()
        for item in page.items:
            if type(item) is not dict:
                _raise_discovery_invalid()
            try:
                content_details = item["contentDetails"]
                snippet = item["snippet"]
                if type(content_details) is not dict or type(snippet) is not dict:
                    _raise_discovery_invalid()
                resource_id = snippet["resourceId"]
                video_id = content_details["videoId"]
                if (
                    type(resource_id) is not dict
                    or snippet["playlistId"] != playlist_id
                    or resource_id.get("kind") != "youtube#video"
                    or resource_id["videoId"] != video_id
                    or not _valid_video_id(video_id)
                    or video_id in seen
                ):
                    _raise_discovery_invalid()
            except DomainError:
                raise
            except (KeyError, TypeError):
                _raise_discovery_invalid()
            seen.add(video_id)
            result.append(video_id)
        return DiscoveredIdPage(tuple(result), page.next_page_token)


class CrossChannelSearchDiscoverer:
    def __init__(self, client: YouTubeClient) -> None:
        self._client = client

    def page_video_ids(
        self,
        profile: DiscoveryProfileVersion,
        window: SearchWindow,
        page_token: str | None,
    ) -> DiscoveredIdPage:
        _validate_search_inputs(profile, window, page_token)
        query = "|".join(profile.search_terms)
        page = _validated_page(
            self._client.search_videos(
                query=query,
                published_after=utc_iso(window.lower_bound),
                published_before=utc_iso(window.upper_bound),
                page_token=page_token,
            )
        )
        result: list[str] = []
        seen: set[str] = set()
        for item in page.items:
            if type(item) is not dict:
                _raise_discovery_invalid()
            try:
                identity = item["id"]
                if type(identity) is not dict:
                    _raise_discovery_invalid()
                video_id = identity["videoId"]
                if (
                    identity.get("kind") != "youtube#video"
                    or not _valid_video_id(video_id)
                    or video_id in seen
                ):
                    _raise_discovery_invalid()
            except DomainError:
                raise
            except (KeyError, TypeError):
                _raise_discovery_invalid()
            seen.add(video_id)
            result.append(video_id)
        return DiscoveredIdPage(tuple(result), page.next_page_token)


class ManualUrlDiscoverer:
    def __init__(
        self, client: YouTubeClient, *, clock: Callable[[], datetime]
    ) -> None:
        self._client = client
        self._clock = clock

    def fetch(self, video_id: str) -> tuple[CanonicalVideoMetadata, ...]:
        if not _valid_video_id(video_id):
            _raise_discovery_invalid()
        items = self._client.videos((video_id,))
        if type(items) is not tuple or len(items) > 1:
            _raise_discovery_invalid()
        if not items:
            return ()
        item = items[0]
        if (
            not isinstance(item, Mapping)
            or item.get("id") != video_id
        ):
            _raise_discovery_invalid()
        fetched_at = self._clock()
        if not _is_exact_utc(fetched_at):
            _raise_discovery_invalid()
        try:
            return (normalize_video_item(item, fetched_at=fetched_at),)
        except DomainError as cause:
            if cause.code == "YOUTUBE_VIDEO_UNAVAILABLE":
                return ()
            raise


def _validate_search_inputs(
    profile: object, window: object, page_token: object
) -> None:
    if type(profile) is not DiscoveryProfileVersion:
        _raise_discovery_invalid()
    try:
        validate_profile_configuration(
            profile.seed_channel_ids, profile.search_terms
        )
        expected_hash = canonical_profile_hash(
            profile.seed_channel_ids, profile.search_terms
        )
    except DomainError:
        _raise_discovery_invalid()
    if (
        profile.config_hash != expected_hash
        or any(
            "|" in term or any(ord(character) < 32 or ord(character) == 127 for character in term)
            for term in profile.search_terms
        )
        or type(window) is not SearchWindow
        or not _is_exact_utc(window.lower_bound)
        or not _is_exact_utc(window.upper_bound)
        or window.lower_bound >= window.upper_bound
    ):
        _raise_discovery_invalid()
    _validate_page_token(page_token)


def _validated_page(page: object) -> YouTubePage:
    if (
        type(page) is not YouTubePage
        or type(page.items) is not tuple
        or len(page.items) > 50
    ):
        _raise_discovery_invalid()
    if page.next_page_token is not None and (
        type(page.next_page_token) is not str
        or _PAGE_TOKEN.fullmatch(page.next_page_token) is None
    ):
        _raise_discovery_invalid()
    return page


def _validate_page_token(value: object) -> None:
    if value is not None and (
        type(value) is not str or _PAGE_TOKEN.fullmatch(value) is None
    ):
        _raise_discovery_invalid()


def _validated_url_video_id(value: object) -> str:
    if not _valid_video_id(value):
        _raise_url_invalid()
    return value


def _valid_channel_id(value: object) -> bool:
    return type(value) is str and _YOUTUBE_CHANNEL_ID.fullmatch(value) is not None


def _valid_uploads_playlist_id(value: object) -> bool:
    return (
        type(value) is str
        and _YOUTUBE_UPLOADS_PLAYLIST_ID.fullmatch(value) is not None
    )


def _valid_video_id(value: object) -> bool:
    return type(value) is str and _YOUTUBE_VIDEO_ID.fullmatch(value) is not None


def _is_exact_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is timezone.utc


def _raise_discovery_invalid() -> None:
    raise DomainError(
        "YOUTUBE_DISCOVERY_INVALID", "YouTube discovery response is invalid"
    )


def _raise_url_invalid() -> None:
    raise DomainError("INVALID_YOUTUBE_URL", "YouTube URL is invalid")


__all__ = [
    "CrossChannelSearchDiscoverer",
    "DiscoveredIdPage",
    "ManualUrlDiscoverer",
    "SeedUploadsDiscoverer",
    "extract_youtube_video_id",
]

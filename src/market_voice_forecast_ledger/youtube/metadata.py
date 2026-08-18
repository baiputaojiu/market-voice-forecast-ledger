from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from market_voice_forecast_ledger.domain.discovery import (
    YOUTUBE_METADATA_SCHEMA_VERSION,
    CanonicalVideoMetadata,
    LiveState,
    canonical_video_metadata_hash,
)
from market_voice_forecast_ledger.domain.errors import DomainError


MAX_CHANNEL_TITLE_CODEPOINTS = 100
MAX_TITLE_CODEPOINTS = 100
MAX_DESCRIPTION_CODEPOINTS = 5_000
MAX_DURATION_SECONDS = 2_147_483_647

_YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PROVIDER_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_DURATION = re.compile(
    r"^P"
    r"(?:(?P<days>0|[1-9]\d*)D)?"
    r"(?:T"
    r"(?:(?P<hours>0|[1-9]\d*)H)?"
    r"(?:(?P<minutes>0|[1-9]\d*)M)?"
    r"(?:(?P<seconds>(?:0|[1-9]\d*)(?:\.\d{1,9})?)S)?"
    r")?$"
)
_LIVE_STATES = {
    "none": LiveState.NOT_LIVE,
    "live": LiveState.LIVE,
    "upcoming": LiveState.UPCOMING,
}
_MISSING = object()


def normalize_video_item(
    item: Mapping[str, object], fetched_at: datetime
) -> CanonicalVideoMetadata:
    if not isinstance(item, Mapping) or not _is_exact_utc(fetched_at):
        _raise_metadata_invalid()
    try:
        video_id = item["id"]
        snippet = item["snippet"]
        content_details = item["contentDetails"]
        status = item["status"]
        live_details = item.get("liveStreamingDetails")
        if (
            type(video_id) is not str
            or _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None
            or type(snippet) is not dict
            or type(content_details) is not dict
            or type(status) is not dict
            or (live_details is not None and type(live_details) is not dict)
        ):
            _raise_metadata_invalid()

        channel_id = snippet["channelId"]
        if (
            type(channel_id) is not str
            or _YOUTUBE_CHANNEL_ID.fullmatch(channel_id) is None
        ):
            _raise_metadata_invalid()
        channel_title = _provider_text(
            snippet["channelTitle"],
            maximum=MAX_CHANNEL_TITLE_CODEPOINTS,
            allow_empty=False,
            allow_linefeeds=False,
        )
        title = _provider_text(
            snippet["title"],
            maximum=MAX_TITLE_CODEPOINTS,
            allow_empty=False,
            allow_linefeeds=False,
        )
        description = _provider_text(
            snippet["description"],
            maximum=MAX_DESCRIPTION_CODEPOINTS,
            allow_empty=True,
            allow_linefeeds=True,
        )
        snippet_published_at = _parse_provider_utc(snippet["publishedAt"])
        live_state_value = snippet["liveBroadcastContent"]
        if type(live_state_value) is not str or live_state_value not in _LIVE_STATES:
            _raise_metadata_invalid()
        live_state = _LIVE_STATES[live_state_value]

        duration_seconds = _parse_youtube_duration(content_details["duration"])
        _validate_optional_bool(content_details, "licensedContent")
        _validate_optional_string_list(snippet, "tags")
        _validate_optional_bool(status, "embeddable")
        _validate_optional_bool(status, "publicStatsViewable")
        _validate_optional_bool(status, "madeForKids")
        _validate_optional_bool(status, "selfDeclaredMadeForKids")

        privacy_status = status["privacyStatus"]
        upload_status = status["uploadStatus"]
        if type(privacy_status) is not str or type(upload_status) is not str:
            _raise_metadata_invalid()
        if privacy_status != "public" or upload_status != "processed":
            _raise_unavailable()

        actual_value = (
            _MISSING
            if live_details is None
            else live_details.get("actualStartTime", _MISSING)
        )
        actual_start_time = (
            None
            if actual_value is _MISSING
            else _parse_provider_utc(actual_value)
        )
        published_at = actual_start_time or snippet_published_at
        return CanonicalVideoMetadata.build(
            youtube_video_id=video_id,
            channel_id=channel_id,
            channel_title=channel_title,
            title=title,
            description=description,
            published_at=published_at,
            duration_seconds=duration_seconds,
            live_state=live_state,
            actual_start_time=actual_start_time,
            schema_version=YOUTUBE_METADATA_SCHEMA_VERSION,
            fetched_at=fetched_at,
        )
    except DomainError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        _raise_metadata_invalid()


def canonical_metadata_hash(metadata: CanonicalVideoMetadata) -> str:
    return canonical_video_metadata_hash(metadata)


def _parse_youtube_duration(value: object) -> int:
    if type(value) is not str or len(value) > 64:
        _raise_metadata_invalid()
    match = _DURATION.fullmatch(value)
    if match is None:
        _raise_metadata_invalid()
    groups = match.groupdict()
    if all(component is None for component in groups.values()):
        _raise_metadata_invalid()
    if "T" in value and all(
        groups[name] is None for name in ("hours", "minutes", "seconds")
    ):
        _raise_metadata_invalid()
    try:
        days = Decimal(groups["days"] or "0")
        hours = Decimal(groups["hours"] or "0")
        minutes = Decimal(groups["minutes"] or "0")
        seconds = Decimal(groups["seconds"] or "0")
        total = days * 86_400 + hours * 3_600 + minutes * 60 + seconds
    except (InvalidOperation, ValueError):
        _raise_metadata_invalid()
    if total < 0 or total > MAX_DURATION_SECONDS:
        _raise_metadata_invalid()
    return int(total)


def _parse_provider_utc(value: object) -> datetime:
    if type(value) is not str or _PROVIDER_UTC.fullmatch(value) is None:
        _raise_metadata_invalid()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _raise_metadata_invalid()
    if parsed.tzinfo is not timezone.utc:
        _raise_metadata_invalid()
    return parsed


def _provider_text(
    value: object,
    *,
    maximum: int,
    allow_empty: bool,
    allow_linefeeds: bool,
) -> str:
    if type(value) is not str or len(value) > maximum:
        _raise_metadata_invalid()
    if not allow_empty and not value.strip():
        _raise_metadata_invalid()
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs" or (
            category == "Cc" and not (allow_linefeeds and character == "\n")
        ):
            _raise_metadata_invalid()
    return value


def _validate_optional_bool(container: dict[str, object], field: str) -> None:
    if field in container and type(container[field]) is not bool:
        _raise_metadata_invalid()


def _validate_optional_string_list(
    container: dict[str, object], field: str
) -> None:
    if field not in container:
        return
    value = container[field]
    if type(value) is not list or any(type(item) is not str for item in value):
        _raise_metadata_invalid()


def _is_exact_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is timezone.utc


def _raise_metadata_invalid() -> None:
    raise DomainError(
        "YOUTUBE_METADATA_INVALID", "YouTube video metadata is invalid"
    )


def _raise_unavailable() -> None:
    raise DomainError(
        "YOUTUBE_VIDEO_UNAVAILABLE", "YouTube video is unavailable"
    )


__all__ = [
    "MAX_CHANNEL_TITLE_CODEPOINTS",
    "MAX_DESCRIPTION_CODEPOINTS",
    "MAX_DURATION_SECONDS",
    "MAX_TITLE_CODEPOINTS",
    "canonical_metadata_hash",
    "normalize_video_item",
]

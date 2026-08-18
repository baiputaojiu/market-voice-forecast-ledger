from __future__ import annotations

import copy
import hashlib
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    LiveState,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.youtube.metadata import (
    MAX_DESCRIPTION_CODEPOINTS,
    MAX_DURATION_SECONDS,
    MAX_TITLE_CODEPOINTS,
    canonical_metadata_hash,
    normalize_video_item,
)
from tests.backend.youtube_fakes import FIXED_NOW, synthetic_video_item


VIDEO_ID = "video000001"
CHANNEL_ID = "UCabcdefghijklmnopqrstuv"


def _assert_metadata_invalid(item: object, *, fetched_at: object = FIXED_NOW) -> None:
    with pytest.raises(DomainError) as captured:
        normalize_video_item(item, fetched_at=fetched_at)  # type: ignore[arg-type]
    assert captured.value.code == "YOUTUBE_METADATA_INVALID"
    assert str(captured.value) == "YouTube video metadata is invalid"


def _assert_unavailable(item: object) -> None:
    with pytest.raises(DomainError) as captured:
        normalize_video_item(item, fetched_at=FIXED_NOW)  # type: ignore[arg-type]
    assert captured.value.code == "YOUTUBE_VIDEO_UNAVAILABLE"
    assert str(captured.value) == "YouTube video is unavailable"


def test_live_actual_start_time_is_analysis_published_at_and_hash_is_exact():
    item = synthetic_video_item(
        snippet_published_at="2026-08-10T01:00:00Z",
        actual_start_time="2026-08-10T02:03:04Z",
        duration="PT1H2M3S",
        live_broadcast_content="live",
    )

    value = normalize_video_item(item, fetched_at=FIXED_NOW)

    assert value == CanonicalVideoMetadata(
        youtube_video_id=VIDEO_ID,
        channel_id=CHANNEL_ID,
        channel_title="Synthetic Channel",
        title="Synthetic market discussion",
        description="Synthetic description.\nSecond line.",
        published_at=datetime(2026, 8, 10, 2, 3, 4, tzinfo=timezone.utc),
        duration_seconds=3723,
        live_state=LiveState.LIVE,
        actual_start_time=datetime(2026, 8, 10, 2, 3, 4, tzinfo=timezone.utc),
        schema_version="youtube-video-metadata.v1",
        canonical_hash=value.canonical_hash,
        fetched_at=FIXED_NOW,
    )
    expected_payload = (
        '{"actual_start_time":"2026-08-10T02:03:04.000000Z",'
        '"channel_id":"UCabcdefghijklmnopqrstuv",'
        '"channel_title":"Synthetic Channel",'
        '"description":"Synthetic description.\\nSecond line.",'
        '"duration_seconds":3723,"live_state":"live",'
        '"published_at":"2026-08-10T02:03:04.000000Z",'
        '"schema":"youtube-video-metadata.v1",'
        '"title":"Synthetic market discussion",'
        '"youtube_video_id":"video000001"}'
    )
    assert value.canonical_hash == hashlib.sha256(
        expected_payload.encode("utf-8")
    ).hexdigest()
    assert canonical_metadata_hash(value) == value.canonical_hash


def test_vod_uses_snippet_published_at_and_not_live_state():
    value = normalize_video_item(
        synthetic_video_item(
            snippet_published_at="2026-08-10T01:02:03.123456Z",
            duration="PT42S",
            live_broadcast_content="none",
        ),
        fetched_at=FIXED_NOW,
    )

    assert value.published_at == datetime(
        2026, 8, 10, 1, 2, 3, 123456, tzinfo=timezone.utc
    )
    assert value.actual_start_time is None
    assert value.live_state is LiveState.NOT_LIVE
    assert value.duration_seconds == 42


def test_upcoming_live_without_actual_start_uses_snippet_timestamp():
    value = normalize_video_item(
        synthetic_video_item(
            snippet_published_at="2026-08-11T01:02:03Z",
            live_broadcast_content="upcoming",
        ),
        fetched_at=FIXED_NOW,
    )

    assert value.published_at == datetime(
        2026, 8, 11, 1, 2, 3, tzinfo=timezone.utc
    )
    assert value.actual_start_time is None
    assert value.live_state is LiveState.UPCOMING


@pytest.mark.parametrize(
    ("duration", "expected_seconds"),
    (
        ("PT0S", 0),
        ("P0D", 0),
        ("PT59S", 59),
        ("PT1M", 60),
        ("PT1H2M3S", 3723),
        ("P1DT2H3M4S", 93_784),
        ("PT1.999S", 1),
        ("PT0.5S", 0),
        (f"PT{MAX_DURATION_SECONDS}S", MAX_DURATION_SECONDS),
    ),
)
def test_iso8601_duration_normalizes_to_bounded_whole_seconds(
    duration: str, expected_seconds: int
):
    value = normalize_video_item(
        synthetic_video_item(duration=duration), fetched_at=FIXED_NOW
    )
    assert value.duration_seconds == expected_seconds


@pytest.mark.parametrize(
    "duration",
    (
        "",
        "P",
        "PT",
        "P1Y",
        "P1M",
        "P1W",
        "P-1D",
        "PT-1S",
        "PT1.5M",
        "PT1,5S",
        "PT1.S",
        "PT.5S",
        "PT1e3S",
        "PT1S ",
        f"PT{MAX_DURATION_SECONDS + 1}S",
        f"PT{MAX_DURATION_SECONDS}.000001S",
        "PT999999999999999999999999999999999999S",
    ),
)
def test_invalid_or_overflowing_iso8601_duration_fails_closed(duration: str):
    _assert_metadata_invalid(synthetic_video_item(duration=duration))


@pytest.mark.parametrize("duration", (True, 1, 1.0, None, [], {}))
def test_duration_requires_an_exact_string(duration: object):
    item = synthetic_video_item()
    item["contentDetails"]["duration"] = duration  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-10 01:00:00Z",
        "2026-08-10T01:00:00z",
        "2026-08-10T01:00:00+00:00",
        "2026-08-10T10:00:00+09:00",
        "2026-08-10T01:00Z",
        "2026-08-10T01:00:00.1234567Z",
        "2026-8-10T01:00:00Z",
        "2026-02-30T01:00:00Z",
        " 2026-08-10T01:00:00Z",
    ),
)
def test_snippet_published_at_requires_strict_utc(timestamp: str):
    _assert_metadata_invalid(
        synthetic_video_item(snippet_published_at=timestamp)
    )


@pytest.mark.parametrize("timestamp", (True, 0, None, [], {}))
def test_snippet_published_at_requires_an_exact_scalar_string(timestamp: object):
    item = synthetic_video_item()
    item["snippet"]["publishedAt"] = timestamp  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-10T02:03:04+00:00",
        "2026-08-10T02:03:04.1234567Z",
        "2026-08-10T02:03:04Z\n",
    ),
)
def test_actual_start_time_requires_strict_utc(timestamp: str):
    _assert_metadata_invalid(
        synthetic_video_item(actual_start_time=timestamp)
    )


@pytest.mark.parametrize("timestamp", (True, 0, [], {}))
def test_actual_start_time_requires_an_exact_scalar_string(timestamp: object):
    item = synthetic_video_item(actual_start_time="2026-08-10T02:03:04Z")
    item["liveStreamingDetails"]["actualStartTime"] = timestamp  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize("live_state", ("", "completed", "LIVE", True, [], {}))
def test_live_broadcast_content_requires_an_exact_supported_scalar(live_state: object):
    item = synthetic_video_item()
    item["snippet"]["liveBroadcastContent"] = live_state  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    ("container", "field", "value"),
    (
        ("contentDetails", "licensedContent", 1),
        ("contentDetails", "licensedContent", "true"),
        ("status", "embeddable", 1),
        ("status", "publicStatsViewable", "true"),
    ),
)
def test_known_provider_boolean_fields_require_exact_booleans(
    container: str, field: str, value: object
):
    item = synthetic_video_item()
    item[container][field] = value  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize("tags", (("synthetic",), ["ok", True], "synthetic", {}))
def test_known_provider_list_fields_require_an_exact_string_list(tags: object):
    item = synthetic_video_item()
    item["snippet"]["tags"] = tags  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    ("container", "field", "value"),
    (
        ("root", "id", True),
        ("root", "id", "short"),
        ("snippet", "channelId", 7),
        ("snippet", "channelId", "UCshort"),
        ("snippet", "channelTitle", []),
        ("snippet", "title", True),
        ("snippet", "description", {}),
        ("status", "privacyStatus", []),
        ("status", "uploadStatus", True),
    ),
)
def test_consumed_provider_scalars_require_exact_types_and_shapes(
    container: str, field: str, value: object
):
    item = synthetic_video_item()
    target = item if container == "root" else item[container]
    target[field] = value  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    "field",
    (
        "id",
        "snippet",
        "contentDetails",
        "status",
    ),
)
def test_missing_required_top_level_field_fails_closed(field: str):
    item = synthetic_video_item()
    del item[field]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize(
    ("container", "field"),
    (
        ("snippet", "publishedAt"),
        ("snippet", "channelId"),
        ("snippet", "channelTitle"),
        ("snippet", "title"),
        ("snippet", "description"),
        ("snippet", "liveBroadcastContent"),
        ("contentDetails", "duration"),
        ("status", "privacyStatus"),
        ("status", "uploadStatus"),
    ),
)
def test_missing_required_nested_field_fails_closed(container: str, field: str):
    item = synthetic_video_item()
    del item[container][field]  # type: ignore[index]
    _assert_metadata_invalid(item)


@pytest.mark.parametrize("item", (None, True, [], (), "provider item", 1))
def test_missing_or_nonscalar_provider_item_fails_closed(item: object):
    _assert_metadata_invalid(item)


@pytest.mark.parametrize("container", ("snippet", "contentDetails", "status"))
@pytest.mark.parametrize("value", ([], (), True, "object"))
def test_nested_provider_containers_require_exact_objects(
    container: str, value: object
):
    item = synthetic_video_item()
    item[container] = value
    _assert_metadata_invalid(item)


@pytest.mark.parametrize("privacy", ("private", "unlisted", "scheduled"))
def test_nonpublic_video_is_reported_as_unavailable(privacy: str):
    _assert_unavailable(synthetic_video_item(privacy_status=privacy))


@pytest.mark.parametrize(
    "upload_status", ("deleted", "failed", "rejected", "uploaded")
)
def test_unprocessed_or_deleted_video_is_reported_as_unavailable(
    upload_status: str,
):
    _assert_unavailable(synthetic_video_item(upload_status=upload_status))


def test_title_and_description_accept_exact_codepoint_boundaries():
    title = "界" * MAX_TITLE_CODEPOINTS
    description = "語" * MAX_DESCRIPTION_CODEPOINTS
    value = normalize_video_item(
        synthetic_video_item(title=title, description=description),
        fetched_at=FIXED_NOW,
    )
    assert value.title == title
    assert value.description == description
    assert len(value.title) == MAX_TITLE_CODEPOINTS
    assert len(value.description) == MAX_DESCRIPTION_CODEPOINTS


@pytest.mark.parametrize(
    ("title", "description"),
    (
        ("界" * (MAX_TITLE_CODEPOINTS + 1), "safe"),
        ("safe", "語" * (MAX_DESCRIPTION_CODEPOINTS + 1)),
        ("", "safe"),
        ("   ", "safe"),
    ),
)
def test_title_and_description_reject_invalid_codepoint_boundaries(
    title: str, description: str
):
    _assert_metadata_invalid(
        synthetic_video_item(title=title, description=description)
    )


@pytest.mark.parametrize("control", ("\x00", "\t", "\n", "\r", "\x7f", "\x85"))
def test_title_rejects_control_characters(control: str):
    _assert_metadata_invalid(synthetic_video_item(title=f"before{control}after"))


@pytest.mark.parametrize("control", ("\x00", "\r", "\x7f", "\x85"))
def test_description_rejects_unsafe_controls_but_preserves_linefeeds(control: str):
    _assert_metadata_invalid(
        synthetic_video_item(description=f"before{control}after")
    )
    accepted = normalize_video_item(
        synthetic_video_item(description="first line\nsecond line"),
        fetched_at=FIXED_NOW,
    )
    assert accepted.description == "first line\nsecond line"


@pytest.mark.parametrize(
    "fetched_at",
    (
        datetime(2026, 8, 18, 2, 3, 4),
        datetime(2026, 8, 18, 11, 3, 4, tzinfo=timezone(timedelta(hours=9))),
        True,
        "2026-08-18T02:03:04Z",
    ),
)
def test_fetched_at_requires_an_exact_utc_datetime(fetched_at: object):
    _assert_metadata_invalid(synthetic_video_item(), fetched_at=fetched_at)


def test_fetched_at_is_excluded_from_canonical_hash():
    first = normalize_video_item(synthetic_video_item(), fetched_at=FIXED_NOW)
    second = normalize_video_item(
        synthetic_video_item(),
        fetched_at=datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc),
    )

    assert first.fetched_at != second.fetched_at
    assert first.canonical_hash == second.canonical_hash
    assert canonical_metadata_hash(replace(first, fetched_at=second.fetched_at)) == (
        first.canonical_hash
    )


def test_every_provider_only_raw_field_is_excluded_from_hash_and_value_object():
    first_item = synthetic_video_item()
    second_item = copy.deepcopy(first_item)
    second_item["etag"] = "different-etag"
    second_item["kind"] = "different-provider-kind"
    second_item["statistics"] = {"viewCount": "999999", "likeCount": "42"}
    second_item["topicDetails"] = {"topicCategories": ["changed"]}
    second_item["snippet"]["tags"] = ["changed"]  # type: ignore[index]
    second_item["snippet"]["thumbnails"] = {"high": {"url": "changed"}}  # type: ignore[index]
    second_item["contentDetails"]["definition"] = "sd"  # type: ignore[index]
    second_item["contentDetails"]["licensedContent"] = False  # type: ignore[index]
    second_item["liveStreamingDetails"]["scheduledStartTime"] = (  # type: ignore[index]
        "2040-01-01T00:00:00Z"
    )
    second_item["status"]["embeddable"] = False  # type: ignore[index]
    second_item["status"]["publicStatsViewable"] = False  # type: ignore[index]

    first = normalize_video_item(first_item, fetched_at=FIXED_NOW)
    second = normalize_video_item(second_item, fetched_at=FIXED_NOW)

    assert first == second
    assert first.canonical_hash == second.canonical_hash
    assert tuple(field.name for field in fields(CanonicalVideoMetadata)) == (
        "youtube_video_id",
        "channel_id",
        "channel_title",
        "title",
        "description",
        "published_at",
        "duration_seconds",
        "live_state",
        "actual_start_time",
        "schema_version",
        "canonical_hash",
        "fetched_at",
    )
    for raw_field in (
        "etag",
        "kind",
        "statistics",
        "topicDetails",
        "tags",
        "thumbnails",
        "definition",
        "licensedContent",
        "scheduledStartTime",
        "embeddable",
        "publicStatsViewable",
    ):
        assert not hasattr(first, raw_field)


def test_normalized_value_does_not_retain_or_follow_mutated_provider_item():
    item = synthetic_video_item(title="Original synthetic title")
    value = normalize_video_item(item, fetched_at=FIXED_NOW)

    item["snippet"]["title"] = "Mutated provider title"  # type: ignore[index]
    item["statistics"] = {"private": "mutated"}

    assert value.title == "Original synthetic title"
    assert "mutated" not in repr(value)

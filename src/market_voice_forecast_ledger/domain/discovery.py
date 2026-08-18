import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError


_YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CANONICAL_HASH = re.compile(r"^[0-9a-f]{64}$")
YOUTUBE_METADATA_SCHEMA_VERSION = "youtube-video-metadata.v1"


class DiscoverySourceKind(StrEnum):
    SEED_UPLOADS = "seed_uploads"
    CROSS_CHANNEL_SEARCH = "cross_channel_search"
    MANUAL_URL = "manual_url"


class PresenceState(StrEnum):
    UNVERIFIED = "presence_unverified"
    CONFIRMED = "presence_confirmed"
    REJECTED = "presence_rejected"


class PresenceOrigin(StrEnum):
    COLLECTION_INITIAL = "collection_initial"
    VOICE_VERIFICATION = "voice_verification"


class LiveState(StrEnum):
    NOT_LIVE = "not_live"
    LIVE = "live"
    UPCOMING = "upcoming"


def canonical_profile_hash(
    seed_channel_ids: tuple[str, ...], search_terms: tuple[str, ...]
) -> str:
    validate_profile_configuration(seed_channel_ids, search_terms)
    return sha256_text(canonical_json({
        "schema": "youtube-discovery-profile.v1",
        "seed_channel_ids": list(seed_channel_ids),
        "search_terms": list(search_terms),
    }))


def validate_profile_configuration(
    seed_channel_ids: object, search_terms: object
) -> None:
    if type(seed_channel_ids) is not tuple or type(search_terms) is not tuple:
        _raise_profile_invalid("profile values require exact tuple inputs")
    if any(
        type(channel_id) is not str
        or _YOUTUBE_CHANNEL_ID.fullmatch(channel_id) is None
        for channel_id in seed_channel_ids
    ):
        _raise_profile_invalid("profile seed channel id is invalid")
    if len(set(seed_channel_ids)) != len(seed_channel_ids):
        _raise_profile_invalid("profile seed channel ids must be unique")
    if not search_terms or any(
        type(term) is not str or not term.strip() or len(term) > 100
        for term in search_terms
    ):
        _raise_profile_invalid("profile search terms are invalid")
    if len(set(search_terms)) != len(search_terms):
        _raise_profile_invalid("profile search terms must be unique")


def _raise_profile_invalid(message: str) -> None:
    raise DomainError("DISCOVERY_PROFILE_INVALID", message)


def canonical_presence_decision_hash(
    *,
    candidate_id: int,
    state: PresenceState,
    decision_origin: PresenceOrigin,
    evidence_ref: str,
    evidence_hash: str,
    created_at: datetime,
) -> str:
    return sha256_text(canonical_json({
        "candidate_id": candidate_id,
        "created_at": utc_iso(created_at),
        "decision_origin": decision_origin.value,
        "evidence_hash": evidence_hash,
        "evidence_ref": evidence_ref,
        "schema": "youtube-presence-decision.v1",
        "state": state.value,
    }))


@dataclass(frozen=True, slots=True)
class DiscoveryProfileVersion:
    id: int
    profile_id: int
    subject_id: int
    config_hash: str
    seed_channel_ids: tuple[str, ...]
    search_terms: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CanonicalVideoMetadata:
    youtube_video_id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    published_at: datetime
    duration_seconds: int
    live_state: LiveState
    actual_start_time: datetime | None
    schema_version: str
    canonical_hash: str
    fetched_at: datetime

    @classmethod
    def build(
        cls,
        *,
        youtube_video_id: str,
        channel_id: str,
        channel_title: str,
        title: str,
        description: str,
        published_at: datetime,
        duration_seconds: int,
        live_state: LiveState,
        actual_start_time: datetime | None,
        schema_version: str,
        fetched_at: datetime,
    ) -> "CanonicalVideoMetadata":
        canonical_hash = sha256_text(canonical_json({
            "actual_start_time": None if actual_start_time is None else utc_iso(actual_start_time),
            "channel_id": channel_id,
            "channel_title": channel_title,
            "description": description,
            "duration_seconds": duration_seconds,
            "live_state": live_state.value,
            "published_at": utc_iso(published_at),
            "schema": schema_version,
            "title": title,
            "youtube_video_id": youtube_video_id,
        }))
        return cls(
            youtube_video_id, channel_id, channel_title, title, description,
            published_at, duration_seconds, live_state, actual_start_time,
            schema_version, canonical_hash, fetched_at,
        )


def canonical_video_metadata_hash(metadata: CanonicalVideoMetadata) -> str:
    _validate_metadata_shape(metadata, validate_hash=False)
    return sha256_text(canonical_json({
        "actual_start_time": (
            None
            if metadata.actual_start_time is None
            else utc_iso(metadata.actual_start_time)
        ),
        "channel_id": metadata.channel_id,
        "channel_title": metadata.channel_title,
        "description": metadata.description,
        "duration_seconds": metadata.duration_seconds,
        "live_state": metadata.live_state.value,
        "published_at": utc_iso(metadata.published_at),
        "schema": metadata.schema_version,
        "title": metadata.title,
        "youtube_video_id": metadata.youtube_video_id,
    }))


def validate_canonical_video_metadata(metadata: object) -> None:
    _validate_metadata_shape(metadata, validate_hash=True)


def _validate_metadata_shape(metadata: object, *, validate_hash: bool) -> None:
    if type(metadata) is not CanonicalVideoMetadata:
        _raise_metadata_invalid()
    if (
        type(metadata.youtube_video_id) is not str
        or _YOUTUBE_VIDEO_ID.fullmatch(metadata.youtube_video_id) is None
        or type(metadata.channel_id) is not str
        or _YOUTUBE_CHANNEL_ID.fullmatch(metadata.channel_id) is None
        or type(metadata.channel_title) is not str
        or type(metadata.title) is not str
        or type(metadata.description) is not str
        or not _is_exact_utc(metadata.published_at)
        or type(metadata.duration_seconds) is not int
        or metadata.duration_seconds < 0
        or type(metadata.live_state) is not LiveState
        or (
            metadata.actual_start_time is not None
            and not _is_exact_utc(metadata.actual_start_time)
        )
        or type(metadata.schema_version) is not str
        or metadata.schema_version != YOUTUBE_METADATA_SCHEMA_VERSION
        or not _is_exact_utc(metadata.fetched_at)
    ):
        _raise_metadata_invalid()
    if validate_hash and (
        type(metadata.canonical_hash) is not str
        or _CANONICAL_HASH.fullmatch(metadata.canonical_hash) is None
        or metadata.canonical_hash != canonical_video_metadata_hash(metadata)
    ):
        _raise_metadata_invalid()


def _is_exact_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is timezone.utc


def _raise_metadata_invalid() -> None:
    raise DomainError(
        "DISCOVERY_METADATA_INVALID",
        "canonical video metadata is invalid",
    )


@dataclass(frozen=True, slots=True)
class DiscoveryObservation:
    id: int
    job_id: int
    profile_id: int
    video_id: int
    metadata_snapshot_id: int
    metadata_snapshot_hash: str
    source_kind: DiscoverySourceKind
    source_key: str
    observed_at: datetime
    observation_hash: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SubjectVideoCandidate:
    id: int
    profile_id: int
    video_id: int
    first_observation_id: int
    current_presence_decision_id: int
    metadata_snapshot_id: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PresenceDecision:
    id: int
    candidate_id: int
    state: PresenceState
    decision_origin: PresenceOrigin
    evidence_ref: str
    evidence_hash: str
    decision_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MetadataBatchResult:
    snapshot_ids: tuple[int, ...]
    observation_ids: tuple[int, ...]
    candidate_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SearchWindow:
    id: int
    job_id: int
    unit_key: str
    ordinal: int
    lower_bound: datetime
    upper_bound: datetime
    next_page_token: str | None
    page_count: int
    split_parent_id: int | None
    completed_at: datetime | None
    window_hash: str


@dataclass(frozen=True, slots=True)
class YouTubeSyncManifest:
    job_id: int
    sync_kind: str
    upper_bound: datetime
    backfill_floor: datetime
    quota_contract_version: str
    profile_set_hash: str
    manual_request_id: int | None
    resume_not_before_utc: datetime | None
    manifest_hash: str
    created_at: datetime

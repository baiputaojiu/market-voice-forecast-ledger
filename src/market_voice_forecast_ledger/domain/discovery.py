from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso


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
    return sha256_text(canonical_json({
        "schema": "youtube-discovery-profile.v1",
        "seed_channel_ids": list(seed_channel_ids),
        "search_terms": list(search_terms),
    }))


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

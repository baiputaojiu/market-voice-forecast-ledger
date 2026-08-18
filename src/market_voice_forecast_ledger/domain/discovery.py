import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.enums import JobStage


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


class YouTubeSyncKind(StrEnum):
    FULL_DISCOVERY = "full_discovery"
    MANUAL = "manual"


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
class YouTubeSyncManifestProfile:
    ordinal: int
    profile_id: int
    profile_version_id: int
    config_hash: str
    discoverer_set_hash: str


YOUTUBE_SEED_EXECUTION_CONTRACT = "youtube-seed-discovery-contract-v1"
YOUTUBE_SEARCH_EXECUTION_CONTRACT = "youtube-search-discovery-contract-v1"
YOUTUBE_MANUAL_EXECUTION_CONTRACT = "youtube-manual-discovery-contract-v1"


@dataclass(frozen=True, slots=True)
class YouTubeSyncUnitSpec:
    unit_key: str
    stage: JobStage
    ordinal: int
    profile_id: int
    profile_version_id: int
    config_hash: str
    source_kind: DiscoverySourceKind
    source_key: str
    declared_input_hash: str
    execution_contract_hash: str


def build_youtube_sync_shape(
    *,
    sync_kind: YouTubeSyncKind,
    profiles: tuple[DiscoveryProfileVersion, ...],
    upper_bound: datetime,
    backfill_floor: datetime,
    quota_contract_version: str,
    manual_request_id: int | None,
    manual_video_id: str | None,
) -> tuple[
    tuple[YouTubeSyncManifestProfile, ...],
    tuple[YouTubeSyncUnitSpec, ...],
]:
    if (
        type(sync_kind) is not YouTubeSyncKind
        or type(profiles) is not tuple
        or not profiles
        or any(type(profile) is not DiscoveryProfileVersion for profile in profiles)
        or len({profile.profile_id for profile in profiles}) != len(profiles)
        or not _is_exact_utc(upper_bound)
        or not _is_exact_utc(backfill_floor)
        or backfill_floor > upper_bound
        or type(quota_contract_version) is not str
        or not quota_contract_version
    ):
        _raise_sync_manifest_invalid()
    for profile in profiles:
        if (
            profile.id <= 0
            or profile.profile_id <= 0
            or profile.config_hash
            != canonical_profile_hash(
                profile.seed_channel_ids, profile.search_terms
            )
        ):
            _raise_sync_manifest_invalid()
    if sync_kind is YouTubeSyncKind.FULL_DISCOVERY:
        if manual_request_id is not None or manual_video_id is not None:
            _raise_sync_manifest_invalid()
    elif (
        len(profiles) != 1
        or type(manual_request_id) is not int
        or manual_request_id <= 0
        or type(manual_video_id) is not str
        or backfill_floor != upper_bound
    ):
        _raise_sync_manifest_invalid()

    unit_specs: list[YouTubeSyncUnitSpec] = []
    manifest_profiles: list[YouTubeSyncManifestProfile] = []
    manual_hash = (
        None
        if manual_video_id is None
        else youtube_manual_video_hash(manual_video_id)
    )
    ordinal = 1
    for profile_ordinal, profile in enumerate(profiles, start=1):
        sources: tuple[
            tuple[str, JobStage, DiscoverySourceKind, str, str], ...
        ]
        if sync_kind is YouTubeSyncKind.FULL_DISCOVERY:
            seed_sources = tuple(
                (
                    f"youtube:profile:{profile.profile_id}:seed:{channel_id}",
                    JobStage.YOUTUBE_SEED_DISCOVERY,
                    DiscoverySourceKind.SEED_UPLOADS,
                    channel_id,
                    YOUTUBE_SEED_EXECUTION_CONTRACT,
                )
                for channel_id in profile.seed_channel_ids
            )
            sources = seed_sources + (
                (
                    f"youtube:profile:{profile.profile_id}:search",
                    JobStage.YOUTUBE_SEARCH_DISCOVERY,
                    DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                    youtube_search_source_key(profile.search_terms),
                    YOUTUBE_SEARCH_EXECUTION_CONTRACT,
                ),
            )
        else:
            sources = (
                (
                    f"youtube:manual-request:{manual_request_id}",
                    JobStage.YOUTUBE_MANUAL_DISCOVERY,
                    DiscoverySourceKind.MANUAL_URL,
                    f"manual-request:{manual_request_id}",
                    YOUTUBE_MANUAL_EXECUTION_CONTRACT,
                ),
            )
        profile_keys = tuple(source[0] for source in sources)
        manifest_profiles.append(
            YouTubeSyncManifestProfile(
                ordinal=profile_ordinal,
                profile_id=profile.profile_id,
                profile_version_id=profile.id,
                config_hash=profile.config_hash,
                discoverer_set_hash=youtube_discoverer_set_hash(profile_keys),
            )
        )
        for unit_key, stage, source_kind, source_key, contract in sources:
            unit_specs.append(
                YouTubeSyncUnitSpec(
                    unit_key=unit_key,
                    stage=stage,
                    ordinal=ordinal,
                    profile_id=profile.profile_id,
                    profile_version_id=profile.id,
                    config_hash=profile.config_hash,
                    source_kind=source_kind,
                    source_key=source_key,
                    declared_input_hash=youtube_sync_unit_input_hash(
                        sync_kind=sync_kind,
                        upper_bound=upper_bound,
                        backfill_floor=backfill_floor,
                        quota_contract_version=quota_contract_version,
                        profile_id=profile.profile_id,
                        profile_version_id=profile.id,
                        config_hash=profile.config_hash,
                        source_kind=source_kind,
                        source_key=source_key,
                        manual_request_id=manual_request_id,
                        manual_video_id_hash=manual_hash,
                    ),
                    execution_contract_hash=contract,
                )
            )
            ordinal += 1
    return tuple(manifest_profiles), tuple(unit_specs)


def _raise_sync_manifest_invalid() -> None:
    raise DomainError(
        "YOUTUBE_SYNC_MANIFEST_INVALID",
        "YouTube sync manifest is invalid",
    )


def youtube_search_source_key(search_terms: tuple[str, ...]) -> str:
    return source_key_for_search_terms(search_terms)


def source_key_for_search_terms(search_terms: tuple[str, ...]) -> str:
    validate_profile_configuration((), search_terms)
    return sha256_text(canonical_json({
        "ordered_terms": list(search_terms),
        "schema": "youtube-search-source.v1",
    }))


def initial_backfill_floor(upper_bound: datetime) -> datetime:
    if not _is_exact_utc(upper_bound):
        raise DomainError(
            "YOUTUBE_SYNC_REQUEST_INVALID",
            "YouTube sync request requires an exact UTC datetime",
        )
    try:
        return upper_bound.replace(year=upper_bound.year - 3)
    except ValueError:
        return upper_bound.replace(year=upper_bound.year - 3, day=28)


def canonical_source_cursor_hash(
    *,
    profile_id: int,
    source_kind: DiscoverySourceKind,
    source_key: str,
    completed_upper_bound: datetime,
) -> str:
    return sha256_text(canonical_json({
        "completed_upper_bound": utc_iso(completed_upper_bound),
        "profile_id": profile_id,
        "schema": "youtube-source-cursor.v1",
        "source_key": source_key,
        "source_kind": source_kind.value,
    }))


def canonical_search_window_hash(
    *,
    job_id: int,
    unit_key: str,
    ordinal: int,
    lower_bound: datetime,
    upper_bound: datetime,
    next_page_token: str | None,
    page_count: int,
    split_parent_id: int | None,
    completed_at: datetime | None,
) -> str:
    return sha256_text(canonical_json({
        "completed_at": None if completed_at is None else utc_iso(completed_at),
        "job_id": job_id,
        "lower_bound": utc_iso(lower_bound),
        "next_page_token": next_page_token,
        "ordinal": ordinal,
        "page_count": page_count,
        "schema": "youtube-search-window.v1",
        "split_parent_id": split_parent_id,
        "unit_key": unit_key,
        "upper_bound": utc_iso(upper_bound),
    }))


def youtube_discoverer_set_hash(unit_keys: tuple[str, ...]) -> str:
    return sha256_text(canonical_json({
        "schema": "youtube-sync-discoverer-set.v1",
        "unit_keys": list(unit_keys),
    }))


def youtube_profile_set_hash(
    profiles: tuple[YouTubeSyncManifestProfile, ...],
) -> str:
    return sha256_text(canonical_json({
        "profiles": [
            {
                "config_hash": profile.config_hash,
                "discoverer_set_hash": profile.discoverer_set_hash,
                "ordinal": profile.ordinal,
                "profile_id": profile.profile_id,
                "profile_version_id": profile.profile_version_id,
            }
            for profile in profiles
        ],
        "schema": "youtube-sync-profile-set.v1",
    }))


def youtube_manual_video_hash(youtube_video_id: str) -> str:
    if type(youtube_video_id) is not str or _YOUTUBE_VIDEO_ID.fullmatch(
        youtube_video_id
    ) is None:
        raise DomainError(
            "YOUTUBE_SYNC_MANIFEST_INVALID",
            "manual YouTube video identity is invalid",
        )
    return sha256_text(canonical_json({
        "schema": "youtube-manual-video.v1",
        "youtube_video_id": youtube_video_id,
    }))


def youtube_sync_unit_input_hash(
    *,
    sync_kind: YouTubeSyncKind,
    upper_bound: datetime,
    backfill_floor: datetime,
    quota_contract_version: str,
    profile_id: int,
    profile_version_id: int,
    config_hash: str,
    source_kind: DiscoverySourceKind,
    source_key: str,
    manual_request_id: int | None,
    manual_video_id_hash: str | None,
) -> str:
    return sha256_text(canonical_json({
        "backfill_floor": utc_iso(backfill_floor),
        "config_hash": config_hash,
        "manual_request_id": manual_request_id,
        "manual_video_id_hash": manual_video_id_hash,
        "profile_id": profile_id,
        "profile_version_id": profile_version_id,
        "quota_contract_version": quota_contract_version,
        "schema": "youtube-sync-unit-input.v1",
        "source_key": source_key,
        "source_kind": source_kind.value,
        "sync_kind": sync_kind.value,
        "upper_bound": utc_iso(upper_bound),
    }))


@dataclass(frozen=True, slots=True)
class YouTubeSyncCheckpoint:
    job_id: int
    unit_key: str
    source_kind: DiscoverySourceKind
    source_key: str
    effective_lower_bound: datetime
    upper_bound: datetime
    uploads_playlist_id: str | None
    next_page_token: str | None
    encountered_video_ids: tuple[str, ...]
    unavailable_video_ids: tuple[str, ...]
    page_count: int
    batch_ordinal: int
    completed_at: datetime | None
    checkpoint_hash: str


def canonical_youtube_sync_checkpoint_hash(
    *,
    job_id: int,
    unit_key: str,
    source_kind: DiscoverySourceKind,
    source_key: str,
    effective_lower_bound: datetime,
    upper_bound: datetime,
    uploads_playlist_id: str | None,
    next_page_token: str | None,
    encountered_video_ids: tuple[str, ...],
    unavailable_video_ids: tuple[str, ...],
    page_count: int,
    batch_ordinal: int,
    completed_at: datetime | None,
) -> str:
    return sha256_text(canonical_json({
        "batch_ordinal": batch_ordinal,
        "completed_at": (
            None if completed_at is None else utc_iso(completed_at)
        ),
        "effective_lower_bound": utc_iso(effective_lower_bound),
        "encountered_video_ids": list(encountered_video_ids),
        "job_id": job_id,
        "next_page_token": next_page_token,
        "page_count": page_count,
        "schema": "youtube-sync-checkpoint.v1",
        "source_key": source_key,
        "source_kind": source_kind.value,
        "unit_key": unit_key,
        "unavailable_video_ids": list(unavailable_video_ids),
        "uploads_playlist_id": uploads_playlist_id,
        "upper_bound": utc_iso(upper_bound),
    }))


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
    profiles: tuple[YouTubeSyncManifestProfile, ...]

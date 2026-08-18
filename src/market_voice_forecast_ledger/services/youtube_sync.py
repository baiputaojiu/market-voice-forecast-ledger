import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    DiscoverySourceKind,
    YouTubeSyncKind,
    YouTubeSyncManifest,
    build_youtube_sync_shape,
    youtube_profile_set_hash,
)
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.repositories.discovery import (
    DiscoveryRepository,
)
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.youtube.client import (
    QUOTA_CONTRACT_VERSION,
    YouTubeClient,
)
from market_voice_forecast_ledger.youtube.discovery import SeedUploadsDiscoverer
from market_voice_forecast_ledger.youtube.metadata import normalize_video_item


_COALESCIBLE_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.RETRYING,
    JobStatus.FAILED,
)
_QUEUE_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.PAUSE_REQUESTED,
    JobStatus.CANCEL_REQUESTED,
    JobStatus.RETRYING,
)
_ACTIVE_STATUSES = frozenset(
    {
        JobStatus.RUNNING,
        JobStatus.PAUSE_REQUESTED,
        JobStatus.CANCEL_REQUESTED,
    }
)


@dataclass(frozen=True, slots=True)
class SyncRequestResult:
    job_id: int
    status: JobStatus
    reused: bool


@dataclass(frozen=True, slots=True)
class ClaimedSyncJob:
    job_id: int
    kind: Literal["full", "manual"]
    manifest: YouTubeSyncManifest


@dataclass(frozen=True, slots=True)
class UnitExecutionResult:
    discovered_count: int
    persisted_count: int
    unavailable_count: int
    output_hash: str


class YouTubeSyncService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        youtube_client: YouTubeClient | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs = JobRepository(conn)
        self._discovery = DiscoveryRepository(conn)
        self._job_state = JobStateService(conn, clock=self._clock)
        self._youtube_client = youtube_client

    def request_full_sync(self, requested_at: datetime) -> SyncRequestResult:
        self._require_exact_utc(requested_at)
        with transaction(self._conn):
            profiles = self._discovery.list_active_profile_versions()
            backfill_floor = _three_calendar_year_floor(requested_at)
            candidate_manifest, candidate_profile_hash = self._full_shape(
                profiles=profiles,
                upper_bound=requested_at,
                backfill_floor=backfill_floor,
            )
            for job_id in self._jobs.list_youtube_sync_job_ids(
                _COALESCIBLE_STATUSES,
                newest_first=True,
            ):
                stored = self.get_sync_manifest(job_id)
                status = self._jobs.get(job_id).status
                if not self._is_compatible_full(
                    stored,
                    profile_set_hash=candidate_profile_hash,
                ):
                    continue
                if status is JobStatus.FAILED:
                    artifacts = (
                        self._discovery.verified_youtube_artifact_hashes(job_id)
                    )
                    self._job_state.retry_failed_in_transaction(
                        job_id, artifacts
                    )
                    status = JobStatus.RETRYING
                return SyncRequestResult(job_id, status, True)

            job_id = self._job_state.create_in_transaction(
                candidate_manifest,
                created_at=requested_at,
            )
            self._discovery.create_youtube_sync_manifest(
                job_id=job_id,
                sync_kind=YouTubeSyncKind.FULL_DISCOVERY.value,
                upper_bound=requested_at,
                backfill_floor=backfill_floor,
                quota_contract_version=QUOTA_CONTRACT_VERSION,
                profiles=profiles,
                manual_request_id=None,
                created_at=requested_at,
            )
            return SyncRequestResult(job_id, JobStatus.QUEUED, False)

    def request_manual_sync(
        self, manual_request_id: int, requested_at: datetime
    ) -> SyncRequestResult:
        self._require_exact_utc(requested_at)
        if type(manual_request_id) is not int or manual_request_id <= 0:
            raise DomainError(
                "YOUTUBE_SYNC_REQUEST_INVALID",
                "YouTube sync request is invalid",
            )
        with transaction(self._conn):
            existing_id = self._discovery.find_manual_sync_job_id(
                manual_request_id
            )
            if existing_id is not None:
                manifest = self.get_sync_manifest(existing_id)
                if (
                    manifest.sync_kind != YouTubeSyncKind.MANUAL.value
                    or manifest.manual_request_id != manual_request_id
                ):
                    raise DomainError(
                        "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                        "stored manual sync manifest is invalid",
                    )
                return SyncRequestResult(
                    existing_id, self._jobs.get(existing_id).status, True
                )

            profile, manual_video_id = (
                self._discovery.manual_sync_request_binding(manual_request_id)
            )
            profiles = (profile,)
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=YouTubeSyncKind.MANUAL,
                profiles=profiles,
                upper_bound=requested_at,
                backfill_floor=requested_at,
                quota_contract_version=QUOTA_CONTRACT_VERSION,
                manual_request_id=manual_request_id,
                manual_video_id=manual_video_id,
            )
            generic = _generic_manifest(unit_specs)
            job_id = self._job_state.create_in_transaction(
                generic,
                created_at=requested_at,
            )
            self._discovery.create_youtube_sync_manifest(
                job_id=job_id,
                sync_kind=YouTubeSyncKind.MANUAL.value,
                upper_bound=requested_at,
                backfill_floor=requested_at,
                quota_contract_version=QUOTA_CONTRACT_VERSION,
                profiles=profiles,
                manual_request_id=manual_request_id,
                created_at=requested_at,
            )
            return SyncRequestResult(job_id, JobStatus.QUEUED, False)

    def get_sync_manifest(self, job_id: int) -> YouTubeSyncManifest:
        generic = self._job_state.stored_manifest(job_id)
        if generic.kind is not JobKind.YOUTUBE_SYNC:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync job is invalid",
            )
        specific = self._discovery.get_youtube_sync_manifest(job_id)
        if (
            specific.manifest_hash != generic.manifest_hash
            or specific.quota_contract_version != QUOTA_CONTRACT_VERSION
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            )
        return specific

    def claim_next_runnable(
        self, now: datetime
    ) -> ClaimedSyncJob | None:
        self._require_exact_utc(now)
        with transaction(self._conn):
            validated: list[
                tuple[int, JobStatus, YouTubeSyncManifest]
            ] = []
            for job_id in self._jobs.list_youtube_sync_job_ids(
                _QUEUE_STATUSES,
                newest_first=False,
            ):
                manifest = self.get_sync_manifest(job_id)
                validated.append((job_id, self._jobs.get(job_id).status, manifest))

            active = tuple(
                item for item in validated if item[1] in _ACTIVE_STATUSES
            )
            if len(active) > 1:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_QUEUE_INVALID",
                    "stored YouTube sync queue has multiple active jobs",
                )
            if active:
                job_id, status, manifest = active[0]
                if status is not JobStatus.RUNNING:
                    return None
                return self._claim_pending(job_id, manifest, now)

            for job_id, status, manifest in validated:
                if (
                    status is JobStatus.RETRYING
                    and manifest.resume_not_before_utc is not None
                ):
                    if manifest.resume_not_before_utc > now:
                        continue
                    self._discovery.set_youtube_resume_not_before(job_id, None)
                    manifest = self.get_sync_manifest(job_id)
                return self._claim_pending(job_id, manifest, now)
            return None

    def execute_seed_unit(
        self, job_id: int, unit_key: str
    ) -> UnitExecutionResult:
        manifest = self.get_sync_manifest(job_id)
        profile_version, spec = self._bound_seed_unit(
            manifest=manifest,
            unit_key=unit_key,
        )
        unit = self._job_state.unit(job_id, unit_key)
        if (
            unit.stage is not JobStage.YOUTUBE_SEED_DISCOVERY
            or unit.declared_input_hash != spec.declared_input_hash
            or unit.execution_contract_hash != spec.execution_contract_hash
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube seed unit is invalid",
            )
        if unit.status is UnitStatus.SUCCESS:
            output_hash, observation_ids = self._discovery.seed_unit_artifact(
                job_id=job_id,
                unit_key=unit_key,
                profile_version_id=profile_version.id,
                profile_id=profile_version.profile_id,
                source_key=spec.source_key,
            )
            if unit.output_hash != output_hash:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                    "stored YouTube sync artifact is invalid",
                )
            return UnitExecutionResult(
                len(observation_ids), len(observation_ids), 0, output_hash
            )
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "YOUTUBE_SEED_UNIT_NOT_RUNNING",
                "YouTube seed unit must be claimed before execution",
            )
        if self._youtube_client is None:
            raise DomainError(
                "YOUTUBE_SYNC_DEPENDENCY_MISSING",
                "YouTube sync dependency is not configured",
            )

        discoverer = SeedUploadsDiscoverer(self._youtube_client)
        checkpoint = self._discovery.get_youtube_sync_checkpoint(
            job_id, unit_key
        )
        if checkpoint.uploads_playlist_id is None:
            uploads_playlist_id = discoverer.resolve_uploads_playlist(
                spec.source_key
            )
            with transaction(self._conn):
                checkpoint = self._discovery.bind_seed_uploads_playlist(
                    job_id=job_id,
                    unit_key=unit_key,
                    uploads_playlist_id=uploads_playlist_id,
                )
        else:
            uploads_playlist_id = checkpoint.uploads_playlist_id

        seen_video_ids = set(
            self._discovery.seed_unit_seen_video_ids(
                job_id=job_id,
                unit_key=unit_key,
                profile_version_id=profile_version.id,
                profile_id=profile_version.profile_id,
                source_key=spec.source_key,
            )
        )
        encountered_video_ids = set(seen_video_ids)
        unavailable_video_ids: set[str] = set()

        terminal_page_committed = (
            checkpoint.page_count > 0
            and checkpoint.next_page_token is None
        )
        while not terminal_page_committed:
            page = discoverer.page_video_ids(
                uploads_playlist_id,
                checkpoint.next_page_token,
            )
            new_video_ids: list[str] = []
            for video_id in page.video_ids:
                if video_id in encountered_video_ids:
                    continue
                encountered_video_ids.add(video_id)
                new_video_ids.append(video_id)

            normalized_by_id = {}
            for offset in range(0, len(new_video_ids), 50):
                requested_ids = tuple(new_video_ids[offset : offset + 50])
                raw_items = self._youtube_client.videos(requested_ids)
                normalized, unavailable = self._normalize_seed_response(
                    requested_ids, raw_items
                )
                for item in normalized:
                    normalized_by_id[item.youtube_video_id] = item
                unavailable_video_ids.update(unavailable)

            canonical_items = tuple(
                normalized_by_id[video_id]
                for video_id in new_video_ids
                if video_id in normalized_by_id
            )
            in_window_items = tuple(
                item
                for item in canonical_items
                if checkpoint.effective_lower_bound
                <= item.published_at
                < checkpoint.upper_bound
            )
            page_wholly_older = bool(canonical_items) and all(
                item.published_at < checkpoint.effective_lower_bound
                for item in canonical_items
            )
            committed_next_token = (
                None if page_wholly_older else page.next_page_token
            )
            with transaction(self._conn):
                self._discovery.persist_metadata_batch(
                    job_id,
                    profile_version.id,
                    DiscoverySourceKind.SEED_UPLOADS,
                    spec.source_key,
                    in_window_items,
                    self._exact_clock_value(),
                )
                checkpoint = self._discovery.advance_seed_checkpoint(
                    job_id=job_id,
                    unit_key=unit_key,
                    next_page_token=committed_next_token,
                )
            seen_video_ids.update(
                item.youtube_video_id for item in in_window_items
            )
            terminal_page_committed = committed_next_token is None

        with transaction(self._conn):
            output_hash, observation_ids = self._discovery.seed_unit_artifact(
                job_id=job_id,
                unit_key=unit_key,
                profile_version_id=profile_version.id,
                profile_id=profile_version.profile_id,
                source_key=spec.source_key,
            )
            self._discovery.record_seed_proposed_cursor(
                job_id=job_id,
                profile_id=profile_version.profile_id,
                source_key=spec.source_key,
                completed_upper_bound=manifest.upper_bound,
            )
            self._discovery.complete_seed_checkpoint(
                job_id=job_id,
                unit_key=unit_key,
                completed_at=self._exact_clock_value(),
            )
            self._job_state.complete_unit_in_transaction(
                job_id, unit_key, output_hash
            )
        return UnitExecutionResult(
            discovered_count=len(encountered_video_ids),
            persisted_count=len(observation_ids),
            unavailable_count=len(unavailable_video_ids),
            output_hash=output_hash,
        )

    def _claim_pending(
        self,
        job_id: int,
        manifest: YouTubeSyncManifest,
        now: datetime,
    ) -> ClaimedSyncJob | None:
        units = self._jobs.list_units(job_id)
        if any(unit.status is UnitStatus.RUNNING for unit in units):
            return None
        pending = tuple(unit for unit in units if unit.status is UnitStatus.PENDING)
        if not pending:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_QUEUE_INVALID",
                "runnable YouTube sync job has no pending unit",
            )
        self._job_state.begin_unit_in_transaction(
            job_id,
            pending[0].unit_key,
            started_at=now,
        )
        kind: Literal["full", "manual"] = (
            "full"
            if manifest.sync_kind == YouTubeSyncKind.FULL_DISCOVERY.value
            else "manual"
        )
        return ClaimedSyncJob(job_id, kind, manifest)

    def _bound_seed_unit(self, *, manifest, unit_key):
        if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
            raise DomainError(
                "YOUTUBE_SEED_UNIT_INVALID",
                "YouTube seed execution requires a full sync manifest",
            )
        try:
            profiles = tuple(
                self._discovery.get_profile_version(item.profile_version_id)
                for item in manifest.profiles
            )
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=YouTubeSyncKind.FULL_DISCOVERY,
                profiles=profiles,
                upper_bound=manifest.upper_bound,
                backfill_floor=manifest.backfill_floor,
                quota_contract_version=manifest.quota_contract_version,
                manual_request_id=None,
                manual_video_id=None,
            )
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            ) from cause
        matches = tuple(
            spec
            for spec in unit_specs
            if spec.unit_key == unit_key
            and spec.stage is JobStage.YOUTUBE_SEED_DISCOVERY
        )
        if len(matches) != 1:
            raise DomainError(
                "YOUTUBE_SEED_UNIT_INVALID",
                "YouTube seed unit is not in the sealed manifest",
            )
        spec = matches[0]
        profile_matches = tuple(
            profile for profile in profiles if profile.id == spec.profile_version_id
        )
        if len(profile_matches) != 1:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube seed profile binding is invalid",
            )
        return profile_matches[0], spec

    def _normalize_seed_response(self, requested_ids, raw_items):
        if type(raw_items) is not tuple or len(raw_items) > len(requested_ids):
            self._raise_seed_response_invalid()
        requested = set(requested_ids)
        by_id = {}
        fetched_at = self._exact_clock_value()
        unavailable: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                self._raise_seed_response_invalid()
            video_id = raw_item.get("id")
            if (
                type(video_id) is not str
                or video_id not in requested
                or video_id in by_id
            ):
                self._raise_seed_response_invalid()
            try:
                by_id[video_id] = normalize_video_item(
                    raw_item, fetched_at=fetched_at
                )
            except DomainError as cause:
                if cause.code != "YOUTUBE_VIDEO_UNAVAILABLE":
                    raise
                unavailable.add(video_id)
        unavailable.update(requested - set(by_id) - unavailable)
        normalized = tuple(
            by_id[video_id] for video_id in requested_ids if video_id in by_id
        )
        return normalized, frozenset(unavailable)

    def _exact_clock_value(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is not timezone.utc:
            raise DomainError(
                "YOUTUBE_SYNC_CLOCK_INVALID",
                "YouTube sync clock requires an exact UTC datetime",
            )
        return value

    @staticmethod
    def _raise_seed_response_invalid() -> None:
        raise DomainError(
            "YOUTUBE_SEED_RESPONSE_INVALID",
            "YouTube seed metadata response is invalid",
        )

    @staticmethod
    def _require_exact_utc(value: object) -> None:
        if type(value) is not datetime or value.tzinfo is not timezone.utc:
            raise DomainError(
                "YOUTUBE_SYNC_REQUEST_INVALID",
                "YouTube sync request requires an exact UTC datetime",
            )

    @staticmethod
    def _is_compatible_full(
        stored: YouTubeSyncManifest,
        *,
        profile_set_hash: str,
    ) -> bool:
        return (
            stored.sync_kind == YouTubeSyncKind.FULL_DISCOVERY.value
            and stored.manual_request_id is None
            and stored.profile_set_hash == profile_set_hash
            and stored.quota_contract_version == QUOTA_CONTRACT_VERSION
        )

    @staticmethod
    def _full_shape(
        *,
        profiles: tuple[DiscoveryProfileVersion, ...],
        upper_bound: datetime,
        backfill_floor: datetime,
    ) -> tuple[JobManifest, str]:
        manifest_profiles, unit_specs = build_youtube_sync_shape(
            sync_kind=YouTubeSyncKind.FULL_DISCOVERY,
            profiles=profiles,
            upper_bound=upper_bound,
            backfill_floor=backfill_floor,
            quota_contract_version=QUOTA_CONTRACT_VERSION,
            manual_request_id=None,
            manual_video_id=None,
        )
        return _generic_manifest(unit_specs), youtube_profile_set_hash(
            manifest_profiles
        )


def _generic_manifest(unit_specs) -> JobManifest:
    return JobManifest.build(
        JobKind.YOUTUBE_SYNC,
        tuple(
            ManifestUnit(
                spec.unit_key,
                spec.stage,
                spec.ordinal,
                spec.declared_input_hash,
                (),
                spec.execution_contract_hash,
            )
            for spec in unit_specs
        ),
    )


def _three_calendar_year_floor(upper_bound: datetime) -> datetime:
    try:
        return upper_bound.replace(year=upper_bound.year - 3)
    except ValueError:
        return upper_bound.replace(year=upper_bound.year - 3, day=28)

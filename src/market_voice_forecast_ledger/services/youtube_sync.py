import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    DiscoverySourceKind,
    YouTubeSyncKind,
    YouTubeSyncManifest,
    build_youtube_sync_shape,
    initial_backfill_floor,
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
    YouTubeProviderFailure,
)
from market_voice_forecast_ledger.youtube.discovery import (
    CrossChannelSearchDiscoverer,
    ManualUrlDiscoverer,
    SeedUploadsDiscoverer,
    extract_youtube_video_id,
)
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
class ManualRequestResult:
    request_id: int
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
        failpoint: object | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs = JobRepository(conn)
        self._discovery = DiscoveryRepository(conn)
        self._job_state = JobStateService(conn, clock=self._clock)
        self._youtube_client = youtube_client
        self._failpoint = failpoint

    def request_full_sync(self, requested_at: datetime) -> SyncRequestResult:
        self._require_exact_utc(requested_at)
        with transaction(self._conn):
            profiles = self._discovery.list_active_profile_versions()
            backfill_floor = initial_backfill_floor(requested_at)
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
            return self._request_manual_sync_in_transaction(
                manual_request_id, requested_at
            )

    def request_manual_candidate(
        self, subject_id: int, url: str, requested_at: datetime
    ) -> ManualRequestResult:
        self._require_exact_utc(requested_at)
        youtube_video_id = extract_youtube_video_id(url)
        with transaction(self._conn):
            profile = self._discovery.get_active_manual_profile_version(
                subject_id
            )
            request_id = self._discovery.find_manual_discovery_request_id(
                profile_id=profile.profile_id,
                youtube_video_id=youtube_video_id,
            )
            request_reused = request_id is not None
            if request_id is None:
                request_id = (
                    self._discovery.create_manual_discovery_request(
                        profile_id=profile.profile_id,
                        youtube_video_id=youtube_video_id,
                        requested_at=requested_at,
                    )
                )
            requested = self._request_manual_sync_in_transaction(
                request_id, requested_at
            )
            status = requested.status
            if status is JobStatus.FAILED:
                artifacts = self._discovery.verified_youtube_artifact_hashes(
                    requested.job_id
                )
                self._job_state.retry_failed_in_transaction(
                    requested.job_id, artifacts
                )
                status = JobStatus.RETRYING
            return ManualRequestResult(
                request_id=request_id,
                job_id=requested.job_id,
                status=status,
                reused=request_reused or requested.reused,
            )

    def _request_manual_sync_in_transaction(
        self, manual_request_id: int, requested_at: datetime
    ) -> SyncRequestResult:
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
            checkpoint = self._discovery.get_youtube_sync_checkpoint(
                job_id, unit_key
            )
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
                len(checkpoint.encountered_video_ids),
                len(observation_ids),
                len(checkpoint.unavailable_video_ids),
                output_hash,
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
        resolved_uploads_playlist_id = discoverer.resolve_uploads_playlist(
            spec.source_key
        )
        if checkpoint.uploads_playlist_id is None:
            with transaction(self._conn):
                checkpoint = self._discovery.bind_seed_uploads_playlist(
                    job_id=job_id,
                    unit_key=unit_key,
                    source_key=spec.source_key,
                    uploads_playlist_id=resolved_uploads_playlist_id,
                )
        elif checkpoint.uploads_playlist_id != resolved_uploads_playlist_id:
            raise DomainError(
                "YOUTUBE_SEED_CHECKPOINT_INVALID",
                "YouTube seed checkpoint is invalid",
            )
        uploads_playlist_id = resolved_uploads_playlist_id

        encountered_video_ids = set(checkpoint.encountered_video_ids)
        unavailable_video_ids = set(checkpoint.unavailable_video_ids)

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
            page_wholly_older = (
                len(canonical_items) == len(page.video_ids)
                and bool(canonical_items)
                and all(
                    item.published_at < checkpoint.effective_lower_bound
                    for item in canonical_items
                )
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
                    encountered_video_ids=tuple(sorted(encountered_video_ids)),
                    unavailable_video_ids=tuple(
                        sorted(unavailable_video_ids)
                    ),
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

    def execute_manual_unit(
        self, job_id: int, unit_key: str
    ) -> UnitExecutionResult:
        manifest = self.get_sync_manifest(job_id)
        profile_version, spec, youtube_video_id = self._bound_manual_unit(
            manifest=manifest,
            unit_key=unit_key,
        )
        unit = self._job_state.unit(job_id, unit_key)
        if (
            unit.stage is not JobStage.YOUTUBE_MANUAL_DISCOVERY
            or unit.declared_input_hash != spec.declared_input_hash
            or unit.execution_contract_hash != spec.execution_contract_hash
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube manual unit is invalid",
            )
        if unit.status is UnitStatus.SUCCESS:
            checkpoint = self._discovery.get_youtube_sync_checkpoint(
                job_id, unit_key
            )
            output_hash, observation_ids = (
                self._discovery.manual_unit_artifact(
                    job_id=job_id,
                    unit_key=unit_key,
                    manual_request_id=manifest.manual_request_id,
                    profile_version_id=profile_version.id,
                    profile_id=profile_version.profile_id,
                    source_key=spec.source_key,
                )
            )
            if unit.output_hash != output_hash:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                    "stored YouTube sync artifact is invalid",
                )
            return UnitExecutionResult(
                discovered_count=len(checkpoint.encountered_video_ids),
                persisted_count=len(observation_ids),
                unavailable_count=len(checkpoint.unavailable_video_ids),
                output_hash=output_hash,
            )
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "YOUTUBE_MANUAL_UNIT_NOT_RUNNING",
                "YouTube manual unit must be claimed before execution",
            )
        if self._youtube_client is None:
            raise DomainError(
                "YOUTUBE_SYNC_DEPENDENCY_MISSING",
                "YouTube sync dependency is not configured",
            )

        completed_at = self._exact_clock_value()
        items = ManualUrlDiscoverer(
            self._youtube_client, clock=lambda: completed_at
        ).fetch(youtube_video_id)
        unavailable = not items
        with transaction(self._conn):
            self._discovery.persist_metadata_batch(
                job_id,
                profile_version.id,
                DiscoverySourceKind.MANUAL_URL,
                spec.source_key,
                items,
                completed_at,
            )
            _, output_hash, observation_ids = (
                self._discovery.complete_manual_checkpoint_and_artifact(
                    job_id=job_id,
                    unit_key=unit_key,
                    manual_request_id=manifest.manual_request_id,
                    profile_version_id=profile_version.id,
                    youtube_video_id=youtube_video_id,
                    unavailable=unavailable,
                    completed_at=completed_at,
                )
            )
            self._job_state.complete_unit_in_transaction(
                job_id, unit_key, output_hash
            )
            self._job_state.succeed_job_in_transaction(job_id)
        return UnitExecutionResult(
            discovered_count=1,
            persisted_count=len(observation_ids),
            unavailable_count=int(unavailable),
            output_hash=output_hash,
        )

    def execute_search_unit(
        self, job_id: int, unit_key: str
    ) -> UnitExecutionResult:
        manifest = self.get_sync_manifest(job_id)
        profile_version, spec = self._bound_search_unit(
            manifest=manifest,
            unit_key=unit_key,
        )
        unit = self._job_state.unit(job_id, unit_key)
        if (
            unit.stage is not JobStage.YOUTUBE_SEARCH_DISCOVERY
            or unit.declared_input_hash != spec.declared_input_hash
            or unit.execution_contract_hash != spec.execution_contract_hash
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube search unit is invalid",
            )
        if unit.status is UnitStatus.SUCCESS:
            checkpoint = self._discovery.get_youtube_sync_checkpoint(
                job_id, unit_key
            )
            output_hash, observation_ids = (
                self._discovery.search_unit_artifact(
                    job_id=job_id,
                    unit_key=unit_key,
                    profile_version_id=profile_version.id,
                    profile_id=profile_version.profile_id,
                    source_key=spec.source_key,
                )
            )
            if unit.output_hash != output_hash:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                    "stored YouTube sync artifact is invalid",
                )
            return UnitExecutionResult(
                len(checkpoint.encountered_video_ids),
                len(observation_ids),
                len(checkpoint.unavailable_video_ids),
                output_hash,
            )
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "YOUTUBE_SEARCH_UNIT_NOT_RUNNING",
                "YouTube search unit must be claimed before execution",
            )
        if self._youtube_client is None:
            raise DomainError(
                "YOUTUBE_SYNC_DEPENDENCY_MISSING",
                "YouTube sync dependency is not configured",
            )

        discoverer = CrossChannelSearchDiscoverer(self._youtube_client)
        checkpoint = self._discovery.get_youtube_sync_checkpoint(
            job_id, unit_key
        )
        encountered_video_ids = set(checkpoint.encountered_video_ids)
        unavailable_video_ids = set(checkpoint.unavailable_video_ids)
        persisted_video_ids = set(
            self._discovery.search_unit_seen_video_ids(
                job_id=job_id,
                unit_key=unit_key,
                profile_version_id=profile_version.id,
                profile_id=profile_version.profile_id,
                source_key=spec.source_key,
            )
        )

        while True:
            window = self._discovery.next_search_window(job_id, unit_key)
            if window is None:
                break
            if window.page_count == 10 and window.next_page_token is not None:
                boundary = self._search_split_boundary(window)
                if boundary is None:
                    self._job_state.fail_unit(
                        job_id,
                        unit_key,
                        "YOUTUBE_SEARCH_WINDOW_SATURATED",
                    )
                    raise DomainError(
                        "YOUTUBE_SEARCH_WINDOW_SATURATED",
                        "YouTube search window cannot be split safely",
                    )
                with transaction(self._conn):
                    self._discovery.split_search_window(
                        job_id=job_id,
                        unit_key=unit_key,
                        window_id=window.id,
                        boundary=boundary,
                        completed_at=self._exact_clock_value(),
                    )
                continue
            provider_window = replace(
                window,
                lower_bound=window.lower_bound - timedelta(seconds=1),
            )
            try:
                page = discoverer.page_video_ids(
                    profile_version,
                    provider_window,
                    window.next_page_token,
                )
            except YouTubeProviderFailure as cause:
                if cause.category != "invalid_page_token":
                    raise
                with transaction(self._conn):
                    self._discovery.restart_search_window(
                        job_id=job_id,
                        unit_key=unit_key,
                        window_id=window.id,
                    )
                continue

            new_video_ids: list[str] = []
            encountered_video_ids.update(page.video_ids)
            for video_id in page.video_ids:
                if (
                    video_id in persisted_video_ids
                    or video_id in unavailable_video_ids
                    or video_id in new_video_ids
                ):
                    continue
                new_video_ids.append(video_id)

            normalized_by_id = {}
            for offset in range(0, len(new_video_ids), 50):
                requested_ids = tuple(new_video_ids[offset : offset + 50])
                raw_items = self._youtube_client.videos(requested_ids)
                normalized, unavailable = self._normalize_video_response(
                    requested_ids,
                    raw_items,
                    error_code="YOUTUBE_SEARCH_RESPONSE_INVALID",
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
                if window.lower_bound
                <= item.published_at
                < window.upper_bound
            )
            saturated = False
            with transaction(self._conn):
                self._discovery.persist_metadata_batch(
                    job_id,
                    profile_version.id,
                    DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                    spec.source_key,
                    in_window_items,
                    self._exact_clock_value(),
                )
                checkpoint, committed_window = (
                    self._discovery.advance_search_window_page(
                        job_id=job_id,
                        unit_key=unit_key,
                        window_id=window.id,
                        next_page_token=page.next_page_token,
                        encountered_video_ids=tuple(
                            sorted(encountered_video_ids)
                        ),
                        unavailable_video_ids=tuple(
                            sorted(unavailable_video_ids)
                        ),
                    )
                )
                persisted_video_ids.update(
                    item.youtube_video_id for item in in_window_items
                )
                if page.next_page_token is None:
                    self._discovery.complete_search_window(
                        job_id=job_id,
                        unit_key=unit_key,
                        window_id=window.id,
                        completed_at=self._exact_clock_value(),
                    )
                elif committed_window.page_count == 10:
                    boundary = self._search_split_boundary(committed_window)
                    if boundary is None:
                        saturated = True
                    else:
                        self._discovery.split_search_window(
                            job_id=job_id,
                            unit_key=unit_key,
                            window_id=window.id,
                            boundary=boundary,
                            completed_at=self._exact_clock_value(),
                        )
            if saturated:
                self._job_state.fail_unit(
                    job_id,
                    unit_key,
                    "YOUTUBE_SEARCH_WINDOW_SATURATED",
                )
                raise DomainError(
                    "YOUTUBE_SEARCH_WINDOW_SATURATED",
                    "YouTube search window cannot be split safely",
                )

        with transaction(self._conn):
            output_hash, observation_ids = (
                self._discovery.search_unit_artifact(
                    job_id=job_id,
                    unit_key=unit_key,
                    profile_version_id=profile_version.id,
                    profile_id=profile_version.profile_id,
                    source_key=spec.source_key,
                )
            )
            self._discovery.record_source_proposed_cursor(
                job_id=job_id,
                profile_id=profile_version.profile_id,
                source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                source_key=spec.source_key,
                completed_upper_bound=manifest.upper_bound,
            )
            self._discovery.complete_search_checkpoint(
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

    def finalize_full_job(self, job_id: int) -> None:
        with transaction(self._conn):
            manifest = self.get_sync_manifest(job_id)
            if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
                raise DomainError(
                    "YOUTUBE_CURSOR_PROMOTION_INVALID",
                    "YouTube cursor promotion requires a full sync job",
                )
            if any(
                unit.status is not UnitStatus.SUCCESS
                for unit in self._jobs.list_units(job_id)
            ):
                raise DomainError(
                    "ALL_UNITS_MUST_SUCCEED",
                    "every manifest unit must succeed before the job",
                )
            self._discovery.verified_youtube_artifact_hashes(job_id)
            self._discovery.promote_full_job_cursors(
                job_id=job_id,
                updated_at=self._exact_clock_value(),
            )
            if self._failpoint is not None:
                callback = getattr(self._failpoint, "after_cursor_update", None)
                if not callable(callback):
                    raise DomainError(
                        "YOUTUBE_CURSOR_PROMOTION_INVALID",
                        "YouTube cursor promotion failpoint is invalid",
                    )
                callback()
            self._job_state.succeed_job_in_transaction(job_id)

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

    def _bound_search_unit(self, *, manifest, unit_key):
        if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
            raise DomainError(
                "YOUTUBE_SEARCH_UNIT_INVALID",
                "YouTube search execution requires a full sync manifest",
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
            and spec.stage is JobStage.YOUTUBE_SEARCH_DISCOVERY
        )
        if len(matches) != 1:
            raise DomainError(
                "YOUTUBE_SEARCH_UNIT_INVALID",
                "YouTube search unit is not in the sealed manifest",
            )
        spec = matches[0]
        profile_matches = tuple(
            profile for profile in profiles if profile.id == spec.profile_version_id
        )
        if len(profile_matches) != 1:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube search profile binding is invalid",
            )
        return profile_matches[0], spec

    def _bound_manual_unit(self, *, manifest, unit_key):
        if (
            manifest.sync_kind != YouTubeSyncKind.MANUAL.value
            or type(manifest.manual_request_id) is not int
            or manifest.manual_request_id <= 0
            or len(manifest.profiles) != 1
        ):
            raise DomainError(
                "YOUTUBE_MANUAL_UNIT_INVALID",
                "YouTube manual execution requires a manual sync manifest",
            )
        try:
            stored_profile = manifest.profiles[0]
            profile = self._discovery.get_profile_version(
                stored_profile.profile_version_id
            )
            request_profile_id, youtube_video_id = (
                self._discovery.manual_request_binding(
                    manifest.manual_request_id
                )
            )
            if (
                profile.profile_id != request_profile_id
                or stored_profile.profile_id != request_profile_id
            ):
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                    "stored manual sync profile binding is invalid",
                )
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=YouTubeSyncKind.MANUAL,
                profiles=(profile,),
                upper_bound=manifest.upper_bound,
                backfill_floor=manifest.backfill_floor,
                quota_contract_version=manifest.quota_contract_version,
                manual_request_id=manifest.manual_request_id,
                manual_video_id=youtube_video_id,
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
            and spec.stage is JobStage.YOUTUBE_MANUAL_DISCOVERY
        )
        if len(matches) != 1:
            raise DomainError(
                "YOUTUBE_MANUAL_UNIT_INVALID",
                "YouTube manual unit is not in the sealed manifest",
            )
        return profile, matches[0], youtube_video_id

    def _normalize_seed_response(self, requested_ids, raw_items):
        return self._normalize_video_response(
            requested_ids,
            raw_items,
            error_code="YOUTUBE_SEED_RESPONSE_INVALID",
        )

    def _normalize_video_response(
        self, requested_ids, raw_items, *, error_code: str
    ):
        if type(raw_items) is not tuple or len(raw_items) > len(requested_ids):
            self._raise_video_response_invalid(error_code)
        requested = set(requested_ids)
        by_id = {}
        fetched_at = self._exact_clock_value()
        unavailable: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                self._raise_video_response_invalid(error_code)
            video_id = raw_item.get("id")
            if (
                type(video_id) is not str
                or video_id not in requested
                or video_id in by_id
            ):
                self._raise_video_response_invalid(error_code)
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

    @staticmethod
    def _search_split_boundary(window) -> datetime | None:
        if window.upper_bound - window.lower_bound <= timedelta(days=1):
            return None
        midpoint = window.lower_bound + (
            window.upper_bound - window.lower_bound
        ) / 2
        midnight_before = midpoint.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Stable order prefers the midnight at-or-before the midpoint, then
        # the immediately following midnight for an asymmetric lower edge.
        for boundary in (
            midnight_before,
            midnight_before + timedelta(days=1),
        ):
            if (
                boundary - window.lower_bound >= timedelta(days=1)
                and window.upper_bound - boundary >= timedelta(days=1)
            ):
                return boundary
        return None

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
        YouTubeSyncService._raise_video_response_invalid(
            "YOUTUBE_SEED_RESPONSE_INVALID"
        )

    @staticmethod
    def _raise_video_response_invalid(error_code: str) -> None:
        raise DomainError(error_code, "YouTube metadata response is invalid")

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

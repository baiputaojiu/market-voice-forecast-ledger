import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    YouTubeSyncKind,
    YouTubeSyncManifest,
    build_youtube_sync_shape,
    youtube_profile_set_hash,
)
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
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
from market_voice_forecast_ledger.youtube.client import QUOTA_CONTRACT_VERSION


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


class YouTubeSyncService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._jobs = JobRepository(conn)
        self._discovery = DiscoveryRepository(conn)
        self._job_state = JobStateService(conn, clock=self._clock)

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
                    and manifest.resume_not_before_utc > now
                ):
                    continue
                return self._claim_pending(job_id, manifest, now)
            return None

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

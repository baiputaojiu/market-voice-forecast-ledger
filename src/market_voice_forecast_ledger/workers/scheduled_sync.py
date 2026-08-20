from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.credentials import CredentialStore
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.windows.task_scheduler import (
    ScheduledTaskStatus,
    TaskScheduleReader,
)
from market_voice_forecast_ledger.youtube.client import YouTubeTransport


@dataclass(frozen=True, slots=True)
class WorkerSummary:
    claimed_jobs: int
    completed_jobs: int
    deferred_jobs: int
    failed_jobs: int


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    credential_store: CredentialStore
    transport: YouTubeTransport
    schedule_reader: TaskScheduleReader
    clock: Callable[[], datetime]
    sleeper: Callable[[float], None]

    @classmethod
    def production(cls, settings: Settings) -> "WorkerDependencies":
        if type(settings) is not Settings:
            raise DomainError(
                "YOUTUBE_SYNC_SETTINGS_INVALID",
                "YouTube sync settings are invalid",
            )
        from market_voice_forecast_ledger.credentials.windows import (
            WindowsCredentialManager,
        )
        from market_voice_forecast_ledger.windows.task_scheduler import (
            TaskSchedulerAdapter,
        )
        from market_voice_forecast_ledger.youtube.client import (
            UrllibYouTubeTransport,
        )

        return cls(
            credential_store=WindowsCredentialManager(),
            transport=UrllibYouTubeTransport(),
            schedule_reader=TaskSchedulerAdapter(),
            clock=_utc_now,
            sleeper=time.sleep,
        )


def run_once(
    settings: Settings,
    dependencies: WorkerDependencies | None = None,
) -> WorkerSummary:
    deps = dependencies or WorkerDependencies.production(settings)
    conn = open_database(settings.database_path)
    try:
        apply_migrations(conn)
        bootstrap_reference_data(conn)
        service = YouTubeSyncService.from_dependencies(conn, deps)
        try:
            schedule = deps.schedule_reader.status()
        except DomainError as cause:
            if cause.code != "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE":
                raise
            schedule = ScheduledTaskStatus.unavailable()
        now = deps.clock()
        if schedule.is_due(now):
            jst_day = schedule.jst_day(now)
            if not service.has_daily_request(jst_day):
                service.ensure_daily_full_request(jst_day)

        service.resume_failed_jobs_for_wake()
        claimed_ids: set[int] = set()
        completed_ids: set[int] = set()
        deferred_ids: set[int] = set()
        failed_ids: set[int] = set()
        while True:
            claimed = service.claim_next_runnable(deps.clock())
            if claimed is None:
                break
            claimed_ids.add(claimed.job_id)
            status = service.execute_claimed_job(claimed)
            if status is JobStatus.SUCCEEDED:
                completed_ids.add(claimed.job_id)
            elif status is JobStatus.RETRYING:
                deferred_ids.add(claimed.job_id)
                break
            elif status is JobStatus.FAILED:
                failed_ids.add(claimed.job_id)
        return WorkerSummary(
            claimed_jobs=len(claimed_ids),
            completed_jobs=len(completed_ids),
            deferred_jobs=len(deferred_ids),
            failed_jobs=len(failed_ids),
        )
    finally:
        conn.close()


def _utc_now() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)

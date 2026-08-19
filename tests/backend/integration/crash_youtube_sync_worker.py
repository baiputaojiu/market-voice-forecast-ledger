from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not timezone.utc:
        raise ValueError("UTC timestamp required")
    return parsed


def _crash_after_checkpoint(database_path: Path, job_id: int, now: datetime) -> None:
    conn = open_database(database_path)
    service = YouTubeSyncService(conn, clock=lambda: now)
    claimed = service.claim_next_runnable(now)
    if claimed is None or claimed.job_id != job_id:
        raise RuntimeError("expected crash job was not claimed")
    running = tuple(
        conn.execute(
            "SELECT unit_key, stage FROM job_units "
            "WHERE job_id=? AND status='running'",
            (job_id,),
        )
    )
    if len(running) != 1 or running[0]["stage"] != "youtube_search_discovery":
        raise RuntimeError("expected one running search unit")
    unit_key = running[0]["unit_key"]
    repository = DiscoveryRepository(conn)
    window = repository.next_search_window(job_id, unit_key)
    if window is None:
        raise RuntimeError("expected a search checkpoint window")
    with transaction(conn):
        repository.advance_search_window_page(
            job_id=job_id,
            unit_key=unit_key,
            window_id=window.id,
            next_page_token="crash_checkpoint_token",
            encountered_video_ids=(),
            unavailable_video_ids=(),
        )
    os._exit(91)


def _run_blocked_worker(
    database_path: Path,
    ready_path: Path,
    release_path: Path,
    summary_path: Path,
    now: datetime,
) -> None:
    from market_voice_forecast_ledger.config import Settings
    from market_voice_forecast_ledger.workers.scheduled_sync import (
        WorkerDependencies,
        run_once,
    )
    from market_voice_forecast_ledger.windows.task_scheduler import ScheduledTaskStatus
    from tests.backend.youtube_fakes import (
        FakeCredentialStore,
        FakeScheduleReader,
        synthetic_video_item,
    )

    class BlockingTransport:
        def get_json(self, endpoint, params, api_key):
            ready_path.write_text("ready", encoding="ascii")
            deadline = time.monotonic() + 20
            while not release_path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test release was not observed")
                time.sleep(0.01)
            if endpoint != "videos":
                raise AssertionError("manual worker expected videos.list only")
            return {
                "items": [
                    synthetic_video_item(video_id=params["id"])
                ],
                "nextPageToken": None,
            }

    settings = Settings.for_data_dir(database_path.parent)
    dependencies = WorkerDependencies(
        credential_store=FakeCredentialStore(),
        transport=BlockingTransport(),
        schedule_reader=FakeScheduleReader(ScheduledTaskStatus.unavailable()),
        clock=lambda: now,
        sleeper=lambda _seconds: None,
    )
    summary = run_once(settings, dependencies)
    summary_path.write_text(
        json.dumps(
            {
                "claimed_jobs": summary.claimed_jobs,
                "completed_jobs": summary.completed_jobs,
                "deferred_jobs": summary.deferred_jobs,
                "failed_jobs": summary.failed_jobs,
            },
            sort_keys=True,
        ),
        encoding="ascii",
    )


def main() -> None:
    mode = sys.argv[1]
    if mode == "crash":
        _crash_after_checkpoint(
            Path(sys.argv[2]), int(sys.argv[3]), _parse_utc(sys.argv[4])
        )
        return
    if mode == "worker":
        _run_blocked_worker(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            _parse_utc(sys.argv[6]),
        )
        return
    raise ValueError("unknown helper mode")


if __name__ == "__main__":
    main()

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.services.job_state import JobStateService


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _create_synthetic_job(db, stages: tuple[JobStage, ...]) -> int:
    units = tuple(
        ManifestUnit(
            "video:one" if ordinal == 1 else f"unit:{ordinal}",
            stage,
            ordinal,
            f"declared-input-{ordinal}",
            (),
            f"execution-contract-{ordinal}",
        )
        for ordinal, stage in enumerate(stages, start=1)
    )
    service = JobStateService(db)
    job_id = service.create(JobManifest.build(JobKind.VIDEO_PIPELINE, units))
    service.begin_unit(job_id, units[0].unit_key)
    return job_id


def test_video_metadata_and_audio_are_separate_progress_stages(db):
    job_id = _create_synthetic_job(
        db, stages=(JobStage.VIDEO_METADATA, JobStage.AUDIO_ACQUISITION)
    )
    JobStateService(db).complete_unit(job_id, "video:one", "meta-hash")

    progress = JobStateService(db).progress(job_id)

    assert progress.stage(JobStage.VIDEO_METADATA).completed == 1
    assert progress.stage(JobStage.AUDIO_ACQUISITION).completed == 0


def test_progress_is_literal_success_count_over_manifest_total(db):
    stages = (
        JobStage.TRANSCRIPTION,
        JobStage.TRANSCRIPTION,
        JobStage.TRANSCRIPTION,
        JobStage.SPEAKER_ASSIGNMENT,
    )
    job_id = _create_synthetic_job(db, stages)
    service = JobStateService(db)
    service.complete_unit(job_id, "video:one", "chunk-1-hash")
    service.begin_unit(job_id, "unit:2")
    service.fail_unit(job_id, "unit:2", "synthetic_retryable")

    progress = service.progress(job_id)

    transcription = progress.stage(JobStage.TRANSCRIPTION)
    speaker_assignment = progress.stage(JobStage.SPEAKER_ASSIGNMENT)
    assert (transcription.completed, transcription.total) == (1, 3)
    assert (speaker_assignment.completed, speaker_assignment.total) == (0, 1)
    assert (progress.completed, progress.total) == (1, 4)


def test_progress_includes_empty_stages_without_synthetic_completion(db):
    job_id = _create_synthetic_job(db, (JobStage.VIDEO_METADATA,))

    progress = JobStateService(db).progress(job_id)

    audio = progress.stage(JobStage.AUDIO_ACQUISITION)
    heatmap = progress.stage(JobStage.HEATMAP_UPDATE)
    assert (audio.completed, audio.total) == (0, 0)
    assert (heatmap.completed, heatmap.total) == (0, 0)
    assert not hasattr(progress, "eta")
    assert not hasattr(progress, "weights")

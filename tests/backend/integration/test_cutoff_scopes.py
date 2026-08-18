from datetime import date, datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import AssignmentKind
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from tests.backend.integration.test_analysis_input_boundaries import (
    _add_video_with_segments,
    _begin,
    _create_job_for_input,
    _create_subject,
    _save_assignment,
)


DAY_ONE = date(2026, 8, 13)
DAY_TWO = date(2026, 8, 14)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _subject_with_two_publication_days(db, *, name: str, channel_index: int):
    subject_id = _create_subject(
        db, name, channel_index=channel_index
    )
    _, first_segments = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id=f"synthetic-day-one-{channel_index}",
        published_at=datetime(2026, 8, 13, 14, 30, tzinfo=timezone.utc),
        texts=("Synthetic first-day evidence.",),
        channel_index=channel_index,
        transcript_created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    _, second_segments = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id=f"synthetic-day-two-{channel_index}",
        published_at=datetime(2026, 8, 14, 14, 30, tzinfo=timezone.utc),
        texts=("Synthetic second-day evidence.",),
        channel_index=channel_index,
        transcript_created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    for segment_id, evidence_hash in (
        (first_segments[0], "first-day-assignment"),
        (second_segments[0], "second-day-assignment"),
    ):
        _save_assignment(
            db,
            segment_id=segment_id,
            kind=AssignmentKind.SUBJECT,
            subject_id=subject_id,
            evidence_hash=f"{evidence_hash}-{channel_index}",
        )
    return subject_id, first_segments[0], second_segments[0]


def test_different_cutoff_scopes_coexist_with_exclusive_jst_midnights(db):
    subject_id, first_segment, second_segment = _subject_with_two_publication_days(
        db, name="Synthetic Cutoff Person", channel_index=10
    )

    day_one_run = _begin(db, _create_job_for_input(db, subject_id, DAY_ONE))
    day_two_run = _begin(db, _create_job_for_input(db, subject_id, DAY_TWO))
    repository = AnalysisRepository(db)
    day_one_scope = repository.get_scope(day_one_run.scope_id)
    day_two_scope = repository.get_scope(day_two_run.scope_id)

    assert day_one_scope.id != day_two_scope.id
    assert day_one_scope.cutoff_day == DAY_ONE
    assert day_two_scope.cutoff_day == DAY_TWO
    assert day_one_scope.cutoff_exclusive_utc == datetime(
        2026, 8, 13, 15, 0, tzinfo=timezone.utc
    )
    assert day_two_scope.cutoff_exclusive_utc == datetime(
        2026, 8, 14, 15, 0, tzinfo=timezone.utc
    )
    assert tuple(
        item.segment_id for item in repository.get_input_segments(day_one_run.id)
    ) == (first_segment,)
    assert tuple(
        item.segment_id for item in repository.get_input_segments(day_two_run.id)
    ) == (first_segment, second_segment)


def test_cutoff_selection_uses_publication_time_not_transcript_creation_time(db):
    subject_id, first_segment, second_segment = _subject_with_two_publication_days(
        db, name="Synthetic Timestamp Boundary Person", channel_index=11
    )

    run = _begin(db, _create_job_for_input(db, subject_id, DAY_ONE))

    assert tuple(
        item.segment_id
        for item in AnalysisRepository(db).get_input_segments(run.id)
    ) == (first_segment,)
    assert second_segment not in {
        item.segment_id
        for item in AnalysisRepository(db).get_input_segments(run.id)
    }


def test_rerunning_one_scope_appends_run_without_changing_other_scope(db):
    subject_id, _, _ = _subject_with_two_publication_days(
        db, name="Synthetic Rerun Person", channel_index=12
    )
    repository = AnalysisRepository(db)
    first_day_one = _begin(db, _create_job_for_input(db, subject_id, DAY_ONE))
    day_two = _begin(db, _create_job_for_input(db, subject_id, DAY_TWO))
    other_scope_before = repository.get_scope(day_two.scope_id)
    other_run_before = repository.get_run(day_two.id)
    other_segments_before = repository.get_input_segments(day_two.id)

    second_day_one = _begin(db, _create_job_for_input(db, subject_id, DAY_ONE))

    assert second_day_one.scope_id == first_day_one.scope_id
    assert second_day_one.id != first_day_one.id
    assert repository.count_runs(first_day_one.scope_id) == 2
    assert repository.count_runs(day_two.scope_id) == 1
    assert repository.get_scope(day_two.scope_id) == other_scope_before
    assert repository.get_run(day_two.id) == other_run_before
    assert repository.get_input_segments(day_two.id) == other_segments_before


def test_same_cutoff_day_for_two_subjects_creates_independent_scopes(db):
    first_subject, _, _ = _subject_with_two_publication_days(
        db, name="Synthetic First Subject", channel_index=13
    )
    second_subject, _, _ = _subject_with_two_publication_days(
        db, name="Synthetic Second Subject", channel_index=14
    )

    first_run = _begin(db, _create_job_for_input(db, first_subject, DAY_TWO))
    second_run = _begin(db, _create_job_for_input(db, second_subject, DAY_TWO))

    assert first_run.scope_id != second_run.scope_id
    assert (
        AnalysisRepository(db).get_scope(first_run.scope_id).subject_id
        == first_subject
    )
    assert (
        AnalysisRepository(db).get_scope(second_run.scope_id).subject_id
        == second_subject
    )

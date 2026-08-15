import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.retention import RetentionRepository
from market_voice_forecast_ledger.services.retention import (
    AudioDeletionResult,
    RetentionService,
    is_safe_audio_path,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path):
    value = Settings.for_data_dir(tmp_path / "runtime")
    value.temp_audio_dir.mkdir(parents=True)
    return value


def test_audio_path_outside_dedicated_folder_is_refused_without_path_leak(
    db, settings, tmp_path
):
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        outside, created_at=NOW
    )

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    assert result.retryable is True
    assert result.retry_count == 1
    assert result.deleted is False
    assert outside.exists()
    artifact = RetentionRepository(db).get_audio_artifact(artifact_id)
    assert artifact.status == "delete_failed"
    assert artifact.local_path == outside
    assert artifact.safe_error_code == "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    assert str(outside) not in repr(result)


def test_safe_audio_file_is_deleted_and_private_path_is_not_public(db, settings):
    audio = settings.temp_audio_dir / "synthetic.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.deleted is True
    assert result.already_absent is False
    assert result.retryable is False
    assert result.error_code is None
    assert result.deleted_at == NOW
    assert not audio.exists()
    assert str(audio) not in repr(result)
    artifact = RetentionRepository(db).get_audio_artifact(artifact_id)
    assert artifact.status == "deleted"
    assert artifact.deleted_at == NOW


def test_permission_failure_records_safe_retry_then_later_succeeds(
    db, settings, monkeypatch
):
    audio = settings.temp_audio_dir / "retry.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )
    original_unlink = Path.unlink
    attempts = 0

    def fail_once(path, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("private path must not escape")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    service = RetentionService(db, settings, clock=lambda: NOW)

    failed = service.delete_audio(artifact_id)
    succeeded = service.delete_audio(artifact_id)

    assert failed.error_code == "AUDIO_DELETE_PERMISSION"
    assert failed.retryable is True
    assert failed.retry_count == 1
    assert str(audio) not in repr(failed)
    assert succeeded.deleted is True
    assert succeeded.retry_count == 1
    assert not audio.exists()


def test_audio_cleanup_gate_remains_owned_by_the_calling_service(db, settings):
    first_audio = settings.temp_audio_dir / "first-service.wav"
    second_audio = settings.temp_audio_dir / "second-service.wav"
    first_audio.write_bytes(b"synthetic-audio")
    second_audio.write_bytes(b"synthetic-audio")
    repository = RetentionRepository(db)
    first_id = repository.add_audio_artifact(first_audio, created_at=NOW)
    second_id = repository.add_audio_artifact(second_audio, created_at=NOW)
    first_service = RetentionService(db, settings, clock=lambda: NOW)
    second_service = RetentionService(db, settings, clock=lambda: NOW)

    first_result = first_service.delete_audio(first_id)
    second_result = second_service.delete_audio(second_id)

    assert first_result.deleted is True
    assert second_result.deleted is True
    assert repository.get_audio_artifact(first_id).status == "deleted"
    assert repository.get_audio_artifact(second_id).status == "deleted"


def test_missing_file_inside_safe_root_has_idempotent_deleted_outcome(db, settings):
    missing = settings.temp_audio_dir / "already-absent.wav"
    artifact_id = RetentionRepository(db).add_audio_artifact(
        missing, created_at=NOW
    )
    service = RetentionService(db, settings, clock=lambda: NOW)

    first = service.delete_audio(artifact_id)
    second = service.delete_audio(artifact_id)

    assert first.deleted is True
    assert first.already_absent is True
    assert first.error_code is None
    assert second == first


def test_false_exists_result_cannot_mark_present_audio_deleted(
    db, settings, monkeypatch
):
    audio = settings.temp_audio_dir / "stat-obscured.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )
    original_exists = Path.exists
    unlink_calls = 0

    def obscured_exists(path):
        if path == audio:
            return False
        return original_exists(path)

    def permission_failure(path, *args, **kwargs):
        nonlocal unlink_calls
        unlink_calls += 1
        raise PermissionError("private path must not escape")

    monkeypatch.setattr(Path, "exists", obscured_exists)
    monkeypatch.setattr(Path, "unlink", permission_failure)

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == "AUDIO_DELETE_PERMISSION"
    assert result.deleted is False
    assert result.retryable is True
    assert unlink_calls == 1
    assert os.path.exists(audio)
    artifact = RetentionRepository(db).get_audio_artifact(artifact_id)
    assert artifact.status == "delete_failed"


def test_safe_audio_path_rejects_root_relative_traversal_and_missing_root(
    settings, tmp_path
):
    inside = settings.temp_audio_dir / "inside.wav"
    outside = settings.temp_audio_dir.parent / "outside.wav"

    assert is_safe_audio_path(settings.temp_audio_dir, inside) is True
    assert (
        is_safe_audio_path(settings.temp_audio_dir, settings.temp_audio_dir)
        is False
    )
    assert (
        is_safe_audio_path(
            settings.temp_audio_dir, Path("..") / "escape.wav"
        )
        is False
    )
    assert is_safe_audio_path(settings.temp_audio_dir, outside) is False
    assert is_safe_audio_path(tmp_path / "missing-root", inside) is False


def test_public_audio_result_is_frozen_slotted_and_artifact_history_is_guarded(
    db, settings
):
    assert AudioDeletionResult.__dataclass_params__.frozen is True
    assert AudioDeletionResult.__slots__
    RetentionService(db, settings, clock=lambda: NOW)
    audio = settings.temp_audio_dir / "guarded.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )

    row = db.execute(
        "SELECT * FROM local_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    columns = tuple(row.keys())
    placeholders = ",".join("?" for _ in columns)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            f"INSERT OR REPLACE INTO local_artifacts({','.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )

    for sql, parameters in (
        (
            "UPDATE local_artifacts SET local_path=? WHERE id=?",
            (str(settings.temp_audio_dir / "rewritten.wav"), artifact_id),
        ),
        (
            "UPDATE local_artifacts SET retry_count=0 WHERE id=?",
            (artifact_id,),
        ),
        (
            "UPDATE local_artifacts SET status='deleted', deleted_at=? WHERE id=?",
            ("not-canonical", artifact_id),
        ),
        ("DELETE FROM local_artifacts WHERE id=?", (artifact_id,)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, parameters)

    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            """
            UPDATE local_artifacts
            SET status='deleted', safe_error_code=NULL,
                deleted_at='2026-08-16T12:00:00.000000Z'
            WHERE id=?
            """,
            (artifact_id,),
        )


def test_plain_sqlite_replace_cannot_forge_audio_artifact_success(
    db, settings, tmp_path
):
    audio = settings.temp_audio_dir / "plain-replace.wav"
    audio.write_bytes(b"synthetic-audio")
    repository = RetentionRepository(db)
    artifact_id = repository.add_audio_artifact(audio, created_at=NOW)
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    plain = sqlite3.connect(database_path, isolation_level=None)
    plain.row_factory = sqlite3.Row
    try:
        assert plain.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert plain.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = plain.execute(
            "SELECT * FROM local_artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        replacement = dict(row)
        replacement.update(
            local_path=str(tmp_path / "forged-outside.wav"),
            status="deleted",
            safe_error_code=None,
            deleted_at="2026-08-16T12:00:00.000000Z",
        )
        columns = tuple(replacement)
        placeholders = ",".join("?" for _ in columns)

        with pytest.raises(sqlite3.IntegrityError):
            plain.execute(
                f"INSERT OR REPLACE INTO local_artifacts({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(replacement[column] for column in columns),
            )
    finally:
        plain.close()

    stored = repository.get_audio_artifact(artifact_id)
    assert stored.status == "pending"
    assert stored.local_path == audio
    assert audio.exists()


def test_symlink_escape_is_refused_where_supported(db, settings, tmp_path):
    outside = tmp_path / "outside-target.wav"
    outside.write_bytes(b"synthetic-audio")
    link = settings.temp_audio_dir / "escape.wav"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as cause:
        pytest.skip(f"symlink creation unavailable: {type(cause).__name__}")
    artifact_id = RetentionRepository(db).add_audio_artifact(link, created_at=NOW)

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    assert outside.exists()
    assert link.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows case semantics")
def test_windows_case_variants_do_not_turn_sibling_into_child(settings):
    sibling = settings.temp_audio_dir.parent / (
        settings.temp_audio_dir.name.upper() + "-ESCAPE"
    ) / "outside.wav"
    case_variant_child = Path(
        str(settings.temp_audio_dir).swapcase()
    ) / "inside.wav"

    assert is_safe_audio_path(settings.temp_audio_dir, sibling) is False
    assert is_safe_audio_path(settings.temp_audio_dir, case_variant_child) is True


def test_outside_refusal_never_invokes_unlink(db, settings, tmp_path, monkeypatch):
    outside = tmp_path / "unlink-must-not-run.wav"
    outside.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        outside, created_at=NOW
    )

    def forbidden_unlink(*args, **kwargs):
        raise AssertionError("unlink must not be called for an outside path")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    assert outside.exists()


def test_generic_os_failure_is_safe_retryable_and_path_free(
    db, settings, monkeypatch
):
    audio = settings.temp_audio_dir / "os-failure.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )

    def fail_unlink(*args, **kwargs):
        raise OSError(f"private location: {audio}")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == "AUDIO_DELETE_OS_ERROR"
    assert result.retryable is True
    assert result.retry_count == 1
    assert str(audio) not in repr(result)
    assert audio.exists()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (PermissionError("private resolution detail"), "AUDIO_DELETE_PERMISSION"),
        (OSError("private resolution detail"), "AUDIO_DELETE_OS_ERROR"),
    ],
)
def test_path_resolution_os_failures_keep_distinct_safe_retry_codes(
    db, settings, monkeypatch, failure, expected_code
):
    audio = settings.temp_audio_dir / "resolution-failure.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )
    original_resolve = Path.resolve

    def fail_candidate_resolution(path, *, strict=False):
        if path == audio:
            raise failure
        return original_resolve(path, strict=strict)

    def forbidden_unlink(*args, **kwargs):
        raise AssertionError("unlink must not run after resolution failure")

    monkeypatch.setattr(Path, "resolve", fail_candidate_resolution)
    monkeypatch.setattr(Path, "unlink", forbidden_unlink)

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert result.error_code == expected_code
    assert result.retryable is True
    assert result.deleted is False
    assert result.retry_count == 1
    assert "private resolution detail" not in repr(result)
    assert str(audio) not in repr(result)
    assert os.path.exists(audio)


def test_embedded_nul_path_is_safe_retryable_and_never_considered_owned(
    db, settings
):
    malformed = Path(str(settings.temp_audio_dir / "malformed") + "\x00private")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        malformed, created_at=NOW
    )

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    assert is_safe_audio_path(settings.temp_audio_dir, malformed) is False
    assert result.error_code == "AUDIO_DELETE_OS_ERROR"
    assert result.deleted is False
    assert result.retryable is True
    assert result.retry_count == 1
    assert str(malformed) not in repr(result)
    artifact = RetentionRepository(db).get_audio_artifact(artifact_id)
    assert artifact.status == "delete_failed"
    assert artifact.safe_error_code == "AUDIO_DELETE_OS_ERROR"


def test_audio_cleanup_does_not_infer_or_mutate_any_job_state(
    db, settings, monkeypatch
):
    timestamp = "2026-08-16T12:00:00.000000Z"
    job_id = db.execute(
        """
        INSERT INTO jobs(
            source_job_id, job_kind, manifest_hash, total_units,
            status, created_at, updated_at
        ) VALUES (NULL, 'video_pipeline', 'synthetic-manifest', 1,
                  'queued', ?, ?)
        """,
        (timestamp, timestamp),
    ).lastrowid
    db.execute(
        """
        INSERT INTO job_units(
            job_id, unit_key, stage, ordinal, declared_input_hash,
            dependency_keys_json, execution_contract_hash,
            external_input_hash, bound_input_hash, output_hash,
            status, attempt_count, error_code, started_at, finished_at
        ) VALUES (?, 'audio:synthetic', 'audio_acquisition', 1, NULL,
                  '[]', 'synthetic-contract', NULL, NULL, NULL,
                  'pending', 0, NULL, NULL, NULL)
        """,
        (job_id,),
    )
    before = tuple(
        (table, tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table}")))
        for table in ("jobs", "job_units", "job_unit_attempts", "job_events")
    )
    audio = settings.temp_audio_dir / "no-inferred-job.wav"
    audio.write_bytes(b"synthetic-audio")
    artifact_id = RetentionRepository(db).add_audio_artifact(
        audio, created_at=NOW
    )
    monkeypatch.setattr(
        Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError())
    )

    result = RetentionService(db, settings, clock=lambda: NOW).delete_audio(
        artifact_id
    )

    after = tuple(
        (table, tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table}")))
        for table in ("jobs", "job_units", "job_unit_attempts", "job_events")
    )
    assert result.error_code == "AUDIO_DELETE_PERMISSION"
    assert after == before


def test_non_audio_artifact_kind_is_rejected_by_private_schema(db, settings):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO local_artifacts(
                kind, local_path, status, retry_count,
                safe_error_code, created_at, deleted_at
            ) VALUES ('video', ?, 'pending', 0, NULL, ?, NULL)
            """,
            (
                str(settings.temp_audio_dir / "not-audio.mp4"),
                "2026-08-16T12:00:00.000000Z",
            ),
        )


@pytest.mark.parametrize(
    ("status", "created_at", "deleted_at"),
    [
        ("pending", "0000-01-01T00:00:00.000000Z", None),
        (
            "deleted",
            "2026-08-16T23:00:00.000000Z",
            "2026-08-16T24:00:00.000000Z",
        ),
    ],
)
def test_audio_artifact_schema_rejects_noncanonical_structured_utc(
    db, settings, status, created_at, deleted_at
):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO local_artifacts(
                kind, local_path, status, retry_count,
                safe_error_code, created_at, deleted_at
            ) VALUES ('audio', ?, ?, 0, NULL, ?, ?)
            """,
            (
                str(settings.temp_audio_dir / "invalid-time.wav"),
                status,
                created_at,
                deleted_at,
            ),
        )


@pytest.mark.parametrize("created_at", [False, 0, "2026-08-16"])
def test_audio_artifact_rejects_malformed_created_at(db, settings, created_at):
    with pytest.raises(DomainError) as error:
        RetentionRepository(db).add_audio_artifact(
            settings.temp_audio_dir / "invalid-created-at.wav",
            created_at=created_at,
        )

    assert error.value.code == "AUDIO_ARTIFACT_INVALID"
    assert db.execute("SELECT COUNT(*) FROM local_artifacts").fetchone()[0] == 0

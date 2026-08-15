import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import HeatmapGranularity
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.retention import RetentionRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.retention import (
    DeleteTextCommand,
    RetentionPolicy,
    RetentionService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.e2e.synthetic_fixture import (
    create_retained_forecast_fixture,
)
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
    _create_job_for_input,
    _prepare_personal_analysis,
)
from tests.backend.integration.test_statement_evidence import (
    _one_forecast,
    _prepare_output,
)


UTC = timezone.utc
NOW = datetime(2028, 8, 16, 12, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_preview_is_nonmutating_and_delete_preserves_public_history(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    command = DeleteTextCommand(cutoff=NOW)
    current_before = CurrentResultService(db).get_scope(fixture.scope_id)
    heatmap_before = HeatmapService(db).read_scope(
        fixture.scope_id, HeatmapGranularity.WEEK
    )
    evidence_before = tuple(
        tuple(row)
        for row in db.execute(
            """
            SELECT statement_id, ordinal, run_segment_id, excerpt, start_ms, end_ms
            FROM analysis_statement_evidence_links
            WHERE statement_id=?
            ORDER BY ordinal
            """,
            (fixture.statement_id,),
        )
    )

    preview = service.preview_text_deletion(command)

    assert preview.affected_video_count == 1
    assert preview.affected_transcript_count == 1
    assert preview.affected_analysis_input_count == 1
    assert preview.full_reproduction_will_be_lost is True
    assert preview.expires_at == NOW + timedelta(minutes=10)
    assert len(preview.token) == 64
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )
    assert AnalysisRepository(db).get_snapshot(fixture.run_id).input_text is not None

    result = service.delete_text(
        DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
    )

    segment = SpeakerRepository(db).get_segment(fixture.segment_id)
    snapshot = AnalysisRepository(db).get_snapshot(fixture.run_id)
    assert segment.text_body is None
    assert segment.text_sha256 == fixture.transcript_hash
    assert segment.text_deleted_at == NOW
    assert snapshot.input_text is None
    assert snapshot.input_sha256 == fixture.input_hash
    assert snapshot.text_deleted_at == NOW
    assert result.deleted_transcript_count == 1
    assert result.deleted_analysis_input_count == 1
    assert result.affected_video_count == 1
    assert result.deleted_at == NOW
    assert CurrentResultService(db).get_scope(fixture.scope_id) == current_before
    assert HeatmapService(db).read_scope(
        fixture.scope_id, HeatmapGranularity.WEEK
    ) == heatmap_before
    assert tuple(
        tuple(row)
        for row in db.execute(
            """
            SELECT statement_id, ordinal, run_segment_id, excerpt, start_ms, end_ms
            FROM analysis_statement_evidence_links
            WHERE statement_id=?
            ORDER BY ordinal
            """,
            (fixture.statement_id,),
        )
    ) == evidence_before

    audit = db.execute(
        """
        SELECT before_json, after_json, reason_text
        FROM audit_events
        WHERE operation='private_text_deleted'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    serialized_audit = json.dumps(dict(audit), ensure_ascii=False)
    assert fixture.source_body not in serialized_audit
    assert "text_body" not in serialized_audit
    assert "input_text" not in serialized_audit
    assert fixture.settings.temp_audio_dir.as_posix() not in serialized_audit
    assert fixture.source_body not in repr(preview)
    assert fixture.source_body not in repr(result)


def test_delete_requires_the_exact_current_preview_token(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)

    with pytest.raises(DomainError) as missing:
        service.delete_text(DeleteTextCommand(cutoff=NOW))
    assert missing.value.code == "DELETION_PREVIEW_TOKEN_REQUIRED"

    with pytest.raises(DomainError) as malformed:
        service.delete_text(DeleteTextCommand(cutoff=NOW, preview_token="not-a-token"))
    assert malformed.value.code == "DELETION_PREVIEW_TOKEN_INVALID"

    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )
    assert db.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation='private_text_deleted'"
    ).fetchone()[0] == 0


def test_unlimited_policy_purge_deletes_nothing(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    service.set_policy(RetentionPolicy(None))

    result = service.purge_expired(NOW)

    assert result.deleted_transcript_count == 0
    assert result.deleted_analysis_input_count == 0
    assert result.affected_video_count == 0
    assert result.deleted_at is None
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )


def test_transcript_and_snapshot_raw_updates_allow_only_canonical_one_way_delete(
    db, tmp_path
):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    deleted_at = "2028-08-16T12:00:00.000000Z"

    for table, key_name, key_value in (
        ("transcript_segments", "id", fixture.segment_id),
        ("analysis_input_snapshots", "run_id", fixture.run_id),
    ):
        row = db.execute(
            f"SELECT * FROM {table} WHERE {key_name}=?", (key_value,)
        ).fetchone()
        columns = tuple(row.keys())
        placeholders = ",".join("?" for _ in columns)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT OR REPLACE INTO {table}({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )

    for sql, parameters in (
        (
            "UPDATE transcript_segments SET text_body=text_body WHERE id=?",
            (fixture.segment_id,),
        ),
        (
            "UPDATE transcript_segments SET text_body=NULL, text_deleted_at=? "
            "WHERE id=?",
            ("2028-08-16 12:00:00", fixture.segment_id),
        ),
        (
            "UPDATE analysis_input_snapshots SET input_text=input_text WHERE run_id=?",
            (fixture.run_id,),
        ),
        (
            "UPDATE analysis_input_snapshots SET input_text=NULL, "
            "text_deleted_at=? WHERE run_id=?",
            ("2028-08-16T12:00:00Z", fixture.run_id),
        ),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, parameters)

    db.execute(
        """
        UPDATE transcript_segments
        SET text_body=NULL, text_deleted_at=?
        WHERE id=?
        """,
        (deleted_at, fixture.segment_id),
    )
    db.execute(
        """
        UPDATE analysis_input_snapshots
        SET input_text=NULL, text_deleted_at=?
        WHERE run_id=?
        """,
        (deleted_at, fixture.run_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE transcript_segments SET text_deleted_at=? WHERE id=?",
            ("2028-08-17T12:00:00.000000Z", fixture.segment_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE analysis_input_snapshots SET input_text=? WHERE run_id=?",
            (fixture.source_body, fixture.run_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM transcript_segments WHERE id=?", (fixture.segment_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "DELETE FROM analysis_input_snapshots WHERE run_id=?", (fixture.run_id,)
        )


@pytest.mark.parametrize(
    (
        "table",
        "identity_column",
        "identity_attribute",
        "body_column",
        "hash_column",
        "replace_primary_key",
    ),
    [
        (
            "transcript_segments",
            "id",
            "segment_id",
            "text_body",
            "text_sha256",
            False,
        ),
        (
            "transcript_segments",
            "id",
            "segment_id",
            "text_body",
            "text_sha256",
            True,
        ),
        (
            "analysis_input_snapshots",
            "run_id",
            "run_id",
            "input_text",
            "input_sha256",
            False,
        ),
        (
            "analysis_input_snapshots",
            "run_id",
            "run_id",
            "input_text",
            "input_sha256",
            True,
        ),
    ],
)
def test_plain_sqlite_replace_cannot_bypass_private_text_immutability(
    db,
    tmp_path,
    table,
    identity_column,
    identity_attribute,
    body_column,
    hash_column,
    replace_primary_key,
):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    identity = getattr(fixture, identity_attribute)
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    plain = sqlite3.connect(database_path, isolation_level=None)
    plain.row_factory = sqlite3.Row
    try:
        assert plain.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert plain.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = plain.execute(
            f"SELECT * FROM {table} WHERE {identity_column}=?", (identity,)
        ).fetchone()
        replacement = dict(row)
        replacement[body_column] = "Synthetic forged replacement body."
        replacement[hash_column] = "0" * 64
        if replace_primary_key:
            replacement["id"] += 100_000
        columns = tuple(replacement)
        placeholders = ",".join("?" for _ in columns)

        with pytest.raises(sqlite3.IntegrityError):
            plain.execute(
                f"INSERT OR REPLACE INTO {table}({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(replacement[column] for column in columns),
            )
    finally:
        plain.close()

    stored = db.execute(
        f"SELECT {body_column}, {hash_column} FROM {table} "
        f"WHERE {identity_column}=?",
        (identity,),
    ).fetchone()
    assert stored[body_column] != "Synthetic forged replacement body."
    assert stored[hash_column] != "0" * 64


def test_preview_is_deterministic_and_new_preview_invalidates_the_old_token(
    db, tmp_path
):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)

    first = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    repeated = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    replacement = service.preview_text_deletion(
        DeleteTextCommand(cutoff=NOW - timedelta(days=900))
    )

    assert repeated == first
    assert replacement.token != first.token
    with pytest.raises(DomainError) as stale:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=first.token)
        )
    assert stale.value.code == "DELETION_PREVIEW_NOT_CURRENT"
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )


def test_identical_preview_reissue_does_not_extend_the_old_tokens_lifetime(
    db, tmp_path
):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    clock = [NOW]
    service = RetentionService(db, fixture.settings, clock=lambda: clock[0])
    command = DeleteTextCommand(cutoff=NOW)
    first = service.preview_text_deletion(command)
    clock[0] = first.expires_at - timedelta(minutes=1)

    renewed = service.preview_text_deletion(command)

    assert renewed.token != first.token
    assert renewed.expires_at > first.expires_at
    clock[0] = first.expires_at + timedelta(microseconds=1)
    with pytest.raises(DomainError) as stale:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=first.token)
        )
    assert stale.value.code == "DELETION_PREVIEW_NOT_CURRENT"

    result = service.delete_text(
        DeleteTextCommand(cutoff=NOW, preview_token=renewed.token)
    )
    assert result.deleted_transcript_count > 0


def test_preview_is_not_valid_before_its_issuance_time(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    clock = [NOW]
    service = RetentionService(db, fixture.settings, clock=lambda: clock[0])
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    clock[0] = NOW - timedelta(microseconds=1)

    with pytest.raises(DomainError) as invalid_window:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
        )

    assert invalid_window.value.code == "DELETION_PREVIEW_EXPIRED"
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )


def test_preview_rejects_command_policy_drift_expiry_and_replay(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    clock = [NOW]
    service = RetentionService(db, fixture.settings, clock=lambda: clock[0])

    command_preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    with pytest.raises(DomainError) as command_mismatch:
        service.delete_text(
            DeleteTextCommand(
                cutoff=NOW - timedelta(microseconds=1),
                preview_token=command_preview.token,
            )
        )
    assert command_mismatch.value.code == "DELETION_PREVIEW_COMMAND_MISMATCH"

    service.set_policy(RetentionPolicy(30))
    with pytest.raises(DomainError) as policy_changed:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=command_preview.token)
        )
    assert policy_changed.value.code == "DELETION_PREVIEW_POLICY_CHANGED"

    current = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    clock[0] = current.expires_at
    with pytest.raises(DomainError) as expired:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=current.token)
        )
    assert expired.value.code == "DELETION_PREVIEW_EXPIRED"

    clock[0] = NOW
    replayable = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    service.delete_text(
        DeleteTextCommand(cutoff=NOW, preview_token=replayable.token)
    )
    with pytest.raises(DomainError) as replayed:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=replayable.token)
        )
    assert replayed.value.code == "DELETION_PREVIEW_NOT_CURRENT"
    assert db.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation='private_text_deleted'"
    ).fetchone()[0] == 1


def test_delete_rechecks_preview_expiry_after_acquiring_its_transaction(
    db, tmp_path, monkeypatch
):
    import market_voice_forecast_ledger.services.retention as retention_module

    fixture = create_retained_forecast_fixture(db, tmp_path)
    clock_value = NOW
    service = RetentionService(db, fixture.settings, clock=lambda: clock_value)
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    clock_value = preview.expires_at - timedelta(microseconds=1)
    original_transaction = retention_module.transaction

    def expire_before_begin(conn):
        nonlocal clock_value
        clock_value = preview.expires_at
        return original_transaction(conn)

    monkeypatch.setattr(retention_module, "transaction", expire_before_begin)

    with pytest.raises(DomainError) as expired:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
        )

    assert expired.value.code == "DELETION_PREVIEW_EXPIRED"
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )


def test_preview_fails_safely_when_expiry_cannot_be_represented(db, tmp_path):
    service = RetentionService(
        db,
        Settings.for_data_dir(tmp_path / "runtime-overflow-preview"),
        clock=lambda: datetime.max.replace(tzinfo=UTC),
    )

    with pytest.raises(DomainError) as error:
        service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))

    assert error.value.code == "RETENTION_TIME_INVALID"
    assert db.execute(
        "SELECT COUNT(*) FROM retention_deletion_previews"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "invalid", "created_at", "expires_at"),
    [
        (
            "cutoff_utc",
            "2026-99-99T99:99:99.000000Z",
            "2026-08-16T12:00:00.000000Z",
            "2026-08-16T12:10:00.000000Z",
        ),
        (
            "created_at",
            "0000-01-01T00:00:00.000000Z",
            "0000-01-01T00:00:00.000000Z",
            "2026-08-16T12:10:00.000000Z",
        ),
        (
            "expires_at",
            "2026-08-16T24:00:00.000000Z",
            "2026-08-16T23:00:00.000000Z",
            "2026-08-16T24:00:00.000000Z",
        ),
    ],
)
def test_preview_schema_rejects_noncanonical_utc_values(
    db, column, invalid, created_at, expires_at
):
    values = {
        "cutoff_utc": "2026-08-16T12:00:00.000000Z",
        "created_at": created_at,
        "expires_at": expires_at,
    }
    values[column] = invalid

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO retention_deletion_previews(
                id, token, cutoff_utc, retention_days, target_fingerprint,
                created_at, expires_at
            ) VALUES (1, ?, ?, 365, ?, ?, ?)
            """,
            (
                "a" * 64,
                values["cutoff_utc"],
                "b" * 64,
                values["created_at"],
                values["expires_at"],
            ),
        )


@pytest.mark.parametrize(
    "invalid",
    ["0000-01-01T00:00:00.000000Z", "2026-08-16T24:00:00.000000Z"],
)
def test_body_deletion_triggers_reject_noncanonical_structured_utc(
    db, tmp_path, invalid
):
    fixture = create_retained_forecast_fixture(db, tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            UPDATE transcript_segments
            SET text_body=NULL, text_deleted_at=?
            WHERE id=?
            """,
            (invalid, fixture.segment_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            UPDATE analysis_input_snapshots
            SET input_text=NULL, text_deleted_at=?
            WHERE run_id=?
            """,
            (invalid, fixture.run_id),
        )


def test_purge_fails_safely_when_cutoff_cannot_be_represented(db, tmp_path):
    service = RetentionService(
        db,
        Settings.for_data_dir(tmp_path / "runtime-underflow-purge"),
    )

    with pytest.raises(DomainError) as error:
        service.purge_expired(datetime.min.replace(tzinfo=UTC))

    assert error.value.code == "RETENTION_TIME_INVALID"


def test_new_eligible_body_after_preview_is_target_drift(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    original = db.execute(
        "SELECT video_id, chunk_id FROM transcript_segments WHERE id=?",
        (fixture.segment_id,),
    ).fetchone()
    added_id = SpeakerRepository(db).add_segment(
        video_id=original["video_id"],
        chunk_id=original["chunk_id"],
        segment_no=999,
        start_ms=999_000,
        end_ms=999_500,
        text_body="Synthetic late-arriving retained body.",
        anonymous_speaker_id="synthetic-late",
        transcript_created_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=29),
    )

    with pytest.raises(DomainError) as drifted:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
        )

    assert drifted.value.code == "DELETION_TARGET_DRIFT"
    assert SpeakerRepository(db).get_segment(fixture.segment_id).text_body == (
        fixture.source_body
    )
    assert SpeakerRepository(db).get_segment(added_id).text_body is not None


def test_delete_fault_rolls_back_bodies_audit_and_preview_after_reopen(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "rollback.sqlite3"
    conn = open_database(db_path)
    apply_migrations(conn)
    fixture = create_retained_forecast_fixture(conn, tmp_path)
    service = RetentionService(conn, fixture.settings, clock=lambda: NOW)
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))

    def fail_after_audit(self, token):
        raise sqlite3.OperationalError("synthetic consume failure")

    monkeypatch.setattr(RetentionRepository, "consume_preview", fail_after_audit)
    with pytest.raises(DomainError) as failed:
        service.delete_text(
            DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
        )
    assert failed.value.code == "TEXT_DELETION_FAILED"
    conn.close()

    reopened = open_database(db_path)
    try:
        assert SpeakerRepository(reopened).get_segment(
            fixture.segment_id
        ).text_body == fixture.source_body
        assert AnalysisRepository(reopened).get_snapshot(
            fixture.run_id
        ).input_text is not None
        assert reopened.execute(
            "SELECT COUNT(*) FROM audit_events WHERE operation='private_text_deleted'"
        ).fetchone()[0] == 0
        assert reopened.execute(
            "SELECT token FROM retention_deletion_previews"
        ).fetchone()["token"] == preview.token
    finally:
        reopened.close()


def test_purge_uses_inclusive_canonical_policy_boundary(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    original = db.execute(
        "SELECT video_id, chunk_id FROM transcript_segments WHERE id=?",
        (fixture.segment_id,),
    ).fetchone()
    boundary_id = SpeakerRepository(db).add_segment(
        video_id=original["video_id"],
        chunk_id=original["chunk_id"],
        segment_no=997,
        start_ms=997_000,
        end_ms=997_500,
        text_body="Synthetic exact retention boundary.",
        anonymous_speaker_id="synthetic-boundary",
        transcript_created_at=NOW - timedelta(days=30),
        expires_at=NOW,
    )
    future_id = SpeakerRepository(db).add_segment(
        video_id=original["video_id"],
        chunk_id=original["chunk_id"],
        segment_no=998,
        start_ms=998_000,
        end_ms=998_500,
        text_body="Synthetic just inside retention.",
        anonymous_speaker_id="synthetic-future",
        transcript_created_at=NOW - timedelta(days=30) + timedelta(microseconds=1),
        expires_at=NOW + timedelta(microseconds=1),
    )
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    service.set_policy(RetentionPolicy(30))

    service.purge_expired(NOW)

    assert SpeakerRepository(db).get_segment(boundary_id).text_body is None
    assert SpeakerRepository(db).get_segment(future_id).text_body is not None


def test_purge_reads_the_current_policy_inside_its_delete_transaction(
    db, tmp_path, monkeypatch
):
    import market_voice_forecast_ledger.services.retention as retention_module

    fixture = create_retained_forecast_fixture(db, tmp_path)
    original = db.execute(
        "SELECT video_id, chunk_id FROM transcript_segments WHERE id=?",
        (fixture.segment_id,),
    ).fetchone()
    sixty_day_id = SpeakerRepository(db).add_segment(
        video_id=original["video_id"],
        chunk_id=original["chunk_id"],
        segment_no=995,
        start_ms=995_000,
        end_ms=995_500,
        text_body="Synthetic policy-race boundary.",
        anonymous_speaker_id="synthetic-policy-race",
        transcript_created_at=NOW - timedelta(days=60),
        expires_at=NOW - timedelta(days=30),
    )
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    service.set_policy(RetentionPolicy(30))
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    original_transaction = retention_module.transaction
    policy_changed = False

    def change_policy_before_begin(conn):
        nonlocal policy_changed
        if not policy_changed:
            policy_changed = True
            other = open_database(database_path)
            try:
                other.execute(
                    "UPDATE retention_settings SET retention_days=365 WHERE id=1"
                )
            finally:
                other.close()
        return original_transaction(conn)

    monkeypatch.setattr(retention_module, "transaction", change_policy_before_begin)

    service.purge_expired(NOW)

    assert policy_changed is True
    assert SpeakerRepository(db).get_segment(sixty_day_id).text_body is not None


@pytest.mark.parametrize(
    ("created_at", "digest", "expected_code"),
    [
        ("not-a-time", "0" * 64, "RETENTION_STORED_TIME_INVALID"),
        (
            "2028-08-15T12:00:00.000000Z",
            "not-a-hash",
            "RETENTION_STORED_HASH_INVALID",
        ),
        (
            "2028-08-15T12:00:00.000000Z",
            "0" * 64,
            "RETENTION_STORED_HASH_INVALID",
        ),
    ],
)
def test_selection_fails_closed_on_malformed_stored_time_or_hash(
    db, tmp_path, created_at, digest, expected_code
):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    original = db.execute(
        "SELECT video_id, chunk_id FROM transcript_segments WHERE id=?",
        (fixture.segment_id,),
    ).fetchone()
    db.execute(
        """
        INSERT INTO transcript_segments(
            video_id, chunk_id, segment_no, start_ms, end_ms, text_body,
            text_sha256, anonymous_speaker_id, transcript_created_at,
            expires_at, text_deleted_at
        ) VALUES (?, ?, 996, 996000, 996500, ?, ?, 'malformed', ?, NULL, NULL)
        """,
        (
            original["video_id"],
            original["chunk_id"],
            "Synthetic malformed stored body.",
            digest,
            created_at,
        ),
    )

    with pytest.raises(DomainError) as error:
        RetentionService(
            db, fixture.settings, clock=lambda: NOW
        ).preview_text_deletion(DeleteTextCommand(cutoff=NOW))

    assert error.value.code == expected_code
    assert db.execute(
        "SELECT COUNT(*) FROM retention_deletion_previews"
    ).fetchone()[0] == 0


def test_preview_and_reads_do_not_extend_persisted_expiry(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    before = (
        db.execute(
            "SELECT expires_at FROM transcript_segments WHERE id=?",
            (fixture.segment_id,),
        ).fetchone()[0],
        db.execute(
            "SELECT expires_at FROM analysis_input_snapshots WHERE run_id=?",
            (fixture.run_id,),
        ).fetchone()[0],
    )

    SpeakerRepository(db).get_segment(fixture.segment_id)
    AnalysisRepository(db).get_snapshot(fixture.run_id)
    RetentionService(
        db, fixture.settings, clock=lambda: NOW
    ).preview_text_deletion(DeleteTextCommand(cutoff=NOW))

    after = (
        db.execute(
            "SELECT expires_at FROM transcript_segments WHERE id=?",
            (fixture.segment_id,),
        ).fetchone()[0],
        db.execute(
            "SELECT expires_at FROM analysis_input_snapshots WHERE run_id=?",
            (fixture.run_id,),
        ).fetchone()[0],
    )
    assert after == before


def test_inflight_reanalysis_that_requires_deleted_body_fails_safely(db, tmp_path):
    prepared = _prepare_output(
        db,
        ("Synthetic first subject evidence.",),
        _one_forecast,
        start_normalization=False,
    )
    settings = Settings.for_data_dir(tmp_path / "runtime-reanalysis")
    service = RetentionService(db, settings, clock=lambda: NOW)
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    service.delete_text(
        DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
    )
    JobStateService(db).begin_unit(
        prepared.job_id, "analysis:normalize-statements"
    )

    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(prepared.run_id)

    assert error.value.code == "EVIDENCE_SOURCE_TEXT_UNAVAILABLE"


def test_new_analysis_run_cannot_silently_omit_deleted_eligible_body(db, tmp_path):
    fixture = create_retained_forecast_fixture(db, tmp_path)
    service = RetentionService(db, fixture.settings, clock=lambda: NOW)
    preview = service.preview_text_deletion(DeleteTextCommand(cutoff=NOW))
    service.delete_text(
        DeleteTextCommand(cutoff=NOW, preview_token=preview.token)
    )
    subject_id = db.execute(
        "SELECT subject_id FROM analysis_scopes WHERE id=?", (fixture.scope_id,)
    ).fetchone()[0]
    prepared = _create_job_for_input(db, subject_id)

    with pytest.raises(sqlite3.IntegrityError, match="SOURCE_TEXT_DELETED"):
        _begin(db, prepared)

    assert db.execute(
        "SELECT COUNT(*) FROM analysis_runs WHERE scope_id=?",
        (fixture.scope_id,),
    ).fetchone()[0] == 1


def test_new_analysis_run_rejects_deleted_required_interviewer_context(db):
    prepared_before_deletion = _prepare_personal_analysis(db)
    interviewer_id = db.execute(
        """
        SELECT segment_id
        FROM speaker_assignments
        WHERE assignment_kind='interviewer'
        """
    ).fetchone()[0]
    db.execute(
        """
        UPDATE transcript_segments
        SET text_body=NULL, text_deleted_at='2028-08-16T12:00:00.000000Z'
        WHERE id=?
        """,
        (interviewer_id,),
    )
    prepared_after_deletion = _create_job_for_input(
        db, prepared_before_deletion.subject_id
    )

    with pytest.raises(sqlite3.IntegrityError, match="SOURCE_TEXT_DELETED"):
        _begin(db, prepared_after_deletion)

    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0

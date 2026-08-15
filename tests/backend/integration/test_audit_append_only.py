import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_audit_table_rejects_update_and_delete(db):
    with transaction(db):
        event_id = AuditRepository(db).append(AuditEventInput.synthetic())
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE audit_events SET reason_code='changed' WHERE id=?", (event_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM audit_events WHERE id=?", (event_id,))


def test_audit_append_requires_an_active_caller_transaction(db):
    with pytest.raises(DomainError) as error:
        AuditRepository(db).append(AuditEventInput.synthetic())
    assert error.value.code == "AUDIT_TRANSACTION_REQUIRED"


def test_audit_append_validates_payloads_canonicalizes_json_and_does_not_commit(db):
    event = AuditEventInput(
        entity_type="synthetic_entity",
        entity_id="entity-1",
        scope_id=7,
        operation="correct",
        actor_kind="user",
        reason_code="synthetic_reason",
        reason_text="Synthetic correction",
        before={"z": 1, "a": {"safe": True}},
        after={"classification": "up", "ids": [2, 1]},
        created_at=datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc),
    )

    with transaction(db):
        event_id = AuditRepository(db).append(event)
        row = db.execute(
            "SELECT before_json, after_json, created_at FROM audit_events WHERE id=?",
            (event_id,),
        ).fetchone()
        assert db.in_transaction
        assert row["before_json"] == '{"a":{"safe":true},"z":1}'
        assert row["after_json"] == '{"classification":"up","ids":[2,1]}'
        assert row["created_at"] == "2026-08-15T01:02:03.456789Z"

    listed = AuditRepository(db).list_for_entity("synthetic_entity", "entity-1")
    assert len(listed) == 1
    assert listed[0].id == event_id
    assert listed[0].operation == "correct"
    assert listed[0].before == {"a": {"safe": True}, "z": 1}
    assert listed[0].after == {"classification": "up", "ids": [2, 1]}


@pytest.mark.parametrize("field", ["before", "after"])
def test_audit_append_rejects_private_keys_in_both_payloads(db, field):
    values = {
        "entity_type": "synthetic_entity",
        "entity_id": "entity-1",
        "scope_id": None,
        "operation": "correct",
        "actor_kind": "system",
        "reason_code": "synthetic_reason",
        "reason_text": "Synthetic correction",
        "before": {"safe": True},
        "after": {"safe": False},
        "created_at": datetime(2026, 8, 15, tzinfo=timezone.utc),
    }
    values[field] = {"nested": {"prompt_body": "private"}}

    with transaction(db):
        with pytest.raises(DomainError) as error:
            AuditRepository(db).append(AuditEventInput(**values))
        assert error.value.code == "AUDIT_PRIVATE_FIELD"

    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_type", "unsafe entity"),
        ("entity_id", "../unsafe"),
        ("scope_id", True),
        ("scope_id", 0),
        ("operation", "unsafe operation"),
        ("actor_kind", "admin"),
        ("reason_code", "unsafe code"),
        ("created_at", "2026-08-15T00:00:00Z"),
        ("created_at", datetime(2026, 8, 15)),
    ],
)
def test_audit_append_rejects_malformed_scalar_fields(db, field, value):
    event = replace(AuditEventInput.synthetic(), **{field: value})

    with pytest.raises(DomainError) as error:
        with transaction(db):
            AuditRepository(db).append(event)

    assert error.value.code == "AUDIT_SCALAR_INVALID"
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


@pytest.mark.parametrize(
    "reason",
    [
        "x" * 7_200,
        "C:\\private\\ledger.sqlite3",
        "source:C:\\private\\ledger.sqlite3",
        "\\\\server\\private\\audio.wav",
        "/var/private/ledger.sqlite3",
        "source=/var/private/ledger.sqlite3",
        "file:///C:/private/ledger.sqlite3",
        "contains\x00nul",
        "contains\x07control",
        "text_body",
        "input_text",
        "audio_path",
        "prompt_body",
    ],
)
def test_audit_append_rejects_unsafe_reason_shapes_without_echo(db, reason):
    event = replace(AuditEventInput.synthetic(), reason_text=reason)

    with pytest.raises(DomainError) as error:
        with transaction(db):
            AuditRepository(db).append(event)

    assert error.value.code == "AUDIT_REASON_PRIVATE"
    assert reason not in error.value.message
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


@pytest.mark.parametrize("reason", [None, False, 1, "", "\u3000\t\n"])
def test_audit_append_requires_exact_practical_reason_string(db, reason):
    event = replace(AuditEventInput.synthetic(), reason_text=reason)

    with pytest.raises(DomainError) as error:
        with transaction(db):
            AuditRepository(db).append(event)

    assert error.value.code == "AUDIT_SCALAR_INVALID"
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_audit_append_rejects_retained_body_exact_and_surrounded(db):
    body = "Private retained transcript body must not become audit rationale."
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        """
        INSERT INTO transcript_segments(
            id, video_id, chunk_id, segment_no, start_ms, end_ms,
            text_body, text_sha256, anonymous_speaker_id,
            transcript_created_at, expires_at, text_deleted_at
        ) VALUES (1, 1, 1, 0, 0, 1000, ?, ?, 'private-speaker',
                  '2026-08-15T00:00:00.000000Z', NULL, NULL)
        """,
        (body, sha256_text(body)),
    )
    db.execute("PRAGMA foreign_keys=ON")

    for reason in (body, f"Prefix {body} suffix"):
        with pytest.raises(DomainError) as error:
            with transaction(db):
                AuditRepository(db).append(
                    replace(AuditEventInput.synthetic(), reason_text=reason)
                )
        assert error.value.code == "AUDIT_REASON_PRIVATE"
        assert reason not in error.value.message

    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_audit_append_rejects_retained_analysis_input_exact_and_surrounded(db):
    body = "Private frozen analysis input must not become audit rationale."
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        """
        INSERT INTO analysis_input_snapshots(
            id, run_id, input_text, metadata_json, input_sha256,
            snapshot_created_at, expires_at, text_deleted_at
        ) VALUES (1, 1, ?, '{}', ?,
                  '2026-08-15T00:00:00.000000Z', NULL, NULL)
        """,
        (body, sha256_text(body)),
    )
    db.execute("PRAGMA foreign_keys=ON")

    for reason in (body, f"Prefix {body} suffix"):
        with pytest.raises(DomainError) as error:
            with transaction(db):
                AuditRepository(db).append(
                    replace(AuditEventInput.synthetic(), reason_text=reason)
                )
        assert error.value.code == "AUDIT_REASON_PRIVATE"

    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_audit_append_rejects_deleted_body_by_hash_but_allows_safe_reasons(db):
    body = "Private deleted transcript body remains protected by its hash."
    body_hash = sha256_text(body)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        """
        INSERT INTO transcript_segments(
            id, video_id, chunk_id, segment_no, start_ms, end_ms,
            text_body, text_sha256, anonymous_speaker_id,
            transcript_created_at, expires_at, text_deleted_at
        ) VALUES (1, 1, 1, 0, 0, 1000, NULL, ?, 'private-speaker',
                  '2026-08-15T00:00:00.000000Z', NULL,
                  '2026-08-16T00:00:00.000000Z')
        """,
        (body_hash,),
    )
    db.execute("PRAGMA foreign_keys=ON")

    for reason in (body, f"\u3000{body}\u3000"):
        with pytest.raises(DomainError) as error:
            with transaction(db):
                AuditRepository(db).append(
                    replace(AuditEventInput.synthetic(), reason_text=reason)
                )
        assert error.value.code == "AUDIT_REASON_PRIVATE"

    for reason in (
        "予測対象の解釈を確認したため修正",
        "Private deleted transcript",
    ):
        with transaction(db):
            AuditRepository(db).append(
                replace(AuditEventInput.synthetic(), reason_text=reason)
            )

    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2


def test_audit_reason_accepts_exact_unicode_code_point_limit(db):
    reason = "理" * 256

    with transaction(db):
        event_id = AuditRepository(db).append(
            replace(AuditEventInput.synthetic(), reason_text=reason)
        )

    assert db.execute(
        "SELECT reason_text FROM audit_events WHERE id=?", (event_id,)
    ).fetchone()[0] == reason

import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
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

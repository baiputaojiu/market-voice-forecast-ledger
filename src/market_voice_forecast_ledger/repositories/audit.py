import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.audit import validate_audit_payload


@dataclass(frozen=True, slots=True)
class AuditEventInput:
    entity_type: str
    entity_id: str
    scope_id: int | None
    operation: str
    actor_kind: str
    reason_code: str
    reason_text: str
    before: object | None
    after: object | None
    created_at: datetime

    @classmethod
    def synthetic(cls) -> "AuditEventInput":
        return cls(
            entity_type="synthetic_entity",
            entity_id="synthetic-1",
            scope_id=None,
            operation="create",
            actor_kind="system",
            reason_code="synthetic_test",
            reason_text="Synthetic audit event",
            before=None,
            after={"entity_id": "synthetic-1"},
            created_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: int
    entity_type: str
    entity_id: str
    scope_id: int | None
    operation: str
    actor_kind: str
    reason_code: str
    reason_text: str
    before: object | None
    after: object | None
    created_at: datetime


class AuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: AuditEventInput) -> int:
        if not self._conn.in_transaction:
            raise DomainError(
                "AUDIT_TRANSACTION_REQUIRED",
                "audit append requires an active caller transaction",
            )
        validate_audit_payload(event.before)
        validate_audit_payload(event.after)
        cursor = self._conn.execute(
            """
            INSERT INTO audit_events(
                entity_type,
                entity_id,
                scope_id,
                operation,
                actor_kind,
                reason_code,
                reason_text,
                before_json,
                after_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.entity_type,
                event.entity_id,
                event.scope_id,
                event.operation,
                event.actor_kind,
                event.reason_code,
                event.reason_text,
                _json_or_none(event.before),
                _json_or_none(event.after),
                utc_iso(event.created_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("audit insert did not return an id")
        return cursor.lastrowid

    def list_for_entity(
        self, entity_type: str, entity_id: str
    ) -> tuple[AuditEvent, ...]:
        rows = self._conn.execute(
            """
            SELECT
                id,
                entity_type,
                entity_id,
                scope_id,
                operation,
                actor_kind,
                reason_code,
                reason_text,
                before_json,
                after_json,
                created_at
            FROM audit_events
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY id
            """,
            (entity_type, entity_id),
        )
        return tuple(_event_from_row(row) for row in rows)


def _json_or_none(value: object | None) -> str | None:
    return None if value is None else canonical_json(value)


def _event_from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        scope_id=row["scope_id"],
        operation=row["operation"],
        actor_kind=row["actor_kind"],
        reason_code=row["reason_code"],
        reason_text=row["reason_text"],
        before=_load_json(row["before_json"]),
        after=_load_json(row["after_json"]),
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
    )


def _load_json(value: str | None) -> object | None:
    return None if value is None else json.loads(value)

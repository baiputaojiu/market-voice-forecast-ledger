import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.sources import SubjectRecord


class SourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_subject(
        self,
        name: str,
        aliases: Sequence[str] = (),
        *,
        created_at: datetime | None = None,
    ) -> int:
        timestamp = utc_iso(created_at or datetime.now(timezone.utc))
        self._conn.execute(
            """
            INSERT INTO analysis_subjects(
                canonical_name, is_active, created_at
            ) VALUES (?, 1, ?)
            ON CONFLICT(canonical_name) DO NOTHING
            """,
            (name, timestamp),
        )
        row = self._conn.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name=?", (name,)
        ).fetchone()
        if row is None:
            raise RuntimeError("subject insert did not return an id")
        subject_id = row["id"]
        self._conn.executemany(
            """
            INSERT INTO subject_aliases(subject_id, alias)
            VALUES (?, ?)
            ON CONFLICT(subject_id, alias) DO NOTHING
            """,
            ((subject_id, alias) for alias in aliases),
        )
        return subject_id

    def get_subject(self, subject_id: int) -> SubjectRecord:
        row = self._conn.execute(
            "SELECT * FROM analysis_subjects WHERE id=?", (subject_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"subject not found: {subject_id}")
        aliases = tuple(
            item["alias"]
            for item in self._conn.execute(
                "SELECT alias FROM subject_aliases WHERE subject_id=? ORDER BY id",
                (subject_id,),
            )
        )
        return SubjectRecord(
            id=row["id"],
            canonical_name=row["canonical_name"],
            is_active=bool(row["is_active"]),
            created_at=_parse_utc(row["created_at"]),
            aliases=aliases,
        )

    def get_subject_by_name(self, name: str) -> SubjectRecord:
        row = self._conn.execute(
            """
            SELECT DISTINCT subject.id
            FROM analysis_subjects AS subject
            LEFT JOIN subject_aliases AS alias ON alias.subject_id=subject.id
            WHERE subject.canonical_name=? OR alias.alias=?
            """,
            (name, name),
        ).fetchone()
        if row is None:
            raise LookupError(f"subject not found: {name}")
        return self.get_subject(row["id"])

    def count_subjects(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM analysis_subjects"
        ).fetchone()[0]


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

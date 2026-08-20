import sqlite3
from collections.abc import Sequence
from datetime import datetime

from market_voice_forecast_ledger.domain.common import sha256_text, utc_iso
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.speakers import (
    SpeakerAssignment,
    SpeakerThresholdConfig,
    TranscriptSegment,
)


class SpeakerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_chunk(
        self,
        video_id: int,
        chunk_no: int,
        start_ms: int,
        end_ms: int,
        input_hash: str,
        output_hash: str,
        status: UnitStatus,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO transcription_chunks(
                video_id, chunk_no, start_ms, end_ms, input_hash, output_hash, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                chunk_no,
                start_ms,
                end_ms,
                input_hash,
                output_hash,
                status.value,
            ),
        )
        return cursor.lastrowid

    def add_segment(
        self,
        video_id: int,
        chunk_id: int,
        segment_no: int,
        start_ms: int,
        end_ms: int,
        text_body: str,
        anonymous_speaker_id: str,
        transcript_created_at: datetime,
        expires_at: datetime | None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO transcript_segments(
                video_id,
                chunk_id,
                segment_no,
                start_ms,
                end_ms,
                text_body,
                text_sha256,
                anonymous_speaker_id,
                transcript_created_at,
                expires_at,
                text_deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                video_id,
                chunk_id,
                segment_no,
                start_ms,
                end_ms,
                text_body,
                sha256_text(text_body),
                anonymous_speaker_id,
                utc_iso(transcript_created_at),
                None if expires_at is None else utc_iso(expires_at),
            ),
        )
        return cursor.lastrowid

    def get_segment(self, segment_id: int) -> TranscriptSegment:
        row = self._conn.execute(
            "SELECT * FROM transcript_segments WHERE id = ?", (segment_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"transcript segment not found: {segment_id}")
        return _segment_from_row(row)

    def get_segment_video_id(self, segment_id: int) -> int:
        row = self._conn.execute(
            "SELECT video_id FROM transcript_segments WHERE id=?",
            (segment_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"transcript segment not found: {segment_id}")
        return row["video_id"]

    def list_segments_for_video(self, video_id: int) -> tuple[TranscriptSegment, ...]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM transcript_segments
            WHERE video_id = ?
            ORDER BY segment_no
            """,
            (video_id,),
        ).fetchall()
        return tuple(_segment_from_row(row) for row in rows)

    def add_threshold_config(
        self,
        config: SpeakerThresholdConfig,
        created_at: datetime,
        is_active: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO speaker_threshold_configs(
                version,
                model_name,
                model_version,
                subject_operator,
                subject_boundary,
                interviewer_operator,
                interviewer_boundary,
                created_at,
                is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config.version,
                config.model_name,
                config.model_version,
                config.subject_rule.operator,
                config.subject_rule.boundary,
                config.interviewer_rule.operator,
                config.interviewer_rule.boundary,
                utc_iso(created_at),
                int(is_active),
            ),
        )

    def get_active_threshold_config(self) -> SpeakerThresholdConfig:
        row = self._conn.execute(
            """
            SELECT *
            FROM speaker_threshold_configs
            WHERE is_active = 1
            """
        ).fetchone()
        if row is None:
            raise LookupError("active speaker threshold config not found")
        from market_voice_forecast_ledger.domain.speakers import ScoreRule

        return SpeakerThresholdConfig(
            version=row["version"],
            model_name=row["model_name"],
            model_version=row["model_version"],
            subject_rule=ScoreRule(row["subject_operator"], row["subject_boundary"]),
            interviewer_rule=ScoreRule(
                row["interviewer_operator"], row["interviewer_boundary"]
            ),
        )

    def save_assignment(self, assignment: SpeakerAssignment) -> None:
        self._conn.execute(
            """
            INSERT INTO speaker_assignments(
                segment_id,
                assignment_kind,
                assigned_subject_id,
                assignment_origin,
                raw_match_score,
                model_name,
                model_version,
                threshold_config_version,
                evidence_hash,
                assigned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(segment_id) DO UPDATE SET
                assignment_kind = excluded.assignment_kind,
                assigned_subject_id = excluded.assigned_subject_id,
                assignment_origin = excluded.assignment_origin,
                raw_match_score = excluded.raw_match_score,
                model_name = excluded.model_name,
                model_version = excluded.model_version,
                threshold_config_version = excluded.threshold_config_version,
                evidence_hash = excluded.evidence_hash,
                assigned_at = excluded.assigned_at
            """,
            (
                assignment.segment_id,
                assignment.assignment_kind.value,
                assignment.assigned_subject_id,
                assignment.assignment_origin.value,
                assignment.raw_match_score,
                assignment.model_name,
                assignment.model_version,
                assignment.threshold_config_version,
                assignment.evidence_hash,
                utc_iso(assignment.assigned_at),
            ),
        )

    def list_assignments(
        self, segment_ids: Sequence[int]
    ) -> tuple[SpeakerAssignment, ...]:
        if not segment_ids:
            return ()
        placeholders = ", ".join("?" for _ in segment_ids)
        rows = self._conn.execute(
            f"SELECT * FROM speaker_assignments WHERE segment_id IN ({placeholders})",
            tuple(segment_ids),
        ).fetchall()
        by_segment_id = {
            row["segment_id"]: _assignment_from_row(row) for row in rows
        }
        return tuple(
            by_segment_id[segment_id]
            for segment_id in segment_ids
            if segment_id in by_segment_id
        )

    def get_assignment(self, segment_id: int) -> SpeakerAssignment:
        row = self._conn.execute(
            "SELECT * FROM speaker_assignments WHERE segment_id=?",
            (segment_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"speaker assignment not found: {segment_id}")
        return _assignment_from_row(row)


def _segment_from_row(row: sqlite3.Row) -> TranscriptSegment:
    return TranscriptSegment(
        id=row["id"],
        video_id=row["video_id"],
        chunk_id=row["chunk_id"],
        segment_no=row["segment_no"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        text_body=row["text_body"],
        text_sha256=row["text_sha256"],
        anonymous_speaker_id=row["anonymous_speaker_id"],
        transcript_created_at=_parse_utc(row["transcript_created_at"]),
        expires_at=_parse_optional_utc(row["expires_at"]),
        text_deleted_at=_parse_optional_utc(row["text_deleted_at"]),
    )


def _assignment_from_row(row: sqlite3.Row) -> SpeakerAssignment:
    return SpeakerAssignment(
        segment_id=row["segment_id"],
        assignment_kind=AssignmentKind(row["assignment_kind"]),
        assigned_subject_id=row["assigned_subject_id"],
        assignment_origin=AssignmentOrigin(row["assignment_origin"]),
        raw_match_score=row["raw_match_score"],
        model_name=row["model_name"],
        model_version=row["model_version"],
        threshold_config_version=row["threshold_config_version"],
        evidence_hash=row["evidence_hash"],
        assigned_at=_parse_utc(row["assigned_at"]),
        id=row["id"],
    )


def _parse_optional_utc(value: str | None) -> datetime | None:
    return None if value is None else _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

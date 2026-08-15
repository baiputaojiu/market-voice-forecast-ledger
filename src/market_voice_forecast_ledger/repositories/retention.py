import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from market_voice_forecast_ledger.domain.common import sha256_text, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_AUDIO_ERROR_CODES = {
    "AUDIO_PATH_OUTSIDE_TEMP_ROOT",
    "AUDIO_DELETE_PERMISSION",
    "AUDIO_DELETE_OS_ERROR",
}


@dataclass(frozen=True, slots=True)
class TranscriptDeletionTarget:
    id: int
    video_id: int
    text_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisInputDeletionTarget:
    id: int
    run_id: int
    input_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TextDeletionTargets:
    transcripts: tuple[TranscriptDeletionTarget, ...]
    analysis_inputs: tuple[AnalysisInputDeletionTarget, ...]
    video_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StoredDeletionPreview:
    token: str
    cutoff_utc: str
    retention_days: int | None
    target_fingerprint: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LocalArtifact:
    id: int
    kind: str
    local_path: Path
    status: str
    retry_count: int
    safe_error_code: str | None
    created_at: datetime
    deleted_at: datetime | None


class RetentionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_retention_days(self) -> int | None:
        row = self._conn.execute(
            "SELECT retention_days FROM retention_settings WHERE id=1"
        ).fetchone()
        if row is None:
            raise DomainError(
                "RETENTION_SETTINGS_INVALID",
                "retention settings are unavailable",
            )
        return row["retention_days"]

    def set_retention_days(self, days: int | None) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            "UPDATE retention_settings SET retention_days=? WHERE id=1",
            (days,),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "RETENTION_SETTINGS_INVALID",
                "retention settings are unavailable",
            )

    def select_text_targets(self, cutoff: datetime) -> TextDeletionTargets:
        cutoff_utc = _require_utc_datetime(cutoff, "RETENTION_TIME_INVALID")
        transcript_targets: list[TranscriptDeletionTarget] = []
        for row in self._conn.execute(
            """
            SELECT
                id,
                video_id,
                text_body,
                text_sha256,
                transcript_created_at,
                expires_at,
                text_deleted_at
            FROM transcript_segments
            ORDER BY id
            """
        ):
            created_at = _parse_utc(row["transcript_created_at"])
            _parse_optional_utc(row["expires_at"])
            deleted_at = _parse_optional_utc(row["text_deleted_at"])
            body = row["text_body"]
            digest = _require_sha256(row["text_sha256"])
            _validate_body_state(body, deleted_at, digest)
            if body is not None and created_at <= cutoff_utc:
                transcript_targets.append(
                    TranscriptDeletionTarget(
                        id=row["id"],
                        video_id=row["video_id"],
                        text_sha256=digest,
                        created_at=created_at,
                    )
                )

        analysis_targets: list[AnalysisInputDeletionTarget] = []
        for row in self._conn.execute(
            """
            SELECT
                id,
                run_id,
                input_text,
                input_sha256,
                snapshot_created_at,
                expires_at,
                text_deleted_at
            FROM analysis_input_snapshots
            ORDER BY id
            """
        ):
            created_at = _parse_utc(row["snapshot_created_at"])
            _parse_optional_utc(row["expires_at"])
            deleted_at = _parse_optional_utc(row["text_deleted_at"])
            body = row["input_text"]
            digest = _require_sha256(row["input_sha256"])
            _validate_body_state(body, deleted_at, digest)
            if body is not None and created_at <= cutoff_utc:
                analysis_targets.append(
                    AnalysisInputDeletionTarget(
                        id=row["id"],
                        run_id=row["run_id"],
                        input_sha256=digest,
                        created_at=created_at,
                    )
                )

        video_ids = {target.video_id for target in transcript_targets}
        run_ids = tuple(target.run_id for target in analysis_targets)
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            video_ids.update(
                row["video_id"]
                for row in self._conn.execute(
                    f"""
                    SELECT DISTINCT video_id
                    FROM analysis_run_segments
                    WHERE run_id IN ({placeholders})
                    """,
                    run_ids,
                )
            )
        return TextDeletionTargets(
            transcripts=tuple(transcript_targets),
            analysis_inputs=tuple(analysis_targets),
            video_ids=tuple(sorted(video_ids)),
        )

    def replace_preview(
        self,
        *,
        token: str,
        cutoff_utc: str,
        retention_days: int | None,
        target_fingerprint: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self._require_transaction()
        self._conn.execute("DELETE FROM retention_deletion_previews")
        self._conn.execute(
            """
            INSERT INTO retention_deletion_previews(
                id,
                token,
                cutoff_utc,
                retention_days,
                target_fingerprint,
                created_at,
                expires_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                cutoff_utc,
                retention_days,
                target_fingerprint,
                utc_iso(created_at),
                utc_iso(expires_at),
            ),
        )

    def get_preview(self, token: str) -> StoredDeletionPreview | None:
        row = self._conn.execute(
            "SELECT * FROM retention_deletion_previews WHERE id=1 AND token=?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        return StoredDeletionPreview(
            token=row["token"],
            cutoff_utc=row["cutoff_utc"],
            retention_days=row["retention_days"],
            target_fingerprint=_require_sha256(row["target_fingerprint"]),
            created_at=_parse_utc(row["created_at"]),
            expires_at=_parse_utc(row["expires_at"]),
        )

    def consume_preview(self, token: str) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            "DELETE FROM retention_deletion_previews WHERE id=1 AND token=?",
            (token,),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "DELETION_PREVIEW_NOT_CURRENT",
                "deletion preview is not current",
            )

    def delete_transcript_targets(
        self, targets: tuple[TranscriptDeletionTarget, ...], deleted_at: datetime
    ) -> None:
        self._require_transaction()
        deleted_at_text = utc_iso(deleted_at)
        for target in targets:
            cursor = self._conn.execute(
                """
                UPDATE transcript_segments
                SET text_body=NULL, text_deleted_at=?
                WHERE id=? AND text_body IS NOT NULL AND text_sha256=?
                """,
                (deleted_at_text, target.id, target.text_sha256),
            )
            if cursor.rowcount != 1:
                raise DomainError(
                    "DELETION_TARGET_DRIFT",
                    "transcript deletion target changed after preview",
                )

    def delete_analysis_input_targets(
        self,
        targets: tuple[AnalysisInputDeletionTarget, ...],
        deleted_at: datetime,
    ) -> None:
        self._require_transaction()
        deleted_at_text = utc_iso(deleted_at)
        for target in targets:
            cursor = self._conn.execute(
                """
                UPDATE analysis_input_snapshots
                SET input_text=NULL, text_deleted_at=?
                WHERE id=? AND input_text IS NOT NULL AND input_sha256=?
                """,
                (deleted_at_text, target.id, target.input_sha256),
            )
            if cursor.rowcount != 1:
                raise DomainError(
                    "DELETION_TARGET_DRIFT",
                    "analysis input deletion target changed after preview",
                )

    def add_audio_artifact(
        self, local_path: Path, *, created_at: datetime | None = None
    ) -> int:
        if not isinstance(local_path, Path):
            raise DomainError(
                "AUDIO_ARTIFACT_INVALID", "audio artifact path is invalid"
            )
        effective_created_at = (
            datetime.now(timezone.utc) if created_at is None else created_at
        )
        created_at_utc = _require_utc_datetime(
            effective_created_at, "AUDIO_ARTIFACT_INVALID"
        )
        cursor = self._conn.execute(
            """
            INSERT INTO local_artifacts(
                kind,
                local_path,
                status,
                retry_count,
                safe_error_code,
                created_at,
                deleted_at
            ) VALUES ('audio', ?, 'pending', 0, NULL, ?, NULL)
            """,
            (str(local_path), utc_iso(created_at_utc)),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("audio artifact insert did not return an id")
        return cursor.lastrowid

    def get_audio_artifact(self, artifact_id: int) -> LocalArtifact:
        if type(artifact_id) is not int or artifact_id <= 0:
            raise DomainError(
                "AUDIO_ARTIFACT_NOT_FOUND", "audio artifact was not found"
            )
        row = self._conn.execute(
            "SELECT * FROM local_artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "AUDIO_ARTIFACT_NOT_FOUND", "audio artifact was not found"
            )
        artifact = LocalArtifact(
            id=row["id"],
            kind=row["kind"],
            local_path=Path(row["local_path"]),
            status=row["status"],
            retry_count=row["retry_count"],
            safe_error_code=row["safe_error_code"],
            created_at=_parse_utc(row["created_at"]),
            deleted_at=_parse_optional_utc(row["deleted_at"]),
        )
        _validate_artifact(artifact)
        return artifact

    def record_audio_failure(self, artifact_id: int, error_code: str) -> int:
        self._require_transaction()
        if error_code not in _AUDIO_ERROR_CODES:
            raise DomainError(
                "AUDIO_ERROR_CODE_INVALID", "audio cleanup error code is invalid"
            )
        cursor = self._conn.execute(
            """
            UPDATE local_artifacts
            SET
                status='delete_failed',
                retry_count=retry_count + 1,
                safe_error_code=?,
                deleted_at=NULL
            WHERE id=? AND status IN ('pending', 'delete_failed')
            """,
            (error_code, artifact_id),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "AUDIO_ARTIFACT_STATE_INVALID",
                "audio artifact cannot record a cleanup failure",
            )
        return self.get_audio_artifact(artifact_id).retry_count

    def mark_audio_deleted(
        self, artifact_id: int, deleted_at: datetime
    ) -> LocalArtifact:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE local_artifacts
            SET status='deleted', safe_error_code=NULL, deleted_at=?
            WHERE id=? AND status IN ('pending', 'delete_failed')
            """,
            (utc_iso(deleted_at), artifact_id),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "AUDIO_ARTIFACT_STATE_INVALID",
                "audio artifact cannot be marked deleted",
            )
        return self.get_audio_artifact(artifact_id)

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "RETENTION_TRANSACTION_REQUIRED",
                "retention mutation requires a caller-owned transaction",
            )


def _validate_body_state(
    body: object, deleted_at: datetime | None, digest: str
) -> None:
    if body is None:
        if deleted_at is None:
            raise DomainError(
                "RETENTION_STORED_STATE_INVALID",
                "stored private text state is invalid",
            )
        return
    if not isinstance(body, str) or deleted_at is not None:
        raise DomainError(
            "RETENTION_STORED_STATE_INVALID",
            "stored private text state is invalid",
        )
    if sha256_text(body) != digest:
        raise DomainError(
            "RETENTION_STORED_HASH_INVALID",
            "stored private text hash is invalid",
        )


def _validate_artifact(artifact: LocalArtifact) -> None:
    if artifact.kind != "audio" or artifact.status not in {
        "pending",
        "delete_failed",
        "deleted",
    }:
        raise DomainError(
            "AUDIO_ARTIFACT_STATE_INVALID", "audio artifact state is invalid"
        )
    if artifact.safe_error_code is not None and (
        artifact.safe_error_code not in _AUDIO_ERROR_CODES
    ):
        raise DomainError(
            "AUDIO_ARTIFACT_STATE_INVALID", "audio artifact state is invalid"
        )


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainError(
            "RETENTION_STORED_HASH_INVALID", "stored private text hash is invalid"
        )
    return value


def _parse_optional_utc(value: object) -> datetime | None:
    return None if value is None else _parse_utc(value)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or _UTC_TEXT.fullmatch(value) is None:
        raise DomainError(
            "RETENTION_STORED_TIME_INVALID", "stored UTC time is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as cause:
        raise DomainError(
            "RETENTION_STORED_TIME_INVALID", "stored UTC time is invalid"
        ) from cause
    if utc_iso(parsed) != value:
        raise DomainError(
            "RETENTION_STORED_TIME_INVALID", "stored UTC time is invalid"
        )
    return parsed


def _require_utc_datetime(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainError(code, "timezone-aware datetime is required")
    try:
        if value.utcoffset() is None:
            raise ValueError("UTC offset is unavailable")
        return value.astimezone(timezone.utc)
    except Exception as cause:
        raise DomainError(code, "timezone-aware datetime is required") from cause

import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.retention import (
    RetentionRepository,
    TextDeletionTargets,
)


ALLOWED_RETENTION_DAYS = (30, 90, 180, 365, None)
_PREVIEW_LIFETIME = timedelta(minutes=10)
_TOKEN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    days: int | None

    def __post_init__(self) -> None:
        _validate_retention_days(self.days)


@dataclass(frozen=True, slots=True)
class DeleteTextCommand:
    cutoff: datetime
    preview_token: str | None = None


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    affected_video_count: int
    affected_transcript_count: int
    affected_analysis_input_count: int
    full_reproduction_will_be_lost: bool
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeletionResult:
    affected_video_count: int
    deleted_transcript_count: int
    deleted_analysis_input_count: int
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class AudioDeletionResult:
    artifact_id: int
    deleted: bool
    already_absent: bool
    retryable: bool
    error_code: str | None
    retry_count: int
    deleted_at: datetime | None


def expiry_for(created_at: datetime, days: int | None) -> datetime | None:
    _validate_retention_days(days)
    created_at_utc = _require_utc_datetime(created_at)
    if days is None:
        return None
    return _shift_retention_time(created_at_utc, timedelta(days=days))


def is_safe_audio_path(root: Path, candidate: Path) -> bool:
    return _resolved_safe_audio_path(root, candidate) is not None


class RetentionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._retention = RetentionRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._authorized_audio_transition: tuple[object, ...] | None = None
        self._conn.create_function(
            "retention_audio_transition_authorized",
            7,
            self._audio_transition_authorized,
        )

    def policy(self) -> RetentionPolicy:
        return RetentionPolicy(self._retention.get_retention_days())

    def set_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        if type(policy) is not RetentionPolicy:
            raise DomainError(
                "RETENTION_VALUE_INVALID", "unsupported retention period"
            )
        try:
            with transaction(self._conn):
                self._retention.set_retention_days(policy.days)
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "RETENTION_SETTINGS_INVALID",
                "retention settings could not be changed",
            ) from cause
        return self.policy()

    def preview_text_deletion(
        self, command: DeleteTextCommand
    ) -> DeletionPreview:
        cutoff = self._command_cutoff(command)
        now = _require_utc_datetime(self._clock())
        policy = self.policy()
        targets = self._retention.select_text_targets(cutoff)
        target_fingerprint = _target_fingerprint(targets)
        expires_at = _shift_retention_time(now, _PREVIEW_LIFETIME)
        token = _preview_token(
            cutoff,
            policy,
            targets,
            created_at=now,
            expires_at=expires_at,
        )
        try:
            with transaction(self._conn):
                self._retention.replace_preview(
                    token=token,
                    cutoff_utc=utc_iso(cutoff),
                    retention_days=policy.days,
                    target_fingerprint=target_fingerprint,
                    created_at=now,
                    expires_at=expires_at,
                )
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "DELETION_PREVIEW_FAILED",
                "private text deletion preview could not be stored",
            ) from cause
        return DeletionPreview(
            affected_video_count=len(targets.video_ids),
            affected_transcript_count=len(targets.transcripts),
            affected_analysis_input_count=len(targets.analysis_inputs),
            full_reproduction_will_be_lost=bool(
                targets.transcripts or targets.analysis_inputs
            ),
            token=token,
            expires_at=expires_at,
        )

    def delete_text(self, command: DeleteTextCommand) -> DeletionResult:
        cutoff = self._command_cutoff(command)
        token = _require_preview_token(command.preview_token)
        try:
            with transaction(self._conn):
                now = _require_utc_datetime(self._clock())
                preview = self._retention.get_preview(token)
                if preview is None:
                    raise DomainError(
                        "DELETION_PREVIEW_NOT_CURRENT",
                        "deletion preview is not current",
                    )
                if now < preview.created_at or now >= preview.expires_at:
                    raise DomainError(
                        "DELETION_PREVIEW_EXPIRED",
                        "deletion preview has expired",
                    )
                policy = self.policy()
                if preview.cutoff_utc != utc_iso(cutoff):
                    raise DomainError(
                        "DELETION_PREVIEW_COMMAND_MISMATCH",
                        "deletion preview does not match the command",
                    )
                if preview.retention_days != policy.days:
                    raise DomainError(
                        "DELETION_PREVIEW_POLICY_CHANGED",
                        "retention policy changed after preview",
                    )
                targets = self._retention.select_text_targets(cutoff)
                recomputed_token = _preview_token(
                    cutoff,
                    policy,
                    targets,
                    created_at=preview.created_at,
                    expires_at=preview.expires_at,
                )
                if (
                    _target_fingerprint(targets) != preview.target_fingerprint
                    or recomputed_token != token
                ):
                    raise DomainError(
                        "DELETION_TARGET_DRIFT",
                        "private text deletion targets changed after preview",
                    )
                result = self._delete_targets(
                    targets,
                    deleted_at=now,
                    actor_kind="user",
                    reason_code="MANUAL_PRIVATE_TEXT_DELETION",
                )
                self._retention.consume_preview(token)
                return result
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "TEXT_DELETION_FAILED", "private text deletion failed"
            ) from cause

    def purge_expired(self, now: datetime) -> DeletionResult:
        now_utc = _require_utc_datetime(now)
        try:
            with transaction(self._conn):
                policy = self.policy()
                if policy.days is None:
                    return DeletionResult(0, 0, 0, None)
                cutoff = _shift_retention_time(
                    now_utc, -timedelta(days=policy.days)
                )
                targets = self._retention.select_text_targets(cutoff)
                return self._delete_targets(
                    targets,
                    deleted_at=now_utc,
                    actor_kind="system",
                    reason_code="RETENTION_EXPIRY_PRIVATE_TEXT_DELETION",
                )
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "TEXT_DELETION_FAILED", "private text deletion failed"
            ) from cause

    def delete_audio(self, artifact_id: int) -> AudioDeletionResult:
        artifact = self._retention.get_audio_artifact(artifact_id)
        if artifact.kind != "audio":
            raise DomainError(
                "AUDIO_ARTIFACT_KIND_INVALID",
                "artifact is not an audio artifact",
            )
        if artifact.status == "deleted":
            return AudioDeletionResult(
                artifact_id=artifact.id,
                deleted=True,
                already_absent=True,
                retryable=False,
                error_code=None,
                retry_count=artifact.retry_count,
                deleted_at=artifact.deleted_at,
            )

        resolved, resolution_error = _resolve_audio_path(
            self._settings.temp_audio_dir, artifact.local_path
        )
        if resolved is None:
            return self._record_audio_failure(
                artifact.id,
                resolution_error or "AUDIO_PATH_OUTSIDE_TEMP_ROOT",
            )

        already_absent = False
        try:
            resolved.unlink()
        except FileNotFoundError:
            already_absent = True
        except PermissionError:
            return self._record_audio_failure(
                artifact.id, "AUDIO_DELETE_PERMISSION"
            )
        except OSError:
            return self._record_audio_failure(
                artifact.id, "AUDIO_DELETE_OS_ERROR"
            )
        except ValueError:
            return self._record_audio_failure(
                artifact.id, "AUDIO_DELETE_OS_ERROR"
            )

        deleted_at = _require_utc_datetime(self._clock())
        try:
            with transaction(self._conn):
                current = self._retention.get_audio_artifact(artifact.id)
                self._conn.create_function(
                    "retention_audio_transition_authorized",
                    7,
                    self._audio_transition_authorized,
                )
                self._authorized_audio_transition = (
                    current.id,
                    current.status,
                    current.retry_count,
                    "deleted",
                    current.retry_count,
                    None,
                    utc_iso(deleted_at),
                )
                try:
                    deleted = self._retention.mark_audio_deleted(
                        artifact.id, deleted_at
                    )
                finally:
                    self._authorized_audio_transition = None
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "AUDIO_CLEANUP_STATE_FAILED",
                "audio cleanup state could not be stored",
            ) from cause
        return AudioDeletionResult(
            artifact_id=deleted.id,
            deleted=True,
            already_absent=already_absent,
            retryable=False,
            error_code=None,
            retry_count=deleted.retry_count,
            deleted_at=deleted.deleted_at,
        )

    def _record_audio_failure(
        self, artifact_id: int, error_code: str
    ) -> AudioDeletionResult:
        try:
            with transaction(self._conn):
                current = self._retention.get_audio_artifact(artifact_id)
                self._conn.create_function(
                    "retention_audio_transition_authorized",
                    7,
                    self._audio_transition_authorized,
                )
                self._authorized_audio_transition = (
                    current.id,
                    current.status,
                    current.retry_count,
                    "delete_failed",
                    current.retry_count + 1,
                    error_code,
                    None,
                )
                try:
                    retry_count = self._retention.record_audio_failure(
                        artifact_id, error_code
                    )
                finally:
                    self._authorized_audio_transition = None
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "AUDIO_CLEANUP_STATE_FAILED",
                "audio cleanup state could not be stored",
            ) from cause
        return AudioDeletionResult(
            artifact_id=artifact_id,
            deleted=False,
            already_absent=False,
            retryable=True,
            error_code=error_code,
            retry_count=retry_count,
            deleted_at=None,
        )

    def _delete_targets(
        self,
        targets: TextDeletionTargets,
        *,
        deleted_at: datetime,
        actor_kind: str,
        reason_code: str,
    ) -> DeletionResult:
        if not self._conn.in_transaction:
            raise DomainError(
                "RETENTION_TRANSACTION_REQUIRED",
                "private text deletion requires an active transaction",
            )
        if not targets.transcripts and not targets.analysis_inputs:
            return DeletionResult(0, 0, 0, None)
        self._retention.delete_transcript_targets(
            targets.transcripts, deleted_at
        )
        self._retention.delete_analysis_input_targets(
            targets.analysis_inputs, deleted_at
        )
        target_fingerprint = _target_fingerprint(targets)
        self._audit.append(
            AuditEventInput(
                entity_type="private_text_retention",
                entity_id=target_fingerprint,
                scope_id=None,
                operation="private_text_deleted",
                actor_kind=actor_kind,
                reason_code=reason_code,
                reason_text="Private text bodies deleted",
                before={
                    "analysis_input_hashes": [
                        target.input_sha256
                        for target in targets.analysis_inputs
                    ],
                    "analysis_input_ids": [
                        target.id for target in targets.analysis_inputs
                    ],
                    "transcript_hashes": [
                        target.text_sha256 for target in targets.transcripts
                    ],
                    "transcript_ids": [
                        target.id for target in targets.transcripts
                    ],
                    "video_ids": list(targets.video_ids),
                },
                after={
                    "affected_video_count": len(targets.video_ids),
                    "deleted_analysis_input_count": len(
                        targets.analysis_inputs
                    ),
                    "deleted_at": utc_iso(deleted_at),
                    "deleted_transcript_count": len(targets.transcripts),
                    "target_fingerprint": target_fingerprint,
                },
                created_at=deleted_at,
            )
        )
        return DeletionResult(
            affected_video_count=len(targets.video_ids),
            deleted_transcript_count=len(targets.transcripts),
            deleted_analysis_input_count=len(targets.analysis_inputs),
            deleted_at=deleted_at,
        )

    @staticmethod
    def _command_cutoff(command: DeleteTextCommand) -> datetime:
        if type(command) is not DeleteTextCommand:
            raise DomainError(
                "DELETION_COMMAND_INVALID", "deletion command is invalid"
            )
        return _require_utc_datetime(command.cutoff)

    def _audio_transition_authorized(
        self,
        artifact_id: object,
        old_status: object,
        old_retry_count: object,
        new_status: object,
        new_retry_count: object,
        new_error_code: object,
        new_deleted_at: object,
    ) -> int:
        attempted = (
            artifact_id,
            old_status,
            old_retry_count,
            new_status,
            new_retry_count,
            new_error_code,
            new_deleted_at,
        )
        return int(attempted == self._authorized_audio_transition)


def _validate_retention_days(days: object) -> None:
    if days is None:
        return
    if type(days) is not int or days not in (30, 90, 180, 365):
        raise DomainError(
            "RETENTION_VALUE_INVALID", "unsupported retention period"
        )


def _require_utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainError(
            "RETENTION_TIME_INVALID", "timezone-aware datetime is required"
        )
    try:
        if value.utcoffset() is None:
            raise ValueError("UTC offset is unavailable")
        return value.astimezone(timezone.utc)
    except Exception as cause:
        raise DomainError(
            "RETENTION_TIME_INVALID", "timezone-aware datetime is required"
        ) from cause


def _shift_retention_time(value: datetime, delta: timedelta) -> datetime:
    try:
        return value + delta
    except OverflowError as cause:
        raise DomainError(
            "RETENTION_TIME_INVALID", "retention time cannot be represented"
        ) from cause


def _require_preview_token(value: object) -> str:
    if value is None:
        raise DomainError(
            "DELETION_PREVIEW_TOKEN_REQUIRED",
            "deletion preview token is required",
        )
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise DomainError(
            "DELETION_PREVIEW_TOKEN_INVALID",
            "deletion preview token is invalid",
        )
    return value


def _target_payload(targets: TextDeletionTargets) -> dict[str, object]:
    return {
        "analysis_inputs": [
            {
                "created_at": utc_iso(target.created_at),
                "id": target.id,
                "input_sha256": target.input_sha256,
                "run_id": target.run_id,
            }
            for target in targets.analysis_inputs
        ],
        "transcripts": [
            {
                "created_at": utc_iso(target.created_at),
                "id": target.id,
                "text_sha256": target.text_sha256,
                "video_id": target.video_id,
            }
            for target in targets.transcripts
        ],
        "video_ids": list(targets.video_ids),
    }


def _target_fingerprint(targets: TextDeletionTargets) -> str:
    return sha256_text(canonical_json(_target_payload(targets)))


def _preview_token(
    cutoff: datetime,
    policy: RetentionPolicy,
    targets: TextDeletionTargets,
    *,
    created_at: datetime,
    expires_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "cutoff_utc": utc_iso(cutoff),
                "created_at": utc_iso(created_at),
                "expires_at": utc_iso(expires_at),
                "retention_days": policy.days,
                "targets": _target_payload(targets),
            }
        )
    )


def _resolved_safe_audio_path(root: object, candidate: object) -> Path | None:
    resolved, _ = _resolve_audio_path(root, candidate)
    return resolved


def _resolve_audio_path(
    root: object, candidate: object
) -> tuple[Path | None, str | None]:
    if not isinstance(root, Path) or not isinstance(candidate, Path):
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    if "\x00" in str(root) or "\x00" in str(candidate):
        return None, "AUDIO_DELETE_OS_ERROR"
    if not root.is_absolute() or not candidate.is_absolute():
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    try:
        resolved_root = root.resolve(strict=True)
        root_stat = resolved_root.stat()
    except FileNotFoundError:
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    except PermissionError:
        return None, "AUDIO_DELETE_PERMISSION"
    except OSError:
        return None, "AUDIO_DELETE_OS_ERROR"
    except ValueError:
        return None, "AUDIO_DELETE_OS_ERROR"
    except RuntimeError:
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    if not stat.S_ISDIR(root_stat.st_mode):
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except PermissionError:
        return None, "AUDIO_DELETE_PERMISSION"
    except OSError:
        return None, "AUDIO_DELETE_OS_ERROR"
    except ValueError:
        return None, "AUDIO_DELETE_OS_ERROR"
    except RuntimeError:
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    root_text = os.path.normcase(os.path.normpath(str(resolved_root)))
    candidate_text = os.path.normcase(
        os.path.normpath(str(resolved_candidate))
    )
    if candidate_text == root_text:
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    try:
        if os.path.commonpath((root_text, candidate_text)) != root_text:
            return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    except OSError:
        return None, "AUDIO_DELETE_OS_ERROR"
    except ValueError:
        return None, "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    return resolved_candidate, None

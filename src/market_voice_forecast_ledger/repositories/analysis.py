import re
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timezone

from market_voice_forecast_ledger.domain.analysis import (
    AnalysisInputSnapshot,
    AnalysisRun,
    AnalysisRunJobAttempt,
    AnalysisRunSettings,
    AnalysisScope,
    FrozenAnalysisInput,
    RunSegment,
    SelectedInputSegment,
)
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    AssignmentKind,
    AssignmentOrigin,
    EligibilityStatus,
    PolicyKind,
    ScopeStatus,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.sources import ChannelPolicy


_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_SAFE_STALE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_STALE_SCOPE_BATCH_SIZE = 500


class AnalysisRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_active_subject_kind(self, subject_id: int) -> SubjectKind:
        row = self._conn.execute(
            """
            SELECT subject_kind
            FROM analysis_subjects
            WHERE id=? AND is_active=1
            """,
            (subject_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "ANALYSIS_SUBJECT_NOT_ELIGIBLE",
                "analysis requires an active subject",
            )
        return SubjectKind(row["subject_kind"])

    def select_input_segments(
        self,
        subject_id: int,
        cutoff_exclusive: datetime,
        subject_kind: SubjectKind,
        policy: ChannelPolicy,
    ) -> tuple[SelectedInputSegment, ...]:
        assignment_origin = (
            AssignmentOrigin.CHANNEL_ORGANIZATION.value
            if subject_kind is SubjectKind.ORGANIZATION
            else None
        )
        rows = self._conn.execute(
            """
            SELECT
                segment.id AS segment_id,
                video.id AS video_id,
                video.youtube_video_id,
                video.title AS video_title,
                video.youtube_channel_id,
                video.channel_display_name,
                video.published_at,
                segment.segment_no,
                segment.start_ms,
                segment.end_ms,
                segment.text_body,
                segment.text_sha256,
                eligibility.policy_id,
                eligibility.policy_hash,
                assignment.assignment_kind,
                assignment.assignment_origin,
                assignment.assigned_subject_id,
                assignment.assigned_at AS assignment_updated_at,
                assignment.evidence_hash AS assignment_evidence_hash
            FROM analysis_subjects AS subject
            JOIN subject_video_eligibility AS eligibility
                ON eligibility.subject_id = subject.id
                AND eligibility.policy_id = ?
                AND eligibility.policy_hash = ?
                AND eligibility.status = ?
            JOIN videos AS video ON video.id = eligibility.video_id
            JOIN transcript_segments AS segment ON segment.video_id = video.id
            JOIN speaker_assignments AS assignment
                ON assignment.segment_id = segment.id
            WHERE subject.id = ?
                AND subject.is_active = 1
                AND subject.subject_kind = ?
                AND (
                    ? = ?
                    OR (
                        ? = ?
                        AND video.youtube_channel_id = ?
                    )
                )
                AND video.published_at < ?
                AND segment.text_body IS NOT NULL
                AND assignment.assignment_kind = ?
                AND assignment.assigned_subject_id = subject.id
                AND (
                    (? IS NULL AND assignment.assignment_origin != ?)
                    OR assignment.assignment_origin = ?
                )
            ORDER BY
                video.published_at,
                video.youtube_video_id,
                segment.segment_no
            """,
            (
                policy.id,
                policy.policy_hash,
                EligibilityStatus.ELIGIBLE.value,
                subject_id,
                subject_kind.value,
                policy.policy_kind.value,
                PolicyKind.ALL_CHANNELS.value,
                policy.policy_kind.value,
                PolicyKind.FIXED_CHANNEL.value,
                policy.youtube_channel_id,
                utc_iso(cutoff_exclusive),
                AssignmentKind.SUBJECT.value,
                assignment_origin,
                AssignmentOrigin.CHANNEL_ORGANIZATION.value,
                assignment_origin,
            ),
        ).fetchall()
        return tuple(_selected_segment_from_row(row) for row in rows)

    def select_interviewer_context_segments(
        self,
        subject_id: int,
        cutoff_exclusive: datetime,
        policy: ChannelPolicy,
    ) -> tuple[SelectedInputSegment, ...]:
        rows = self._conn.execute(
            """
            SELECT
                segment.id AS segment_id,
                video.id AS video_id,
                video.youtube_video_id,
                video.title AS video_title,
                video.youtube_channel_id,
                video.channel_display_name,
                video.published_at,
                segment.segment_no,
                segment.start_ms,
                segment.end_ms,
                segment.text_body,
                segment.text_sha256,
                eligibility.policy_id,
                eligibility.policy_hash,
                assignment.assignment_kind,
                assignment.assignment_origin,
                assignment.assigned_subject_id,
                assignment.assigned_at AS assignment_updated_at,
                assignment.evidence_hash AS assignment_evidence_hash
            FROM analysis_subjects AS subject
            JOIN subject_video_eligibility AS eligibility
                ON eligibility.subject_id = subject.id
                AND eligibility.policy_id = ?
                AND eligibility.policy_hash = ?
                AND eligibility.status = ?
            JOIN videos AS video ON video.id = eligibility.video_id
            JOIN transcript_segments AS segment ON segment.video_id = video.id
            JOIN speaker_assignments AS assignment
                ON assignment.segment_id = segment.id
            WHERE subject.id = ?
                AND subject.is_active = 1
                AND subject.subject_kind = ?
                AND (
                    ? = ?
                    OR (
                        ? = ?
                        AND video.youtube_channel_id = ?
                    )
                )
                AND video.published_at < ?
                AND segment.text_body IS NOT NULL
                AND assignment.assignment_kind = ?
                AND assignment.assigned_subject_id IS NULL
                AND assignment.assignment_origin != ?
            ORDER BY
                video.published_at,
                video.youtube_video_id,
                segment.segment_no
            """,
            (
                policy.id,
                policy.policy_hash,
                EligibilityStatus.ELIGIBLE.value,
                subject_id,
                SubjectKind.PERSON.value,
                policy.policy_kind.value,
                PolicyKind.ALL_CHANNELS.value,
                policy.policy_kind.value,
                PolicyKind.FIXED_CHANNEL.value,
                policy.youtube_channel_id,
                utc_iso(cutoff_exclusive),
                AssignmentKind.INTERVIEWER.value,
                AssignmentOrigin.CHANNEL_ORGANIZATION.value,
            ),
        ).fetchall()
        return tuple(_selected_segment_from_row(row) for row in rows)

    def get_or_create_scope(
        self,
        subject_id: int,
        cutoff_day: date,
        cutoff_exclusive: datetime,
    ) -> tuple[int, int]:
        self._require_transaction()
        cutoff_day_text = cutoff_day.isoformat()
        cutoff_text = utc_iso(cutoff_exclusive)
        row = self._conn.execute(
            """
            SELECT id, cutoff_exclusive_utc, generation
            FROM analysis_scopes
            WHERE subject_id=? AND cutoff_day_jst=?
            """,
            (subject_id, cutoff_day_text),
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
                """
                INSERT INTO analysis_scopes(
                    subject_id,
                    cutoff_day_jst,
                    cutoff_exclusive_utc,
                    status,
                    stale_reason,
                    generation
                ) VALUES (?, ?, ?, ?, NULL, 1)
                """,
                (
                    subject_id,
                    cutoff_day_text,
                    cutoff_text,
                    ScopeStatus.RUNNING.value,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("analysis scope insert did not return an id")
            return cursor.lastrowid, 1
        if row["cutoff_exclusive_utc"] != cutoff_text:
            raise DomainError(
                "ANALYSIS_SCOPE_CUTOFF_MISMATCH",
                "stored scope cutoff does not match the fixed JST rule",
            )
        generation = _nonnegative_generation(
            row["generation"], "ANALYSIS_SCOPE_STORED_INVALID"
        )
        updated = self._conn.execute(
            """
            UPDATE analysis_scopes
            SET status=?, stale_reason=NULL, generation=generation+1
            WHERE id=?
            """,
            (ScopeStatus.RUNNING.value, row["id"]),
        )
        if updated.rowcount != 1:
            raise DomainError(
                "ANALYSIS_SCOPE_STORED_INVALID",
                "analysis scope generation could not advance",
            )
        return row["id"], generation + 1

    def insert_run(
        self,
        scope_id: int,
        scope_generation: int,
        settings: AnalysisRunSettings,
        frozen_input: FrozenAnalysisInput,
        started_at: datetime,
    ) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_runs(
                scope_id,
                model,
                reasoning_effort,
                prompt_version,
                schema_version,
                information_boundary_version,
                input_hash,
                input_contract_hash,
                started_at,
                scope_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_id,
                settings.model,
                settings.reasoning_effort,
                settings.prompt_version,
                settings.schema_version,
                settings.information_boundary_version,
                frozen_input.input_sha256,
                frozen_input.input_contract_hash,
                utc_iso(started_at),
                scope_generation,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("analysis run insert did not return an id")
        return cursor.lastrowid

    def insert_job_attempt(
        self,
        run_id: int,
        job_id: int,
        attempt_ordinal: int,
        source_job_id: int | None,
        attached_at: datetime,
    ) -> AnalysisRunJobAttempt:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_run_job_attempts(
                run_id,
                job_id,
                attempt_ordinal,
                source_job_id,
                attached_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                job_id,
                attempt_ordinal,
                source_job_id,
                utc_iso(attached_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("analysis attempt insert did not return an id")
        return self.get_job_attempt(cursor.lastrowid)

    def insert_run_segments(
        self, run_id: int, segments: Sequence[SelectedInputSegment]
    ) -> None:
        self._require_transaction()
        self._conn.executemany(
            """
            INSERT INTO analysis_run_segments(
                run_id,
                segment_id,
                ordinal,
                video_id,
                published_at,
                policy_id,
                policy_hash,
                assignment_kind,
                assigned_subject_id,
                assignment_updated_at,
                assignment_evidence_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    run_id,
                    segment.segment_id,
                    ordinal,
                    segment.video_id,
                    utc_iso(segment.published_at),
                    segment.policy_id,
                    segment.policy_hash,
                    segment.assignment_kind.value,
                    segment.assigned_subject_id,
                    utc_iso(segment.assignment_updated_at),
                    segment.assignment_evidence_hash,
                )
                for ordinal, segment in enumerate(segments, start=1)
            ),
        )

    def insert_snapshot(
        self,
        run_id: int,
        frozen_input: FrozenAnalysisInput,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_input_snapshots(
                run_id,
                input_text,
                metadata_json,
                input_sha256,
                snapshot_created_at,
                expires_at,
                text_deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                run_id,
                frozen_input.input_text,
                frozen_input.metadata_json,
                frozen_input.input_sha256,
                utc_iso(created_at),
                None if expires_at is None else utc_iso(expires_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("analysis snapshot insert did not return an id")
        return cursor.lastrowid

    def get_scope(self, scope_id: int) -> AnalysisScope:
        row = self._conn.execute(
            "SELECT * FROM analysis_scopes WHERE id=?", (scope_id,)
        ).fetchone()
        if row is None:
            raise DomainError("ANALYSIS_SCOPE_NOT_FOUND", "analysis scope not found")
        return _scope_from_row(row)

    def get_run(self, run_id: int) -> AnalysisRun:
        row = self._conn.execute(
            """
            SELECT
                run.*,
                (
                    SELECT attempt.job_id
                    FROM analysis_run_job_attempts AS attempt
                    WHERE attempt.run_id=run.id
                    ORDER BY attempt.attempt_ordinal DESC
                    LIMIT 1
                ) AS active_job_id
            FROM analysis_runs AS run
            WHERE run.id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise DomainError("ANALYSIS_RUN_NOT_FOUND", "analysis run not found")
        if row["active_job_id"] is None:
            raise DomainError(
                "ANALYSIS_RUN_HAS_NO_JOB", "analysis run has no job attempt"
            )
        return _run_from_row(row)

    def get_active_job_id(self, run_id: int) -> int:
        row = self._conn.execute(
            """
            SELECT job_id
            FROM analysis_run_job_attempts
            WHERE run_id=?
            ORDER BY attempt_ordinal DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            self.get_run(run_id)
            raise DomainError(
                "ANALYSIS_RUN_HAS_NO_JOB", "analysis run has no job attempt"
            )
        return row["job_id"]

    def count_runs(self, scope_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE scope_id=?", (scope_id,)
        ).fetchone()[0]

    def get_input_segments(self, run_id: int) -> tuple[RunSegment, ...]:
        self.get_run(run_id)
        rows = self._conn.execute(
            """
            SELECT *
            FROM analysis_run_segments
            WHERE run_id=?
            ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        return tuple(_run_segment_from_row(row) for row in rows)

    def get_snapshot(self, run_id: int) -> AnalysisInputSnapshot:
        row = self._conn.execute(
            "SELECT * FROM analysis_input_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "ANALYSIS_SNAPSHOT_NOT_FOUND", "analysis snapshot not found"
            )
        return _snapshot_from_row(row)

    def get_effective_run_status(self, run_id: int) -> AnalysisRunStatus:
        row = self._conn.execute(
            """
            SELECT status
            FROM analysis_run_events
            WHERE run_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            self.get_run(run_id)
            raise DomainError(
                "ANALYSIS_RUN_HAS_NO_EVENT", "analysis run has no status event"
            )
        return AnalysisRunStatus(row["status"])

    def append_run_event(
        self,
        run_id: int,
        status: AnalysisRunStatus,
        error_code: str | None,
        *,
        created_at: datetime | None = None,
    ) -> int:
        self._require_transaction()
        if error_code is not None and (
            not isinstance(error_code, str)
            or not _SAFE_ERROR_CODE.fullmatch(error_code)
        ):
            raise DomainError(
                "UNSAFE_ANALYSIS_ERROR_CODE",
                "analysis event error code must be a safe token",
            )
        self.get_run(run_id)
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_run_events(
                run_id, status, safe_error_code, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                status.value,
                error_code,
                utc_iso(created_at or datetime.now(timezone.utc)),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("analysis event insert did not return an id")
        return cursor.lastrowid

    def get_job_attempt(self, attempt_id: int) -> AnalysisRunJobAttempt:
        row = self._conn.execute(
            "SELECT * FROM analysis_run_job_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "ANALYSIS_JOB_ATTEMPT_NOT_FOUND", "analysis attempt not found"
            )
        return _attempt_from_row(row)

    def list_job_attempts(
        self, run_id: int
    ) -> tuple[AnalysisRunJobAttempt, ...]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM analysis_run_job_attempts
            WHERE run_id=?
            ORDER BY attempt_ordinal
            """,
            (run_id,),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def is_job_attached(self, job_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM analysis_run_job_attempts WHERE job_id=?", (job_id,)
            ).fetchone()
            is not None
        )

    def next_attempt_ordinal(self, run_id: int) -> int:
        return self._conn.execute(
            """
            SELECT COALESCE(MAX(attempt_ordinal), 0) + 1
            FROM analysis_run_job_attempts
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()[0]

    def scope_ids_affected_by_speaker_correction(
        self,
        segment_id: int,
        assigned_subject_id: int | None,
    ) -> tuple[int, ...]:
        if (
            type(segment_id) is not int
            or segment_id <= 0
            or (
                assigned_subject_id is not None
                and (
                    type(assigned_subject_id) is not int
                    or assigned_subject_id <= 0
                )
            )
        ):
            raise DomainError(
                "STALE_TRANSITION_INVALID",
                "speaker stale lookup requires valid segment and subject ids",
            )
        return tuple(
            row["id"]
            for row in self._conn.execute(
                """
                WITH corrected_segment AS (
                    SELECT video_id
                    FROM transcript_segments
                    WHERE id=?
                )
                SELECT scope.id
                FROM analysis_scopes AS scope
                WHERE EXISTS (SELECT 1 FROM corrected_segment)
                    AND (
                        (? IS NOT NULL AND scope.subject_id=?)
                        OR EXISTS (
                            SELECT 1
                            FROM analysis_runs AS run
                            JOIN analysis_run_segments AS run_segment
                                ON run_segment.run_id=run.id
                            JOIN corrected_segment
                                ON corrected_segment.video_id=run_segment.video_id
                            WHERE run.scope_id=scope.id
                        )
                    )
                ORDER BY scope.id
                """,
                (segment_id, assigned_subject_id, assigned_subject_id),
            )
        )

    def scope_ids_for_subject(self, subject_id: int) -> tuple[int, ...]:
        if type(subject_id) is not int or subject_id <= 0:
            raise DomainError(
                "STALE_TRANSITION_INVALID",
                "policy stale lookup requires a valid subject id",
            )
        return tuple(
            row["id"]
            for row in self._conn.execute(
                """
                SELECT id
                FROM analysis_scopes
                WHERE subject_id=?
                ORDER BY id
                """,
                (subject_id,),
            )
        )

    def mark_scope_ids_stale(
        self, scope_ids: Sequence[int], reason: str
    ) -> tuple[int, ...]:
        self._require_transaction()
        if (
            type(reason) is not str
            or _SAFE_STALE_REASON.fullmatch(reason) is None
        ):
            raise DomainError(
                "STALE_TRANSITION_INVALID",
                "stale transition requires positive ids and a safe reason code",
            )
        identifiers = tuple(scope_ids)
        if any(type(scope_id) is not int or scope_id <= 0 for scope_id in identifiers):
            raise DomainError(
                "STALE_TRANSITION_INVALID",
                "stale transition requires positive ids and a safe reason code",
            )
        deduplicated = tuple(sorted(set(identifiers)))
        if not deduplicated:
            return ()
        variable_limit = self._conn.getlimit(
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
        )
        batch_size = min(_STALE_SCOPE_BATCH_SIZE, variable_limit - 2)
        if batch_size <= 0:
            raise DomainError(
                "ANALYSIS_SCOPE_STORED_INVALID",
                "SQLite variable limit cannot support scope invalidation",
            )
        updated_count = 0
        for start in range(0, len(deduplicated), batch_size):
            batch = deduplicated[start : start + batch_size]
            placeholders = ", ".join("?" for _ in batch)
            updated = self._conn.execute(
                f"""
                UPDATE analysis_scopes
                SET status=?, stale_reason=?, generation=generation+1
                WHERE id IN ({placeholders})
                """,
                (ScopeStatus.STALE.value, reason, *batch),
            )
            updated_count += updated.rowcount
        if updated_count != len(deduplicated):
            raise DomainError(
                "ANALYSIS_SCOPE_STORED_INVALID",
                "affected analysis scopes changed during invalidation",
            )
        return deduplicated

    def mark_scopes_using_segment_stale(
        self,
        segment_id: int,
        reason: str,
        assigned_subject_id: int | None = None,
    ) -> tuple[int, ...]:
        self._validate_stale_input(segment_id, reason)
        self._require_transaction()
        scope_ids = self.scope_ids_affected_by_speaker_correction(
            segment_id, assigned_subject_id
        )
        return self.mark_scope_ids_stale(scope_ids, reason)

    def mark_scopes_using_policy_stale(
        self, policy_id: int, reason: str
    ) -> tuple[int, ...]:
        self._validate_stale_input(policy_id, reason)
        self._require_transaction()
        row = self._conn.execute(
            "SELECT subject_id FROM subject_channel_policies WHERE id=?",
            (policy_id,),
        ).fetchone()
        scope_ids = () if row is None else self.scope_ids_for_subject(
            row["subject_id"]
        )
        return self.mark_scope_ids_stale(scope_ids, reason)

    @staticmethod
    def _validate_stale_input(identifier: int, reason: str) -> None:
        if (
            type(identifier) is not int
            or identifier <= 0
            or type(reason) is not str
            or _SAFE_STALE_REASON.fullmatch(reason) is None
        ):
            raise DomainError(
                "STALE_TRANSITION_INVALID",
                "stale transition requires a positive id and safe reason code",
            )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "ANALYSIS_TRANSACTION_REQUIRED",
                "analysis mutation requires an active caller transaction",
            )


def _selected_segment_from_row(row: sqlite3.Row) -> SelectedInputSegment:
    return SelectedInputSegment(
        segment_id=row["segment_id"],
        video_id=row["video_id"],
        youtube_video_id=row["youtube_video_id"],
        video_title=row["video_title"],
        youtube_channel_id=row["youtube_channel_id"],
        channel_display_name=row["channel_display_name"],
        published_at=_parse_utc(row["published_at"]),
        segment_no=row["segment_no"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        text_body=row["text_body"],
        text_sha256=row["text_sha256"],
        policy_id=row["policy_id"],
        policy_hash=row["policy_hash"],
        assignment_kind=AssignmentKind(row["assignment_kind"]),
        assignment_origin=row["assignment_origin"],
        assigned_subject_id=row["assigned_subject_id"],
        assignment_updated_at=_parse_utc(row["assignment_updated_at"]),
        assignment_evidence_hash=row["assignment_evidence_hash"],
    )


def _scope_from_row(row: sqlite3.Row) -> AnalysisScope:
    return AnalysisScope(
        id=row["id"],
        subject_id=row["subject_id"],
        cutoff_day_jst=date.fromisoformat(row["cutoff_day_jst"]),
        cutoff_exclusive_utc=_parse_utc(row["cutoff_exclusive_utc"]),
        status=ScopeStatus(row["status"]),
        stale_reason=row["stale_reason"],
        generation=_nonnegative_generation(
            row["generation"], "ANALYSIS_SCOPE_STORED_INVALID"
        ),
    )


def _run_from_row(row: sqlite3.Row) -> AnalysisRun:
    return AnalysisRun(
        id=row["id"],
        scope_id=row["scope_id"],
        scope_generation=_nonnegative_generation(
            row["scope_generation"], "ANALYSIS_RUN_STORED_INVALID"
        ),
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        prompt_version=row["prompt_version"],
        schema_version=row["schema_version"],
        information_boundary_version=row["information_boundary_version"],
        input_hash=row["input_hash"],
        input_contract_hash=row["input_contract_hash"],
        started_at=_parse_utc(row["started_at"]),
        active_job_id=row["active_job_id"],
    )


def _nonnegative_generation(value: object, error_code: str) -> int:
    if type(value) is not int or value < 0:
        raise DomainError(error_code, "stored analysis generation is invalid")
    return value


def _attempt_from_row(row: sqlite3.Row) -> AnalysisRunJobAttempt:
    return AnalysisRunJobAttempt(
        id=row["id"],
        run_id=row["run_id"],
        job_id=row["job_id"],
        attempt_ordinal=row["attempt_ordinal"],
        source_job_id=row["source_job_id"],
        attached_at=_parse_utc(row["attached_at"]),
    )


def _run_segment_from_row(row: sqlite3.Row) -> RunSegment:
    return RunSegment(
        id=row["id"],
        run_id=row["run_id"],
        segment_id=row["segment_id"],
        ordinal=row["ordinal"],
        video_id=row["video_id"],
        published_at=_parse_utc(row["published_at"]),
        policy_id=row["policy_id"],
        policy_hash=row["policy_hash"],
        assignment_kind=AssignmentKind(row["assignment_kind"]),
        assigned_subject_id=row["assigned_subject_id"],
        assignment_updated_at=_parse_utc(row["assignment_updated_at"]),
        assignment_evidence_hash=row["assignment_evidence_hash"],
    )


def _snapshot_from_row(row: sqlite3.Row) -> AnalysisInputSnapshot:
    return AnalysisInputSnapshot(
        id=row["id"],
        run_id=row["run_id"],
        input_text=row["input_text"],
        metadata_json=row["metadata_json"],
        input_sha256=row["input_sha256"],
        snapshot_created_at=_parse_utc(row["snapshot_created_at"]),
        expires_at=_parse_optional_utc(row["expires_at"]),
        text_deleted_at=_parse_optional_utc(row["text_deleted_at"]),
    )


def _parse_optional_utc(value: str | None) -> datetime | None:
    return None if value is None else _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.enums import AssignmentKind, AssignmentOrigin
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.audit import AuditEventInput, AuditRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository


_SAFE_HASH_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class SpeakerCorrection:
    segment_id: int
    assignment_kind: AssignmentKind
    assigned_subject_id: int | None
    actor: str
    reason: str


class SpeakerCorrectionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._speakers = SpeakerRepository(conn)
        self._analysis = AnalysisRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def correct(self, command: SpeakerCorrection) -> SpeakerAssignment:
        self._validate_command(command)
        with transaction(self._conn):
            try:
                video_id = self._speakers.get_segment_video_id(command.segment_id)
            except LookupError as cause:
                raise DomainError(
                    "SPEAKER_CORRECTION_SEGMENT_NOT_FOUND",
                    "speaker correction requires an existing transcript segment",
                ) from cause
            try:
                before = self._speakers.get_assignment(command.segment_id)
            except LookupError as cause:
                raise DomainError(
                    "SPEAKER_ASSIGNMENT_NOT_FOUND",
                    "speaker correction requires a current assignment",
                ) from cause
            except (TypeError, ValueError) as cause:
                raise DomainError(
                    "SPEAKER_ASSIGNMENT_STORED_INVALID",
                    "stored speaker assignment metadata is invalid",
                ) from cause
            if not _stored_assignment_is_valid(before):
                raise DomainError(
                    "SPEAKER_ASSIGNMENT_STORED_INVALID",
                    "stored speaker assignment metadata is invalid",
                )
            if command.assignment_kind is AssignmentKind.SUBJECT:
                subject = self._conn.execute(
                    "SELECT is_active FROM analysis_subjects WHERE id=?",
                    (command.assigned_subject_id,),
                ).fetchone()
                if (
                    subject is None
                    or subject["is_active"] != 1
                    or not self._subject_has_confirmed_presence(
                        command.assigned_subject_id, video_id
                    )
                ):
                    raise DomainError(
                        "SPEAKER_CORRECTION_SUBJECT_INVALID",
                        "subject assignment requires active confirmed presence",
                    )

            affected_scope_ids = self._analysis.scope_ids_affected_by_speaker_correction(
                command.segment_id,
                command.assigned_subject_id,
            )
            assigned_at = self._clock()
            evidence_hash = sha256_text(
                canonical_json(
                    {
                        "assigned_subject_id": command.assigned_subject_id,
                        "assignment_kind": command.assignment_kind.value,
                        "assignment_origin": AssignmentOrigin.MANUAL.value,
                        "segment_id": command.segment_id,
                    }
                )
            )
            after = SpeakerAssignment(
                segment_id=command.segment_id,
                assignment_kind=command.assignment_kind,
                assigned_subject_id=command.assigned_subject_id,
                assignment_origin=AssignmentOrigin.MANUAL,
                raw_match_score=None,
                model_name=None,
                model_version=None,
                threshold_config_version=None,
                evidence_hash=evidence_hash,
                assigned_at=assigned_at,
            )
            self._speakers.save_assignment(after)
            self._audit.append(
                AuditEventInput(
                    entity_type="speaker_assignment",
                    entity_id=str(command.segment_id),
                    scope_id=None,
                    operation="correct",
                    actor_kind=command.actor,
                    reason_code="SPEAKER_CORRECTION",
                    reason_text=command.reason,
                    before=_assignment_audit_view(before),
                    after=_assignment_audit_view(after),
                    created_at=assigned_at,
                )
            )
            self._analysis.mark_scope_ids_stale(
                affected_scope_ids, "SPEAKER_ASSIGNMENT_CHANGED"
            )
        return after

    def _subject_has_confirmed_presence(self, subject_id: int, video_id: int) -> bool:
        return self._conn.execute(
            """
            SELECT 1
            FROM subject_video_candidates AS candidate
            JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id
            JOIN presence_decisions AS decision
                ON decision.id=candidate.current_presence_decision_id
                AND decision.candidate_id=candidate.id
            WHERE profile.subject_id=?
                AND candidate.video_id=?
                AND decision.state='presence_confirmed'
            LIMIT 1
            """,
            (subject_id, video_id),
        ).fetchone() is not None

    @staticmethod
    def _validate_command(command: SpeakerCorrection) -> None:
        if type(command) is not SpeakerCorrection:
            raise DomainError(
                "SPEAKER_CORRECTION_INVALID",
                "speaker correction requires an exact command",
            )
        kind_is_exact = type(command.assignment_kind) is AssignmentKind
        valid_shape = kind_is_exact and (
            (
                command.assignment_kind is AssignmentKind.SUBJECT
                and type(command.assigned_subject_id) is int
                and command.assigned_subject_id > 0
            )
            or (
                command.assignment_kind in {AssignmentKind.INTERVIEWER, AssignmentKind.HOLD}
                and command.assigned_subject_id is None
            )
        )
        if (
            type(command.segment_id) is not int
            or command.segment_id <= 0
            or not valid_shape
            or type(command.actor) is not str
            or command.actor not in {"user", "system"}
            or not _has_practical_text(command.reason)
        ):
            raise DomainError(
                "SPEAKER_CORRECTION_INVALID",
                "speaker correction requires a valid assignment, actor, and reason",
            )


def _assignment_audit_view(assignment: SpeakerAssignment) -> dict[str, object]:
    return {
        "assigned_at": utc_iso(assignment.assigned_at),
        "assigned_subject_id": assignment.assigned_subject_id,
        "assignment_kind": assignment.assignment_kind.value,
        "assignment_origin": assignment.assignment_origin.value,
        "evidence_hash": assignment.evidence_hash,
        "segment_id": assignment.segment_id,
    }


def _has_practical_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _stored_assignment_is_valid(assignment: SpeakerAssignment) -> bool:
    assigned_subject_valid = assignment.assigned_subject_id is None or (
        type(assignment.assigned_subject_id) is int
        and assignment.assigned_subject_id > 0
    )
    assignment_shape_valid = (
        assignment.assignment_kind is AssignmentKind.SUBJECT
        and type(assignment.assigned_subject_id) is int
        and assignment.assigned_subject_id > 0
    ) or (
        assignment.assignment_kind in {AssignmentKind.INTERVIEWER, AssignmentKind.HOLD}
        and assignment.assigned_subject_id is None
    )
    optional_text_valid = all(
        value is None or type(value) is str
        for value in (
            assignment.model_name,
            assignment.model_version,
            assignment.threshold_config_version,
        )
    )
    return (
        type(assignment.segment_id) is int
        and assignment.segment_id > 0
        and type(assignment.assignment_kind) is AssignmentKind
        and assigned_subject_valid
        and assignment_shape_valid
        and type(assignment.assignment_origin) is AssignmentOrigin
        and (
            assignment.raw_match_score is None
            or type(assignment.raw_match_score) is float
        )
        and optional_text_valid
        and type(assignment.evidence_hash) is str
        and _SAFE_HASH_TOKEN.fullmatch(assignment.evidence_hash) is not None
        and type(assignment.assigned_at) is datetime
        and assignment.assigned_at.tzinfo is not None
        and assignment.assigned_at.utcoffset()
        == timezone.utc.utcoffset(assignment.assigned_at)
    )

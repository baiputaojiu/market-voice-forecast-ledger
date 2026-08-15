import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    ConfigurationStatus,
    EligibilityStatus,
    JobStatus,
    PolicyKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.domain.sources import ChannelPolicy
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import evaluate_policy
from market_voice_forecast_ledger.services.job_state import JobStateService


_STOPPABLE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.PAUSE_REQUESTED,
    JobStatus.PAUSED,
    JobStatus.CANCEL_REQUESTED,
    JobStatus.FAILED,
    JobStatus.RETRYING,
}
_SAFE_HASH_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True, slots=True)
class SpeakerCorrection:
    segment_id: int
    assignment_kind: AssignmentKind
    assigned_subject_id: int | None
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChannelPolicyChange:
    subject_id: int
    policy_kind: PolicyKind
    configuration_status: ConfigurationStatus
    youtube_channel_id: str | None
    channel_display_name: str | None
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
        self._sources = SourceRepository(conn)
        self._analysis = AnalysisRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def correct(self, command: SpeakerCorrection) -> SpeakerAssignment:
        self._validate_command(command)
        with transaction(self._conn):
            try:
                video_id = self._speakers.get_segment_video_id(
                    command.segment_id
                )
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
                    or not self._sources.subject_is_currently_eligible_for_video(
                        command.assigned_subject_id, video_id
                    )
                ):
                    raise DomainError(
                        "SPEAKER_CORRECTION_SUBJECT_INVALID",
                        "subject assignment requires an active eligible subject",
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
            self._analysis.mark_scopes_using_segment_stale(
                command.segment_id, "SPEAKER_ASSIGNMENT_CHANGED"
            )
        return after

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
                command.assignment_kind
                in {AssignmentKind.INTERVIEWER, AssignmentKind.HOLD}
                and command.assigned_subject_id is None
            )
        )
        if (
            type(command.segment_id) is not int
            or command.segment_id <= 0
            or not kind_is_exact
            or not valid_shape
            or type(command.actor) is not str
            or command.actor not in {"user", "system"}
            or not _has_practical_text(command.reason)
        ):
            raise DomainError(
                "SPEAKER_CORRECTION_INVALID",
                "speaker correction requires a valid assignment, actor, and reason",
            )


class ChannelPolicyCorrectionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._sources = SourceRepository(conn)
        self._analysis = AnalysisRepository(conn)
        self._audit = AuditRepository(conn)
        self._jobs = JobRepository(conn)
        self._job_state = JobStateService(conn, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def change(self, command: ChannelPolicyChange) -> ChannelPolicy:
        self._validate_command(command)
        with transaction(self._conn):
            subject = self._conn.execute(
                "SELECT id FROM analysis_subjects WHERE id=?",
                (command.subject_id,),
            ).fetchone()
            if subject is None:
                raise DomainError(
                    "CHANNEL_POLICY_NOT_FOUND",
                    "channel policy change requires an existing subject",
                )
            try:
                before = self._sources.get_policy(command.subject_id)
            except LookupError as cause:
                raise DomainError(
                    "CHANNEL_POLICY_NOT_FOUND",
                    "channel policy change requires an existing policy",
                ) from cause
            if _policy_configuration(before) == _command_configuration(command):
                raise DomainError(
                    "CHANNEL_POLICY_NO_CHANGE",
                    "channel policy correction must change current metadata",
                )

            changed_at = self._clock()
            after = self._sources.replace_policy(
                command.subject_id,
                ChannelPolicy(
                    policy_kind=command.policy_kind,
                    configuration_status=command.configuration_status,
                    youtube_channel_id=command.youtube_channel_id,
                    channel_display_name=command.channel_display_name,
                ),
                changed_at,
            )
            self._audit.append(
                AuditEventInput(
                    entity_type="subject_channel_policy",
                    entity_id=str(before.id),
                    scope_id=None,
                    operation="correct",
                    actor_kind=command.actor,
                    reason_code="CHANNEL_POLICY_CORRECTION",
                    reason_text=command.reason,
                    before=_policy_audit_view(before),
                    after=_policy_audit_view(after),
                    created_at=changed_at,
                )
            )

            candidate_job_ids: set[int] = set()
            for eligibility in self._sources.list_subject_eligibilities(
                command.subject_id
            ):
                decision = evaluate_policy(
                    after, eligibility["video_youtube_channel_id"]
                )
                changed = (
                    eligibility["status"] != decision.status.value
                    or eligibility["policy_id"] != after.id
                    or eligibility["policy_hash"] != after.policy_hash
                    or eligibility["decision_reason"] != decision.reason
                )
                if not changed:
                    continue
                old_status = EligibilityStatus(eligibility["status"])
                before_view = _eligibility_audit_view(eligibility)
                self._sources.replace_eligibility(
                    eligibility["id"],
                    status=decision.status,
                    policy_id=after.id,
                    policy_hash=after.policy_hash,
                    decision_reason=decision.reason,
                    decided_at=changed_at,
                )
                after_view = dict(before_view)
                after_view.update(
                    {
                        "status": decision.status.value,
                        "policy_id": after.id,
                        "policy_hash": after.policy_hash,
                        "decision_reason": decision.reason,
                        "decided_at": utc_iso(changed_at),
                    }
                )
                self._audit.append(
                    AuditEventInput(
                        entity_type="subject_video_eligibility",
                        entity_id=(
                            f"{eligibility['subject_id']}:"
                            f"{eligibility['video_id']}"
                        ),
                        scope_id=None,
                        operation="update",
                        actor_kind="system",
                        reason_code=decision.reason,
                        reason_text=(
                            "Channel eligibility reevaluated after policy correction"
                        ),
                        before=before_view,
                        after=after_view,
                        created_at=changed_at,
                    )
                )
                if (
                    old_status is EligibilityStatus.ELIGIBLE
                    and decision.status is not EligibilityStatus.ELIGIBLE
                ):
                    candidate_job_ids.update(
                        self._jobs.list_video_job_ids_for_eligibility(
                            eligibility["id"]
                        )
                    )

            for job_id in sorted(candidate_job_ids):
                if self._jobs.has_current_eligible_video_binding(job_id):
                    continue
                job = self._jobs.get(job_id)
                if job.status in _STOPPABLE_JOB_STATUSES:
                    self._job_state.request_stop_in_transaction(job_id)

            if before.policy_hash != after.policy_hash:
                self._analysis.mark_scopes_using_policy_stale(
                    before.id, "CHANNEL_POLICY_CHANGED"
                )
        return after

    @staticmethod
    def _validate_command(command: ChannelPolicyChange) -> None:
        if type(command) is not ChannelPolicyChange:
            raise DomainError(
                "CHANNEL_POLICY_CHANGE_INVALID",
                "channel policy correction requires an exact command",
            )
        channel_id_type_valid = command.youtube_channel_id is None or type(
            command.youtube_channel_id
        ) is str
        display_type_valid = command.channel_display_name is None or type(
            command.channel_display_name
        ) is str
        channel_shape_valid = True
        if command.policy_kind is PolicyKind.ALL_CHANNELS:
            channel_shape_valid = command.youtube_channel_id is None
        elif (
            command.policy_kind is PolicyKind.FIXED_CHANNEL
            and command.configuration_status is ConfigurationStatus.CONFIGURED
        ):
            channel_shape_valid = (
                type(command.youtube_channel_id) is str
                and len(command.youtube_channel_id) == 24
                and command.youtube_channel_id.startswith("UC")
            )
        if (
            type(command.subject_id) is not int
            or command.subject_id <= 0
            or type(command.policy_kind) is not PolicyKind
            or type(command.configuration_status) is not ConfigurationStatus
            or not channel_id_type_valid
            or not display_type_valid
            or not channel_shape_valid
            or type(command.actor) is not str
            or command.actor not in {"user", "system"}
            or not _has_practical_text(command.reason)
        ):
            raise DomainError(
                "CHANNEL_POLICY_CHANGE_INVALID",
                "channel policy correction has invalid configuration metadata",
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


def _policy_audit_view(policy: ChannelPolicy) -> dict[str, object]:
    return {
        "channel_display_name": policy.channel_display_name,
        "configuration_status": policy.configuration_status.value,
        "policy_hash": policy.policy_hash,
        "policy_id": policy.id,
        "policy_kind": policy.policy_kind.value,
        "subject_id": policy.subject_id,
        "youtube_channel_id": policy.youtube_channel_id,
    }


def _eligibility_audit_view(row: sqlite3.Row) -> dict[str, object]:
    return {
        "decided_at": row["decided_at"],
        "decision_reason": row["decision_reason"],
        "discovery_method": row["discovery_method"],
        "policy_hash": row["policy_hash"],
        "policy_id": row["policy_id"],
        "status": row["status"],
        "subject_id": row["subject_id"],
        "video_id": row["video_id"],
    }


def _policy_configuration(policy: ChannelPolicy) -> tuple[object, ...]:
    return (
        policy.policy_kind,
        policy.configuration_status,
        policy.youtube_channel_id,
        policy.channel_display_name,
    )


def _command_configuration(command: ChannelPolicyChange) -> tuple[object, ...]:
    return (
        command.policy_kind,
        command.configuration_status,
        command.youtube_channel_id,
        command.channel_display_name,
    )


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
        assignment.assignment_kind
        in {AssignmentKind.INTERVIEWER, AssignmentKind.HOLD}
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
        and assignment.assigned_at.utcoffset() == timezone.utc.utcoffset(
            assignment.assigned_at
        )
    )

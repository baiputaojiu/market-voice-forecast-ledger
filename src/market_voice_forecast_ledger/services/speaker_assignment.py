import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    ConfigurationStatus,
    EligibilityStatus,
    PolicyKind,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.speakers import (
    PersonalAssignmentCommand,
    SpeakerAssignment,
    classify_raw_score,
)
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository


class SpeakerAssignmentService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._speakers = SpeakerRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record_personal(
        self, command: PersonalAssignmentCommand
    ) -> SpeakerAssignment:
        with transaction(self._conn):
            subject = self._conn.execute(
                "SELECT subject_kind, is_active FROM analysis_subjects WHERE id = ?",
                (command.subject_id,),
            ).fetchone()
            if (
                subject is None
                or subject["subject_kind"] != SubjectKind.PERSON.value
                or subject["is_active"] != 1
            ):
                raise ValueError("personal assignment requires an active person subject")

            try:
                config = self._speakers.get_active_threshold_config()
            except LookupError as error:
                raise ValueError("active speaker threshold config is required") from error
            if (
                command.threshold_config_version != config.version
                or command.model_name != config.model_name
                or command.model_version != config.model_version
            ):
                raise ValueError("assignment must match the active speaker threshold config")
            if not command.evidence_hash:
                raise ValueError("personal assignment requires an evidence hash")

            assignment_kind = classify_raw_score(command.raw_match_score, config)
            assignment = SpeakerAssignment(
                segment_id=command.segment_id,
                assignment_kind=assignment_kind,
                assigned_subject_id=(
                    command.subject_id
                    if assignment_kind is AssignmentKind.SUBJECT
                    else None
                ),
                assignment_origin=AssignmentOrigin.AUTO_VOICE,
                raw_match_score=command.raw_match_score,
                model_name=command.model_name,
                model_version=command.model_version,
                threshold_config_version=command.threshold_config_version,
                evidence_hash=command.evidence_hash,
                assigned_at=command.assigned_at or self._clock(),
            )
            self._speakers.save_assignment(assignment)
            return assignment

    def assign_organization_video(
        self, subject_id: int, video_id: int
    ) -> tuple[int, ...]:
        with transaction(self._conn):
            subject = self._conn.execute(
                "SELECT subject_kind, is_active FROM analysis_subjects WHERE id = ?",
                (subject_id,),
            ).fetchone()
            if (
                subject is None
                or subject["subject_kind"] != SubjectKind.ORGANIZATION.value
                or subject["is_active"] != 1
            ):
                raise ValueError(
                    "channel organization assignment requires an active organization subject"
                )

            eligibility = self._conn.execute(
                """
                SELECT eligibility.policy_hash, policy.policy_hash AS current_policy_hash
                FROM subject_video_eligibility AS eligibility
                JOIN subject_channel_policies AS policy
                    ON policy.id = eligibility.policy_id
                    AND policy.subject_id = eligibility.subject_id
                JOIN videos AS video ON video.id = eligibility.video_id
                WHERE eligibility.subject_id = ?
                    AND eligibility.video_id = ?
                    AND eligibility.status = ?
                    AND policy.policy_kind = ?
                    AND policy.configuration_status = ?
                    AND video.youtube_channel_id = policy.youtube_channel_id
                """,
                (
                    subject_id,
                    video_id,
                    EligibilityStatus.ELIGIBLE.value,
                    PolicyKind.FIXED_CHANNEL.value,
                    ConfigurationStatus.CONFIGURED.value,
                ),
            ).fetchone()
            if (
                eligibility is None
                or eligibility["policy_hash"] != eligibility["current_policy_hash"]
            ):
                raise ValueError(
                    "organization assignment requires a current eligible channel policy"
                )

            assigned_at = self._clock()
            segment_ids: list[int] = []
            for segment in self._speakers.list_segments_for_video(video_id):
                evidence_hash = sha256_text(
                    f"channel_organization:{subject_id}:{video_id}:"
                    f"{segment.id}:{eligibility['policy_hash']}"
                )
                self._speakers.save_assignment(
                    SpeakerAssignment(
                        segment_id=segment.id,
                        assignment_kind=AssignmentKind.SUBJECT,
                        assigned_subject_id=subject_id,
                        assignment_origin=AssignmentOrigin.CHANNEL_ORGANIZATION,
                        raw_match_score=None,
                        model_name=None,
                        model_version=None,
                        threshold_config_version=None,
                        evidence_hash=evidence_hash,
                        assigned_at=assigned_at,
                    )
                )
                segment_ids.append(segment.id)
            return tuple(segment_ids)

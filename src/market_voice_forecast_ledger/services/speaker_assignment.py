import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.enums import AssignmentKind, AssignmentOrigin
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
                "SELECT is_active FROM analysis_subjects WHERE id=?",
                (command.subject_id,),
            ).fetchone()
            if subject is None or subject["is_active"] != 1:
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
            return self._speakers.get_assignment(command.segment_id)

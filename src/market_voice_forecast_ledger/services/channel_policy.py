import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    DiscoveryMethod,
    EligibilityStatus,
    PolicyKind,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoRecord
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.sources import SourceRepository


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    status: EligibilityStatus
    may_download_audio: bool
    may_analyze: bool
    reason: str

    @classmethod
    def allowed(cls, reason: str) -> "EligibilityDecision":
        return cls(
            status=EligibilityStatus.ELIGIBLE,
            may_download_audio=True,
            may_analyze=True,
            reason=reason,
        )

    @classmethod
    def blocked(
        cls, status: EligibilityStatus, reason: str
    ) -> "EligibilityDecision":
        return cls(
            status=status,
            may_download_audio=False,
            may_analyze=False,
            reason=reason,
        )


def evaluate_policy(
    policy: ChannelPolicy, video_channel_id: str | None
) -> EligibilityDecision:
    if policy.configuration_status is ConfigurationStatus.CONFIGURATION_REQUIRED:
        return EligibilityDecision.blocked(
            EligibilityStatus.CONFIGURATION_REQUIRED,
            "CHANNEL_CONFIGURATION_REQUIRED",
        )
    if video_channel_id is None:
        return EligibilityDecision.blocked(
            EligibilityStatus.CHANNEL_UNRESOLVED,
            "VIDEO_CHANNEL_UNRESOLVED",
        )
    if policy.policy_kind is PolicyKind.ALL_CHANNELS:
        return EligibilityDecision.allowed("ALL_CHANNELS")
    if video_channel_id != policy.youtube_channel_id:
        return EligibilityDecision.blocked(
            EligibilityStatus.CHANNEL_OUT_OF_SCOPE,
            "FIXED_CHANNEL_MISMATCH",
        )
    return EligibilityDecision.allowed("FIXED_CHANNEL_MATCH")


class ChannelPolicyService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sources = SourceRepository(conn)
        self._audit = AuditRepository(conn)

    def evaluate(
        self,
        subject_id: int,
        video_id: int,
        discovery_method: DiscoveryMethod,
    ) -> EligibilityDecision:
        with transaction(self._conn):
            policy = self._sources.get_policy(subject_id)
            video = self._sources.get_video(video_id)
            return self._evaluate_and_persist(policy, video, discovery_method)

    def evaluate_by_subject_name(
        self,
        name: str,
        video_id: int,
        discovery_method: DiscoveryMethod,
    ) -> EligibilityDecision:
        with transaction(self._conn):
            policy = self._sources.get_policy_by_subject_name(name)
            video = self._sources.get_video(video_id)
            return self._evaluate_and_persist(policy, video, discovery_method)

    def _evaluate_and_persist(
        self,
        policy: ChannelPolicy,
        video: VideoRecord,
        discovery_method: DiscoveryMethod,
    ) -> EligibilityDecision:
        if policy.id is None or policy.subject_id is None or policy.policy_hash is None:
            raise RuntimeError("eligibility requires a persisted channel policy")

        decision = evaluate_policy(policy, video.youtube_channel_id)
        decided_at = datetime.now(timezone.utc)
        entity_id = f"{policy.subject_id}:{video.id}"
        after = {
            "subject_id": policy.subject_id,
            "video_id": video.id,
            "discovery_method": discovery_method.value,
            "status": decision.status.value,
            "policy_id": policy.id,
            "policy_hash": policy.policy_hash,
            "decision_reason": decision.reason,
            "decided_at": utc_iso(decided_at),
        }

        existing = self._conn.execute(
            """
            SELECT
                subject_id,
                video_id,
                discovery_method,
                status,
                policy_id,
                policy_hash,
                decision_reason,
                decided_at
            FROM subject_video_eligibility
            WHERE subject_id = ? AND video_id = ?
            """,
            (policy.subject_id, video.id),
        ).fetchone()
        before = None if existing is None else dict(existing)
        self._conn.execute(
            """
            INSERT INTO subject_video_eligibility(
                subject_id,
                video_id,
                discovery_method,
                status,
                policy_id,
                policy_hash,
                decision_reason,
                decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id, video_id) DO UPDATE SET
                discovery_method = excluded.discovery_method,
                status = excluded.status,
                policy_id = excluded.policy_id,
                policy_hash = excluded.policy_hash,
                decision_reason = excluded.decision_reason,
                decided_at = excluded.decided_at
            """,
            (
                policy.subject_id,
                video.id,
                discovery_method.value,
                decision.status.value,
                policy.id,
                policy.policy_hash,
                decision.reason,
                utc_iso(decided_at),
            ),
        )
        self._audit.append(
            AuditEventInput(
                entity_type="subject_video_eligibility",
                entity_id=entity_id,
                scope_id=None,
                operation="create" if before is None else "update",
                actor_kind="system",
                reason_code=decision.reason,
                reason_text="Channel eligibility evaluated",
                before=before,
                after=after,
                created_at=decided_at,
            )
        )

        return decision

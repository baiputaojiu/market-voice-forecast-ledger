import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    EligibilityStatus,
    PolicyKind,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.sources import (
    ChannelPolicy,
    VideoInput,
    VideoRecord,
)


class SourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_video(self, video: VideoInput) -> int:
        self._conn.execute(
            """
            INSERT INTO videos(
                youtube_video_id,
                youtube_channel_id,
                channel_display_name,
                title,
                published_at,
                duration_seconds,
                live_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(youtube_video_id) DO UPDATE SET
                youtube_channel_id = excluded.youtube_channel_id,
                channel_display_name = excluded.channel_display_name,
                title = excluded.title,
                published_at = excluded.published_at,
                duration_seconds = excluded.duration_seconds,
                live_kind = excluded.live_kind
            """,
            (
                video.youtube_video_id,
                video.youtube_channel_id,
                video.channel_display_name,
                video.title,
                utc_iso(video.published_at),
                video.duration_seconds,
                video.live_kind,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM videos WHERE youtube_video_id = ?",
            (video.youtube_video_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("video upsert did not return an id")
        return row["id"]

    def create_subject(
        self,
        name: str,
        kind: SubjectKind,
        aliases: Sequence[str] = (),
    ) -> int:
        self._conn.execute(
            """
            INSERT INTO analysis_subjects(canonical_name, subject_kind)
            VALUES (?, ?)
            ON CONFLICT(canonical_name) DO NOTHING
            """,
            (name, kind.value),
        )
        row = self._conn.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name = ?", (name,)
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

    def create_policy(self, subject_id: int, policy: ChannelPolicy) -> int:
        policy_hash = _policy_hash(policy)
        updated_at = utc_iso(datetime.now(timezone.utc))
        self._conn.execute(
            """
            INSERT INTO subject_channel_policies(
                subject_id,
                policy_kind,
                configuration_status,
                youtube_channel_id,
                channel_display_name,
                policy_hash,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO NOTHING
            """,
            (
                subject_id,
                policy.policy_kind.value,
                policy.configuration_status.value,
                policy.youtube_channel_id,
                policy.channel_display_name,
                policy_hash,
                updated_at,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM subject_channel_policies WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("policy insert did not return an id")
        return row["id"]

    def get_video(self, video_id: int) -> VideoRecord:
        row = self._conn.execute(
            """
            SELECT
                id,
                youtube_video_id,
                youtube_channel_id,
                channel_display_name,
                title,
                published_at,
                duration_seconds,
                live_kind
            FROM videos
            WHERE id = ?
            """,
            (video_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"video not found: {video_id}")
        return _video_from_row(row)

    def get_policy(self, subject_id: int) -> ChannelPolicy:
        row = self._conn.execute(
            """
            SELECT
                id,
                subject_id,
                policy_kind,
                configuration_status,
                youtube_channel_id,
                channel_display_name,
                policy_hash,
                updated_at
            FROM subject_channel_policies
            WHERE subject_id = ?
            """,
            (subject_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"policy not found for subject: {subject_id}")
        return _policy_from_row(row)

    def get_policy_by_subject_name(self, name: str) -> ChannelPolicy:
        row = self._conn.execute(
            """
            SELECT
                policy.id,
                policy.subject_id,
                policy.policy_kind,
                policy.configuration_status,
                policy.youtube_channel_id,
                policy.channel_display_name,
                policy.policy_hash,
                policy.updated_at
            FROM subject_channel_policies AS policy
            JOIN analysis_subjects AS subject ON subject.id = policy.subject_id
            LEFT JOIN subject_aliases AS alias ON alias.subject_id = subject.id
            WHERE subject.canonical_name = ? OR alias.alias = ?
            LIMIT 1
            """,
            (name, name),
        ).fetchone()
        if row is None:
            raise LookupError(f"policy not found for subject name: {name}")
        return _policy_from_row(row)

    def replace_policy(
        self,
        subject_id: int,
        policy: ChannelPolicy,
        updated_at: datetime,
    ) -> ChannelPolicy:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE subject_channel_policies
            SET
                policy_kind=?,
                configuration_status=?,
                youtube_channel_id=?,
                channel_display_name=?,
                policy_hash=?,
                updated_at=?
            WHERE subject_id=?
            """,
            (
                policy.policy_kind.value,
                policy.configuration_status.value,
                policy.youtube_channel_id,
                policy.channel_display_name,
                _policy_hash(policy),
                utc_iso(updated_at),
                subject_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"policy not found for subject: {subject_id}")
        return self.get_policy(subject_id)

    def list_subject_eligibilities(
        self, subject_id: int
    ) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                """
                SELECT
                    eligibility.id,
                    eligibility.subject_id,
                    eligibility.video_id,
                    eligibility.discovery_method,
                    eligibility.status,
                    eligibility.policy_id,
                    eligibility.policy_hash,
                    eligibility.decision_reason,
                    eligibility.decided_at,
                    video.youtube_channel_id AS video_youtube_channel_id
                FROM subject_video_eligibility AS eligibility
                JOIN videos AS video ON video.id=eligibility.video_id
                WHERE eligibility.subject_id=?
                ORDER BY eligibility.video_id
                """,
                (subject_id,),
            )
        )

    def replace_eligibility(
        self,
        eligibility_id: int,
        *,
        status: EligibilityStatus,
        policy_id: int,
        policy_hash: str,
        decision_reason: str,
        decided_at: datetime,
    ) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE subject_video_eligibility
            SET
                status=?,
                policy_id=?,
                policy_hash=?,
                decision_reason=?,
                decided_at=?
            WHERE id=?
            """,
            (
                status.value,
                policy_id,
                policy_hash,
                decision_reason,
                utc_iso(decided_at),
                eligibility_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"eligibility not found: {eligibility_id}")

    def subject_is_currently_eligible_for_video(
        self, subject_id: int, video_id: int
    ) -> bool:
        return self._conn.execute(
            """
            SELECT 1
            FROM subject_video_eligibility AS eligibility
            JOIN subject_channel_policies AS policy
                ON policy.id=eligibility.policy_id
                AND policy.subject_id=eligibility.subject_id
                AND policy.policy_hash=eligibility.policy_hash
            WHERE eligibility.subject_id=?
                AND eligibility.video_id=?
                AND eligibility.status=?
            LIMIT 1
            """,
            (subject_id, video_id, EligibilityStatus.ELIGIBLE.value),
        ).fetchone() is not None

    def count_videos(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise RuntimeError("source correction requires an active transaction")


def _policy_hash(policy: ChannelPolicy) -> str:
    configuration = {
        "configuration_status": policy.configuration_status.value,
        "policy_kind": policy.policy_kind.value,
        "youtube_channel_id": policy.youtube_channel_id,
    }
    return sha256_text(canonical_json(configuration))


def _video_from_row(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(
        id=row["id"],
        youtube_video_id=row["youtube_video_id"],
        youtube_channel_id=row["youtube_channel_id"],
        channel_display_name=row["channel_display_name"],
        title=row["title"],
        published_at=_parse_utc(row["published_at"]),
        duration_seconds=row["duration_seconds"],
        live_kind=row["live_kind"],
    )


def _policy_from_row(row: sqlite3.Row) -> ChannelPolicy:
    return ChannelPolicy(
        id=row["id"],
        subject_id=row["subject_id"],
        policy_kind=PolicyKind(row["policy_kind"]),
        configuration_status=ConfigurationStatus(row["configuration_status"]),
        youtube_channel_id=row["youtube_channel_id"],
        channel_display_name=row["channel_display_name"],
        policy_hash=row["policy_hash"],
        updated_at=_parse_utc(row["updated_at"]),
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

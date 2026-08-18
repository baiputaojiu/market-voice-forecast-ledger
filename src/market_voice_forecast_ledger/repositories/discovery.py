import sqlite3
from datetime import datetime

from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoveryProfileVersion,
    DiscoverySourceKind,
    PresenceDecision,
    PresenceOrigin,
    PresenceState,
    SubjectVideoCandidate,
    canonical_presence_decision_hash,
    canonical_profile_hash,
)
from market_voice_forecast_ledger.domain.errors import DomainError


class DiscoveryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_profile_version(
        self,
        subject_id: int,
        *,
        seed_channel_ids: tuple[str, ...],
        search_terms: tuple[str, ...],
        created_at: datetime,
    ) -> DiscoveryProfileVersion:
        self._require_transaction()
        config_hash = canonical_profile_hash(seed_channel_ids, search_terms)
        profile = self._conn.execute(
            "SELECT id FROM discovery_profiles WHERE subject_id=?",
            (subject_id,),
        ).fetchone()
        if profile is None:
            cursor = self._conn.execute(
                "INSERT INTO discovery_profiles(subject_id, is_active, created_at) "
                "VALUES (?, 1, ?)",
                (subject_id, utc_iso(created_at)),
            )
            profile_id = cursor.lastrowid
        else:
            profile_id = profile["id"]
        existing = self._conn.execute(
            "SELECT id FROM discovery_profile_versions "
            "WHERE profile_id=? AND config_hash=?",
            (profile_id, config_hash),
        ).fetchone()
        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO discovery_profile_versions(profile_id, config_hash, created_at) "
                "VALUES (?, ?, ?)",
                (profile_id, config_hash, utc_iso(created_at)),
            )
            version_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO discovery_seed_channels("
                "profile_version_id, ordinal, youtube_channel_id) VALUES (?, ?, ?)",
                (
                    (version_id, ordinal, channel_id)
                    for ordinal, channel_id in enumerate(seed_channel_ids, start=1)
                ),
            )
            self._conn.executemany(
                "INSERT INTO discovery_search_terms("
                "profile_version_id, ordinal, search_term) VALUES (?, ?, ?)",
                (
                    (version_id, ordinal, term)
                    for ordinal, term in enumerate(search_terms, start=1)
                ),
            )
        else:
            version_id = existing["id"]
        self._conn.execute(
            "UPDATE discovery_profiles SET current_version_id=? WHERE id=?",
            (version_id, profile_id),
        )
        return self.get_profile_version(version_id)

    def get_current_profile_version(
        self, subject_id: int
    ) -> DiscoveryProfileVersion:
        row = self._conn.execute(
            """
            SELECT current_version_id
            FROM discovery_profiles
            WHERE subject_id=? AND is_active=1
            """,
            (subject_id,),
        ).fetchone()
        if row is None or row["current_version_id"] is None:
            raise LookupError(f"active discovery profile not found: {subject_id}")
        return self.get_profile_version(row["current_version_id"])

    def get_profile_version(self, version_id: int) -> DiscoveryProfileVersion:
        row = self._conn.execute(
            """
            SELECT version.*, profile.subject_id
            FROM discovery_profile_versions AS version
            JOIN discovery_profiles AS profile ON profile.id=version.profile_id
            WHERE version.id=?
            """,
            (version_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"discovery profile version not found: {version_id}")
        seeds = tuple(
            item["youtube_channel_id"]
            for item in self._conn.execute(
                "SELECT youtube_channel_id FROM discovery_seed_channels "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (version_id,),
            )
        )
        terms = tuple(
            item["search_term"]
            for item in self._conn.execute(
                "SELECT search_term FROM discovery_search_terms "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (version_id,),
            )
        )
        if row["config_hash"] != canonical_profile_hash(seeds, terms):
            raise DomainError(
                "STORED_DISCOVERY_PROFILE_HASH_MISMATCH",
                "stored discovery profile does not match its canonical hash",
            )
        return DiscoveryProfileVersion(
            id=row["id"],
            profile_id=row["profile_id"],
            subject_id=row["subject_id"],
            config_hash=row["config_hash"],
            seed_channel_ids=seeds,
            search_terms=terms,
            created_at=_parse_utc(row["created_at"]),
        )

    def create_initial_candidate(
        self,
        *,
        profile_id: int,
        job_id: int,
        metadata: CanonicalVideoMetadata,
        source_kind: DiscoverySourceKind,
        source_key: str,
        observation_hash: str,
        idempotency_key: str,
        observed_at: datetime,
    ) -> SubjectVideoCandidate:
        self._require_transaction()
        video_cursor = self._conn.execute(
            "INSERT INTO videos(youtube_video_id, current_metadata_snapshot_id, created_at) "
            "VALUES (?, NULL, ?)",
            (metadata.youtube_video_id, utc_iso(observed_at)),
        )
        video_id = video_cursor.lastrowid
        snapshot_cursor = self._conn.execute(
            """
            INSERT INTO video_metadata_snapshots(
                video_id, youtube_video_id, channel_id, channel_title, title,
                description, published_at, duration_seconds, live_state,
                actual_start_time, schema_version, canonical_hash, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                metadata.youtube_video_id,
                metadata.channel_id,
                metadata.channel_title,
                metadata.title,
                metadata.description,
                utc_iso(metadata.published_at),
                metadata.duration_seconds,
                metadata.live_state.value,
                None if metadata.actual_start_time is None else utc_iso(metadata.actual_start_time),
                metadata.schema_version,
                metadata.canonical_hash,
                utc_iso(metadata.fetched_at),
            ),
        )
        snapshot_id = snapshot_cursor.lastrowid
        self._conn.execute(
            "UPDATE videos SET current_metadata_snapshot_id=? WHERE id=?",
            (snapshot_id, video_id),
        )
        observation_cursor = self._conn.execute(
            """
            INSERT INTO discovery_observations(
                job_id, profile_id, video_id, metadata_snapshot_id,
                metadata_snapshot_hash, source_kind, source_key, observed_at,
                observation_hash, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, profile_id, video_id, snapshot_id,
                metadata.canonical_hash, source_kind.value, source_key,
                utc_iso(observed_at), observation_hash, idempotency_key,
            ),
        )
        observation_id = observation_cursor.lastrowid
        candidate_cursor = self._conn.execute(
            """
            INSERT INTO subject_video_candidates(
                profile_id, video_id, first_observation_id,
                current_presence_decision_id, created_at
            ) VALUES (?, ?, ?, NULL, ?)
            """,
            (profile_id, video_id, observation_id, utc_iso(observed_at)),
        )
        candidate_id = candidate_cursor.lastrowid
        evidence_ref = f"observation:{observation_id}"
        decision_hash = canonical_presence_decision_hash(
            candidate_id=candidate_id,
            state=PresenceState.UNVERIFIED,
            decision_origin=PresenceOrigin.COLLECTION_INITIAL,
            evidence_ref=evidence_ref,
            evidence_hash=observation_hash,
            created_at=observed_at,
        )
        decision_cursor = self._conn.execute(
            """
            INSERT INTO presence_decisions(
                candidate_id, state, decision_origin, evidence_ref,
                evidence_hash, decision_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id, PresenceState.UNVERIFIED.value,
                PresenceOrigin.COLLECTION_INITIAL.value, evidence_ref,
                observation_hash, decision_hash, utc_iso(observed_at),
            ),
        )
        decision_id = decision_cursor.lastrowid
        self._conn.execute(
            "UPDATE subject_video_candidates SET current_presence_decision_id=? "
            "WHERE id=?",
            (decision_id, candidate_id),
        )
        return SubjectVideoCandidate(
            id=candidate_id,
            profile_id=profile_id,
            video_id=video_id,
            first_observation_id=observation_id,
            current_presence_decision_id=decision_id,
            metadata_snapshot_id=snapshot_id,
            created_at=observed_at,
        )

    def get_presence_decision(self, decision_id: int) -> PresenceDecision:
        row = self._conn.execute(
            "SELECT * FROM presence_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"presence decision not found: {decision_id}")
        return PresenceDecision(
            id=row["id"], candidate_id=row["candidate_id"],
            state=PresenceState(row["state"]),
            decision_origin=PresenceOrigin(row["decision_origin"]),
            evidence_ref=row["evidence_ref"], evidence_hash=row["evidence_hash"],
            decision_hash=row["decision_hash"], created_at=_parse_utc(row["created_at"]),
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "DISCOVERY_TRANSACTION_REQUIRED",
                "discovery persistence requires an active caller transaction",
            )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

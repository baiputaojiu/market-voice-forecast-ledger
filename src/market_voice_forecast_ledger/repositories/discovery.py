import re
import sqlite3
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from collections.abc import Callable

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoveryProfileVersion,
    DiscoverySourceKind,
    LiveState,
    MetadataBatchResult,
    PresenceDecision,
    PresenceOrigin,
    PresenceState,
    SubjectVideoCandidate,
    canonical_presence_decision_hash,
    canonical_profile_hash,
    validate_canonical_video_metadata,
)
from market_voice_forecast_ledger.domain.errors import DomainError


_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SAFE_SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_YOUTUBE_ENDPOINT_CLASSES = frozenset({
    "search_list",
    "channels_list",
    "playlist_items_list",
    "videos_list",
})
_YOUTUBE_UNIT_STAGES = frozenset({
    "youtube_seed_discovery",
    "youtube_search_discovery",
    "youtube_manual_discovery",
})


class DiscoveryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def youtube_attempt_reservation(
        self,
        *,
        job_id: int,
        unit_key: str,
        request_ordinal: int,
    ) -> Callable[[object, int, datetime], None]:
        self._validate_quota_reservation_identity(
            job_id=job_id,
            unit_key=unit_key,
            request_ordinal=request_ordinal,
        )
        database_path = self._database_path()

        def reserve(
            endpoint_class: object,
            attempt_no: int,
            attempted_at: datetime,
        ) -> None:
            endpoint_value = (
                endpoint_class.value
                if isinstance(endpoint_class, StrEnum)
                and type(endpoint_class.value) is str
                else None
            )
            if endpoint_value not in _YOUTUBE_ENDPOINT_CLASSES:
                raise DomainError(
                    "YOUTUBE_QUOTA_RESERVATION_INVALID",
                    "YouTube quota reservation is invalid",
                )
            conn: sqlite3.Connection | None = None
            try:
                conn = open_database(database_path)
                with transaction(conn):
                    DiscoveryRepository(conn).reserve_youtube_quota_attempt(
                        job_id=job_id,
                        unit_key=unit_key,
                        request_ordinal=request_ordinal,
                        attempt_no=attempt_no,
                        endpoint_class=endpoint_value,
                        attempted_at=attempted_at,
                    )
            except DomainError:
                raise
            except (OSError, sqlite3.Error):
                raise DomainError(
                    "YOUTUBE_QUOTA_RESERVATION_STORAGE_FAILED",
                    "YouTube quota reservation could not be stored",
                ) from None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

        return reserve

    def reserve_youtube_quota_attempt(
        self,
        *,
        job_id: int,
        unit_key: str,
        request_ordinal: int,
        attempt_no: int,
        endpoint_class: str,
        attempted_at: datetime,
    ) -> int:
        self._require_transaction()
        self._validate_quota_reservation_identity(
            job_id=job_id,
            unit_key=unit_key,
            request_ordinal=request_ordinal,
        )
        if (
            type(attempt_no) is not int
            or attempt_no <= 0
            or attempt_no > 4
            or type(endpoint_class) is not str
            or endpoint_class not in _YOUTUBE_ENDPOINT_CLASSES
            or not _is_exact_utc(attempted_at)
        ):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "YouTube quota reservation is invalid",
            )
        existing_rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_quota_reservations "
                "WHERE job_id=? AND unit_key=? AND request_ordinal=? "
                "ORDER BY attempt_no",
                (job_id, unit_key, request_ordinal),
            )
        )
        for row in existing_rows:
            self._validate_stored_quota_reservation(
                row,
                job_id=job_id,
                unit_key=unit_key,
                request_ordinal=request_ordinal,
            )
        if any(row["attempt_no"] == attempt_no for row in existing_rows):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_CONFLICT",
                "YouTube quota reservation already exists",
            )
        expected_attempts = tuple(range(1, len(existing_rows) + 1))
        stored_attempts = tuple(row["attempt_no"] for row in existing_rows)
        if stored_attempts != expected_attempts or attempt_no != len(existing_rows) + 1:
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_SEQUENCE_INVALID",
                "YouTube quota reservation attempt sequence is invalid",
            )
        try:
            cursor = self._conn.execute(
                "INSERT INTO youtube_quota_reservations("
                "job_id, unit_key, request_ordinal, attempt_no, "
                "endpoint_class, attempted_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    unit_key,
                    request_ordinal,
                    attempt_no,
                    endpoint_class,
                    utc_iso(attempted_at),
                ),
            )
        except sqlite3.IntegrityError:
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "YouTube quota reservation is invalid",
            ) from None
        row = self._conn.execute(
            "SELECT * FROM youtube_quota_reservations WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
            )
        self._validate_stored_quota_reservation(
            row,
            job_id=job_id,
            unit_key=unit_key,
            request_ordinal=request_ordinal,
        )
        if (
            row["attempt_no"] != attempt_no
            or row["endpoint_class"] != endpoint_class
            or row["attempted_at"] != utc_iso(attempted_at)
        ):
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
            )
        return row["id"]

    def _validate_quota_reservation_identity(
        self,
        *,
        job_id: object,
        unit_key: object,
        request_ordinal: object,
    ) -> None:
        if (
            type(job_id) is not int
            or job_id <= 0
            or type(unit_key) is not str
            or _SAFE_SOURCE_KEY.fullmatch(unit_key) is None
            or type(request_ordinal) is not int
            or request_ordinal <= 0
        ):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "YouTube quota reservation is invalid",
            )
        owner = self._conn.execute(
            "SELECT job.job_kind, unit.stage "
            "FROM jobs AS job "
            "JOIN job_units AS unit ON unit.job_id=job.id "
            "WHERE job.id=? AND unit.unit_key=?",
            (job_id, unit_key),
        ).fetchone()
        if (
            owner is None
            or owner["job_kind"] != "youtube_sync"
            or owner["stage"] not in _YOUTUBE_UNIT_STAGES
        ):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "YouTube quota reservation is invalid",
            )

    def _validate_stored_quota_reservation(
        self,
        row: sqlite3.Row,
        *,
        job_id: int,
        unit_key: str,
        request_ordinal: int,
    ) -> None:
        try:
            _parse_canonical_utc(row["attempted_at"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
            ) from cause
        if (
            type(row["id"]) is not int
            or row["id"] <= 0
            or row["job_id"] != job_id
            or row["unit_key"] != unit_key
            or row["request_ordinal"] != request_ordinal
            or type(row["attempt_no"]) is not int
            or row["attempt_no"] <= 0
            or row["attempt_no"] > 4
            or type(row["endpoint_class"]) is not str
            or row["endpoint_class"] not in _YOUTUBE_ENDPOINT_CLASSES
        ):
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
            )

    def _database_path(self) -> Path:
        rows = tuple(self._conn.execute("PRAGMA database_list"))
        main_rows = tuple(row for row in rows if row["name"] == "main")
        if (
            len(main_rows) != 1
            or type(main_rows[0]["file"]) is not str
            or not main_rows[0]["file"]
        ):
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_STORAGE_FAILED",
                "YouTube quota reservation requires a file database",
            )
        return Path(main_rows[0]["file"])

    def create_profile_version(
        self,
        subject_id: int,
        *,
        seed_channel_ids: tuple[str, ...],
        search_terms: tuple[str, ...],
        created_at: datetime,
    ) -> DiscoveryProfileVersion:
        self._require_transaction()
        if type(subject_id) is not int or subject_id <= 0 or not _is_exact_utc(
            created_at
        ):
            raise DomainError(
                "DISCOVERY_PROFILE_INVALID",
                "profile identity and creation time are invalid",
            )
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
            SELECT id, subject_id, current_version_id
            FROM discovery_profiles
            WHERE subject_id=? AND is_active=1
            """,
            (subject_id,),
        ).fetchone()
        if row is None or row["current_version_id"] is None:
            raise LookupError(f"active discovery profile not found: {subject_id}")
        version = self.get_profile_version(row["current_version_id"])
        self._validate_profile_owner(version, row["id"], row["subject_id"])
        return version

    def get_current_profile_version_by_subject_name(
        self, subject_name: str
    ) -> DiscoveryProfileVersion:
        if type(subject_name) is not str or not subject_name:
            raise DomainError(
                "DISCOVERY_PROFILE_INVALID",
                "subject name is invalid",
            )
        row = self._conn.execute(
            """
            SELECT profile.id, profile.subject_id, profile.current_version_id
            FROM discovery_profiles AS profile
            JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
            WHERE subject.canonical_name=?
              AND subject.is_active=1
              AND profile.is_active=1
            """,
            (subject_name,),
        ).fetchone()
        if row is None or row["current_version_id"] is None:
            raise LookupError(f"active discovery profile not found: {subject_name}")
        version = self.get_profile_version(row["current_version_id"])
        self._validate_profile_owner(version, row["id"], row["subject_id"])
        return version

    def list_active_profile_versions(self) -> tuple[DiscoveryProfileVersion, ...]:
        rows = self._conn.execute(
            """
            SELECT profile.id, profile.subject_id, profile.current_version_id
            FROM discovery_profiles AS profile
            JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
            WHERE profile.is_active=1 AND subject.is_active=1
            ORDER BY profile.id
            """
        )
        versions: list[DiscoveryProfileVersion] = []
        for row in rows:
            if row["current_version_id"] is None:
                raise DomainError(
                    "STORED_DISCOVERY_PROFILE_INVALID",
                    "active discovery profile has no current version",
                )
            version = self.get_profile_version(row["current_version_id"])
            self._validate_profile_owner(version, row["id"], row["subject_id"])
            versions.append(version)
        return tuple(versions)

    @staticmethod
    def _validate_profile_owner(
        version: DiscoveryProfileVersion, profile_id: object, subject_id: object
    ) -> None:
        if (
            type(profile_id) is not int
            or type(subject_id) is not int
            or version.profile_id != profile_id
            or version.subject_id != subject_id
        ):
            raise DomainError(
                "STORED_DISCOVERY_PROFILE_INVALID",
                "stored discovery profile pointer has the wrong owner",
            )

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
        seed_rows = tuple(
            self._conn.execute(
                "SELECT ordinal, youtube_channel_id FROM discovery_seed_channels "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (version_id,),
            )
        )
        term_rows = tuple(
            self._conn.execute(
                "SELECT ordinal, search_term FROM discovery_search_terms "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (version_id,),
            )
        )
        if tuple(item["ordinal"] for item in seed_rows) != tuple(
            range(1, len(seed_rows) + 1)
        ) or tuple(item["ordinal"] for item in term_rows) != tuple(
            range(1, len(term_rows) + 1)
        ):
            raise DomainError(
                "STORED_DISCOVERY_PROFILE_INVALID",
                "stored discovery profile ordinals are not contiguous",
            )
        seeds = tuple(item["youtube_channel_id"] for item in seed_rows)
        terms = tuple(item["search_term"] for item in term_rows)
        try:
            expected_hash = canonical_profile_hash(seeds, terms)
            created_at = _parse_canonical_utc(row["created_at"])
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_DISCOVERY_PROFILE_INVALID",
                "stored discovery profile is invalid",
            ) from cause
        if row["config_hash"] != expected_hash:
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
            created_at=created_at,
        )

    def persist_metadata_batch(
        self,
        job_id: int,
        profile_version_id: int,
        source_kind: DiscoverySourceKind,
        source_key: str,
        items: tuple[CanonicalVideoMetadata, ...],
        observed_at: datetime,
    ) -> MetadataBatchResult:
        self._require_transaction()
        self._validate_batch_arguments(
            job_id=job_id,
            profile_version_id=profile_version_id,
            source_kind=source_kind,
            source_key=source_key,
            items=items,
            observed_at=observed_at,
        )
        for item in items:
            validate_canonical_video_metadata(item)
        _validate_batch_video_duplicates(items)

        profile_version = self.get_profile_version(profile_version_id)
        profile = self._conn.execute(
            """
            SELECT profile.is_active, subject.is_active AS subject_is_active
            FROM discovery_profiles AS profile
            JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
            WHERE profile.id=?
            """,
            (profile_version.profile_id,),
        ).fetchone()
        if (
            profile is None
            or profile["is_active"] != 1
            or profile["subject_is_active"] != 1
        ):
            raise DomainError(
                "DISCOVERY_PROFILE_NOT_FOUND",
                "metadata persistence requires an active discovery profile",
            )
        job = self._conn.execute(
            "SELECT job_kind FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if job is None or job["job_kind"] != "youtube_sync":
            raise DomainError(
                "DISCOVERY_METADATA_BATCH_INVALID",
                "metadata persistence requires a YouTube sync job",
            )

        observed_at_text = utc_iso(observed_at)
        for item in items:
            existing = self._find_observation(
                _observation_idempotency_key(
                    job_id=job_id,
                    profile_id=profile_version.profile_id,
                    source_kind=source_kind,
                    source_key=source_key,
                    youtube_video_id=item.youtube_video_id,
                )
            )
            if existing is not None:
                self._validate_existing_observation(
                    existing,
                    job_id=job_id,
                    profile_id=profile_version.profile_id,
                    source_kind=source_kind,
                    source_key=source_key,
                    metadata=item,
                    observed_at_text=observed_at_text,
                )

        snapshot_ids: list[int] = []
        observation_ids: list[int] = []
        candidate_ids: list[int] = []
        for item in items:
            video_id = self._get_or_create_video(item.youtube_video_id, observed_at)
            snapshot_id = self._get_or_create_snapshot(video_id, item)
            observation_id, observation_hash = self._get_or_create_observation(
                job_id=job_id,
                profile_id=profile_version.profile_id,
                video_id=video_id,
                snapshot_id=snapshot_id,
                metadata=item,
                source_kind=source_kind,
                source_key=source_key,
                observed_at=observed_at,
            )
            candidate_id = self._get_or_create_candidate(
                profile_id=profile_version.profile_id,
                video_id=video_id,
                observation_id=observation_id,
                observation_hash=observation_hash,
                created_at=observed_at,
            )
            snapshot_ids.append(snapshot_id)
            observation_ids.append(observation_id)
            candidate_ids.append(candidate_id)
        return MetadataBatchResult(
            snapshot_ids=tuple(snapshot_ids),
            observation_ids=tuple(observation_ids),
            candidate_ids=tuple(candidate_ids),
        )

    @staticmethod
    def _validate_batch_arguments(
        *,
        job_id: object,
        profile_version_id: object,
        source_kind: object,
        source_key: object,
        items: object,
        observed_at: object,
    ) -> None:
        if (
            type(job_id) is not int
            or job_id <= 0
            or type(profile_version_id) is not int
            or profile_version_id <= 0
            or type(source_kind) is not DiscoverySourceKind
            or type(source_key) is not str
            or _SAFE_SOURCE_KEY.fullmatch(source_key) is None
            or type(items) is not tuple
            or len(items) > 50
            or not _is_exact_utc(observed_at)
        ):
            raise DomainError(
                "DISCOVERY_METADATA_BATCH_INVALID",
                "metadata batch has an invalid canonical shape",
            )

    def _get_or_create_video(
        self, youtube_video_id: str, created_at: datetime
    ) -> int:
        row = self._conn.execute(
            "SELECT * FROM videos WHERE youtube_video_id=?",
            (youtube_video_id,),
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
                "INSERT INTO videos(youtube_video_id, current_metadata_snapshot_id, "
                "created_at) VALUES (?, NULL, ?)",
                (youtube_video_id, utc_iso(created_at)),
            )
            return cursor.lastrowid
        try:
            _parse_canonical_utc(row["created_at"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored video identity is invalid",
            ) from cause
        if type(row["id"]) is not int or row["youtube_video_id"] != youtube_video_id:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored video identity is invalid",
            )
        if row["current_metadata_snapshot_id"] is None:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored video metadata pointer is invalid",
            )
        current = self._conn.execute(
            "SELECT * FROM video_metadata_snapshots WHERE id=?",
            (row["current_metadata_snapshot_id"],),
        ).fetchone()
        if current is None or current["video_id"] != row["id"]:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored video metadata pointer is invalid",
            )
        _validate_snapshot_integrity(current)
        return row["id"]

    def _get_or_create_snapshot(
        self, video_id: int, metadata: CanonicalVideoMetadata
    ) -> int:
        row = self._conn.execute(
            "SELECT * FROM video_metadata_snapshots "
            "WHERE video_id=? AND canonical_hash=?",
            (video_id, metadata.canonical_hash),
        ).fetchone()
        if row is None:
            cursor = self._conn.execute(
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
                    None
                    if metadata.actual_start_time is None
                    else utc_iso(metadata.actual_start_time),
                    metadata.schema_version,
                    metadata.canonical_hash,
                    utc_iso(metadata.fetched_at),
                ),
            )
            snapshot_id = cursor.lastrowid
        else:
            self._validate_snapshot_row(row, metadata)
            snapshot_id = row["id"]
        self._conn.execute(
            "UPDATE videos SET current_metadata_snapshot_id=? WHERE id=? "
            "AND current_metadata_snapshot_id IS NOT ?",
            (snapshot_id, video_id, snapshot_id),
        )
        return snapshot_id

    def _get_or_create_observation(
        self,
        *,
        job_id: int,
        profile_id: int,
        video_id: int,
        snapshot_id: int,
        metadata: CanonicalVideoMetadata,
        source_kind: DiscoverySourceKind,
        source_key: str,
        observed_at: datetime,
    ) -> tuple[int, str]:
        idempotency_key = _observation_idempotency_key(
            job_id=job_id,
            profile_id=profile_id,
            source_kind=source_kind,
            source_key=source_key,
            youtube_video_id=metadata.youtube_video_id,
        )
        observation_hash = _observation_hash(
            idempotency_key=idempotency_key,
            snapshot_id=snapshot_id,
            metadata_snapshot_hash=metadata.canonical_hash,
            observed_at=observed_at,
        )
        existing = self._find_observation(idempotency_key)
        if existing is not None:
            self._validate_existing_observation(
                existing,
                job_id=job_id,
                profile_id=profile_id,
                source_kind=source_kind,
                source_key=source_key,
                metadata=metadata,
                observed_at_text=utc_iso(observed_at),
            )
            if (
                existing["video_id"] != video_id
                or existing["metadata_snapshot_id"] != snapshot_id
                or existing["observation_hash"] != observation_hash
            ):
                _raise_observation_conflict()
            return existing["id"], observation_hash
        cursor = self._conn.execute(
            """
            INSERT INTO discovery_observations(
                job_id, profile_id, video_id, metadata_snapshot_id,
                metadata_snapshot_hash, source_kind, source_key, observed_at,
                observation_hash, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                profile_id,
                video_id,
                snapshot_id,
                metadata.canonical_hash,
                source_kind.value,
                source_key,
                utc_iso(observed_at),
                observation_hash,
                idempotency_key,
            ),
        )
        return cursor.lastrowid, observation_hash

    def _get_or_create_candidate(
        self,
        *,
        profile_id: int,
        video_id: int,
        observation_id: int,
        observation_hash: str,
        created_at: datetime,
    ) -> int:
        row = self._conn.execute(
            "SELECT * FROM subject_video_candidates WHERE profile_id=? AND video_id=?",
            (profile_id, video_id),
        ).fetchone()
        if row is not None:
            self._validate_candidate_row(row, profile_id=profile_id, video_id=video_id)
            return row["id"]
        cursor = self._conn.execute(
            """
            INSERT INTO subject_video_candidates(
                profile_id, video_id, first_observation_id,
                current_presence_decision_id, created_at
            ) VALUES (?, ?, ?, NULL, ?)
            """,
            (profile_id, video_id, observation_id, utc_iso(created_at)),
        )
        candidate_id = cursor.lastrowid
        evidence_ref = f"observation:{observation_id}"
        decision_hash = canonical_presence_decision_hash(
            candidate_id=candidate_id,
            state=PresenceState.UNVERIFIED,
            decision_origin=PresenceOrigin.COLLECTION_INITIAL,
            evidence_ref=evidence_ref,
            evidence_hash=observation_hash,
            created_at=created_at,
        )
        decision_cursor = self._conn.execute(
            """
            INSERT INTO presence_decisions(
                candidate_id, state, decision_origin, evidence_ref,
                evidence_hash, decision_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                PresenceState.UNVERIFIED.value,
                PresenceOrigin.COLLECTION_INITIAL.value,
                evidence_ref,
                observation_hash,
                decision_hash,
                utc_iso(created_at),
            ),
        )
        self._conn.execute(
            "UPDATE subject_video_candidates SET current_presence_decision_id=? "
            "WHERE id=?",
            (decision_cursor.lastrowid, candidate_id),
        )
        return candidate_id

    def _find_observation(self, idempotency_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT observation.*, video.youtube_video_id
            FROM discovery_observations AS observation
            JOIN videos AS video ON video.id=observation.video_id
            WHERE observation.idempotency_key=?
            """,
            (idempotency_key,),
        ).fetchone()

    def _validate_existing_observation(
        self,
        row: sqlite3.Row,
        *,
        job_id: int,
        profile_id: int,
        source_kind: DiscoverySourceKind,
        source_key: str,
        metadata: CanonicalVideoMetadata,
        observed_at_text: str,
    ) -> None:
        self._validate_stored_observation(row)
        if (
            row["job_id"] != job_id
            or row["profile_id"] != profile_id
            or row["youtube_video_id"] != metadata.youtube_video_id
            or row["metadata_snapshot_hash"] != metadata.canonical_hash
            or row["source_kind"] != source_kind.value
            or row["source_key"] != source_key
            or row["observed_at"] != observed_at_text
        ):
            _raise_observation_conflict()
        snapshot = self._conn.execute(
            "SELECT * FROM video_metadata_snapshots WHERE id=?",
            (row["metadata_snapshot_id"],),
        ).fetchone()
        if snapshot is None:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored discovery observation snapshot is missing",
            )
        self._validate_snapshot_row(snapshot, metadata)

    def _validate_stored_observation(self, row: sqlite3.Row) -> None:
        try:
            source_kind = DiscoverySourceKind(row["source_kind"])
            observed_at = _parse_canonical_utc(row["observed_at"])
            expected_key = _observation_idempotency_key(
                job_id=row["job_id"],
                profile_id=row["profile_id"],
                source_kind=source_kind,
                source_key=row["source_key"],
                youtube_video_id=row["youtube_video_id"],
            )
            expected_hash = _observation_hash(
                idempotency_key=row["idempotency_key"],
                snapshot_id=row["metadata_snapshot_id"],
                metadata_snapshot_hash=row["metadata_snapshot_hash"],
                observed_at=observed_at,
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_DISCOVERY_OBSERVATION_INVALID",
                "stored discovery observation is invalid",
            ) from cause
        if (
            type(row["id"]) is not int
            or type(row["job_id"]) is not int
            or type(row["profile_id"]) is not int
            or type(row["video_id"]) is not int
            or type(row["metadata_snapshot_id"]) is not int
            or type(row["source_key"]) is not str
            or _SAFE_SOURCE_KEY.fullmatch(row["source_key"]) is None
            or type(row["youtube_video_id"]) is not str
            or row["idempotency_key"] != expected_key
            or row["observation_hash"] != expected_hash
        ):
            raise DomainError(
                "STORED_DISCOVERY_OBSERVATION_INVALID",
                "stored discovery observation is invalid",
            )
        snapshot = self._conn.execute(
            "SELECT * FROM video_metadata_snapshots WHERE id=?",
            (row["metadata_snapshot_id"],),
        ).fetchone()
        if snapshot is None:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored discovery observation snapshot is missing",
            )
        _validate_snapshot_integrity(snapshot)
        if (
            snapshot["video_id"] != row["video_id"]
            or snapshot["youtube_video_id"] != row["youtube_video_id"]
            or snapshot["canonical_hash"] != row["metadata_snapshot_hash"]
        ):
            raise DomainError(
                "STORED_DISCOVERY_OBSERVATION_INVALID",
                "stored discovery observation is invalid",
            )

    @staticmethod
    def _validate_snapshot_row(
        row: sqlite3.Row, metadata: CanonicalVideoMetadata
    ) -> None:
        _validate_snapshot_integrity(row)
        try:
            published_at = _parse_canonical_utc(row["published_at"])
            fetched_at = _parse_canonical_utc(row["fetched_at"])
            actual_start_time = (
                None
                if row["actual_start_time"] is None
                else _parse_canonical_utc(row["actual_start_time"])
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored discovery metadata is invalid",
            ) from cause
        if (
            type(row["id"]) is not int
            or row["youtube_video_id"] != metadata.youtube_video_id
            or row["channel_id"] != metadata.channel_id
            or row["channel_title"] != metadata.channel_title
            or row["title"] != metadata.title
            or row["description"] != metadata.description
            or published_at != metadata.published_at
            or type(row["duration_seconds"]) is not int
            or row["duration_seconds"] != metadata.duration_seconds
            or row["live_state"] != metadata.live_state.value
            or actual_start_time != metadata.actual_start_time
            or row["schema_version"] != metadata.schema_version
            or row["canonical_hash"] != metadata.canonical_hash
            or not _is_exact_utc(fetched_at)
        ):
            raise DomainError(
                "STORED_DISCOVERY_METADATA_INVALID",
                "stored discovery metadata is invalid",
            )

    def _validate_candidate_row(
        self, row: sqlite3.Row, *, profile_id: int, video_id: int
    ) -> None:
        try:
            _parse_canonical_utc(row["created_at"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_DISCOVERY_CANDIDATE_INVALID",
                "stored discovery candidate is invalid",
            ) from cause
        first = self._conn.execute(
            """
            SELECT observation.*, video.youtube_video_id
            FROM discovery_observations AS observation
            JOIN videos AS video ON video.id=observation.video_id
            WHERE observation.id=?
            """,
            (row["first_observation_id"],),
        ).fetchone()
        if (
            row["profile_id"] != profile_id
            or row["video_id"] != video_id
            or first is None
            or first["profile_id"] != profile_id
            or first["video_id"] != video_id
            or row["current_presence_decision_id"] is None
        ):
            raise DomainError(
                "STORED_DISCOVERY_CANDIDATE_INVALID",
                "stored discovery candidate is invalid",
            )
        self._validate_stored_observation(first)
        initial_rows = self._conn.execute(
            """
            SELECT id
            FROM presence_decisions
            WHERE candidate_id=? AND decision_origin=?
            ORDER BY id
            """,
            (row["id"], PresenceOrigin.COLLECTION_INITIAL.value),
        ).fetchall()
        if len(initial_rows) != 1:
            raise DomainError(
                "STORED_DISCOVERY_CANDIDATE_INVALID",
                "stored discovery candidate is invalid",
            )
        initial = self.get_presence_decision(initial_rows[0]["id"])
        if (
            initial.candidate_id != row["id"]
            or initial.state is not PresenceState.UNVERIFIED
            or initial.decision_origin is not PresenceOrigin.COLLECTION_INITIAL
            or initial.evidence_ref != f'observation:{first["id"]}'
            or initial.evidence_hash != first["observation_hash"]
        ):
            raise DomainError(
                "STORED_DISCOVERY_CANDIDATE_INVALID",
                "stored discovery candidate is invalid",
            )
        decision = self.get_presence_decision(row["current_presence_decision_id"])
        if decision.candidate_id != row["id"]:
            raise DomainError(
                "STORED_DISCOVERY_CANDIDATE_INVALID",
                "stored discovery candidate is invalid",
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
        try:
            state = PresenceState(row["state"])
            origin = PresenceOrigin(row["decision_origin"])
            created_at = _parse_canonical_utc(row["created_at"])
            expected_hash = canonical_presence_decision_hash(
                candidate_id=row["candidate_id"],
                state=state,
                decision_origin=origin,
                evidence_ref=row["evidence_ref"],
                evidence_hash=row["evidence_hash"],
                created_at=created_at,
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_PRESENCE_DECISION_INVALID",
                "stored presence decision is invalid",
            ) from cause
        if (
            type(row["id"]) is not int
            or type(row["candidate_id"]) is not int
            or type(row["evidence_ref"]) is not str
            or type(row["evidence_hash"]) is not str
            or row["decision_hash"] != expected_hash
        ):
            raise DomainError(
                "STORED_PRESENCE_DECISION_INVALID",
                "stored presence decision is invalid",
            )
        return PresenceDecision(
            id=row["id"],
            candidate_id=row["candidate_id"],
            state=state,
            decision_origin=origin,
            evidence_ref=row["evidence_ref"],
            evidence_hash=row["evidence_hash"],
            decision_hash=row["decision_hash"],
            created_at=created_at,
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "DISCOVERY_TRANSACTION_REQUIRED",
                "discovery persistence requires an active caller transaction",
            )


def _parse_utc(value: str) -> datetime:
    return _parse_canonical_utc(value)


def _parse_canonical_utc(value: object) -> datetime:
    if type(value) is not str or _UTC_TEXT.fullmatch(value) is None:
        raise ValueError("stored datetime is not canonical UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not _is_exact_utc(parsed) or utc_iso(parsed) != value:
        raise ValueError("stored datetime is not canonical UTC")
    return parsed


def _is_exact_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is timezone.utc


def _validate_batch_video_duplicates(
    items: tuple[CanonicalVideoMetadata, ...]
) -> None:
    hashes_by_video: dict[str, str] = {}
    for item in items:
        previous = hashes_by_video.setdefault(
            item.youtube_video_id, item.canonical_hash
        )
        if previous != item.canonical_hash:
            raise DomainError(
                "DISCOVERY_METADATA_BATCH_CONFLICT",
                "metadata batch contains conflicting duplicate video identities",
            )


def _validate_snapshot_integrity(row: sqlite3.Row) -> None:
    try:
        stored = CanonicalVideoMetadata(
            youtube_video_id=row["youtube_video_id"],
            channel_id=row["channel_id"],
            channel_title=row["channel_title"],
            title=row["title"],
            description=row["description"],
            published_at=_parse_canonical_utc(row["published_at"]),
            duration_seconds=row["duration_seconds"],
            live_state=LiveState(row["live_state"]),
            actual_start_time=(
                None
                if row["actual_start_time"] is None
                else _parse_canonical_utc(row["actual_start_time"])
            ),
            schema_version=row["schema_version"],
            canonical_hash=row["canonical_hash"],
            fetched_at=_parse_canonical_utc(row["fetched_at"]),
        )
        validate_canonical_video_metadata(stored)
    except (DomainError, TypeError, ValueError) as cause:
        raise DomainError(
            "STORED_DISCOVERY_METADATA_INVALID",
            "stored discovery metadata is invalid",
        ) from cause
    if type(row["id"]) is not int or type(row["video_id"]) is not int:
        raise DomainError(
            "STORED_DISCOVERY_METADATA_INVALID",
            "stored discovery metadata is invalid",
        )


def _observation_idempotency_key(
    *,
    job_id: int,
    profile_id: int,
    source_kind: DiscoverySourceKind,
    source_key: str,
    youtube_video_id: str,
) -> str:
    return sha256_text(canonical_json({
        "schema": "youtube-discovery-observation-key.v1",
        "job_id": job_id,
        "profile_id": profile_id,
        "source_kind": source_kind.value,
        "source_key": source_key,
        "youtube_video_id": youtube_video_id,
    }))


def _observation_hash(
    *,
    idempotency_key: str,
    snapshot_id: int,
    metadata_snapshot_hash: str,
    observed_at: datetime,
) -> str:
    return sha256_text(canonical_json({
        "schema": "youtube-discovery-observation.v1",
        "idempotency_key": idempotency_key,
        "metadata_snapshot_id": snapshot_id,
        "metadata_snapshot_hash": metadata_snapshot_hash,
        "observed_at": utc_iso(observed_at),
    }))


def _raise_observation_conflict() -> None:
    raise DomainError(
        "DISCOVERY_OBSERVATION_CONFLICT",
        "stored discovery observation conflicts with its idempotency key",
    )

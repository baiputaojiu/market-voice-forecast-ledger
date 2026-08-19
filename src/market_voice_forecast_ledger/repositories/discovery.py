import json
import re
import sqlite3
from datetime import date, datetime, timezone
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
    SearchWindow,
    SubjectVideoCandidate,
    YouTubeSyncCheckpoint,
    YouTubeSyncKind,
    YouTubeSyncManifest,
    YouTubeSyncManifestProfile,
    build_youtube_sync_shape,
    canonical_manual_unit_output_hash,
    canonical_search_window_hash,
    canonical_source_cursor_hash,
    canonical_youtube_sync_checkpoint_hash,
    canonical_presence_decision_hash,
    canonical_profile_hash,
    validate_canonical_video_metadata,
    youtube_manual_video_hash,
    youtube_profile_set_hash,
)
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit


_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_CANONICAL_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_YOUTUBE_UPLOADS_PLAYLIST_ID = re.compile(r"^UU[A-Za-z0-9_-]{22}$")
_YOUTUBE_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
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

    def youtube_attempt_reservation_chain(
        self, *, job_id: int, unit_key: str
    ) -> Callable[[object, int, datetime], None]:
        self._validate_quota_reservation_identity(
            job_id=job_id,
            unit_key=unit_key,
            request_ordinal=1,
        )
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_quota_reservations "
                "WHERE job_id=? AND unit_key=? "
                "ORDER BY request_ordinal, attempt_no",
                (job_id, unit_key),
            )
        )
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            request_ordinal = row["request_ordinal"]
            if type(request_ordinal) is not int or request_ordinal <= 0:
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
            grouped.setdefault(request_ordinal, []).append(row)
        if tuple(grouped) != tuple(range(1, len(grouped) + 1)):
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
            )
        for grouped_rows in grouped.values():
            if (
                tuple(row["attempt_no"] for row in grouped_rows)
                != tuple(range(1, len(grouped_rows) + 1))
                or len({row["endpoint_class"] for row in grouped_rows}) != 1
            ):
                raise DomainError(
                    "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                    "stored YouTube quota reservation is invalid",
                )

        current_ordinal = len(grouped)

        def reserve(
            endpoint_class: object,
            attempt_no: int,
            attempted_at: datetime,
        ) -> None:
            nonlocal current_ordinal
            if attempt_no == 1:
                current_ordinal += 1
            elif current_ordinal <= len(grouped):
                raise DomainError(
                    "YOUTUBE_QUOTA_RESERVATION_SEQUENCE_INVALID",
                    "YouTube quota reservation attempt sequence is invalid",
                )
            self.youtube_attempt_reservation(
                job_id=job_id,
                unit_key=unit_key,
                request_ordinal=current_ordinal,
            )(endpoint_class, attempt_no, attempted_at)

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
        stored_endpoint_classes = {
            row["endpoint_class"] for row in existing_rows
        }
        if len(stored_endpoint_classes) > 1:
            raise DomainError(
                "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID",
                "stored YouTube quota reservation is invalid",
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
        if existing_rows and endpoint_class != existing_rows[0]["endpoint_class"]:
            raise DomainError(
                "YOUTUBE_QUOTA_RESERVATION_INVALID",
                "YouTube quota reservation is invalid",
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

    def get_active_manual_profile_version(
        self, subject_id: int
    ) -> DiscoveryProfileVersion:
        if type(subject_id) is not int or subject_id <= 0:
            raise DomainError(
                "YOUTUBE_SYNC_REQUEST_INVALID",
                "manual YouTube subject identity is invalid",
            )
        subject = self._conn.execute(
            "SELECT id, is_active FROM analysis_subjects WHERE id=?",
            (subject_id,),
        ).fetchone()
        if subject is None:
            raise LookupError(f"analysis subject not found: {subject_id}")
        profile = self._conn.execute(
            "SELECT id, subject_id, current_version_id, is_active "
            "FROM discovery_profiles WHERE subject_id=?",
            (subject_id,),
        ).fetchone()
        if (
            subject["is_active"] != 1
            or profile is None
            or profile["is_active"] != 1
            or profile["current_version_id"] is None
        ):
            raise DomainError(
                "DISCOVERY_PROFILE_NOT_ACTIVE",
                "manual discovery requires an active subject and profile",
            )
        version = self.get_profile_version(profile["current_version_id"])
        self._validate_profile_owner(
            version, profile["id"], profile["subject_id"]
        )
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

    def create_youtube_sync_manifest(
        self,
        *,
        job_id: int,
        sync_kind: str,
        upper_bound: datetime,
        backfill_floor: datetime,
        quota_contract_version: str,
        profiles: tuple[DiscoveryProfileVersion, ...],
        manual_request_id: int | None,
        created_at: datetime,
    ) -> YouTubeSyncManifest:
        self._require_transaction()
        try:
            kind = YouTubeSyncKind(sync_kind)
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "YOUTUBE_SYNC_MANIFEST_INVALID",
                "YouTube sync manifest is invalid",
            ) from cause
        if not _is_exact_utc(created_at):
            raise DomainError(
                "YOUTUBE_SYNC_MANIFEST_INVALID",
                "YouTube sync manifest is invalid",
            )
        manual_video_id: str | None = None
        if kind is YouTubeSyncKind.FULL_DISCOVERY:
            current = self.list_active_profile_versions()
            if profiles != current:
                raise DomainError(
                    "YOUTUBE_SYNC_MANIFEST_INVALID",
                    "full sync must bind every current active profile version",
                )
        else:
            request = self._manual_request(manual_request_id)
            current = self.get_current_profile_version(request["subject_id"])
            if profiles != (current,) or request["profile_id"] != current.profile_id:
                raise DomainError(
                    "YOUTUBE_SYNC_MANIFEST_INVALID",
                    "manual sync profile binding is invalid",
                )
            manual_video_id = request["youtube_video_id"]

        manifest_profiles, unit_specs = build_youtube_sync_shape(
            sync_kind=kind,
            profiles=profiles,
            upper_bound=upper_bound,
            backfill_floor=backfill_floor,
            quota_contract_version=quota_contract_version,
            manual_request_id=manual_request_id,
            manual_video_id=manual_video_id,
        )
        generic = self._stored_youtube_job_manifest(job_id)
        self._require_expected_youtube_units(generic, unit_specs)
        profile_set_hash = youtube_profile_set_hash(manifest_profiles)
        self._conn.execute(
            "INSERT INTO youtube_sync_manifests("
            "job_id, sync_kind, upper_bound, backfill_floor, "
            "quota_contract_version, profile_set_hash, manual_request_id, "
            "resume_not_before_utc, manifest_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                job_id,
                kind.value,
                utc_iso(upper_bound),
                utc_iso(backfill_floor),
                quota_contract_version,
                profile_set_hash,
                manual_request_id,
                generic.manifest_hash,
                utc_iso(created_at),
            ),
        )
        self._conn.executemany(
            "INSERT INTO youtube_sync_manifest_profiles("
            "job_id, ordinal, profile_id, profile_version_id, config_hash, "
            "discoverer_set_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    job_id,
                    item.ordinal,
                    item.profile_id,
                    item.profile_version_id,
                    item.config_hash,
                    item.discoverer_set_hash,
                )
                for item in manifest_profiles
            ),
        )
        for spec in unit_specs:
            effective_lower_bound = backfill_floor
            if kind is YouTubeSyncKind.FULL_DISCOVERY:
                effective_lower_bound = self._source_cursor_lower_bound(
                    profile_id=spec.profile_id,
                    source_kind=spec.source_kind,
                    source_key=spec.source_key,
                    backfill_floor=backfill_floor,
                    upper_bound=upper_bound,
                )
            checkpoint_hash = canonical_youtube_sync_checkpoint_hash(
                job_id=job_id,
                unit_key=spec.unit_key,
                source_kind=spec.source_kind,
                source_key=spec.source_key,
                effective_lower_bound=effective_lower_bound,
                upper_bound=upper_bound,
                uploads_playlist_id=None,
                next_page_token=None,
                encountered_video_ids=(),
                unavailable_video_ids=(),
                page_count=0,
                batch_ordinal=0,
                completed_at=None,
            )
            self._conn.execute(
                "INSERT INTO youtube_sync_checkpoints("
                "job_id, unit_key, source_kind, source_key, "
                "effective_lower_bound, upper_bound, uploads_playlist_id, "
                "next_page_token, encountered_video_ids_json, "
                "unavailable_video_ids_json, page_count, batch_ordinal, "
                "completed_at, checkpoint_hash) VALUES "
                "(?, ?, ?, ?, ?, ?, NULL, NULL, '[]', '[]', 0, 0, NULL, ?)",
                (
                    job_id,
                    spec.unit_key,
                    spec.source_kind.value,
                    spec.source_key,
                    utc_iso(effective_lower_bound),
                    utc_iso(upper_bound),
                    checkpoint_hash,
                ),
            )
            if spec.source_kind is DiscoverySourceKind.CROSS_CHANNEL_SEARCH:
                completed_at = (
                    created_at if effective_lower_bound == upper_bound else None
                )
                window_hash = canonical_search_window_hash(
                    job_id=job_id,
                    unit_key=spec.unit_key,
                    ordinal=1,
                    lower_bound=effective_lower_bound,
                    upper_bound=upper_bound,
                    next_page_token=None,
                    page_count=0,
                    split_parent_id=None,
                    completed_at=completed_at,
                )
                self._conn.execute(
                    "INSERT INTO youtube_search_windows("
                    "job_id, unit_key, ordinal, lower_bound, upper_bound, "
                    "next_page_token, page_count, split_parent_id, "
                    "completed_at, window_hash) VALUES "
                    "(?, ?, 1, ?, ?, NULL, 0, NULL, ?, ?)",
                    (
                        job_id,
                        spec.unit_key,
                        utc_iso(effective_lower_bound),
                        utc_iso(upper_bound),
                        None if completed_at is None else utc_iso(completed_at),
                        window_hash,
                    ),
                )
        return self.get_youtube_sync_manifest(job_id)

    def _source_cursor_lower_bound(
        self,
        *,
        profile_id: int,
        source_kind: DiscoverySourceKind,
        source_key: str,
        backfill_floor: datetime,
        upper_bound: datetime,
    ) -> datetime:
        row = self._conn.execute(
            "SELECT * FROM youtube_source_cursors WHERE profile_id=? "
            "AND source_kind=? AND source_key=?",
            (profile_id, source_kind.value, source_key),
        ).fetchone()
        if row is None:
            return backfill_floor
        try:
            completed_upper_bound = _parse_canonical_utc(
                row["completed_upper_bound"]
            )
            updated_at = _parse_canonical_utc(row["updated_at"])
            expected_hash = canonical_source_cursor_hash(
                profile_id=profile_id,
                source_kind=source_kind,
                source_key=source_key,
                completed_upper_bound=completed_upper_bound,
            )
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SOURCE_CURSOR_INVALID",
                "stored YouTube source cursor is invalid",
            ) from cause
        if (
            row["profile_id"] != profile_id
            or row["source_kind"] != source_kind.value
            or row["source_key"] != source_key
            or row["cursor_hash"] != expected_hash
            or completed_upper_bound > upper_bound
            or updated_at < completed_upper_bound
        ):
            raise DomainError(
                "STORED_YOUTUBE_SOURCE_CURSOR_INVALID",
                "stored YouTube source cursor is invalid",
            )
        return completed_upper_bound

    def get_youtube_sync_manifest(self, job_id: int) -> YouTubeSyncManifest:
        generic = self._stored_youtube_job_manifest(job_id)
        row = self._conn.execute(
            "SELECT manifest.*, job.created_at AS job_created_at, "
            "job.status AS job_status "
            "FROM youtube_sync_manifests AS manifest "
            "JOIN jobs AS job ON job.id=manifest.job_id WHERE manifest.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            )
        try:
            kind = YouTubeSyncKind(row["sync_kind"])
            upper_bound = _parse_canonical_utc(row["upper_bound"])
            backfill_floor = _parse_canonical_utc(row["backfill_floor"])
            created_at = _parse_canonical_utc(row["created_at"])
            job_created_at = _parse_canonical_utc(row["job_created_at"])
            job_status = JobStatus(row["job_status"])
            resume_not_before = (
                None
                if row["resume_not_before_utc"] is None
                else _parse_canonical_utc(row["resume_not_before_utc"])
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            ) from cause
        if (
            row["job_id"] != job_id
            or row["manifest_hash"] != generic.manifest_hash
            or created_at != job_created_at
            or created_at != upper_bound
            or backfill_floor > upper_bound
            or type(row["quota_contract_version"]) is not str
            or not row["quota_contract_version"]
            or type(row["profile_set_hash"]) is not str
            or _CANONICAL_HASH.fullmatch(row["profile_set_hash"]) is None
            or (resume_not_before is not None and resume_not_before < created_at)
            or (
                resume_not_before is not None
                and job_status is not JobStatus.RETRYING
            )
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            )
        profile_rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_sync_manifest_profiles "
                "WHERE job_id=? ORDER BY ordinal",
                (job_id,),
            )
        )
        if not profile_rows or tuple(
            profile["ordinal"] for profile in profile_rows
        ) != tuple(range(1, len(profile_rows) + 1)):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            )
        versions: list[DiscoveryProfileVersion] = []
        stored_profiles: list[YouTubeSyncManifestProfile] = []
        for profile_row in profile_rows:
            try:
                version = self.get_profile_version(profile_row["profile_version_id"])
            except (LookupError, DomainError) as cause:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                    "stored YouTube sync profile binding is invalid",
                ) from cause
            stored_profile = YouTubeSyncManifestProfile(
                ordinal=profile_row["ordinal"],
                profile_id=profile_row["profile_id"],
                profile_version_id=profile_row["profile_version_id"],
                config_hash=profile_row["config_hash"],
                discoverer_set_hash=profile_row["discoverer_set_hash"],
            )
            if (
                version.profile_id != stored_profile.profile_id
                or version.id != stored_profile.profile_version_id
                or version.config_hash != stored_profile.config_hash
                or _CANONICAL_HASH.fullmatch(
                    stored_profile.discoverer_set_hash
                ) is None
            ):
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                    "stored YouTube sync profile binding is invalid",
                )
            versions.append(version)
            stored_profiles.append(stored_profile)

        manual_video_id: str | None = None
        if kind is YouTubeSyncKind.MANUAL:
            request = self._manual_request(row["manual_request_id"])
            if request["profile_id"] != stored_profiles[0].profile_id:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                    "stored manual sync request binding is invalid",
                )
            manual_video_id = request["youtube_video_id"]
        elif row["manual_request_id"] is not None:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored full sync request binding is invalid",
            )
        try:
            expected_profiles, unit_specs = build_youtube_sync_shape(
                sync_kind=kind,
                profiles=tuple(versions),
                upper_bound=upper_bound,
                backfill_floor=backfill_floor,
                quota_contract_version=row["quota_contract_version"],
                manual_request_id=row["manual_request_id"],
                manual_video_id=manual_video_id,
            )
        except DomainError as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            ) from cause
        if (
            tuple(stored_profiles) != expected_profiles
            or row["profile_set_hash"]
            != youtube_profile_set_hash(expected_profiles)
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync profile set is invalid",
            )
        self._require_expected_youtube_units(generic, unit_specs)
        self._validated_youtube_checkpoints(
            job_id=job_id,
            unit_specs=unit_specs,
            manifest_upper_bound=upper_bound,
            manual_request_id=(
                row["manual_request_id"]
                if kind is YouTubeSyncKind.MANUAL
                else None
            ),
        )
        return YouTubeSyncManifest(
            job_id=job_id,
            sync_kind=kind.value,
            upper_bound=upper_bound,
            backfill_floor=backfill_floor,
            quota_contract_version=row["quota_contract_version"],
            profile_set_hash=row["profile_set_hash"],
            manual_request_id=row["manual_request_id"],
            resume_not_before_utc=resume_not_before,
            manifest_hash=row["manifest_hash"],
            created_at=created_at,
            profiles=tuple(stored_profiles),
        )

    def verified_youtube_artifact_hashes(
        self, job_id: int
    ) -> dict[str, str]:
        try:
            manifest = self.get_youtube_sync_manifest(job_id)
            profiles = tuple(
                self.get_profile_version(item.profile_version_id)
                for item in manifest.profiles
            )
            manual_video_id = None
            sync_kind = YouTubeSyncKind(manifest.sync_kind)
            if sync_kind is YouTubeSyncKind.MANUAL:
                if manifest.manual_request_id is None:
                    self._raise_manual_artifact_invalid()
                _, manual_video_id = self.manual_request_binding(
                    manifest.manual_request_id
                )
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=sync_kind,
                profiles=profiles,
                upper_bound=manifest.upper_bound,
                backfill_floor=manifest.backfill_floor,
                quota_contract_version=manifest.quota_contract_version,
                manual_request_id=manifest.manual_request_id,
                manual_video_id=manual_video_id,
            )
        except DomainError as cause:
            if not (
                cause.code.startswith("STORED_DISCOVERY_")
                or cause.code == "STORED_PRESENCE_DECISION_INVALID"
            ):
                raise
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        except (LookupError, TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        specs = {spec.unit_key: spec for spec in unit_specs}
        rows = tuple(self._conn.execute(
            "SELECT unit_key, output_hash FROM job_units "
            "WHERE job_id=? AND status=? ORDER BY ordinal",
            (job_id, UnitStatus.SUCCESS.value),
        ))
        verified: dict[str, str] = {}
        try:
            successful_specs = tuple(
                specs[row["unit_key"]]
                for row in rows
                if row["unit_key"] in specs
            )
            if len(successful_specs) != len(rows):
                self._raise_seed_artifact_invalid()
            self._verify_successful_unit_proposals(
                job_id=job_id,
                manifest=manifest,
                sync_kind=sync_kind,
                successful_specs=successful_specs,
            )
            for row in rows:
                spec = specs.get(row["unit_key"])
                if spec is None:
                    self._raise_seed_artifact_invalid()
                if spec.source_kind is DiscoverySourceKind.SEED_UPLOADS:
                    output_hash, _ = self.seed_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                elif (
                    spec.source_kind
                    is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
                ):
                    output_hash, _ = self.search_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                elif spec.source_kind is DiscoverySourceKind.MANUAL_URL:
                    if manifest.manual_request_id is None:
                        self._raise_manual_artifact_invalid()
                    output_hash, _ = self.manual_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        manual_request_id=manifest.manual_request_id,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                else:
                    self._raise_seed_artifact_invalid()
                if row["output_hash"] != output_hash:
                    self._raise_seed_artifact_invalid()
                verified[spec.unit_key] = output_hash
        except DomainError as cause:
            if cause.code == "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID":
                raise
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        return verified

    def _verify_successful_unit_proposals(
        self, *, job_id: int, manifest, sync_kind, successful_specs
    ) -> None:
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=? "
                "ORDER BY profile_id, source_kind, source_key",
                (job_id,),
            )
        )
        if sync_kind is YouTubeSyncKind.MANUAL:
            if rows:
                self._raise_manual_artifact_invalid()
            return
        expected = {
            (spec.profile_id, spec.source_kind.value, spec.source_key): spec
            for spec in successful_specs
            if spec.source_kind
            in {
                DiscoverySourceKind.SEED_UPLOADS,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            }
        }
        if len(expected) != len(successful_specs) or len(rows) != len(expected):
            self._raise_seed_artifact_invalid()
        seen: set[tuple[int, str, str]] = set()
        for row in rows:
            identity = (
                row["profile_id"],
                row["source_kind"],
                row["source_key"],
            )
            spec = expected.get(identity)
            try:
                source_kind = DiscoverySourceKind(row["source_kind"])
                completed_upper_bound = _parse_canonical_utc(
                    row["completed_upper_bound"]
                )
                cursor_hash = canonical_source_cursor_hash(
                    profile_id=row["profile_id"],
                    source_kind=source_kind,
                    source_key=row["source_key"],
                    completed_upper_bound=completed_upper_bound,
                )
            except (DomainError, TypeError, ValueError):
                self._raise_seed_artifact_invalid()
            if (
                spec is None
                or identity in seen
                or source_kind is DiscoverySourceKind.MANUAL_URL
                or completed_upper_bound != manifest.upper_bound
                or row["cursor_hash"] != cursor_hash
            ):
                self._raise_seed_artifact_invalid()
            seen.add(identity)
        if seen != set(expected):
            self._raise_seed_artifact_invalid()

    def has_daily_youtube_sync_request(self, jst_day: date) -> bool:
        day_text = self._validate_jst_day(jst_day)
        row = self._conn.execute(
            "SELECT job_id, requested_at FROM youtube_daily_sync_requests "
            "WHERE jst_day=?",
            (day_text,),
        ).fetchone()
        if row is None:
            return False
        try:
            requested_at = _parse_canonical_utc(row["requested_at"])
            manifest = self.get_youtube_sync_manifest(row["job_id"])
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_DAILY_REQUEST_INVALID",
                "stored YouTube daily request is invalid",
            ) from cause
        if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
            raise DomainError(
                "STORED_YOUTUBE_DAILY_REQUEST_INVALID",
                "stored YouTube daily request is invalid",
            )
        return True

    def record_daily_youtube_sync_request(
        self, *, jst_day: date, job_id: int, requested_at: datetime
    ) -> None:
        self._require_transaction()
        day_text = self._validate_jst_day(jst_day)
        if (
            type(job_id) is not int
            or job_id <= 0
            or not _is_exact_utc(requested_at)
        ):
            raise DomainError(
                "YOUTUBE_DAILY_REQUEST_INVALID",
                "YouTube daily request is invalid",
            )
        manifest = self.get_youtube_sync_manifest(job_id)
        if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
            raise DomainError(
                "YOUTUBE_DAILY_REQUEST_INVALID",
                "YouTube daily request is invalid",
            )
        try:
            self._conn.execute(
                "INSERT INTO youtube_daily_sync_requests("
                "jst_day, job_id, requested_at) VALUES (?, ?, ?)",
                (day_text, job_id, utc_iso(requested_at)),
            )
        except sqlite3.IntegrityError:
            raise DomainError(
                "YOUTUBE_DAILY_REQUEST_CONFLICT",
                "YouTube daily request already exists",
            ) from None
        if not self.has_daily_youtube_sync_request(jst_day):
            raise DomainError(
                "STORED_YOUTUBE_DAILY_REQUEST_INVALID",
                "stored YouTube daily request is invalid",
            )

    @staticmethod
    def _validate_jst_day(jst_day: object) -> str:
        if type(jst_day) is not date:
            raise DomainError(
                "YOUTUBE_DAILY_REQUEST_INVALID",
                "YouTube daily request is invalid",
            )
        return jst_day.isoformat()

    def get_youtube_sync_checkpoint(
        self, job_id: int, unit_key: str
    ) -> YouTubeSyncCheckpoint:
        self.get_youtube_sync_manifest(job_id)
        row = self._conn.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
        if row is None:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                "stored YouTube sync checkpoint is invalid",
            )
        return self._checkpoint_value(row)

    def next_search_window(
        self, job_id: int, unit_key: str
    ) -> SearchWindow | None:
        self.get_youtube_sync_manifest(job_id)
        row = self._conn.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
        if row is None:
            self._raise_search_checkpoint_invalid()
        checkpoint = self._checkpoint_value(row)
        if checkpoint.source_kind is not DiscoverySourceKind.CROSS_CHANNEL_SEARCH:
            self._raise_search_checkpoint_invalid()
        windows = self._validated_search_windows(checkpoint)
        parent_ids = {
            window.split_parent_id
            for window in windows
            if window.split_parent_id is not None
        }
        return next(
            (
                window
                for window in windows
                if window.completed_at is None and window.id not in parent_ids
            ),
            None,
        )

    def advance_search_window_page(
        self,
        *,
        job_id: int,
        unit_key: str,
        window_id: int,
        next_page_token: str | None,
        encountered_video_ids: tuple[str, ...],
        unavailable_video_ids: tuple[str, ...],
    ) -> tuple[YouTubeSyncCheckpoint, SearchWindow]:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        window = self.next_search_window(job_id, unit_key)
        if (
            checkpoint.source_kind
            is not DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            or window is None
            or window.id != window_id
            or window.page_count >= 10
            or (
                next_page_token is not None
                and (
                    type(next_page_token) is not str
                    or _YOUTUBE_PAGE_TOKEN.fullmatch(next_page_token) is None
                )
            )
        ):
            self._raise_search_checkpoint_invalid()
        try:
            self._validate_seed_progress_values(
                encountered_video_ids, unavailable_video_ids
            )
        except (TypeError, ValueError):
            self._raise_search_checkpoint_invalid()
        if (
            not set(checkpoint.encountered_video_ids).issubset(
                encountered_video_ids
            )
            or not set(checkpoint.unavailable_video_ids).issubset(
                unavailable_video_ids
            )
        ):
            self._raise_search_checkpoint_invalid()
        checkpoint = self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=None,
            next_page_token=None,
            encountered_video_ids=encountered_video_ids,
            unavailable_video_ids=unavailable_video_ids,
            page_count=checkpoint.page_count + 1,
            batch_ordinal=checkpoint.batch_ordinal + 1,
            completed_at=None,
        )
        window = self._replace_search_window(
            window,
            next_page_token=next_page_token,
            page_count=window.page_count + 1,
            completed_at=None,
        )
        return checkpoint, window

    def restart_search_window(
        self, *, job_id: int, unit_key: str, window_id: int
    ) -> SearchWindow:
        self._require_transaction()
        window = self.next_search_window(job_id, unit_key)
        if (
            window is None
            or window.id != window_id
            or window.next_page_token is None
            or window.page_count <= 0
        ):
            self._raise_search_checkpoint_invalid()
        return self._replace_search_window(
            window,
            next_page_token=None,
            page_count=0,
            completed_at=None,
        )

    def complete_search_window(
        self,
        *,
        job_id: int,
        unit_key: str,
        window_id: int,
        completed_at: datetime,
    ) -> SearchWindow:
        self._require_transaction()
        window = self.next_search_window(job_id, unit_key)
        if (
            window is None
            or window.id != window_id
            or window.next_page_token is not None
            or window.page_count <= 0
            or not _is_exact_utc(completed_at)
        ):
            self._raise_search_checkpoint_invalid()
        return self._replace_search_window(
            window,
            next_page_token=None,
            page_count=window.page_count,
            completed_at=completed_at,
        )

    def split_search_window(
        self,
        *,
        job_id: int,
        unit_key: str,
        window_id: int,
        boundary: datetime,
        completed_at: datetime,
    ) -> tuple[SearchWindow, SearchWindow]:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        window = self.next_search_window(job_id, unit_key)
        one_day_seconds = 86_400
        if (
            window is None
            or window.id != window_id
            or window.page_count != 10
            or window.next_page_token is None
            or not _is_exact_utc(boundary)
            or not _is_exact_utc(completed_at)
            or boundary.time() != datetime.min.time()
            or (boundary - window.lower_bound).total_seconds()
            < one_day_seconds
            or (window.upper_bound - boundary).total_seconds()
            < one_day_seconds
        ):
            self._raise_search_checkpoint_invalid()
        self._replace_search_window(
            window,
            next_page_token=None,
            page_count=10,
            completed_at=completed_at,
        )
        next_ordinal = self._conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM youtube_search_windows "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()[0]
        values: list[SearchWindow] = []
        for ordinal, lower_bound, upper_bound in (
            (next_ordinal, boundary, window.upper_bound),
            (next_ordinal + 1, window.lower_bound, boundary),
        ):
            window_hash = canonical_search_window_hash(
                job_id=job_id,
                unit_key=unit_key,
                ordinal=ordinal,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                next_page_token=None,
                page_count=0,
                split_parent_id=window.id,
                completed_at=None,
            )
            cursor = self._conn.execute(
                "INSERT INTO youtube_search_windows("
                "job_id, unit_key, ordinal, lower_bound, upper_bound, "
                "next_page_token, page_count, split_parent_id, completed_at, "
                "window_hash) VALUES (?, ?, ?, ?, ?, NULL, 0, ?, NULL, ?)",
                (
                    job_id,
                    unit_key,
                    ordinal,
                    utc_iso(lower_bound),
                    utc_iso(upper_bound),
                    window.id,
                    window_hash,
                ),
            )
            values.append(self._search_window_value(
                self._conn.execute(
                    "SELECT * FROM youtube_search_windows WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
            ))
        self._validated_search_windows(checkpoint)
        return values[0], values[1]

    def complete_search_checkpoint(
        self,
        *,
        job_id: int,
        unit_key: str,
        completed_at: datetime,
    ) -> YouTubeSyncCheckpoint:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        windows = self._validated_search_windows(checkpoint)
        parent_ids = {
            window.split_parent_id
            for window in windows
            if window.split_parent_id is not None
        }
        leaves = tuple(window for window in windows if window.id not in parent_ids)
        if (
            checkpoint.source_kind
            is not DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            or checkpoint.completed_at is not None
            or checkpoint.uploads_playlist_id is not None
            or checkpoint.next_page_token is not None
            or any(window.completed_at is None for window in leaves)
            or not _is_exact_utc(completed_at)
        ):
            self._raise_search_checkpoint_invalid()
        return self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=None,
            next_page_token=None,
            encountered_video_ids=checkpoint.encountered_video_ids,
            unavailable_video_ids=checkpoint.unavailable_video_ids,
            page_count=checkpoint.page_count,
            batch_ordinal=checkpoint.batch_ordinal,
            completed_at=completed_at,
        )

    def bind_seed_uploads_playlist(
        self,
        *,
        job_id: int,
        unit_key: str,
        source_key: str,
        uploads_playlist_id: str,
    ) -> YouTubeSyncCheckpoint:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        if (
            checkpoint.source_kind is not DiscoverySourceKind.SEED_UPLOADS
            or source_key != checkpoint.source_key
            or type(uploads_playlist_id) is not str
            or _YOUTUBE_UPLOADS_PLAYLIST_ID.fullmatch(uploads_playlist_id)
            is None
            or checkpoint.completed_at is not None
        ):
            self._raise_seed_checkpoint_invalid()
        if checkpoint.uploads_playlist_id is not None:
            if checkpoint.uploads_playlist_id != uploads_playlist_id:
                self._raise_seed_checkpoint_invalid()
            return checkpoint
        return self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=uploads_playlist_id,
            next_page_token=checkpoint.next_page_token,
            encountered_video_ids=checkpoint.encountered_video_ids,
            unavailable_video_ids=checkpoint.unavailable_video_ids,
            page_count=checkpoint.page_count,
            batch_ordinal=checkpoint.batch_ordinal,
            completed_at=None,
        )

    def advance_seed_checkpoint(
        self,
        *,
        job_id: int,
        unit_key: str,
        next_page_token: str | None,
        encountered_video_ids: tuple[str, ...],
        unavailable_video_ids: tuple[str, ...],
    ) -> YouTubeSyncCheckpoint:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        if (
            checkpoint.source_kind is not DiscoverySourceKind.SEED_UPLOADS
            or checkpoint.uploads_playlist_id is None
            or checkpoint.completed_at is not None
            or (
                next_page_token is not None
                and (
                    type(next_page_token) is not str
                    or _YOUTUBE_PAGE_TOKEN.fullmatch(next_page_token) is None
                )
            )
        ):
            self._raise_seed_checkpoint_invalid()
        try:
            self._validate_seed_progress_values(
                encountered_video_ids, unavailable_video_ids
            )
        except (TypeError, ValueError):
            self._raise_seed_checkpoint_invalid()
        if (
            not set(checkpoint.encountered_video_ids).issubset(
                encountered_video_ids
            )
            or not set(checkpoint.unavailable_video_ids).issubset(
                unavailable_video_ids
            )
        ):
            self._raise_seed_checkpoint_invalid()
        return self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=checkpoint.uploads_playlist_id,
            next_page_token=next_page_token,
            encountered_video_ids=encountered_video_ids,
            unavailable_video_ids=unavailable_video_ids,
            page_count=checkpoint.page_count + 1,
            batch_ordinal=checkpoint.batch_ordinal + 1,
            completed_at=None,
        )

    def complete_seed_checkpoint(
        self,
        *,
        job_id: int,
        unit_key: str,
        completed_at: datetime,
    ) -> YouTubeSyncCheckpoint:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        if (
            checkpoint.source_kind is not DiscoverySourceKind.SEED_UPLOADS
            or checkpoint.uploads_playlist_id is None
            or checkpoint.next_page_token is not None
            or checkpoint.page_count <= 0
            or checkpoint.completed_at is not None
            or not _is_exact_utc(completed_at)
        ):
            self._raise_seed_checkpoint_invalid()
        return self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=checkpoint.uploads_playlist_id,
            next_page_token=None,
            encountered_video_ids=checkpoint.encountered_video_ids,
            unavailable_video_ids=checkpoint.unavailable_video_ids,
            page_count=checkpoint.page_count,
            batch_ordinal=checkpoint.batch_ordinal,
            completed_at=completed_at,
        )

    def complete_manual_checkpoint_and_artifact(
        self,
        *,
        job_id: int,
        unit_key: str,
        manual_request_id: int,
        profile_version_id: int,
        youtube_video_id: str,
        unavailable: bool,
        completed_at: datetime,
    ) -> tuple[YouTubeSyncCheckpoint, str, tuple[int, ...]]:
        self._require_transaction()
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        try:
            profile_version = self.get_profile_version(profile_version_id)
            request = self._manual_request(manual_request_id)
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        if (
            checkpoint.source_kind is not DiscoverySourceKind.MANUAL_URL
            or checkpoint.source_key != f"manual-request:{manual_request_id}"
            or checkpoint.effective_lower_bound != checkpoint.upper_bound
            or checkpoint.uploads_playlist_id is not None
            or checkpoint.next_page_token is not None
            or checkpoint.encountered_video_ids
            or checkpoint.unavailable_video_ids
            or checkpoint.page_count != 0
            or checkpoint.batch_ordinal != 0
            or checkpoint.completed_at is not None
            or request["profile_id"] != profile_version.profile_id
            or request["youtube_video_id"] != youtube_video_id
            or type(unavailable) is not bool
            or not _is_exact_utc(completed_at)
        ):
            self._raise_manual_artifact_invalid()
        checkpoint = self._replace_youtube_checkpoint(
            checkpoint,
            uploads_playlist_id=None,
            next_page_token=None,
            encountered_video_ids=(youtube_video_id,),
            unavailable_video_ids=(youtube_video_id,) if unavailable else (),
            page_count=1,
            batch_ordinal=1,
            completed_at=completed_at,
        )
        output_hash, observation_ids = self._canonical_manual_artifact(
            profile_version=profile_version,
            checkpoint=checkpoint,
            manual_request_id=manual_request_id,
            youtube_video_id=youtube_video_id,
        )
        return checkpoint, output_hash, observation_ids

    def manual_unit_artifact(
        self,
        *,
        job_id: int,
        unit_key: str,
        manual_request_id: int,
        profile_version_id: int,
        profile_id: int,
        source_key: str,
    ) -> tuple[str, tuple[int, ...]]:
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        try:
            profile_version = self.get_profile_version(profile_version_id)
            request = self._manual_request(manual_request_id)
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        if (
            checkpoint.source_kind is not DiscoverySourceKind.MANUAL_URL
            or checkpoint.source_key != source_key
            or source_key != f"manual-request:{manual_request_id}"
            or profile_version.profile_id != profile_id
            or request["profile_id"] != profile_id
        ):
            self._raise_manual_artifact_invalid()
        return self._canonical_manual_artifact(
            profile_version=profile_version,
            checkpoint=checkpoint,
            manual_request_id=manual_request_id,
            youtube_video_id=request["youtube_video_id"],
        )

    def seed_unit_artifact(
        self,
        *,
        job_id: int,
        unit_key: str,
        profile_version_id: int,
        profile_id: int,
        source_key: str,
    ) -> tuple[str, tuple[int, ...]]:
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        try:
            profile_version = self.get_profile_version(profile_version_id)
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        if (
            checkpoint.source_kind is not DiscoverySourceKind.SEED_UPLOADS
            or checkpoint.source_key != source_key
            or profile_version.profile_id != profile_id
        ):
            self._raise_seed_artifact_invalid()
        return self._canonical_seed_artifact(profile_version, checkpoint)

    def seed_unit_seen_video_ids(
        self,
        *,
        job_id: int,
        unit_key: str,
        profile_version_id: int,
        profile_id: int,
        source_key: str,
    ) -> tuple[str, ...]:
        self.seed_unit_artifact(
            job_id=job_id,
            unit_key=unit_key,
            profile_version_id=profile_version_id,
            profile_id=profile_id,
            source_key=source_key,
        )
        return tuple(
            row["youtube_video_id"]
            for row in self._conn.execute(
                "SELECT video.youtube_video_id "
                "FROM discovery_observations AS observation "
                "JOIN videos AS video ON video.id=observation.video_id "
                "WHERE observation.job_id=? AND observation.profile_id=? "
                "AND observation.source_kind='seed_uploads' "
                "AND observation.source_key=? ORDER BY observation.id",
                (job_id, profile_id, source_key),
            )
        )

    def search_unit_artifact(
        self,
        *,
        job_id: int,
        unit_key: str,
        profile_version_id: int,
        profile_id: int,
        source_key: str,
    ) -> tuple[str, tuple[int, ...]]:
        checkpoint = self.get_youtube_sync_checkpoint(job_id, unit_key)
        try:
            profile_version = self.get_profile_version(profile_version_id)
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        if (
            checkpoint.source_kind
            is not DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            or checkpoint.source_key != source_key
            or profile_version.profile_id != profile_id
        ):
            self._raise_seed_artifact_invalid()
        return self._canonical_search_artifact(profile_version, checkpoint)

    def search_unit_seen_video_ids(
        self,
        *,
        job_id: int,
        unit_key: str,
        profile_version_id: int,
        profile_id: int,
        source_key: str,
    ) -> tuple[str, ...]:
        self.search_unit_artifact(
            job_id=job_id,
            unit_key=unit_key,
            profile_version_id=profile_version_id,
            profile_id=profile_id,
            source_key=source_key,
        )
        return tuple(
            row["youtube_video_id"]
            for row in self._conn.execute(
                "SELECT video.youtube_video_id "
                "FROM discovery_observations AS observation "
                "JOIN videos AS video ON video.id=observation.video_id "
                "WHERE observation.job_id=? AND observation.profile_id=? "
                "AND observation.source_kind='cross_channel_search' "
                "AND observation.source_key=? ORDER BY observation.id",
                (job_id, profile_id, source_key),
            )
        )

    def record_seed_proposed_cursor(
        self,
        *,
        job_id: int,
        profile_id: int,
        source_key: str,
        completed_upper_bound: datetime,
    ) -> str:
        return self.record_source_proposed_cursor(
            job_id=job_id,
            profile_id=profile_id,
            source_kind=DiscoverySourceKind.SEED_UPLOADS,
            source_key=source_key,
            completed_upper_bound=completed_upper_bound,
        )

    def record_source_proposed_cursor(
        self,
        *,
        job_id: int,
        profile_id: int,
        source_kind: DiscoverySourceKind,
        source_key: str,
        completed_upper_bound: datetime,
    ) -> str:
        self._require_transaction()
        manifest = self.get_youtube_sync_manifest(job_id)
        profiles, unit_specs = self._bound_full_unit_specs(manifest)
        matches = tuple(
            spec
            for spec in unit_specs
            if spec.profile_id == profile_id
            and spec.source_kind is source_kind
            and spec.source_key == source_key
        )
        if (
            type(source_kind) is not DiscoverySourceKind
            or source_kind is DiscoverySourceKind.MANUAL_URL
            or type(profile_id) is not int
            or profile_id <= 0
            or type(source_key) is not str
            or _SAFE_SOURCE_KEY.fullmatch(source_key) is None
            or not _is_exact_utc(completed_upper_bound)
            or completed_upper_bound != manifest.upper_bound
            or len(matches) != 1
            or not any(profile.profile_id == profile_id for profile in profiles)
        ):
            self._raise_seed_artifact_invalid()
        cursor_hash = canonical_source_cursor_hash(
            profile_id=profile_id,
            source_kind=source_kind,
            source_key=source_key,
            completed_upper_bound=completed_upper_bound,
        )
        existing = self._conn.execute(
            "SELECT * FROM youtube_sync_proposed_cursors "
            "WHERE job_id=? AND profile_id=? AND source_kind=? AND source_key=?",
            (
                job_id,
                profile_id,
                source_kind.value,
                source_key,
            ),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO youtube_sync_proposed_cursors("
                "job_id, profile_id, source_kind, source_key, "
                "completed_upper_bound, cursor_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    profile_id,
                    source_kind.value,
                    source_key,
                    utc_iso(completed_upper_bound),
                    cursor_hash,
                ),
            )
        elif (
            existing["completed_upper_bound"] != utc_iso(completed_upper_bound)
            or existing["cursor_hash"] != cursor_hash
        ):
            self._raise_seed_artifact_invalid()
        return cursor_hash

    def promote_full_job_cursors(
        self, *, job_id: int, updated_at: datetime
    ) -> int:
        self._require_transaction()
        manifest = self.get_youtube_sync_manifest(job_id)
        profiles, unit_specs = self._bound_full_unit_specs(manifest)
        if not _is_exact_utc(updated_at):
            self._raise_cursor_promotion_invalid()
        profile_by_version = {profile.id: profile for profile in profiles}
        unit_rows = {
            row["unit_key"]: row
            for row in self._conn.execute(
                "SELECT unit_key, status, output_hash FROM job_units "
                "WHERE job_id=?",
                (job_id,),
            )
        }
        for spec in unit_specs:
            unit = unit_rows.get(spec.unit_key)
            checkpoint_row = self._conn.execute(
                "SELECT * FROM youtube_sync_checkpoints WHERE job_id=? "
                "AND unit_key=?",
                (job_id, spec.unit_key),
            ).fetchone()
            profile = profile_by_version.get(spec.profile_version_id)
            if (
                unit is None
                or unit["status"] != UnitStatus.SUCCESS.value
                or checkpoint_row is None
                or profile is None
            ):
                self._raise_cursor_promotion_invalid()
            checkpoint = self._checkpoint_value(checkpoint_row)
            if spec.source_kind is DiscoverySourceKind.SEED_UPLOADS:
                artifact_hash, _ = self._canonical_seed_artifact(
                    profile, checkpoint
                )
            elif (
                spec.source_kind
                is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            ):
                artifact_hash, _ = self._canonical_search_artifact(
                    profile, checkpoint
                )
            else:
                self._raise_cursor_promotion_invalid()
            if unit["output_hash"] != artifact_hash:
                self._raise_seed_artifact_invalid()
        expected = {
            (spec.profile_id, spec.source_kind.value, spec.source_key): spec
            for spec in unit_specs
        }
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=? "
                "ORDER BY profile_id, source_kind, source_key",
                (job_id,),
            )
        )
        if len(rows) != len(expected):
            self._raise_cursor_promotion_invalid()
        seen: set[tuple[int, str, str]] = set()
        for row in rows:
            identity = (
                row["profile_id"],
                row["source_kind"],
                row["source_key"],
            )
            spec = expected.get(identity)
            try:
                source_kind = DiscoverySourceKind(row["source_kind"])
                completed_upper_bound = _parse_canonical_utc(
                    row["completed_upper_bound"]
                )
                expected_hash = canonical_source_cursor_hash(
                    profile_id=row["profile_id"],
                    source_kind=source_kind,
                    source_key=row["source_key"],
                    completed_upper_bound=completed_upper_bound,
                )
            except (DomainError, TypeError, ValueError) as cause:
                raise DomainError(
                    "YOUTUBE_CURSOR_PROMOTION_INVALID",
                    "YouTube cursor promotion evidence is invalid",
                ) from cause
            if (
                spec is None
                or identity in seen
                or source_kind is DiscoverySourceKind.MANUAL_URL
                or completed_upper_bound != manifest.upper_bound
                or row["cursor_hash"] != expected_hash
            ):
                self._raise_cursor_promotion_invalid()
            seen.add(identity)
        if seen != set(expected):
            self._raise_cursor_promotion_invalid()

        for row in rows:
            existing = self._conn.execute(
                "SELECT * FROM youtube_source_cursors WHERE profile_id=? "
                "AND source_kind=? AND source_key=?",
                (
                    row["profile_id"],
                    row["source_kind"],
                    row["source_key"],
                ),
            ).fetchone()
            if existing is not None:
                try:
                    old_bound = _parse_canonical_utc(
                        existing["completed_upper_bound"]
                    )
                    old_updated_at = _parse_canonical_utc(
                        existing["updated_at"]
                    )
                    old_kind = DiscoverySourceKind(existing["source_kind"])
                    old_hash = canonical_source_cursor_hash(
                        profile_id=existing["profile_id"],
                        source_kind=old_kind,
                        source_key=existing["source_key"],
                        completed_upper_bound=old_bound,
                    )
                except (DomainError, TypeError, ValueError) as cause:
                    raise DomainError(
                        "STORED_YOUTUBE_SOURCE_CURSOR_INVALID",
                        "stored YouTube source cursor is invalid",
                    ) from cause
                if (
                    old_hash != existing["cursor_hash"]
                    or old_updated_at < old_bound
                    or old_bound > manifest.upper_bound
                ):
                    raise DomainError(
                        "STORED_YOUTUBE_SOURCE_CURSOR_INVALID",
                        "stored YouTube source cursor is invalid",
                    )
            self._conn.execute(
                "INSERT INTO youtube_source_cursors("
                "profile_id, source_kind, source_key, completed_upper_bound, "
                "cursor_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, source_kind, source_key) DO UPDATE SET "
                "completed_upper_bound=excluded.completed_upper_bound, "
                "cursor_hash=excluded.cursor_hash, updated_at=excluded.updated_at",
                (
                    row["profile_id"],
                    row["source_kind"],
                    row["source_key"],
                    row["completed_upper_bound"],
                    row["cursor_hash"],
                    utc_iso(updated_at),
                ),
            )
        return len(rows)

    def set_youtube_resume_not_before(
        self, job_id: int, value: datetime | None
    ) -> None:
        self._require_transaction()
        manifest = self.get_youtube_sync_manifest(job_id)
        status_row = self._conn.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        try:
            status = JobStatus(status_row["status"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync job status is invalid",
            ) from cause
        if value is not None and (
            not _is_exact_utc(value)
            or value < manifest.created_at
            or status is not JobStatus.RETRYING
        ):
            raise DomainError(
                "YOUTUBE_SYNC_DEFER_INVALID",
                "YouTube sync defer time is invalid",
            )
        cursor = self._conn.execute(
            "UPDATE youtube_sync_manifests SET resume_not_before_utc=? "
            "WHERE job_id=?",
            (None if value is None else utc_iso(value), job_id),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "YOUTUBE_SYNC_MANIFEST_NOT_FOUND",
                "YouTube sync manifest does not exist",
            )
        self.get_youtube_sync_manifest(job_id)

    def find_manual_discovery_request_id(
        self, *, profile_id: int, youtube_video_id: str
    ) -> int | None:
        if (
            type(profile_id) is not int
            or profile_id <= 0
            or type(youtube_video_id) is not str
            or _YOUTUBE_VIDEO_ID.fullmatch(youtube_video_id) is None
        ):
            raise DomainError(
                "YOUTUBE_SYNC_REQUEST_INVALID",
                "manual YouTube request identity is invalid",
            )
        row = self._conn.execute(
            "SELECT id FROM manual_discovery_requests "
            "WHERE profile_id=? AND youtube_video_id=?",
            (profile_id, youtube_video_id),
        ).fetchone()
        if row is None:
            return None
        request = self._manual_request(row["id"])
        if (
            request["profile_id"] != profile_id
            or request["youtube_video_id"] != youtube_video_id
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored manual sync request binding is invalid",
            )
        return request["id"]

    def create_manual_discovery_request(
        self,
        *,
        profile_id: int,
        youtube_video_id: str,
        requested_at: datetime,
    ) -> int:
        self._require_transaction()
        if (
            type(profile_id) is not int
            or profile_id <= 0
            or type(youtube_video_id) is not str
            or _YOUTUBE_VIDEO_ID.fullmatch(youtube_video_id) is None
            or not _is_exact_utc(requested_at)
        ):
            raise DomainError(
                "YOUTUBE_SYNC_REQUEST_INVALID",
                "manual YouTube request is invalid",
            )
        profile = self._conn.execute(
            "SELECT profile.id, profile.current_version_id, "
            "profile.is_active, subject.is_active AS subject_is_active "
            "FROM discovery_profiles AS profile "
            "JOIN analysis_subjects AS subject ON subject.id=profile.subject_id "
            "WHERE profile.id=?",
            (profile_id,),
        ).fetchone()
        if (
            profile is None
            or profile["is_active"] != 1
            or profile["subject_is_active"] != 1
            or profile["current_version_id"] is None
        ):
            raise DomainError(
                "DISCOVERY_PROFILE_NOT_ACTIVE",
                "manual discovery requires an active subject and profile",
            )
        cursor = self._conn.execute(
            "INSERT INTO manual_discovery_requests("
            "profile_id, youtube_video_id, requested_at) VALUES (?, ?, ?)",
            (profile_id, youtube_video_id, utc_iso(requested_at)),
        )
        request_id = cursor.lastrowid
        request = self._manual_request(request_id)
        if (
            request["profile_id"] != profile_id
            or request["youtube_video_id"] != youtube_video_id
            or request["requested_at"] != utc_iso(requested_at)
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored manual sync request is invalid",
            )
        return request_id

    def find_manual_sync_job_id(self, manual_request_id: int) -> int | None:
        rows = tuple(
            self._conn.execute(
                "SELECT job_id FROM youtube_sync_manifests "
                "WHERE sync_kind='manual' AND manual_request_id=? ORDER BY job_id",
                (manual_request_id,),
            )
        )
        if len(rows) > 1:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "manual request is linked to multiple sync jobs",
            )
        return None if not rows else rows[0]["job_id"]

    def manual_request_binding(
        self, manual_request_id: int
    ) -> tuple[int, str]:
        request = self._manual_request(manual_request_id)
        return request["profile_id"], request["youtube_video_id"]

    def manual_sync_request_binding(
        self, manual_request_id: int
    ) -> tuple[DiscoveryProfileVersion, str]:
        request = self._manual_request(manual_request_id)
        version = self.get_current_profile_version(request["subject_id"])
        if version.profile_id != request["profile_id"]:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored manual sync request binding is invalid",
            )
        return version, request["youtube_video_id"]

    def _manual_request(self, manual_request_id: object) -> sqlite3.Row:
        if type(manual_request_id) is not int or manual_request_id <= 0:
            raise DomainError(
                "YOUTUBE_SYNC_MANIFEST_INVALID",
                "manual sync request identity is invalid",
            )
        row = self._conn.execute(
            "SELECT request.*, profile.subject_id "
            "FROM manual_discovery_requests AS request "
            "JOIN discovery_profiles AS profile ON profile.id=request.profile_id "
            "WHERE request.id=?",
            (manual_request_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "YOUTUBE_SYNC_MANIFEST_INVALID",
                "manual sync request does not exist",
            )
        try:
            _parse_canonical_utc(row["requested_at"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored manual sync request is invalid",
            ) from cause
        if (
            type(row["profile_id"]) is not int
            or row["profile_id"] <= 0
            or type(row["subject_id"]) is not int
            or row["subject_id"] <= 0
            or type(row["youtube_video_id"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{11}", row["youtube_video_id"])
            is None
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored manual sync request is invalid",
            )
        return row

    def _stored_youtube_job_manifest(self, job_id: int) -> JobManifest:
        row = self._conn.execute(
            "SELECT job_kind, manifest_hash, total_units FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None or row["job_kind"] != JobKind.YOUTUBE_SYNC.value:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync job is invalid",
            )
        unit_rows = tuple(
            self._conn.execute(
                "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal",
                (job_id,),
            )
        )
        try:
            manifest = JobManifest.build(
                JobKind.YOUTUBE_SYNC,
                tuple(
                    ManifestUnit(
                        unit["unit_key"],
                        JobStage(unit["stage"]),
                        unit["ordinal"],
                        unit["declared_input_hash"],
                        tuple(json.loads(unit["dependency_keys_json"])),
                        unit["execution_contract_hash"],
                    )
                    for unit in unit_rows
                ),
            )
        except (DomainError, TypeError, ValueError, json.JSONDecodeError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync job manifest is invalid",
            ) from cause
        if (
            row["manifest_hash"] != manifest.manifest_hash
            or row["total_units"] != len(unit_rows)
        ):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync job manifest is invalid",
            )
        return manifest

    @staticmethod
    def _require_expected_youtube_units(generic, unit_specs) -> None:
        expected = tuple(
            ManifestUnit(
                spec.unit_key,
                spec.stage,
                spec.ordinal,
                spec.declared_input_hash,
                (),
                spec.execution_contract_hash,
            )
            for spec in unit_specs
        )
        if generic.units != expected:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync units do not match the sealed manifest",
            )

    def _bound_full_unit_specs(self, manifest):
        if manifest.sync_kind != YouTubeSyncKind.FULL_DISCOVERY.value:
            self._raise_cursor_promotion_invalid()
        try:
            profiles = tuple(
                self.get_profile_version(item.profile_version_id)
                for item in manifest.profiles
            )
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=YouTubeSyncKind.FULL_DISCOVERY,
                profiles=profiles,
                upper_bound=manifest.upper_bound,
                backfill_floor=manifest.backfill_floor,
                quota_contract_version=manifest.quota_contract_version,
                manual_request_id=None,
                manual_video_id=None,
            )
        except (LookupError, DomainError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_MANIFEST_INVALID",
                "stored YouTube sync manifest is invalid",
            ) from cause
        return profiles, unit_specs

    def _validated_youtube_checkpoints(
        self,
        *,
        job_id: int,
        unit_specs,
        manifest_upper_bound: datetime,
        manual_request_id: int | None,
    ) -> tuple[YouTubeSyncCheckpoint, ...]:
        manual = manual_request_id is not None
        manual_request = (
            self._manual_request(manual_request_id) if manual else None
        )
        rows = tuple(
            self._conn.execute(
                "SELECT checkpoint.* FROM youtube_sync_checkpoints AS checkpoint "
                "JOIN job_units AS unit ON unit.job_id=checkpoint.job_id "
                "AND unit.unit_key=checkpoint.unit_key "
                "WHERE checkpoint.job_id=? ORDER BY unit.ordinal",
                (job_id,),
            )
        )
        if len(rows) != len(unit_specs):
            raise DomainError(
                "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                "stored YouTube sync checkpoint set is invalid",
            )
        stored_units = {
            row["unit_key"]: row
            for row in self._conn.execute(
                "SELECT unit_key, status, output_hash FROM job_units WHERE job_id=?",
                (job_id,),
            )
        }
        values: list[YouTubeSyncCheckpoint] = []
        for row, spec in zip(rows, unit_specs, strict=True):
            try:
                source_kind = DiscoverySourceKind(row["source_kind"])
                effective_lower = _parse_canonical_utc(
                    row["effective_lower_bound"]
                )
                upper_bound = _parse_canonical_utc(row["upper_bound"])
                completed_at = (
                    None
                    if row["completed_at"] is None
                    else _parse_canonical_utc(row["completed_at"])
                )
                encountered_video_ids, unavailable_video_ids = (
                    self._stored_seed_progress(row, source_kind)
                )
            except (TypeError, ValueError) as cause:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                    "stored YouTube sync checkpoint is invalid",
                ) from cause
            unit = stored_units.get(spec.unit_key)
            if (
                row["job_id"] != job_id
                or row["unit_key"] != spec.unit_key
                or source_kind is not spec.source_kind
                or row["source_key"] != spec.source_key
                or effective_lower > upper_bound
                or upper_bound != manifest_upper_bound
                or (manual and effective_lower != upper_bound)
                or (not manual and effective_lower > upper_bound)
                or manual
                and (
                    row["uploads_playlist_id"] is not None
                    or row["next_page_token"] is not None
                )
                or type(row["page_count"]) is not int
                or row["page_count"] < 0
                or type(row["batch_ordinal"]) is not int
                or row["batch_ordinal"] < 0
                or row["uploads_playlist_id"] is not None
                and type(row["uploads_playlist_id"]) is not str
                or row["next_page_token"] is not None
                and type(row["next_page_token"]) is not str
                or type(row["checkpoint_hash"]) is not str
                or _CANONICAL_HASH.fullmatch(row["checkpoint_hash"]) is None
                or unit is None
            ):
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                    "stored YouTube sync checkpoint is invalid",
                )
            expected_hash = canonical_youtube_sync_checkpoint_hash(
                job_id=job_id,
                unit_key=spec.unit_key,
                source_kind=source_kind,
                source_key=row["source_key"],
                effective_lower_bound=effective_lower,
                upper_bound=upper_bound,
                uploads_playlist_id=row["uploads_playlist_id"],
                next_page_token=row["next_page_token"],
                encountered_video_ids=encountered_video_ids,
                unavailable_video_ids=unavailable_video_ids,
                page_count=row["page_count"],
                batch_ordinal=row["batch_ordinal"],
                completed_at=completed_at,
            )
            if row["checkpoint_hash"] != expected_hash:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                    "stored YouTube sync checkpoint hash is invalid",
                )
            checkpoint = YouTubeSyncCheckpoint(
                job_id=job_id,
                unit_key=spec.unit_key,
                source_kind=source_kind,
                source_key=row["source_key"],
                effective_lower_bound=effective_lower,
                upper_bound=upper_bound,
                uploads_playlist_id=row["uploads_playlist_id"],
                next_page_token=row["next_page_token"],
                encountered_video_ids=encountered_video_ids,
                unavailable_video_ids=unavailable_video_ids,
                page_count=row["page_count"],
                batch_ordinal=row["batch_ordinal"],
                completed_at=completed_at,
                checkpoint_hash=row["checkpoint_hash"],
            )
            if source_kind is DiscoverySourceKind.CROSS_CHANNEL_SEARCH:
                self._validated_search_windows(checkpoint)
            if unit["status"] == UnitStatus.SUCCESS.value:
                if (
                    completed_at is None
                    or type(unit["output_hash"]) is not str
                    or _CANONICAL_HASH.fullmatch(unit["output_hash"]) is None
                ):
                    raise DomainError(
                        "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                        "stored YouTube sync artifact is invalid",
                    )
                profile_version = self.get_profile_version(
                    spec.profile_version_id
                )
                if source_kind is DiscoverySourceKind.MANUAL_URL:
                    if manual_request is None:
                        self._raise_manual_artifact_invalid()
                    artifact_hash, _ = self._canonical_manual_artifact(
                        profile_version=profile_version,
                        checkpoint=checkpoint,
                        manual_request_id=manual_request_id,
                        youtube_video_id=manual_request["youtube_video_id"],
                    )
                    if unit["output_hash"] != artifact_hash:
                        self._raise_manual_artifact_invalid()
                elif unit["output_hash"] != expected_hash:
                    if source_kind is DiscoverySourceKind.SEED_UPLOADS:
                        artifact_hash, _ = self._canonical_seed_artifact(
                            profile_version, checkpoint
                        )
                    elif (
                        source_kind
                        is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
                    ):
                        artifact_hash, _ = self._canonical_search_artifact(
                            profile_version, checkpoint
                        )
                    else:
                        raise DomainError(
                            "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                            "stored YouTube sync artifact is invalid",
                        )
                    if unit["output_hash"] != artifact_hash:
                        self._raise_seed_artifact_invalid()
            else:
                if completed_at is not None or manual and (
                    encountered_video_ids
                    or unavailable_video_ids
                    or row["page_count"] != 0
                    or row["batch_ordinal"] != 0
                ):
                    raise DomainError(
                        "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                        "stored YouTube sync checkpoint is invalid",
                    )
            values.append(checkpoint)
        return tuple(values)

    def _checkpoint_value(self, row: sqlite3.Row) -> YouTubeSyncCheckpoint:
        try:
            source_kind = DiscoverySourceKind(row["source_kind"])
            effective_lower = _parse_canonical_utc(
                row["effective_lower_bound"]
            )
            upper_bound = _parse_canonical_utc(row["upper_bound"])
            completed_at = (
                None
                if row["completed_at"] is None
                else _parse_canonical_utc(row["completed_at"])
            )
            encountered_video_ids, unavailable_video_ids = (
                self._stored_seed_progress(row, source_kind)
            )
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID",
                "stored YouTube sync checkpoint is invalid",
            ) from cause
        return YouTubeSyncCheckpoint(
            job_id=row["job_id"],
            unit_key=row["unit_key"],
            source_kind=source_kind,
            source_key=row["source_key"],
            effective_lower_bound=effective_lower,
            upper_bound=upper_bound,
            uploads_playlist_id=row["uploads_playlist_id"],
            next_page_token=row["next_page_token"],
            encountered_video_ids=encountered_video_ids,
            unavailable_video_ids=unavailable_video_ids,
            page_count=row["page_count"],
            batch_ordinal=row["batch_ordinal"],
            completed_at=completed_at,
            checkpoint_hash=row["checkpoint_hash"],
        )

    def _replace_youtube_checkpoint(
        self,
        checkpoint: YouTubeSyncCheckpoint,
        *,
        uploads_playlist_id: str | None,
        next_page_token: str | None,
        encountered_video_ids: tuple[str, ...],
        unavailable_video_ids: tuple[str, ...],
        page_count: int,
        batch_ordinal: int,
        completed_at: datetime | None,
    ) -> YouTubeSyncCheckpoint:
        checkpoint_hash = canonical_youtube_sync_checkpoint_hash(
            job_id=checkpoint.job_id,
            unit_key=checkpoint.unit_key,
            source_kind=checkpoint.source_kind,
            source_key=checkpoint.source_key,
            effective_lower_bound=checkpoint.effective_lower_bound,
            upper_bound=checkpoint.upper_bound,
            uploads_playlist_id=uploads_playlist_id,
            next_page_token=next_page_token,
            encountered_video_ids=encountered_video_ids,
            unavailable_video_ids=unavailable_video_ids,
            page_count=page_count,
            batch_ordinal=batch_ordinal,
            completed_at=completed_at,
        )
        cursor = self._conn.execute(
            "UPDATE youtube_sync_checkpoints SET uploads_playlist_id=?, "
            "next_page_token=?, encountered_video_ids_json=?, "
            "unavailable_video_ids_json=?, page_count=?, batch_ordinal=?, "
            "completed_at=?, checkpoint_hash=? "
            "WHERE job_id=? AND unit_key=? AND checkpoint_hash=?",
            (
                uploads_playlist_id,
                next_page_token,
                canonical_json(list(encountered_video_ids)),
                canonical_json(list(unavailable_video_ids)),
                page_count,
                batch_ordinal,
                None if completed_at is None else utc_iso(completed_at),
                checkpoint_hash,
                checkpoint.job_id,
                checkpoint.unit_key,
                checkpoint.checkpoint_hash,
            ),
        )
        if cursor.rowcount != 1:
            self._raise_seed_checkpoint_invalid()
        return self._checkpoint_value(
            self._conn.execute(
                "SELECT * FROM youtube_sync_checkpoints "
                "WHERE job_id=? AND unit_key=?",
                (checkpoint.job_id, checkpoint.unit_key),
            ).fetchone()
        )

    def _validated_search_windows(
        self, checkpoint: YouTubeSyncCheckpoint
    ) -> tuple[SearchWindow, ...]:
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM youtube_search_windows WHERE job_id=? "
                "AND unit_key=? ORDER BY ordinal",
                (checkpoint.job_id, checkpoint.unit_key),
            )
        )
        if (
            checkpoint.source_kind
            is not DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            or not rows
        ):
            raise DomainError(
                "STORED_YOUTUBE_SEARCH_WINDOW_INVALID",
                "stored YouTube search window is invalid",
            )
        windows = tuple(self._search_window_value(row) for row in rows)
        if tuple(window.ordinal for window in windows) != tuple(
            range(1, len(windows) + 1)
        ):
            self._raise_stored_search_window_invalid()
        root = windows[0]
        if (
            root.split_parent_id is not None
            or root.lower_bound != checkpoint.effective_lower_bound
            or root.upper_bound != checkpoint.upper_bound
            or any(
                window.job_id != checkpoint.job_id
                or window.unit_key != checkpoint.unit_key
                for window in windows
            )
        ):
            self._raise_stored_search_window_invalid()
        by_id = {window.id: window for window in windows}
        children: dict[int, list[SearchWindow]] = {}
        for window in windows[1:]:
            parent = by_id.get(window.split_parent_id)
            if parent is None or parent.ordinal >= window.ordinal:
                self._raise_stored_search_window_invalid()
            children.setdefault(parent.id, []).append(window)
        for window in windows:
            child_pair = children.get(window.id, [])
            if child_pair:
                if (
                    len(child_pair) != 2
                    or window.completed_at is None
                    or window.next_page_token is not None
                    or window.page_count != 10
                ):
                    self._raise_stored_search_window_invalid()
                newer, older = sorted(child_pair, key=lambda item: item.ordinal)
                if (
                    newer.ordinal + 1 != older.ordinal
                    or newer.lower_bound != older.upper_bound
                    or newer.upper_bound != window.upper_bound
                    or older.lower_bound != window.lower_bound
                    or newer.lower_bound.time() != datetime.min.time()
                    or (
                        newer.upper_bound - newer.lower_bound
                    ).total_seconds() < 86_400
                    or (
                        older.upper_bound - older.lower_bound
                    ).total_seconds() < 86_400
                ):
                    self._raise_stored_search_window_invalid()
            elif (
                window.completed_at is not None
                and window.next_page_token is not None
            ):
                self._raise_stored_search_window_invalid()
            elif (
                window.completed_at is not None
                and window.page_count == 0
                and checkpoint.effective_lower_bound
                != checkpoint.upper_bound
                or window.completed_at is None
                and window.page_count == 0
                and window.next_page_token is not None
            ):
                self._raise_stored_search_window_invalid()
        if checkpoint.effective_lower_bound == checkpoint.upper_bound:
            if (
                len(windows) != 1
                or root.completed_at is None
                or root.page_count != 0
                or root.next_page_token is not None
            ):
                self._raise_stored_search_window_invalid()
        elif root.lower_bound >= root.upper_bound:
            self._raise_stored_search_window_invalid()
        return windows

    def _search_window_value(self, row: sqlite3.Row) -> SearchWindow:
        try:
            lower_bound = _parse_canonical_utc(row["lower_bound"])
            upper_bound = _parse_canonical_utc(row["upper_bound"])
            completed_at = (
                None
                if row["completed_at"] is None
                else _parse_canonical_utc(row["completed_at"])
            )
            expected_hash = canonical_search_window_hash(
                job_id=row["job_id"],
                unit_key=row["unit_key"],
                ordinal=row["ordinal"],
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                next_page_token=row["next_page_token"],
                page_count=row["page_count"],
                split_parent_id=row["split_parent_id"],
                completed_at=completed_at,
            )
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "STORED_YOUTUBE_SEARCH_WINDOW_INVALID",
                "stored YouTube search window is invalid",
            ) from cause
        if (
            type(row["id"]) is not int
            or row["id"] <= 0
            or type(row["job_id"]) is not int
            or row["job_id"] <= 0
            or type(row["unit_key"]) is not str
            or not row["unit_key"]
            or type(row["ordinal"]) is not int
            or row["ordinal"] <= 0
            or lower_bound > upper_bound
            or row["next_page_token"] is not None
            and (
                type(row["next_page_token"]) is not str
                or _YOUTUBE_PAGE_TOKEN.fullmatch(row["next_page_token"])
                is None
            )
            or type(row["page_count"]) is not int
            or row["page_count"] < 0
            or row["page_count"] > 10
            or row["split_parent_id"] is not None
            and (
                type(row["split_parent_id"]) is not int
                or row["split_parent_id"] <= 0
            )
            or row["window_hash"] != expected_hash
        ):
            self._raise_stored_search_window_invalid()
        return SearchWindow(
            id=row["id"],
            job_id=row["job_id"],
            unit_key=row["unit_key"],
            ordinal=row["ordinal"],
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            next_page_token=row["next_page_token"],
            page_count=row["page_count"],
            split_parent_id=row["split_parent_id"],
            completed_at=completed_at,
            window_hash=row["window_hash"],
        )

    def _replace_search_window(
        self,
        window: SearchWindow,
        *,
        next_page_token: str | None,
        page_count: int,
        completed_at: datetime | None,
    ) -> SearchWindow:
        window_hash = canonical_search_window_hash(
            job_id=window.job_id,
            unit_key=window.unit_key,
            ordinal=window.ordinal,
            lower_bound=window.lower_bound,
            upper_bound=window.upper_bound,
            next_page_token=next_page_token,
            page_count=page_count,
            split_parent_id=window.split_parent_id,
            completed_at=completed_at,
        )
        cursor = self._conn.execute(
            "UPDATE youtube_search_windows SET next_page_token=?, "
            "page_count=?, completed_at=?, window_hash=? WHERE id=? "
            "AND window_hash=?",
            (
                next_page_token,
                page_count,
                None if completed_at is None else utc_iso(completed_at),
                window_hash,
                window.id,
                window.window_hash,
            ),
        )
        if cursor.rowcount != 1:
            self._raise_search_checkpoint_invalid()
        return self._search_window_value(
            self._conn.execute(
                "SELECT * FROM youtube_search_windows WHERE id=?",
                (window.id,),
            ).fetchone()
        )

    def _canonical_seed_artifact(
        self,
        profile_version: DiscoveryProfileVersion,
        checkpoint: YouTubeSyncCheckpoint,
    ) -> tuple[str, tuple[int, ...]]:
        rows = tuple(
            self._conn.execute(
                "SELECT observation.*, video.youtube_video_id "
                "FROM discovery_observations AS observation "
                "JOIN videos AS video ON video.id=observation.video_id "
                "WHERE observation.job_id=? AND observation.profile_id=? "
                "AND observation.source_kind='seed_uploads' "
                "AND observation.source_key=? ORDER BY observation.id",
                (
                    checkpoint.job_id,
                    profile_version.profile_id,
                    checkpoint.source_key,
                ),
            )
        )
        for row in rows:
            self._validate_stored_observation(row)
            if (
                row["youtube_video_id"]
                not in checkpoint.encountered_video_ids
                or row["youtube_video_id"]
                in checkpoint.unavailable_video_ids
            ):
                self._raise_seed_artifact_invalid()
            snapshot = self._conn.execute(
                "SELECT published_at FROM video_metadata_snapshots WHERE id=?",
                (row["metadata_snapshot_id"],),
            ).fetchone()
            try:
                published_at = _parse_canonical_utc(snapshot["published_at"])
            except (TypeError, ValueError) as cause:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                    "stored YouTube sync artifact is invalid",
                ) from cause
            if not (
                checkpoint.effective_lower_bound
                <= published_at
                < checkpoint.upper_bound
            ):
                self._raise_seed_artifact_invalid()
            candidate = self._conn.execute(
                "SELECT * FROM subject_video_candidates "
                "WHERE profile_id=? AND video_id=?",
                (profile_version.profile_id, row["video_id"]),
            ).fetchone()
            if candidate is None:
                self._raise_seed_artifact_invalid()
            self._validate_candidate_row(
                candidate,
                profile_id=profile_version.profile_id,
                video_id=row["video_id"],
            )
        observation_ids = tuple(row["id"] for row in rows)
        return (
            sha256_text(canonical_json({
                "completed_upper_bound": utc_iso(checkpoint.upper_bound),
                "persisted_observation_ids": list(observation_ids),
                "profile_version_id": profile_version.id,
                "schema": "youtube-seed-unit-output.v1",
                "source_key": checkpoint.source_key,
            })),
            observation_ids,
        )

    def _canonical_search_artifact(
        self,
        profile_version: DiscoveryProfileVersion,
        checkpoint: YouTubeSyncCheckpoint,
    ) -> tuple[str, tuple[int, ...]]:
        rows = tuple(
            self._conn.execute(
                "SELECT observation.*, video.youtube_video_id "
                "FROM discovery_observations AS observation "
                "JOIN videos AS video ON video.id=observation.video_id "
                "WHERE observation.job_id=? AND observation.profile_id=? "
                "AND observation.source_kind='cross_channel_search' "
                "AND observation.source_key=? ORDER BY observation.id",
                (
                    checkpoint.job_id,
                    profile_version.profile_id,
                    checkpoint.source_key,
                ),
            )
        )
        for row in rows:
            self._validate_stored_observation(row)
            if (
                row["youtube_video_id"]
                not in checkpoint.encountered_video_ids
                or row["youtube_video_id"]
                in checkpoint.unavailable_video_ids
            ):
                self._raise_seed_artifact_invalid()
            snapshot = self._conn.execute(
                "SELECT published_at FROM video_metadata_snapshots WHERE id=?",
                (row["metadata_snapshot_id"],),
            ).fetchone()
            try:
                published_at = _parse_canonical_utc(snapshot["published_at"])
            except (TypeError, ValueError) as cause:
                raise DomainError(
                    "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                    "stored YouTube sync artifact is invalid",
                ) from cause
            if not (
                checkpoint.effective_lower_bound
                <= published_at
                < checkpoint.upper_bound
            ):
                self._raise_seed_artifact_invalid()
            candidate = self._conn.execute(
                "SELECT * FROM subject_video_candidates "
                "WHERE profile_id=? AND video_id=?",
                (profile_version.profile_id, row["video_id"]),
            ).fetchone()
            if candidate is None:
                self._raise_seed_artifact_invalid()
            self._validate_candidate_row(
                candidate,
                profile_id=profile_version.profile_id,
                video_id=row["video_id"],
            )
        observation_ids = tuple(row["id"] for row in rows)
        return (
            sha256_text(canonical_json({
                "completed_upper_bound": utc_iso(checkpoint.upper_bound),
                "persisted_observation_ids": list(observation_ids),
                "profile_version_id": profile_version.id,
                "schema": "youtube-search-unit-output.v1",
                "source_key": checkpoint.source_key,
            })),
            observation_ids,
        )

    def _canonical_manual_artifact(
        self,
        *,
        profile_version: DiscoveryProfileVersion,
        checkpoint: YouTubeSyncCheckpoint,
        manual_request_id: int,
        youtube_video_id: str,
    ) -> tuple[str, tuple[int, ...]]:
        try:
            if (
                checkpoint.source_kind is not DiscoverySourceKind.MANUAL_URL
                or checkpoint.source_key
                != f"manual-request:{manual_request_id}"
                or checkpoint.effective_lower_bound != checkpoint.upper_bound
                or checkpoint.uploads_playlist_id is not None
                or checkpoint.next_page_token is not None
                or checkpoint.encountered_video_ids != (youtube_video_id,)
                or checkpoint.unavailable_video_ids
                not in ((), (youtube_video_id,))
                or checkpoint.page_count != 1
                or checkpoint.batch_ordinal != 1
                or checkpoint.completed_at is None
            ):
                self._raise_manual_artifact_invalid()
            rows = tuple(
                self._conn.execute(
                    "SELECT observation.*, video.youtube_video_id "
                    "FROM discovery_observations AS observation "
                    "JOIN videos AS video ON video.id=observation.video_id "
                    "WHERE observation.job_id=? ORDER BY observation.id",
                    (checkpoint.job_id,),
                )
            )
            unavailable = bool(checkpoint.unavailable_video_ids)
            if len(rows) != (0 if unavailable else 1):
                self._raise_manual_artifact_invalid()
            for row in rows:
                self._validate_stored_observation(row)
                if (
                    row["profile_id"] != profile_version.profile_id
                    or row["source_kind"]
                    != DiscoverySourceKind.MANUAL_URL.value
                    or row["source_key"] != checkpoint.source_key
                    or row["youtube_video_id"] != youtube_video_id
                ):
                    self._raise_manual_artifact_invalid()
                candidate = self._conn.execute(
                    "SELECT * FROM subject_video_candidates "
                    "WHERE profile_id=? AND video_id=?",
                    (profile_version.profile_id, row["video_id"]),
                ).fetchone()
                if candidate is None:
                    self._raise_manual_artifact_invalid()
                self._validate_candidate_row(
                    candidate,
                    profile_id=profile_version.profile_id,
                    video_id=row["video_id"],
                )
            observation_ids = tuple(row["id"] for row in rows)
            output_hash = canonical_manual_unit_output_hash(
                manual_request_id=manual_request_id,
                profile_version_id=profile_version.id,
                source_key=checkpoint.source_key,
                youtube_video_id_hash=youtube_manual_video_hash(
                    youtube_video_id
                ),
                persisted_observation_ids=observation_ids,
                unavailable=unavailable,
            )
        except DomainError as cause:
            if cause.code == "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID":
                raise
            raise DomainError(
                "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
                "stored YouTube sync artifact is invalid",
            ) from cause
        return output_hash, observation_ids

    @classmethod
    def _stored_seed_progress(
        cls,
        row: sqlite3.Row,
        source_kind: DiscoverySourceKind,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        encountered_raw = json.loads(row["encountered_video_ids_json"])
        unavailable_raw = json.loads(row["unavailable_video_ids_json"])
        if type(encountered_raw) is not list or type(unavailable_raw) is not list:
            raise ValueError("checkpoint progress must use JSON arrays")
        encountered = tuple(encountered_raw)
        unavailable = tuple(unavailable_raw)
        cls._validate_seed_progress_values(encountered, unavailable)
        if (
            row["encountered_video_ids_json"]
            != canonical_json(list(encountered))
            or row["unavailable_video_ids_json"]
            != canonical_json(list(unavailable))
            or source_kind not in {
                DiscoverySourceKind.SEED_UPLOADS,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                DiscoverySourceKind.MANUAL_URL,
            }
        ):
            raise ValueError("checkpoint progress is not canonical")
        return encountered, unavailable

    @staticmethod
    def _validate_seed_progress_values(
        encountered_video_ids: tuple[str, ...],
        unavailable_video_ids: tuple[str, ...],
    ) -> None:
        if (
            type(encountered_video_ids) is not tuple
            or type(unavailable_video_ids) is not tuple
            or any(
                type(video_id) is not str
                or _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None
                for video_id in encountered_video_ids + unavailable_video_ids
            )
            or encountered_video_ids
            != tuple(sorted(set(encountered_video_ids)))
            or unavailable_video_ids
            != tuple(sorted(set(unavailable_video_ids)))
            or not set(unavailable_video_ids).issubset(encountered_video_ids)
        ):
            raise ValueError("checkpoint progress is invalid")

    @staticmethod
    def _raise_seed_checkpoint_invalid() -> None:
        raise DomainError(
            "YOUTUBE_SEED_CHECKPOINT_INVALID",
            "YouTube seed checkpoint is invalid",
        )

    @staticmethod
    def _raise_seed_artifact_invalid() -> None:
        raise DomainError(
            "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
            "stored YouTube sync artifact is invalid",
        )

    @staticmethod
    def _raise_manual_artifact_invalid() -> None:
        raise DomainError(
            "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID",
            "stored YouTube sync artifact is invalid",
        )

    @staticmethod
    def _raise_search_checkpoint_invalid() -> None:
        raise DomainError(
            "YOUTUBE_SEARCH_CHECKPOINT_INVALID",
            "YouTube search checkpoint is invalid",
        )

    @staticmethod
    def _raise_stored_search_window_invalid() -> None:
        raise DomainError(
            "STORED_YOUTUBE_SEARCH_WINDOW_INVALID",
            "stored YouTube search window is invalid",
        )

    @staticmethod
    def _raise_cursor_promotion_invalid() -> None:
        raise DomainError(
            "YOUTUBE_CURSOR_PROMOTION_INVALID",
            "YouTube cursor promotion evidence is invalid",
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

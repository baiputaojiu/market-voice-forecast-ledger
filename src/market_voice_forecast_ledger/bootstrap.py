import sqlite3
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.discovery import canonical_profile_hash
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository


DEFAULT_DISCOVERY_PROFILES = (
    ("木野内栄治", ("UCJ1DVBLVpe4FvBZZ94kreaQ",), ("木野内栄治",)),
    ("大川智宏", (), ("大川智宏",)),
    ("江守哲", ("UCVXka7buS_WptsAzSE0LcKg",), ("江守哲",)),
    (
        "千竈 鉄平",
        ("UCOfzLmXpI3qmZfV7_Cs1sYA",),
        ("千竈鉄平", "千竃鉄平"),
    ),
)


def bootstrap_reference_data(conn: sqlite3.Connection) -> None:
    created_at = datetime.now(timezone.utc)
    with transaction(conn):
        approved_names = {row[0] for row in DEFAULT_DISCOVERY_PROFILES}
        stored_profile_names = {
            row[0]
            for row in conn.execute(
                "SELECT subject.canonical_name FROM discovery_profiles AS profile "
                "JOIN analysis_subjects AS subject ON subject.id=profile.subject_id"
            )
        }
        if not stored_profile_names.issubset(approved_names):
            _raise_bootstrap_mismatch()
        sources = SourceRepository(conn)
        discovery = DiscoveryRepository(conn)
        for name, seed_channel_ids, search_terms in DEFAULT_DISCOVERY_PROFILES:
            subject_row = conn.execute(
                "SELECT id, is_active FROM analysis_subjects WHERE canonical_name=?",
                (name,),
            ).fetchone()
            if subject_row is None:
                subject_id = sources.create_subject(name, created_at=created_at)
            elif subject_row["is_active"] != 1:
                _raise_bootstrap_mismatch()
            else:
                subject_id = subject_row["id"]

            profile_row = conn.execute(
                "SELECT id, current_version_id, is_active "
                "FROM discovery_profiles WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if profile_row is None:
                discovery.create_profile_version(
                    subject_id,
                    seed_channel_ids=seed_channel_ids,
                    search_terms=search_terms,
                    created_at=created_at,
                )
                continue
            if (
                profile_row["is_active"] != 1
                or profile_row["current_version_id"] is None
            ):
                _raise_bootstrap_mismatch()
            try:
                current = discovery.get_profile_version(
                    profile_row["current_version_id"]
                )
            except (LookupError, DomainError) as error:
                raise DomainError(
                    "BOOTSTRAP_REFERENCE_MISMATCH",
                    "stored bootstrap reference data differs from the approved set",
                ) from error
            if (
                current.seed_channel_ids != seed_channel_ids
                or current.search_terms != search_terms
                or current.config_hash
                != canonical_profile_hash(seed_channel_ids, search_terms)
            ):
                _raise_bootstrap_mismatch()


def _raise_bootstrap_mismatch() -> None:
    raise DomainError(
        "BOOTSTRAP_REFERENCE_MISMATCH",
        "stored bootstrap reference data differs from the approved set",
    )

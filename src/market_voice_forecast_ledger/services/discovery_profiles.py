import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    validate_profile_configuration,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.audit import validate_audit_reason


@dataclass(frozen=True, slots=True)
class ReplaceDiscoveryProfileVersion:
    subject_id: int
    seed_channel_ids: tuple[str, ...]
    search_terms: tuple[str, ...]
    reason: str


class DiscoveryProfileService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._profiles = DiscoveryRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def replace_version(
        self, command: ReplaceDiscoveryProfileVersion
    ) -> DiscoveryProfileVersion:
        self._validate_command(command)
        try:
            with transaction(self._conn):
                validate_audit_reason(self._conn, command.reason)
                try:
                    before = self._profiles.get_current_profile_version(
                        command.subject_id
                    )
                except LookupError as cause:
                    raise DomainError(
                        "DISCOVERY_PROFILE_NOT_FOUND",
                        "an active discovery profile is required",
                    ) from cause
                if (
                    before.seed_channel_ids == command.seed_channel_ids
                    and before.search_terms == command.search_terms
                ):
                    return before
                created_at = self._clock()
                if (
                    type(created_at) is not datetime
                    or created_at.tzinfo is not timezone.utc
                ):
                    raise DomainError(
                        "DISCOVERY_PROFILE_INVALID",
                        "profile version creation time must be exact UTC",
                    )
                after = self._profiles.create_profile_version(
                    command.subject_id,
                    seed_channel_ids=command.seed_channel_ids,
                    search_terms=command.search_terms,
                    created_at=created_at,
                )
                self._audit.append(
                    AuditEventInput(
                        entity_type="discovery_profile",
                        entity_id=str(before.profile_id),
                        scope_id=None,
                        operation="replace_version",
                        actor_kind="user",
                        reason_code="DISCOVERY_PROFILE_VERSION_REPLACED",
                        reason_text=command.reason,
                        before=_audit_view(before),
                        after=_audit_view(after),
                        created_at=created_at,
                    )
                )
            return after
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "DISCOVERY_PROFILE_STORAGE_FAILED",
                "discovery profile version could not be stored",
            ) from cause

    @staticmethod
    def _validate_command(command: object) -> None:
        if type(command) is not ReplaceDiscoveryProfileVersion:
            raise DomainError(
                "DISCOVERY_PROFILE_INVALID",
                "profile replacement requires an exact command",
            )
        if type(command.subject_id) is not int or command.subject_id <= 0:
            raise DomainError(
                "DISCOVERY_PROFILE_INVALID",
                "profile subject identity is invalid",
            )
        validate_profile_configuration(
            command.seed_channel_ids,
            command.search_terms,
        )


def _audit_view(version: DiscoveryProfileVersion) -> dict[str, object]:
    return {
        "config_hash": version.config_hash,
        "profile_id": version.profile_id,
        "profile_version_id": version.id,
        "subject_id": version.subject_id,
    }

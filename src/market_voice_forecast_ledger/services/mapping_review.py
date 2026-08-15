import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    MappingReviewDecision,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.mappings import AssetMapping
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.mappings import MappingRepository


_UTC_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_REVIEWABLE_CONFIDENCES = {Confidence.LOW, Confidence.UNRESOLVED}
_REVIEW_REASON_CODES = {
    MappingReviewDecision.APPROVE: "REVIEW_APPROVED",
    MappingReviewDecision.CORRECT: "REVIEW_CORRECTED",
    MappingReviewDecision.REJECT: "REVIEW_REJECTED",
}


@dataclass(frozen=True, slots=True)
class MappingReviewCommand:
    mapping_id: int
    decision: MappingReviewDecision
    actor: str
    reason: str
    corrected_asset: Asset | None


@dataclass(frozen=True, slots=True)
class EffectiveMappingDecision:
    asset: Asset
    heatmap_eligible: bool
    reason_code: str


class MappingReviewService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._mappings = MappingRepository(conn)
        self._audit = AuditRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def review(self, command: MappingReviewCommand) -> int:
        self._validate_command(command)
        try:
            with transaction(self._conn):
                mapping = self._mapping_for_command(command.mapping_id)
                if mapping.final_confidence not in _REVIEWABLE_CONFIDENCES:
                    self._raise_invalid(
                        "only low or unresolved mappings can be reviewed"
                    )
                before = self._effective_for_mapping(mapping)
                after_asset = self._after_asset(command, mapping, before.asset)
                created_at = self._clock()
                cursor = self._conn.execute(
                    """
                    INSERT INTO mapping_reviews(
                        mapping_id,
                        decision,
                        actor,
                        reason,
                        before_asset,
                        after_asset,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.mapping_id,
                        command.decision.value,
                        command.actor,
                        command.reason,
                        before.asset.value,
                        after_asset.value,
                        utc_iso(created_at),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("mapping review insert did not return an id")
                review_id = cursor.lastrowid
                self._audit.append(
                    AuditEventInput(
                        entity_type="analysis_asset_mapping",
                        entity_id=str(command.mapping_id),
                        scope_id=self._scope_id(mapping.run_id),
                        operation="review",
                        actor_kind=command.actor,
                        reason_code=command.decision.value,
                        reason_text=command.reason,
                        before={
                            "asset": before.asset.value,
                            "mapping_id": command.mapping_id,
                        },
                        after={
                            "actor": command.actor,
                            "asset": after_asset.value,
                            "decision": command.decision.value,
                            "mapping_id": command.mapping_id,
                            "reason": command.reason,
                        },
                        created_at=created_at,
                    )
                )
                return review_id
        except DomainError:
            raise
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise DomainError(
                "MAPPING_REVIEW_STORAGE_FAILED",
                "mapping review could not be stored",
            ) from cause

    def effective(self, mapping_id: int) -> EffectiveMappingDecision:
        if (
            not isinstance(mapping_id, int)
            or isinstance(mapping_id, bool)
            or mapping_id <= 0
        ):
            self._raise_invalid("mapping review requires a valid mapping id")
        try:
            mapping = self._mappings.get(mapping_id)
        except (TypeError, ValueError) as cause:
            raise self._stored_invalid() from cause
        return self._effective_for_mapping(mapping)

    def _effective_for_mapping(
        self, mapping: AssetMapping
    ) -> EffectiveMappingDecision:
        if (
            not isinstance(mapping.id, int)
            or isinstance(mapping.id, bool)
            or mapping.id <= 0
        ):
            raise self._stored_invalid()
        rows = self._conn.execute(
            """
            SELECT
                id,
                mapping_id,
                decision,
                actor,
                reason,
                before_asset,
                after_asset,
                created_at
            FROM mapping_reviews
            WHERE mapping_id=?
            ORDER BY id
            """,
            (mapping.id,),
        ).fetchall()
        if not rows:
            if mapping.final_confidence in {
                Confidence.HIGH,
                Confidence.MEDIUM,
            }:
                return EffectiveMappingDecision(
                    mapping.asset, True, "AUTO_CONFIDENCE"
                )
            if mapping.final_confidence in _REVIEWABLE_CONFIDENCES:
                return EffectiveMappingDecision(
                    mapping.asset, False, "REVIEW_REQUIRED"
                )
            raise self._stored_invalid()

        if mapping.final_confidence not in _REVIEWABLE_CONFIDENCES:
            raise self._stored_invalid()

        current_asset = mapping.asset
        previous_id = 0
        decision: MappingReviewDecision | None = None
        for row in rows:
            review_id, review_decision, before_asset, after_asset = (
                self._validate_stored_row(row, mapping.id, previous_id)
            )
            if before_asset is not current_asset:
                raise self._stored_invalid()
            if review_decision is MappingReviewDecision.APPROVE:
                if after_asset is not mapping.asset:
                    raise self._stored_invalid()
            elif review_decision is MappingReviewDecision.CORRECT:
                if after_asset in {mapping.asset, current_asset}:
                    raise self._stored_invalid()
            elif after_asset is not current_asset:
                raise self._stored_invalid()
            previous_id = review_id
            decision = review_decision
            current_asset = after_asset

        if decision is None:
            raise self._stored_invalid()
        return EffectiveMappingDecision(
            current_asset,
            decision is not MappingReviewDecision.REJECT,
            _REVIEW_REASON_CODES[decision],
        )

    def _validate_stored_row(
        self,
        row: sqlite3.Row,
        mapping_id: int,
        previous_id: int,
    ) -> tuple[int, MappingReviewDecision, Asset, Asset]:
        try:
            review_id = row["id"]
            stored_mapping_id = row["mapping_id"]
            actor = row["actor"]
            reason = row["reason"]
            created_at = row["created_at"]
            if (
                not isinstance(review_id, int)
                or isinstance(review_id, bool)
                or review_id <= previous_id
                or stored_mapping_id != mapping_id
                or actor not in {"user", "system"}
                or not isinstance(reason, str)
                or not reason.strip()
                or not isinstance(created_at, str)
                or _UTC_TEXT.fullmatch(created_at) is None
            ):
                raise ValueError
            parsed_created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )
            if parsed_created_at.utcoffset() != timezone.utc.utcoffset(None):
                raise ValueError
            return (
                review_id,
                MappingReviewDecision(row["decision"]),
                Asset(row["before_asset"]),
                Asset(row["after_asset"]),
            )
        except (KeyError, TypeError, ValueError) as cause:
            raise self._stored_invalid() from cause

    def _mapping_for_command(self, mapping_id: int) -> AssetMapping:
        try:
            return self._mappings.get(mapping_id)
        except DomainError as cause:
            if cause.code == "ASSET_MAPPING_NOT_FOUND":
                raise DomainError(
                    "MAPPING_REVIEW_INVALID",
                    "mapping review requires an existing mapping",
                ) from cause
            raise

    def _after_asset(
        self,
        command: MappingReviewCommand,
        mapping: AssetMapping,
        current_asset: Asset,
    ) -> Asset:
        if command.decision is MappingReviewDecision.APPROVE:
            return mapping.asset
        if command.decision is MappingReviewDecision.REJECT:
            return current_asset
        corrected_asset = command.corrected_asset
        if corrected_asset in {mapping.asset, current_asset}:
            self._raise_invalid(
                "a correction must differ from calculated and current assets"
            )
        if not isinstance(corrected_asset, Asset):
            self._raise_invalid("a correction requires a valid asset")
        return corrected_asset

    def _scope_id(self, run_id: int) -> int:
        row = self._conn.execute(
            "SELECT scope_id FROM analysis_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("mapping run does not have a scope")
        return row["scope_id"]

    @classmethod
    def _validate_command(cls, command: MappingReviewCommand) -> None:
        if not isinstance(command, MappingReviewCommand):
            cls._raise_invalid("mapping review requires a command")
        if (
            not isinstance(command.mapping_id, int)
            or isinstance(command.mapping_id, bool)
            or command.mapping_id <= 0
            or type(command.decision) is not MappingReviewDecision
            or not isinstance(command.actor, str)
            or command.actor not in {"user", "system"}
            or not isinstance(command.reason, str)
            or not command.reason.strip()
        ):
            cls._raise_invalid(
                "mapping review requires a mapping, decision, actor, and reason"
            )
        if command.decision is MappingReviewDecision.CORRECT:
            if type(command.corrected_asset) is not Asset:
                cls._raise_invalid("a correction requires an exact asset")
        elif command.corrected_asset is not None:
            cls._raise_invalid(
                "only a correction can provide a corrected asset"
            )

    @staticmethod
    def _raise_invalid(message: str) -> None:
        raise DomainError("MAPPING_REVIEW_INVALID", message)

    @staticmethod
    def _stored_invalid() -> DomainError:
        return DomainError(
            "MAPPING_REVIEW_STORED_INVALID",
            "stored mapping review history is invalid",
        )

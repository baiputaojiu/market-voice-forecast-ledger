"""Immutable identities for the single supported presence-pilot repair."""

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import VoiceManifestSnapshot


@dataclass(frozen=True, slots=True)
class RepairRowIdentity:
    table: str
    identity: str


@dataclass(frozen=True, slots=True)
class PresenceRepairJob:
    job_id: int
    candidate_id: int
    snapshot: VoiceManifestSnapshot
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class PresenceRepairTarget:
    jobs: tuple[PresenceRepairJob, ...]
    row_identities: tuple[RepairRowIdentity, ...]
    row_counts: tuple[tuple[str, int], ...]
    candidate_order_hash: str
    target_fingerprint: str
    preserved_fingerprint: str

    @property
    def counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self.row_counts))


@dataclass(frozen=True, slots=True)
class PresenceRepairPreview:
    from_vad_contract_version: str
    to_vad_contract_version: str
    target: PresenceRepairTarget
    preview_hash: str


@dataclass(frozen=True, slots=True)
class PresenceRepairResult:
    old_job_ids: tuple[int, ...]
    new_job_ids: tuple[int, ...]
    candidate_ids: tuple[int, ...]
    to_vad_contract_version: str


def build_presence_repair_preview(
    from_contract: str, to_contract: str, target: PresenceRepairTarget
) -> PresenceRepairPreview:
    if (from_contract, to_contract) != ("vad-v1", "vad-v2"):
        raise DomainError("PRESENCE_REPAIR_TARGET_INVALID", "presence repair target is invalid")
    payload = {
        "schema": "presence-vad-repair-preview.v1",
        "from_contract": from_contract,
        "to_contract": to_contract,
        "candidate_ids": [job.candidate_id for job in target.jobs],
        "candidate_order_hash": target.candidate_order_hash,
        "job_ids": [job.job_id for job in target.jobs],
        "rows": [(row.table, row.identity) for row in target.row_identities],
        "counts": dict(target.row_counts),
        "target_fingerprint": target.target_fingerprint,
        "preserved_fingerprint": target.preserved_fingerprint,
    }
    return PresenceRepairPreview(from_contract, to_contract, target, sha256_text(canonical_json(payload)))

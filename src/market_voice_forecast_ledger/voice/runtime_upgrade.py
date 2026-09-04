"""Back up and advance only the VAD identity of three attested private locks."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists,
    RuntimeAttestation,
    VersionProbe,
    _private_child_root,
    _private_file,
    _private_root,
    _read_lock,
    _require_no_reparse,
    attest_runtime,
    verify_runtime_startup,
)


LOCK_NAMES = ("runtime-lock.campplus.json", "runtime-lock.wespeaker.json", "runtime-lock.json")


@dataclass(frozen=True, slots=True)
class RuntimeLockBackup:
    settings: Settings
    backup_directory: Path
    originals: tuple[tuple[str, bytes], ...]
    before_contract: str
    backup_fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeLockUpgradeResult:
    backup_directory: Path
    backup_fingerprint: str
    before_contract: str
    after_contract: str
    attestations: tuple[RuntimeAttestation, ...]


def _invalid() -> DomainError:
    return DomainError("PRESENCE_REPAIR_RUNTIME_INVALID", "presence repair runtime is invalid")


def _fingerprint(originals: tuple[tuple[str, bytes], ...]) -> str:
    return sha256_text(canonical_json([(name, hashlib.sha256(body).hexdigest()) for name, body in originals]))


def _read_attested(settings: Settings, probe: VersionProbe, allowlists: RuntimeAllowlists):
    data_root = _private_root(settings.data_dir)
    root = _private_child_root(settings.voice_runtime_dir, data_root)
    originals, documents, attestations = [], [], []
    for name in LOCK_NAMES:
        path = _private_file(root / name, root)
        body = path.read_bytes()
        document = _read_lock(path)
        attestation = attest_runtime(settings, version_probe=probe, allowlists=allowlists, lock_name=name)
        verify_runtime_startup(attestation, data_root)
        if path.read_bytes() != body or json.loads(body) != document:
            raise _invalid()
        originals.append((name, body))
        documents.append(document)
        attestations.append(attestation)
    contracts = {document["vad_contract_version"] for document in documents}
    shared = [canonical_json({key: value for key, value in document.items() if key != "model"}) for document in documents]
    if (
        len(contracts) != 1 or not contracts <= {"vad-v1", "vad-v2"}
        or len(set(shared)) != 1
        or sum(document["model"] == documents[-1]["model"] for document in documents[:2]) != 1
    ):
        raise _invalid()
    return tuple(originals), tuple(attestations), next(iter(contracts))


def _exclusive_write(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != body:
        raise _invalid()


def backup_runtime_locks(
    settings: Settings, *, backup_directory: Path, version_probe: VersionProbe,
    allowlists: RuntimeAllowlists = RuntimeAllowlists(),
) -> RuntimeLockBackup:
    """Validate everything before creating a new, never-overwritten backup."""
    try:
        originals, _attestations, contract = _read_attested(settings, version_probe, allowlists)
        root = _private_root(settings.data_dir)
        destination = backup_directory.absolute()
        _require_no_reparse(destination)
        relative = destination.relative_to(root)
        if not relative.parts or ".." in relative.parts or destination.exists():
            raise _invalid()
        destination.mkdir(parents=True, exist_ok=False)
        destination = _private_child_root(destination, root)
        for name, body in originals:
            if (settings.voice_runtime_dir / name).read_bytes() != body:
                raise _invalid()
            _exclusive_write(destination / name, body)
        return RuntimeLockBackup(settings, destination, originals, contract, _fingerprint(originals))
    except Exception:
        raise _invalid() from None


def upgrade_runtime_locks(
    backup: RuntimeLockBackup, *, version_probe: VersionProbe,
    allowlists: RuntimeAllowlists = RuntimeAllowlists(),
) -> RuntimeLockUpgradeResult:
    """Call only after the separate database backup has been verified.

    Candidate locks are replaced first; the active lock is last. A failure
    preserves the backup and intentionally does not restore over live files.
    """
    try:
        originals, attestations, contract = _read_attested(backup.settings, version_probe, allowlists)
        root = _private_root(backup.settings.data_dir)
        destination = _private_child_root(backup.backup_directory, root)
        if originals != backup.originals or contract != backup.before_contract or _fingerprint(originals) != backup.backup_fingerprint:
            raise _invalid()
        for name, body in originals:
            if _private_file(destination / name, destination).read_bytes() != body:
                raise _invalid()
        if contract == "vad-v1":
            prepared = []
            for name, body in originals:
                replacement = canonical_json(dict(json.loads(body), vad_contract_version="vad-v2")).encode("utf-8")
                temporary = destination / (name + ".prepared")
                _exclusive_write(temporary, replacement)
                prepared.append((temporary, backup.settings.voice_runtime_dir / name, body))
            for temporary, live, body in prepared:
                _require_no_reparse(live)
                if live.read_bytes() != body:
                    raise _invalid()
                os.replace(temporary, live)
            after, attestations, contract = _read_attested(backup.settings, version_probe, allowlists)
            if contract != "vad-v2" or any(
                json.loads(after_body) != dict(json.loads(before_body), vad_contract_version="vad-v2")
                for (_name, after_body), (_old_name, before_body) in zip(after, originals, strict=True)
            ):
                raise _invalid()
        return RuntimeLockUpgradeResult(destination, backup.backup_fingerprint, backup.before_contract, contract, attestations)
    except Exception:
        raise _invalid() from None

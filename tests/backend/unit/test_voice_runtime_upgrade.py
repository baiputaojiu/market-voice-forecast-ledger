import json
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice import runtime_upgrade
from tests.backend.unit.test_voice_runtime import _runtime_fixture, _write


LOCK_NAMES = ("runtime-lock.campplus.json", "runtime-lock.wespeaker.json", "runtime-lock.json")


def three_lock_fixture(tmp_path: Path):
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    active_path = settings.voice_runtime_dir / "runtime-lock.json"
    active = json.loads(active_path.read_bytes())
    (settings.voice_runtime_dir / LOCK_NAMES[0]).write_bytes(active_path.read_bytes())
    other = json.loads(active_path.read_bytes())
    model = settings.voice_model_dir / "other-model.onnx"
    other["model"] = dict(active["model"], name="other-model.onnx", path=str(model), sha256=_write(model, b"synthetic-other-model"))
    (settings.voice_runtime_dir / LOCK_NAMES[1]).write_text(json.dumps(other), encoding="utf-8")
    return settings, probe, allowlists


def _bodies(settings):
    return {name: (settings.voice_runtime_dir / name).read_bytes() for name in LOCK_NAMES}


def test_backup_and_upgrade_preserve_every_non_vad_field(tmp_path: Path) -> None:
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    original = _bodies(settings)
    backup = runtime_upgrade.backup_runtime_locks(
        settings, backup_directory=settings.data_dir / "backups" / "test-repair" / "runtime-locks",
        version_probe=probe, allowlists=allowlists,
    )
    assert _bodies(settings) == original
    assert {name: (backup.backup_directory / name).read_bytes() for name in LOCK_NAMES} == original

    result = runtime_upgrade.upgrade_runtime_locks(backup, version_probe=probe, allowlists=allowlists)

    assert result.before_contract == "vad-v1"
    assert result.after_contract == "vad-v2"
    assert len(result.attestations) == 3
    for name, body in _bodies(settings).items():
        expected = dict(json.loads(original[name]), vad_contract_version="vad-v2")
        assert json.loads(body) == expected
    assert {name: (backup.backup_directory / name).read_bytes() for name in LOCK_NAMES} == original


@pytest.mark.parametrize("mutation", ("mixed", "artifact", "unknown_field", "different_shared_identity"))
def test_invalid_locks_are_rejected_before_backup_creation(tmp_path: Path, mutation: str) -> None:
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    path = settings.voice_runtime_dir / LOCK_NAMES[1]
    values = json.loads(path.read_bytes())
    if mutation == "mixed":
        values["vad_contract_version"] = "vad-v2"
    elif mutation == "artifact":
        values["model"]["sha256"] = "f" * 64
    elif mutation == "unknown_field":
        values["unexpected"] = "private-sentinel"
    else:
        values["adapter_contract_version"] = "other-adapter"
    path.write_text(json.dumps(values), encoding="utf-8")
    original = _bodies(settings)
    destination = settings.data_dir / "backups" / "rejected"
    with pytest.raises(DomainError) as caught:
        runtime_upgrade.backup_runtime_locks(settings, backup_directory=destination, version_probe=probe, allowlists=allowlists)
    assert caught.value.code == "PRESENCE_REPAIR_RUNTIME_INVALID"
    assert not destination.exists()
    assert _bodies(settings) == original


def test_backup_collision_is_never_overwritten(tmp_path: Path) -> None:
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    destination = settings.data_dir / "backups" / "existing"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(DomainError):
        runtime_upgrade.backup_runtime_locks(settings, backup_directory=destination, version_probe=probe, allowlists=allowlists)
    assert marker.read_bytes() == b"keep"


def test_all_v2_locks_are_accepted_without_rewriting(tmp_path: Path) -> None:
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    for name in LOCK_NAMES:
        path = settings.voice_runtime_dir / name
        path.write_text(json.dumps(dict(json.loads(path.read_bytes()), vad_contract_version="vad-v2")), encoding="utf-8")
    original = _bodies(settings)
    times = tuple((settings.voice_runtime_dir / name).stat().st_mtime_ns for name in LOCK_NAMES)
    backup = runtime_upgrade.backup_runtime_locks(settings, backup_directory=settings.data_dir / "backups" / "v2", version_probe=probe, allowlists=allowlists)
    result = runtime_upgrade.upgrade_runtime_locks(backup, version_probe=probe, allowlists=allowlists)
    assert result.before_contract == result.after_contract == "vad-v2"
    assert _bodies(settings) == original
    assert tuple((settings.voice_runtime_dir / name).stat().st_mtime_ns for name in LOCK_NAMES) == times


def test_partial_replace_keeps_active_lock_old_and_preserves_backup(tmp_path: Path, monkeypatch) -> None:
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    original = _bodies(settings)
    backup = runtime_upgrade.backup_runtime_locks(settings, backup_directory=settings.data_dir / "backups" / "partial", version_probe=probe, allowlists=allowlists)
    real_replace = runtime_upgrade.os.replace
    calls = []

    def interrupted_replace(source, destination):
        calls.append(Path(destination).name)
        if len(calls) == 2:
            raise OSError("private-sentinel")
        real_replace(source, destination)

    monkeypatch.setattr(runtime_upgrade.os, "replace", interrupted_replace)
    with pytest.raises(DomainError) as caught:
        runtime_upgrade.upgrade_runtime_locks(backup, version_probe=probe, allowlists=allowlists)
    assert "private-sentinel" not in str(caught.value)
    assert calls == list(LOCK_NAMES[:2])
    assert (settings.voice_runtime_dir / "runtime-lock.json").read_bytes() == original["runtime-lock.json"]
    assert {name: (backup.backup_directory / name).read_bytes() for name in LOCK_NAMES} == original

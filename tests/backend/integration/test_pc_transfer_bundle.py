import hashlib
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer import portable
from market_voice_forecast_ledger.pc_transfer.bundle import (
    ExportDependencies,
    ExportRequest,
    export_bundle,
    verify_bundle,
)
from market_voice_forecast_ledger.windows.task_scheduler import (
    ScheduledTaskStatus,
)
from tests.backend.integration.test_pc_transfer_checkpoint import (
    create_pushed_repository,
    git,
)
from tests.backend.integration.test_pc_transfer_snapshot import (
    insert_job,
    seed_valid_reference_feature,
)
from tests.backend.unit.test_pc_transfer_portable import (
    DEPENDENCY_WHEELS,
    EXPECTED_REQUIREMENT_LINES,
    SuccessfulBuildRunner,
    write_file,
)


class FakeScheduleReader:
    def __init__(self, value: ScheduledTaskStatus) -> None:
        self.value = value

    def status(self) -> ScheduledTaskStatus:
        return self.value


@dataclass
class TransferSourceFixture:
    repository_root: Path
    settings: Settings
    operator_state_dir: Path
    commit_sha: str
    process_runner: SuccessfulBuildRunner

    @property
    def expected_bundle_name(self) -> str:
        return (
            "MarketVoiceForecastLedger-transfer-20260829T030405Z-"
            f"{self.commit_sha[:12]}.zip"
        )

    def export_request(self, tmp_path: Path) -> ExportRequest:
        destination = tmp_path / "drive"
        destination.mkdir(exist_ok=True)
        return ExportRequest(
            repository_root=self.repository_root,
            settings=self.settings,
            operator_state_dir=self.operator_state_dir,
            destination_dir=destination,
            created_at_utc="2026-08-29T03:04:05.000000Z",
            schedule_local_time="06:00",
        )

    def dependencies(
        self,
        *,
        schedule_status: ScheduledTaskStatus | None = None,
        after_temporary_verify=lambda: None,
    ) -> ExportDependencies:
        return ExportDependencies(
            version_probe=lambda _command: "unused",
            process_runner=self.process_runner,
            schedule_reader=FakeScheduleReader(
                schedule_status or ScheduledTaskStatus.unavailable()
            ),
            after_temporary_verify=after_temporary_verify,
        )

    def insert_completed_job(self) -> None:
        insert_job(self.settings.database_path, "succeeded")


@pytest.fixture
def transfer_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TransferSourceFixture:
    git_root = tmp_path / "git"
    git_root.mkdir()
    _, repository = create_pushed_repository(git_root)
    ignored_state_root = repository / ".superpowers/sdd"
    write_file(ignored_state_root / ".gitignore", b"*\n!.gitignore\n")
    git(repository, "add", ".superpowers/sdd/.gitignore")
    git(repository, "commit", "-m", "ignore local operator state")
    git(repository, "push", "origin", "feature/test")
    operator_state = tmp_path / "operator-state"
    write_file(operator_state / "progress.md", b"# Progress\nReady to migrate.\n")
    write_file(operator_state / "rulings.md", b"# Rulings\nFinite checks.\n")
    commit_sha = git(repository, "rev-parse", "HEAD")

    settings = Settings.for_data_dir(tmp_path / "private-data")
    connection = open_database(settings.database_path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    seed_valid_reference_feature(settings.database_path)

    candidate_definitions = {
        "runtime-lock.campplus.json": (
            "3dspeaker",
            "campplus",
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        ),
        "runtime-lock.wespeaker.json": (
            "wespeaker",
            "zh-cnceleb-resnet34",
            "wespeaker_zh_cnceleb_resnet34.onnx",
        ),
    }
    model_hashes: dict[str, str] = {}
    for lock_name, (_, _, filename) in candidate_definitions.items():
        model_hashes[lock_name] = write_file(
            settings.voice_model_dir / filename,
            f"model:{lock_name}".encode(),
        )
    vad_path = settings.voice_model_dir / "silero_vad.onnx"
    vad_hash = write_file(vad_path, b"fixed vad")

    runtime_tools = {
        "deno": ("deno.exe", b"deno"),
        "ffmpeg": ("ffmpeg.exe", b"ffmpeg"),
        "yt_dlp": ("yt-dlp.exe", b"yt-dlp"),
    }
    runtime_paths: dict[str, Path] = {}
    runtime_hashes: dict[str, str] = {}
    for name, (filename, body) in runtime_tools.items():
        runtime_paths[name] = settings.voice_runtime_dir / filename
        runtime_hashes[name] = write_file(runtime_paths[name], body)
    write_file(settings.voice_work_dir / "install/deno-2.9.5/deno.exe", b"deno")
    write_file(
        settings.voice_work_dir
        / "install/ffmpeg-9.0.1/"
        "ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe",
        b"ffmpeg",
    )
    write_file(settings.voice_work_dir / "install/yt-dlp.exe", b"yt-dlp")

    wheelhouse = settings.data_dir / "voice-wheelhouse"
    for filename in DEPENDENCY_WHEELS:
        write_file(wheelhouse / filename, f"wheel:{filename}".encode())
    write_file(
        wheelhouse / "requirements-runtime.txt",
        ("\n".join(EXPECTED_REQUIREMENT_LINES) + "\n").encode(),
    )

    attestations: dict[str, SimpleNamespace] = {}
    for lock_name, (model_name, model_version, filename) in (
        candidate_definitions.items()
    ):
        attestations[lock_name] = SimpleNamespace(
            python_version="3.14.6",
            sherpa_onnx_version="1.13.4",
            yt_dlp_version="2026.08.19",
            deno_version="2.9.5",
            ffmpeg_version="9.0.1",
            vad_version="silero-vad-v5",
            provider="CPUExecutionProvider",
            adapter_contract_version="voice-adapter-v1",
            vad_contract_version="vad-v1",
            model_path=(settings.voice_model_dir / filename).resolve(),
            model_sha256=model_hashes[lock_name],
            model_name=model_name,
            model_version=model_version,
            vad_path=vad_path.resolve(),
            vad_sha256=vad_hash,
            deno_path=runtime_paths["deno"].resolve(),
            deno_sha256=runtime_hashes["deno"],
            ffmpeg_path=runtime_paths["ffmpeg"].resolve(),
            ffmpeg_sha256=runtime_hashes["ffmpeg"],
            yt_dlp_path=runtime_paths["yt_dlp"].resolve(),
            yt_dlp_sha256=runtime_hashes["yt_dlp"],
        )
    attestations["runtime-lock.json"] = attestations[
        "runtime-lock.campplus.json"
    ]

    def fake_attest_runtime(
        candidate_settings: Settings,
        *,
        version_probe,
        lock_name: str = "runtime-lock.json",
    ) -> SimpleNamespace:
        assert candidate_settings == settings
        assert callable(version_probe)
        return attestations[lock_name]

    monkeypatch.setattr(portable, "attest_runtime", fake_attest_runtime)
    return TransferSourceFixture(
        repository_root=repository,
        settings=settings,
        operator_state_dir=operator_state,
        commit_sha=commit_sha,
        process_runner=SuccessfulBuildRunner(),
    )


def assert_transfer_error(code: str, callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code == code


def test_export_writes_one_verified_completed_zip(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    result = export_bundle(
        transfer_source.export_request(tmp_path),
        transfer_source.dependencies(),
    )

    assert result.bundle_path.name == transfer_source.expected_bundle_name
    assert tuple(result.bundle_path.parent.iterdir()) == (result.bundle_path,)
    assert verify_bundle(result.bundle_path).manifest == result.manifest
    with zipfile.ZipFile(result.bundle_path) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names
    assert "data/ledger.sqlite3" in names
    assert not any(".codex" in name for name in names)
    assert not any("voice-runtime" in name for name in names)
    assert not any("archive" in name for name in names)
    assert not any(
        name.endswith(("-wal", "-shm", "-journal")) for name in names
    )


def test_export_refuses_installed_scheduler(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    dependencies = transfer_source.dependencies(
        schedule_status=ScheduledTaskStatus(True, "06:00", True, "Queue")
    )

    assert_transfer_error(
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
        lambda: export_bundle(
            transfer_source.export_request(tmp_path),
            dependencies,
        ),
    )


def test_export_does_not_publish_if_source_changes_after_snapshot(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    request = transfer_source.export_request(tmp_path)
    dependencies = transfer_source.dependencies(
        after_temporary_verify=transfer_source.insert_completed_job
    )

    assert_transfer_error(
        "PC_TRANSFER_SOURCE_CHANGED",
        lambda: export_bundle(request, dependencies),
    )
    assert tuple(request.destination_dir.iterdir()) == ()


def test_export_failure_preserves_existing_destination_file(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    request = transfer_source.export_request(tmp_path)
    occupied = request.destination_dir / transfer_source.expected_bundle_name
    occupied.write_bytes(b"keep")

    assert_transfer_error(
        "PC_TRANSFER_DESTINATION_EXISTS",
        lambda: export_bundle(request, transfer_source.dependencies()),
    )
    assert occupied.read_bytes() == b"keep"
    assert tuple(request.destination_dir.iterdir()) == (occupied,)


@pytest.fixture
def verified_bundle(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> Path:
    return export_bundle(
        transfer_source.export_request(tmp_path),
        transfer_source.dependencies(),
    ).bundle_path


def rewrite_bundle(source: Path, destination: Path, mutation: str) -> Path:
    malformed = destination / f"malformed-{mutation}.zip"
    if mutation == "truncate":
        body = source.read_bytes()
        malformed.write_bytes(body[: len(body) // 2])
        return malformed
    with zipfile.ZipFile(source) as original:
        entries = [(info, original.read(info)) for info in original.infolist()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(malformed, "x", zipfile.ZIP_DEFLATED) as rewritten:
            for info, body in entries:
                if mutation == "remove_manifest" and info.filename == "manifest.json":
                    continue
                if mutation == "remove_member" and info.filename == (
                    "operator-state/presence-verification/progress.md"
                ):
                    continue
                if mutation == "change_member" and info.filename == (
                    "operator-state/presence-verification/progress.md"
                ):
                    body += b"changed"
                rewritten.writestr(info, body)
            if mutation == "add_unknown":
                rewritten.writestr("unknown.txt", b"unknown")
            elif mutation == "parent_escape":
                rewritten.writestr("../escape.txt", b"escape")
            elif mutation == "case_collision":
                rewritten.writestr("Manifest.json", b"collision")
            elif mutation == "duplicate_member":
                info, body = entries[-1]
                rewritten.writestr(info, body)
            elif mutation == "symlink_entry":
                link = zipfile.ZipInfo("link")
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                rewritten.writestr(link, b"target")
    return malformed


@pytest.mark.parametrize(
    "mutation",
    (
        "truncate",
        "change_member",
        "remove_manifest",
        "remove_member",
        "add_unknown",
        "duplicate_member",
        "case_collision",
        "parent_escape",
        "symlink_entry",
    ),
)
def test_verify_rejects_malformed_archive(
    verified_bundle: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    malformed = rewrite_bundle(verified_bundle, tmp_path, mutation)

    assert_transfer_error(
        "PC_TRANSFER_BUNDLE_INVALID",
        lambda: verify_bundle(malformed),
    )


def test_verify_rejects_member_hash_mismatch_even_when_zip_crc_is_valid(
    verified_bundle: Path,
    tmp_path: Path,
) -> None:
    malformed = rewrite_bundle(verified_bundle, tmp_path, "change_member")
    with zipfile.ZipFile(malformed) as archive:
        changed = archive.read(
            "operator-state/presence-verification/progress.md"
        )
    assert hashlib.sha256(changed).hexdigest()

    assert_transfer_error(
        "PC_TRANSFER_BUNDLE_INVALID",
        lambda: verify_bundle(malformed),
    )

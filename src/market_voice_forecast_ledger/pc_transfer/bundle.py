"""Atomic export and strict verification of a PC-transfer ZIP bundle."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib import resources
from pathlib import Path, PurePosixPath

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.checkpoint import (
    CommandRunner,
    inspect_git_checkpoint,
    run_command,
)
from market_voice_forecast_ledger.pc_transfer.manifest import (
    MAX_MANIFEST_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    MAX_TOTAL_BYTES,
    OPERATOR_STATE_DESTINATION,
    SCHEMA,
    BundleMember,
    TransferManifest,
    compute_bundle_id,
    decode_manifest,
    encode_manifest,
    validate_member_path,
)
from market_voice_forecast_ledger.pc_transfer.portable import (
    ProcessRunner,
    collect_portable_inventory,
    run_process,
)
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    DatabaseSnapshotGuard,
    validate_database_snapshot,
)
from market_voice_forecast_ledger.voice.runtime import VersionProbe
from market_voice_forecast_ledger.windows.task_scheduler import (
    ScheduledTaskStatus,
    TaskScheduleReader,
    TaskSchedulerAdapter,
)


_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SCHEDULE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExportRequest:
    repository_root: Path
    settings: Settings
    operator_state_dir: Path
    destination_dir: Path
    created_at_utc: str
    schedule_local_time: str


def _no_operation() -> None:
    return None


@dataclass(frozen=True, slots=True)
class ExportDependencies:
    version_probe: VersionProbe
    process_runner: ProcessRunner = run_process
    command_runner: CommandRunner = run_command
    schedule_reader: TaskScheduleReader = field(
        default_factory=TaskSchedulerAdapter
    )
    after_temporary_verify: Callable[[], None] = _no_operation


@dataclass(frozen=True, slots=True)
class ExportResult:
    bundle_path: Path
    manifest: TransferManifest


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    bundle_path: Path
    manifest: TransferManifest


@dataclass(frozen=True, slots=True)
class ImportRequest:
    bundle_path: Path
    repository_root: Path
    data_root: Path


@dataclass(frozen=True, slots=True)
class ImportResult:
    data_root: Path
    operator_state_dir: Path
    manifest: TransferManifest
    runtime_required: bool
    credential_required: bool
    schedule_required: bool


def _bundle_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_BUNDLE_INVALID",
        "transfer bundle is invalid",
    )


def _quiescence_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
        "transfer source is not quiescent",
    )


def _destination_exists_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_DESTINATION_EXISTS",
        "transfer destination already exists",
    )


def _destination_not_empty_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_DESTINATION_NOT_EMPTY",
        "transfer destination is not empty",
    )


def _git_mismatch_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_GIT_MISMATCH",
        "Git checkout does not match transfer bundle",
    )


def _partial_import_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_IMPORT_PARTIAL",
        "transfer data was restored but operator state placement is incomplete",
    )


def _is_reparse(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        path.is_symlink()
        or bool(is_junction(path))
        or bool(attributes & reparse_flag)
    )


def _validate_request(
    request: ExportRequest,
    dependencies: ExportDependencies,
) -> tuple[Path, datetime]:
    if (
        type(request) is not ExportRequest
        or type(dependencies) is not ExportDependencies
        or not isinstance(request.repository_root, Path)
        or not isinstance(request.settings, Settings)
        or not isinstance(request.operator_state_dir, Path)
        or not isinstance(request.destination_dir, Path)
        or type(request.created_at_utc) is not str
        or _UTC.fullmatch(request.created_at_utc) is None
        or type(request.schedule_local_time) is not str
        or _SCHEDULE.fullmatch(request.schedule_local_time) is None
        or not callable(dependencies.version_probe)
        or not callable(dependencies.process_runner)
        or not callable(dependencies.command_runner)
        or not callable(dependencies.after_temporary_verify)
        or not hasattr(dependencies.schedule_reader, "status")
    ):
        raise _bundle_error()
    try:
        created_at = datetime.strptime(
            request.created_at_utc,
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        if created_at.year < 1980:
            raise ValueError("ZIP timestamp is too old")
        destination = request.destination_dir.absolute()
        if (
            not destination.is_dir()
            or _is_reparse(destination)
            or destination.resolve(strict=True) != destination
        ):
            raise _bundle_error()
        return destination, created_at
    except DomainError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _bundle_error() from exc


def _repository_migrations() -> tuple[str, ...]:
    names = tuple(
        sorted(
            entry.name
            for entry in resources.files(
                "market_voice_forecast_ledger.db.migrations"
            ).iterdir()
            if entry.name[:4].isdigit()
            and entry.name[4:5] == "_"
            and entry.name.endswith(".sql")
        )
    )
    if not names:
        raise _bundle_error()
    return names


def _require_frozen_source(
    request: ExportRequest,
    dependencies: ExportDependencies,
) -> None:
    try:
        status = dependencies.schedule_reader.status()
        if type(status) is not ScheduledTaskStatus or status.installed:
            raise _quiescence_error()
        temp_audio = request.settings.temp_audio_dir
        if temp_audio.exists():
            if (
                not temp_audio.is_dir()
                or _is_reparse(temp_audio)
                or next(temp_audio.iterdir(), None) is not None
            ):
                raise _quiescence_error()
    except DomainError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _quiescence_error() from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while block := reader.read(_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _copy_member(
    source: Path,
    destination: Path,
    member_path: str,
    role: str,
) -> BundleMember:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while block := reader.read(_BLOCK_BYTES):
            size += len(block)
            if size > MAX_MEMBER_BYTES:
                raise _bundle_error()
            digest.update(block)
            writer.write(block)
    return BundleMember(
        path=member_path,
        role=role,
        size_bytes=size,
        sha256=digest.hexdigest(),
    )


def _zip_info(path: str, created_at: datetime, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(
        filename=path,
        date_time=(
            created_at.year,
            created_at.month,
            created_at.day,
            created_at.hour,
            created_at.minute,
            created_at.second,
        ),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.file_size = size
    return info


def _write_zip(
    destination: Path,
    staging_root: Path,
    manifest: TransferManifest,
    created_at: datetime,
) -> None:
    entries = (
        ("manifest.json", staging_root / "manifest.json"),
        *((member.path, staging_root / Path(member.path)) for member in manifest.members),
    )
    with zipfile.ZipFile(
        destination,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for member_path, source in entries:
            size = source.stat().st_size
            info = _zip_info(member_path, created_at, size)
            with source.open("rb") as reader, archive.open(
                info,
                mode="w",
                force_zip64=True,
            ) as writer:
                while block := reader.read(_BLOCK_BYTES):
                    writer.write(block)


def _final_name(created_at: datetime, commit_sha: str) -> str:
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"MarketVoiceForecastLedger-transfer-{timestamp}-"
        f"{commit_sha[:12]}.zip"
    )


def export_bundle(
    request: ExportRequest,
    dependencies: ExportDependencies,
) -> ExportResult:
    destination, created_at = _validate_request(request, dependencies)
    _require_frozen_source(request, dependencies)
    temporary_zip: Path | None = None
    try:
        checkpoint = inspect_git_checkpoint(
            request.repository_root,
            dependencies.command_runner,
        )
        final_path = destination / _final_name(
            created_at,
            checkpoint.commit_sha,
        )
        temporary_zip = destination / (
            final_path.name.removesuffix(".zip") + ".partial.zip"
        )
        if final_path.exists() or temporary_zip.exists():
            raise _destination_exists_error()
        expected_migrations = _repository_migrations()
        with tempfile.TemporaryDirectory(
            prefix="market-voice-pc-transfer-"
        ) as temporary_directory:
            staging_root = Path(temporary_directory)
            with DatabaseSnapshotGuard(
                request.settings.database_path,
                expected_migrations,
            ) as database_guard:
                snapshot = database_guard.create_snapshot(
                    staging_root / "data/ledger.sqlite3"
                )
                inventory = collect_portable_inventory(
                    settings=request.settings,
                    repository_root=request.repository_root,
                    expected_commit=checkpoint.commit_sha,
                    operator_state_dir=request.operator_state_dir,
                    build_dir=staging_root / "build",
                    version_probe=dependencies.version_probe,
                    runner=dependencies.process_runner,
                )
                database_path = staging_root / "data/ledger.sqlite3"
                members: list[BundleMember] = [
                    BundleMember(
                        path="data/ledger.sqlite3",
                        role="database",
                        size_bytes=database_path.stat().st_size,
                        sha256=snapshot.database.snapshot_sha256,
                    )
                ]
                source_hashes: list[tuple[Path, str]] = []
                for item in inventory.files:
                    copied = _copy_member(
                        item.source,
                        staging_root / Path(item.path),
                        item.path,
                        item.role,
                    )
                    if _file_sha256(item.source) != copied.sha256:
                        raise DomainError(
                            "PC_TRANSFER_SOURCE_CHANGED",
                            "transfer source changed during export",
                        )
                    source_hashes.append((item.source, copied.sha256))
                    members.append(copied)
                canonical_members = tuple(
                    sorted(members, key=lambda item: item.path.casefold())
                )
                manifest = TransferManifest(
                    schema=SCHEMA,
                    bundle_id="0" * 64,
                    created_at_utc=request.created_at_utc,
                    repository_url=checkpoint.repository_url,
                    branch=checkpoint.branch,
                    commit_sha=checkpoint.commit_sha,
                    source_tree_clean=True,
                    remote_verified=True,
                    schedule_local_time=request.schedule_local_time,
                    runtime_rebuild_required=True,
                    credential_registration_required=True,
                    schedule_install_required=True,
                    operator_state_destination=OPERATOR_STATE_DESTINATION,
                    database=snapshot.database,
                    runtime=inventory.runtime,
                    members=canonical_members,
                )
                manifest = replace(
                    manifest,
                    bundle_id=compute_bundle_id(manifest),
                )
                (staging_root / "manifest.json").write_bytes(
                    encode_manifest(manifest)
                )
                _write_zip(
                    temporary_zip,
                    staging_root,
                    manifest,
                    created_at,
                )
                verified = verify_bundle(temporary_zip)
                if verified.manifest != manifest:
                    raise _bundle_error()
                dependencies.after_temporary_verify()
                if (
                    inspect_git_checkpoint(
                        request.repository_root,
                        dependencies.command_runner,
                    )
                    != checkpoint
                ):
                    raise DomainError(
                        "PC_TRANSFER_SOURCE_CHANGED",
                        "transfer source changed during export",
                    )
                database_guard.verify_unchanged()
                _require_frozen_source(request, dependencies)
                if any(
                    _file_sha256(source) != expected_hash
                    for source, expected_hash in source_hashes
                ):
                    raise DomainError(
                        "PC_TRANSFER_SOURCE_CHANGED",
                        "transfer source changed during export",
                    )
                if final_path.exists():
                    raise _destination_exists_error()
                temporary_zip.rename(final_path)
                temporary_zip = None
                return ExportResult(
                    bundle_path=final_path,
                    manifest=manifest,
                )
    except DomainError:
        raise
    except (
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        raise _bundle_error() from exc
    finally:
        if temporary_zip is not None:
            try:
                temporary_zip.unlink(missing_ok=True)
            except OSError:
                pass


def _safe_archive_infos(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBERS + 1:
        raise _bundle_error()
    result: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    total = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for info in infos:
        name = info.filename
        if name != "manifest.json":
            validate_member_path(name)
        unix_kind = (info.external_attr >> 16) & 0o170000
        windows_attributes = info.external_attr & 0xFFFF
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or unix_kind == stat.S_IFLNK
            or windows_attributes & reparse_flag
        ):
            raise _bundle_error()
        folded_name = name.casefold()
        if name in result or folded_name in folded:
            raise _bundle_error()
        total += info.file_size
        if (
            type(info.file_size) is not int
            or info.file_size < 0
            or info.file_size > MAX_MEMBER_BYTES
            or total > MAX_TOTAL_BYTES
        ):
            raise _bundle_error()
        result[name] = info
        folded.add(folded_name)
    return result


def _read_manifest(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> TransferManifest:
    if info.file_size <= 0 or info.file_size > MAX_MANIFEST_BYTES:
        raise _bundle_error()
    with archive.open(info, "r") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if stream.read(1) or len(raw) != info.file_size:
            raise _bundle_error()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _bundle_error()
    return decode_manifest(raw)


def verify_bundle(bundle_path: Path) -> VerifiedBundle:
    try:
        if (
            not isinstance(bundle_path, Path)
            or bundle_path.suffix.casefold() != ".zip"
            or not bundle_path.is_file()
            or _is_reparse(bundle_path)
        ):
            raise _bundle_error()
        resolved = bundle_path.resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="market-voice-pc-transfer-verify-"
        ) as temporary_directory:
            database_copy = Path(temporary_directory) / "ledger.sqlite3"
            with zipfile.ZipFile(resolved, "r") as archive:
                infos = _safe_archive_infos(archive)
                manifest_info = infos.get("manifest.json")
                if manifest_info is None:
                    raise _bundle_error()
                manifest = _read_manifest(archive, manifest_info)
                expected_names = {"manifest.json"} | {
                    member.path for member in manifest.members
                }
                if set(infos) != expected_names:
                    raise _bundle_error()
                member_by_path = {
                    member.path: member for member in manifest.members
                }
                for member_path in sorted(member_by_path, key=str.casefold):
                    member = member_by_path[member_path]
                    info = infos[member_path]
                    if info.file_size != member.size_bytes:
                        raise _bundle_error()
                    digest = hashlib.sha256()
                    size = 0
                    database_writer = (
                        database_copy.open("xb")
                        if member_path == "data/ledger.sqlite3"
                        else None
                    )
                    try:
                        with archive.open(info, "r") as reader:
                            while block := reader.read(_BLOCK_BYTES):
                                size += len(block)
                                if size > member.size_bytes:
                                    raise _bundle_error()
                                digest.update(block)
                                if database_writer is not None:
                                    database_writer.write(block)
                    finally:
                        if database_writer is not None:
                            database_writer.close()
                    if (
                        size != member.size_bytes
                        or digest.hexdigest() != member.sha256
                    ):
                        raise _bundle_error()
            validate_database_snapshot(database_copy, manifest.database)
        return VerifiedBundle(bundle_path=resolved, manifest=manifest)
    except DomainError as exc:
        if exc.code == "PC_TRANSFER_BUNDLE_INVALID":
            raise
        raise _bundle_error() from None
    except (
        OSError,
        EOFError,
        RuntimeError,
        UnicodeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        raise _bundle_error() from exc


def imported_member_path(data_root: Path, member: BundleMember) -> Path:
    path = PurePosixPath(member.path)
    if member.role == "database":
        return data_root / "ledger.sqlite3"
    if member.role == "model":
        return data_root / "voice-models" / path.name
    if member.role in {"runtime-requirements", "runtime-wheel"}:
        return data_root / "voice-wheelhouse" / path.name
    if member.role in {"runtime-tool", "project-wheel"}:
        return data_root / "voice-work" / "install" / path.name
    raise _bundle_error()


def _repository_urls_match(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    left_local = left_path.is_absolute()
    right_local = right_path.is_absolute()
    if left_local or right_local:
        if not left_local or not right_local:
            return False
        try:
            return left_path.resolve(strict=True) == right_path.resolve(strict=True)
        except OSError:
            return False
    return left.rstrip("/") == right.rstrip("/")


def _require_matching_checkpoint(
    repository_root: Path,
    manifest: TransferManifest,
) -> object:
    try:
        checkpoint = inspect_git_checkpoint(repository_root)
    except DomainError:
        raise _git_mismatch_error() from None
    if (
        not _repository_urls_match(
            checkpoint.repository_url,
            manifest.repository_url,
        )
        or checkpoint.branch != manifest.branch
        or checkpoint.commit_sha != manifest.commit_sha
        or checkpoint.remote_sha != manifest.commit_sha
    ):
        raise _git_mismatch_error()
    return checkpoint


def _require_absent_or_empty(path: Path) -> bool:
    if path.is_symlink() or _is_reparse(path):
        raise _destination_not_empty_error()
    if not path.exists():
        return False
    if not path.is_dir() or next(path.iterdir(), None) is not None:
        raise _destination_not_empty_error()
    return True


def _extract_exact_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    member: BundleMember,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as reader, destination.open("xb") as writer:
        while block := reader.read(_BLOCK_BYTES):
            size += len(block)
            if size > member.size_bytes:
                raise _bundle_error()
            digest.update(block)
            writer.write(block)
    if size != member.size_bytes or digest.hexdigest() != member.sha256:
        raise _bundle_error()


def _operator_relative_path(member: BundleMember) -> Path:
    prefix = "operator-state/presence-verification/"
    if member.role != "operator-state" or not member.path.startswith(prefix):
        raise _bundle_error()
    relative = PurePosixPath(member.path.removeprefix(prefix))
    if not relative.parts:
        raise _bundle_error()
    return Path(*relative.parts)


def _after_staged_extract() -> None:
    return None


def _place_operator_staging(source: Path, destination: Path) -> None:
    source.rename(destination)


def _remove_staging(path: Path | None, parent: Path, prefix: str) -> None:
    if path is None or not path.exists():
        return
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(parent)
        if resolved.parent != parent or not resolved.name.startswith(prefix):
            return
        shutil.rmtree(resolved)
    except OSError:
        return


def import_bundle(request: ImportRequest) -> ImportResult:
    if (
        type(request) is not ImportRequest
        or not isinstance(request.bundle_path, Path)
        or not isinstance(request.repository_root, Path)
        or not isinstance(request.data_root, Path)
    ):
        raise _bundle_error()
    verified = verify_bundle(request.bundle_path)
    manifest = verified.manifest
    _require_matching_checkpoint(request.repository_root, manifest)
    data_staging: Path | None = None
    operator_staging: Path | None = None
    data_placed = False
    try:
        repository = request.repository_root.resolve(strict=True)
        if not repository.is_dir() or _is_reparse(repository):
            raise _git_mismatch_error()
        raw_data_root = request.data_root.absolute()
        data_parent = raw_data_root.parent.resolve(strict=True)
        if not data_parent.is_dir() or _is_reparse(data_parent):
            raise _destination_not_empty_error()
        data_root = data_parent / raw_data_root.name
        operator_raw = repository / Path(manifest.operator_state_destination)
        operator_destination = operator_raw.resolve(strict=False)
        operator_destination.relative_to(repository)
        operator_parent = operator_destination.parent.resolve(strict=True)
        if not operator_parent.is_dir() or _is_reparse(operator_parent):
            raise _destination_not_empty_error()
        data_was_empty = _require_absent_or_empty(data_root)
        operator_was_empty = _require_absent_or_empty(operator_destination)
        if data_was_empty:
            data_root.rmdir()
        if operator_was_empty:
            operator_destination.rmdir()

        data_staging = Path(
            tempfile.mkdtemp(dir=data_parent, prefix=".mvfl-import-")
        )
        operator_staging = Path(
            tempfile.mkdtemp(dir=operator_parent, prefix=".mvfl-operator-")
        )
        with zipfile.ZipFile(verified.bundle_path, "r") as archive:
            infos = _safe_archive_infos(archive)
            expected_names = {"manifest.json"} | {
                member.path for member in manifest.members
            }
            if set(infos) != expected_names:
                raise _bundle_error()
            for member in manifest.members:
                if member.role == "operator-state":
                    destination = operator_staging / _operator_relative_path(member)
                else:
                    destination = imported_member_path(data_staging, member)
                _extract_exact_member(
                    archive,
                    infos[member.path],
                    member,
                    destination,
                )
        try:
            validate_database_snapshot(
                data_staging / "ledger.sqlite3",
                manifest.database,
            )
        except DomainError:
            raise _bundle_error() from None
        _after_staged_extract()
        _require_matching_checkpoint(repository, manifest)
        second_verification = verify_bundle(verified.bundle_path)
        if second_verification.manifest != manifest:
            raise _bundle_error()
        data_staging.rename(data_root)
        data_placed = True
        data_staging = None
        try:
            _place_operator_staging(operator_staging, operator_destination)
        except OSError as exc:
            raise _partial_import_error() from exc
        operator_staging = None
        return ImportResult(
            data_root=data_root,
            operator_state_dir=operator_destination,
            manifest=manifest,
            runtime_required=manifest.runtime_rebuild_required,
            credential_required=manifest.credential_registration_required,
            schedule_required=manifest.schedule_install_required,
        )
    except DomainError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
        if data_placed:
            raise _partial_import_error() from exc
        raise _bundle_error() from exc
    finally:
        if data_staging is not None:
            _remove_staging(data_staging, data_parent, ".mvfl-import-")
        if operator_staging is not None:
            _remove_staging(
                operator_staging,
                operator_parent,
                ".mvfl-operator-",
            )


__all__ = [
    "ExportDependencies",
    "ExportRequest",
    "ExportResult",
    "ImportRequest",
    "ImportResult",
    "VerifiedBundle",
    "export_bundle",
    "import_bundle",
    "imported_member_path",
    "verify_bundle",
]

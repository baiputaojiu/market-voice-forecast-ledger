"""User-facing command workflow for verified PC transfer."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.bundle import (
    ExportDependencies,
    ExportRequest,
    ExportResult,
    ImportRequest,
    ImportResult,
    VerifiedBundle,
    export_bundle,
    import_bundle,
    verify_bundle,
)
from market_voice_forecast_ledger.pc_transfer.runtime_rebuild import (
    RuntimeRebuildDependencies,
    RuntimeRebuildRequest,
    RuntimeRebuildResult,
    probe_version,
    rebuild_voice_runtime,
    verify_rebuilt_runtime,
)
from market_voice_forecast_ledger.windows.task_scheduler import (
    TaskSchedulerAdapter,
)


PUBLIC_TRANSFER_ERRORS = frozenset(
    {
        "PC_TRANSFER_BUNDLE_INVALID",
        "PC_TRANSFER_DATABASE_INVALID",
        "PC_TRANSFER_DESTINATION_EXISTS",
        "PC_TRANSFER_DESTINATION_NOT_EMPTY",
        "PC_TRANSFER_GIT_MISMATCH",
        "PC_TRANSFER_GIT_UNVERIFIED",
        "PC_TRANSFER_IMPORT_PARTIAL",
        "PC_TRANSFER_LOCAL_DATA_UNAVAILABLE",
        "PC_TRANSFER_MANIFEST_INVALID",
        "PC_TRANSFER_PORTABLE_INVALID",
        "PC_TRANSFER_RUNTIME_EXISTS",
        "PC_TRANSFER_RUNTIME_INCOMPLETE",
        "PC_TRANSFER_RUNTIME_INVALID",
        "PC_TRANSFER_SOURCE_CHANGED",
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
    }
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


class SingleUseAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del option_string
        marker = f"_single_use_{self.dest}"
        if getattr(namespace, marker, False):
            parser.error("duplicate option")
        setattr(namespace, marker, True)
        setattr(namespace, self.dest, values)


def _schedule_time(value: str) -> str:
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise argparse.ArgumentTypeError("invalid schedule time")
    return value


def _path_option(
    parser: argparse.ArgumentParser,
    name: str,
    *,
    required: bool = False,
) -> None:
    parser.add_argument(
        name,
        type=Path,
        action=SingleUseAction,
        required=required,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="pc-transfer",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", allow_abbrev=False)
    _path_option(export, "--destination", required=True)
    export.add_argument(
        "--schedule-local-time",
        type=_schedule_time,
        action=SingleUseAction,
        required=True,
    )
    _path_option(export, "--repository-root")
    _path_option(export, "--data-root")
    _path_option(export, "--operator-state")

    verify = commands.add_parser("verify", allow_abbrev=False)
    _path_option(verify, "--bundle", required=True)

    import_command = commands.add_parser("import", allow_abbrev=False)
    _path_option(import_command, "--bundle", required=True)
    _path_option(import_command, "--repository-root")
    _path_option(import_command, "--data-root")

    rebuild = commands.add_parser("rebuild-runtime", allow_abbrev=False)
    _path_option(rebuild, "--bundle", required=True)
    _path_option(rebuild, "--data-root")

    verify_runtime = commands.add_parser(
        "verify-runtime",
        allow_abbrev=False,
    )
    _path_option(verify_runtime, "--bundle", required=True)
    _path_option(verify_runtime, "--data-root")
    return parser


@dataclass(frozen=True, slots=True)
class CliDependencies:
    repository_root: Path
    clock: Callable[[], datetime]
    export_dependencies: ExportDependencies
    runtime_dependencies: RuntimeRebuildDependencies
    export_service: Callable[
        [ExportRequest, ExportDependencies],
        ExportResult,
    ]
    verify_service: Callable[[Path], VerifiedBundle]
    import_service: Callable[[ImportRequest], ImportResult]
    rebuild_service: Callable[
        [RuntimeRebuildRequest, RuntimeRebuildDependencies],
        RuntimeRebuildResult,
    ]
    runtime_verify_service: Callable[
        [RuntimeRebuildRequest, RuntimeRebuildDependencies],
        RuntimeRebuildResult,
    ]

    @classmethod
    def production(cls) -> "CliDependencies":
        repository_root = Path(__file__).resolve().parents[3]
        return cls(
            repository_root=repository_root,
            clock=lambda: datetime.now(timezone.utc),
            export_dependencies=ExportDependencies(
                version_probe=probe_version,
                schedule_reader=TaskSchedulerAdapter(),
            ),
            runtime_dependencies=RuntimeRebuildDependencies(),
            export_service=export_bundle,
            verify_service=verify_bundle,
            import_service=import_bundle,
            rebuild_service=rebuild_voice_runtime,
            runtime_verify_service=verify_rebuilt_runtime,
        )


def _default_data_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if type(value) is not str or not value.strip():
        raise DomainError(
            "PC_TRANSFER_LOCAL_DATA_UNAVAILABLE",
            "local data root is unavailable",
        )
    return Path(value) / "MarketVoiceForecastLedger"


def _repository_root(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> Path:
    value = getattr(arguments, "repository_root", None)
    return value if isinstance(value, Path) else dependencies.repository_root


def _data_root(arguments: argparse.Namespace) -> Path:
    value = getattr(arguments, "data_root", None)
    return value if isinstance(value, Path) else _default_data_root()


def _export_request(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> ExportRequest:
    repository_root = _repository_root(arguments, dependencies)
    operator = getattr(arguments, "operator_state", None)
    operator_state = (
        operator
        if isinstance(operator, Path)
        else repository_root
        / ".superpowers"
        / "sdd"
        / "2026-08-22-presence-verification"
    )
    return ExportRequest(
        repository_root=repository_root,
        settings=Settings.for_data_dir(_data_root(arguments)),
        operator_state_dir=operator_state,
        destination_dir=arguments.destination,
        created_at_utc=utc_iso(dependencies.clock()),
        schedule_local_time=arguments.schedule_local_time,
    )


def _import_request(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> ImportRequest:
    return ImportRequest(
        bundle_path=arguments.bundle,
        repository_root=_repository_root(arguments, dependencies),
        data_root=_data_root(arguments),
    )


def _emit_stdout(value: dict[str, object]) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def _emit_error(code: str) -> None:
    sys.stderr.write(
        canonical_json({"status": "failed", "error_code": code}) + "\n"
    )


def main(
    argv: Sequence[str] | None = None,
    dependencies: CliDependencies | None = None,
) -> int:
    deps = dependencies or CliDependencies.production()
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if arguments.command == "export":
            result = deps.export_service(
                _export_request(arguments, deps),
                deps.export_dependencies,
            )
            _emit_stdout(
                {
                    "status": "exported",
                    "bundle_id": result.manifest.bundle_id,
                    "bundle_path": str(result.bundle_path),
                    "commit_sha": result.manifest.commit_sha,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "verify":
            result = deps.verify_service(arguments.bundle)
            _emit_stdout(
                {
                    "status": "verified",
                    "bundle_id": result.manifest.bundle_id,
                    "commit_sha": result.manifest.commit_sha,
                    "branch": result.manifest.branch,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "import":
            result = deps.import_service(_import_request(arguments, deps))
            _emit_stdout(
                {
                    "status": "imported",
                    "bundle_id": result.manifest.bundle_id,
                    "runtime_required": result.runtime_required,
                    "credential_required": result.credential_required,
                    "schedule_required": result.schedule_required,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "rebuild-runtime":
            verified = deps.verify_service(arguments.bundle)
            result = deps.rebuild_service(
                RuntimeRebuildRequest(
                    data_root=_data_root(arguments),
                    manifest=verified.manifest,
                ),
                deps.runtime_dependencies,
            )
            _emit_stdout(
                {
                    "status": "runtime-rebuilt",
                    "bundle_id": verified.manifest.bundle_id,
                    "attestation_count": len(result.attestations),
                }
            )
        else:
            verified = deps.verify_service(arguments.bundle)
            result = deps.runtime_verify_service(
                RuntimeRebuildRequest(
                    data_root=_data_root(arguments),
                    manifest=verified.manifest,
                ),
                deps.runtime_dependencies,
            )
            _emit_stdout(
                {
                    "status": "runtime-verified",
                    "bundle_id": verified.manifest.bundle_id,
                    "attestation_count": len(result.attestations),
                }
            )
        return 0
    except DomainError as error:
        code = (
            error.code
            if error.code in PUBLIC_TRANSFER_ERRORS
            else "PC_TRANSFER_COMMAND_FAILED"
        )
        _emit_error(code)
        return 2
    except Exception:
        _emit_error("PC_TRANSFER_COMMAND_FAILED")
        return 2


__all__ = [
    "CliDependencies",
    "PUBLIC_TRANSFER_ERRORS",
    "build_parser",
    "main",
]

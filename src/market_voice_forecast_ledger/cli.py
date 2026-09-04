from __future__ import annotations

import argparse
import getpass
import math
import os
import re
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import time
from pathlib import Path

import uvicorn

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.credentials import CredentialStore
from market_voice_forecast_ledger.credentials.windows import (
    WindowsCredentialManager,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.windows.task_scheduler import (
    ScheduledTaskStatus,
    TaskSchedulerAdapter,
)


_PUBLIC_CLI_ERROR_CODES = frozenset(
    {
        "CLI_COMMAND_INVALID",
        "INVALID_SERVER_PORT",
        "LOCAL_DATA_DIRECTORY_UNAVAILABLE",
        "NON_LOOPBACK_BIND_FORBIDDEN",
        "YOUTUBE_CREDENTIAL_INVALID",
        "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
        "YOUTUBE_SCHEDULE_OPERATION_FAILED",
        "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE",
    }
)
_PUBLIC_PRESENCE_CLI_ERRORS = {
    **{code: code for code in (
        "PRESENCE_REPAIR_TARGET_INVALID", "PRESENCE_REPAIR_PREVIEW_CHANGED",
        "PRESENCE_REPAIR_BACKUP_FAILED", "PRESENCE_REPAIR_RUNTIME_INVALID",
        "PRESENCE_REPAIR_ALREADY_APPLIED", "PRESENCE_REPAIR_APPLY_FAILED",
        "PRESENCE_REPAIR_POSTVERIFY_FAILED",
    )},
    "VOICE_REFERENCE_INVALID": "Presence reference unavailable.",
    "VOICE_REFERENCE_STORED_INVALID": "Presence reference unavailable.",
    "VOICE_REFERENCE_CALIBRATION_FAILED": "Presence calibration unavailable.",
    "PRESENCE_PILOT_INSUFFICIENT": "Presence pilot unavailable.",
    "PRESENCE_REVIEW_INVALID": "Presence review unavailable.",
    "PRESENCE_REVIEW_UNAVAILABLE": "Presence review unavailable.",
    "PRESENCE_REVIEW_FAILED": "Presence review unavailable.",
    "PRESENCE_REVIEW_STALE": "Presence review unavailable.",
    "VOICE_PROCESSING_FAILED": "Presence worker unavailable.",
    "PRESENCE_COMMAND_UNAVAILABLE": "Presence command failed.",
    "PRESENCE_COMMAND_FAILED": "Presence command failed.",
}
_SCHEDULE_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_POSITIVE_CLI_INTEGER = re.compile(r"^[1-9]\d*$")
_SAFE_CLI_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PRIVATE_PATH = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])[a-z]:[\\/]"
    r"|(?<![\\/])(?:\\\\|//)[^\\/\s]"
    r"|(?<![A-Za-z0-9/])/(?!/)[^/\s])"
)


def _parse_preview_hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("invalid preview hash")
    return value


class _SafeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


class _SingleUseAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        marker = f"_single_use_{self.dest}"
        if getattr(namespace, marker, False):
            parser.error("duplicate option")
        setattr(namespace, marker, True)
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def validate_bind_host(host: str) -> str:
    if type(host) is not str or host != "127.0.0.1":
        raise DomainError(
            "NON_LOOPBACK_BIND_FORBIDDEN", "server host must be 127.0.0.1"
        )
    return host


def validate_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise DomainError("INVALID_SERVER_PORT", "server port is invalid")
    return port


def default_settings() -> Settings:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if type(local_app_data) is not str or not local_app_data.strip():
        raise DomainError(
            "LOCAL_DATA_DIRECTORY_UNAVAILABLE",
            "local application data directory is unavailable",
        )
    return Settings.for_data_dir(
        Path(local_app_data) / "MarketVoiceForecastLedger"
    )


def _parse_schedule_time(value: str) -> time:
    if type(value) is not str or _SCHEDULE_TIME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("invalid schedule time")
    return time(hour=int(value[:2]), minute=int(value[3:]))


def _parse_positive_cli_integer(value: str) -> int:
    if (
        type(value) is not str
        or _POSITIVE_CLI_INTEGER.fullmatch(value) is None
    ):
        raise argparse.ArgumentTypeError("positive integer required")
    parsed = int(value)
    if parsed > 2**63 - 1:
        raise argparse.ArgumentTypeError("positive integer required")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="market-voice-forecast-ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    youtube = commands.add_parser("youtube")
    youtube_commands = youtube.add_subparsers(
        dest="youtube_command", required=True
    )
    credential = youtube_commands.add_parser("credential")
    credential_commands = credential.add_subparsers(
        dest="credential_command", required=True
    )
    credential_commands.add_parser("set")
    credential_commands.add_parser("status")
    credential_commands.add_parser("delete")
    schedule = youtube_commands.add_parser("schedule")
    schedule_commands = schedule.add_subparsers(
        dest="schedule_command", required=True
    )
    schedule_install = schedule_commands.add_parser("install")
    schedule_install.add_argument(
        "--time",
        dest="schedule_time",
        type=_parse_schedule_time,
        action=_SingleUseAction,
        default=time(6, 0),
    )
    schedule_update = schedule_commands.add_parser("update")
    schedule_update.add_argument(
        "--time",
        dest="schedule_time",
        type=_parse_schedule_time,
        action=_SingleUseAction,
        required=True,
    )
    schedule_commands.add_parser("status")
    schedule_commands.add_parser("remove")

    youtube_sync = commands.add_parser("youtube-sync")
    youtube_sync_commands = youtube_sync.add_subparsers(
        dest="youtube_sync_command", required=True
    )
    worker = youtube_sync_commands.add_parser("worker")
    worker.add_argument(
        "--once",
        action=_SingleUseAction,
        nargs=0,
        const=True,
        default=False,
        required=True,
    )
    presence = commands.add_parser("presence")
    presence_commands = presence.add_subparsers(
        dest="presence_command", required=True
    )
    reference = presence_commands.add_parser("reference")
    reference_commands = reference.add_subparsers(
        dest="presence_reference_command", required=True
    )
    reference_commands.add_parser("list-candidates")
    approve = reference_commands.add_parser("approve")
    for option in ("subject_id", "video_id", "start_ms", "end_ms"):
        approve.add_argument(
            f"--{option.replace('_', '-')}",
            dest=option,
            type=_parse_positive_cli_integer,
            action=_SingleUseAction,
            required=True,
        )
    presence_commands.add_parser("calibrate")
    pilot = presence_commands.add_parser("pilot")
    pilot_commands = pilot.add_subparsers(
        dest="presence_pilot_command", required=True
    )
    pilot_commands.add_parser("create")
    repair = pilot_commands.add_parser("repair")
    repair_commands = repair.add_subparsers(dest="presence_repair_command", required=True)
    repair_commands.add_parser("preview")
    repair_apply = repair_commands.add_parser("apply")
    repair_apply.add_argument("--expected-preview-hash", type=_parse_preview_hash, action=_SingleUseAction, required=True)
    presence_worker = presence_commands.add_parser("worker")
    presence_worker.add_argument(
        "--once",
        action=_SingleUseAction,
        nargs=0,
        const=True,
        default=False,
        required=True,
    )
    review = presence_commands.add_parser("review")
    review_commands = review.add_subparsers(
        dest="presence_review_command", required=True
    )
    review_commands.add_parser("list")
    show = review_commands.add_parser("show")
    show.add_argument("run_id", type=_parse_positive_cli_integer)
    for action in ("confirm", "reject", "hold"):
        action_parser = review_commands.add_parser(action)
        action_parser.add_argument("run_id", type=_parse_positive_cli_integer)
        action_parser.add_argument(
            "--reason",
            type=str,
            action=_SingleUseAction,
            required=True,
        )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
    task_scheduler_factory: Callable[[], object] | None = None,
    worker_runner: Callable[[Settings], object] | None = None,
    reference_service_factory: Callable[[], object] | None = None,
    presence_service_factory: Callable[[], object] | None = None,
    calibration_runner: Callable[[], object] | None = None,
    presence_worker_runner: Callable[[], object] | None = None,
    presence_repair_service_factory: Callable[[], object] | None = None,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if (
        arguments.command == "presence"
        and arguments.presence_command == "reference"
        and arguments.presence_reference_command == "approve"
        and arguments.start_ms >= arguments.end_ms
    ):
        parser.error("invalid reference interval")
    if arguments.command == "serve":
        host = validate_bind_host(arguments.host)
        port = validate_port(arguments.port)
        app = create_app(default_settings())
        uvicorn.run(app, host=host, port=port)
        return 0
    if (
        arguments.command == "youtube"
        and arguments.youtube_command == "credential"
    ):
        factory = credential_store_factory or WindowsCredentialManager
        store = factory()
        if arguments.credential_command == "set":
            secret = getpass.getpass("YouTube API key: ")
            confirmation = getpass.getpass("Confirm YouTube API key: ")
            if secret != confirmation:
                raise DomainError(
                    "YOUTUBE_CREDENTIAL_INVALID",
                    "YouTube credential is invalid",
                )
            store.set_api_key(secret)
            print("YouTube credential configured.")
            return 0
        if arguments.credential_command == "status":
            print("configured" if store.has_api_key() else "not configured")
            return 0
        if arguments.credential_command == "delete":
            print("deleted" if store.delete_api_key() else "not configured")
            return 0
    if arguments.command == "youtube" and arguments.youtube_command == "schedule":
        scheduler_factory = task_scheduler_factory or TaskSchedulerAdapter
        scheduler = scheduler_factory()
        if arguments.schedule_command == "install":
            scheduler.install(arguments.schedule_time)
            print(f"installed {arguments.schedule_time.strftime('%H:%M')}")
            return 0
        if arguments.schedule_command == "update":
            scheduler.update(arguments.schedule_time)
            print(f"updated {arguments.schedule_time.strftime('%H:%M')}")
            return 0
        if arguments.schedule_command == "status":
            status = scheduler.status()
            if type(status) is not ScheduledTaskStatus:
                raise DomainError(
                    "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE",
                    "YouTube schedule status is unavailable",
                )
            print(
                f"installed {status.local_time}"
                if status.installed
                else "not installed"
            )
            return 0
        if arguments.schedule_command == "remove":
            print("removed" if scheduler.remove() else "not installed")
            return 0
    if (
        arguments.command == "youtube-sync"
        and arguments.youtube_sync_command == "worker"
        and arguments.once is True
    ):
        if worker_runner is None:
            from market_voice_forecast_ledger.workers.scheduled_sync import run_once

            worker_runner = run_once
        worker_runner(default_settings())
        return 0
    if arguments.command == "presence":
        try:
            return _run_presence_command(
                arguments,
                reference_service_factory=reference_service_factory,
                presence_service_factory=presence_service_factory,
                calibration_runner=calibration_runner,
                presence_worker_runner=presence_worker_runner,
                presence_repair_service_factory=presence_repair_service_factory,
            )
        except DomainError as error:
            if error.code in _PUBLIC_PRESENCE_CLI_ERRORS:
                raise
            raise DomainError(
                "PRESENCE_COMMAND_FAILED", "presence command failed"
            ) from None
        except Exception:
            raise DomainError(
                "PRESENCE_COMMAND_FAILED", "presence command failed"
            ) from None
    raise DomainError("CLI_COMMAND_INVALID", "CLI command is invalid")


def _run_presence_command(
    arguments: argparse.Namespace,
    *,
    reference_service_factory: Callable[[], object] | None,
    presence_service_factory: Callable[[], object] | None,
    calibration_runner: Callable[[], object] | None,
    presence_worker_runner: Callable[[], object] | None,
    presence_repair_service_factory: Callable[[], object] | None,
) -> int:
    if arguments.presence_command == "pilot" and arguments.presence_pilot_command == "repair":
        command = arguments.presence_repair_command
        token = getattr(arguments, "expected_preview_hash", None)
        if presence_repair_service_factory is None:
            result = _run_production_presence_repair(command, token)
        else:
            service = presence_repair_service_factory()
            result = service.preview() if command == "preview" else service.apply(token)
        if command == "preview":
            if (result.from_vad_contract_version, result.to_vad_contract_version) != ("vad-v1", "vad-v2") or result.target.counts["jobs"] != 20:
                raise ValueError("invalid repair preview")
            preview_hash = _parse_preview_hash(result.preview_hash)
            print(f"Presence repair preview: 20 jobs, vad-v1 -> vad-v2, preview_hash={preview_hash}")
        else:
            if result.to_vad_contract_version != "vad-v2" or type(result.new_job_ids) is not tuple or len(result.new_job_ids) != 20 or len(set(result.new_job_ids)) != 20 or any(type(value) is not int or value <= 0 for value in result.new_job_ids):
                raise ValueError("invalid repair result")
            print("Presence repair completed: 20 queued vad-v2 jobs.")
        return 0
    if arguments.presence_command == "reference":
        if reference_service_factory is None:
            _presence_dependency_unavailable()
        service = reference_service_factory()
        if arguments.presence_reference_command == "list-candidates":
            clips = service.list_all_candidates()
            for clip in clips:
                print(_format_reference_candidate(clip))
            return 0
        if arguments.presence_reference_command == "approve":
            from market_voice_forecast_ledger.domain.voice_verification import (
                ReferenceClipCommand,
            )

            clip = service.approve_clip(
                ReferenceClipCommand(
                    subject_id=arguments.subject_id,
                    video_id=arguments.video_id,
                    start_ms=arguments.start_ms,
                    end_ms=arguments.end_ms,
                    actor="local_user",
                    reason="approved_reference_clip",
                )
            )
            _require_positive_int(clip.subject_id)
            _require_positive_int(clip.video_id)
            print(
                "Presence reference approved: "
                f"subject {clip.subject_id}, video {clip.video_id}."
            )
            return 0
    if arguments.presence_command == "calibrate":
        if calibration_runner is None:
            _presence_dependency_unavailable()
        calibration_runner()
        print("Presence calibration completed.")
        return 0
    if arguments.presence_command == "pilot":
        if presence_service_factory is None:
            _presence_dependency_unavailable()
        service = presence_service_factory()
        preview = service.preview_pilot()
        preview_hash = preview.preview_hash
        if (
            type(preview_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", preview_hash) is None
        ):
            raise ValueError("invalid pilot preview")
        creation = service.create_pilot(preview_hash)
        job_ids = creation.job_ids
        if (
            type(job_ids) is not tuple
            or any(type(job_id) is not int or job_id <= 0 for job_id in job_ids)
        ):
            raise ValueError("invalid pilot creation")
        print(f"Presence pilot created: {len(job_ids)} jobs.")
        return 0
    if arguments.presence_command == "worker" and arguments.once is True:
        if presence_worker_runner is None:
            _presence_dependency_unavailable()
        presence_worker_runner()
        print("Presence worker completed.")
        return 0
    if arguments.presence_command == "review":
        if presence_service_factory is None:
            _presence_dependency_unavailable()
        service = presence_service_factory()
        if arguments.presence_review_command == "list":
            details = service.list_pending_reviews()
            for detail in details:
                print(_format_review_detail(detail))
            return 0
        if arguments.presence_review_command == "show":
            print(_format_review_detail(service.show_review(arguments.run_id)))
            return 0
        from market_voice_forecast_ledger.domain.voice_verification import (
            ReviewAction,
        )
        from market_voice_forecast_ledger.services.voice_verification import (
            ReviewCommand,
        )
        service.review(
            ReviewCommand(
                run_id=arguments.run_id,
                action=ReviewAction(arguments.presence_review_command),
                reason=arguments.reason,
                actor="local_user",
            )
        )
        print(
            "Presence review recorded: "
            f"{arguments.presence_review_command}."
        )
        return 0
    raise DomainError("PRESENCE_COMMAND_FAILED", "presence command failed")


def _run_production_presence_repair(command: str, expected_preview_hash: str | None = None):
    from market_voice_forecast_ledger.db.connection import open_database
    from market_voice_forecast_ledger.db.migrate import apply_migrations
    from market_voice_forecast_ledger.pc_transfer.runtime_rebuild import probe_version
    from market_voice_forecast_ledger.services.presence_repair import PresenceRepairService, open_repair_readonly

    settings = default_settings()
    with closing(open_repair_readonly(settings.database_path)) as readonly:
        preview = PresenceRepairService(readonly, settings, version_probe=probe_version).preview()
    if command == "preview":
        return preview
    if command != "apply" or expected_preview_hash != preview.preview_hash:
        raise DomainError("PRESENCE_REPAIR_PREVIEW_CHANGED", "presence repair preview changed")
    with closing(open_database(settings.database_path)) as conn:
        service = PresenceRepairService(conn, settings, version_probe=probe_version)
        service._validate_database()
        apply_migrations(conn)
        return service.apply(expected_preview_hash)


def _presence_dependency_unavailable() -> None:
    raise DomainError(
        "PRESENCE_COMMAND_UNAVAILABLE", "presence command unavailable"
    )


def _require_positive_int(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid public identifier")


def _format_reference_candidate(clip: object) -> str:
    subject_id = getattr(clip, "subject_id")
    video_id = getattr(clip, "video_id")
    start_ms = getattr(clip, "start_ms")
    end_ms = getattr(clip, "end_ms")
    ordinal = getattr(clip, "ordinal")
    clip_kind = getattr(clip, "clip_kind")
    for value in (subject_id, video_id, end_ms, ordinal):
        _require_positive_int(value)
    if type(start_ms) is not int or start_ms < 0 or start_ms >= end_ms or (
        type(clip_kind) is not str
        or _SAFE_CLI_TOKEN.fullmatch(clip_kind) is None
    ):
        raise ValueError("invalid reference candidate")
    return (
        "Presence reference candidate: "
        f"subject {subject_id}, video {video_id}, {clip_kind} {ordinal}, "
        f"{start_ms}-{end_ms} ms."
    )


def _format_review_detail(detail: object) -> str:
    run_id = getattr(detail, "run_id")
    name = getattr(detail, "person_display_name")
    watch_url = getattr(detail, "watch_url")
    youtube_video_id = getattr(detail, "youtube_video_id")
    proposal = getattr(detail, "proposal")
    model_name = getattr(detail, "model_name")
    model_version = getattr(detail, "model_version")
    adapter_version = getattr(detail, "adapter_version")
    threshold_version = getattr(detail, "threshold_version")
    segments = getattr(detail, "segments")
    _require_positive_int(run_id)
    if (
        type(name) is not str
        or not name
        or len(name) > 120
        or "\n" in name
        or _PRIVATE_PATH.search(name) is not None
        or "file://" in name.casefold()
        or type(youtube_video_id) is not str
        or _YOUTUBE_VIDEO_ID.fullmatch(youtube_video_id) is None
        or watch_url != f"https://www.youtube.com/watch?v={youtube_video_id}"
        or type(segments) is not tuple
        or not 1 <= len(segments) <= 64
    ):
        raise ValueError("invalid review detail")
    tokens = (model_name, model_version, adapter_version, threshold_version)
    if any(
        type(value) is not str or _SAFE_CLI_TOKEN.fullmatch(value) is None
        for value in tokens
    ):
        raise ValueError("invalid review detail")
    proposal_value = getattr(proposal, "value", proposal)
    if (
        type(proposal_value) is not str
        or _SAFE_CLI_TOKEN.fullmatch(proposal_value) is None
    ):
        raise ValueError("invalid review detail")
    rendered_segments = []
    for segment in segments:
        start_ms = getattr(segment, "start_ms")
        end_ms = getattr(segment, "end_ms")
        score = getattr(segment, "score")
        _require_positive_int(end_ms)
        if (
            type(start_ms) is not int
            or start_ms < 0
            or start_ms >= end_ms
            or type(score) not in (int, float)
            or not math.isfinite(score)
        ):
            raise ValueError("invalid review detail")
        rendered_segments.append(f"{start_ms}-{end_ms}ms={score:.4f}")
    return (
        f"Presence review: run {run_id}, {name}, {watch_url}, "
        f"proposal {proposal_value}, model {model_name} {model_version}, "
        f"adapter {adapter_version}, threshold {threshold_version}, "
        f"segments {', '.join(rendered_segments)}."
    )


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
    task_scheduler_factory: Callable[[], object] | None = None,
    worker_runner: Callable[[Settings], object] | None = None,
    reference_service_factory: Callable[[], object] | None = None,
    presence_service_factory: Callable[[], object] | None = None,
    calibration_runner: Callable[[], object] | None = None,
    presence_worker_runner: Callable[[], object] | None = None,
    presence_repair_service_factory: Callable[[], object] | None = None,
) -> int:
    try:
        return main(
            argv,
            credential_store_factory=credential_store_factory,
            task_scheduler_factory=task_scheduler_factory,
            worker_runner=worker_runner,
            reference_service_factory=reference_service_factory,
            presence_service_factory=presence_service_factory,
            calibration_runner=calibration_runner,
            presence_worker_runner=presence_worker_runner,
            presence_repair_service_factory=presence_repair_service_factory,
        )
    except DomainError as error:
        presence_message = _PUBLIC_PRESENCE_CLI_ERRORS.get(error.code)
        if presence_message is not None:
            print(presence_message, file=sys.stderr)
            return 1
        code = (
            error.code
            if error.code in _PUBLIC_CLI_ERROR_CODES
            else "INTERNAL_ERROR"
        )
        print(code, file=sys.stderr)
        return 1
    except Exception:
        print("INTERNAL_ERROR", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

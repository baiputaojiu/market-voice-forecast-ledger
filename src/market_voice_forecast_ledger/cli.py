from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections.abc import Callable, Sequence
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
_SCHEDULE_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


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
        default=time(6, 0),
    )
    schedule_update = schedule_commands.add_parser("update")
    schedule_update.add_argument(
        "--time",
        dest="schedule_time",
        type=_parse_schedule_time,
        required=True,
    )
    schedule_commands.add_parser("status")
    schedule_commands.add_parser("remove")

    youtube_sync = commands.add_parser("youtube-sync")
    youtube_sync_commands = youtube_sync.add_subparsers(
        dest="youtube_sync_command", required=True
    )
    worker = youtube_sync_commands.add_parser("worker")
    worker.add_argument("--once", action="store_true", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
    task_scheduler_factory: Callable[[], object] | None = None,
    worker_runner: Callable[[Settings], object] | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
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
    raise DomainError("CLI_COMMAND_INVALID", "CLI command is invalid")


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
    task_scheduler_factory: Callable[[], object] | None = None,
    worker_runner: Callable[[Settings], object] | None = None,
) -> int:
    try:
        return main(
            argv,
            credential_store_factory=credential_store_factory,
            task_scheduler_factory=task_scheduler_factory,
            worker_runner=worker_runner,
        )
    except DomainError as error:
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

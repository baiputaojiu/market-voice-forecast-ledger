from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import uvicorn

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.credentials import CredentialStore
from market_voice_forecast_ledger.credentials.windows import (
    WindowsCredentialManager,
)
from market_voice_forecast_ledger.domain.errors import DomainError


_PUBLIC_CLI_ERROR_CODES = frozenset(
    {
        "CLI_COMMAND_INVALID",
        "INVALID_SERVER_PORT",
        "LOCAL_DATA_DIRECTORY_UNAVAILABLE",
        "NON_LOOPBACK_BIND_FORBIDDEN",
        "YOUTUBE_CREDENTIAL_INVALID",
        "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
    }
)


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
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
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
    raise DomainError("CLI_COMMAND_INVALID", "CLI command is invalid")


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    credential_store_factory: Callable[[], CredentialStore] | None = None,
) -> int:
    try:
        return main(
            argv,
            credential_store_factory=credential_store_factory,
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

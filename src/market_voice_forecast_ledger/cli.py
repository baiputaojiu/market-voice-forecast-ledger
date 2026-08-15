from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError


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
    parser = argparse.ArgumentParser(prog="market-voice-forecast-ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command != "serve":
        raise DomainError("CLI_COMMAND_INVALID", "CLI command is invalid")
    host = validate_bind_host(arguments.host)
    port = validate_port(arguments.port)
    app = create_app(default_settings())
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

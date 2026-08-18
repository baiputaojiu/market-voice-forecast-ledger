from __future__ import annotations

import logging

import pytest

import market_voice_forecast_ledger.cli as cli
from market_voice_forecast_ledger.domain.errors import DomainError


SYNTHETIC_SECRET = "synthetic-key-token-000001"


class FakeCredentialStore:
    def __init__(self) -> None:
        self.secret: str | None = None
        self.set_calls: list[str] = []
        self.status_calls = 0
        self.delete_calls = 0
        self.raise_error: Exception | None = None

    def set_api_key(self, secret: str) -> None:
        if self.raise_error is not None:
            raise self.raise_error
        self.set_calls.append(secret)
        self.secret = secret

    def has_api_key(self) -> bool:
        if self.raise_error is not None:
            raise self.raise_error
        self.status_calls += 1
        return self.secret is not None

    def read_api_key(self) -> str:
        assert self.secret is not None
        return self.secret

    def delete_api_key(self) -> bool:
        if self.raise_error is not None:
            raise self.raise_error
        self.delete_calls += 1
        existed = self.secret is not None
        self.secret = None
        return existed


def test_credential_cli_uses_hidden_confirmation_and_exact_safe_outputs(
    monkeypatch, capsys
):
    store = FakeCredentialStore()
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return SYNTHETIC_SECRET

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    factory_calls: list[None] = []

    def factory() -> FakeCredentialStore:
        factory_calls.append(None)
        return store

    assert cli.main(
        ["youtube", "credential", "set"],
        credential_store_factory=factory,
    ) == 0
    assert prompts == ["YouTube API key: ", "Confirm YouTube API key: "]
    assert store.secret == SYNTHETIC_SECRET
    assert capsys.readouterr().out == "YouTube credential configured.\n"

    assert cli.main(
        ["youtube", "credential", "status"],
        credential_store_factory=factory,
    ) == 0
    assert capsys.readouterr().out == "configured\n"

    assert cli.main(
        ["youtube", "credential", "delete"],
        credential_store_factory=factory,
    ) == 0
    assert capsys.readouterr().out == "deleted\n"
    assert factory_calls == [None, None, None]


def test_set_mismatch_fails_closed_without_calling_store_or_echoing_secret(
    monkeypatch, capsys, caplog
):
    store = FakeCredentialStore()
    values = iter((SYNTHETIC_SECRET, "different-synthetic-token-2"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(values))

    with pytest.raises(DomainError) as error:
        cli.main(
            ["youtube", "credential", "set"],
            credential_store_factory=lambda: store,
        )

    assert error.value.code == "YOUTUBE_CREDENTIAL_INVALID"
    assert store.set_calls == []
    captured = capsys.readouterr()
    exposed = captured.out + captured.err + str(error.value) + caplog.text
    assert SYNTHETIC_SECRET not in exposed
    assert "different-synthetic-token" not in exposed


def test_status_and_delete_are_exact_when_credential_is_missing(capsys):
    store = FakeCredentialStore()

    assert cli.main(
        ["youtube", "credential", "status"],
        credential_store_factory=lambda: store,
    ) == 0
    assert capsys.readouterr().out == "not configured\n"

    assert cli.main(
        ["youtube", "credential", "delete"],
        credential_store_factory=lambda: store,
    ) == 0
    assert capsys.readouterr().out == "not configured\n"


@pytest.mark.parametrize(
    "argv",
    (
        ["youtube", "credential", "set", SYNTHETIC_SECRET],
        ["youtube", "credential", "set", "--api-key", SYNTHETIC_SECRET],
        ["youtube", "credential", "set", f"--api-key={SYNTHETIC_SECRET}"],
        ["youtube", "credential", "status", SYNTHETIC_SECRET],
        ["youtube", "credential", "delete", f"--secret={SYNTHETIC_SECRET}"],
        ["youtube", "credential", "read"],
    ),
    ids=(
        "positional-secret",
        "separate-secret-flag",
        "joined-secret-flag",
        "status-positional",
        "delete-secret-flag",
        "read-command-absent",
    ),
)
def test_parser_rejects_secret_inputs_with_safe_text_and_no_dependency(
    argv, capsys
):
    factory_calls: list[None] = []

    def factory() -> FakeCredentialStore:
        factory_calls.append(None)
        return FakeCredentialStore()

    with pytest.raises(SystemExit) as error:
        cli.main(argv, credential_store_factory=factory)

    assert error.value.code == 2
    captured = capsys.readouterr()
    exposed = captured.out + captured.err + str(error.value)
    assert SYNTHETIC_SECRET not in exposed
    assert "--api-key" not in exposed
    assert "--secret" not in exposed
    assert factory_calls == []


def test_help_builds_without_constructing_or_accessing_credentials(capsys):
    factory_calls: list[None] = []

    def factory() -> FakeCredentialStore:
        factory_calls.append(None)
        return FakeCredentialStore()

    with pytest.raises(SystemExit) as error:
        cli.main(
            ["youtube", "credential", "--help"],
            credential_store_factory=factory,
        )

    assert error.value.code == 0
    assert factory_calls == []
    output = capsys.readouterr().out
    assert "{set,status,delete}" in output
    assert "api-key" not in output.lower()
    assert "secret" not in output.lower()


def test_credential_command_ignores_environment_database_and_logging(
    monkeypatch, capsys, caplog
):
    environment_secret = "environment-secret-token-0001"
    prompted_secret = SYNTHETIC_SECRET
    store = FakeCredentialStore()
    monkeypatch.setenv("YOUTUBE_API_KEY", environment_secret)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: prompted_secret)
    monkeypatch.setattr(
        cli,
        "default_settings",
        lambda: (_ for _ in ()).throw(AssertionError("database accessed")),
    )
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda *_args: (_ for _ in ()).throw(AssertionError("app accessed")),
    )
    caplog.set_level(logging.DEBUG)

    assert cli.main(
        ["youtube", "credential", "set"],
        credential_store_factory=lambda: store,
    ) == 0

    assert store.set_calls == [prompted_secret]
    captured = capsys.readouterr()
    exposed = captured.out + captured.err + caplog.text
    assert environment_secret not in exposed
    assert prompted_secret not in exposed


def test_safe_public_runner_exposes_only_domain_code(monkeypatch, capsys):
    store = FakeCredentialStore()
    store.raise_error = DomainError(
        "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
        "C:/private/native/path synthetic-key-token-000001",
    )

    assert cli.run_cli(
        ["youtube", "credential", "status"],
        credential_store_factory=lambda: store,
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "YOUTUBE_CREDENTIAL_STORAGE_FAILED\n"


def test_safe_public_runner_sanitizes_unexpected_dependency_failure(capsys):
    store = FakeCredentialStore()
    store.raise_error = RuntimeError(
        "C:/private/native/path synthetic-key-token-000001"
    )

    assert cli.run_cli(
        ["youtube", "credential", "status"],
        credential_store_factory=lambda: store,
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "INTERNAL_ERROR\n"

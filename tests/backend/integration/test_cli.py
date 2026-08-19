from __future__ import annotations

import logging
from datetime import time

import pytest

import market_voice_forecast_ledger.cli as cli
from market_voice_forecast_ledger.api import dependencies as api_dependencies
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.windows.task_scheduler import ScheduledTaskStatus


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


class FakeTaskScheduler:
    def __init__(
        self,
        *,
        status: ScheduledTaskStatus | None = None,
        remove_result: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.status_value = status or ScheduledTaskStatus.unavailable()
        self.remove_result = remove_result
        self.error = error
        self.install_calls: list[time] = []
        self.update_calls: list[time] = []
        self.status_calls = 0
        self.remove_calls = 0

    def install(self, local_time: time) -> None:
        self._raise_if_needed()
        self.install_calls.append(local_time)

    def update(self, local_time: time) -> None:
        self._raise_if_needed()
        self.update_calls.append(local_time)

    def status(self) -> ScheduledTaskStatus:
        self._raise_if_needed()
        self.status_calls += 1
        return self.status_value

    def remove(self) -> bool:
        self._raise_if_needed()
        self.remove_calls += 1
        return self.remove_result

    def request_start(self) -> None:
        raise AssertionError("schedule CLI must not request an on-demand start")

    def _raise_if_needed(self) -> None:
        if self.error is not None:
            raise self.error


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


@pytest.mark.parametrize(
    ("argv", "operation", "expected_time", "expected_output"),
    (
        (
            ["youtube", "schedule", "install"],
            "install",
            time(6, 0),
            "installed 06:00\n",
        ),
        (
            ["youtube", "schedule", "install", "--time", "00:00"],
            "install",
            time(0, 0),
            "installed 00:00\n",
        ),
        (
            ["youtube", "schedule", "install", "--time", "23:59"],
            "install",
            time(23, 59),
            "installed 23:59\n",
        ),
        (
            ["youtube", "schedule", "update", "--time", "06:00"],
            "update",
            time(6, 0),
            "updated 06:00\n",
        ),
    ),
)
def test_schedule_cli_accepts_only_canonical_times_and_defaults_install(
    argv, operation, expected_time, expected_output, capsys
):
    scheduler = FakeTaskScheduler()
    scheduler_factory_calls: list[None] = []

    def scheduler_factory() -> FakeTaskScheduler:
        scheduler_factory_calls.append(None)
        return scheduler

    def forbidden_dependency():
        raise AssertionError("unrelated dependency constructed")

    assert cli.main(
        argv,
        credential_store_factory=forbidden_dependency,
        task_scheduler_factory=scheduler_factory,
        worker_runner=forbidden_dependency,
    ) == 0

    assert scheduler_factory_calls == [None]
    assert scheduler.install_calls == (
        [expected_time] if operation == "install" else []
    )
    assert scheduler.update_calls == (
        [expected_time] if operation == "update" else []
    )
    assert capsys.readouterr().out == expected_output


@pytest.mark.parametrize(
    "argv",
    (
        ["youtube", "schedule", "install", "--time", "6:00"],
        ["youtube", "schedule", "install", "--time", "06:00:00"],
        ["youtube", "schedule", "install", "--time", "06:00+09:00"],
        ["youtube", "schedule", "install", "--time", "06:00&whoami"],
        ["youtube", "schedule", "install", "extra"],
        ["youtube", "schedule", "update"],
        ["youtube", "schedule", "update", "--time", "00:00", "extra"],
        ["youtube", "schedule", "status", "extra"],
        ["youtube", "schedule", "remove", "--force"],
        ["youtube-sync", "worker"],
        ["youtube-sync", "worker", "--once", "extra"],
    ),
)
def test_schedule_and_worker_parser_rejects_noncanonical_or_extra_arguments(
    argv, capsys, monkeypatch
):
    scheduler_factory_calls: list[None] = []
    worker_calls: list[object] = []

    def scheduler_factory() -> FakeTaskScheduler:
        scheduler_factory_calls.append(None)
        return FakeTaskScheduler()

    monkeypatch.setattr(
        cli,
        "default_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings accessed")),
    )

    with pytest.raises(SystemExit) as error:
        cli.main(
            argv,
            credential_store_factory=lambda: (_ for _ in ()).throw(
                AssertionError("credential accessed")
            ),
            task_scheduler_factory=scheduler_factory,
            worker_runner=worker_calls.append,
        )

    assert error.value.code == 2
    assert scheduler_factory_calls == []
    assert worker_calls == []
    captured = capsys.readouterr()
    assert "invalid arguments" in captured.err
    assert "whoami" not in captured.err.lower()
    assert "06:00:00" not in captured.err
    assert "06:00+09:00" not in captured.err


@pytest.mark.parametrize(
    ("status", "expected_output"),
    (
        (ScheduledTaskStatus(True, "06:00", True, "Queue"), "installed 06:00\n"),
        (ScheduledTaskStatus.unavailable(), "not installed\n"),
    ),
)
def test_schedule_status_prints_only_safe_canonical_state(
    status, expected_output, capsys
):
    scheduler = FakeTaskScheduler(status=status)

    assert cli.main(
        ["youtube", "schedule", "status"],
        task_scheduler_factory=lambda: scheduler,
    ) == 0

    assert scheduler.status_calls == 1
    assert capsys.readouterr().out == expected_output


@pytest.mark.parametrize(
    ("removed", "expected_output"),
    ((True, "removed\n"), (False, "not installed\n")),
)
def test_schedule_remove_is_idempotent(removed, expected_output, capsys):
    scheduler = FakeTaskScheduler(remove_result=removed)

    assert cli.main(
        ["youtube", "schedule", "remove"],
        task_scheduler_factory=lambda: scheduler,
    ) == 0

    assert scheduler.remove_calls == 1
    assert capsys.readouterr().out == expected_output


def test_worker_once_calls_only_injected_runner_with_default_settings(
    monkeypatch, tmp_path, capsys
):
    settings = Settings.for_data_dir(tmp_path / "runtime")
    settings_calls: list[None] = []
    worker_calls: list[Settings] = []

    def settings_factory() -> Settings:
        settings_calls.append(None)
        return settings

    monkeypatch.setattr(cli, "default_settings", settings_factory)

    def forbidden_dependency():
        raise AssertionError("unrelated native dependency constructed")

    assert cli.main(
        ["youtube-sync", "worker", "--once"],
        credential_store_factory=forbidden_dependency,
        task_scheduler_factory=forbidden_dependency,
        worker_runner=worker_calls.append,
    ) == 0

    assert settings_calls == [None]
    assert worker_calls == [settings]
    assert capsys.readouterr().out == ""


def test_schedule_safe_runner_does_not_expose_native_failure(capsys):
    scheduler = FakeTaskScheduler(
        error=DomainError(
            "YOUTUBE_SCHEDULE_OPERATION_FAILED",
            "C:/private/native/path synthetic-key-token-000001",
        )
    )

    assert cli.run_cli(
        ["youtube", "schedule", "install"],
        task_scheduler_factory=lambda: scheduler,
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "YOUTUBE_SCHEDULE_OPERATION_FAILED\n"


def test_app_construction_does_not_construct_or_run_task_scheduler(
    monkeypatch, tmp_path
):
    native_calls: list[object] = []

    def forbidden_scheduler():
        native_calls.append(None)
        raise AssertionError("scheduler constructed during app creation")

    monkeypatch.setattr(
        api_dependencies,
        "TaskSchedulerAdapter",
        forbidden_scheduler,
    )
    settings = Settings.for_data_dir(tmp_path / "runtime")

    app = cli.create_app(settings)

    assert app is not None
    assert native_calls == []


@pytest.mark.parametrize(
    "argv",
    (
        ["youtube", "schedule", "install", "--ti", "06:00"],
        ["youtube", "schedule", "update", "--ti", "06:00"],
        ["youtube-sync", "worker", "--onc"],
        [
            "youtube",
            "schedule",
            "install",
            "--time",
            "06:00",
            "--time",
            "07:00",
        ],
        [
            "youtube",
            "schedule",
            "update",
            "--time",
            "06:00",
            "--time",
            "07:00",
        ],
        ["youtube-sync", "worker", "--once", "--once"],
    ),
)
def test_m1_parser_rejects_abbreviated_or_duplicate_single_use_options(
    argv, monkeypatch, tmp_path
):
    scheduler = FakeTaskScheduler()
    worker_calls: list[Settings] = []
    settings = Settings.for_data_dir(tmp_path / "runtime")
    monkeypatch.setattr(cli, "default_settings", lambda: settings)

    with pytest.raises(SystemExit) as caught:
        cli.main(
            argv,
            task_scheduler_factory=lambda: scheduler,
            worker_runner=worker_calls.append,
        )

    assert caught.value.code == 2
    assert scheduler.install_calls == []
    assert scheduler.update_calls == []
    assert worker_calls == []

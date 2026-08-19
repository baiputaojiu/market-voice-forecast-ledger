from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.windows.task_scheduler import (
    SCHEDULE_QUERY_TIMEOUT_SECONDS,
    YOUTUBE_SYNC_TASK_NAME,
    ScheduledTaskStatus,
    TaskSchedulerAdapter,
)
from tests.backend.youtube_fakes import FakeCompletedProcess, FakeSubprocessRunner


VALID_TASK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.4">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-19T06:00:00+09:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
</Task>
"""

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
CURRENT_USER_SID = "S-1-5-21-111111111-222222222-333333333-1001"
WHOAMI_CSV = f'"SYNTHETIC\\User","{CURRENT_USER_SID}"\r\n'.encode()


def _assert_safe_native_call(call, expected_argv):
    argv, kwargs = call
    assert argv == expected_argv
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == SCHEDULE_QUERY_TIMEOUT_SECONDS
    assert kwargs["check"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_status_queries_the_fixed_task_and_strictly_reads_its_schedule():
    runner = FakeSubprocessRunner(FakeCompletedProcess(stdout=VALID_TASK_XML))

    status = TaskSchedulerAdapter(runner=runner).status()

    assert status == ScheduledTaskStatus(
        installed=True,
        local_time="06:00",
        start_when_available=True,
        multiple_instances="Queue",
    )
    _assert_safe_native_call(
        runner.calls[0],
        ("schtasks.exe", "/Query", "/TN", YOUTUBE_SYNC_TASK_NAME, "/XML"),
    )


def test_request_start_runs_only_the_fixed_task_with_native_output_suppressed():
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(stdout=b"native-private-output", stderr=b"native-private-error")
    )

    TaskSchedulerAdapter(runner=runner).request_start()

    _assert_safe_native_call(
        runner.calls[0],
        ("schtasks.exe", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME),
    )


def test_missing_task_is_reported_without_parsing_native_output():
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-output",
            stderr=b"native-private-error",
        )
    )

    assert TaskSchedulerAdapter(runner=runner).status() == ScheduledTaskStatus.unavailable()


@pytest.mark.parametrize(
    "xml",
    (
        b"not xml",
        VALID_TASK_XML.replace(b"+09:00", b"Z"),
        VALID_TASK_XML.replace(b"<DaysInterval>1</DaysInterval>", b"<DaysInterval>2</DaysInterval>"),
        VALID_TASK_XML.replace(b"InteractiveToken", b"Password"),
        VALID_TASK_XML.replace(b"<StartWhenAvailable>true</StartWhenAvailable>", b"<StartWhenAvailable>false</StartWhenAvailable>"),
        VALID_TASK_XML.replace(b"<MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>", b"<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>"),
        b'<!DOCTYPE Task [<!ENTITY leak SYSTEM "file:///private">]>' + VALID_TASK_XML,
    ),
)
def test_status_rejects_unsafe_or_unexpected_task_xml(xml):
    runner = FakeSubprocessRunner(FakeCompletedProcess(stdout=xml))

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).status()

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
    assert "private" not in str(caught.value).lower()


@pytest.mark.parametrize(
    "operation",
    ("status", "request_start"),
)
def test_native_failures_expose_only_a_safe_scheduler_code(operation):
    runner = FakeSubprocessRunner(OSError("native-private-output"))
    adapter = TaskSchedulerAdapter(runner=runner)

    with pytest.raises(DomainError) as caught:
        getattr(adapter, operation)()

    expected = (
        "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
        if operation == "status"
        else "YOUTUBE_SYNC_UNAVAILABLE"
    )
    assert caught.value.code == expected
    assert "native-private-output" not in str(caught.value)


def test_schedule_status_uses_fixed_jst_day_and_due_boundary():
    status = ScheduledTaskStatus(True, "06:00", True, "Queue")
    before = datetime(2026, 8, 18, 20, 59, 59, tzinfo=timezone.utc)
    exact = datetime(2026, 8, 18, 21, 0, 0, tzinfo=timezone.utc)
    after = datetime(2026, 8, 19, 5, 0, 0, tzinfo=timezone.utc)

    assert status.jst_day(before) == date(2026, 8, 19)
    assert status.is_due(before) is False
    assert status.is_due(exact) is True
    assert status.is_due(after) is True
    assert ScheduledTaskStatus.unavailable().is_due(after) is False


@pytest.mark.parametrize(
    "value",
    (
        datetime(2026, 8, 19, 6, 0),
        datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc).astimezone(),
        "2026-08-19T06:00:00Z",
    ),
)
def test_schedule_time_helpers_reject_non_exact_utc_inputs(value):
    status = ScheduledTaskStatus(True, "06:00", True, "Queue")

    with pytest.raises(DomainError) as caught:
        status.jst_day(value)  # type: ignore[arg-type]

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_INVALID"


def _registration_runner(*, create_response: object = None, whoami: bytes = WHOAMI_CSV):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    captured: dict[str, object] = {}
    response = create_response or FakeCompletedProcess()

    def runner(argv: object, **kwargs: object) -> object:
        if type(argv) is not list or any(type(item) is not str for item in argv):
            raise AssertionError("scheduler argv must be a list of strings")
        call = (tuple(argv), dict(kwargs))
        calls.append(call)
        if tuple(argv) == ("whoami.exe", "/user", "/fo", "csv", "/nh"):
            return FakeCompletedProcess(stdout=whoami)
        if len(argv) == 7 and argv[:4] == [
            "schtasks.exe",
            "/Create",
            "/TN",
            YOUTUBE_SYNC_TASK_NAME,
        ]:
            assert argv[4] == "/XML"
            assert argv[6] == "/F"
            xml_path = Path(argv[5])
            captured["xml_path"] = xml_path
            captured["xml_existed_during_call"] = xml_path.is_file()
            captured["xml_bytes"] = xml_path.read_bytes()
            if isinstance(response, BaseException):
                raise response
            return response
        raise AssertionError(f"unexpected fake scheduler command: {argv!r}")

    return runner, calls, captured


@pytest.mark.parametrize(
    ("method_name", "local_time", "expected_clock"),
    (
        ("install", time(6, 0), "06:00"),
        ("update", time(23, 59), "23:59"),
    ),
)
def test_registration_builds_exact_utf16_interactive_task_xml(
    method_name, local_time, expected_clock
):
    runner, calls, captured = _registration_runner()
    adapter = TaskSchedulerAdapter(
        runner=runner,
        today=lambda: date(2026, 8, 19),
    )

    getattr(adapter, method_name)(local_time)

    assert captured["xml_existed_during_call"] is True
    xml_path = captured["xml_path"]
    assert isinstance(xml_path, Path)
    assert not xml_path.exists()
    xml_bytes = captured["xml_bytes"]
    assert isinstance(xml_bytes, bytes)
    assert xml_bytes.startswith((b"\xff\xfe", b"\xfe\xff"))
    assert "encoding='utf-16'" in xml_bytes.decode("utf-16").splitlines()[0]

    root = ElementTree.fromstring(xml_bytes)
    ns = {"task": TASK_NAMESPACE}
    assert root.tag == f"{{{TASK_NAMESPACE}}}Task"
    triggers = root.findall("./task:Triggers/task:CalendarTrigger", ns)
    assert len(triggers) == 1
    assert triggers[0].findtext("task:StartBoundary", namespaces=ns) == (
        f"2026-08-19T{expected_clock}:00+09:00"
    )
    assert triggers[0].findtext("task:Enabled", namespaces=ns) == "true"
    assert (
        triggers[0].findtext(
            "task:ScheduleByDay/task:DaysInterval", namespaces=ns
        )
        == "1"
    )

    principals = root.findall("./task:Principals/task:Principal", ns)
    assert len(principals) == 1
    assert principals[0].findtext("task:UserId", namespaces=ns) == CURRENT_USER_SID
    assert principals[0].findtext("task:LogonType", namespaces=ns) == (
        "InteractiveToken"
    )
    assert principals[0].findtext("task:RunLevel", namespaces=ns) == (
        "LeastPrivilege"
    )

    settings = root.findall("./task:Settings", ns)
    assert len(settings) == 1
    assert settings[0].findtext("task:MultipleInstancesPolicy", namespaces=ns) == (
        "Queue"
    )
    assert settings[0].findtext("task:StartWhenAvailable", namespaces=ns) == "true"
    assert settings[0].findtext("task:ExecutionTimeLimit", namespaces=ns) == "PT0S"

    actions = root.findall("./task:Actions/task:Exec", ns)
    assert len(actions) == 1
    assert actions[0].findtext("task:Command", namespaces=ns) == sys.executable
    assert actions[0].findtext("task:Arguments", namespaces=ns) == (
        "-m market_voice_forecast_ledger.cli youtube-sync worker --once"
    )

    exposed = xml_bytes.decode("utf-16").lower()
    assert "password" not in exposed
    assert "api_key" not in exposed
    assert "api-key" not in exposed
    assert "synthetic-key-token" not in exposed
    assert "environment" not in exposed

    assert len(calls) == 2
    _assert_safe_native_call(
        calls[0],
        ("whoami.exe", "/user", "/fo", "csv", "/nh"),
    )
    _assert_safe_native_call(
        calls[1],
        (
            "schtasks.exe",
            "/Create",
            "/TN",
            YOUTUBE_SYNC_TASK_NAME,
            "/XML",
            str(xml_path),
            "/F",
        ),
    )


@pytest.mark.parametrize(
    "whoami_output",
    (
        b"",
        b'"SYNTHETIC\\User","not-a-sid"\r\n',
        b'"SYNTHETIC\\User","S-1-5-21-1","extra"\r\n',
        b'"SYNTHETIC\\User","S-1-5-21-1\r\n',
        b'"SYNTHETIC\\User","S-1-5-21-1"\r\n"OTHER","S-1-5-21-2"\r\n',
        b'"","S-1-5-21-1"\r\n',
        b"\xff\xfe\x00",
    ),
)
def test_registration_rejects_malformed_current_user_identity_without_create(
    whoami_output,
):
    runner, calls, _captured = _registration_runner(whoami=whoami_output)
    adapter = TaskSchedulerAdapter(
        runner=runner,
        today=lambda: date(2026, 8, 19),
    )

    with pytest.raises(DomainError) as caught:
        adapter.install(time(6, 0))

    assert caught.value.code == "YOUTUBE_SCHEDULE_OPERATION_FAILED"
    assert "synthetic" not in str(caught.value).lower()
    assert len(calls) == 1
    _assert_safe_native_call(
        calls[0],
        ("whoami.exe", "/user", "/fo", "csv", "/nh"),
    )


@pytest.mark.parametrize(
    "create_response",
    (
        FakeCompletedProcess(
            returncode=9,
            stdout=b"native-private-output",
            stderr=b"native-private-error",
        ),
        OSError("native-private-path"),
    ),
)
def test_registration_failure_cleans_temp_xml_and_exposes_only_safe_code(
    create_response,
):
    runner, _calls, captured = _registration_runner(
        create_response=create_response
    )
    adapter = TaskSchedulerAdapter(
        runner=runner,
        today=lambda: date(2026, 8, 19),
    )

    with pytest.raises(DomainError) as caught:
        adapter.update(time(0, 0))

    assert caught.value.code == "YOUTUBE_SCHEDULE_OPERATION_FAILED"
    assert "native-private" not in str(caught.value)
    xml_path = captured["xml_path"]
    assert isinstance(xml_path, Path)
    assert not xml_path.exists()


@pytest.mark.parametrize(
    ("returncode", "expected"),
    ((0, True), (1, False)),
)
def test_remove_is_idempotent_and_uses_only_the_fixed_task(returncode, expected):
    runner = FakeSubprocessRunner(FakeCompletedProcess(returncode=returncode))

    assert TaskSchedulerAdapter(runner=runner).remove() is expected

    _assert_safe_native_call(
        runner.calls[0],
        ("schtasks.exe", "/Delete", "/TN", YOUTUBE_SYNC_TASK_NAME, "/F"),
    )


@pytest.mark.parametrize(
    ("operation", "response", "expected_code"),
    (
        (
            "status",
            FakeCompletedProcess(returncode=2, stderr=b"native-private-error"),
            "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE",
        ),
        (
            "request_start",
            FakeCompletedProcess(returncode=2, stderr=b"native-private-error"),
            "YOUTUBE_SYNC_UNAVAILABLE",
        ),
        (
            "remove",
            FakeCompletedProcess(returncode=2, stderr=b"native-private-error"),
            "YOUTUBE_SCHEDULE_OPERATION_FAILED",
        ),
    ),
)
def test_nonzero_native_results_expose_only_safe_codes(
    operation, response, expected_code
):
    adapter = TaskSchedulerAdapter(runner=FakeSubprocessRunner(response))

    with pytest.raises(DomainError) as caught:
        getattr(adapter, operation)()

    assert caught.value.code == expected_code
    assert "native-private" not in str(caught.value)

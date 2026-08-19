from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime, time, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

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
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(stdout=MANAGED_TASK_XML),
        FakeCompletedProcess(stdout=WHOAMI_CSV),
    )

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
    _assert_safe_native_call(runner.calls[1], WHOAMI_ARGV)


def test_request_start_runs_only_the_fixed_task_with_native_output_suppressed():
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(stdout=MANAGED_TASK_XML),
        FakeCompletedProcess(stdout=WHOAMI_CSV),
        FakeCompletedProcess(stdout=b"native-private-output", stderr=b"native-private-error")
    )

    TaskSchedulerAdapter(runner=runner).request_start()

    _assert_safe_native_call(
        runner.calls[2],
        ("schtasks.exe", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME),
    )
    assert [call[0] for call in runner.calls] == [
        QUERY_XML_ARGV,
        WHOAMI_ARGV,
        RUN_ARGV,
    ]


def test_missing_task_is_reported_without_parsing_native_output():
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-output",
            stderr=b"native-private-error",
        ),
        FakeCompletedProcess(stdout=OTHER_TASK_LISTING),
    )

    assert TaskSchedulerAdapter(runner=runner).status() == ScheduledTaskStatus.unavailable()
    assert [call[0] for call in runner.calls] == [QUERY_XML_ARGV, QUERY_LIST_ARGV]


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
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(stdout=xml),
        FakeCompletedProcess(stdout=WHOAMI_CSV),
    )

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


def _registration_runner(
    *,
    create_response: object = None,
    whoami: bytes = WHOAMI_CSV,
    existing_xml: bytes | None = None,
):
    runner = ReviewSchedulerRunner(
        query_response=(
            FakeCompletedProcess(returncode=1)
            if existing_xml is None
            else FakeCompletedProcess(stdout=existing_xml)
        ),
        listing_response=FakeCompletedProcess(stdout=OTHER_TASK_LISTING),
        whoami_response=FakeCompletedProcess(stdout=whoami),
        create_response=create_response or FakeCompletedProcess(),
    )
    return runner, runner.calls, runner.captured


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
    registration = root.findall("./task:RegistrationInfo", ns)
    assert len(registration) == 1
    assert registration[0].findtext("task:URI", namespaces=ns) == REVIEW_TASK_URI
    assert registration[0].findtext("task:Description", namespaces=ns) == (
        REVIEW_TASK_DESCRIPTION
    )
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
    assert tuple(child.tag.rsplit("}", 1)[-1] for child in settings[0]) == (
        "AllowStartOnDemand",
        "MultipleInstancesPolicy",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "AllowHardTerminate",
        "StartWhenAvailable",
        "RunOnlyIfNetworkAvailable",
        "WakeToRun",
        "Enabled",
        "Hidden",
        "DeleteExpiredTaskAfter",
        "IdleSettings",
        "ExecutionTimeLimit",
        "Priority",
        "RunOnlyIfIdle",
        "UseUnifiedSchedulingEngine",
        "DisallowStartOnRemoteAppSession",
    )
    for setting_name, expected_value in EXPECTED_SCALAR_SETTINGS.items():
        assert settings[0].findtext(
            f"task:{setting_name}", namespaces=ns
        ) == expected_value
    idle_settings = settings[0].findall("task:IdleSettings", ns)
    assert len(idle_settings) == 1
    assert tuple(child.tag.rsplit("}", 1)[-1] for child in idle_settings[0]) == (
        "Duration",
        "WaitTimeout",
        "StopOnIdleEnd",
        "RestartOnIdle",
    )
    for setting_name, expected_value in EXPECTED_IDLE_SETTINGS.items():
        assert idle_settings[0].findtext(
            f"task:{setting_name}", namespaces=ns
        ) == expected_value

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

    assert len(calls) == 4
    _assert_safe_native_call(
        calls[0],
        QUERY_XML_ARGV,
    )
    _assert_safe_native_call(
        calls[1],
        QUERY_LIST_ARGV,
    )
    _assert_safe_native_call(
        calls[2],
        WHOAMI_ARGV,
    )
    _assert_safe_native_call(
        calls[3],
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
    assert len(calls) == 3
    _assert_safe_native_call(
        calls[0],
        QUERY_XML_ARGV,
    )
    _assert_safe_native_call(calls[1], QUERY_LIST_ARGV)
    _assert_safe_native_call(calls[2], WHOAMI_ARGV)


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


def test_remove_attests_and_deletes_only_the_managed_fixed_task():
    runner = ReviewSchedulerRunner()

    assert TaskSchedulerAdapter(runner=runner).remove() is True

    assert [call[0] for call in runner.calls] == [
        QUERY_XML_ARGV,
        WHOAMI_ARGV,
        DELETE_ARGV,
    ]
    for call in runner.calls:
        _assert_safe_native_call(call, call[0])


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
    if operation == "status":
        runner = ReviewSchedulerRunner(query_response=response)
    elif operation == "request_start":
        runner = ReviewSchedulerRunner(run_response=response)
    else:
        runner = ReviewSchedulerRunner(delete_response=response)
    adapter = TaskSchedulerAdapter(runner=runner)

    with pytest.raises(DomainError) as caught:
        getattr(adapter, operation)()

    assert caught.value.code == expected_code
    assert "native-private" not in str(caught.value)


REVIEW_TASK_URI = f"\\{YOUTUBE_SYNC_TASK_NAME}"
REVIEW_TASK_DESCRIPTION = (
    "Managed by Market Voice Forecast Ledger for daily YouTube sync."
)
REVIEW_ACTION_ARGUMENTS = (
    "-m market_voice_forecast_ledger.cli youtube-sync worker --once"
)
XML_COMMAND = xml_escape(sys.executable)
MANAGED_TASK_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Task xmlns="{TASK_NAMESPACE}" version="1.4">
  <RegistrationInfo>
    <URI>{REVIEW_TASK_URI}</URI>
    <Description>{REVIEW_TASK_DESCRIPTION}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-19T06:00:00+09:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{CURRENT_USER_SID}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{XML_COMMAND}</Command>
      <Arguments>{REVIEW_ACTION_ARGUMENTS}</Arguments>
    </Exec>
  </Actions>
</Task>
""".encode()
ARBITRARY_TASK_XML = MANAGED_TASK_XML.replace(
    f"""  <RegistrationInfo>
    <URI>{REVIEW_TASK_URI}</URI>
    <Description>{REVIEW_TASK_DESCRIPTION}</Description>
  </RegistrationInfo>
""".encode(),
    b"",
).replace(
    f"<Command>{XML_COMMAND}</Command>".encode(),
    b"<Command>C:\\private\\arbitrary-task.exe</Command>",
)
QUERY_XML_ARGV = (
    "schtasks.exe",
    "/Query",
    "/TN",
    YOUTUBE_SYNC_TASK_NAME,
    "/XML",
)
QUERY_LIST_ARGV = ("schtasks.exe", "/Query", "/FO", "CSV", "/NH")
WHOAMI_ARGV = ("whoami.exe", "/user", "/fo", "csv", "/nh")
RUN_ARGV = ("schtasks.exe", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME)
DELETE_ARGV = (
    "schtasks.exe",
    "/Delete",
    "/TN",
    YOUTUBE_SYNC_TASK_NAME,
    "/F",
)
OTHER_TASK_LISTING = (
    '"\\Synthetic Other Task","PRIVATE LOCALIZED TIME",'
    '"PRIVATE LOCALIZED STATUS"\r\n'
).encode()


class ReviewSchedulerRunner:
    def __init__(
        self,
        *,
        query_response: object = FakeCompletedProcess(stdout=MANAGED_TASK_XML),
        listing_response: object = FakeCompletedProcess(stdout=OTHER_TASK_LISTING),
        whoami_response: object = FakeCompletedProcess(stdout=WHOAMI_CSV),
        create_response: object = FakeCompletedProcess(),
        run_response: object = FakeCompletedProcess(),
        delete_response: object = FakeCompletedProcess(),
    ) -> None:
        self.query_response = query_response
        self.listing_response = listing_response
        self.whoami_response = whoami_response
        self.create_response = create_response
        self.run_response = run_response
        self.delete_response = delete_response
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.captured: dict[str, object] = {}

    def __call__(self, argv: object, **kwargs: object) -> object:
        if type(argv) is not list or any(type(item) is not str for item in argv):
            raise AssertionError("scheduler argv must be a list of strings")
        command = tuple(argv)
        self.calls.append((command, dict(kwargs)))
        if command == QUERY_XML_ARGV:
            response = self.query_response
        elif command == QUERY_LIST_ARGV:
            response = self.listing_response
        elif command == WHOAMI_ARGV:
            response = self.whoami_response
        elif command == RUN_ARGV:
            response = self.run_response
        elif command == DELETE_ARGV:
            response = self.delete_response
        elif (
            len(command) == 7
            and command[:4]
            == ("schtasks.exe", "/Create", "/TN", YOUTUBE_SYNC_TASK_NAME)
            and command[4] == "/XML"
            and command[6] == "/F"
        ):
            xml_path = Path(command[5])
            self.captured["xml_path"] = xml_path
            self.captured["xml_existed_during_call"] = xml_path.is_file()
            self.captured["xml_bytes"] = xml_path.read_bytes()
            response = self.create_response
        else:
            raise AssertionError(f"unexpected fake scheduler command: {command!r}")
        if isinstance(response, BaseException):
            raise response
        return response


def _invoke_review_operation(adapter, operation):
    if operation in {"install", "update"}:
        return getattr(adapter, operation)(time(6, 0))
    return getattr(adapter, operation)()


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    (
        ("install", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("update", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("status", "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"),
        ("request_start", "YOUTUBE_SYNC_UNAVAILABLE"),
        ("remove", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
    ),
)
def test_i1_unmanaged_same_name_collision_cannot_be_observed_or_mutated(
    operation, expected_code
):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=ARBITRARY_TASK_XML)
    )
    adapter = TaskSchedulerAdapter(runner=runner, today=lambda: date(2026, 8, 19))
    caught = None

    try:
        _invoke_review_operation(adapter, operation)
    except DomainError as error:
        caught = error

    mutating_commands = [
        call[0]
        for call in runner.calls
        if len(call[0]) >= 2 and call[0][1] in {"/Create", "/Run", "/Delete"}
    ]
    assert mutating_commands == []
    assert caught is not None
    assert caught.code == expected_code
    assert "private" not in str(caught).lower()


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    (
        ("install", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("update", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("status", "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"),
        ("request_start", "YOUTUBE_SYNC_UNAVAILABLE"),
        ("remove", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
    ),
)
def test_i1_successful_query_without_xml_never_means_absent_or_mutates(
    operation, expected_code
):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=None)  # type: ignore[arg-type]
    )
    adapter = TaskSchedulerAdapter(runner=runner, today=lambda: date(2026, 8, 19))
    caught = None

    try:
        _invoke_review_operation(adapter, operation)
    except DomainError as error:
        caught = error

    mutating_commands = [
        call[0]
        for call in runner.calls
        if len(call[0]) >= 2 and call[0][1] in {"/Create", "/Run", "/Delete"}
    ]
    assert mutating_commands == []
    assert caught is not None
    assert caught.code == expected_code


@pytest.mark.parametrize(
    "mutated_xml",
    (
        MANAGED_TASK_XML.replace(REVIEW_TASK_URI.encode(), b"\\Foreign Task"),
        MANAGED_TASK_XML.replace(
            REVIEW_TASK_DESCRIPTION.encode(), b"Foreign task description"
        ),
        MANAGED_TASK_XML.replace(CURRENT_USER_SID.encode(), b"S-1-5-21-9-8-7-6"),
        MANAGED_TASK_XML.replace(b"InteractiveToken", b"Password"),
        MANAGED_TASK_XML.replace(b"LeastPrivilege", b"HighestAvailable"),
        MANAGED_TASK_XML.replace(b"<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", b"<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>"),
        MANAGED_TASK_XML.replace(b"<MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>", b"<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>"),
        MANAGED_TASK_XML.replace(b"<StartWhenAvailable>true</StartWhenAvailable>", b"<StartWhenAvailable>false</StartWhenAvailable>"),
        MANAGED_TASK_XML.replace(
            b"</CalendarTrigger>",
            b"<Repetition><Interval>PT1M</Interval></Repetition></CalendarTrigger>",
        ),
        MANAGED_TASK_XML.replace(
            b"</Triggers>",
            b"<EventTrigger><Enabled>true</Enabled></EventTrigger></Triggers>",
        ),
        MANAGED_TASK_XML.replace(
            b"</Triggers>",
            b"<CalendarTrigger><StartBoundary>2026-08-19T07:00:00+09:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>",
        ),
        MANAGED_TASK_XML.replace(
            b"</Actions>",
            b"<Exec><Command>C:\\private\\extra.exe</Command><Arguments>private</Arguments></Exec></Actions>",
        ),
        MANAGED_TASK_XML.replace(
            f"<Command>{XML_COMMAND}</Command>".encode(),
            b"<Command>C:\\private\\arbitrary-task.exe</Command>",
        ),
        MANAGED_TASK_XML.replace(
            REVIEW_ACTION_ARGUMENTS.encode(),
            b"-m private.module --secret private-token",
        ),
    ),
    ids=(
        "ownership-uri",
        "ownership-description",
        "current-sid",
        "interactive-token",
        "least-privilege",
        "unlimited-runtime",
        "queue",
        "catch-up",
        "repetition",
        "extra-trigger",
        "second-daily-trigger",
        "extra-action",
        "command",
        "arguments",
    ),
)
def test_i1_full_managed_definition_attestation_rejects_mutations(mutated_xml):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=mutated_xml)
    )

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).status()

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
    assert "private" not in str(caught.value).lower()


@pytest.mark.parametrize("operation", ("install", "update"))
def test_i1_registration_replaces_only_an_attested_managed_task(operation):
    runner, calls, captured = _registration_runner(existing_xml=MANAGED_TASK_XML)
    adapter = TaskSchedulerAdapter(runner=runner, today=lambda: date(2026, 8, 19))

    getattr(adapter, operation)(time(7, 30))

    assert [call[0] for call in calls] == [
        QUERY_XML_ARGV,
        WHOAMI_ARGV,
        (
            "schtasks.exe",
            "/Create",
            "/TN",
            YOUTUBE_SYNC_TASK_NAME,
            "/XML",
            str(captured["xml_path"]),
            "/F",
        ),
    ]


def test_i2_query_exit_one_requires_structured_absence_proof():
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-output",
            stderr=b"native-private-error",
        ),
        listing_response=FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-listing",
            stderr=b"native-private-listing-error",
        ),
    )

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).status()

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
    assert "private" not in str(caught.value).lower()
    assert [call[0] for call in runner.calls] == [QUERY_XML_ARGV, QUERY_LIST_ARGV]


TARGET_TASK_LISTING = (
    f'"\\{YOUTUBE_SYNC_TASK_NAME}","PRIVATE LOCALIZED TIME",'
    '"PRIVATE LOCALIZED STATUS"\r\n'
).encode()


@pytest.mark.parametrize(
    "listing_output",
    (
        TARGET_TASK_LISTING,
        b'"\\Other","PRIVATE","STATUS\r\n',
        OTHER_TASK_LISTING + OTHER_TASK_LISTING,
        b'"\\Other","PRIVATE"\r\n',
        b"",
    ),
    ids=(
        "target-still-listed",
        "malformed-quote",
        "duplicate-task-row",
        "wrong-column-count",
        "empty-listing",
    ),
)
def test_i2_target_or_malformed_listing_never_proves_absence(listing_output):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-target-query",
            stderr=b"native-private-target-error",
        ),
        listing_response=FakeCompletedProcess(
            stdout=listing_output,
            stderr=b"native-private-list-error",
        ),
    )

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).status()

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
    assert "private" not in str(caught.value).lower()
    assert [call[0] for call in runner.calls] == [QUERY_XML_ARGV, QUERY_LIST_ARGV]


@pytest.mark.parametrize("operation", ("status", "remove"))
def test_i2_structured_listing_is_required_to_prove_predelete_absence(operation):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(returncode=1),
        listing_response=FakeCompletedProcess(stdout=OTHER_TASK_LISTING),
    )
    adapter = TaskSchedulerAdapter(runner=runner)

    result = getattr(adapter, operation)()

    expected = ScheduledTaskStatus.unavailable() if operation == "status" else False
    assert result == expected
    assert [call[0] for call in runner.calls] == [QUERY_XML_ARGV, QUERY_LIST_ARGV]


def test_i2_managed_delete_nonzero_is_a_safe_error_not_absence():
    runner = ReviewSchedulerRunner(
        delete_response=FakeCompletedProcess(
            returncode=1,
            stdout=b"native-private-output",
            stderr=b"native-private-error",
        )
    )

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).remove()

    assert caught.value.code == "YOUTUBE_SCHEDULE_OPERATION_FAILED"
    assert "private" not in str(caught.value).lower()
    assert [call[0] for call in runner.calls] == [
        QUERY_XML_ARGV,
        WHOAMI_ARGV,
        DELETE_ARGV,
    ]


EXPECTED_SCALAR_SETTINGS = {
    "AllowStartOnDemand": "true",
    "MultipleInstancesPolicy": "Queue",
    "DisallowStartIfOnBatteries": "true",
    "StopIfGoingOnBatteries": "true",
    "AllowHardTerminate": "true",
    "StartWhenAvailable": "true",
    "RunOnlyIfNetworkAvailable": "false",
    "WakeToRun": "false",
    "Enabled": "true",
    "Hidden": "false",
    "DeleteExpiredTaskAfter": "PT0S",
    "ExecutionTimeLimit": "PT0S",
    "Priority": "7",
    "RunOnlyIfIdle": "false",
    "UseUnifiedSchedulingEngine": "false",
    "DisallowStartOnRemoteAppSession": "false",
}
EXPECTED_IDLE_SETTINGS = {
    "Duration": "PT10M",
    "WaitTimeout": "PT1H",
    "StopOnIdleEnd": "true",
    "RestartOnIdle": "false",
}
REQUIRED_SETTINGS = (
    ("MultipleInstancesPolicy", "Queue"),
    ("StartWhenAvailable", "true"),
    ("ExecutionTimeLimit", "PT0S"),
)


def _task_xml_with_settings(*setting_entries):
    root = ElementTree.fromstring(MANAGED_TASK_XML)
    namespace = f"{{{TASK_NAMESPACE}}}"
    settings = root.find(f"./{namespace}Settings")
    assert settings is not None
    settings.clear()
    for setting_name, setting_value in setting_entries:
        setting = ElementTree.SubElement(settings, f"{namespace}{setting_name}")
        if type(setting_value) is tuple:
            for child_name, child_value in setting_value:
                ElementTree.SubElement(
                    setting, f"{namespace}{child_name}"
                ).text = child_value
        else:
            setting.text = setting_value
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _task_xml_with_added_settings(*setting_entries):
    return _task_xml_with_settings(*REQUIRED_SETTINGS, *setting_entries)


SETTINGS_MUTATIONS = (
    _task_xml_with_added_settings(("Enabled", "false")),
    _task_xml_with_added_settings(("RunOnlyIfNetworkAvailable", "true")),
    _task_xml_with_added_settings(("DisallowStartIfOnBatteries", "false")),
    _task_xml_with_added_settings(("StopIfGoingOnBatteries", "false")),
    _task_xml_with_added_settings(("AllowHardTerminate", "false")),
    _task_xml_with_added_settings(("AllowStartOnDemand", "false")),
    _task_xml_with_added_settings(("Hidden", "true")),
    _task_xml_with_added_settings(("Priority", "8")),
    _task_xml_with_added_settings(("RunOnlyIfIdle", "true")),
    _task_xml_with_added_settings(("UseUnifiedSchedulingEngine", "true")),
    _task_xml_with_added_settings(
        ("DisallowStartOnRemoteAppSession", "true")
    ),
    _task_xml_with_added_settings(("WakeToRun", "true")),
    _task_xml_with_added_settings(("DeleteExpiredTaskAfter", "PT1H")),
    _task_xml_with_added_settings(
        (
            "IdleSettings",
            (
                ("Duration", "PT5M"),
                ("WaitTimeout", "PT1H"),
                ("StopOnIdleEnd", "true"),
                ("RestartOnIdle", "false"),
            ),
        )
    ),
    _task_xml_with_added_settings(
        (
            "IdleSettings",
            (
                ("Duration", "PT10M"),
                ("WaitTimeout", "PT1H"),
                ("StopOnIdleEnd", "true"),
                ("RestartOnIdle", "false"),
                ("UnknownIdleSetting", "true"),
            ),
        )
    ),
    _task_xml_with_added_settings(
        ("RestartOnFailure", (("Interval", "PT1M"), ("Count", "1")))
    ),
    _task_xml_with_added_settings(("NetworkProfileName", "private-profile")),
    _task_xml_with_added_settings(
        ("NetworkSettings", (("Name", "private-network"),))
    ),
    _task_xml_with_added_settings(("UnknownSetting", "true")),
    _task_xml_with_added_settings(("Enabled", "true"), ("Enabled", "true")),
    _task_xml_with_added_settings(("Enabled", "True")),
    _task_xml_with_added_settings(("Priority", "07")),
)


@pytest.mark.parametrize(
    "mutated_xml",
    SETTINGS_MUTATIONS,
    ids=(
        "disabled",
        "network-required",
        "battery-start",
        "battery-stop",
        "hard-terminate",
        "on-demand",
        "hidden",
        "priority",
        "idle-only",
        "unified-engine",
        "remote-app-session",
        "wake-to-run",
        "delete-expired",
        "idle-duration",
        "unknown-idle-setting",
        "restart-on-failure",
        "network-profile",
        "network-settings",
        "unknown-setting",
        "duplicate-setting",
        "noncanonical-boolean",
        "noncanonical-priority",
    ),
)
def test_i3_settings_mutations_fail_managed_attestation(mutated_xml):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=mutated_xml)
    )

    with pytest.raises(DomainError) as caught:
        TaskSchedulerAdapter(runner=runner).status()

    assert caught.value.code == "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"
    assert "private" not in str(caught.value).lower()


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    (
        ("install", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("update", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
        ("status", "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE"),
        ("request_start", "YOUTUBE_SYNC_UNAVAILABLE"),
        ("remove", "YOUTUBE_SCHEDULE_OPERATION_FAILED"),
    ),
)
def test_i3_changed_settings_block_status_run_delete_and_create(
    operation, expected_code
):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=SETTINGS_MUTATIONS[0])
    )
    adapter = TaskSchedulerAdapter(runner=runner, today=lambda: date(2026, 8, 19))
    caught = None

    try:
        _invoke_review_operation(adapter, operation)
    except DomainError as error:
        caught = error

    mutating_commands = [
        call[0]
        for call in runner.calls
        if len(call[0]) >= 2 and call[0][1] in {"/Create", "/Run", "/Delete"}
    ]
    assert mutating_commands == []
    assert caught is not None
    assert caught.code == expected_code


FULL_DEFAULT_SETTINGS_ARBITRARY_ORDER = (
    ("Priority", "7"),
    ("Enabled", "true"),
    ("ExecutionTimeLimit", "PT0S"),
    ("WakeToRun", "false"),
    (
        "IdleSettings",
        (
            ("RestartOnIdle", "false"),
            ("StopOnIdleEnd", "true"),
            ("WaitTimeout", "PT1H"),
            ("Duration", "PT10M"),
        ),
    ),
    ("MultipleInstancesPolicy", "Queue"),
    ("RunOnlyIfIdle", "false"),
    ("DisallowStartIfOnBatteries", "true"),
    ("StartWhenAvailable", "true"),
    ("AllowHardTerminate", "true"),
    ("Hidden", "false"),
    ("UseUnifiedSchedulingEngine", "false"),
    ("AllowStartOnDemand", "true"),
    ("StopIfGoingOnBatteries", "true"),
    ("DeleteExpiredTaskAfter", "PT0S"),
    ("DisallowStartOnRemoteAppSession", "false"),
    ("RunOnlyIfNetworkAvailable", "false"),
)


@pytest.mark.parametrize(
    "xml",
    (
        MANAGED_TASK_XML,
        _task_xml_with_added_settings(
            ("IdleSettings", (("Duration", "PT10M"),))
        ),
        _task_xml_with_settings(*FULL_DEFAULT_SETTINGS_ARBITRARY_ORDER),
    ),
    ids=(
        "documented-defaults-omitted",
        "idle-defaults-partly-omitted",
        "documented-defaults-explicit",
    ),
)
def test_i3_documented_settings_defaults_normalize_to_managed_task(xml):
    runner = ReviewSchedulerRunner(
        query_response=FakeCompletedProcess(stdout=xml)
    )

    status = TaskSchedulerAdapter(runner=runner).status()

    assert status == ScheduledTaskStatus(True, "06:00", True, "Queue")

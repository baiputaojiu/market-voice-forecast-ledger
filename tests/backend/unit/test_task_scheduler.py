from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone

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
        ("schtasks", "/Query", "/TN", YOUTUBE_SYNC_TASK_NAME, "/XML"),
    )


def test_request_start_runs_only_the_fixed_task_with_native_output_suppressed():
    runner = FakeSubprocessRunner(
        FakeCompletedProcess(stdout=b"native-private-output", stderr=b"native-private-error")
    )

    TaskSchedulerAdapter(runner=runner).request_start()

    _assert_safe_native_call(
        runner.calls[0],
        ("schtasks", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME),
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

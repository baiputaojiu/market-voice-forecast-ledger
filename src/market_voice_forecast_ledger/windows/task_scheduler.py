from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

from market_voice_forecast_ledger.domain.errors import DomainError


YOUTUBE_SYNC_TASK_NAME = "Market Voice Forecast Ledger - YouTube Sync"
SCHEDULE_QUERY_TIMEOUT_SECONDS = 30
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_MAX_TASK_XML_BYTES = 262_144
_LOCAL_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_START_BOUNDARY = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T((?:[01]\d|2[0-3]):[0-5]\d):00\+09:00$"
)
_JST = timezone(timedelta(hours=9))


class TaskWakeAdapter(Protocol):
    def request_start(self) -> None: ...


class TaskScheduleReader(Protocol):
    def status(self) -> "ScheduledTaskStatus": ...


@dataclass(frozen=True, slots=True)
class ScheduledTaskStatus:
    installed: bool
    local_time: str | None
    start_when_available: bool
    multiple_instances: str | None

    def __post_init__(self) -> None:
        valid = (
            type(self.installed) is bool
            and type(self.start_when_available) is bool
            and (
                (
                    self.installed
                    and type(self.local_time) is str
                    and _LOCAL_TIME.fullmatch(self.local_time) is not None
                    and self.start_when_available
                    and self.multiple_instances == "Queue"
                )
                or (
                    not self.installed
                    and self.local_time is None
                    and not self.start_when_available
                    and self.multiple_instances is None
                )
            )
        )
        if not valid:
            raise DomainError(
                "YOUTUBE_SCHEDULE_STATUS_INVALID",
                "YouTube schedule status is invalid",
            )

    @classmethod
    def unavailable(cls) -> "ScheduledTaskStatus":
        return cls(False, None, False, None)

    def jst_day(self, now: datetime) -> date:
        self._require_exact_utc(now)
        return now.astimezone(_JST).date()

    def is_due(self, now: datetime) -> bool:
        day = self.jst_day(now)
        if not self.installed:
            return False
        assert self.local_time is not None
        local_time = time.fromisoformat(self.local_time)
        due_at = datetime.combine(day, local_time, tzinfo=_JST)
        return now >= due_at.astimezone(timezone.utc)

    @staticmethod
    def _require_exact_utc(value: object) -> None:
        if type(value) is not datetime or value.tzinfo is not timezone.utc:
            raise DomainError(
                "YOUTUBE_SCHEDULE_STATUS_INVALID",
                "YouTube schedule status requires an exact UTC datetime",
            )


class TaskSchedulerAdapter(TaskWakeAdapter, TaskScheduleReader):
    def __init__(
        self,
        *,
        runner: Callable[..., object] | None = None,
    ) -> None:
        self._runner = runner or subprocess.run

    def status(self) -> ScheduledTaskStatus:
        try:
            completed = self._run(
                ("schtasks", "/Query", "/TN", YOUTUBE_SYNC_TASK_NAME, "/XML")
            )
            returncode = getattr(completed, "returncode", None)
        except Exception:
            self._raise_status_unavailable()
        if returncode == 1:
            return ScheduledTaskStatus.unavailable()
        if returncode != 0:
            self._raise_status_unavailable()
        try:
            return _parse_task_status(getattr(completed, "stdout", None))
        except (DomainError, ElementTree.ParseError, TypeError, ValueError):
            self._raise_status_unavailable()

    def request_start(self) -> None:
        try:
            completed = self._run(
                ("schtasks", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME)
            )
            returncode = getattr(completed, "returncode", None)
        except Exception:
            self._raise_sync_unavailable()
        if returncode != 0:
            self._raise_sync_unavailable()

    def _run(self, argv: tuple[str, ...]):
        return self._runner(
            list(argv),
            shell=False,
            timeout=SCHEDULE_QUERY_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    @staticmethod
    def _raise_status_unavailable() -> None:
        raise DomainError(
            "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE",
            "YouTube schedule status is unavailable",
        ) from None

    @staticmethod
    def _raise_sync_unavailable() -> None:
        raise DomainError(
            "YOUTUBE_SYNC_UNAVAILABLE",
            "YouTube sync could not be started",
        ) from None


def _parse_task_status(xml_bytes: object) -> ScheduledTaskStatus:
    if (
        type(xml_bytes) is not bytes
        or not xml_bytes
        or len(xml_bytes) > _MAX_TASK_XML_BYTES
        or b"<!DOCTYPE" in xml_bytes.upper()
        or b"<!ENTITY" in xml_bytes.upper()
    ):
        raise ValueError("task XML is invalid")
    root = ElementTree.fromstring(xml_bytes)
    namespace = f"{{{_TASK_NAMESPACE}}}"
    if root.tag != f"{namespace}Task":
        raise ValueError("task XML root is invalid")

    trigger_containers = root.findall(f"./{namespace}Triggers")
    if (
        len(trigger_containers) != 1
        or tuple(child.tag for child in trigger_containers[0])
        != (f"{namespace}CalendarTrigger",)
    ):
        raise ValueError("daily trigger is invalid")
    trigger = trigger_containers[0][0]
    boundary = _one_text(trigger, f"{namespace}StartBoundary")
    match = _START_BOUNDARY.fullmatch(boundary)
    if match is None:
        raise ValueError("start boundary is invalid")
    datetime.fromisoformat(boundary)
    if _one_text(trigger, f"{namespace}Enabled") != "true":
        raise ValueError("daily trigger is disabled")
    interval = _one_text(
        trigger,
        f"{namespace}ScheduleByDay/{namespace}DaysInterval",
    )
    if interval != "1":
        raise ValueError("daily interval is invalid")

    principal_containers = root.findall(f"./{namespace}Principals")
    if (
        len(principal_containers) != 1
        or tuple(child.tag for child in principal_containers[0])
        != (f"{namespace}Principal",)
    ):
        raise ValueError("task principal is invalid")
    logon_types = principal_containers[0].findall(
        f"./{namespace}Principal/{namespace}LogonType"
    )
    if len(logon_types) != 1 or logon_types[0].text != "InteractiveToken":
        raise ValueError("task principal is invalid")
    start_when_available = _one_text(
        root, f"./{namespace}Settings/{namespace}StartWhenAvailable"
    )
    multiple_instances = _one_text(
        root, f"./{namespace}Settings/{namespace}MultipleInstancesPolicy"
    )
    if start_when_available != "true" or multiple_instances != "Queue":
        raise ValueError("task settings are invalid")
    return ScheduledTaskStatus(True, match.group(2), True, "Queue")


def _one_text(element: ElementTree.Element, path: str) -> str:
    matches = element.findall(path)
    if len(matches) != 1 or type(matches[0].text) is not str:
        raise ValueError("task XML value is invalid")
    return matches[0].text

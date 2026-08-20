from __future__ import annotations

import csv
import io
import locale
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
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
_CURRENT_USER_SID = re.compile(
    r"^S-1-(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*)){1,15}$"
)
_JST = timezone(timedelta(hours=9))
_TASK_ACTION_ARGUMENTS = (
    "-m market_voice_forecast_ledger.cli youtube-sync worker --once"
)
_TASK_URI = f"\\{YOUTUBE_SYNC_TASK_NAME}"
_TASK_DESCRIPTION = (
    "Managed by Market Voice Forecast Ledger for daily YouTube sync."
)
_TASK_SETTINGS = (
    ("AllowStartOnDemand", "true"),
    ("MultipleInstancesPolicy", "Queue"),
    ("DisallowStartIfOnBatteries", "true"),
    ("StopIfGoingOnBatteries", "true"),
    ("AllowHardTerminate", "true"),
    ("StartWhenAvailable", "true"),
    ("RunOnlyIfNetworkAvailable", "false"),
    ("WakeToRun", "false"),
    ("Enabled", "true"),
    ("Hidden", "false"),
    ("DeleteExpiredTaskAfter", "PT0S"),
    ("IdleSettings", None),
    ("ExecutionTimeLimit", "PT0S"),
    ("Priority", "7"),
    ("RunOnlyIfIdle", "false"),
    ("UseUnifiedSchedulingEngine", "false"),
    ("DisallowStartOnRemoteAppSession", "false"),
)
_TASK_IDLE_SETTINGS = (
    ("Duration", "PT10M"),
    ("WaitTimeout", "PT1H"),
    ("StopOnIdleEnd", "true"),
    ("RestartOnIdle", "false"),
)
_REQUIRED_TASK_SETTINGS = frozenset(
    {"MultipleInstancesPolicy", "StartWhenAvailable", "ExecutionTimeLimit"}
)
_MAX_WHOAMI_BYTES = 8_192
_MAX_TASK_LIST_BYTES = 1_048_576


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
        today: Callable[[], date] | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._today = today or _jst_today

    def install(self, local_time: time) -> None:
        self._register(local_time)

    def update(self, local_time: time) -> None:
        self._register(local_time)

    def status(self) -> ScheduledTaskStatus:
        try:
            status = self._managed_status()
        except Exception:
            self._raise_status_unavailable()
        return status or ScheduledTaskStatus.unavailable()

    def request_start(self) -> None:
        try:
            if self._managed_status() is None:
                raise ValueError("managed task is unavailable")
            completed = self._run(
                ("schtasks.exe", "/Run", "/TN", YOUTUBE_SYNC_TASK_NAME)
            )
            returncode = getattr(completed, "returncode", None)
            if returncode != 0:
                raise ValueError("managed task could not be started")
        except Exception:
            self._raise_sync_unavailable()

    def remove(self) -> bool:
        try:
            if self._managed_status() is None:
                return False
            completed = self._run(
                (
                    "schtasks.exe",
                    "/Delete",
                    "/TN",
                    YOUTUBE_SYNC_TASK_NAME,
                    "/F",
                )
            )
            returncode = getattr(completed, "returncode", None)
            if returncode != 0:
                raise ValueError("managed task could not be removed")
            return True
        except Exception:
            self._raise_operation_failed()

    def _register(self, local_time: time) -> None:
        try:
            canonical_time = _require_schedule_time(local_time)
            day = self._today()
            if type(day) is not date:
                raise ValueError("task date is invalid")
            existing_xml = self._query_task_xml()
            sid = self._current_user_sid()
            if existing_xml is not None:
                _parse_task_status(existing_xml, sid)
            xml_bytes = _build_task_xml(day, canonical_time, sid)
            with tempfile.TemporaryDirectory(prefix="mvfl-youtube-sync-") as temp_dir:
                xml_path = Path(temp_dir) / "youtube-sync-task.xml"
                xml_path.write_bytes(xml_bytes)
                completed = self._run(
                    (
                        "schtasks.exe",
                        "/Create",
                        "/TN",
                        YOUTUBE_SYNC_TASK_NAME,
                        "/XML",
                        str(xml_path),
                        "/F",
                    )
                )
                if getattr(completed, "returncode", None) != 0:
                    raise ValueError("task registration failed")
        except Exception:
            self._raise_operation_failed()

    def _managed_status(self) -> ScheduledTaskStatus | None:
        xml_bytes = self._query_task_xml()
        if xml_bytes is None:
            return None
        return _parse_task_status(xml_bytes, self._current_user_sid())

    def _query_task_xml(self) -> bytes | None:
        completed = self._run(
            (
                "schtasks.exe",
                "/Query",
                "/TN",
                YOUTUBE_SYNC_TASK_NAME,
                "/XML",
            )
        )
        returncode = getattr(completed, "returncode", None)
        if returncode == 0:
            xml_bytes = getattr(completed, "stdout", None)
            if type(xml_bytes) is not bytes:
                raise ValueError("task XML is unavailable")
            return xml_bytes
        if returncode != 1:
            raise ValueError("task query failed")
        listing = self._run(("schtasks.exe", "/Query", "/FO", "CSV", "/NH"))
        if getattr(listing, "returncode", None) != 0 or not _proves_task_absent(
            getattr(listing, "stdout", None)
        ):
            raise ValueError("task absence is unproven")
        return None

    def _current_user_sid(self) -> str:
        identity = self._run(("whoami.exe", "/user", "/fo", "csv", "/nh"))
        if getattr(identity, "returncode", None) != 0:
            raise ValueError("task identity is unavailable")
        return _parse_current_user_sid(getattr(identity, "stdout", None))

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

    @staticmethod
    def _raise_operation_failed() -> None:
        raise DomainError(
            "YOUTUBE_SCHEDULE_OPERATION_FAILED",
            "YouTube schedule operation failed",
        ) from None


def _require_schedule_time(value: object) -> time:
    if (
        type(value) is not time
        or value.tzinfo is not None
        or value.second != 0
        or value.microsecond != 0
        or value.fold != 0
    ):
        raise ValueError("task time is invalid")
    return value


def _parse_current_user_sid(csv_bytes: object) -> str:
    if (
        type(csv_bytes) is not bytes
        or not csv_bytes
        or len(csv_bytes) > _MAX_WHOAMI_BYTES
        or b"\x00" in csv_bytes
    ):
        raise ValueError("task identity is invalid")
    text = csv_bytes.decode(locale.getpreferredencoding(False), errors="strict")
    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ValueError("task identity is invalid")
    user_name, sid = rows[0]
    if (
        not user_name
        or user_name != user_name.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in user_name)
        or _CURRENT_USER_SID.fullmatch(sid) is None
    ):
        raise ValueError("task identity is invalid")
    return sid


def _proves_task_absent(csv_bytes: object) -> bool:
    if (
        type(csv_bytes) is not bytes
        or not csv_bytes
        or len(csv_bytes) > _MAX_TASK_LIST_BYTES
        or b"\x00" in csv_bytes
    ):
        raise ValueError("task listing is invalid")
    text = csv_bytes.decode(locale.getpreferredencoding(False), errors="strict")
    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    if not rows:
        raise ValueError("task listing is invalid")
    seen: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise ValueError("task listing is invalid")
        task_path = row[0]
        if (
            not task_path.startswith("\\")
            or task_path == "\\"
            or any(
                not value
                or value != value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in row
            )
        ):
            raise ValueError("task listing is invalid")
        normalized = task_path.casefold()
        seen.add(normalized)
    return _TASK_URI.casefold() not in seen


def _build_task_xml(day: date, local_time: time, sid: str) -> bytes:
    namespace = f"{{{_TASK_NAMESPACE}}}"
    ElementTree.register_namespace("", _TASK_NAMESPACE)
    task = ElementTree.Element(f"{namespace}Task", {"version": "1.4"})

    registration = ElementTree.SubElement(task, f"{namespace}RegistrationInfo")
    ElementTree.SubElement(registration, f"{namespace}URI").text = _TASK_URI
    ElementTree.SubElement(registration, f"{namespace}Description").text = (
        _TASK_DESCRIPTION
    )

    triggers = ElementTree.SubElement(task, f"{namespace}Triggers")
    calendar = ElementTree.SubElement(triggers, f"{namespace}CalendarTrigger")
    ElementTree.SubElement(calendar, f"{namespace}StartBoundary").text = (
        f"{day.isoformat()}T{local_time.strftime('%H:%M')}:00+09:00"
    )
    ElementTree.SubElement(calendar, f"{namespace}Enabled").text = "true"
    schedule_by_day = ElementTree.SubElement(
        calendar, f"{namespace}ScheduleByDay"
    )
    ElementTree.SubElement(
        schedule_by_day, f"{namespace}DaysInterval"
    ).text = "1"

    principals = ElementTree.SubElement(task, f"{namespace}Principals")
    principal = ElementTree.SubElement(
        principals, f"{namespace}Principal", {"id": "Author"}
    )
    ElementTree.SubElement(principal, f"{namespace}UserId").text = sid
    ElementTree.SubElement(principal, f"{namespace}LogonType").text = (
        "InteractiveToken"
    )
    ElementTree.SubElement(principal, f"{namespace}RunLevel").text = (
        "LeastPrivilege"
    )

    settings = ElementTree.SubElement(task, f"{namespace}Settings")
    for setting_name, setting_value in _TASK_SETTINGS:
        setting = ElementTree.SubElement(
            settings, f"{namespace}{setting_name}"
        )
        if setting_name == "IdleSettings":
            for idle_name, idle_value in _TASK_IDLE_SETTINGS:
                ElementTree.SubElement(
                    setting, f"{namespace}{idle_name}"
                ).text = idle_value
        else:
            assert setting_value is not None
            setting.text = setting_value

    actions = ElementTree.SubElement(
        task, f"{namespace}Actions", {"Context": "Author"}
    )
    execute = ElementTree.SubElement(actions, f"{namespace}Exec")
    ElementTree.SubElement(execute, f"{namespace}Command").text = sys.executable
    ElementTree.SubElement(execute, f"{namespace}Arguments").text = (
        _TASK_ACTION_ARGUMENTS
    )
    return ElementTree.tostring(
        task,
        encoding="utf-16",
        xml_declaration=True,
    )


def _jst_today() -> date:
    return datetime.now(_JST).date()


def _parse_task_status(
    xml_bytes: object,
    expected_sid: object,
) -> ScheduledTaskStatus:
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
    if root.tag != f"{namespace}Task" or root.attrib.get("version") != "1.4":
        raise ValueError("task XML root is invalid")

    registrations = root.findall(f"./{namespace}RegistrationInfo")
    if len(registrations) != 1:
        raise ValueError("task ownership is invalid")
    if (
        _one_text(registrations[0], f"{namespace}URI") != _TASK_URI
        or _one_text(registrations[0], f"{namespace}Description")
        != _TASK_DESCRIPTION
    ):
        raise ValueError("task ownership is invalid")

    trigger_containers = root.findall(f"./{namespace}Triggers")
    if (
        len(trigger_containers) != 1
        or tuple(child.tag for child in trigger_containers[0])
        != (f"{namespace}CalendarTrigger",)
    ):
        raise ValueError("daily trigger is invalid")
    trigger = trigger_containers[0][0]
    if {
        child.tag for child in trigger
    } != {
        f"{namespace}StartBoundary",
        f"{namespace}Enabled",
        f"{namespace}ScheduleByDay",
    } or len(trigger) != 3:
        raise ValueError("daily trigger is invalid")
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
    daily_schedules = trigger.findall(f"./{namespace}ScheduleByDay")
    if (
        interval != "1"
        or len(daily_schedules) != 1
        or tuple(child.tag for child in daily_schedules[0])
        != (f"{namespace}DaysInterval",)
    ):
        raise ValueError("daily interval is invalid")

    principal_containers = root.findall(f"./{namespace}Principals")
    if (
        len(principal_containers) != 1
        or tuple(child.tag for child in principal_containers[0])
        != (f"{namespace}Principal",)
    ):
        raise ValueError("task principal is invalid")
    principal = principal_containers[0][0]
    if (
        principal.attrib != {"id": "Author"}
        or tuple(child.tag for child in principal)
        != (
            f"{namespace}UserId",
            f"{namespace}LogonType",
            f"{namespace}RunLevel",
        )
        or type(expected_sid) is not str
        or _CURRENT_USER_SID.fullmatch(expected_sid) is None
        or _one_text(principal, f"{namespace}UserId") != expected_sid
        or _one_text(principal, f"{namespace}LogonType") != "InteractiveToken"
        or _one_text(principal, f"{namespace}RunLevel") != "LeastPrivilege"
    ):
        raise ValueError("task principal is invalid")
    settings_containers = root.findall(f"./{namespace}Settings")
    if len(settings_containers) != 1:
        raise ValueError("task settings are invalid")
    _validate_task_settings(settings_containers[0], namespace)

    action_containers = root.findall(f"./{namespace}Actions")
    if (
        len(action_containers) != 1
        or action_containers[0].attrib != {"Context": "Author"}
        or tuple(child.tag for child in action_containers[0])
        != (f"{namespace}Exec",)
    ):
        raise ValueError("task action is invalid")
    action = action_containers[0][0]
    if (
        tuple(child.tag for child in action)
        != (f"{namespace}Command", f"{namespace}Arguments")
        or _one_text(action, f"{namespace}Command") != sys.executable
        or _one_text(action, f"{namespace}Arguments")
        != _TASK_ACTION_ARGUMENTS
    ):
        raise ValueError("task action is invalid")
    return ScheduledTaskStatus(True, match.group(2), True, "Queue")


def _validate_task_settings(
    settings: ElementTree.Element,
    namespace: str,
) -> None:
    if settings.attrib:
        raise ValueError("task settings are invalid")
    expected = {
        f"{namespace}{setting_name}": (setting_name, setting_value)
        for setting_name, setting_value in _TASK_SETTINGS
    }
    seen: set[str] = set()
    for setting in settings:
        definition = expected.get(setting.tag)
        if definition is None:
            raise ValueError("task settings are invalid")
        setting_name, setting_value = definition
        if setting_name in seen:
            raise ValueError("task settings are invalid")
        seen.add(setting_name)
        if setting_name == "IdleSettings":
            _validate_idle_settings(setting, namespace)
        elif (
            setting.attrib
            or len(setting) != 0
            or setting.text != setting_value
        ):
            raise ValueError("task settings are invalid")
    if not _REQUIRED_TASK_SETTINGS.issubset(seen):
        raise ValueError("task settings are invalid")


def _validate_idle_settings(
    idle_settings: ElementTree.Element,
    namespace: str,
) -> None:
    if idle_settings.attrib:
        raise ValueError("task settings are invalid")
    expected = {
        f"{namespace}{setting_name}": (setting_name, setting_value)
        for setting_name, setting_value in _TASK_IDLE_SETTINGS
    }
    seen: set[str] = set()
    for setting in idle_settings:
        definition = expected.get(setting.tag)
        if definition is None:
            raise ValueError("task settings are invalid")
        setting_name, setting_value = definition
        if (
            setting_name in seen
            or setting.attrib
            or len(setting) != 0
            or setting.text != setting_value
        ):
            raise ValueError("task settings are invalid")
        seen.add(setting_name)


def _one_text(element: ElementTree.Element, path: str) -> str:
    matches = element.findall(path)
    if len(matches) != 1 or type(matches[0].text) is not str:
        raise ValueError("task XML value is invalid")
    return matches[0].text

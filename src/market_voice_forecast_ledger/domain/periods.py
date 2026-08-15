import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from market_voice_forecast_ledger.domain.common import to_jst
from market_voice_forecast_ledger.domain.enums import (
    PeriodReviewDecision,
    TimeBasis,
)


_EXPLICIT_FIRST_WEEK = re.compile(r"^(\d{4})年(\d{1,2})月第1週$")
_EXPLICIT_MONTH = re.compile(r"^(\d{4})年(\d{1,2})月$")
_EXPLICIT_YEAR = re.compile(r"^(\d{4})年$")
_RELATIVE_WEEK_OFFSETS = {"今週": 0, "来週": 1, "再来週": 2}
_RELATIVE_MONTH_OFFSETS = {"今月": 0, "来月": 1, "再来月": 2}


@dataclass(frozen=True, slots=True)
class NormalizedPeriod:
    start_date: date | None
    end_date: date | None
    time_basis: TimeBasis | None
    source_expression: str | None
    is_unknown: bool
    basis_published_at: datetime | None = None
    id: int | None = None
    statement_id: int | None = None


@dataclass(frozen=True, slots=True)
class EffectivePeriodReview:
    id: int
    period_id: int
    decision: PeriodReviewDecision
    actor: str
    reason: str
    created_at: datetime
    period_is_unknown: bool

    @property
    def approved_for_unknown_column(self) -> bool:
        return (
            self.period_is_unknown
            and self.decision is PeriodReviewDecision.APPROVE_UNKNOWN
        )

    @property
    def excluded(self) -> bool:
        return self.decision is PeriodReviewDecision.REJECT


def first_week_of_month(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    monday = first - timedelta(days=first.weekday())
    return monday, monday + timedelta(days=6)


def relative_week(published_at: datetime, offset: int) -> tuple[date, date]:
    local_day = to_jst(published_at).date()
    monday = local_day - timedelta(days=local_day.weekday()) + timedelta(
        weeks=offset
    )
    return monday, monday + timedelta(days=6)


def add_months(day: date, offset: int) -> date:
    month_index = day.year * 12 + day.month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def normalize_period(
    expression: str | None, published_at: datetime
) -> NormalizedPeriod:
    _require_aware(published_at)

    explicit = _explicit_period(expression)
    if explicit is not None:
        start, end = explicit
        return NormalizedPeriod(
            start,
            end,
            TimeBasis.EXPLICIT_STATEMENT,
            expression,
            False,
        )

    if expression in _RELATIVE_WEEK_OFFSETS:
        start, end = relative_week(
            published_at, _RELATIVE_WEEK_OFFSETS[expression]
        )
        return _relative_result(expression, start, end, published_at)

    if expression in _RELATIVE_MONTH_OFFSETS:
        local_day = to_jst(published_at).date()
        target = add_months(
            local_day, _RELATIVE_MONTH_OFFSETS[expression]
        )
        start, end = _calendar_month(target)
        return _relative_result(expression, start, end, published_at)

    if expression == "半年後":
        target = add_months(to_jst(published_at).date(), 6)
        start, end = _calendar_month(target)
        return _relative_result(expression, start, end, published_at)

    return NormalizedPeriod(None, None, None, expression, True)


def _explicit_period(expression: str | None) -> tuple[date, date] | None:
    if expression is None:
        return None
    try:
        match = _EXPLICIT_FIRST_WEEK.fullmatch(expression)
        if match is not None:
            return first_week_of_month(int(match.group(1)), int(match.group(2)))

        match = _EXPLICIT_MONTH.fullmatch(expression)
        if match is not None:
            year, month = int(match.group(1)), int(match.group(2))
            return _calendar_month(date(year, month, 1))

        match = _EXPLICIT_YEAR.fullmatch(expression)
        if match is not None:
            year = int(match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
    except (OverflowError, ValueError):
        return None
    return None


def _calendar_month(day: date) -> tuple[date, date]:
    return (
        date(day.year, day.month, 1),
        date(day.year, day.month, calendar.monthrange(day.year, day.month)[1]),
    )


def _relative_result(
    expression: str,
    start: date,
    end: date,
    published_at: datetime,
) -> NormalizedPeriod:
    return NormalizedPeriod(
        start,
        end,
        TimeBasis.PUBLISHED_AT,
        expression,
        False,
        published_at.astimezone(timezone.utc),
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("published_at must be timezone-aware")

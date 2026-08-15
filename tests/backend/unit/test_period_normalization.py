from datetime import date, datetime, timezone

import pytest

from market_voice_forecast_ledger.domain.common import to_jst
from market_voice_forecast_ledger.domain.enums import TimeBasis
from market_voice_forecast_ledger.domain.periods import normalize_period


PUBLISHED_AT = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("expression", "expected_start", "expected_end"),
    (
        ("2027年", date(2027, 1, 1), date(2027, 12, 31)),
        ("2026年9月", date(2026, 9, 1), date(2026, 9, 30)),
        ("2026年9月第1週", date(2026, 8, 31), date(2026, 9, 6)),
    ),
)
def test_exact_explicit_periods_use_statement_basis(
    expression, expected_start, expected_end
):
    result = normalize_period(expression, PUBLISHED_AT)

    assert result.start_date == expected_start
    assert result.end_date == expected_end
    assert result.time_basis is TimeBasis.EXPLICIT_STATEMENT
    assert result.source_expression == expression
    assert result.is_unknown is False
    assert result.basis_published_at is None


def test_month_first_week_contains_month_first_day_and_may_cross_month():
    result = normalize_period(
        "2026年9月第1週", datetime(2026, 8, 1, tzinfo=timezone.utc)
    )

    assert result.start_date == date(2026, 8, 31)
    assert result.end_date == date(2026, 9, 6)
    assert result.time_basis is TimeBasis.EXPLICIT_STATEMENT


@pytest.mark.parametrize(
    ("expression", "expected_start", "expected_end"),
    (
        ("今週", date(2026, 8, 10), date(2026, 8, 16)),
        ("来週", date(2026, 8, 17), date(2026, 8, 23)),
        ("再来週", date(2026, 8, 24), date(2026, 8, 30)),
        ("今月", date(2026, 8, 1), date(2026, 8, 31)),
        ("来月", date(2026, 9, 1), date(2026, 9, 30)),
        ("再来月", date(2026, 10, 1), date(2026, 10, 31)),
        ("半年後", date(2027, 2, 1), date(2027, 2, 28)),
    ),
)
def test_exact_relative_periods_use_published_at_in_fixed_jst(
    expression, expected_start, expected_end
):
    result = normalize_period(expression, PUBLISHED_AT)

    assert result.start_date == expected_start
    assert result.end_date == expected_end
    assert result.time_basis is TimeBasis.PUBLISHED_AT
    assert result.source_expression == expression
    assert result.is_unknown is False
    assert result.basis_published_at == PUBLISHED_AT


def test_relative_next_week_uses_published_at_in_jst():
    result = normalize_period("来週", PUBLISHED_AT)

    assert result.start_date == date(2026, 8, 17)
    assert result.end_date == date(2026, 8, 23)
    assert result.time_basis is TimeBasis.PUBLISHED_AT


def test_relative_next_week_uses_fixed_jst_across_utc_date_boundary():
    published_at = datetime(2026, 8, 16, 15, 30, tzinfo=timezone.utc)

    result = normalize_period("来週", published_at)

    assert to_jst(published_at).date() == date(2026, 8, 17)
    assert result.start_date == date(2026, 8, 24)
    assert result.end_date == date(2026, 8, 30)


@pytest.mark.parametrize(
    "expression",
    (
        None,
        "",
        "しばらく",
        "当面",
        "近いうち",
        " 来週",
        "来週 ",
        "2026年9月第2週",
        "2026年9月1日",
        "2026年13月",
        "2027年ごろ",
    ),
)
def test_missing_ambiguous_and_unsupported_expressions_stay_unknown(expression):
    result = normalize_period(expression, PUBLISHED_AT)

    assert result.start_date is None
    assert result.end_date is None
    assert result.time_basis is None
    assert result.source_expression == expression
    assert result.is_unknown is True
    assert result.basis_published_at is None

from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.retention import (
    DeletionPreview,
    DeletionResult,
    RetentionPolicy,
    expiry_for,
)


UTC = timezone.utc


class ExplodingTimezone(tzinfo):
    def utcoffset(self, value):
        raise RuntimeError("private timezone detail")

    def dst(self, value):
        return timedelta(0)


@pytest.mark.parametrize("days", [30, 90, 180, 365, None])
def test_supported_retention_values_are_exact(days):
    assert RetentionPolicy(days).days == days


@pytest.mark.parametrize(
    "invalid",
    [True, False, 30.0, "30", b"30", [], {}, {30}, object()],
)
def test_malformed_retention_values_raise_safe_domain_error(invalid):
    with pytest.raises(DomainError) as policy_error:
        RetentionPolicy(invalid)
    assert policy_error.value.code == "RETENTION_VALUE_INVALID"

    with pytest.raises(DomainError) as expiry_error:
        expiry_for(datetime(2026, 8, 16, tzinfo=UTC), invalid)
    assert expiry_error.value.code == "RETENTION_VALUE_INVALID"


def test_expiry_uses_creation_time_and_unlimited_is_exact_none():
    created_at = datetime(2026, 8, 16, 12, 30, tzinfo=UTC)

    assert expiry_for(created_at, 30) == created_at + timedelta(days=30)
    assert expiry_for(created_at, None) is None


@pytest.mark.parametrize(
    "created_at",
    [datetime(2026, 8, 16), "2026-08-16T00:00:00.000000Z", None],
)
def test_expiry_rejects_malformed_or_naive_creation_times(created_at):
    with pytest.raises(DomainError) as error:
        expiry_for(created_at, 30)
    assert error.value.code == "RETENTION_TIME_INVALID"


def test_expiry_rejects_an_unrepresentable_result_with_safe_error():
    with pytest.raises(DomainError) as error:
        expiry_for(datetime.max.replace(tzinfo=UTC), 30)
    assert error.value.code == "RETENTION_TIME_INVALID"


def test_expiry_maps_broken_timezone_behavior_to_safe_domain_error():
    created_at = datetime(2026, 8, 16, tzinfo=ExplodingTimezone())

    with pytest.raises(DomainError) as error:
        expiry_for(created_at, 30)

    assert error.value.code == "RETENTION_TIME_INVALID"
    assert "private timezone detail" not in str(error.value)


def test_public_text_results_are_frozen_slotted_value_objects():
    for result_type in (DeletionPreview, DeletionResult):
        assert result_type.__dataclass_params__.frozen is True
        assert result_type.__slots__

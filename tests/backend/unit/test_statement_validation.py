import pytest

from market_voice_forecast_ledger.domain.enums import (
    ConditionKind,
    DirectionKind,
    ForecastBasis,
    StatementType,
    TurningPointKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.statements import validate_statement
from market_voice_forecast_ledger.services.codex_contract import StatementProposal


def _proposal(**overrides) -> StatementProposal:
    values = {
        "statement_type": StatementType.FUTURE_FORECAST,
        "forecast_basis": ForecastBasis.DIRECT,
        "condition_kind": ConditionKind.UNCONDITIONAL,
        "condition_text": None,
        "direction_kind": DirectionKind.UP,
        "turning_point_kind": None,
        "target_expression": "Synthetic equity benchmark",
        "period_expression": "Synthetic future period",
        "codex_asset_hints": (),
        "evidence": (
            {
                "segment_id": 1,
                "excerpt": "Synthetic subject evidence.",
            },
        ),
    }
    values.update(overrides)
    return StatementProposal.model_validate(values)


@pytest.mark.parametrize(
    ("proposal", "code"),
    [
        (
            _proposal(forecast_basis=None),
            "FORECAST_BASIS_REQUIRED",
        ),
        (
            _proposal(direction_kind=None),
            "FORECAST_DIRECTION_REQUIRED",
        ),
        (
            _proposal(
                statement_type=StatementType.CURRENT_ANALYSIS,
                forecast_basis=ForecastBasis.DIRECT,
            ),
            "FORECAST_BASIS_NOT_ALLOWED",
        ),
        (
            _proposal(
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text=None,
            ),
            "CONDITION_TEXT_REQUIRED",
        ),
        (
            _proposal(
                direction_kind=DirectionKind.TURNING_POINT,
                turning_point_kind=None,
            ),
            "TURNING_POINT_KIND_REQUIRED",
        ),
    ],
    ids=(
        "future-basis",
        "future-direction",
        "non-future-basis",
        "conditional-text",
        "turning-point-subtype",
    ),
)
def test_statement_semantics_fail_closed(proposal, code):
    with pytest.raises(DomainError) as error:
        validate_statement(proposal)

    assert error.value.code == code


@pytest.mark.parametrize(
    ("statement_type", "direction_kind"),
    [
        (StatementType.CURRENT_ANALYSIS, None),
        (StatementType.CURRENT_ANALYSIS, DirectionKind.FLAT),
        (StatementType.PAST_RESULT_ANALYSIS, DirectionKind.UNKNOWN),
        (StatementType.GENERAL_STATEMENT, DirectionKind.DOWN),
    ],
)
def test_non_future_statements_allow_nullable_or_observed_direction(
    statement_type, direction_kind
):
    proposal = _proposal(
        statement_type=statement_type,
        forecast_basis=None,
        direction_kind=direction_kind,
    )

    assert validate_statement(proposal) is None
    assert proposal.direction_kind is direction_kind


@pytest.mark.parametrize(
    ("direction_kind", "turning_point_kind"),
    [
        (DirectionKind.FLAT, None),
        (DirectionKind.UNKNOWN, None),
        (DirectionKind.TURNING_POINT, TurningPointKind.BOTTOM),
    ],
)
def test_flat_unknown_and_turning_point_are_valid_distinct_directions(
    direction_kind, turning_point_kind
):
    proposal = _proposal(
        direction_kind=direction_kind,
        turning_point_kind=turning_point_kind,
    )

    assert validate_statement(proposal) is None
    assert proposal.direction_kind is direction_kind
    assert proposal.turning_point_kind is turning_point_kind

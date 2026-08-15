from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    ForecastBasis,
    MappingKind,
    StatementType,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.mappings import (
    MarketCode,
    MarketEvidence,
    RuleEvidenceKind,
    StatementContext,
    map_statement,
    min_confidence,
)
from market_voice_forecast_ledger.domain.statements import (
    EvidenceLink,
    NormalizedStatement,
)
from market_voice_forecast_ledger.services.codex_contract import AssetHint


def _statement(expression: str) -> NormalizedStatement:
    return NormalizedStatement(
        id=7,
        run_id=3,
        ordinal=1,
        batch_ordinal=2,
        proposal_ordinal=1,
        source_video_id=11,
        statement_type=StatementType.FUTURE_FORECAST,
        forecast_basis=ForecastBasis.DIRECT,
        condition_kind=ConditionKind.UNCONDITIONAL,
        condition_text=None,
        direction_kind=None,
        turning_point_kind=None,
        target_expression=expression,
        period_expression="来月",
        heatmap_candidate=True,
        evidence_links=(
            EvidenceLink(
                statement_id=7,
                ordinal=1,
                run_segment_id=101,
                segment_id=17,
                excerpt="Synthetic mapping evidence.",
                start_ms=0,
                end_ms=1_000,
            ),
        ),
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )


def _hint(
    expression: str,
    asset: Asset,
    confidence: Confidence = Confidence.HIGH,
) -> AssetHint:
    return AssetHint(
        expression=expression,
        suggested_asset=asset,
        confidence=confidence,
    )


def _context(
    expression: str,
    *assets: Asset,
    subject_kind: SubjectKind = SubjectKind.PERSON,
    adopted: tuple[MarketEvidence, ...] = (),
    interviewer: tuple[MarketEvidence, ...] = (),
    confidence: Confidence = Confidence.HIGH,
) -> StatementContext:
    return StatementContext(
        subject_kind=subject_kind,
        codex_asset_hints=tuple(
            _hint(expression, asset, confidence) for asset in assets
        ),
        adopted_subject_evidence=adopted,
        interviewer_evidence=interviewer,
    )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    (
        (Confidence.HIGH, Confidence.HIGH, Confidence.HIGH),
        (Confidence.HIGH, Confidence.MEDIUM, Confidence.MEDIUM),
        (Confidence.MEDIUM, Confidence.LOW, Confidence.LOW),
        (Confidence.LOW, Confidence.UNRESOLVED, Confidence.UNRESOLVED),
        (Confidence.UNRESOLVED, Confidence.HIGH, Confidence.UNRESOLVED),
    ),
)
def test_min_confidence_uses_the_weaker_ceiling(left, right, expected):
    assert min_confidence(left, right) is expected


def test_japanese_equity_expression_maps_to_two_inferred_assets():
    statement = _statement("日本株")

    mappings = map_statement(
        statement,
        _context(
            statement.target_expression,
            Asset.NIKKEI_225,
            Asset.TOPIX,
        ),
    )

    assert {(row.asset, row.mapping_kind) for row in mappings} == {
        (Asset.NIKKEI_225, MappingKind.INFERRED),
        (Asset.TOPIX, MappingKind.INFERRED),
    }
    assert {row.rule_confidence for row in mappings} == {Confidence.HIGH}
    assert {row.final_confidence for row in mappings} == {Confidence.HIGH}
    assert {row.reason_code for row in mappings} == {
        "japan_equity_to_nikkei_225",
        "japan_equity_to_topix",
    }
    assert all(
        evidence.to_safe_dict()
        == {
            "segment_id": 17,
            "evidence_kind": "explicit_market_expression",
            "market_code": "japan",
            "is_competing": False,
        }
        for row in mappings
        for evidence in row.rule_evidence
    )


@pytest.mark.parametrize(
    ("expression", "asset"),
    (
        ("日経平均", Asset.NIKKEI_225),
        ("TOPIX", Asset.TOPIX),
        ("S&P 500", Asset.SP500),
        ("S&P500", Asset.SP500),
        ("金", Asset.XAU_USD),
        ("XAU/USD", Asset.XAU_USD),
    ),
)
def test_exact_index_and_gold_references_are_direct(expression, asset):
    statement = _statement(expression)

    mappings = map_statement(
        statement,
        _context(statement.target_expression, asset),
    )

    assert len(mappings) == 1
    assert mappings[0].asset is asset
    assert mappings[0].mapping_kind is MappingKind.DIRECT
    assert mappings[0].rule_confidence is Confidence.HIGH
    assert mappings[0].final_confidence is Confidence.HIGH
    assert mappings[0].confidence_disagrees is False


def test_competing_adopted_market_lowers_an_exact_reference():
    statement = _statement("日経平均")
    context = _context(
        statement.target_expression,
        Asset.NIKKEI_225,
        adopted=(MarketEvidence(23, MarketCode.US),),
    )

    mapping = map_statement(statement, context)[0]

    assert mapping.rule_confidence is Confidence.LOW
    assert mapping.final_confidence is Confidence.LOW
    assert mapping.confidence_disagrees is True
    assert mapping.review_required is True
    assert mapping.rule_evidence[-1].to_safe_dict() == {
        "segment_id": 23,
        "evidence_kind": "surrounding_subject_statement",
        "market_code": "us",
        "is_competing": True,
    }


def test_generic_stock_market_uses_one_consistent_adopted_market_at_medium():
    statement = _statement("株式市場")
    context = _context(
        statement.target_expression,
        Asset.SP500,
        adopted=(MarketEvidence(31, MarketCode.US),),
    )

    mapping = map_statement(statement, context)[0]

    assert mapping.asset is Asset.SP500
    assert mapping.mapping_kind is MappingKind.INFERRED
    assert mapping.reason_code == "generic_stock_market_from_subject_context"
    assert mapping.rule_confidence is Confidence.MEDIUM
    assert mapping.final_confidence is Confidence.MEDIUM
    assert mapping.confidence_disagrees is True


def test_generic_stock_market_keeps_codex_candidate_low_with_competition():
    statement = _statement("株式市場")
    context = _context(
        statement.target_expression,
        Asset.SP500,
        adopted=(
            MarketEvidence(31, MarketCode.US),
            MarketEvidence(32, MarketCode.JAPAN),
        ),
    )

    mappings = map_statement(statement, context)

    assert tuple(row.asset for row in mappings) == (Asset.SP500,)
    assert mappings[0].rule_confidence is Confidence.LOW
    assert mappings[0].final_confidence is Confidence.LOW
    assert mappings[0].review_required is True


def test_interviewer_only_market_hint_cannot_raise_personal_confidence():
    statement = _statement("株式市場")
    context = _context(
        statement.target_expression,
        Asset.SP500,
        interviewer=(MarketEvidence(41, MarketCode.US),),
        subject_kind=SubjectKind.PERSON,
    )

    mappings = map_statement(statement, context)

    assert len(mappings) == 1
    assert mappings[0].asset is Asset.SP500
    assert mappings[0].rule_confidence is Confidence.UNRESOLVED
    assert mappings[0].final_confidence is Confidence.UNRESOLVED
    assert mappings[0].rule_evidence[-1].evidence_kind is (
        RuleEvidenceKind.INTERVIEWER_CONTEXT
    )


def test_organization_assigned_context_is_adopted_regardless_speaker_role():
    statement = _statement("株式市場")
    context = _context(
        statement.target_expression,
        Asset.SP500,
        interviewer=(MarketEvidence(51, MarketCode.US),),
        subject_kind=SubjectKind.ORGANIZATION,
    )

    mapping = map_statement(statement, context)[0]

    assert mapping.rule_confidence is Confidence.MEDIUM
    assert mapping.rule_evidence[-1].evidence_kind is (
        RuleEvidenceKind.ORGANIZATION_ASSIGNED_STATEMENT
    )


def test_codex_confidence_is_an_upper_bound_and_disagreement_is_visible():
    statement = _statement("米国株")

    mapping = map_statement(
        statement,
        _context(
            statement.target_expression,
            Asset.SP500,
            confidence=Confidence.MEDIUM,
        ),
    )[0]

    assert mapping.codex_confidence is Confidence.MEDIUM
    assert mapping.rule_confidence is Confidence.HIGH
    assert mapping.final_confidence is Confidence.MEDIUM
    assert mapping.confidence_disagrees is True


def test_missing_codex_support_caps_an_application_mapping_at_unresolved():
    statement = _statement("米国株")

    mapping = map_statement(
        statement,
        StatementContext(subject_kind=SubjectKind.PERSON),
    )[0]

    assert mapping.codex_confidence is Confidence.UNRESOLVED
    assert mapping.rule_confidence is Confidence.HIGH
    assert mapping.final_confidence is Confidence.UNRESOLVED


def test_absent_gold_statement_creates_no_xau_mapping():
    statement = _statement("米国株")
    context = _context(
        statement.target_expression,
        Asset.SP500,
        Asset.XAU_USD,
    )

    mappings = map_statement(statement, context)

    assert {row.asset for row in mappings} == {Asset.SP500}


def test_unrelated_kanji_does_not_create_a_gold_mapping():
    statement = _statement("資金市場")

    mappings = map_statement(
        statement,
        _context(statement.target_expression, Asset.XAU_USD),
    )

    assert mappings == ()

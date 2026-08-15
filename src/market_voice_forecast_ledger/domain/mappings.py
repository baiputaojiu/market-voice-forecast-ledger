from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    MappingKind,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.statements import NormalizedStatement


class MarketCode(StrEnum):
    JAPAN = "japan"
    US = "us"
    GOLD = "gold"


class RuleEvidenceKind(StrEnum):
    DIRECT_EXPRESSION = "direct_expression"
    EXPLICIT_MARKET_EXPRESSION = "explicit_market_expression"
    GENERIC_EXPRESSION = "generic_expression"
    SURROUNDING_SUBJECT_STATEMENT = "surrounding_subject_statement"
    INTERVIEWER_CONTEXT = "interviewer_context"
    ORGANIZATION_ASSIGNED_STATEMENT = "organization_assigned_statement"


class AssetHintLike(Protocol):
    expression: str
    suggested_asset: Asset
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    segment_id: int
    market_code: MarketCode


@dataclass(frozen=True, slots=True)
class StatementContext:
    subject_kind: SubjectKind
    codex_asset_hints: tuple[AssetHintLike, ...] = ()
    adopted_subject_evidence: tuple[MarketEvidence, ...] = ()
    interviewer_evidence: tuple[MarketEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleEvidence:
    segment_id: int
    evidence_kind: RuleEvidenceKind
    market_code: MarketCode
    is_competing: bool

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "evidence_kind": self.evidence_kind.value,
            "market_code": self.market_code.value,
            "is_competing": self.is_competing,
        }


@dataclass(frozen=True, slots=True)
class AssetMapping:
    asset: Asset
    mapping_kind: MappingKind
    reason_code: str
    codex_confidence: Confidence
    rule_confidence: Confidence
    final_confidence: Confidence
    confidence_disagrees: bool
    rule_evidence: tuple[RuleEvidence, ...]
    run_id: int
    statement_id: int
    original_expression: str
    source_video_id: int
    id: int | None = None

    @property
    def conversion_reason(self) -> str:
        return self.reason_code

    @property
    def review_required(self) -> bool:
        return self.final_confidence in {
            Confidence.LOW,
            Confidence.UNRESOLVED,
        }

    @property
    def heatmap_eligible(self) -> bool:
        return not self.review_required


CONFIDENCE_ORDER = {
    Confidence.UNRESOLVED: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def min_confidence(left: Confidence, right: Confidence) -> Confidence:
    return (
        left
        if CONFIDENCE_ORDER[left] <= CONFIDENCE_ORDER[right]
        else right
    )


_ASSET_ORDER = {
    Asset.NIKKEI_225: 0,
    Asset.TOPIX: 1,
    Asset.SP500: 2,
    Asset.XAU_USD: 3,
}
_ASSET_MARKET = {
    Asset.NIKKEI_225: MarketCode.JAPAN,
    Asset.TOPIX: MarketCode.JAPAN,
    Asset.SP500: MarketCode.US,
    Asset.XAU_USD: MarketCode.GOLD,
}
_MARKET_ASSETS = {
    MarketCode.JAPAN: (Asset.NIKKEI_225, Asset.TOPIX),
    MarketCode.US: (Asset.SP500,),
    MarketCode.GOLD: (Asset.XAU_USD,),
}
_DIRECT_REASONS = {
    Asset.NIKKEI_225: "direct_nikkei_225_reference",
    Asset.TOPIX: "direct_topix_reference",
    Asset.SP500: "direct_sp500_reference",
    Asset.XAU_USD: "direct_xau_usd_reference",
}
_EXPLICIT_MARKET_REASONS = {
    Asset.NIKKEI_225: "japan_equity_to_nikkei_225",
    Asset.TOPIX: "japan_equity_to_topix",
    Asset.SP500: "us_equity_to_sp500",
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    asset: Asset
    mapping_kind: MappingKind
    reason_code: str
    evidence_kind: RuleEvidenceKind

    @property
    def market_code(self) -> MarketCode:
        return _ASSET_MARKET[self.asset]


def map_statement(
    statement: NormalizedStatement, context: StatementContext
) -> tuple[AssetMapping, ...]:
    adopted, interviewer = _effective_context(context)
    candidates = _specific_candidates(statement.target_expression)
    if candidates:
        current_markets = expression_market_codes(statement.target_expression)
        return tuple(
            _build_mapping(
                statement,
                context,
                candidate,
                _specific_rule_confidence(
                    candidate.market_code,
                    current_markets,
                    adopted,
                    interviewer,
                ),
                adopted,
                interviewer,
                current_markets,
            )
            for candidate in candidates
        )

    if "株式市場" not in statement.target_expression:
        return ()

    generic = _generic_candidates(context, adopted, interviewer)
    return tuple(
        _build_mapping(
            statement,
            context,
            candidate,
            rule_confidence,
            adopted,
            interviewer,
            (),
        )
        for candidate, rule_confidence in generic
    )


def expression_market_codes(expression: str) -> tuple[MarketCode, ...]:
    return tuple(
        dict.fromkeys(
            candidate.market_code
            for candidate in _specific_candidates(expression)
        )
    )


def _specific_candidates(expression: str) -> tuple[_Candidate, ...]:
    direct_assets = _direct_assets(expression)
    by_asset = {
        asset: _Candidate(
            asset,
            MappingKind.DIRECT,
            _DIRECT_REASONS[asset],
            RuleEvidenceKind.DIRECT_EXPRESSION,
        )
        for asset in direct_assets
    }
    if "日本株" in expression:
        for asset in _MARKET_ASSETS[MarketCode.JAPAN]:
            by_asset.setdefault(
                asset,
                _Candidate(
                    asset,
                    MappingKind.INFERRED,
                    _EXPLICIT_MARKET_REASONS[asset],
                    RuleEvidenceKind.EXPLICIT_MARKET_EXPRESSION,
                ),
            )
    if "米国株" in expression:
        asset = Asset.SP500
        by_asset.setdefault(
            asset,
            _Candidate(
                asset,
                MappingKind.INFERRED,
                _EXPLICIT_MARKET_REASONS[asset],
                RuleEvidenceKind.EXPLICIT_MARKET_EXPRESSION,
            ),
        )
    return tuple(sorted(by_asset.values(), key=lambda row: _ASSET_ORDER[row.asset]))


def _direct_assets(expression: str) -> tuple[Asset, ...]:
    normalized_ascii = expression.upper().replace(" ", "")
    assets: list[Asset] = []
    if "日経平均" in expression:
        assets.append(Asset.NIKKEI_225)
    if "TOPIX" in normalized_ascii:
        assets.append(Asset.TOPIX)
    if "S&P500" in normalized_ascii:
        assets.append(Asset.SP500)
    stripped = expression.strip()
    if stripped == "金" or "XAU/USD" in normalized_ascii:
        assets.append(Asset.XAU_USD)
    return tuple(dict.fromkeys(assets))


def _effective_context(
    context: StatementContext,
) -> tuple[
    tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
]:
    adopted = tuple(
        (evidence, RuleEvidenceKind.SURROUNDING_SUBJECT_STATEMENT)
        for evidence in _ordered_evidence(context.adopted_subject_evidence)
    )
    interviewer_evidence = _ordered_evidence(context.interviewer_evidence)
    if context.subject_kind is SubjectKind.ORGANIZATION:
        adopted += tuple(
            (evidence, RuleEvidenceKind.ORGANIZATION_ASSIGNED_STATEMENT)
            for evidence in interviewer_evidence
        )
        return adopted, ()
    return adopted, tuple(
        (evidence, RuleEvidenceKind.INTERVIEWER_CONTEXT)
        for evidence in interviewer_evidence
    )


def _ordered_evidence(
    evidence: tuple[MarketEvidence, ...],
) -> tuple[MarketEvidence, ...]:
    return tuple(
        MarketEvidence(segment_id, MarketCode(market_code))
        for segment_id, market_code in sorted(
            {
                (item.segment_id, item.market_code.value)
                for item in evidence
                if isinstance(item.segment_id, int)
                and not isinstance(item.segment_id, bool)
                and item.segment_id > 0
            }
        )
    )


def _specific_rule_confidence(
    candidate_market: MarketCode,
    current_markets: tuple[MarketCode, ...],
    adopted: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    interviewer: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
) -> Confidence:
    evidence_markets = {
        evidence.market_code for evidence, _ in adopted + interviewer
    }
    has_competition = any(
        market is not candidate_market
        for market in set(current_markets) | evidence_markets
    )
    return Confidence.LOW if has_competition else Confidence.HIGH


def _generic_candidates(
    context: StatementContext,
    adopted: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    interviewer: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
) -> tuple[tuple[_Candidate, Confidence], ...]:
    adopted_markets = {evidence.market_code for evidence, _ in adopted}
    stock_markets = adopted_markets & {MarketCode.JAPAN, MarketCode.US}
    hinted_assets = _hinted_stock_assets(context)

    if len(stock_markets) == 1:
        selected_market = next(iter(stock_markets))
        assets = _MARKET_ASSETS[selected_market]
        interviewer_competes = any(
            evidence.market_code is not selected_market
            for evidence, _ in interviewer
        )
        has_competition = (
            any(market is not selected_market for market in adopted_markets)
            or interviewer_competes
        )
        confidence = Confidence.LOW if has_competition else Confidence.MEDIUM
        return tuple(
            (
                _Candidate(
                    asset,
                    MappingKind.INFERRED,
                    "generic_stock_market_from_subject_context",
                    RuleEvidenceKind.GENERIC_EXPRESSION,
                ),
                confidence,
            )
            for asset in assets
        )

    if len(stock_markets) > 1:
        assets = tuple(
            asset
            for asset in hinted_assets
            if _ASSET_MARKET[asset] in stock_markets
        )
        if not assets:
            assets = tuple(
                asset
                for market in (MarketCode.JAPAN, MarketCode.US)
                if market in stock_markets
                for asset in _MARKET_ASSETS[market]
            )
        return tuple(
            (
                _Candidate(
                    asset,
                    MappingKind.INFERRED,
                    "generic_stock_market_candidate_with_competition",
                    RuleEvidenceKind.GENERIC_EXPRESSION,
                ),
                Confidence.LOW,
            )
            for asset in assets
        )

    return tuple(
        (
            _Candidate(
                asset,
                MappingKind.INFERRED,
                "generic_stock_market_unresolved_candidate",
                RuleEvidenceKind.GENERIC_EXPRESSION,
            ),
            Confidence.UNRESOLVED,
        )
        for asset in hinted_assets
    )


def _hinted_stock_assets(context: StatementContext) -> tuple[Asset, ...]:
    hinted = {
        hint.suggested_asset
        for hint in context.codex_asset_hints
        if hint.suggested_asset in {
            Asset.NIKKEI_225,
            Asset.TOPIX,
            Asset.SP500,
        }
    }
    return tuple(sorted(hinted, key=_ASSET_ORDER.__getitem__))


def _build_mapping(
    statement: NormalizedStatement,
    context: StatementContext,
    candidate: _Candidate,
    rule_confidence: Confidence,
    adopted: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    interviewer: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    current_markets: tuple[MarketCode, ...],
) -> AssetMapping:
    codex_confidence = _codex_confidence(context, candidate.asset)
    final_confidence = min_confidence(codex_confidence, rule_confidence)
    evidence = _rule_evidence(
        statement,
        candidate,
        adopted,
        interviewer,
        current_markets,
    )
    return AssetMapping(
        asset=candidate.asset,
        mapping_kind=candidate.mapping_kind,
        reason_code=candidate.reason_code,
        codex_confidence=codex_confidence,
        rule_confidence=rule_confidence,
        final_confidence=final_confidence,
        confidence_disagrees=codex_confidence is not rule_confidence,
        rule_evidence=evidence,
        run_id=statement.run_id,
        statement_id=statement.id,
        original_expression=statement.target_expression,
        source_video_id=statement.source_video_id,
    )


def _codex_confidence(
    context: StatementContext, asset: Asset
) -> Confidence:
    values = tuple(
        hint.confidence
        for hint in context.codex_asset_hints
        if hint.suggested_asset is asset
    )
    if not values:
        return Confidence.UNRESOLVED
    return min(values, key=CONFIDENCE_ORDER.__getitem__)


def _rule_evidence(
    statement: NormalizedStatement,
    candidate: _Candidate,
    adopted: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    interviewer: tuple[tuple[MarketEvidence, RuleEvidenceKind], ...],
    current_markets: tuple[MarketCode, ...],
) -> tuple[RuleEvidence, ...]:
    rows = [
        RuleEvidence(
            link.segment_id,
            candidate.evidence_kind,
            candidate.market_code,
            False,
        )
        for link in statement.evidence_links
    ]
    rows.extend(
        RuleEvidence(
            evidence.segment_id,
            kind,
            evidence.market_code,
            evidence.market_code is not candidate.market_code,
        )
        for evidence, kind in adopted + interviewer
    )
    for market in current_markets:
        if market is candidate.market_code:
            continue
        rows.extend(
            RuleEvidence(
                link.segment_id,
                candidate.evidence_kind,
                market,
                True,
            )
            for link in statement.evidence_links
        )
    unique_keys = dict.fromkeys(
        (
            row.segment_id,
            row.evidence_kind,
            row.market_code,
            row.is_competing,
        )
        for row in rows
    )
    return tuple(
        RuleEvidence(segment_id, kind, market, competing)
        for segment_id, kind, market, competing in unique_keys
    )

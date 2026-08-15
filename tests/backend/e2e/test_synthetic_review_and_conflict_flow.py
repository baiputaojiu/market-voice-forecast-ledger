import pytest

from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    DirectionKind,
    MappingReviewDecision,
    PeriodReviewDecision,
    ViewRelation,
)
from tests.backend.e2e.synthetic_fixture import SyntheticLedgerFixture


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    runtime_dir = tmp_path_factory.mktemp("synthetic-review-conflict")
    with SyntheticLedgerFixture(runtime_dir) as ledger:
        yield ledger.run_complete_flow()


def test_reviews_conflicts_reversals_and_reposts_survive_public_boundaries(flow):
    review_name = flow.subject_name("review_boundary")
    assert all(
        not flow.row(flow.before_reviews_week, review_name, asset).cells
        for asset in Asset
    )

    assert {
        (item.decision, item.before_confidence, item.asset)
        for item in flow.mapping_review_evidence
    } == {
        (MappingReviewDecision.APPROVE, Confidence.LOW, Asset.NIKKEI_225),
        (MappingReviewDecision.APPROVE, Confidence.LOW, Asset.TOPIX),
        (MappingReviewDecision.APPROVE, Confidence.UNRESOLVED, Asset.SP500),
    }
    assert all(
        item.result.applied_to_current
        and item.result.current_summary is not None
        for item in flow.mapping_review_evidence
    )
    assert {
        item.decision for item in flow.period_review_evidence
    } == {PeriodReviewDecision.APPROVE_UNKNOWN}
    assert all(
        item.result.applied_to_current
        and item.result.current_summary is not None
        for item in flow.period_review_evidence
    )

    low_nikkei = flow.month.cell(review_name, Asset.NIKKEI_225, "2027-01")
    low_topix = flow.month.cell(review_name, Asset.TOPIX, "2027-01")
    unresolved_us = flow.month.cell(review_name, Asset.SP500, "2027-02")
    unknown = flow.month.cell(review_name, Asset.NIKKEI_225, "unknown")
    assert low_nikkei.confidence is Confidence.LOW
    assert low_topix.confidence is Confidence.LOW
    assert unresolved_us.confidence is Confidence.UNRESOLVED
    assert unknown.unknown_period is True
    assert unknown.primary_direction is DirectionKind.UNKNOWN

    conflict_name = flow.subject_name("conflict_history")
    disagreement = flow.month.cell(
        conflict_name, Asset.SP500, "2026-09"
    )
    assert disagreement.view_relation is ViewRelation.DISAGREEMENT
    assert disagreement.directions == (DirectionKind.UP, DirectionKind.DOWN)
    assert disagreement.evidence_count == 2
    assert len(disagreement.supporting_statement_ids) == 2

    changed = flow.month.cell(conflict_name, Asset.SP500, "2026-10")
    assert changed.view_relation is ViewRelation.CHANGED
    assert changed.primary_direction is DirectionKind.UP
    assert changed.directions == (DirectionKind.UP,)
    assert changed.evidence_count == 1
    assert len(changed.counterevidence_statement_ids) == 1

    repost = flow.month.cell(conflict_name, Asset.SP500, "2027-02")
    assert repost.view_relation is ViewRelation.CURRENT
    assert repost.primary_direction is DirectionKind.UP
    assert repost.evidence_count == 2
    assert len(repost.supporting_statement_ids) == 2
    assert len(repost.source_forecast_ids) == 2
    assert len(set(repost.source_forecast_ids)) == 2

    assert flow.original_video_id != flow.repost_video_id
    assert {
        flow.original_video_id,
        flow.repost_video_id,
    }.issubset(set(flow.input_video_ids("conflict_history")))
    assert flow.forecast_source_video_ids(repost) == {
        flow.original_video_id,
        flow.repost_video_id,
    }

    # A second build uses a different subject execution order but has the same
    # stable semantic result; no test depends on database insertion order.
    with SyntheticLedgerFixture(
        flow.runtime_dir.parent / "reverse-order",
        subject_order=tuple(reversed(flow.subject_roles)),
    ) as reversed_ledger:
        reversed_flow = reversed_ledger.run_complete_flow()
    assert reversed_flow.view_semantic_signature(reversed_flow.week) == (
        flow.view_semantic_signature(flow.week)
    )
    assert reversed_flow.view_semantic_signature(reversed_flow.month) == (
        flow.view_semantic_signature(flow.month)
    )

from collections import Counter
from dataclasses import asdict

import pytest

from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    DirectionKind,
    HeatmapGranularity,
    MappingKind,
    StatementType,
)
from tests.backend.e2e.synthetic_fixture import (
    SYNTHETIC_CREATED_AT,
    SyntheticLedgerFixture,
)


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    runtime_dir = tmp_path_factory.mktemp("synthetic-heatmap")
    with SyntheticLedgerFixture(runtime_dir) as ledger:
        yield ledger.run_complete_flow()


def test_public_pipeline_builds_deterministic_private_safe_heatmaps(flow):
    expected_pairs = {
        (subject_name, asset)
        for subject_name in flow.subject_names
        for asset in Asset
    }
    for view in (flow.week, flow.month):
        assert len(view.rows) == 16
        assert Counter(row.subject_key for row in view.rows) == {
            subject_name: 4 for subject_name in flow.subject_names
        }
        assert Counter(row.asset for row in view.rows) == {
            asset: 4 for asset in Asset
        }
        assert {(row.subject_key, row.asset) for row in view.rows} == expected_pairs

    japan_name = flow.subject_name("personal_japan")
    japan_nikkei = flow.month.cell(japan_name, Asset.NIKKEI_225, "2026-09")
    japan_topix = flow.month.cell(japan_name, Asset.TOPIX, "2026-09")
    assert japan_nikkei.mapping_kind is MappingKind.INFERRED
    assert japan_topix.mapping_kind is MappingKind.INFERRED
    assert japan_nikkei.primary_direction is DirectionKind.UP
    assert japan_topix.primary_direction is DirectionKind.UP

    organization_name = flow.subject_name("organization_us")
    us_cell = flow.month.cell(organization_name, Asset.SP500, "2026-09")
    assert us_cell.mapping_kind is MappingKind.INFERRED
    assert us_cell.primary_direction is DirectionKind.STRONG_UP

    for view in (flow.week, flow.month):
        assert all(
            not flow.row(view, subject_name, Asset.XAU_USD).cells
            for subject_name in flow.subject_names
        )

    assert {statement.statement_type for statement in flow.statements} == set(
        StatementType
    )
    assert all(
        statement.is_heatmap_candidate
        is (statement.statement_type is StatementType.FUTURE_FORECAST)
        for statement in flow.statements
    )

    unconditional = flow.month.cell(
        japan_name,
        Asset.NIKKEI_225,
        "2026-10",
        ConditionKind.UNCONDITIONAL,
    )
    conditional = flow.month.cell(
        japan_name,
        Asset.NIKKEI_225,
        "2026-10",
        ConditionKind.CONDITIONAL,
    )
    assert unconditional.condition_texts == ()
    assert conditional.condition_texts == (
        "Synthetic policy threshold remains satisfied",
    )
    assert set(unconditional.source_forecast_ids).isdisjoint(
        conditional.source_forecast_ids
    )

    assert flow.month.cell(
        organization_name, Asset.SP500, "2026-11"
    ).primary_direction is DirectionKind.TURNING_POINT
    assert flow.month.cell(
        organization_name, Asset.SP500, "2026-12"
    ).primary_direction is DirectionKind.FLAT
    assert flow.month.cell(
        flow.subject_name("review_boundary"),
        Asset.NIKKEI_225,
        "unknown",
    ).primary_direction is DirectionKind.UNKNOWN

    assert flow.later_segment_id not in flow.input_segment_ids("personal_japan")
    assert all(
        source.published_at.date() <= flow.cutoff_day
        for source in flow.input_sources
    )
    assert flow.later_source.published_at.date() > flow.cutoff_day

    assert {run.output.created_at for run in flow.runs} == {
        SYNTHETIC_CREATED_AT
    }
    assert {statement.created_at for statement in flow.statements} == {
        SYNTHETIC_CREATED_AT
    }
    assert {run.batch.created_at for run in flow.runs} == {
        SYNTHETIC_CREATED_AT
    }

    assert flow.receipts
    assert {
        (receipt.model, receipt.reasoning_effort, receipt.boundary_mode)
        for receipt in flow.receipts
    } == {("gpt-5.6-sol", "max", "stored_statements_only")}
    assert [receipt.tool_call_count for receipt in flow.receipts] == [
        0
    ] * len(flow.receipts)
    assert flow.external_tool_calls == sum(
        receipt.tool_call_count for receipt in flow.receipts
    )
    assert flow.external_tool_calls == 0

    for granularity, payload in (
        (HeatmapGranularity.WEEK, flow.api_week),
        (HeatmapGranularity.MONTH, flow.api_month),
    ):
        assert payload["cutoff"] == flow.cutoff_day.isoformat()
        assert payload["granularity"] == granularity.value
        assert len(payload["rows"]) == 16
        assert {
            (row["subject_key"], row["asset"]) for row in payload["rows"]
        } == {
            (subject_name, asset.value)
            for subject_name in flow.subject_names
            for asset in Asset
        }
        assert all(
            set(row)
            == {
                "subject_id",
                "subject_key",
                "scope_id",
                "scope_status",
                "stale_reason",
                "asset",
                "cells",
            }
            for row in payload["rows"]
        )

    serialized_api = flow.serialized_api
    persisted_evidence = flow.persisted_evidence_links
    assert persisted_evidence
    source_bodies = {
        source.segment_id: source.body
        for run in flow.runs
        for source in run.sources
    }
    assert all(
        link.excerpt
        and len(link.excerpt) <= 300
        and link.excerpt in source_bodies[link.segment_id]
        for link in persisted_evidence
    )
    assert '"evidence_count":' in serialized_api
    assert '"supporting_statement_ids":' in serialized_api
    for private_key in (
        "canonical_output_json",
        "excerpt",
        "input_text",
        "transcript",
        "audio_path",
        "prompt_body",
    ):
        assert private_key not in serialized_api
    for private_body in flow.private_segment_bodies:
        assert private_body not in serialized_api

    # The typed service and API views must describe the same public state.
    assert flow.api_semantic_signature(HeatmapGranularity.WEEK) == (
        flow.view_semantic_signature(flow.week)
    )
    assert flow.api_semantic_signature(HeatmapGranularity.MONTH) == (
        flow.view_semantic_signature(flow.month)
    )
    assert asdict(flow.week)["cutoff_day"] == flow.cutoff_day

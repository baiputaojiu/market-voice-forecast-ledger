import json
import sqlite3

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    MappingKind,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ASSET_MAPPING_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.mappings import MappingRepository
from market_voice_forecast_ledger.repositories.statements import (
    StatementRepository,
)
from market_voice_forecast_ledger.services.asset_mapping import (
    AssetMappingService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.periods import PeriodService
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_statement_evidence import (
    _prepare_output,
    _statement,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _proposal(
    segment_id: int,
    excerpt: str,
    expression: str,
    hints: tuple[tuple[Asset, Confidence], ...],
    *,
    statement_type: str = "future_forecast",
):
    proposal = _statement(
        ({"segment_id": segment_id, "excerpt": excerpt},),
        target_expression=expression,
        period_expression="来月",
        statement_type=statement_type,
        forecast_basis=(
            "direct" if statement_type == "future_forecast" else None
        ),
        direction_kind=("up" if statement_type != "general_statement" else None),
    )
    proposal["codex_asset_hints"] = [
        {
            "expression": expression,
            "suggested_asset": asset.value,
            "confidence": confidence.value,
        }
        for asset, confidence in hints
    ]
    return proposal


def _prepare_run(
    db,
    specs: tuple[
        tuple[
            str,
            str,
            tuple[tuple[Asset, Confidence], ...],
            str,
        ],
        ...,
    ],
    *,
    subject_kind: SubjectKind = SubjectKind.PERSON,
):
    texts = tuple(spec[0] for spec in specs)

    def statements(segment_ids):
        return [
            _proposal(
                segment_id,
                excerpt,
                expression,
                hints,
                statement_type=statement_type,
            )
            for segment_id, (excerpt, expression, hints, statement_type) in zip(
                segment_ids, specs, strict=True
            )
        ]

    prepared = _prepare_output(
        db,
        texts,
        statements,
        subject_kind=subject_kind,
    )
    StatementService(db).normalize_and_store(prepared.run_id)
    jobs = JobStateService(db)
    jobs.begin_unit(prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY)
    PeriodService(db).normalize_run(prepared.run_id)
    return prepared


def _prepare_and_map(db, specs, *, subject_kind=SubjectKind.PERSON):
    prepared = _prepare_run(db, specs, subject_kind=subject_kind)
    JobStateService(db).begin_unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    )
    mappings = AssetMappingService(db).map_run(prepared.run_id)
    return prepared, mappings


def _japan_equity_specs():
    return (
        (
            "Synthetic Japanese equity evidence.",
            "日本株",
            (
                (Asset.NIKKEI_225, Confidence.HIGH),
                (Asset.TOPIX, Confidence.MEDIUM),
            ),
            "future_forecast",
        ),
    )


def _unit_state(db, job_id):
    row = db.execute(
        """
        SELECT status, error_code
        FROM job_units
        WHERE job_id=? AND unit_key=?
        """,
        (job_id, ASSET_MAPPING_UNIT_KEY),
    ).fetchone()
    return row["status"], row["error_code"]


def test_map_run_requires_caller_to_start_mapping_unit(db):
    prepared = _prepare_run(db, _japan_equity_specs())

    with pytest.raises(DomainError) as error:
        AssetMappingService(db).map_run(prepared.run_id)

    assert error.value.code == "ASSET_MAPPING_UNIT_NOT_RUNNING"
    assert _unit_state(db, prepared.job_id) == ("pending", None)
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_asset_mappings"
    ).fetchone()[0] == 0


def test_rows_safe_evidence_hash_and_unit_success_commit_together(db):
    prepared, mappings = _prepare_and_map(db, _japan_equity_specs())
    statements = StatementRepository(db).list_run_statements(prepared.run_id)

    assert tuple(row.asset for row in mappings) == (
        Asset.NIKKEI_225,
        Asset.TOPIX,
    )
    assert tuple(row.mapping_kind for row in mappings) == (
        MappingKind.INFERRED,
        MappingKind.INFERRED,
    )
    assert tuple(row.codex_confidence for row in mappings) == (
        Confidence.HIGH,
        Confidence.MEDIUM,
    )
    assert tuple(row.rule_confidence for row in mappings) == (
        Confidence.HIGH,
        Confidence.HIGH,
    )
    assert tuple(row.final_confidence for row in mappings) == (
        Confidence.HIGH,
        Confidence.MEDIUM,
    )
    assert tuple(row.confidence_disagrees for row in mappings) == (
        False,
        True,
    )
    assert all(row.run_id == prepared.run_id for row in mappings)
    assert all(row.statement_id == statements[0].id for row in mappings)
    assert all(row.original_expression == "日本株" for row in mappings)
    assert all(row.source_video_id == prepared.video_id for row in mappings)

    raw_rows = db.execute(
        "SELECT * FROM analysis_asset_mappings ORDER BY id"
    ).fetchall()
    assert len(raw_rows) == 2
    for raw in raw_rows:
        evidence = json.loads(raw["rule_evidence_json"])
        assert evidence == [
            {
                "evidence_kind": "explicit_market_expression",
                "is_competing": False,
                "market_code": "japan",
                "segment_id": prepared.segment_ids[0],
            }
        ]
        assert set(evidence[0]) == {
            "segment_id",
            "evidence_kind",
            "market_code",
            "is_competing",
        }
        assert "Synthetic Japanese equity evidence." not in raw[
            "rule_evidence_json"
        ]
        assert "日本株" not in raw["rule_evidence_json"]

    expected_payload = [
        {
            "asset": "nikkei_225",
            "codex_confidence": "high",
            "confidence_disagrees": False,
            "conversion_reason": "japan_equity_to_nikkei_225",
            "final_confidence": "high",
            "mapping_kind": "inferred",
            "original_expression": "日本株",
            "rule_confidence": "high",
            "rule_evidence": [
                {
                    "evidence_kind": "explicit_market_expression",
                    "is_competing": False,
                    "market_code": "japan",
                    "segment_id": prepared.segment_ids[0],
                }
            ],
            "run_id": prepared.run_id,
            "source_video_id": prepared.video_id,
            "statement_id": statements[0].id,
        },
        {
            "asset": "topix",
            "codex_confidence": "medium",
            "confidence_disagrees": True,
            "conversion_reason": "japan_equity_to_topix",
            "final_confidence": "medium",
            "mapping_kind": "inferred",
            "original_expression": "日本株",
            "rule_confidence": "high",
            "rule_evidence": [
                {
                    "evidence_kind": "explicit_market_expression",
                    "is_competing": False,
                    "market_code": "japan",
                    "segment_id": prepared.segment_ids[0],
                }
            ],
            "run_id": prepared.run_id,
            "source_video_id": prepared.video_id,
            "statement_id": statements[0].id,
        },
    ]
    unit = JobStateService(db).unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    )
    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == sha256_text(canonical_json(expected_payload))
    assert MappingRepository(db).list_run_mappings(prepared.run_id) == mappings


def test_successful_mapping_unit_is_reused_without_duplicate_rows(db):
    prepared, first = _prepare_and_map(db, _japan_equity_specs())

    second = AssetMappingService(db).map_run(prepared.run_id)

    assert second == first
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_asset_mappings"
    ).fetchone()[0] == 2
    assert JobStateService(db).unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    ).attempt_count == 1


def test_generic_subject_context_is_medium_and_absent_gold_stays_empty(db):
    specs = (
        (
            "Synthetic generic stock evidence.",
            "株式市場",
            ((Asset.SP500, Confidence.HIGH),),
            "future_forecast",
        ),
        (
            "Synthetic surrounding US evidence.",
            "米国株",
            ((Asset.SP500, Confidence.HIGH),),
            "general_statement",
        ),
    )

    _, mappings = _prepare_and_map(db, specs)
    generic = next(row for row in mappings if row.original_expression == "株式市場")

    assert generic.asset is Asset.SP500
    assert generic.rule_confidence is Confidence.MEDIUM
    assert generic.final_confidence is Confidence.MEDIUM
    assert generic.review_required is False
    assert Asset.XAU_USD not in {row.asset for row in mappings}


def test_unresolved_mapping_is_valid_success_but_requires_review(db):
    specs = (
        (
            "Synthetic unresolved stock evidence.",
            "株式市場",
            ((Asset.SP500, Confidence.HIGH),),
            "future_forecast",
        ),
    )

    prepared, mappings = _prepare_and_map(db, specs)

    assert len(mappings) == 1
    assert mappings[0].final_confidence is Confidence.UNRESOLVED
    assert mappings[0].review_required is True
    assert mappings[0].heatmap_eligible is False
    assert _unit_state(db, prepared.job_id) == ("success", None)


def test_storage_failure_rolls_back_rows_and_retry_reuses_upstream_units(db):
    prepared = _prepare_run(db, _japan_equity_specs())
    statement_ids = tuple(
        row.id
        for row in StatementRepository(db).list_run_statements(prepared.run_id)
    )
    JobStateService(db).begin_unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    )
    db.execute(
        """
        CREATE TRIGGER synthetic_reject_topix_mapping
        BEFORE INSERT ON analysis_asset_mappings
        WHEN NEW.asset='topix'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_MAPPING_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as error:
        AssetMappingService(db).map_run(prepared.run_id)

    assert error.value.code == "ASSET_MAPPING_STORAGE_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_asset_mappings"
    ).fetchone()[0] == 0
    assert _unit_state(db, prepared.job_id) == (
        "failed",
        "ASSET_MAPPING_STORAGE_FAILED",
    )
    assert JobStateService(db).unit(
        prepared.job_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).status is UnitStatus.SUCCESS
    assert JobStateService(db).unit(
        prepared.job_id, PERIOD_NORMALIZATION_UNIT_KEY
    ).status is UnitStatus.SUCCESS

    db.execute("DROP TRIGGER synthetic_reject_topix_mapping")
    artifacts = {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            """
            SELECT unit_key, output_hash
            FROM job_units
            WHERE job_id=? AND status='success'
            """,
            (prepared.job_id,),
        )
    }
    plan = JobStateService(db).resume(prepared.job_id, artifacts)
    assert plan.next_unit_key == ASSET_MAPPING_UNIT_KEY
    JobStateService(db).begin_unit(
        prepared.job_id, ASSET_MAPPING_UNIT_KEY
    )

    mappings = AssetMappingService(db).map_run(prepared.run_id)

    assert len(mappings) == 2
    assert tuple(row.statement_id for row in mappings) == (
        statement_ids[0],
        statement_ids[0],
    )
    assert _unit_state(db, prepared.job_id) == ("success", None)


def test_mapping_rows_reject_raw_update_delete_and_replace(db):
    _, mappings = _prepare_and_map(db, _japan_equity_specs())
    mapping_id = mappings[0].id

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            UPDATE analysis_asset_mappings
            SET final_confidence=final_confidence
            WHERE id=?
            """,
            (mapping_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "DELETE FROM analysis_asset_mappings WHERE id=?",
            (mapping_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_asset_mappings(
                id, run_id, statement_id, original_expression, asset,
                mapping_kind, conversion_reason, codex_confidence,
                rule_confidence, final_confidence, confidence_disagrees,
                rule_evidence_json, source_video_id
            )
            SELECT id, run_id, statement_id, original_expression, asset,
                   mapping_kind, conversion_reason, codex_confidence,
                   rule_confidence, final_confidence, confidence_disagrees,
                   rule_evidence_json, source_video_id
            FROM analysis_asset_mappings
            WHERE id=?
            """,
            (mapping_id,),
        )


def test_schema_rejects_a_final_confidence_above_either_input(db):
    prepared, mappings = _prepare_and_map(db, _japan_equity_specs())
    source = mappings[0]

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_asset_mappings(
                run_id, statement_id, original_expression, asset,
                mapping_kind, conversion_reason, codex_confidence,
                rule_confidence, final_confidence, confidence_disagrees,
                rule_evidence_json, source_video_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared.run_id,
                source.statement_id,
                source.original_expression,
                Asset.SP500.value,
                MappingKind.INFERRED.value,
                "synthetic_invalid_ceiling",
                Confidence.LOW.value,
                Confidence.HIGH.value,
                Confidence.HIGH.value,
                1,
                canonical_json([]),
                source.source_video_id,
            ),
        )


def test_schema_rejects_rule_evidence_with_transcript_body_fields(db):
    prepared, mappings = _prepare_and_map(db, _japan_equity_specs())
    source = mappings[0]
    unsafe_evidence = canonical_json(
        [
            {
                "segment_id": prepared.segment_ids[0],
                "evidence_kind": "explicit_market_expression",
                "market_code": "japan",
                "is_competing": False,
                "body": "Synthetic transcript body must never persist here.",
            }
        ]
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="ASSET_MAPPING_EVIDENCE_UNSAFE"
    ):
        db.execute(
            """
            INSERT INTO analysis_asset_mappings(
                run_id, statement_id, original_expression, asset,
                mapping_kind, conversion_reason, codex_confidence,
                rule_confidence, final_confidence, confidence_disagrees,
                rule_evidence_json, source_video_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared.run_id,
                source.statement_id,
                source.original_expression,
                Asset.SP500.value,
                MappingKind.INFERRED.value,
                "synthetic_unsafe_evidence",
                Confidence.HIGH.value,
                Confidence.HIGH.value,
                Confidence.HIGH.value,
                0,
                unsafe_evidence,
                source.source_video_id,
            ),
        )

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    MappingReviewDecision,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.audit import AuditRepository
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from tests.backend.integration.test_asset_mapping_storage import (
    _japan_equity_specs,
    _prepare_and_map,
)


FIXED_UTC = datetime(2026, 8, 15, 6, 7, 8, 901234, tzinfo=timezone.utc)
PRACTICAL_PYTHON_WHITESPACE = "".join(
    chr(codepoint)
    for codepoint in (
        9,
        10,
        11,
        12,
        13,
        28,
        29,
        30,
        31,
        32,
        133,
        160,
        5760,
        8192,
        8193,
        8194,
        8195,
        8196,
        8197,
        8198,
        8199,
        8200,
        8201,
        8202,
        8232,
        8233,
        8239,
        8287,
        12288,
    )
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _mapping_with_confidence(db, confidence: Confidence):
    if confidence in {Confidence.HIGH, Confidence.MEDIUM}:
        _, mappings = _prepare_and_map(db, _japan_equity_specs())
        return next(
            mapping
            for mapping in mappings
            if mapping.final_confidence is confidence
        )

    if confidence is Confidence.LOW:
        specs = (
            (
                "Synthetic low-confidence mapping evidence.",
                "日経平均",
                ((Asset.NIKKEI_225, Confidence.LOW),),
                "future_forecast",
            ),
        )
    else:
        specs = (
            (
                "Synthetic unresolved mapping evidence.",
                "株式市場",
                ((Asset.SP500, Confidence.UNRESOLVED),),
                "future_forecast",
            ),
        )
    _, mappings = _prepare_and_map(db, specs)
    assert len(mappings) == 1
    assert mappings[0].final_confidence is confidence
    return mappings[0]


@pytest.fixture
def low_mapping(db):
    return _mapping_with_confidence(db, Confidence.LOW)


@pytest.fixture
def unresolved_mapping(db):
    return _mapping_with_confidence(db, Confidence.UNRESOLVED)


def _command(
    mapping_id: int,
    decision: object,
    *,
    actor: object = "user",
    reason: object = "Synthetic review reason",
    corrected_asset: object = None,
) -> MappingReviewCommand:
    return MappingReviewCommand(
        mapping_id=mapping_id,
        decision=decision,
        actor=actor,
        reason=reason,
        corrected_asset=corrected_asset,
    )


def _service(db, *, clock=lambda: FIXED_UTC):
    return MappingReviewService(db, clock=clock)


def test_low_mapping_is_ineligible_until_approved(db, low_mapping):
    service = _service(db)

    before = service.effective(low_mapping.id)
    review_id = service.review(
        _command(low_mapping.id, MappingReviewDecision.APPROVE)
    )
    after = service.effective(low_mapping.id)

    assert before.asset is Asset.NIKKEI_225
    assert before.heatmap_eligible is False
    assert before.reason_code == "REVIEW_REQUIRED"
    assert review_id > 0
    assert after.asset is Asset.NIKKEI_225
    assert after.heatmap_eligible is True
    assert after.reason_code == "REVIEW_APPROVED"


def test_unresolved_mapping_is_ineligible_without_review(db, unresolved_mapping):
    effective = _service(db).effective(unresolved_mapping.id)

    assert effective.asset is Asset.SP500
    assert effective.heatmap_eligible is False
    assert effective.reason_code == "REVIEW_REQUIRED"


@pytest.mark.parametrize("confidence", [Confidence.HIGH, Confidence.MEDIUM])
def test_high_and_medium_mappings_are_auto_eligible(db, confidence):
    mapping = _mapping_with_confidence(db, confidence)

    effective = _service(db).effective(mapping.id)

    assert effective.asset is mapping.asset
    assert effective.heatmap_eligible is True
    assert effective.reason_code == "AUTO_CONFIDENCE"


def test_correct_reject_and_approve_append_sequential_effective_history(
    db, unresolved_mapping
):
    service = _service(db)
    calculated_before = db.execute(
        "SELECT asset, final_confidence FROM analysis_asset_mappings WHERE id=?",
        (unresolved_mapping.id,),
    ).fetchone()

    corrected_id = service.review(
        _command(
            unresolved_mapping.id,
            MappingReviewDecision.CORRECT,
            corrected_asset=Asset.TOPIX,
            reason="Synthetic correction",
        )
    )
    corrected = service.effective(unresolved_mapping.id)
    rejected_id = service.review(
        _command(
            unresolved_mapping.id,
            MappingReviewDecision.REJECT,
            reason="Synthetic rejection",
        )
    )
    rejected = service.effective(unresolved_mapping.id)
    approved_id = service.review(
        _command(
            unresolved_mapping.id,
            MappingReviewDecision.APPROVE,
            reason="Synthetic approval reset",
        )
    )
    approved = service.effective(unresolved_mapping.id)

    assert corrected_id < rejected_id < approved_id
    assert corrected.asset is Asset.TOPIX
    assert corrected.heatmap_eligible is True
    assert corrected.reason_code == "REVIEW_CORRECTED"
    assert rejected.asset is Asset.TOPIX
    assert rejected.heatmap_eligible is False
    assert rejected.reason_code == "REVIEW_REJECTED"
    assert approved.asset is Asset.SP500
    assert approved.heatmap_eligible is True
    assert approved.reason_code == "REVIEW_APPROVED"
    assert [tuple(row) for row in db.execute(
        """
        SELECT decision, before_asset, after_asset
        FROM mapping_reviews
        WHERE mapping_id=?
        ORDER BY id
        """,
        (unresolved_mapping.id,),
    )] == [
        ("correct", "sp500", "topix"),
        ("reject", "topix", "topix"),
        ("approve", "topix", "sp500"),
    ]
    calculated_after = db.execute(
        "SELECT asset, final_confidence FROM analysis_asset_mappings WHERE id=?",
        (unresolved_mapping.id,),
    ).fetchone()
    assert tuple(calculated_after) == tuple(calculated_before)


@pytest.mark.parametrize(
    "case",
    [
        "missing_mapping",
        "bool_mapping_id",
        "string_decision",
        "blank_actor",
        "whitespace_actor",
        "unsafe_actor",
        "blank_reason",
        "whitespace_reason",
        "approve_with_asset",
        "reject_with_asset",
        "correct_without_asset",
        "correct_with_string_asset",
        "correct_to_calculated_asset",
    ],
)
def test_invalid_mapping_review_commands_fail_with_safe_code(
    db, unresolved_mapping, case
):
    mapping_id = unresolved_mapping.id
    values = {
        "mapping_id": mapping_id,
        "decision": MappingReviewDecision.APPROVE,
        "actor": "user",
        "reason": "Synthetic valid reason",
        "corrected_asset": None,
    }
    if case == "missing_mapping":
        values["mapping_id"] = 999999
    elif case == "bool_mapping_id":
        values["mapping_id"] = True
    elif case == "string_decision":
        values["decision"] = "approve"
    elif case == "blank_actor":
        values["actor"] = ""
    elif case == "whitespace_actor":
        values["actor"] = " \t"
    elif case == "unsafe_actor":
        values["actor"] = "administrator"
    elif case == "blank_reason":
        values["reason"] = ""
    elif case == "whitespace_reason":
        values["reason"] = " \n"
    elif case == "approve_with_asset":
        values["corrected_asset"] = Asset.TOPIX
    elif case == "reject_with_asset":
        values["decision"] = MappingReviewDecision.REJECT
        values["corrected_asset"] = Asset.TOPIX
    elif case == "correct_without_asset":
        values["decision"] = MappingReviewDecision.CORRECT
    elif case == "correct_with_string_asset":
        values["decision"] = MappingReviewDecision.CORRECT
        values["corrected_asset"] = "topix"
    elif case == "correct_to_calculated_asset":
        values["decision"] = MappingReviewDecision.CORRECT
        values["corrected_asset"] = Asset.SP500

    with pytest.raises(DomainError) as error:
        _service(db).review(MappingReviewCommand(**values))

    assert error.value.code == "MAPPING_REVIEW_INVALID"
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM audit_events WHERE entity_type='analysis_asset_mapping'"
    ).fetchone()[0] == 0


def test_correct_cannot_repeat_current_asset_or_return_to_calculated_asset(
    db, unresolved_mapping
):
    service = _service(db)
    service.review(
        _command(
            unresolved_mapping.id,
            MappingReviewDecision.CORRECT,
            corrected_asset=Asset.TOPIX,
        )
    )

    for asset in (Asset.TOPIX, Asset.SP500):
        with pytest.raises(DomainError) as error:
            service.review(
                _command(
                    unresolved_mapping.id,
                    MappingReviewDecision.CORRECT,
                    corrected_asset=asset,
                )
            )
        assert error.value.code == "MAPPING_REVIEW_INVALID"

    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 1


@pytest.mark.parametrize("confidence", [Confidence.HIGH, Confidence.MEDIUM])
def test_auto_eligible_mappings_cannot_be_reviewed(db, confidence):
    mapping = _mapping_with_confidence(db, confidence)

    with pytest.raises(DomainError) as error:
        _service(db).review(
            _command(mapping.id, MappingReviewDecision.REJECT)
        )

    assert error.value.code == "MAPPING_REVIEW_INVALID"
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_review_and_safe_audit_event_commit_together(db, low_mapping):
    review_id = _service(db).review(
        _command(
            low_mapping.id,
            MappingReviewDecision.CORRECT,
            actor="system",
            reason="Synthetic system correction",
            corrected_asset=Asset.XAU_USD,
        )
    )

    review = db.execute(
        "SELECT * FROM mapping_reviews WHERE id=?", (review_id,)
    ).fetchone()
    events = AuditRepository(db).list_for_entity(
        "analysis_asset_mapping", str(low_mapping.id)
    )

    assert set(review.keys()) == {
        "id",
        "mapping_id",
        "decision",
        "actor",
        "reason",
        "before_asset",
        "after_asset",
        "created_at",
    }
    assert tuple(review) == (
        review_id,
        low_mapping.id,
        "correct",
        "system",
        "Synthetic system correction",
        "nikkei_225",
        "xau_usd",
        "2026-08-15T06:07:08.901234Z",
    )
    assert len(events) == 1
    assert events[0].operation == "review"
    assert events[0].actor_kind == "system"
    assert events[0].reason_code == "correct"
    assert events[0].reason_text == "Synthetic system correction"
    assert events[0].before == {
        "asset": "nikkei_225",
        "mapping_id": low_mapping.id,
    }
    assert events[0].after == {
        "actor": "system",
        "asset": "xau_usd",
        "decision": "correct",
        "mapping_id": low_mapping.id,
        "reason": "Synthetic system correction",
    }
    serialized = json.dumps(
        {"review": dict(review), "audit": events[0].after},
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (
        "transcript",
        "excerpt",
        "path",
        "text_body",
        "input_text",
        "audio_path",
        "embedding",
        "prompt_body",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("failure_target", ["review", "audit"])
def test_review_or_audit_insert_failure_rolls_back_both_rows(
    db, low_mapping, failure_target
):
    table = "mapping_reviews" if failure_target == "review" else "audit_events"
    condition = (
        "1"
        if failure_target == "review"
        else "NEW.entity_type='analysis_asset_mapping'"
    )
    db.execute(
        f"""
        CREATE TRIGGER synthetic_mapping_review_{failure_target}_failure
        BEFORE INSERT ON {table}
        WHEN {condition}
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_REVIEW_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as error:
        _service(db).review(
            _command(low_mapping.id, MappingReviewDecision.APPROVE)
        )

    assert error.value.code == "MAPPING_REVIEW_STORAGE_FAILED"
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM audit_events WHERE entity_type='analysis_asset_mapping'"
    ).fetchone()[0] == 0


def test_raw_sql_constraints_reject_invalid_review_rows(db, low_mapping):
    valid = {
        "mapping_id": low_mapping.id,
        "decision": "approve",
        "actor": "user",
        "reason": "Synthetic valid reason",
        "before_asset": "nikkei_225",
        "after_asset": "nikkei_225",
        "created_at": "2026-08-15T06:07:08.901234Z",
    }
    invalid_overrides = (
        {"mapping_id": 999999},
        {"decision": "invalid"},
        {"actor": ""},
        {"actor": "administrator"},
        {"reason": " \t"},
        {"before_asset": "nasdaq"},
        {"after_asset": "nasdaq"},
        {"decision": "approve", "after_asset": "topix"},
        {"decision": "correct", "after_asset": "nikkei_225"},
        {"decision": "correct", "before_asset": "topix", "after_asset": "xau_usd"},
        {"decision": "reject", "after_asset": "topix"},
    )

    for overrides in invalid_overrides:
        values = valid | overrides
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO mapping_reviews(
                    mapping_id, decision, actor, reason,
                    before_asset, after_asset, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values.values()),
            )

    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


@pytest.mark.parametrize(
    "reason", ["\u3000", "\u00a0", PRACTICAL_PYTHON_WHITESPACE]
)
def test_raw_sql_rejects_unicode_whitespace_only_reason(
    db, low_mapping, reason
):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO mapping_reviews(
                mapping_id, decision, actor, reason,
                before_asset, after_asset, created_at
            ) VALUES (?, 'approve', 'user', ?, 'nikkei_225', 'nikkei_225', ?)
            """,
            (
                low_mapping.id,
                reason,
                "2026-08-15T06:07:08.901234Z",
            ),
        )

    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_raw_sql_accepts_unicode_whitespace_around_reason_content(
    db, low_mapping
):
    reason = "\u3000Synthetic review content\u00a0"

    db.execute(
        """
        INSERT INTO mapping_reviews(
            mapping_id, decision, actor, reason,
            before_asset, after_asset, created_at
        ) VALUES (?, 'approve', 'user', ?, 'nikkei_225', 'nikkei_225', ?)
        """,
        (
            low_mapping.id,
            reason,
            "2026-08-15T06:07:08.901234Z",
        ),
    )

    assert db.execute(
        "SELECT reason FROM mapping_reviews WHERE mapping_id=?",
        (low_mapping.id,),
    ).fetchone()[0] == reason


def test_raw_sql_rejects_nonpositive_review_id(db, low_mapping):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO mapping_reviews(
                id, mapping_id, decision, actor, reason,
                before_asset, after_asset, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                0,
                low_mapping.id,
                "reject",
                "user",
                "Synthetic zero ID rejection",
                "nikkei_225",
                "nikkei_225",
                "2026-08-15T06:07:08.901234Z",
            ),
        )

    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_raw_sql_rejects_review_id_older_than_existing_mapping_history(
    db, low_mapping
):
    rows = (
        (10, "Synthetic newest review"),
        (5, "Synthetic backdated review"),
    )
    db.execute(
        """
        INSERT INTO mapping_reviews(
            id, mapping_id, decision, actor, reason,
            before_asset, after_asset, created_at
        ) VALUES (?, ?, 'reject', 'user', ?, 'nikkei_225', 'nikkei_225', ?)
        """,
        (
            rows[0][0],
            low_mapping.id,
            rows[0][1],
            "2026-08-15T06:07:08.901234Z",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO mapping_reviews(
                id, mapping_id, decision, actor, reason,
                before_asset, after_asset, created_at
            ) VALUES (?, ?, 'reject', 'user', ?, 'nikkei_225', 'nikkei_225', ?)
            """,
            (
                rows[1][0],
                low_mapping.id,
                rows[1][1],
                "2026-08-15T06:07:09.901234Z",
            ),
        )

    assert [row["id"] for row in db.execute(
        "SELECT id FROM mapping_reviews WHERE mapping_id=? ORDER BY id",
        (low_mapping.id,),
    )] == [10]


def test_service_auto_ids_remain_increasing_and_latest(db, low_mapping):
    service = _service(db)

    rejected_id = service.review(
        _command(low_mapping.id, MappingReviewDecision.REJECT)
    )
    approved_id = service.review(
        _command(low_mapping.id, MappingReviewDecision.APPROVE)
    )

    assert 0 < rejected_id < approved_id
    assert db.execute(
        "SELECT MAX(id) FROM mapping_reviews WHERE mapping_id=?",
        (low_mapping.id,),
    ).fetchone()[0] == approved_id
    effective = service.effective(low_mapping.id)
    assert effective.heatmap_eligible is True
    assert effective.reason_code == "REVIEW_APPROVED"


def test_review_rows_and_calculated_mapping_reject_raw_mutation_and_replace(
    db, low_mapping
):
    review_id = _service(db).review(
        _command(low_mapping.id, MappingReviewDecision.REJECT)
    )

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE mapping_reviews SET reason=reason WHERE id=?", (review_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM mapping_reviews WHERE id=?", (review_id,))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            INSERT OR REPLACE INTO mapping_reviews(
                id, mapping_id, decision, actor, reason,
                before_asset, after_asset, created_at
            )
            SELECT id, mapping_id, decision, actor, reason,
                   before_asset, after_asset, created_at
            FROM mapping_reviews
            WHERE id=?
            """,
            (review_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            """
            UPDATE analysis_asset_mappings
            SET asset='topix', final_confidence='high'
            WHERE id=?
            """,
            (low_mapping.id,),
        )

    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert tuple(db.execute(
        "SELECT decision, before_asset, after_asset FROM mapping_reviews WHERE id=?",
        (review_id,),
    ).fetchone()) == ("reject", "nikkei_225", "nikkei_225")
    assert tuple(db.execute(
        "SELECT asset, final_confidence FROM analysis_asset_mappings WHERE id=?",
        (low_mapping.id,),
    ).fetchone()) == ("nikkei_225", "low")


def test_effective_fails_closed_when_stored_history_is_semantically_malformed(
    db, unresolved_mapping
):
    review_id = _service(db).review(
        _command(
            unresolved_mapping.id,
            MappingReviewDecision.CORRECT,
            corrected_asset=Asset.TOPIX,
        )
    )
    db.execute("DROP TRIGGER mapping_reviews_no_update")
    db.execute(
        "UPDATE mapping_reviews SET before_asset='xau_usd' WHERE id=?",
        (review_id,),
    )

    with pytest.raises(DomainError) as error:
        _service(db).effective(unresolved_mapping.id)

    assert error.value.code == "MAPPING_REVIEW_STORED_INVALID"

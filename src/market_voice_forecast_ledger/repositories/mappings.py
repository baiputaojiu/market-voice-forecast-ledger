import json
import sqlite3

from market_voice_forecast_ledger.domain.common import canonical_json
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    MappingKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.mappings import (
    AssetMapping,
    MarketCode,
    RuleEvidence,
    RuleEvidenceKind,
)


class MappingRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, mapping: AssetMapping) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_asset_mappings(
                run_id,
                statement_id,
                original_expression,
                asset,
                mapping_kind,
                conversion_reason,
                codex_confidence,
                rule_confidence,
                final_confidence,
                confidence_disagrees,
                rule_evidence_json,
                source_video_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mapping.run_id,
                mapping.statement_id,
                mapping.original_expression,
                mapping.asset.value,
                mapping.mapping_kind.value,
                mapping.reason_code,
                mapping.codex_confidence.value,
                mapping.rule_confidence.value,
                mapping.final_confidence.value,
                int(mapping.confidence_disagrees),
                canonical_json(
                    [row.to_safe_dict() for row in mapping.rule_evidence]
                ),
                mapping.source_video_id,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("asset mapping insert did not return an id")
        return cursor.lastrowid

    def get(self, mapping_id: int) -> AssetMapping:
        row = self._conn.execute(
            "SELECT * FROM analysis_asset_mappings WHERE id=?",
            (mapping_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "ASSET_MAPPING_NOT_FOUND", "asset mapping does not exist"
            )
        return _mapping_from_row(row)

    def list_run_mappings(self, run_id: int) -> tuple[AssetMapping, ...]:
        rows = self._conn.execute(
            """
            SELECT mapping.*
            FROM analysis_asset_mappings AS mapping
            JOIN analysis_statements AS statement
                ON statement.id=mapping.statement_id
            WHERE mapping.run_id=?
            ORDER BY
                statement.ordinal,
                CASE mapping.asset
                    WHEN 'nikkei_225' THEN 1
                    WHEN 'topix' THEN 2
                    WHEN 'sp500' THEN 3
                    WHEN 'xau_usd' THEN 4
                END,
                mapping.id
            """,
            (run_id,),
        ).fetchall()
        return tuple(_mapping_from_row(row) for row in rows)

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "ASSET_MAPPING_TRANSACTION_REQUIRED",
                "asset mapping mutation requires an active caller transaction",
            )


def _mapping_from_row(row: sqlite3.Row) -> AssetMapping:
    return AssetMapping(
        id=row["id"],
        run_id=row["run_id"],
        statement_id=row["statement_id"],
        original_expression=row["original_expression"],
        asset=Asset(row["asset"]),
        mapping_kind=MappingKind(row["mapping_kind"]),
        reason_code=row["conversion_reason"],
        codex_confidence=Confidence(row["codex_confidence"]),
        rule_confidence=Confidence(row["rule_confidence"]),
        final_confidence=Confidence(row["final_confidence"]),
        confidence_disagrees=bool(row["confidence_disagrees"]),
        rule_evidence=_parse_rule_evidence(row["rule_evidence_json"]),
        source_video_id=row["source_video_id"],
    )


def _parse_rule_evidence(value: str) -> tuple[RuleEvidence, ...]:
    try:
        payload = json.loads(value)
        if not isinstance(payload, list):
            raise ValueError
        evidence: list[RuleEvidence] = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {
                "segment_id",
                "evidence_kind",
                "market_code",
                "is_competing",
            }:
                raise ValueError
            segment_id = item["segment_id"]
            is_competing = item["is_competing"]
            if (
                not isinstance(segment_id, int)
                or isinstance(segment_id, bool)
                or segment_id <= 0
                or not isinstance(is_competing, bool)
            ):
                raise ValueError
            evidence.append(
                RuleEvidence(
                    segment_id=segment_id,
                    evidence_kind=RuleEvidenceKind(item["evidence_kind"]),
                    market_code=MarketCode(item["market_code"]),
                    is_competing=is_competing,
                )
            )
        return tuple(evidence)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as cause:
        raise DomainError(
            "ASSET_MAPPING_EVIDENCE_INVALID",
            "stored mapping evidence is not a safe rule payload",
        ) from cause

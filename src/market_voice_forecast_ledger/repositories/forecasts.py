import json
import sqlite3
from datetime import date, datetime

from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    MappingKind,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import (
    ForecastProjectionBatch,
    ProjectedForecast,
    ProjectionTrigger,
)


class ForecastRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_batch(
        self,
        run_id: int,
        trigger_kind: ProjectionTrigger,
        latest_mapping_review_id: int | None,
        latest_period_review_id: int | None,
        created_at: datetime,
    ) -> int:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            INSERT INTO forecast_projection_batches(
                run_id,
                trigger_kind,
                latest_mapping_review_id,
                latest_period_review_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                trigger_kind.value,
                latest_mapping_review_id,
                latest_period_review_id,
                utc_iso(created_at),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("projection batch insert did not return an id")
        return cursor.lastrowid

    def insert_forecast(self, forecast: ProjectedForecast) -> int:
        self._require_transaction()
        if forecast.id is not None:
            raise DomainError(
                "FORECAST_ALREADY_STORED",
                "an immutable forecast cannot be inserted twice",
            )
        if forecast.evidence_count != len(
            set(forecast.supporting_statement_ids)
        ):
            raise DomainError(
                "FORECAST_EVIDENCE_COUNT_INVALID",
                "forecast evidence count must match distinct support",
            )
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_forecasts(
                projection_batch_id,
                run_id,
                asset,
                mapping_kind,
                period_start,
                period_end,
                unknown_period,
                condition_kind,
                condition_text,
                view_relation,
                primary_direction,
                directions_json,
                confidence,
                evidence_count,
                selected_published_at,
                selected_forecast_basis,
                period_specificity,
                stable_selection_key,
                heatmap_eligible,
                exclusion_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forecast.projection_batch_id,
                forecast.run_id,
                forecast.asset.value,
                forecast.mapping_kind.value,
                _date_text(forecast.period_start),
                _date_text(forecast.period_end),
                int(forecast.unknown_period),
                forecast.condition_kind.value,
                forecast.condition_text,
                forecast.view_relation.value,
                forecast.primary_direction.value,
                canonical_json(
                    [direction.value for direction in forecast.directions]
                ),
                forecast.confidence.value,
                forecast.evidence_count,
                utc_iso(forecast.selected_published_at),
                forecast.selected_forecast_basis.value,
                forecast.period_specificity,
                forecast.stable_selection_key,
                int(forecast.heatmap_eligible),
                forecast.exclusion_reason,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("forecast insert did not return an id")
        forecast_id = cursor.lastrowid
        self._insert_links(
            forecast_id,
            "supporting",
            forecast.supporting_statement_ids,
        )
        self._insert_links(
            forecast_id,
            "counterevidence",
            forecast.counterevidence_statement_ids,
        )
        return forecast_id

    def get_batch(self, batch_id: int) -> ForecastProjectionBatch:
        row = self._conn.execute(
            "SELECT * FROM forecast_projection_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise DomainError(
                "FORECAST_BATCH_NOT_FOUND", "forecast batch does not exist"
            )
        return ForecastProjectionBatch(
            id=row["id"],
            run_id=row["run_id"],
            trigger_kind=ProjectionTrigger(row["trigger_kind"]),
            latest_mapping_review_id=row["latest_mapping_review_id"],
            latest_period_review_id=row["latest_period_review_id"],
            created_at=_parse_utc(row["created_at"]),
            forecasts=self.list_batch_forecasts(row["id"]),
        )

    def initial_batch(self, run_id: int) -> ForecastProjectionBatch:
        rows = self._conn.execute(
            """
            SELECT id
            FROM forecast_projection_batches
            WHERE run_id=? AND trigger_kind='initial'
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        if len(rows) != 1:
            raise DomainError(
                "FORECAST_INITIAL_BATCH_INVALID",
                "a successful projection requires exactly one initial batch",
            )
        return self.get_batch(rows[0]["id"])

    def list_batch_forecasts(
        self, batch_id: int
    ) -> tuple[ProjectedForecast, ...]:
        rows = self._conn.execute(
            """
            SELECT forecast.*, scope.subject_id
            FROM analysis_forecasts AS forecast
            JOIN forecast_projection_batches AS batch
                ON batch.id=forecast.projection_batch_id
            JOIN analysis_runs AS run ON run.id=forecast.run_id
            JOIN analysis_scopes AS scope ON scope.id=run.scope_id
            WHERE forecast.projection_batch_id=?
                AND batch.run_id=forecast.run_id
            ORDER BY forecast.id
            """,
            (batch_id,),
        ).fetchall()
        links = self._links_by_forecast(batch_id)
        return tuple(
            _forecast_from_row(row, links.get(row["id"], ())) for row in rows
        )

    def batch_artifact_hash(self, batch_id: int) -> str:
        return sha256_text(canonical_json(self.batch_artifact(batch_id)))

    def batch_artifact(self, batch_id: int) -> dict[str, object]:
        batch = self.get_batch(batch_id)
        links_by_forecast = self._links_by_forecast(batch_id)
        return {
            "batch": {
                "id": batch.id,
                "run_id": batch.run_id,
                "trigger_kind": batch.trigger_kind.value,
                "latest_mapping_review_id": batch.latest_mapping_review_id,
                "latest_period_review_id": batch.latest_period_review_id,
                "created_at": utc_iso(batch.created_at),
            },
            "forecasts": [
                {
                    "id": forecast.id,
                    "run_id": forecast.run_id,
                    "asset": forecast.asset.value,
                    "mapping_kind": forecast.mapping_kind.value,
                    "period_start": _date_text(forecast.period_start),
                    "period_end": _date_text(forecast.period_end),
                    "unknown_period": forecast.unknown_period,
                    "condition_kind": forecast.condition_kind.value,
                    "condition_text": forecast.condition_text,
                    "view_relation": forecast.view_relation.value,
                    "primary_direction": forecast.primary_direction.value,
                    "directions": [
                        direction.value for direction in forecast.directions
                    ],
                    "confidence": forecast.confidence.value,
                    "evidence_count": forecast.evidence_count,
                    "selected_published_at": utc_iso(
                        forecast.selected_published_at
                    ),
                    "selected_forecast_basis": (
                        forecast.selected_forecast_basis.value
                    ),
                    "period_specificity": forecast.period_specificity,
                    "stable_selection_key": forecast.stable_selection_key,
                    "heatmap_eligible": forecast.heatmap_eligible,
                    "exclusion_reason": forecast.exclusion_reason,
                    "supporting_statement_ids": list(
                        forecast.supporting_statement_ids
                    ),
                    "counterevidence_statement_ids": list(
                        forecast.counterevidence_statement_ids
                    ),
                    "statement_links": [
                        {
                            "statement_id": link["statement_id"],
                            "relation_kind": link["relation_kind"],
                            "ordinal": link["ordinal"],
                        }
                        for link in links_by_forecast.get(
                            forecast.id, ()
                        )
                    ],
                }
                for forecast in batch.forecasts
            ],
        }

    def _insert_links(
        self,
        forecast_id: int,
        relation_kind: str,
        statement_ids: tuple[int, ...],
    ) -> None:
        if len(set(statement_ids)) != len(statement_ids):
            raise DomainError(
                "FORECAST_EVIDENCE_DUPLICATED",
                "forecast evidence links must be distinct",
            )
        self._conn.executemany(
            """
            INSERT INTO analysis_forecast_statement_links(
                forecast_id, statement_id, relation_kind, ordinal
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (forecast_id, statement_id, relation_kind, ordinal)
                for ordinal, statement_id in enumerate(statement_ids, start=1)
            ),
        )

    def _links_by_forecast(
        self, batch_id: int
    ) -> dict[int, tuple[sqlite3.Row, ...]]:
        rows = self._conn.execute(
            """
            SELECT link.*
            FROM analysis_forecast_statement_links AS link
            JOIN analysis_forecasts AS forecast
                ON forecast.id=link.forecast_id
            WHERE forecast.projection_batch_id=?
            ORDER BY
                forecast.id,
                CASE link.relation_kind
                    WHEN 'supporting' THEN 1
                    ELSE 2
                END,
                link.ordinal
            """,
            (batch_id,),
        ).fetchall()
        by_forecast: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_forecast.setdefault(row["forecast_id"], []).append(row)
        return {
            forecast_id: _validated_link_rows(tuple(forecast_rows))
            for forecast_id, forecast_rows in by_forecast.items()
        }

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "FORECAST_REPOSITORY_TRANSACTION_REQUIRED",
                "forecast mutation requires a caller-owned transaction",
            )


def _forecast_from_row(
    row: sqlite3.Row, links: tuple[sqlite3.Row, ...]
) -> ProjectedForecast:
    directions = _parse_directions(row["directions_json"])
    supporting = tuple(
        link["statement_id"]
        for link in links
        if link["relation_kind"] == "supporting"
    )
    counterevidence = tuple(
        link["statement_id"]
        for link in links
        if link["relation_kind"] == "counterevidence"
    )
    if row["evidence_count"] != len(set(supporting)):
        raise DomainError(
            "FORECAST_ARTIFACT_INVALID",
            "stored forecast evidence count does not match its links",
        )
    return ProjectedForecast(
        id=row["id"],
        projection_batch_id=row["projection_batch_id"],
        run_id=row["run_id"],
        subject_id=row["subject_id"],
        asset=Asset(row["asset"]),
        mapping_kind=MappingKind(row["mapping_kind"]),
        period_start=_parse_date(row["period_start"]),
        period_end=_parse_date(row["period_end"]),
        unknown_period=bool(row["unknown_period"]),
        condition_kind=ConditionKind(row["condition_kind"]),
        condition_text=row["condition_text"],
        view_relation=ViewRelation(row["view_relation"]),
        primary_direction=DirectionKind(row["primary_direction"]),
        directions=directions,
        confidence=Confidence(row["confidence"]),
        evidence_count=row["evidence_count"],
        selected_published_at=_parse_utc(row["selected_published_at"]),
        selected_forecast_basis=ForecastBasis(
            row["selected_forecast_basis"]
        ),
        period_specificity=row["period_specificity"],
        stable_selection_key=row["stable_selection_key"],
        heatmap_eligible=bool(row["heatmap_eligible"]),
        exclusion_reason=row["exclusion_reason"],
        supporting_statement_ids=supporting,
        counterevidence_statement_ids=counterevidence,
        source_forecast_ids=(row["id"],),
    )


def _validated_link_rows(
    links: tuple[sqlite3.Row, ...],
) -> tuple[sqlite3.Row, ...]:
    ordinals_by_relation: dict[str, list[int]] = {
        "supporting": [],
        "counterevidence": [],
    }
    for link in links:
        relation_kind = link["relation_kind"]
        statement_id = link["statement_id"]
        ordinal = link["ordinal"]
        if (
            relation_kind not in ordinals_by_relation
            or not _is_positive_int(statement_id)
            or not _is_positive_int(ordinal)
        ):
            raise DomainError(
                "FORECAST_ARTIFACT_INVALID",
                "stored forecast evidence links are invalid",
            )
        ordinals_by_relation[relation_kind].append(ordinal)
    if any(
        len(set(ordinals)) != len(ordinals)
        or sorted(ordinals) != list(range(1, len(ordinals) + 1))
        for ordinals in ordinals_by_relation.values()
    ):
        raise DomainError(
            "FORECAST_ARTIFACT_INVALID",
            "stored forecast evidence ordinals are not contiguous",
        )
    return links


def _parse_directions(value: str) -> tuple[DirectionKind, ...]:
    try:
        payload = json.loads(value)
        if (
            not isinstance(payload, list)
            or not payload
            or any(not isinstance(item, str) for item in payload)
            or canonical_json(payload) != value
        ):
            raise ValueError
        directions = tuple(DirectionKind(item) for item in payload)
        if len(set(directions)) != len(directions):
            raise ValueError
        return directions
    except (json.JSONDecodeError, TypeError, ValueError) as cause:
        raise DomainError(
            "FORECAST_DIRECTIONS_STORED_INVALID",
            "stored forecast directions are invalid",
        ) from cause


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

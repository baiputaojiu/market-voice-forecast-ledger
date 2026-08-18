import sqlite3
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.e2e.synthetic_fixture import SyntheticLedgerFixture


def _video_manifest() -> JobManifest:
    return JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "video:metadata",
                JobStage.VIDEO_METADATA,
                1,
                "synthetic-video-input",
                (),
                "synthetic-video-contract-v1",
            ),
        ),
    )


def test_migration_runner_records_0018_once_and_remains_idempotent(tmp_path):
    conn = open_database(tmp_path / "runner.sqlite3")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert first[-1] == "0018_youtube_discovery_cutover"
        assert second == ()
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='0018_youtube_discovery_cutover'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.fixture(scope="module")
def populated_database(tmp_path_factory) -> Path:
    runtime = tmp_path_factory.mktemp("append-only-guards")
    with SyntheticLedgerFixture(runtime) as fixture:
        fixture.run_complete_flow()
        conn = fixture.connection
        threshold = conn.execute(
            "SELECT version FROM speaker_threshold_configs WHERE is_active=1"
        ).fetchone()[0]
        subject_id = conn.execute(
            "SELECT id FROM analysis_subjects ORDER BY id LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (?, 'synthetic-voice-model', '1.0', 'adapter-v1',
                      'synthetic-feature-hash', ?,
                      '2026-08-15T00:00:00.000000Z', 1)
            """,
            (subject_id, threshold),
        )
        candidate_id = conn.execute(
            "SELECT id FROM subject_video_candidates ORDER BY id LIMIT 1"
        ).fetchone()[0]
        JobStateService(conn).create_video_pipeline(
            _video_manifest(), (candidate_id,)
        )
        conn.execute(
            """
            INSERT INTO jobs(
                id, source_job_id, job_kind, manifest_hash, total_units,
                status, created_at, updated_at
            ) VALUES (
                900101, NULL, 'analysis_scope', 'isolated-attempt-job', 1,
                'queued', '2026-08-15T00:00:00.000000Z',
                '2026-08-15T00:00:00.000000Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs(
                id, source_job_id, job_kind, manifest_hash, total_units,
                status, created_at, updated_at
            ) VALUES (
                900102, NULL, 'analysis_scope', 'open-manifest', 2,
                'queued', '2026-08-15T00:00:00.000000Z',
                '2026-08-15T00:00:00.000000Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO job_units(
                job_id, unit_key, stage, ordinal, dependency_keys_json,
                execution_contract_hash, status
            ) VALUES (
                900102, 'open:unit:1', 'analysis_input_extraction', 1,
                '[]', 'open-manifest-contract', 'pending'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO jobs(
                id, source_job_id, job_kind, manifest_hash, total_units,
                status, created_at, updated_at
            ) VALUES (
                900103, NULL, 'video_pipeline', 'open-binding-job', 1,
                'queued', '2026-08-15T00:00:00.000000Z',
                '2026-08-15T00:00:00.000000Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO video_pipeline_job_binding_sets(
                job_id, expected_binding_count, is_sealed
            ) VALUES (900103, 2, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO video_pipeline_job_bindings(job_id, candidate_id)
            VALUES (900103, ?)
            """,
            (candidate_id,),
        )
        database_path = fixture.settings.database_path
    return database_path


_COLLISION_TABLES = (
    ("schema_migrations", "APPEND_ONLY"),
    ("audit_events", "APPEND_ONLY"),
    ("transcription_chunks", "APPEND_ONLY"),
    ("transcript_segments", "IMMUTABLE_TRANSCRIPT_BODY"),
    ("speaker_threshold_configs", "APPEND_ONLY"),
    ("voice_reference_profiles", "APPEND_ONLY"),
    ("jobs", "IMMUTABLE_JOB_MANIFEST"),
    ("job_units", "IMMUTABLE_JOB_MANIFEST"),
    ("job_unit_attempts", "APPEND_ONLY"),
    ("job_events", "APPEND_ONLY"),
    ("analysis_scopes", "ANALYSIS_SCOPE_GENERATION_INVALID"),
    ("analysis_runs", "IMMUTABLE_ANALYSIS_RUN_GENERATION"),
    ("analysis_run_job_attempts", "APPEND_ONLY"),
    ("analysis_run_events", "APPEND_ONLY"),
    ("analysis_run_segments", "APPEND_ONLY"),
    ("analysis_input_snapshots", "IMMUTABLE_ANALYSIS_SNAPSHOT"),
    ("analysis_run_outputs", "APPEND_ONLY"),
    ("analysis_statements", "APPEND_ONLY"),
    ("analysis_statement_evidence_links", "APPEND_ONLY"),
    ("analysis_statement_periods", "APPEND_ONLY"),
    ("period_reviews", "APPEND_ONLY"),
    ("analysis_asset_mappings", "APPEND_ONLY"),
    ("mapping_reviews", "APPEND_ONLY"),
    ("forecast_projection_batches", "APPEND_ONLY"),
    ("analysis_forecasts", "APPEND_ONLY"),
    ("analysis_forecast_statement_links", "APPEND_ONLY"),
    ("video_pipeline_job_binding_sets", "IMMUTABLE_JOB_BINDING"),
    ("video_pipeline_job_bindings", "IMMUTABLE_JOB_BINDING"),
)


@pytest.mark.parametrize(("table", "error_code"), _COLLISION_TABLES)
def test_plain_sqlite_same_identity_replace_is_rejected(
    populated_database, table, error_code
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 1").fetchone()
        assert row is not None, table
        columns = tuple(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=error_code):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(row),
            )
        conn.execute("ROLLBACK")
        assert tuple(
            conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 1").fetchone()
        ) == tuple(row)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


@pytest.mark.parametrize(
    ("table", "overrides", "error_code"),
    (
        ("transcription_chunks", {"id": 900001}, "APPEND_ONLY"),
        (
            "transcript_segments",
            {"id": 900002},
            "IMMUTABLE_TRANSCRIPT_BODY",
        ),
        (
            "speaker_threshold_configs",
            {"version": "synthetic-colliding-active-threshold"},
            "APPEND_ONLY",
        ),
        ("voice_reference_profiles", {"id": 900003}, "APPEND_ONLY"),
        (
            "job_units",
            {"unit_key": "colliding:unit"},
            "IMMUTABLE_JOB_MANIFEST",
        ),
        ("job_unit_attempts", {"id": 900004}, "APPEND_ONLY"),
        (
            "analysis_scopes",
            {"id": 900005},
            "ANALYSIS_SCOPE_GENERATION_INVALID",
        ),
        (
            "analysis_input_snapshots",
            {"id": 900008},
            "IMMUTABLE_ANALYSIS_SNAPSHOT",
        ),
        ("analysis_run_outputs", {"id": 900009}, "APPEND_ONLY"),
        ("analysis_statement_periods", {"id": 900011}, "APPEND_ONLY"),
        ("analysis_asset_mappings", {"id": 900012}, "APPEND_ONLY"),
        ("analysis_forecasts", {"id": 900013}, "APPEND_ONLY"),
    ),
)
def test_plain_sqlite_alternate_primary_same_logical_identity_is_rejected(
    populated_database, table, overrides, error_code
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 1").fetchone()
        assert row is not None, table
        values = dict(row)
        values.update(overrides)
        columns = tuple(values)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=error_code):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )
        conn.execute("ROLLBACK")
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


@pytest.mark.parametrize(
    ("table", "overrides", "error_code"),
    (
        (
            "transcription_chunks",
            {"video_id": 900001, "chunk_no": 900001},
            "APPEND_ONLY",
        ),
        (
            "transcript_segments",
            {"video_id": 900002, "segment_no": 900002},
            "IMMUTABLE_TRANSCRIPT_BODY",
        ),
        ("speaker_threshold_configs", {"is_active": 0}, "APPEND_ONLY"),
        (
            "voice_reference_profiles",
            {"subject_id": 900003, "is_active": 0},
            "APPEND_ONLY",
        ),
        ("job_units", {"ordinal": 900004}, "IMMUTABLE_JOB_MANIFEST"),
        (
            "job_unit_attempts",
            {
                "job_id": 900005,
                "unit_key": "isolated:attempt",
                "attempt_no": 900005,
            },
            "APPEND_ONLY",
        ),
        (
            "analysis_scopes",
            {"subject_id": 900006, "cutoff_day_jst": "2099-01-01"},
            "ANALYSIS_SCOPE_GENERATION_INVALID",
        ),
        (
            "analysis_input_snapshots",
            {"run_id": 900009},
            "IMMUTABLE_ANALYSIS_SNAPSHOT",
        ),
        (
            "analysis_run_outputs",
            {"run_id": 900010, "unit_key": "isolated:output"},
            "APPEND_ONLY",
        ),
        (
            "analysis_statement_periods",
            {"statement_id": 900012},
            "APPEND_ONLY",
        ),
        (
            "analysis_asset_mappings",
            {"run_id": 900013, "statement_id": 900013},
            "APPEND_ONLY",
        ),
        (
            "analysis_forecasts",
            {"projection_batch_id": 900014},
            "APPEND_ONLY",
        ),
    ),
)
def test_plain_sqlite_primary_identity_collision_is_isolated_from_logical_keys(
    populated_database, table, overrides, error_code
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 1").fetchone()
        values = dict(row)
        values.update(overrides)
        columns = tuple(values)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=error_code):
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"ordinal": 2}, id="primary-pair"),
        pytest.param(
            {"unit_key": "open:unit:alternate"},
            id="job-ordinal",
        ),
    ),
)
def test_plain_sqlite_job_unit_identity_collisions_reject_replace_in_open_manifest(
    populated_database, overrides
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM job_units WHERE job_id=900102"
        ).fetchone()
        assert row is not None
        values = dict(row)
        values.update(overrides)
        columns = tuple(values)
        conn.execute("BEGIN")
        with pytest.raises(
            sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"
        ):
            conn.execute(
                "INSERT OR REPLACE INTO job_units "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_binding_set_identity_rejects_replace_while_open(
    populated_database,
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM video_pipeline_job_binding_sets WHERE job_id=900103"
        ).fetchone()
        assert row is not None
        assert row["is_sealed"] == 0
        columns = tuple(row.keys())
        conn.execute("BEGIN")
        with pytest.raises(
            sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"
        ):
            conn.execute(
                "INSERT OR REPLACE INTO video_pipeline_job_binding_sets "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_binding_identity_rejects_replace_while_set_open(
    populated_database,
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM video_pipeline_job_bindings WHERE job_id=900103"
        ).fetchone()
        assert row is not None
        assert conn.execute(
            "SELECT is_sealed FROM video_pipeline_job_binding_sets "
            "WHERE job_id=900103"
        ).fetchone()[0] == 0
        columns = tuple(row.keys())
        conn.execute("BEGIN")
        with pytest.raises(
            sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"
        ):
            conn.execute(
                "INSERT OR REPLACE INTO video_pipeline_job_bindings "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


@pytest.mark.parametrize(
    ("identity", "overrides"),
    (
        (
            "primary-id",
            {
                "job_id": 900101,
                "run_id": 900102,
                "attempt_ordinal": 1,
            },
        ),
        (
            "job-id",
            {"id": 900103, "run_id": 900103, "attempt_ordinal": 1},
        ),
        (
            "run-attempt-ordinal",
            {"id": 900104, "job_id": 900101},
        ),
    ),
)
def test_plain_sqlite_run_job_attempt_identity_collisions_are_isolated(
    populated_database, identity, overrides
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analysis_run_job_attempts ORDER BY id LIMIT 1"
        ).fetchone()
        values = dict(row)
        values.update(overrides)
        columns = tuple(values)
        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            conn.execute(
                "INSERT OR REPLACE INTO analysis_run_job_attempts "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_run_segment_identity_collisions_are_isolated(
    populated_database
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analysis_run_segments ORDER BY id LIMIT 1"
        ).fetchone()
        other_segment = conn.execute(
            """
            SELECT segment.id, segment.video_id
            FROM transcript_segments AS segment
            WHERE NOT EXISTS (
                SELECT 1
                FROM analysis_run_segments AS existing
                WHERE existing.run_id=? AND existing.segment_id=segment.id
            )
            ORDER BY segment.id
            LIMIT 1
            """,
            (row["run_id"],),
        ).fetchone()
        assert other_segment is not None
        cases = (
            (
                "primary-id",
                {"run_id": 900201, "ordinal": 900201},
            ),
            (
                "run-ordinal",
                {
                    "id": 900202,
                    "segment_id": other_segment["id"],
                    "video_id": other_segment["video_id"],
                },
            ),
            (
                "run-segment",
                {"id": 900203, "ordinal": 900203},
            ),
        )
        for identity, overrides in cases:
            values = dict(row)
            values.update(overrides)
            columns = tuple(values)
            conn.execute("BEGIN")
            with pytest.raises(
                sqlite3.IntegrityError, match="APPEND_ONLY"
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO analysis_run_segments "
                    f"({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_statement_identity_collisions_are_isolated(
    populated_database
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analysis_statements ORDER BY id LIMIT 1"
        ).fetchone()
        cases = (
            ("primary-id", {"run_id": 900301}),
            (
                "run-ordinal",
                {"id": 900302, "proposal_ordinal": 900302},
            ),
            (
                "run-batch-proposal",
                {"id": 900303, "ordinal": 900303},
            ),
        )
        for identity, overrides in cases:
            values = dict(row)
            values.update(overrides)
            columns = tuple(values)
            conn.execute("BEGIN")
            with pytest.raises(
                sqlite3.IntegrityError, match="APPEND_ONLY"
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO analysis_statements "
                    f"({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_evidence_link_unique_identities_are_isolated(
    populated_database
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analysis_statement_evidence_links "
            "ORDER BY statement_id, ordinal LIMIT 1"
        ).fetchone()
        cases = (
            ("statement-ordinal", {"run_segment_id": 900401}),
            ("statement-run-segment", {"ordinal": 900402}),
        )
        for identity, overrides in cases:
            values = dict(row)
            values.update(overrides)
            columns = tuple(values)
            conn.execute("BEGIN")
            with pytest.raises(
                sqlite3.IntegrityError, match="APPEND_ONLY"
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO analysis_statement_evidence_links "
                    f"({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_forecast_link_unique_identities_are_isolated(
    populated_database
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT link.*, forecast.run_id
            FROM analysis_forecast_statement_links AS link
            JOIN analysis_forecasts AS forecast ON forecast.id=link.forecast_id
            WHERE EXISTS (
                SELECT 1
                FROM analysis_statements AS statement
                WHERE statement.run_id=forecast.run_id
                    AND statement.id!=link.statement_id
                    AND NOT EXISTS (
                        SELECT 1
                        FROM analysis_forecast_statement_links AS used
                        WHERE used.forecast_id=link.forecast_id
                            AND used.statement_id=statement.id
                    )
            )
            ORDER BY link.forecast_id, link.ordinal
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        alternate_statement_id = conn.execute(
            """
            SELECT statement.id
            FROM analysis_statements AS statement
            WHERE statement.run_id=?
                AND statement.id!=?
                AND NOT EXISTS (
                    SELECT 1
                    FROM analysis_forecast_statement_links AS used
                    WHERE used.forecast_id=?
                        AND used.statement_id=statement.id
                )
            ORDER BY statement.id
            LIMIT 1
            """,
            (row["run_id"], row["statement_id"], row["forecast_id"]),
        ).fetchone()[0]
        base = {
            key: row[key]
            for key in ("forecast_id", "statement_id", "relation_kind", "ordinal")
        }
        cases = (
            ("primary-pair", {"ordinal": 900501}),
            (
                "forecast-relation-ordinal",
                {"statement_id": alternate_statement_id},
            ),
        )
        for identity, overrides in cases:
            values = base | overrides
            columns = tuple(values)
            conn.execute("BEGIN")
            with pytest.raises(
                sqlite3.IntegrityError, match="APPEND_ONLY"
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO analysis_forecast_statement_links "
                    f"({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

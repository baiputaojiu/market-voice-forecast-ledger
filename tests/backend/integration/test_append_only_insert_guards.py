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


def test_migration_runner_records_current_migrations_once_and_remains_idempotent(
    tmp_path,
):
    conn = open_database(tmp_path / "runner.sqlite3")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert "0018_youtube_discovery_cutover" in first
        assert first[-1] == "0020_presence_verification"
        assert second == ()
        for migration_name in (
            "0018_youtube_discovery_cutover",
            "0019_market_masters_seed_channel",
            "0020_presence_verification",
        ):
            assert conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (migration_name,),
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
            "SELECT version, model_name, model_version "
            "FROM speaker_threshold_configs WHERE is_active=1"
        ).fetchone()
        profile_version_id = conn.execute(
            "SELECT id FROM discovery_profile_versions ORDER BY id LIMIT 1"
        ).fetchone()[0]
        if conn.execute(
            "SELECT COUNT(*) FROM discovery_seed_channels"
        ).fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO discovery_seed_channels("
                "profile_version_id, ordinal, youtube_channel_id"
                ") VALUES (?, 1, 'UCabcdefghijklmnopqrstuv')",
                (profile_version_id,),
            )
        candidate = conn.execute(
            """
            SELECT candidate.id, candidate.video_id, candidate.profile_id,
                   candidate.current_presence_decision_id,
                   profile.subject_id,
                   decision.decision_hash
            FROM subject_video_candidates AS candidate
            JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id
            JOIN presence_decisions AS decision
              ON decision.id=candidate.current_presence_decision_id
            ORDER BY candidate.id
            LIMIT 1
            """
        ).fetchone()
        feature_hash = "f" * 64
        reference_profile_id = conn.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (?, ?, ?, 'adapter-v1', ?, ?,
                      '2026-08-15T00:00:00.000000Z', 1)
            """,
            (
                candidate["subject_id"],
                threshold["model_name"],
                threshold["model_version"],
                feature_hash,
                threshold["version"],
            ),
        ).lastrowid
        manifest = _video_manifest()
        voice_job_id = JobStateService(conn).create_video_pipeline(
            manifest, (candidate["id"],)
        )
        conn.execute(
            """
            INSERT INTO voice_reference_clips(
                reference_profile_id, ordinal, clip_kind, subject_id, video_id,
                start_ms, end_ms, normalized_audio_sha256, approval_actor,
                approval_reason, approved_at, clip_hash
            ) VALUES (?, 1, 'enrollment', ?, ?, 1000, 3000, ?, 'local_user',
                      'clear solo speech', '2026-08-15T00:00:00.000000Z', ?)
            """,
            (
                reference_profile_id,
                candidate["subject_id"],
                candidate["video_id"],
                "a" * 64,
                "b" * 64,
            ),
        )
        conn.execute(
            """
            INSERT INTO voice_reference_features(
                reference_profile_id, encoding_version, float_dtype, dimension,
                embedding_blob, feature_sha256, created_at
            ) VALUES (?, 'embedding-v1', 'float32', 4, ?, ?,
                      '2026-08-15T00:00:00.000000Z')
            """,
            (reference_profile_id, sqlite3.Binary(b"\x00" * 16), feature_hash),
        )
        conn.execute(
            """
            INSERT INTO voice_verification_manifests(
                job_id, candidate_id, video_id, profile_id,
                presence_decision_id, presence_decision_hash,
                reference_profile_id, reference_feature_hash,
                threshold_config_version, model_name, model_version,
                adapter_version, vad_contract_version,
                selection_contract_version, manifest_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'adapter-v1',
                      'vad-v1', 'selection-v1', ?,
                      '2026-08-15T00:00:00.000000Z')
            """,
            (
                voice_job_id,
                candidate["id"],
                candidate["video_id"],
                candidate["profile_id"],
                candidate["current_presence_decision_id"],
                candidate["decision_hash"],
                reference_profile_id,
                feature_hash,
                threshold["version"],
                threshold["model_name"],
                threshold["model_version"],
                manifest.manifest_hash,
            ),
        )
        run_id = conn.execute(
            """
            INSERT INTO voice_verification_runs(
                job_id, candidate_id, input_hash, output_hash, proposal,
                result_code, completed_at
            ) VALUES (?, ?, ?, ?, 'likely_present', 'VOICE_PROPOSAL_READY',
                      '2026-08-15T00:00:00.000000Z')
            """,
            (voice_job_id, candidate["id"], "c" * 64, "d" * 64),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO voice_verification_segments(
                run_id, ordinal, start_ms, end_ms, raw_match_score,
                evidence_hash
            ) VALUES (?, 1, 1000, 3000, 0.75, ?)
            """,
            (run_id, "e" * 64),
        )
        conn.execute(
            """
            INSERT INTO voice_verification_reviews(
                run_id, action, actor, reason, prior_presence_decision_id,
                prior_presence_decision_hash, review_hash, reviewed_at
            ) VALUES (?, 'hold', 'local_user', 'needs another listen', ?, ?, ?,
                      '2026-08-15T00:00:00.000000Z')
            """,
            (
                run_id,
                candidate["current_presence_decision_id"],
                candidate["decision_hash"],
                "1" * 64,
            ),
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
            (candidate["id"],),
        )
        database_path = fixture.settings.database_path
    return database_path


_COLLISION_TABLES = (
    ("schema_migrations", "APPEND_ONLY"),
    ("audit_events", "APPEND_ONLY"),
    ("discovery_profile_versions", "APPEND_ONLY"),
    ("discovery_seed_channels", "APPEND_ONLY"),
    ("discovery_search_terms", "APPEND_ONLY"),
    ("video_metadata_snapshots", "APPEND_ONLY"),
    ("discovery_observations", "APPEND_ONLY"),
    ("presence_decisions", "APPEND_ONLY"),
    ("transcription_chunks", "APPEND_ONLY"),
    ("transcript_segments", "IMMUTABLE_TRANSCRIPT_BODY"),
    ("speaker_threshold_configs", "APPEND_ONLY"),
    ("voice_reference_profiles", "APPEND_ONLY"),
    ("voice_reference_clips", "IMMUTABLE_VOICE_REFERENCE"),
    ("voice_reference_features", "IMMUTABLE_VOICE_REFERENCE"),
    ("voice_verification_manifests", "IMMUTABLE_VOICE_MANIFEST"),
    ("voice_verification_runs", "IMMUTABLE_VOICE_RUN"),
    ("voice_verification_segments", "IMMUTABLE_VOICE_RUN"),
    ("voice_verification_reviews", "IMMUTABLE_VOICE_REVIEW"),
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


@pytest.mark.parametrize(
    ("table", "error_code"),
    (
        ("voice_reference_clips", "IMMUTABLE_VOICE_REFERENCE"),
        ("voice_reference_features", "IMMUTABLE_VOICE_REFERENCE"),
        ("voice_verification_manifests", "IMMUTABLE_VOICE_MANIFEST"),
        ("voice_verification_runs", "IMMUTABLE_VOICE_RUN"),
        ("voice_verification_segments", "IMMUTABLE_VOICE_RUN"),
        ("voice_verification_reviews", "IMMUTABLE_VOICE_REVIEW"),
    ),
)
def test_voice_records_reject_raw_update_and_delete(
    populated_database, table, error_code
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 1").fetchone()
        assert row is not None, table
        first_column = row.keys()[0]

        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=error_code):
            conn.execute(
                f"UPDATE {table} SET {first_column}={first_column} WHERE rowid=?",
                (row["id"],),
            )
        conn.execute("ROLLBACK")

        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match=error_code):
            conn.execute(f"DELETE FROM {table} WHERE rowid=?", (row["id"],))
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_presence_tables_reject_replace_with_logical_identity(
    populated_database,
) -> None:
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        cases = (
            ("voice_reference_clips", {"id": 990001}, "IMMUTABLE_VOICE_REFERENCE"),
            (
                "voice_reference_features",
                {"id": 990002},
                "IMMUTABLE_VOICE_REFERENCE",
            ),
            (
                "voice_verification_manifests",
                {"id": 990003},
                "IMMUTABLE_VOICE_MANIFEST",
            ),
            ("voice_verification_runs", {"id": 990004}, "IMMUTABLE_VOICE_RUN"),
            (
                "voice_verification_segments",
                {"id": 990005},
                "IMMUTABLE_VOICE_RUN",
            ),
            (
                "voice_verification_reviews",
                {"id": 990006},
                "IMMUTABLE_VOICE_REVIEW",
            ),
        )
        for table, overrides, error_code in cases:
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
    "table",
    (
        "discovery_profile_versions",
        "discovery_seed_channels",
        "discovery_search_terms",
        "video_metadata_snapshots",
        "discovery_observations",
        "presence_decisions",
    ),
)
def test_plain_sqlite_discovery_records_reject_raw_update_and_delete(
    populated_database, table
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        before = tuple(
            tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
        )
        assert before, table
        first_column = conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchone()[1]

        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            conn.execute(
                f"UPDATE {table} SET {first_column}={first_column} "
                f"WHERE rowid=(SELECT rowid FROM {table} ORDER BY rowid LIMIT 1)"
            )
        conn.execute("ROLLBACK")

        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            conn.execute(
                f"DELETE FROM {table} "
                f"WHERE rowid=(SELECT rowid FROM {table} ORDER BY rowid LIMIT 1)"
            )
        conn.execute("ROLLBACK")
        assert tuple(
            tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")
        ) == before
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


@pytest.mark.parametrize(
    ("table", "pointer_column"),
    (
        ("discovery_profiles", "current_version_id"),
        ("videos", "current_metadata_snapshot_id"),
        ("subject_video_candidates", "current_presence_decision_id"),
    ),
)
def test_plain_sqlite_completed_discovery_pointers_cannot_be_cleared(
    populated_database, table, pointer_column
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {pointer_column} IS NOT NULL "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None, table

        conn.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError, match="POINTER_OWNER_MISMATCH"):
            conn.execute(
                f"UPDATE {table} SET {pointer_column}=NULL WHERE id=?",
                (row["id"],),
            )
        conn.execute("ROLLBACK")

        assert tuple(
            conn.execute(
                f"SELECT * FROM {table} WHERE id=?", (row["id"],)
            ).fetchone()
        ) == tuple(row)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def test_plain_sqlite_discovery_construction_allows_initial_null_pointers(
    populated_database,
):
    conn = sqlite3.connect(populated_database, isolation_level=None)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO discovery_profiles(
                id, subject_id, current_version_id, is_active, created_at
            ) VALUES (
                990001, 990001, NULL, 1, '2026-08-18T00:00:00.000000Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO videos(
                id, youtube_video_id, current_metadata_snapshot_id, created_at
            ) VALUES (
                990002, 'initial00001', NULL, '2026-08-18T00:00:00.000000Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO subject_video_candidates(
                id, profile_id, video_id, first_observation_id,
                current_presence_decision_id, created_at
            ) VALUES (
                990003, 990001, 990002, 990001, NULL,
                '2026-08-18T00:00:00.000000Z'
            )
            """
        )
        assert conn.execute(
            "SELECT current_version_id FROM discovery_profiles WHERE id=990001"
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT current_metadata_snapshot_id FROM videos WHERE id=990002"
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT current_presence_decision_id "
            "FROM subject_video_candidates WHERE id=990003"
        ).fetchone()[0] is None
        conn.execute("ROLLBACK")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


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

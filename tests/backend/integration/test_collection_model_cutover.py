import sqlite3
from dataclasses import dataclass
from importlib import resources

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError


CUTOVER = "0018_youtube_discovery_cutover"

EXPECTED_TABLES = (
    "analysis_asset_mappings",
    "analysis_forecast_statement_links",
    "analysis_forecasts",
    "analysis_input_snapshots",
    "analysis_run_events",
    "analysis_run_job_attempts",
    "analysis_run_outputs",
    "analysis_run_segments",
    "analysis_runs",
    "analysis_scopes",
    "analysis_statement_evidence_links",
    "analysis_statement_periods",
    "analysis_statements",
    "analysis_subjects",
    "app_metadata",
    "audit_events",
    "current_asset_mappings",
    "current_forecasts",
    "current_result_sets",
    "current_statements",
    "discovery_observations",
    "discovery_profile_versions",
    "discovery_profiles",
    "discovery_search_terms",
    "discovery_seed_channels",
    "forecast_projection_batches",
    "heatmap_cell_forecasts",
    "heatmap_cells",
    "job_events",
    "job_unit_attempts",
    "job_units",
    "jobs",
    "local_artifacts",
    "manual_discovery_requests",
    "mapping_reviews",
    "period_reviews",
    "presence_decisions",
    "retention_deletion_previews",
    "retention_settings",
    "schema_migrations",
    "speaker_assignments",
    "speaker_threshold_configs",
    "subject_aliases",
    "subject_video_candidates",
    "transcript_segments",
    "transcription_chunks",
    "video_metadata_snapshots",
    "video_pipeline_job_binding_sets",
    "video_pipeline_job_bindings",
    "videos",
    "voice_reference_profiles",
    "youtube_daily_sync_requests",
    "youtube_quota_reservations",
    "youtube_search_windows",
    "youtube_source_cursors",
    "youtube_sync_checkpoints",
    "youtube_sync_manifest_profiles",
    "youtube_sync_manifests",
    "youtube_sync_proposed_cursors",
)

EXPECTED_TRIGGERS = (
    "analysis_asset_mappings_no_delete",
    "analysis_asset_mappings_no_replace",
    "analysis_asset_mappings_no_update",
    "analysis_asset_mappings_require_running_unit",
    "analysis_asset_mappings_require_safe_rule_evidence",
    "analysis_asset_mappings_require_statement_source",
    "analysis_forecast_links_require_same_run",
    "analysis_forecast_statement_links_no_delete",
    "analysis_forecast_statement_links_no_replace",
    "analysis_forecast_statement_links_no_update",
    "analysis_forecast_statement_links_ordinal_no_replace",
    "analysis_forecasts_logical_no_replace",
    "analysis_forecasts_no_delete",
    "analysis_forecasts_no_replace",
    "analysis_forecasts_no_update",
    "analysis_forecasts_require_batch_run",
    "analysis_forecasts_require_safe_directions",
    "analysis_input_snapshots_limited_update",
    "analysis_input_snapshots_no_delete",
    "analysis_input_snapshots_no_replace",
    "analysis_run_events_no_delete",
    "analysis_run_events_no_replace",
    "analysis_run_events_no_update",
    "analysis_run_job_attempts_match_job_source",
    "analysis_run_job_attempts_no_delete",
    "analysis_run_job_attempts_no_replace",
    "analysis_run_job_attempts_no_update",
    "analysis_run_job_attempts_require_analysis_job",
    "analysis_run_outputs_no_delete",
    "analysis_run_outputs_no_replace",
    "analysis_run_outputs_no_update",
    "analysis_run_outputs_require_owned_codex_unit",
    "analysis_run_segments_match_video",
    "analysis_run_segments_no_delete",
    "analysis_run_segments_no_replace",
    "analysis_run_segments_no_update",
    "analysis_runs_no_delete",
    "analysis_runs_no_replace",
    "analysis_runs_no_update",
    "analysis_runs_reject_deleted_source_text",
    "analysis_runs_scope_generation_match",
    "analysis_scopes_generation_monotonic",
    "analysis_scopes_no_replace",
    "analysis_statement_evidence_links_no_delete",
    "analysis_statement_evidence_links_no_replace",
    "analysis_statement_evidence_links_no_update",
    "analysis_statement_evidence_requires_exact_times",
    "analysis_statement_evidence_requires_same_run",
    "analysis_statement_first_evidence_requires_source_video",
    "analysis_statement_periods_no_delete",
    "analysis_statement_periods_no_replace",
    "analysis_statement_periods_no_update",
    "analysis_statements_no_delete",
    "analysis_statements_no_replace",
    "analysis_statements_no_update",
    "audit_events_no_delete",
    "audit_events_no_replace",
    "audit_events_no_update",
    "current_asset_mappings_validate_insert",
    "current_asset_mappings_validate_update",
    "current_forecasts_validate_insert",
    "current_forecasts_validate_update",
    "current_result_sets_validate_insert",
    "current_result_sets_validate_update",
    "current_statements_validate_insert",
    "current_statements_validate_update",
    "discovery_observations_no_delete",
    "discovery_observations_no_replace",
    "discovery_observations_no_update",
    "discovery_profile_versions_no_delete",
    "discovery_profile_versions_no_replace",
    "discovery_profile_versions_no_update",
    "discovery_profiles_current_version_owner_insert",
    "discovery_profiles_current_version_owner_update",
    "discovery_profiles_limited_update",
    "discovery_profiles_no_delete",
    "discovery_profiles_no_replace",
    "discovery_search_terms_no_delete",
    "discovery_search_terms_no_replace",
    "discovery_search_terms_no_update",
    "discovery_seed_channels_no_delete",
    "discovery_seed_channels_no_replace",
    "discovery_seed_channels_no_update",
    "forecast_projection_batches_no_delete",
    "forecast_projection_batches_no_replace",
    "forecast_projection_batches_no_update",
    "forecast_projection_batches_require_current_review_heads",
    "heatmap_cell_forecasts_no_update",
    "heatmap_cells_no_update",
    "heatmap_cells_validate_insert",
    "job_events_no_delete",
    "job_events_no_replace",
    "job_events_no_update",
    "job_unit_attempts_no_delete",
    "job_unit_attempts_no_replace",
    "job_unit_attempts_no_update",
    "job_units_input_binding_immutable",
    "job_units_manifest_immutable",
    "job_units_manifest_no_delete",
    "job_units_manifest_no_extra_insert",
    "job_units_no_replace",
    "jobs_manifest_immutable",
    "jobs_no_replace",
    "local_artifacts_limited_update",
    "local_artifacts_no_delete",
    "local_artifacts_no_replace",
    "manual_discovery_requests_no_delete",
    "manual_discovery_requests_no_replace",
    "manual_discovery_requests_no_update",
    "mapping_reviews_no_delete",
    "mapping_reviews_no_replace",
    "mapping_reviews_no_update",
    "mapping_reviews_require_consistent_state",
    "mapping_reviews_require_latest_id",
    "period_reviews_approve_requires_unknown",
    "period_reviews_no_delete",
    "period_reviews_no_replace",
    "period_reviews_no_update",
    "period_reviews_require_positive_latest_id",
    "presence_decisions_no_delete",
    "presence_decisions_no_replace",
    "presence_decisions_no_update",
    "retention_settings_limited_update",
    "retention_settings_no_delete",
    "schema_migrations_no_replace",
    "speaker_threshold_configs_no_replace",
    "subject_video_candidates_current_presence_owner_insert",
    "subject_video_candidates_current_presence_owner_update",
    "subject_video_candidates_limited_update",
    "subject_video_candidates_no_delete",
    "subject_video_candidates_no_replace",
    "transcript_segments_limited_update",
    "transcript_segments_no_delete",
    "transcript_segments_no_replace",
    "transcription_chunks_no_replace",
    "video_metadata_snapshots_no_delete",
    "video_metadata_snapshots_no_replace",
    "video_metadata_snapshots_no_update",
    "video_pipeline_job_binding_sets_no_delete",
    "video_pipeline_job_binding_sets_no_replace",
    "video_pipeline_job_binding_sets_require_open_insert",
    "video_pipeline_job_binding_sets_require_video_job",
    "video_pipeline_job_binding_sets_seal_once",
    "video_pipeline_job_bindings_no_delete",
    "video_pipeline_job_bindings_no_replace",
    "video_pipeline_job_bindings_no_update",
    "video_pipeline_job_bindings_require_one_video",
    "video_pipeline_job_bindings_require_open_set",
    "video_pipeline_job_bindings_require_video_job",
    "videos_current_metadata_snapshot_owner_insert",
    "videos_current_metadata_snapshot_owner_update",
    "videos_limited_update",
    "videos_no_delete",
    "videos_no_replace",
    "voice_reference_profiles_no_replace",
    "youtube_daily_sync_requests_no_delete",
    "youtube_daily_sync_requests_no_replace",
    "youtube_daily_sync_requests_no_update",
    "youtube_daily_sync_requests_require_full_manifest",
    "youtube_quota_reservations_no_delete",
    "youtube_quota_reservations_no_replace",
    "youtube_quota_reservations_no_update",
    "youtube_quota_reservations_require_youtube_unit",
    "youtube_sync_checkpoints_require_manifest_unit",
    "youtube_sync_manifest_profiles_no_delete",
    "youtube_sync_manifest_profiles_no_replace",
    "youtube_sync_manifest_profiles_no_update",
    "youtube_sync_manifest_profiles_require_contiguous_ordinal",
    "youtube_sync_manifest_profiles_require_owner",
    "youtube_sync_manifests_limited_update",
    "youtube_sync_manifests_no_delete",
    "youtube_sync_manifests_no_replace",
    "youtube_sync_manifests_require_job",
)

NEW_APPEND_ONLY_TABLES = (
    "discovery_profile_versions",
    "discovery_search_terms",
    "discovery_seed_channels",
    "video_metadata_snapshots",
    "discovery_observations",
    "presence_decisions",
    "youtube_quota_reservations",
)


@dataclass(frozen=True, slots=True)
class LogicalIdentityCase:
    table: str
    logical_columns: tuple[str, ...]
    primary_columns: tuple[str, ...]
    primary_overrides: dict[str, object]
    primary_guard: str
    error_code: str


COLLECTION_LOGICAL_IDENTITIES = (
    LogicalIdentityCase(
        "discovery_profile_versions",
        ("profile_id", "config_hash"),
        ("id",),
        {"id": 302},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "discovery_profiles",
        ("subject_id",),
        ("id",),
        {"id": 202, "current_version_id": None},
        "id=NEW.id",
        "IMMUTABLE_DISCOVERY_PROFILE",
    ),
    LogicalIdentityCase(
        "discovery_seed_channels",
        ("profile_version_id", "youtube_channel_id"),
        ("profile_version_id", "ordinal"),
        {"ordinal": 2},
        (
            "profile_version_id=NEW.profile_version_id "
            "AND ordinal=NEW.ordinal"
        ),
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "discovery_search_terms",
        ("profile_version_id", "search_term"),
        ("profile_version_id", "ordinal"),
        {"ordinal": 2},
        (
            "profile_version_id=NEW.profile_version_id "
            "AND ordinal=NEW.ordinal"
        ),
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "videos",
        ("youtube_video_id",),
        ("id",),
        {"id": 402, "current_metadata_snapshot_id": None},
        "id=NEW.id",
        "IMMUTABLE_VIDEO",
    ),
    LogicalIdentityCase(
        "video_metadata_snapshots",
        ("video_id", "canonical_hash"),
        ("id",),
        {"id": 502},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "discovery_observations",
        ("idempotency_key",),
        ("id",),
        {"id": 702},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "subject_video_candidates",
        ("profile_id", "video_id"),
        ("id",),
        {"id": 802, "current_presence_decision_id": None},
        "id=NEW.id",
        "IMMUTABLE_CANDIDATE",
    ),
    LogicalIdentityCase(
        "presence_decisions",
        ("candidate_id", "decision_hash"),
        ("id",),
        {"id": 902},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "manual_discovery_requests",
        ("profile_id", "youtube_video_id"),
        ("id",),
        {"id": 1002},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "youtube_daily_sync_requests",
        ("job_id",),
        ("jst_day",),
        {"jst_day": "2026-08-19"},
        "jst_day=NEW.jst_day",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "youtube_quota_reservations",
        ("job_id", "unit_key", "request_ordinal", "attempt_no"),
        ("id",),
        {"id": 1102},
        "id=NEW.id",
        "APPEND_ONLY",
    ),
    LogicalIdentityCase(
        "youtube_sync_manifest_profiles",
        ("job_id", "profile_id"),
        ("job_id", "ordinal"),
        {"ordinal": 2},
        "job_id=NEW.job_id AND ordinal=NEW.ordinal",
        "APPEND_ONLY",
    ),
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for character in script:
        statement += character
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def _apply_packaged_migrations_through(
    conn: sqlite3.Connection, final_name: str
) -> None:
    conn.execute(
        "CREATE TABLE schema_migrations("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    migration_files = sorted(
        resource
        for resource in resources.files(
            "market_voice_forecast_ledger.db.migrations"
        ).iterdir()
        if resource.name[:4].isdigit()
        and resource.name[4:5] == "_"
        and resource.name.endswith(".sql")
        and resource.name.removesuffix(".sql") <= final_name
    )
    for migration_file in migration_files:
        migration_name = migration_file.name.removesuffix(".sql")
        with transaction(conn):
            _execute_script(conn, migration_file.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration_name, "2026-08-18T00:00:00.000000Z"),
            )


def _schema_fingerprint(conn: sqlite3.Connection) -> tuple[object, ...]:
    schema = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        )
    )
    ledger = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT name, applied_at FROM schema_migrations ORDER BY name"
        )
    )
    return schema, ledger


def _schema_names(conn: sqlite3.Connection, kind: str) -> tuple[str, ...]:
    return tuple(
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type=? AND name NOT LIKE 'sqlite_%' ORDER BY name",
            (kind,),
        )
    )


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))


def _migration_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row["name"]
        for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")
    )


def test_pre_cutover_database_is_rejected_without_any_schema_change(tmp_path):
    conn = open_database(tmp_path / "legacy.sqlite3")
    try:
        _apply_packaged_migrations_through(conn, "0017_append_only_guards")
        before = _schema_fingerprint(conn)

        with pytest.raises(DomainError, match="COLLECTION_MODEL_RESET_REQUIRED") as caught:
            apply_migrations(conn)

        assert caught.value.code == "COLLECTION_MODEL_RESET_REQUIRED"
        assert _schema_fingerprint(conn) == before
        assert CUTOVER not in _migration_names(conn)
    finally:
        conn.close()


def test_precreated_empty_migration_ledger_is_not_a_fresh_database(tmp_path):
    conn = open_database(tmp_path / "empty-ledger.sqlite3")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        before = _schema_fingerprint(conn)

        with pytest.raises(DomainError, match="COLLECTION_MODEL_RESET_REQUIRED") as caught:
            apply_migrations(conn)

        assert caught.value.code == "COLLECTION_MODEL_RESET_REQUIRED"
        assert _schema_fingerprint(conn) == before
    finally:
        conn.close()


def test_fresh_database_finishes_with_only_collection_model_schema(db):
    assert _schema_names(db, "table") == EXPECTED_TABLES
    assert {"subject_channel_policies", "subject_video_eligibility"}.isdisjoint(
        EXPECTED_TABLES
    )
    assert _columns(db, "analysis_subjects") == (
        "id",
        "canonical_name",
        "is_active",
        "created_at",
    )
    assert _columns(db, "videos") == (
        "id",
        "youtube_video_id",
        "current_metadata_snapshot_id",
        "created_at",
    )
    assert _columns(db, "speaker_assignments")[:2] == ("id", "segment_id")
    assert _columns(db, "analysis_run_segments") == (
        "id",
        "run_id",
        "segment_id",
        "ordinal",
        "video_id",
        "published_at",
        "metadata_snapshot_id",
        "metadata_snapshot_hash",
        "presence_decision_id",
        "presence_decision_hash",
        "speaker_assignment_id",
        "assignment_kind",
        "assigned_subject_id",
        "assignment_updated_at",
        "assignment_evidence_hash",
    )
    assert _columns(db, "video_pipeline_job_bindings") == (
        "job_id",
        "candidate_id",
    )
    assert CUTOVER in _migration_names(db)


def test_final_schema_has_collection_pointer_and_append_only_guards(db):
    assert _schema_names(db, "trigger") == EXPECTED_TRIGGERS
    trigger_names = set(EXPECTED_TRIGGERS)
    assert {
        "discovery_profiles_current_version_owner_insert",
        "discovery_profiles_current_version_owner_update",
        "videos_current_metadata_snapshot_owner_insert",
        "videos_current_metadata_snapshot_owner_update",
        "subject_video_candidates_current_presence_owner_insert",
        "subject_video_candidates_current_presence_owner_update",
        "video_pipeline_job_bindings_require_one_video",
    } <= trigger_names
    assert {
        "bound_video_eligibility_identity_immutable",
        "bound_video_eligibility_no_delete",
    }.isdisjoint(trigger_names)
    for table in NEW_APPEND_ONLY_TABLES:
        assert {
            f"{table}_no_update",
            f"{table}_no_delete",
            f"{table}_no_replace",
        } <= trigger_names


def test_collection_append_only_rows_reject_update_delete_and_replace(db):
    db.execute(
        "INSERT INTO analysis_subjects(canonical_name, is_active, created_at) "
        "VALUES ('Synthetic guard subject', 1, '2026-08-18T00:00:00.000000Z')"
    )
    subject_id = db.execute(
        "SELECT id FROM analysis_subjects WHERE canonical_name='Synthetic guard subject'"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO discovery_profiles(subject_id, is_active, created_at) "
        "VALUES (?, 1, '2026-08-18T00:00:00.000000Z')",
        (subject_id,),
    )
    profile_id = db.execute(
        "SELECT id FROM discovery_profiles WHERE subject_id=?", (subject_id,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO discovery_profile_versions(profile_id, config_hash, created_at) "
        "VALUES (?, 'profile-hash', '2026-08-18T00:00:00.000000Z')",
        (profile_id,),
    )
    version_id = db.execute(
        "SELECT id FROM discovery_profile_versions WHERE profile_id=?", (profile_id,)
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE discovery_profile_versions SET config_hash='changed' WHERE id=?",
            (version_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM discovery_profile_versions WHERE id=?", (version_id,))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "INSERT OR REPLACE INTO discovery_profile_versions"
            "(id, profile_id, config_hash, created_at) VALUES (?, ?, ?, ?)",
            (
                version_id,
                profile_id,
                "replacement",
                "2026-08-18T00:00:00.000000Z",
            ),
        )


def test_collection_pointer_ownership_fails_closed(db):
    for ordinal in (1, 2):
        db.execute(
            "INSERT INTO analysis_subjects(canonical_name, is_active, created_at) "
            "VALUES (?, 1, '2026-08-18T00:00:00.000000Z')",
            (f"Synthetic pointer subject {ordinal}",),
        )
        subject_id = db.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name=?",
            (f"Synthetic pointer subject {ordinal}",),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO discovery_profiles(subject_id, is_active, created_at) "
            "VALUES (?, 1, '2026-08-18T00:00:00.000000Z')",
            (subject_id,),
        )
    profiles = tuple(
        row[0]
        for row in db.execute(
            "SELECT id FROM discovery_profiles ORDER BY id DESC LIMIT 2"
        )
    )
    db.execute(
        "INSERT INTO discovery_profile_versions(profile_id, config_hash, created_at) "
        "VALUES (?, 'owner-version', '2026-08-18T00:00:00.000000Z')",
        (profiles[0],),
    )
    foreign_version_id = db.execute(
        "SELECT id FROM discovery_profile_versions WHERE profile_id=?",
        (profiles[0],),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="POINTER_OWNER_MISMATCH"):
        db.execute(
            "UPDATE discovery_profiles SET current_version_id=? WHERE id=?",
            (foreign_version_id, profiles[1]),
        )


def test_only_one_youtube_sync_job_can_be_active(db):
    db.execute(
        "INSERT INTO jobs(job_kind, manifest_hash, total_units, status, created_at, updated_at) "
        "VALUES ('youtube_sync', 'manifest-running', 1, 'running', "
        "'2026-08-18T00:00:00.000000Z', '2026-08-18T00:00:00.000000Z')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO jobs(job_kind, manifest_hash, total_units, status, created_at, updated_at) "
            "VALUES ('youtube_sync', ?, 1, ?, '2026-08-18T00:00:00.000000Z', "
            "'2026-08-18T00:00:00.000000Z')",
            ("manifest-pause", "pause_requested"),
        )


def _seed_collection_logical_identity_graph(conn: sqlite3.Connection) -> None:
    timestamp = "2026-08-18T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO analysis_subjects(id, canonical_name, is_active, created_at) "
        "VALUES (101, 'Synthetic identity subject', 1, ?)",
        (timestamp,),
    )
    conn.execute(
        "INSERT INTO discovery_profiles("
        "id, subject_id, current_version_id, is_active, created_at"
        ") VALUES (201, 101, NULL, 1, ?)",
        (timestamp,),
    )
    conn.execute(
        "INSERT INTO discovery_profile_versions("
        "id, profile_id, config_hash, created_at"
        ") VALUES (301, 201, 'profile-config-v1', ?)",
        (timestamp,),
    )
    conn.execute(
        "UPDATE discovery_profiles SET current_version_id=301 WHERE id=201"
    )
    conn.execute(
        "INSERT INTO discovery_seed_channels("
        "profile_version_id, ordinal, youtube_channel_id"
        ") VALUES (301, 1, 'UCidentity0000000000000001')"
    )
    conn.execute(
        "INSERT INTO discovery_search_terms("
        "profile_version_id, ordinal, search_term"
        ") VALUES (301, 1, 'Synthetic identity term')"
    )
    conn.execute(
        "INSERT INTO videos(id, youtube_video_id, current_metadata_snapshot_id, "
        "created_at) VALUES (401, 'identity001', NULL, ?)",
        (timestamp,),
    )
    conn.execute(
        """
        INSERT INTO video_metadata_snapshots(
            id, video_id, youtube_video_id, channel_id, channel_title,
            title, description, published_at, duration_seconds, live_state,
            actual_start_time, schema_version, canonical_hash, fetched_at
        ) VALUES (
            501, 401, 'identity001', 'UCidentity0000000000000001',
            'Synthetic identity channel', 'Synthetic identity video',
            'Synthetic identity description', ?, 60, 'not_live', NULL,
            'youtube-video-metadata-v1', 'metadata-hash-v1', ?
        )
        """,
        (timestamp, timestamp),
    )
    conn.execute(
        "UPDATE videos SET current_metadata_snapshot_id=501 WHERE id=401"
    )
    conn.execute(
        """
        INSERT INTO jobs(
            id, source_job_id, job_kind, manifest_hash, total_units,
            status, created_at, updated_at
        ) VALUES (
            601, NULL, 'youtube_sync', 'youtube-manifest-v1', 1,
            'queued', ?, ?
        )
        """,
        (timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO job_units(
            job_id, unit_key, stage, ordinal, declared_input_hash,
            dependency_keys_json, execution_contract_hash, status
        ) VALUES (
            601, 'youtube:search:identity', 'youtube_search_discovery', 1,
            'identity-input-v1', '[]', 'identity-contract-v1', 'pending'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO discovery_observations(
            id, job_id, profile_id, video_id, metadata_snapshot_id,
            metadata_snapshot_hash, source_kind, source_key, observed_at,
            observation_hash, idempotency_key
        ) VALUES (
            701, 601, 201, 401, 501, 'metadata-hash-v1',
            'cross_channel_search', 'Synthetic identity term', ?,
            'observation-hash-v1', 'observation-idempotency-v1'
        )
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT INTO subject_video_candidates("
        "id, profile_id, video_id, first_observation_id, "
        "current_presence_decision_id, created_at"
        ") VALUES (801, 201, 401, 701, NULL, ?)",
        (timestamp,),
    )
    conn.execute(
        """
        INSERT INTO presence_decisions(
            id, candidate_id, state, decision_origin, evidence_ref,
            evidence_hash, decision_hash, created_at
        ) VALUES (
            901, 801, 'presence_unverified', 'collection_initial',
            'observation:701', 'presence-evidence-v1',
            'presence-decision-v1', ?
        )
        """,
        (timestamp,),
    )
    conn.execute(
        "UPDATE subject_video_candidates "
        "SET current_presence_decision_id=901 WHERE id=801"
    )
    conn.execute(
        "INSERT INTO manual_discovery_requests("
        "id, profile_id, youtube_video_id, requested_at"
        ") VALUES (1001, 201, 'manual001', ?)",
        (timestamp,),
    )
    conn.execute(
        """
        INSERT INTO youtube_sync_manifests(
            job_id, sync_kind, upper_bound, backfill_floor,
            quota_contract_version, profile_set_hash, manual_request_id,
            resume_not_before_utc, manifest_hash, created_at
        ) VALUES (
            601, 'full_discovery', ?, ?, 'quota-v1', 'profile-set-v1',
            NULL, NULL, 'youtube-manifest-v1', ?
        )
        """,
        (timestamp, timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO youtube_sync_manifest_profiles(
            job_id, ordinal, profile_id, profile_version_id,
            config_hash, discoverer_set_hash
        ) VALUES (
            601, 1, 201, 301, 'profile-config-v1', 'discoverers-v1'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO youtube_quota_reservations(
            id, job_id, unit_key, request_ordinal, attempt_no,
            endpoint_class, attempted_at
        ) VALUES (
            1101, 601, 'youtube:search:identity', 1, 1,
            'search_list', ?
        )
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT INTO youtube_daily_sync_requests(jst_day, job_id, requested_at) "
        "VALUES ('2026-08-18', 601, ?)",
        (timestamp,),
    )


def _table_snapshot(
    conn: sqlite3.Connection, table: str
) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))


def _collection_pointer_snapshot(
    conn: sqlite3.Connection,
) -> tuple[object, ...]:
    return (
        conn.execute(
            "SELECT current_version_id FROM discovery_profiles WHERE id=201"
        ).fetchone()[0],
        conn.execute(
            "SELECT current_metadata_snapshot_id FROM videos WHERE id=401"
        ).fetchone()[0],
        conn.execute(
            "SELECT current_presence_decision_id "
            "FROM subject_video_candidates WHERE id=801"
        ).fetchone()[0],
    )


def _replacement_values(
    conn: sqlite3.Connection, case: LogicalIdentityCase
) -> tuple[tuple[str, ...], dict[str, object], sqlite3.Row]:
    original = conn.execute(f"SELECT * FROM {case.table}").fetchone()
    assert original is not None
    values = dict(original)
    values.update(case.primary_overrides)
    return tuple(values), values, original


def _insert_or_replace(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: dict[str, object],
) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


@pytest.mark.parametrize("case", COLLECTION_LOGICAL_IDENTITIES, ids=lambda c: c.table)
def test_collection_logical_identity_replace_is_rejected_without_mutation(
    db, case: LogicalIdentityCase
):
    _seed_collection_logical_identity_graph(db)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA recursive_triggers=OFF")
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    before_rows = _table_snapshot(db, case.table)
    before_pointers = _collection_pointer_snapshot(db)
    columns, values, _ = _replacement_values(db, case)

    db.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError, match=case.error_code):
        _insert_or_replace(db, case.table, columns, values)
    assert _table_snapshot(db, case.table) == before_rows
    assert _collection_pointer_snapshot(db) == before_pointers
    db.execute("ROLLBACK")

    assert _table_snapshot(db, case.table) == before_rows
    assert _collection_pointer_snapshot(db) == before_pointers


@pytest.mark.parametrize("case", COLLECTION_LOGICAL_IDENTITIES, ids=lambda c: c.table)
def test_collection_logical_identity_guard_is_mutation_sensitive(
    db, case: LogicalIdentityCase
):
    _seed_collection_logical_identity_graph(db)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA recursive_triggers=OFF")
    trigger_name = f"{case.table}_no_replace"
    db.execute(f"DROP TRIGGER {trigger_name}")
    db.execute(
        f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON {case.table} "
        f"WHEN EXISTS (SELECT 1 FROM {case.table} WHERE {case.primary_guard}) "
        f"BEGIN SELECT RAISE(ABORT, '{case.error_code}'); END"
    )
    columns, values, original = _replacement_values(db, case)

    db.execute("BEGIN")
    _insert_or_replace(db, case.table, columns, values)
    primary_predicate = " AND ".join(
        f"{column}=?" for column in case.primary_columns
    )
    old_primary = tuple(original[column] for column in case.primary_columns)
    new_primary = tuple(values[column] for column in case.primary_columns)
    assert old_primary != new_primary
    assert db.execute(
        f"SELECT 1 FROM {case.table} WHERE {primary_predicate}", old_primary
    ).fetchone() is None
    replacement = db.execute(
        f"SELECT * FROM {case.table} WHERE {primary_predicate}", new_primary
    ).fetchone()
    assert replacement is not None
    assert tuple(replacement[column] for column in case.logical_columns) == tuple(
        original[column] for column in case.logical_columns
    )
    db.execute("ROLLBACK")

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import urllib.request
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import sha256_text, utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoverySourceKind,
    LiveState,
    PresenceOrigin,
    PresenceState,
    canonical_presence_decision_hash,
)
from market_voice_forecast_ledger.domain.voice_verification import (
    PRESENCE_UNITS,
    ReferenceClipCommand,
    ReviewAction,
    VoiceProposal,
)
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.voice_verification import (
    VoiceVerificationRepository,
)
from market_voice_forecast_ledger.services.retention import RetentionService
from market_voice_forecast_ledger.services.voice_reference import (
    VoiceReferenceService,
)
from market_voice_forecast_ledger.services.voice_verification import (
    PresenceVerificationService,
    ReviewCommand,
)
from tests.backend.integration.test_voice_verification_jobs import (
    presence_worker_harness,
)
from tests.backend.voice_fakes import (
    SequencedPresenceAdapter,
    SimulatedPresenceCrash,
    SyntheticReferenceMedia,
    SyntheticReferenceScorer,
    fake_runtime_attestation,
)


NOW = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)
PILOT_CANDIDATE_COUNT = 20
PILOT_JOB_COUNT = 20
PILOT_UNIT_COUNT = 140
REFERENCE_ARTIFACT_COUNT = 144
PILOT_ARTIFACT_COUNT = 60
ANALYSIS_OUTPUT_TABLES = (
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
    "current_asset_mappings",
    "current_forecasts",
    "current_result_sets",
    "current_statements",
    "forecast_projection_batches",
    "heatmap_cell_forecasts",
    "heatmap_cells",
    "mapping_reviews",
    "period_reviews",
)
AUTOMATIC_COLLECTION_TABLES = (
    "youtube_daily_sync_requests",
    "youtube_quota_reservations",
    "youtube_search_windows",
    "youtube_source_cursors",
    "youtube_sync_checkpoints",
    "youtube_sync_manifest_profiles",
    "youtube_sync_manifests",
    "youtube_sync_proposed_cursors",
)
COUNTED_TABLES = frozenset(
    {
        *ANALYSIS_OUTPUT_TABLES,
        *AUTOMATIC_COLLECTION_TABLES,
        "audit_events",
        "discovery_observations",
        "job_unit_attempts",
        "job_units",
        "jobs",
        "local_artifacts",
        "presence_decisions",
        "speaker_assignments",
        "speaker_threshold_configs",
        "subject_video_candidates",
        "transcript_segments",
        "transcription_chunks",
        "video_metadata_snapshots",
        "video_pipeline_job_binding_sets",
        "video_pipeline_job_bindings",
        "videos",
        "voice_reference_calibrations",
        "voice_reference_clips",
        "voice_reference_features",
        "voice_reference_profiles",
        "voice_verification_manifests",
        "voice_verification_reviews",
        "voice_verification_runs",
        "voice_verification_segments",
    }
)


@pytest.fixture
def presence_db(tmp_path: Path):
    conn = open_database(tmp_path / "presence-flow.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def table_count(db: sqlite3.Connection, table: str) -> int:
    if table not in COUNTED_TABLES:
        raise AssertionError("table inventory is not allowlisted")
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_exact_candidates(
    db: sqlite3.Connection,
) -> tuple[int, dict[int, tuple[sqlite3.Row, ...]]]:
    discovery = DiscoveryRepository(db)
    profiles = discovery.list_active_profile_versions()
    assert len(profiles) == 4
    source_job_id = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
        """,
        (
            sha256_text("synthetic-explicit-candidate-source"),
            utc_iso(NOW),
            utc_iso(NOW),
        ),
    ).lastrowid
    candidates: dict[int, tuple[sqlite3.Row, ...]] = {}
    ordinal = 0
    with transaction(db):
        for profile in profiles:
            profile_rows: list[sqlite3.Row] = []
            for local_ordinal in range(5):
                ordinal += 1
                source_kind = (
                    DiscoverySourceKind.SEED_UPLOADS
                    if profile.seed_channel_ids and local_ordinal < 2
                    else DiscoverySourceKind.CROSS_CHANNEL_SEARCH
                )
                metadata = CanonicalVideoMetadata.build(
                    youtube_video_id=f"p{ordinal:010d}",
                    channel_id=f"UC{ordinal:022d}",
                    channel_title=f"Synthetic channel {ordinal}",
                    title=f"Synthetic candidate {ordinal}",
                    description="synthetic metadata only",
                    published_at=NOW - timedelta(days=local_ordinal),
                    duration_seconds=600,
                    live_state=LiveState.NOT_LIVE,
                    actual_start_time=None,
                    schema_version="youtube-video-metadata.v1",
                    fetched_at=NOW,
                )
                result = discovery.persist_metadata_batch(
                    source_job_id,
                    profile.id,
                    source_kind,
                    f"synthetic-source-{ordinal}",
                    (metadata,),
                    NOW + timedelta(microseconds=ordinal),
                )
                candidate_id = result.candidate_ids[0]
                row = db.execute(
                    """
                    SELECT candidate.id AS candidate_id, candidate.video_id,
                           candidate.current_presence_decision_id,
                           decision.decision_hash
                    FROM subject_video_candidates AS candidate
                    JOIN presence_decisions AS decision
                      ON decision.id=candidate.current_presence_decision_id
                    WHERE candidate.id=?
                    """,
                    (candidate_id,),
                ).fetchone()
                assert row is not None
                profile_rows.append(row)
            candidates[profile.subject_id] = tuple(profile_rows)
    return int(source_job_id), candidates


def _approve_complete_references(
    service: VoiceReferenceService,
    candidates: dict[int, tuple[sqlite3.Row, ...]],
) -> None:
    subject_ids = tuple(sorted(candidates))
    for subject_id in subject_ids:
        own_video = candidates[subject_id][0]["video_id"]
        for command in (
            ReferenceClipCommand(
                subject_id, own_video, 0, 15_000,
                "local_user", "clear enrollment speech one",
            ),
            ReferenceClipCommand(
                subject_id, own_video, 15_000, 30_000,
                "local_user", "clear enrollment speech two",
            ),
            ReferenceClipCommand(
                subject_id, own_video, 30_000, 40_000,
                "local_user", "clear held out speech",
            ),
        ):
            service.approve_clip(command)
        negative_subjects = tuple(
            other for other in subject_ids if other != subject_id
        )
        for negative_ordinal, other in enumerate(negative_subjects, start=1):
            service.approve_clip(
                ReferenceClipCommand(
                    subject_id,
                    candidates[other][0]["video_id"],
                    negative_ordinal * 10_000,
                    (negative_ordinal + 1) * 10_000,
                    "local_user",
                    f"confirmed negative speaker {negative_ordinal}",
                )
            )


def _assert_exact_pilot_jobs(
    db: sqlite3.Connection,
    *,
    source_job_id: int,
    pilot_job_ids: tuple[int, ...],
) -> None:
    rows = tuple(db.execute("SELECT id, job_kind FROM jobs ORDER BY id"))
    assert tuple(row["id"] for row in rows) == (
        source_job_id,
        *pilot_job_ids,
    )
    assert tuple(row["job_kind"] for row in rows) == (
        "youtube_sync",
        *("video_pipeline" for _ in range(PILOT_JOB_COUNT)),
    )


def _assert_no_audio_files(*roots: Path) -> None:
    assert not any(
        path.is_file() or path.is_symlink()
        for root in roots
        for path in root.rglob("*")
    ), "synthetic audio filesystem is not empty"


def _presence_pointers(db: sqlite3.Connection) -> dict[int, int]:
    return {
        row["id"]: row["current_presence_decision_id"]
        for row in db.execute(
            """
            SELECT id, current_presence_decision_id
            FROM subject_video_candidates ORDER BY id
            """
        )
    }


def model_only_presence_changes(
    db: sqlite3.Connection,
    frozen_pointers: dict[int, int],
) -> int:
    unauthorized_decisions = db.execute(
        """
        SELECT COUNT(*)
        FROM presence_decisions AS decision
        LEFT JOIN voice_verification_reviews AS review
          ON CAST(review.id AS TEXT)=decision.evidence_ref
         AND review.review_hash=decision.evidence_hash
        WHERE decision.decision_origin='voice_verification'
          AND (
              review.id IS NULL
              OR (decision.state='presence_confirmed' AND review.action!='confirm')
              OR (decision.state='presence_rejected' AND review.action!='reject')
              OR decision.state NOT IN (
                  'presence_confirmed', 'presence_rejected'
              )
          )
        """
    ).fetchone()[0]
    unauthorized_pointers = 0
    for candidate_id, frozen_decision_id in frozen_pointers.items():
        current = db.execute(
            """
            SELECT candidate.current_presence_decision_id,
                   decision.state, decision.evidence_ref,
                   decision.evidence_hash, review.action,
                   review.review_hash
            FROM subject_video_candidates AS candidate
            JOIN presence_decisions AS decision
              ON decision.id=candidate.current_presence_decision_id
            LEFT JOIN voice_verification_reviews AS review
              ON CAST(review.id AS TEXT)=decision.evidence_ref
            WHERE candidate.id=?
            """,
            (candidate_id,),
        ).fetchone()
        assert current is not None
        if current["current_presence_decision_id"] == frozen_decision_id:
            continue
        authorized = (
            current["evidence_hash"] == current["review_hash"]
            and (
                (
                    current["state"] == "presence_confirmed"
                    and current["action"] == "confirm"
                )
                or (
                    current["state"] == "presence_rejected"
                    and current["action"] == "reject"
                )
            )
        )
        unauthorized_pointers += not authorized
    return int(unauthorized_decisions + unauthorized_pointers)


def confirmed_or_rejected_changes(db: sqlite3.Connection) -> int:
    return int(
        db.execute(
            """
            SELECT COUNT(*) FROM presence_decisions
            WHERE decision_origin='voice_verification'
              AND state IN ('presence_confirmed', 'presence_rejected')
            """
        ).fetchone()[0]
    )


def hold_pointer_changes(
    db: sqlite3.Connection,
    before: dict[int, int],
    hold_candidate_ids: tuple[int, ...],
) -> int:
    after = _presence_pointers(db)
    return sum(after[item] != before[item] for item in hold_candidate_ids)


def test_complete_four_person_five_candidate_presence_acceptance(
    presence_db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_calls: list[str] = []

    def reject_external(*_args: object, **_kwargs: object) -> object:
        external_calls.append("external")
        raise AssertionError("real external resource was invoked")

    monkeypatch.setattr(subprocess, "Popen", reject_external)
    monkeypatch.setattr(subprocess, "run", reject_external)
    monkeypatch.setattr(socket, "create_connection", reject_external)
    monkeypatch.setattr(urllib.request, "urlopen", reject_external)
    source_job_id, candidates = _seed_exact_candidates(presence_db)
    assert len(candidates) == 4
    assert tuple(len(items) for items in candidates.values()) == (5, 5, 5, 5)
    assert table_count(presence_db, "subject_video_candidates") == 20
    assert table_count(presence_db, "videos") == 20
    assert table_count(presence_db, "video_metadata_snapshots") == 20
    assert table_count(presence_db, "discovery_observations") == 20

    reference_audio_root = (tmp_path / "reference-audio").resolve()
    reference_audio_root.mkdir()
    reference_settings = Settings(
        data_dir=tmp_path / "private-runtime-data",
        database_path=tmp_path / "unused.sqlite3",
        temp_audio_dir=reference_audio_root,
    )
    model_a, _ = fake_runtime_attestation(tmp_path / "model-a")
    model_b, _ = fake_runtime_attestation(tmp_path / "model-b")
    models = (
        replace(
            model_a,
            model_name="3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            model_version="sherpa-onnx-1.13.4",
        ),
        replace(
            model_b,
            model_name="wespeaker_zh_cnceleb_resnet34.onnx",
            model_version="sherpa-onnx-1.13.4",
        ),
    )
    reference_media = SyntheticReferenceMedia(reference_audio_root)
    reference_scorer = SyntheticReferenceScorer()
    reference_service = VoiceReferenceService(
        presence_db,
        media=reference_media,
        scorer=reference_scorer,
        retention=RetentionService(
            presence_db, reference_settings, clock=lambda: NOW
        ),
        clock=lambda: NOW,
    )
    _approve_complete_references(reference_service, candidates)
    calibration = reference_service.calibrate(tuple(reversed(models)))
    activation = reference_service.activate_calibration(calibration)

    assert calibration.model_name == models[1].model_name
    assert calibration.subject_boundary == 0.75
    assert calibration.interviewer_boundary == 0.10
    assert calibration.margin == pytest.approx(0.65)
    assert len(activation.reference_profile_ids) == 4
    assert len(reference_media.calls) == 48
    assert reference_scorer.dry_run_calls == (
        (models[0].model_name, 20),
        (models[1].model_name, 20),
    )
    assert table_count(presence_db, "audit_events") == 24
    assert table_count(presence_db, "voice_reference_calibrations") == 1
    assert table_count(presence_db, "voice_reference_profiles") == 4
    assert table_count(presence_db, "voice_reference_clips") == 24
    assert table_count(presence_db, "voice_reference_features") == 4
    assert table_count(presence_db, "speaker_threshold_configs") == 1
    _assert_no_audio_files(reference_audio_root)

    service = PresenceVerificationService(presence_db, clock=lambda: NOW)
    preview = service.preview_pilot()
    creation = service.create_pilot(preview.preview_hash)
    assert len(preview.candidates) == PILOT_CANDIDATE_COUNT
    assert Counter(item.subject_id for item in preview.candidates) == {
        subject_id: 5 for subject_id in candidates
    }
    assert len(set(creation.candidate_ids)) == PILOT_CANDIDATE_COUNT
    assert creation.candidate_ids == tuple(
        item.candidate_id for item in preview.candidates
    )
    _assert_exact_pilot_jobs(
        presence_db,
        source_job_id=source_job_id,
        pilot_job_ids=creation.job_ids,
    )

    presence_db.execute("SAVEPOINT hidden_job_mutation")
    try:
        presence_db.execute(
            """
            INSERT INTO jobs(
                job_kind, manifest_hash, total_units, status,
                created_at, updated_at
            ) VALUES ('video_pipeline', ?, 1, 'queued', ?, ?)
            """,
            (sha256_text("hidden-21st-job"), utc_iso(NOW), utc_iso(NOW)),
        )
        with pytest.raises(AssertionError):
            _assert_exact_pilot_jobs(
                presence_db,
                source_job_id=source_job_id,
                pilot_job_ids=creation.job_ids,
            )
    finally:
        presence_db.execute("ROLLBACK TO hidden_job_mutation")
        presence_db.execute("RELEASE hidden_job_mutation")
    _assert_exact_pilot_jobs(
        presence_db,
        source_job_id=source_job_id,
        pilot_job_ids=creation.job_ids,
    )

    frozen_pointers = _presence_pointers(presence_db)
    proposal_scores = (
        *((0.90, 0.82) for _ in range(7)),
        *((0.05, 0.08) for _ in range(7)),
        *((0.50, 0.45) for _ in range(6)),
    )
    adapter = SequencedPresenceAdapter(proposal_scores)
    selected_runtime = next(
        model for model in models if model.model_name == calibration.model_name
    )
    repository = VoiceVerificationRepository(presence_db)
    first_manifest = repository.get_manifest_for_job(creation.job_ids[0])
    crash_count = 0

    def crash_after_normalize(_job_id: int, unit_key: str) -> None:
        nonlocal crash_count
        if crash_count == 0 and unit_key == "audio:normalize":
            crash_count += 1
            raise SimulatedPresenceCrash

    worker_tmp = tmp_path / "worker"
    worker_tmp.mkdir()
    crashing = presence_worker_harness(
        presence_db,
        worker_tmp,
        SimpleNamespace(snapshot=first_manifest.snapshot),
        adapter=adapter,
        runtime=selected_runtime,
        after_unit_committed=crash_after_normalize,
    )
    with pytest.raises(SimulatedPresenceCrash):
        crashing.worker.run_once()
    resumed = presence_worker_harness(
        presence_db,
        worker_tmp,
        SimpleNamespace(snapshot=first_manifest.snapshot),
        adapter=adapter,
        runtime=selected_runtime,
    )
    summaries = (resumed.worker.run_once(),) + tuple(
        resumed.worker.run_once() for _ in range(PILOT_JOB_COUNT - 1)
    )
    empty_wake = resumed.worker.run_once()

    assert crash_count == 1
    assert tuple(item.job_id for item in summaries) == creation.job_ids
    assert all(item.succeeded_jobs == 1 for item in summaries)
    assert all(item.failed_jobs == 0 for item in summaries)
    assert all(item.failed_code is None for item in summaries)
    assert empty_wake.job_id is None
    assert len(adapter.calls) == 20
    assert model_only_presence_changes(presence_db, frozen_pointers) == 0
    presence_db.execute("SAVEPOINT model_writer_mutation")
    try:
        mutated_candidate_id = creation.candidate_ids[0]
        mutation_time = NOW + timedelta(hours=1)
        mutation_evidence = sha256_text("synthetic-model-only-evidence")
        mutation_decision_hash = canonical_presence_decision_hash(
            candidate_id=mutated_candidate_id,
            state=PresenceState.CONFIRMED,
            decision_origin=PresenceOrigin.VOICE_VERIFICATION,
            evidence_ref="synthetic-model-only",
            evidence_hash=mutation_evidence,
            created_at=mutation_time,
        )
        mutation_decision_id = presence_db.execute(
            """
            INSERT INTO presence_decisions(
                candidate_id, state, decision_origin, evidence_ref,
                evidence_hash, decision_hash, created_at
            ) VALUES (?, 'presence_confirmed', 'voice_verification',
                      'synthetic-model-only', ?, ?, ?)
            """,
            (
                mutated_candidate_id,
                mutation_evidence,
                mutation_decision_hash,
                utc_iso(mutation_time),
            ),
        ).lastrowid
        presence_db.execute(
            """
            UPDATE subject_video_candidates
            SET current_presence_decision_id=? WHERE id=?
            """,
            (mutation_decision_id, mutated_candidate_id),
        )
        with pytest.raises(AssertionError):
            assert model_only_presence_changes(
                presence_db, frozen_pointers
            ) == 0
    finally:
        presence_db.execute("ROLLBACK TO model_writer_mutation")
        presence_db.execute("RELEASE model_writer_mutation")
    assert model_only_presence_changes(presence_db, frozen_pointers) == 0

    manifest_rows = tuple(
        presence_db.execute(
            "SELECT id, job_id, candidate_id FROM voice_verification_manifests "
            "ORDER BY job_id"
        )
    )
    run_rows = tuple(
        presence_db.execute(
            "SELECT id, job_id, candidate_id, proposal "
            "FROM voice_verification_runs ORDER BY job_id"
        )
    )
    unit_rows = tuple(
        presence_db.execute(
            "SELECT job_id, unit_key, status FROM job_units "
            "ORDER BY job_id, ordinal"
        )
    )
    attempt_rows = tuple(
        presence_db.execute(
            "SELECT job_id, unit_key, attempt_no, result_status "
            "FROM job_unit_attempts ORDER BY job_id, unit_key, attempt_no"
        )
    )
    segment_rows = tuple(
        presence_db.execute(
            "SELECT run_id, ordinal FROM voice_verification_segments "
            "ORDER BY run_id, ordinal"
        )
    )
    assert len(manifest_rows) == 20
    assert tuple(row["job_id"] for row in manifest_rows) == creation.job_ids
    assert tuple(row["candidate_id"] for row in manifest_rows) == (
        creation.candidate_ids
    )
    assert len(run_rows) == 20
    assert tuple(row["job_id"] for row in run_rows) == creation.job_ids
    assert Counter(row["proposal"] for row in run_rows) == {
        VoiceProposal.LIKELY_PRESENT.value: 7,
        VoiceProposal.LIKELY_ABSENT.value: 7,
        VoiceProposal.NEEDS_REVIEW.value: 6,
    }
    assert len(unit_rows) == PILOT_UNIT_COUNT
    assert Counter(row["job_id"] for row in unit_rows) == {
        job_id: len(PRESENCE_UNITS) for job_id in creation.job_ids
    }
    assert all(row["status"] == "success" for row in unit_rows)
    expected_unit_keys = tuple(unit_key for unit_key, _stage in PRESENCE_UNITS)
    assert all(
        tuple(
            row["unit_key"]
            for row in unit_rows
            if row["job_id"] == job_id
        )
        == expected_unit_keys
        for job_id in creation.job_ids
    )
    assert len(attempt_rows) == PILOT_UNIT_COUNT
    assert Counter(
        (row["job_id"], row["unit_key"]) for row in attempt_rows
    ) == {
        (job_id, unit_key): 1
        for job_id in creation.job_ids
        for unit_key, _stage in PRESENCE_UNITS
    }
    assert all(row["attempt_no"] == 1 for row in attempt_rows)
    assert all(row["result_status"] == "success" for row in attempt_rows)
    assert len(segment_rows) == 40
    assert all(
        tuple(row["ordinal"] for row in segment_rows[index : index + 2])
        == (1, 2)
        for index in range(0, len(segment_rows), 2)
    )
    assert len(service.list_pending_reviews()) == 20

    review_actions = (
        *((ReviewAction.CONFIRM,) * 8),
        *((ReviewAction.REJECT,) * 7),
        *((ReviewAction.HOLD,) * 5),
    )
    pointer_before_reviews = _presence_pointers(presence_db)
    hold_candidate_ids: list[int] = []
    for row, action in zip(run_rows, review_actions, strict=True):
        result = service.review(
            ReviewCommand(
                run_id=row["id"],
                action=action,
                reason="listened to the synthetic cited segment",
                actor="local_user",
            )
        )
        expected_state = {
            ReviewAction.CONFIRM: PresenceState.CONFIRMED,
            ReviewAction.REJECT: PresenceState.REJECTED,
            ReviewAction.HOLD: PresenceState.UNVERIFIED,
        }[action]
        assert result.action is action
        assert result.current_state is expected_state
        if action is ReviewAction.HOLD:
            hold_candidate_ids.append(row["candidate_id"])

    confirm_review_count = review_actions.count(ReviewAction.CONFIRM)
    reject_review_count = review_actions.count(ReviewAction.REJECT)
    assert table_count(presence_db, "voice_verification_manifests") == 20
    assert table_count(presence_db, "voice_verification_runs") == 20
    assert table_count(presence_db, "voice_verification_segments") == 40
    assert table_count(presence_db, "voice_verification_reviews") == 20
    assert model_only_presence_changes(presence_db, frozen_pointers) == 0
    assert confirmed_or_rejected_changes(presence_db) == (
        confirm_review_count + reject_review_count
    )
    assert hold_pointer_changes(
        presence_db,
        pointer_before_reviews,
        tuple(hold_candidate_ids),
    ) == 0
    assert len(service.list_pending_reviews()) == 0
    assert table_count(presence_db, "presence_decisions") == 35

    decision_rows = tuple(
        presence_db.execute(
            "SELECT id, candidate_id, state, decision_origin "
            "FROM presence_decisions ORDER BY id"
        )
    )
    pointer_rows = tuple(
        presence_db.execute(
            "SELECT id, current_presence_decision_id "
            "FROM subject_video_candidates ORDER BY id"
        )
    )
    review_rows = tuple(
        presence_db.execute(
            "SELECT id, run_id, action, prior_presence_decision_id "
            "FROM voice_verification_reviews ORDER BY run_id"
        )
    )
    assert len(decision_rows) == 35
    assert len(pointer_rows) == 20
    assert len(review_rows) == 20
    assert Counter(row["action"] for row in review_rows) == {
        "confirm": 8,
        "reject": 7,
        "hold": 5,
    }

    artifact_rows = tuple(
        presence_db.execute(
            "SELECT id, kind, local_path, status "
            "FROM local_artifacts ORDER BY id"
        )
    )
    assert len(artifact_rows) == (
        REFERENCE_ARTIFACT_COUNT + PILOT_ARTIFACT_COUNT
    )
    assert all(row["kind"] == "audio" for row in artifact_rows)
    assert all(row["status"] == "deleted" for row in artifact_rows)
    assert all(not os.path.lexists(row["local_path"]) for row in artifact_rows)
    assert sum(row["status"] != "deleted" for row in artifact_rows) == 0
    assert table_count(presence_db, "transcription_chunks") == 0
    assert table_count(presence_db, "transcript_segments") == 0
    assert table_count(presence_db, "speaker_assignments") == 0
    assert all(
        table_count(presence_db, table) == 0
        for table in ANALYSIS_OUTPUT_TABLES
    )
    assert all(
        table_count(presence_db, table) == 0
        for table in AUTOMATIC_COLLECTION_TABLES
    )
    assert table_count(presence_db, "video_pipeline_job_binding_sets") == 20
    assert table_count(presence_db, "video_pipeline_job_bindings") == 20
    _assert_exact_pilot_jobs(
        presence_db,
        source_job_id=source_job_id,
        pilot_job_ids=creation.job_ids,
    )

    worker_audio_root = resumed.work_root
    _assert_no_audio_files(reference_audio_root, worker_audio_root)
    hidden_audio = worker_audio_root / ".hidden-audio"
    hidden_audio.write_bytes(b"synthetic-hidden-audio-mutation")
    try:
        with pytest.raises(AssertionError):
            _assert_no_audio_files(reference_audio_root, worker_audio_root)
    finally:
        hidden_audio.unlink(missing_ok=True)
    _assert_no_audio_files(reference_audio_root, worker_audio_root)
    assert external_calls == []

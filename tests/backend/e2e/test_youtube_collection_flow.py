from __future__ import annotations

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.workers.scheduled_sync import WorkerSummary
from tests.backend.e2e.synthetic_fixture import (
    SYNTHETIC_DISCOVERY,
    run_synthetic_youtube_collection,
)


APPROVED_PROFILES = {
    "木野内栄治": (
        ("UCXvjRTXoDa8tKwdkTaukGug",),
        ("木野内栄治",),
    ),
    "大川智宏": ((), ("大川智宏",)),
    "江守哲": (
        ("UCVXka7buS_WptsAzSE0LcKg",),
        ("江守哲",),
    ),
    "千竈 鉄平": (
        ("UCOfzLmXpI3qmZfV7_Cs1sYA",),
        ("千竈鉄平", "千竃鉄平"),
    ),
}
EXPECTED_DISCOVERY = {
    "木野内栄治": {
        "seed": ("vidseed0001",),
        "search": ("vidshared01",),
    },
    "大川智宏": {
        "seed": (),
        "search": ("vidsearch02",),
    },
    "江守哲": {
        "seed": ("vidseed0003",),
        "search": ("vidextern03",),
    },
    "千竈 鉄平": {
        "seed": ("vidseed0004",),
        "search": ("vidshared01",),
    },
}
EXPECTED_CURSOR_MAP = {
    "木野内栄治": {
        (
            "cross_channel_search",
            "927860dce39e32b70d8ae77e19e294943e6bee7a649283466b9b6b528894d7fb",
        ),
        ("seed_uploads", "UCXvjRTXoDa8tKwdkTaukGug"),
    },
    "大川智宏": {
        (
            "cross_channel_search",
            "dd32eb3462cb6cfbd7b43d13ae6065374e8706d9cec9c8567e0e53222fa62184",
        ),
    },
    "江守哲": {
        (
            "cross_channel_search",
            "4bd3d1ca1745313b530598b2acd095260cdde26e632007acf79ea2d42bc1d608",
        ),
        ("seed_uploads", "UCVXka7buS_WptsAzSE0LcKg"),
    },
    "千竈 鉄平": {
        (
            "cross_channel_search",
            "333e4376a3af7384978e14e3b231143ec943189fe7496734e925e2e8b723f338",
        ),
        ("seed_uploads", "UCOfzLmXpI3qmZfV7_Cs1sYA"),
    },
}
SYNC_UPPER_BOUND = "2026-08-18T03:04:05.000000Z"
EXPECTED_JOB_UNITS = (
    (
        "youtube:profile:1:seed:UCXvjRTXoDa8tKwdkTaukGug",
        "youtube_seed_discovery",
        1,
    ),
    ("youtube:profile:1:search", "youtube_search_discovery", 2),
    ("youtube:profile:2:search", "youtube_search_discovery", 3),
    (
        "youtube:profile:3:seed:UCVXka7buS_WptsAzSE0LcKg",
        "youtube_seed_discovery",
        4,
    ),
    ("youtube:profile:3:search", "youtube_search_discovery", 5),
    (
        "youtube:profile:4:seed:UCOfzLmXpI3qmZfV7_Cs1sYA",
        "youtube_seed_discovery",
        6,
    ),
    ("youtube:profile:4:search", "youtube_search_discovery", 7),
)


def test_four_profile_collection_flow_is_deterministic_and_stops_unverified(
    tmp_path,
):
    evidence = run_synthetic_youtube_collection(tmp_path)

    assert dict(SYNTHETIC_DISCOVERY) == EXPECTED_DISCOVERY
    assert evidence.worker_summary == WorkerSummary(1, 1, 0, 0)
    assert evidence.credential_read_count == len(evidence.safe_requests) > 0
    assert evidence.schedule_status_count == 1
    assert evidence.sleeper_delays == ()

    conn = open_database(evidence.settings.database_path)
    try:
        subject_rows = tuple(
            conn.execute(
                "SELECT id, canonical_name, is_active "
                "FROM analysis_subjects ORDER BY id"
            )
        )
        assert tuple(
            (row["canonical_name"], row["is_active"]) for row in subject_rows
        ) == tuple((name, 1) for name in APPROVED_PROFILES)
        assert "subject_kind" not in {
            row["name"] for row in conn.execute("PRAGMA table_info(analysis_subjects)")
        }

        stored_profiles = {}
        for row in conn.execute(
            "SELECT subject.canonical_name, profile.id AS profile_id, "
            "version.id AS version_id "
            "FROM analysis_subjects AS subject "
            "JOIN discovery_profiles AS profile ON profile.subject_id=subject.id "
            "JOIN discovery_profile_versions AS version "
            "ON version.id=profile.current_version_id "
            "WHERE subject.is_active=1 AND profile.is_active=1 ORDER BY subject.id"
        ):
            seeds = tuple(
                item["youtube_channel_id"]
                for item in conn.execute(
                    "SELECT youtube_channel_id FROM discovery_seed_channels "
                    "WHERE profile_version_id=? ORDER BY ordinal",
                    (row["version_id"],),
                )
            )
            terms = tuple(
                item["search_term"]
                for item in conn.execute(
                    "SELECT search_term FROM discovery_search_terms "
                    "WHERE profile_version_id=? ORDER BY ordinal",
                    (row["version_id"],),
                )
            )
            stored_profiles[row["canonical_name"]] = (seeds, terms)
        assert stored_profiles == APPROVED_PROFILES
        assert conn.execute(
            "SELECT COUNT(*) FROM discovery_profile_versions"
        ).fetchone()[0] == 4

        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM video_metadata_snapshots"
        ).fetchone()[0] == 6
        shared = conn.execute(
            "SELECT id FROM videos WHERE youtube_video_id='vidshared01'"
        ).fetchone()
        assert shared is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM videos WHERE youtube_video_id='vidshared01'"
        ).fetchone()[0] == 1
        assert tuple(
            row["canonical_name"]
            for row in conn.execute(
                "SELECT subject.canonical_name "
                "FROM subject_video_candidates AS candidate "
                "JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id "
                "JOIN analysis_subjects AS subject ON subject.id=profile.subject_id "
                "WHERE candidate.video_id=? ORDER BY subject.id",
                (shared["id"],),
            )
        ) == ("木野内栄治", "千竈 鉄平")
        assert conn.execute(
            "SELECT COUNT(*) FROM discovery_observations WHERE video_id=?",
            (shared["id"],),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM discovery_observations"
        ).fetchone()[0] == 7
        assert conn.execute(
            "SELECT COUNT(*) FROM subject_video_candidates"
        ).fetchone()[0] == 7
        candidate_ids = tuple(
            row["id"]
            for row in conn.execute(
                "SELECT id FROM subject_video_candidates ORDER BY id"
            )
        )
        all_decisions = tuple(
            conn.execute(
                "SELECT candidate.id AS candidate_id, decision.id AS decision_id, "
                "candidate.current_presence_decision_id, decision.state, "
                "decision.decision_origin "
                "FROM subject_video_candidates AS candidate "
                "JOIN presence_decisions AS decision "
                "ON decision.candidate_id=candidate.id "
                "ORDER BY candidate.id, decision.id"
            )
        )
        assert tuple(row["candidate_id"] for row in all_decisions) == candidate_ids
        assert tuple(
            (
                row["state"],
                row["decision_origin"],
                row["decision_id"] == row["current_presence_decision_id"],
            )
            for row in all_decisions
        ) == (("presence_unverified", "collection_initial", True),) * 7
        assert tuple(
            (row["state"], row["decision_origin"])
            for row in conn.execute(
                "SELECT decision.state, decision.decision_origin "
                "FROM subject_video_candidates AS candidate "
                "JOIN presence_decisions AS decision "
                "ON decision.id=candidate.current_presence_decision_id "
                "ORDER BY candidate.id"
            )
        ) == (("presence_unverified", "collection_initial"),) * 7

        cursor_map = {name: set() for name in APPROVED_PROFILES}
        durable_cursors = tuple(
            conn.execute(
                "SELECT subject.canonical_name, cursor.source_kind, "
                "cursor.source_key, cursor.completed_upper_bound, "
                "cursor.cursor_hash "
                "FROM youtube_source_cursors AS cursor "
                "JOIN discovery_profiles AS profile ON profile.id=cursor.profile_id "
                "JOIN analysis_subjects AS subject ON subject.id=profile.subject_id"
            )
        )
        for row in durable_cursors:
            assert row["completed_upper_bound"] == SYNC_UPPER_BOUND
            cursor_map[row["canonical_name"]].add(
                (row["source_kind"], row["source_key"])
            )
        assert cursor_map == EXPECTED_CURSOR_MAP
        proposed_cursors = tuple(
            conn.execute(
                "SELECT subject.canonical_name, proposal.source_kind, "
                "proposal.source_key, proposal.completed_upper_bound, "
                "proposal.cursor_hash "
                "FROM youtube_sync_proposed_cursors AS proposal "
                "JOIN discovery_profiles AS profile ON profile.id=proposal.profile_id "
                "JOIN analysis_subjects AS subject ON subject.id=profile.subject_id "
                "WHERE proposal.job_id=?",
                (evidence.job_id,),
            )
        )
        assert len(proposed_cursors) == 7
        assert {tuple(row) for row in proposed_cursors} == {
            tuple(row) for row in durable_cursors
        }

        all_jobs = tuple(
            conn.execute(
                "SELECT id, source_job_id, job_kind, total_units, status "
                "FROM jobs ORDER BY id"
            )
        )
        assert tuple(tuple(row) for row in all_jobs) == (
            (evidence.job_id, None, "youtube_sync", 7, "succeeded"),
        )
        assert {row["job_kind"] for row in all_jobs} == {"youtube_sync"}
        all_job_units = tuple(
            conn.execute(
                "SELECT job_id, unit_key, stage, ordinal, status, attempt_count "
                "FROM job_units ORDER BY job_id, ordinal"
            )
        )
        assert tuple(
            (row["unit_key"], row["stage"], row["ordinal"])
            for row in all_job_units
        ) == EXPECTED_JOB_UNITS
        assert tuple(
            (row["job_id"], row["status"], row["attempt_count"])
            for row in all_job_units
        ) == ((evidence.job_id, "success", 1),) * len(EXPECTED_JOB_UNITS)

        job = conn.execute(
            "SELECT job.status, job.job_kind, job.manifest_hash, "
            "job.total_units, manifest.manifest_hash AS youtube_manifest_hash "
            "FROM jobs AS job JOIN youtube_sync_manifests AS manifest "
            "ON manifest.job_id=job.id WHERE job.id=?",
            (evidence.job_id,),
        ).fetchone()
        assert job is not None
        assert {
            "status": job["status"],
            "job_kind": job["job_kind"],
            "total_units": job["total_units"],
        } == {
            "status": "succeeded",
            "job_kind": "youtube_sync",
            "total_units": 7,
        }
        assert job["manifest_hash"] == job["youtube_manifest_hash"]
        assert conn.execute(
            "SELECT COUNT(*) FROM youtube_sync_manifest_profiles WHERE job_id=?",
            (evidence.job_id,),
        ).fetchone()[0] == 4
        assert tuple(
            (row["status"], row["attempt_count"])
            for row in conn.execute(
                "SELECT status, attempt_count FROM job_units "
                "WHERE job_id=? ORDER BY ordinal",
                (evidence.job_id,),
            )
        ) == (("success", 1),) * 7

        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "transcription_chunks",
                "transcript_segments",
                "speaker_assignments",
                "analysis_runs",
                "analysis_input_snapshots",
                "analysis_statements",
                "analysis_forecasts",
                "video_pipeline_job_binding_sets",
                "video_pipeline_job_bindings",
                "local_artifacts",
            )
        } == {
            "transcription_chunks": 0,
            "transcript_segments": 0,
            "speaker_assignments": 0,
            "analysis_runs": 0,
            "analysis_input_snapshots": 0,
            "analysis_statements": 0,
            "analysis_forecasts": 0,
            "video_pipeline_job_binding_sets": 0,
            "video_pipeline_job_bindings": 0,
            "local_artifacts": 0,
        }
    finally:
        conn.close()

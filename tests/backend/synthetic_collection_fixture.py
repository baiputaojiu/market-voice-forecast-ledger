from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import count

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository


_FIXTURE_COUNTER = count(1)


@dataclass(frozen=True, slots=True)
class SyntheticCollectionCandidate:
    subject_id: int
    profile_id: int
    profile_version_id: int
    video_id: int
    metadata_snapshot_id: int
    metadata_snapshot_hash: str
    observation_id: int
    observation_hash: str
    candidate_id: int
    presence_decision_id: int
    presence_decision_hash: str
    segment_id: int
    speaker_assignment_id: int
    cutoff: date


def create_synthetic_collection_candidate(
    conn,
    *,
    presence_state,
    assignment_kind,
    assigned_subject_id=None,
    subject_id=None,
    youtube_video_id=None,
    published_at=None,
    text_body=None,
    transcript_created_at=None,
    expires_at=None,
    segment_no=1,
    start_ms=1_000,
    end_ms=5_000,
) -> SyntheticCollectionCandidate:
    from market_voice_forecast_ledger.domain.discovery import (
        CanonicalVideoMetadata,
        DiscoverySourceKind,
        LiveState,
        PresenceOrigin,
        PresenceState,
        canonical_presence_decision_hash,
    )
    from market_voice_forecast_ledger.repositories.discovery import (
        DiscoveryRepository,
    )

    ordinal = next(_FIXTURE_COUNTER)
    observed_at = datetime(2026, 8, 17, 3, ordinal % 60, tzinfo=timezone.utc)
    published_at = published_at or observed_at - timedelta(days=2)
    cutoff = date(2026, 8, 18)
    youtube_video_id = youtube_video_id or f"fixture{ordinal:04d}"[-11:]
    source_key = f"synthetic-source-{ordinal}"
    observation_hash = sha256_text(
        canonical_json(
            {
                "job_id": ordinal,
                "profile_ordinal": ordinal,
                "source_key": source_key,
                "youtube_video_id": youtube_video_id,
            }
        )
    )

    with transaction(conn):
        sources = SourceRepository(conn)
        subject_id = subject_id or sources.create_subject(
            f"Synthetic Collection Subject {ordinal}"
        )
        if assigned_subject_id == "different":
            assigned_subject_id = sources.create_subject(
                f"Synthetic Different Subject {ordinal}"
            )
        repository = DiscoveryRepository(conn)
        profile = repository.create_profile_version(
            subject_id,
            seed_channel_ids=(),
            search_terms=(f"Synthetic Collection Subject {ordinal}",),
            created_at=observed_at,
        )
        job_cursor = conn.execute(
            """
            INSERT INTO jobs(
                job_kind, manifest_hash, total_units, status, created_at, updated_at
            ) VALUES ('youtube_sync', ?, 1, ?, ?, ?)
            """,
            (
                f"synthetic-sync-manifest-{ordinal}",
                JobStatus.SUCCEEDED.value,
                observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        metadata = CanonicalVideoMetadata.build(
            youtube_video_id=youtube_video_id,
            channel_id=f"UCfixture{ordinal:014d}",
            channel_title=f"Synthetic Channel {ordinal}",
            title=f"Synthetic Video {ordinal}",
            description=f"Synthetic Description {ordinal}",
            published_at=published_at,
            duration_seconds=600,
            live_state=LiveState.NOT_LIVE,
            actual_start_time=None,
            schema_version="youtube-video-metadata.v1",
            fetched_at=observed_at,
        )
        candidate = repository.create_initial_candidate(
            profile_id=profile.profile_id,
            job_id=job_cursor.lastrowid,
            metadata=metadata,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key=source_key,
            observation_hash=observation_hash,
            idempotency_key=f"synthetic-observation-{ordinal}",
            observed_at=observed_at,
        )

        current_decision_id = candidate.current_presence_decision_id
        current_decision_hash = repository.get_presence_decision(
            current_decision_id
        ).decision_hash
        requested_state = PresenceState(presence_state)
        if requested_state is not PresenceState.UNVERIFIED:
            evidence_ref = f"synthetic-verifier:{ordinal}"
            evidence_hash = sha256_text(evidence_ref)
            current_decision_hash = canonical_presence_decision_hash(
                candidate_id=candidate.id,
                state=requested_state,
                decision_origin=PresenceOrigin.VOICE_VERIFICATION,
                evidence_ref=evidence_ref,
                evidence_hash=evidence_hash,
                created_at=observed_at,
            )
            decision_cursor = conn.execute(
                """
                INSERT INTO presence_decisions(
                    candidate_id, state, decision_origin, evidence_ref,
                    evidence_hash, decision_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    requested_state.value,
                    PresenceOrigin.VOICE_VERIFICATION.value,
                    evidence_ref,
                    evidence_hash,
                    current_decision_hash,
                    observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            )
            current_decision_id = decision_cursor.lastrowid
            conn.execute(
                "UPDATE subject_video_candidates "
                "SET current_presence_decision_id=? WHERE id=?",
                (current_decision_id, candidate.id),
            )

        speakers = SpeakerRepository(conn)
        chunk_id = speakers.add_chunk(
            candidate.video_id,
            1,
            0,
            60_000,
            f"chunk-input-{ordinal}",
            f"chunk-output-{ordinal}",
            UnitStatus.SUCCESS,
        )
        segment_id = speakers.add_segment(
            candidate.video_id,
            chunk_id,
            segment_no,
            start_ms,
            end_ms,
            text_body or f"Synthetic subject statement {ordinal}",
            "speaker-1",
            transcript_created_at or observed_at,
            expires_at or observed_at + timedelta(days=365),
        )
        kind = AssignmentKind(assignment_kind)
        if assigned_subject_id is None and kind is AssignmentKind.SUBJECT:
            assigned_subject_id = subject_id
        if kind is not AssignmentKind.SUBJECT:
            assigned_subject_id = None
        speakers.save_assignment(
            SpeakerAssignment(
                segment_id=segment_id,
                assignment_kind=kind,
                assigned_subject_id=assigned_subject_id,
                assignment_origin=AssignmentOrigin.MANUAL,
                raw_match_score=None,
                model_name=None,
                model_version=None,
                threshold_config_version=None,
                evidence_hash=f"speaker-evidence-{ordinal}",
                assigned_at=observed_at,
            )
        )
        assignment_id = conn.execute(
            "SELECT id FROM speaker_assignments WHERE segment_id=?", (segment_id,)
        ).fetchone()[0]

    return SyntheticCollectionCandidate(
        subject_id=subject_id,
        profile_id=profile.profile_id,
        profile_version_id=profile.id,
        video_id=candidate.video_id,
        metadata_snapshot_id=candidate.metadata_snapshot_id,
        metadata_snapshot_hash=metadata.canonical_hash,
        observation_id=candidate.first_observation_id,
        observation_hash=observation_hash,
        candidate_id=candidate.id,
        presence_decision_id=current_decision_id,
        presence_decision_hash=current_decision_hash,
        segment_id=segment_id,
        speaker_assignment_id=assignment_id,
        cutoff=cutoff,
    )

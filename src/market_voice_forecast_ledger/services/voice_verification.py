import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Final, Literal

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.discovery import (
    DiscoveryProfileVersion,
    DiscoverySourceKind,
    PresenceState,
)
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import (
    ReviewAction,
    VoiceManifestSnapshot,
    VoiceProposal,
    build_presence_job_manifest,
)
from market_voice_forecast_ledger.repositories.discovery import (
    DiscoveryRepository,
)
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.repositories.retention import (
    RetentionRepository,
)
from market_voice_forecast_ledger.repositories.voice_verification import (
    ReferenceBundle,
    StoredCalibrationIdentity,
    StoredVoiceRun,
    VoiceVerificationRepository,
)
from market_voice_forecast_ledger.services.audit import validate_audit_reason
from market_voice_forecast_ledger.services.job_state import JobStateService


PRESENCE_VAD_CONTRACT_VERSION: Final = "vad-v1"
PRESENCE_SELECTION_CONTRACT_VERSION: Final = "presence-pilot-selection-v1"
_CALIBRATION_VERSION_PREFIX = "voice-calibration-"
_SQLITE_INT_MAX = 2**63 - 1
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])[a-z]:[\\/]"
    r"|(?<![\\/])(?:\\\\|//)[^\\/\s]"
    r"|(?<![A-Za-z0-9/])/(?!/)[^/\s])"
)
_INACTIVE_BOUND_JOB_STATUSES = frozenset(
    {JobStatus.STOPPED, JobStatus.SUCCEEDED}
)


@dataclass(frozen=True, slots=True)
class PilotCandidate:
    candidate_id: int
    video_id: int
    youtube_video_id: str
    profile_id: int
    profile_version_id: int
    profile_config_hash: str
    subject_id: int
    first_observation_id: int
    first_observation_hash: str
    metadata_snapshot_id: int
    metadata_snapshot_hash: str
    source_kind: DiscoverySourceKind
    published_at: datetime
    calibration_hash: str
    model_sha256: str
    feature_contract_hash: str
    manifest_snapshot: VoiceManifestSnapshot


@dataclass(frozen=True, slots=True)
class PilotReferenceIdentity:
    subject_id: int
    reference_profile_id: int
    bundle_hash: str


@dataclass(frozen=True, slots=True)
class PilotCalibrationSnapshot:
    calibration_hash: str
    expected_prior_fingerprint: str
    threshold_config_version: str
    subject_operator: str
    subject_boundary: float
    interviewer_operator: str
    interviewer_boundary: float
    model_name: str
    model_version: str
    adapter_version: str
    model_sha256: str
    feature_contract_hash: str
    activated_at: datetime
    references: tuple[PilotReferenceIdentity, ...]
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class PilotPreview:
    candidates: tuple[PilotCandidate, ...]
    calibration_snapshot: PilotCalibrationSnapshot
    preview_hash: str


@dataclass(frozen=True, slots=True)
class PilotCreation:
    candidate_ids: tuple[int, ...]
    job_ids: tuple[int, ...]
    manifest_ids: tuple[int, ...]
    preview_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewCommand:
    run_id: int
    action: ReviewAction
    reason: str
    actor: Literal["local_user"]


@dataclass(frozen=True, slots=True)
class ReviewSegmentDetail:
    start_ms: int
    end_ms: int
    score: float


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    run_id: int
    person_display_name: str
    watch_url: str
    youtube_video_id: str
    segments: tuple[ReviewSegmentDetail, ...]
    proposal: VoiceProposal
    model_name: str
    model_version: str
    adapter_version: str
    threshold_version: str


@dataclass(frozen=True, slots=True)
class ReviewResult:
    review_id: int
    run_id: int
    action: ReviewAction
    current_presence_decision_id: int
    current_state: PresenceState


class PresenceVerificationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        vad_contract_version: str = PRESENCE_VAD_CONTRACT_VERSION,
        selection_contract_version: str = (
            PRESENCE_SELECTION_CONTRACT_VERSION
        ),
    ) -> None:
        if (
            _SAFE_TOKEN.fullmatch(vad_contract_version) is None
            or _SAFE_TOKEN.fullmatch(selection_contract_version) is None
        ):
            raise DomainError(
                "PRESENCE_PILOT_CONFIG_INVALID",
                "presence pilot contract configuration is invalid",
            )
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._vad_contract_version = vad_contract_version
        self._selection_contract_version = selection_contract_version
        self._discovery = DiscoveryRepository(conn)
        self._jobs = JobRepository(conn)
        self._retention = RetentionRepository(conn)
        self._job_state = JobStateService(conn, clock=self._clock)
        self._voice = VoiceVerificationRepository(conn, clock=self._clock)

    def list_pending_reviews(self) -> tuple[ReviewDetail, ...]:
        error: DomainError | None = None
        try:
            owns_transaction = not self._conn.in_transaction
            if owns_transaction:
                self._conn.execute("BEGIN")
            try:
                runs = self._voice.list_pending_reviews()
                return tuple(self._review_detail(run.id) for run in runs)
            finally:
                if owns_transaction:
                    self._conn.rollback()
        except Exception:
            error = _review_unavailable_error()
        if error is not None:
            raise error
        raise _review_unavailable_error()

    def show_review(self, run_id: int) -> ReviewDetail:
        if not _positive_sqlite_int(run_id):
            raise _review_invalid_error()
        error: DomainError | None = None
        try:
            owns_transaction = not self._conn.in_transaction
            if owns_transaction:
                self._conn.execute("BEGIN")
            try:
                return self._review_detail(run_id)
            finally:
                if owns_transaction:
                    self._conn.rollback()
        except Exception:
            error = _review_unavailable_error()
        if error is not None:
            raise error
        raise _review_unavailable_error()

    def review(self, command: ReviewCommand) -> ReviewResult:
        validation_error: DomainError | None = None
        try:
            self._validate_review_command(command)
        except Exception:
            validation_error = _review_invalid_error()
        if validation_error is not None:
            raise validation_error
        if self._conn.in_transaction:
            raise DomainError(
                "PRESENCE_REVIEW_TRANSACTION_ACTIVE",
                "presence review owns its transaction",
            )

        result: ReviewResult | None = None
        storage_error: DomainError | None = None
        try:
            with transaction(self._conn):
                job_id = self._require_pending_review(command.run_id)
                self._review_detail(command.run_id)
                self._require_cleanup_receipt(job_id)
                review_id = self._voice.add_review_and_decision(command)
                result = self._review_result(command, review_id)
        except DomainError as cause:
            if cause.code == "PRESENCE_REVIEW_STALE":
                storage_error = _review_stale_error()
            else:
                storage_error = _review_failed_error()
        except Exception:
            storage_error = _review_failed_error()
        if storage_error is not None:
            raise storage_error
        if result is None:
            raise _review_failed_error()
        return result

    def _validate_review_command(self, command: object) -> None:
        if (
            type(command) is not ReviewCommand
            or not _positive_sqlite_int(command.run_id)
            or type(command.action) is not ReviewAction
            or type(command.reason) is not str
            or not 1 <= len(command.reason) <= 240
            or type(command.actor) is not str
            or command.actor != "local_user"
        ):
            raise _review_invalid_error()
        validate_audit_reason(self._conn, command.reason)

    def _require_pending_review(self, run_id: int) -> int:
        row = self._conn.execute(
            """
            SELECT run.job_id, run.candidate_id AS run_candidate_id,
                   manifest.candidate_id AS manifest_candidate_id,
                   manifest.presence_decision_id,
                   candidate.current_presence_decision_id,
                   typeof(run.job_id) AS type_job_id,
                   typeof(run.candidate_id) AS type_run_candidate_id,
                   typeof(manifest.candidate_id) AS type_manifest_candidate_id,
                   typeof(manifest.presence_decision_id) AS type_prior_id,
                   typeof(candidate.current_presence_decision_id)
                       AS type_current_id
            FROM voice_verification_runs AS run
            JOIN voice_verification_manifests AS manifest
              ON manifest.job_id=run.job_id
            JOIN subject_video_candidates AS candidate
              ON candidate.id=run.candidate_id
            WHERE run.id=?
            """,
            (run_id,),
        ).fetchone()
        if (
            row is None
            or any(
                row[key] != "integer"
                for key in (
                    "type_job_id",
                    "type_run_candidate_id",
                    "type_manifest_candidate_id",
                    "type_prior_id",
                    "type_current_id",
                )
            )
            or not _positive_sqlite_int(row["job_id"])
            or not _positive_sqlite_int(row["run_candidate_id"])
            or row["run_candidate_id"] != row["manifest_candidate_id"]
        ):
            raise ValueError("presence review identity is invalid")
        review_rows = tuple(
            self._conn.execute(
                """
                SELECT id, typeof(id) AS type_id
                FROM voice_verification_reviews
                WHERE run_id=? ORDER BY id
                """,
                (run_id,),
            )
        )
        if review_rows:
            raise _review_stale_error()
        if row["current_presence_decision_id"] != row["presence_decision_id"]:
            raise _review_stale_error()
        return row["job_id"]

    def _require_cleanup_receipt(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        cleanup = self._job_state.unit(job_id, "audio:cleanup")
        if (
            cleanup.status is not UnitStatus.SUCCESS
            or cleanup.external_input_hash is None
            or cleanup.output_hash is None
        ):
            raise ValueError("presence review cleanup is incomplete")
        self._retention.require_presence_cleanup_receipt(
            job_id,
            manifest_hash=job.manifest_hash,
            expected_external_input_hash=cleanup.external_input_hash,
            expected_output_hash=cleanup.output_hash,
        )

    def _review_detail(self, run_id: int) -> ReviewDetail:
        run = self._voice.get_run(run_id)
        artifacts = self._voice.require_job_artifacts(run.job_id)
        job = self._job_state.require_canonical_video_pipeline_job(run.job_id)
        if (
            job.status is not JobStatus.SUCCEEDED
            or artifacts.run is None
            or artifacts.run != run
        ):
            raise ValueError("presence review job is incomplete")
        frozen = self._discovery.get_presence_decision(
            artifacts.manifest.snapshot.presence_decision_id
        )
        if (
            frozen.candidate_id != run.candidate_id
            or frozen.state is not PresenceState.UNVERIFIED
            or frozen.decision_hash
            != artifacts.manifest.snapshot.presence_decision_hash
        ):
            raise ValueError("presence review prior decision is invalid")
        identity = self._review_public_identity(run)
        rounded_segments = tuple(
            ReviewSegmentDetail(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                score=_round_public_score(segment.raw_match_score),
            )
            for segment in run.segments
        )
        if not rounded_segments:
            raise ValueError("presence review segments are unavailable")
        snapshot = artifacts.manifest.snapshot
        return ReviewDetail(
            run_id=run.id,
            person_display_name=identity["canonical_name"],
            watch_url=(
                "https://www.youtube.com/watch?v="
                f"{identity['youtube_video_id']}"
            ),
            youtube_video_id=identity["youtube_video_id"],
            segments=rounded_segments,
            proposal=run.proposal,
            model_name=snapshot.model_name,
            model_version=snapshot.model_version,
            adapter_version=snapshot.adapter_version,
            threshold_version=snapshot.threshold_config_version,
        )

    def _review_public_identity(self, run: StoredVoiceRun) -> sqlite3.Row:
        row = self._conn.execute(
            """
            SELECT subject.id AS subject_id, subject.canonical_name,
                   video.youtube_video_id,
                   typeof(subject.id) AS type_subject_id,
                   typeof(subject.canonical_name) AS type_canonical_name,
                   typeof(video.youtube_video_id) AS type_youtube_video_id
            FROM voice_verification_runs AS run
            JOIN voice_verification_manifests AS manifest
              ON manifest.job_id=run.job_id
            JOIN subject_video_candidates AS candidate
              ON candidate.id=manifest.candidate_id
             AND candidate.profile_id=manifest.profile_id
             AND candidate.video_id=manifest.video_id
            JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id
            JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
            JOIN videos AS video ON video.id=candidate.video_id
            WHERE run.id=? AND run.candidate_id=candidate.id
            """,
            (run.id,),
        ).fetchone()
        if (
            row is None
            or row["type_subject_id"] != "integer"
            or row["type_canonical_name"] != "text"
            or row["type_youtube_video_id"] != "text"
            or not _positive_sqlite_int(row["subject_id"])
            or row["subject_id"]
            != self._voice.require_job_artifacts(run.job_id).reference.subject_id
            or not _is_public_display_name(row["canonical_name"])
            or type(row["youtube_video_id"]) is not str
            or _YOUTUBE_VIDEO_ID.fullmatch(row["youtube_video_id"]) is None
        ):
            raise ValueError("presence review public identity is invalid")
        return row

    def _review_result(
        self,
        command: ReviewCommand,
        review_id: int,
    ) -> ReviewResult:
        if not _positive_sqlite_int(review_id):
            raise ValueError("presence review result identity is invalid")
        run = self._voice.get_run(command.run_id)
        artifacts = self._voice.require_job_artifacts(run.job_id)
        job = self._job_state.require_canonical_video_pipeline_job(run.job_id)
        row = self._conn.execute(
            """
            SELECT current_presence_decision_id,
                   typeof(current_presence_decision_id) AS type_current_id
            FROM subject_video_candidates WHERE id=?
            """,
            (run.candidate_id,),
        ).fetchone()
        if (
            job.status is not JobStatus.SUCCEEDED
            or artifacts.run != run
            or row is None
            or row["type_current_id"] != "integer"
            or not _positive_sqlite_int(row["current_presence_decision_id"])
        ):
            raise ValueError("presence review result is invalid")
        decision = self._discovery.get_presence_decision(
            row["current_presence_decision_id"]
        )
        expected_state = {
            ReviewAction.CONFIRM: PresenceState.CONFIRMED,
            ReviewAction.REJECT: PresenceState.REJECTED,
            ReviewAction.HOLD: PresenceState.UNVERIFIED,
        }[command.action]
        if (
            decision.candidate_id != run.candidate_id
            or decision.state is not expected_state
            or (
                command.action is ReviewAction.HOLD
                and decision.id
                != artifacts.manifest.snapshot.presence_decision_id
            )
        ):
            raise ValueError("presence review result is invalid")
        return ReviewResult(
            review_id=review_id,
            run_id=command.run_id,
            action=command.action,
            current_presence_decision_id=decision.id,
            current_state=decision.state,
        )

    def preview_pilot(self) -> PilotPreview:
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN")
        try:
            return self._preview_pilot_in_transaction()
        finally:
            if owns_transaction:
                self._conn.rollback()

    def create_pilot(self, expected_preview_hash: str) -> PilotCreation:
        if type(expected_preview_hash) is not str or _HASH.fullmatch(
            expected_preview_hash
        ) is None:
            raise DomainError(
                "PRESENCE_PILOT_PREVIEW_INVALID",
                "presence pilot preview identity is invalid",
            )
        if self._conn.in_transaction:
            raise DomainError(
                "PRESENCE_PILOT_TRANSACTION_ACTIVE",
                "presence pilot creation owns its transaction",
            )
        with transaction(self._conn):
            preview = self._preview_pilot_in_transaction()
            if preview.preview_hash != expected_preview_hash:
                raise DomainError(
                    "PRESENCE_PILOT_CHANGED",
                    "presence pilot inputs changed after preview",
                )
            created_at = self._clock()
            if not _is_exact_utc(created_at):
                raise DomainError(
                    "PRESENCE_PILOT_TIME_INVALID",
                    "presence pilot creation time is invalid",
                )
            job_ids: list[int] = []
            manifest_ids: list[int] = []
            for candidate in preview.candidates:
                manifest = build_presence_job_manifest(
                    candidate.manifest_snapshot
                )
                job_id = self._job_state.create_video_pipeline_in_transaction(
                    manifest,
                    (candidate.candidate_id,),
                    created_at=created_at,
                )
                manifest_id = self._voice.add_manifest(
                    job_id,
                    candidate.manifest_snapshot,
                    created_at=created_at,
                )
                job_ids.append(job_id)
                manifest_ids.append(manifest_id)
            if len(job_ids) != 20 or len(manifest_ids) != 20:
                raise DomainError(
                    "PRESENCE_PILOT_INSUFFICIENT",
                    "presence pilot requires exactly twenty candidates",
                )
            return PilotCreation(
                candidate_ids=tuple(
                    item.candidate_id for item in preview.candidates
                ),
                job_ids=tuple(job_ids),
                manifest_ids=tuple(manifest_ids),
                preview_hash=preview.preview_hash,
                created_at=created_at,
            )

    def _preview_pilot_in_transaction(self) -> PilotPreview:
        profiles = self._discovery.list_active_profile_versions()
        if (
            len(profiles) != 4
            or len({item.profile_id for item in profiles}) != 4
            or len({item.subject_id for item in profiles}) != 4
        ):
            _raise_insufficient()
        references, calibration, calibration_snapshot = (
            self._active_references()
        )
        profile_subjects = {item.subject_id for item in profiles}
        if set(references) != profile_subjects:
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot references do not match active profiles",
            )

        selected: list[PilotCandidate] = []
        for profile in profiles:
            reference = references[profile.subject_id]
            eligible = self._eligible_candidates(
                profile=profile,
                reference=reference,
                calibration=calibration,
            )
            profile_selection = self._select_profile_candidates(
                eligible,
                has_seed=bool(profile.seed_channel_ids),
            )
            if len(profile_selection) != 5:
                _raise_insufficient()
            selected.extend(profile_selection)
        candidates = tuple(selected)
        if (
            len(candidates) != 20
            or len({item.candidate_id for item in candidates}) != 20
        ):
            _raise_insufficient()
        preview_hash = sha256_text(
            canonical_json(
                {
                    "calibration": _calibration_preview_payload(
                        calibration_snapshot
                    ),
                    "candidates": [
                        _candidate_preview_payload(item) for item in candidates
                    ],
                    "schema": "presence-pilot-preview.v1",
                }
            )
        )
        return PilotPreview(
            candidates=candidates,
            calibration_snapshot=calibration_snapshot,
            preview_hash=preview_hash,
        )

    def _active_references(
        self,
    ) -> tuple[
        dict[int, ReferenceBundle],
        StoredCalibrationIdentity,
        PilotCalibrationSnapshot,
    ]:
        reference_ids = self._voice.list_active_reference_profile_ids()
        if len(reference_ids) != 4:
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot requires four active references",
            )
        bundles = tuple(
            self._voice.get_reference_bundle(reference_id)
            for reference_id in reference_ids
        )
        if (
            len({item.subject_id for item in bundles}) != 4
            or len({item.threshold_config_version for item in bundles}) != 1
            or len({(item.model_name, item.model_version) for item in bundles})
            != 1
            or len({item.adapter_version for item in bundles}) != 1
            or any(not item.is_active for item in bundles)
        ):
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot active reference set is invalid",
            )
        threshold_version = bundles[0].threshold_config_version
        if not threshold_version.startswith(_CALIBRATION_VERSION_PREFIX):
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot active calibration identity is invalid",
            )
        calibration_hash = threshold_version[
            len(_CALIBRATION_VERSION_PREFIX) :
        ]
        if _HASH.fullmatch(calibration_hash) is None:
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot active calibration identity is invalid",
            )
        calibration = self._voice.get_calibration_identity(calibration_hash)
        if calibration.threshold_config_version != threshold_version:
            raise DomainError(
                "PRESENCE_PILOT_REFERENCE_INVALID",
                "presence pilot active calibration identity is invalid",
            )
        threshold_rows = tuple(
            self._conn.execute(
                """
                SELECT *, typeof(version) AS type_version,
                       typeof(model_name) AS type_model_name,
                       typeof(model_version) AS type_model_version,
                       typeof(subject_operator) AS type_subject_operator,
                       typeof(subject_boundary) AS type_subject_boundary,
                       typeof(interviewer_operator) AS type_interviewer_operator,
                       typeof(interviewer_boundary) AS type_interviewer_boundary,
                       typeof(created_at) AS type_created_at,
                       typeof(is_active) AS type_is_active
                FROM speaker_threshold_configs
                WHERE is_active=1
                ORDER BY version
                """
            )
        )
        if len(threshold_rows) != 1:
            _raise_reference_invalid()
        threshold = threshold_rows[0]
        if (
            any(
                threshold[key] != expected
                for key, expected in (
                    ("type_version", "text"),
                    ("type_model_name", "text"),
                    ("type_model_version", "text"),
                    ("type_subject_operator", "text"),
                    ("type_subject_boundary", "real"),
                    ("type_interviewer_operator", "text"),
                    ("type_interviewer_boundary", "real"),
                    ("type_created_at", "text"),
                    ("type_is_active", "integer"),
                )
            )
            or threshold["version"] != threshold_version
            or threshold["model_name"] != bundles[0].model_name
            or threshold["model_version"] != bundles[0].model_version
            or threshold["subject_operator"] != "gte"
            or threshold["interviewer_operator"] != "lte"
            or not isfinite(threshold["subject_boundary"])
            or not isfinite(threshold["interviewer_boundary"])
            or threshold["subject_boundary"]
            <= threshold["interviewer_boundary"]
            or threshold["created_at"] != utc_iso(calibration.activated_at)
            or threshold["is_active"] != 1
            or any(item.created_at != calibration.activated_at for item in bundles)
        ):
            _raise_reference_invalid()
        reference_identities = tuple(
            PilotReferenceIdentity(
                subject_id=item.subject_id,
                reference_profile_id=item.reference_profile_id,
                bundle_hash=_reference_bundle_hash(item),
            )
            for item in bundles
        )
        snapshot_values = {
            "activated_at": utc_iso(calibration.activated_at),
            "adapter_version": bundles[0].adapter_version,
            "calibration_hash": calibration.calibration_hash,
            "expected_prior_fingerprint": (
                calibration.expected_prior_fingerprint
            ),
            "feature_contract_hash": calibration.feature_contract_hash,
            "interviewer_boundary": threshold["interviewer_boundary"],
            "interviewer_operator": threshold["interviewer_operator"],
            "model_name": bundles[0].model_name,
            "model_sha256": calibration.model_sha256,
            "model_version": bundles[0].model_version,
            "references": [asdict(item) for item in reference_identities],
            "schema": "presence-pilot-active-calibration.v1",
            "subject_boundary": threshold["subject_boundary"],
            "subject_operator": threshold["subject_operator"],
            "threshold_config_version": threshold_version,
        }
        snapshot = PilotCalibrationSnapshot(
            calibration_hash=calibration.calibration_hash,
            expected_prior_fingerprint=calibration.expected_prior_fingerprint,
            threshold_config_version=threshold_version,
            subject_operator=threshold["subject_operator"],
            subject_boundary=threshold["subject_boundary"],
            interviewer_operator=threshold["interviewer_operator"],
            interviewer_boundary=threshold["interviewer_boundary"],
            model_name=bundles[0].model_name,
            model_version=bundles[0].model_version,
            adapter_version=bundles[0].adapter_version,
            model_sha256=calibration.model_sha256,
            feature_contract_hash=calibration.feature_contract_hash,
            activated_at=calibration.activated_at,
            references=reference_identities,
            snapshot_hash=sha256_text(canonical_json(snapshot_values)),
        )
        return (
            {item.subject_id: item for item in bundles},
            calibration,
            snapshot,
        )

    def _eligible_candidates(
        self,
        *,
        profile: DiscoveryProfileVersion,
        reference: ReferenceBundle,
        calibration: StoredCalibrationIdentity,
    ) -> tuple[PilotCandidate, ...]:
        rows = tuple(
            self._conn.execute(
                """
                SELECT candidate.*,
                       typeof(candidate.id) AS type_id,
                       typeof(candidate.profile_id) AS type_profile_id,
                       typeof(candidate.video_id) AS type_video_id,
                       typeof(candidate.first_observation_id) AS type_first_id,
                       typeof(candidate.current_presence_decision_id)
                           AS type_current_id,
                       typeof(candidate.created_at) AS type_created_at
                FROM subject_video_candidates AS candidate
                WHERE candidate.profile_id=?
                ORDER BY candidate.id
                """,
                (profile.profile_id,),
            )
        )
        eligible: list[PilotCandidate] = []
        for row in rows:
            if any(
                row[key] != expected
                for key, expected in (
                    ("type_id", "integer"),
                    ("type_profile_id", "integer"),
                    ("type_video_id", "integer"),
                    ("type_first_id", "integer"),
                    ("type_current_id", "integer"),
                    ("type_created_at", "text"),
                )
            ):
                raise DomainError(
                    "STORED_DISCOVERY_CANDIDATE_INVALID",
                    "stored discovery candidate is invalid",
                )
            self._discovery._validate_candidate_row(
                row,
                profile_id=profile.profile_id,
                video_id=row["video_id"],
            )
            observation = self._first_observation(row["first_observation_id"])
            decision = self._discovery.get_presence_decision(
                row["current_presence_decision_id"]
            )
            has_active_binding = self._has_active_video_binding(row["id"])
            if decision.state is not PresenceState.UNVERIFIED:
                continue
            if has_active_binding:
                continue
            published_at = _parse_exact_utc(observation["published_at"])
            snapshot = VoiceManifestSnapshot(
                candidate_id=row["id"],
                video_id=row["video_id"],
                profile_id=profile.profile_id,
                presence_decision_id=decision.id,
                presence_decision_hash=decision.decision_hash,
                reference_profile_id=reference.reference_profile_id,
                reference_feature_hash=reference.feature_hash,
                threshold_config_version=reference.threshold_config_version,
                model_name=reference.model_name,
                model_version=reference.model_version,
                adapter_version=reference.adapter_version,
                vad_contract_version=self._vad_contract_version,
                selection_contract_version=self._selection_contract_version,
            )
            eligible.append(
                PilotCandidate(
                    candidate_id=row["id"],
                    video_id=row["video_id"],
                    youtube_video_id=observation["youtube_video_id"],
                    profile_id=profile.profile_id,
                    profile_version_id=profile.id,
                    profile_config_hash=profile.config_hash,
                    subject_id=profile.subject_id,
                    first_observation_id=observation["id"],
                    first_observation_hash=observation["observation_hash"],
                    metadata_snapshot_id=observation["metadata_snapshot_id"],
                    metadata_snapshot_hash=observation[
                        "metadata_snapshot_hash"
                    ],
                    source_kind=DiscoverySourceKind(
                        observation["source_kind"]
                    ),
                    published_at=published_at,
                    calibration_hash=calibration.calibration_hash,
                    model_sha256=calibration.model_sha256,
                    feature_contract_hash=calibration.feature_contract_hash,
                    manifest_snapshot=snapshot,
                )
            )
        return tuple(eligible)

    def _first_observation(self, observation_id: int) -> sqlite3.Row:
        observation = self._conn.execute(
            """
            SELECT observation.*, video.youtube_video_id,
                   snapshot.published_at
            FROM discovery_observations AS observation
            JOIN videos AS video ON video.id=observation.video_id
            JOIN video_metadata_snapshots AS snapshot
              ON snapshot.id=observation.metadata_snapshot_id
            WHERE observation.id=?
            """,
            (observation_id,),
        ).fetchone()
        if observation is None:
            raise DomainError(
                "STORED_DISCOVERY_OBSERVATION_INVALID",
                "stored discovery observation is invalid",
            )
        self._discovery._validate_stored_observation(observation)
        return observation

    def _has_active_video_binding(self, candidate_id: int) -> bool:
        binding_rows = tuple(
            self._conn.execute(
                """
                SELECT job_id,
                       typeof(job_id) AS type_job_id,
                       typeof(candidate_id) AS type_candidate_id
                FROM video_pipeline_job_bindings
                WHERE candidate_id=?
                ORDER BY job_id
                """,
                (candidate_id,),
            )
        )
        if any(
            row["type_job_id"] != "integer"
            or row["type_candidate_id"] != "integer"
            or not _positive_sqlite_int(row["job_id"])
            for row in binding_rows
        ):
            raise DomainError(
                "VIDEO_PIPELINE_BINDINGS_INVALID",
                "video-pipeline binding inventory is invalid",
            )
        job_ids = tuple(row["job_id"] for row in binding_rows)
        if len(job_ids) != len(set(job_ids)):
            raise DomainError(
                "VIDEO_PIPELINE_BINDINGS_INVALID",
                "video-pipeline binding inventory is invalid",
            )
        active = False
        for job_id in job_ids:
            bindings = self._jobs.list_video_pipeline_binding_ids(job_id)
            if candidate_id not in bindings:
                raise DomainError(
                    "VIDEO_PIPELINE_BINDINGS_INVALID",
                    "video-pipeline binding inventory is invalid",
                )
            try:
                job = self._job_state.require_canonical_video_pipeline_job(
                    job_id
                )
            except (DomainError, TypeError, ValueError) as cause:
                raise DomainError(
                    "VIDEO_PIPELINE_BINDINGS_INVALID",
                    "video-pipeline binding inventory is invalid",
                ) from cause
            if self._conn.execute(
                "SELECT 1 FROM voice_verification_manifests WHERE job_id=?",
                (job_id,),
            ).fetchone() is not None:
                self._voice.get_manifest_for_job(job_id)
            if job.status not in _INACTIVE_BOUND_JOB_STATUSES:
                active = True
        return active

    @staticmethod
    def _select_profile_candidates(
        eligible: tuple[PilotCandidate, ...],
        *,
        has_seed: bool,
    ) -> tuple[PilotCandidate, ...]:
        newest = _newest(eligible)
        selected: list[PilotCandidate] = []

        def take_source(source: DiscoverySourceKind, count: int) -> None:
            for candidate in newest:
                if len(
                    tuple(
                        item
                        for item in selected
                        if item.source_kind is source
                    )
                ) >= count:
                    return
                if (
                    candidate.source_kind is source
                    and candidate.candidate_id
                    not in {item.candidate_id for item in selected}
                ):
                    selected.append(candidate)

        if not has_seed:
            searches = tuple(
                item
                for item in newest
                if item.source_kind
                is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            )
            selected.extend(searches[:4])
            if len(searches) >= 5:
                selected.append(_oldest(searches[4:])[0])
                return tuple(selected)
            selected_ids = {item.candidate_id for item in selected}
            for candidate in newest:
                if len(selected) >= 5:
                    break
                if candidate.candidate_id not in selected_ids:
                    selected.append(candidate)
                    selected_ids.add(candidate.candidate_id)
            return tuple(selected)

        if has_seed:
            take_source(DiscoverySourceKind.SEED_UPLOADS, 2)
            take_source(DiscoverySourceKind.CROSS_CHANNEL_SEARCH, 2)
        selected_ids = {item.candidate_id for item in selected}
        for candidate in newest:
            if len(selected) >= 4:
                break
            if candidate.candidate_id not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate.candidate_id)
        remaining = tuple(
            item for item in eligible if item.candidate_id not in selected_ids
        )
        if remaining:
            selected.append(_oldest(remaining)[0])
        return tuple(selected)


def _newest(
    candidates: tuple[PilotCandidate, ...],
) -> tuple[PilotCandidate, ...]:
    by_id = sorted(candidates, key=lambda item: item.candidate_id)
    return tuple(
        sorted(by_id, key=lambda item: item.published_at, reverse=True)
    )


def _oldest(
    candidates: tuple[PilotCandidate, ...],
) -> tuple[PilotCandidate, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda item: (item.published_at, item.candidate_id),
        )
    )


def _candidate_preview_payload(candidate: PilotCandidate) -> dict[str, object]:
    return {
        "calibration_hash": candidate.calibration_hash,
        "candidate_id": candidate.candidate_id,
        "feature_contract_hash": candidate.feature_contract_hash,
        "first_observation_hash": candidate.first_observation_hash,
        "first_observation_id": candidate.first_observation_id,
        "manifest_snapshot": asdict(candidate.manifest_snapshot),
        "metadata_snapshot_hash": candidate.metadata_snapshot_hash,
        "metadata_snapshot_id": candidate.metadata_snapshot_id,
        "model_sha256": candidate.model_sha256,
        "profile_config_hash": candidate.profile_config_hash,
        "profile_id": candidate.profile_id,
        "profile_version_id": candidate.profile_version_id,
        "published_at": utc_iso(candidate.published_at),
        "source_kind": candidate.source_kind.value,
        "subject_id": candidate.subject_id,
        "video_id": candidate.video_id,
        "youtube_video_id": candidate.youtube_video_id,
    }


def _calibration_preview_payload(
    snapshot: PilotCalibrationSnapshot,
) -> dict[str, object]:
    return {
        "activated_at": utc_iso(snapshot.activated_at),
        "adapter_version": snapshot.adapter_version,
        "calibration_hash": snapshot.calibration_hash,
        "expected_prior_fingerprint": snapshot.expected_prior_fingerprint,
        "feature_contract_hash": snapshot.feature_contract_hash,
        "interviewer_boundary": snapshot.interviewer_boundary,
        "interviewer_operator": snapshot.interviewer_operator,
        "model_name": snapshot.model_name,
        "model_sha256": snapshot.model_sha256,
        "model_version": snapshot.model_version,
        "references": [asdict(item) for item in snapshot.references],
        "snapshot_hash": snapshot.snapshot_hash,
        "subject_boundary": snapshot.subject_boundary,
        "subject_operator": snapshot.subject_operator,
        "threshold_config_version": snapshot.threshold_config_version,
    }


def _reference_bundle_hash(reference: ReferenceBundle) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": reference.adapter_version,
                "clips": [item.clip_hash for item in reference.clips],
                "created_at": utc_iso(reference.created_at),
                "feature": {
                    "created_at": utc_iso(reference.feature.created_at),
                    "dimension": reference.feature.dimension,
                    "encoding_version": reference.feature.encoding_version,
                    "feature_sha256": reference.feature.feature_sha256,
                    "float_dtype": reference.feature.float_dtype,
                },
                "feature_hash": reference.feature_hash,
                "is_active": reference.is_active,
                "model_name": reference.model_name,
                "model_version": reference.model_version,
                "reference_profile_id": reference.reference_profile_id,
                "schema": "presence-pilot-reference-bundle.v1",
                "subject_id": reference.subject_id,
                "threshold_config_version": reference.threshold_config_version,
            }
        )
    )


def _raise_reference_invalid() -> None:
    raise DomainError(
        "PRESENCE_PILOT_REFERENCE_INVALID",
        "presence pilot active calibration state is invalid",
    )


def _parse_exact_utc(value: object) -> datetime:
    if type(value) is not str:
        raise DomainError(
            "STORED_DISCOVERY_METADATA_INVALID",
            "stored discovery metadata is invalid",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as cause:
        raise DomainError(
            "STORED_DISCOVERY_METADATA_INVALID",
            "stored discovery metadata is invalid",
        ) from cause
    if not _is_exact_utc(parsed) or utc_iso(parsed) != value:
        raise DomainError(
            "STORED_DISCOVERY_METADATA_INVALID",
            "stored discovery metadata is invalid",
        )
    return parsed


def _is_exact_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is timezone.utc
        and value.utcoffset() == timedelta(0)
    )


def _positive_sqlite_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_INT_MAX


def _is_public_display_name(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 200
        and value == value.strip()
        and not any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
        and _ABSOLUTE_PATH.search(value) is None
        and "file://" not in value.casefold()
    )


def _round_public_score(value: float) -> float:
    rounded = round(value, 4)
    return 0.0 if rounded == 0.0 else rounded


def _review_invalid_error() -> DomainError:
    return DomainError(
        "PRESENCE_REVIEW_INVALID",
        "presence review input is invalid",
    )


def _review_stale_error() -> DomainError:
    return DomainError(
        "PRESENCE_REVIEW_STALE",
        "presence review is stale",
    )


def _review_unavailable_error() -> DomainError:
    return DomainError(
        "PRESENCE_REVIEW_UNAVAILABLE",
        "presence review detail is unavailable",
    )


def _review_failed_error() -> DomainError:
    return DomainError(
        "PRESENCE_REVIEW_FAILED",
        "presence review could not be saved",
    )


def _raise_insufficient() -> None:
    raise DomainError(
        "PRESENCE_PILOT_INSUFFICIENT",
        "presence pilot requires exactly four profiles and five candidates each",
    )

import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    LEGAL_TRANSITIONS,
    STAGE_ORDER,
    JobManifest,
    JobProgress,
    JobUnit,
    ManifestUnit,
    ResumePlan,
    StageProgress,
    effective_input_hash,
)
from market_voice_forecast_ledger.repositories.jobs import (
    JobRepository,
    StoredJob,
)


_SAFE_HASH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_SUCCESSOR_SOURCE_STATUSES = frozenset(
    {
        JobStatus.STOPPED,
        JobStatus.FAILED,
        JobStatus.SUCCEEDED,
    }
)


class JobStateService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._jobs = JobRepository(conn)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, manifest: JobManifest) -> int:
        normalized = self._validate_manifest(manifest)
        with transaction(self._conn):
            return self._jobs.create(
                normalized, source_job_id=None, created_at=self._clock()
            )

    def create_successor(
        self,
        source_job_id: int,
        manifest: JobManifest,
        artifact_hashes: Mapping[str, str],
        external_input_hashes: Mapping[str, str | None],
    ) -> tuple[int, ResumePlan]:
        normalized = self._validate_manifest(manifest)
        for artifact_hash in artifact_hashes.values():
            self._validate_hash(artifact_hash, "UNSAFE_ARTIFACT_HASH")
        for external_input_hash in external_input_hashes.values():
            if external_input_hash is not None:
                self._validate_hash(
                    external_input_hash, "UNSAFE_EXTERNAL_INPUT_HASH"
                )

        with transaction(self._conn):
            source_job = self._jobs.get(source_job_id)
            if source_job.status not in _SUCCESSOR_SOURCE_STATUSES:
                raise DomainError(
                    "SOURCE_JOB_NOT_READY_FOR_SUCCESSOR",
                    "a successor requires a stopped, failed, or succeeded job",
                )
            if source_job.kind is not normalized.kind:
                raise DomainError(
                    "SUCCESSOR_KIND_MISMATCH",
                    "a successor must use the source job kind",
                )
            self._stored_manifest(source_job)
            source_units = self._jobs.list_units(source_job_id)
            source_by_key = {unit.unit_key: unit for unit in source_units}

            successor_id = self._jobs.create(
                normalized,
                source_job_id=source_job_id,
                created_at=self._clock(),
            )
            successor_by_key = {
                unit.unit_key: unit
                for unit in self._jobs.list_units(successor_id)
            }
            reused: list[str] = []
            reused_outputs: dict[str, str] = {}

            for manifest_unit in normalized.units:
                source_unit = source_by_key.get(manifest_unit.unit_key)
                if source_unit is None or not self._manifest_fields_match(
                    source_unit, manifest_unit
                ):
                    continue
                if source_unit.status is not UnitStatus.SUCCESS:
                    continue
                if source_unit.output_hash is None:
                    continue
                if (
                    artifact_hashes.get(manifest_unit.unit_key)
                    != source_unit.output_hash
                ):
                    continue
                if manifest_unit.unit_key not in external_input_hashes:
                    continue
                supplied_external = external_input_hashes[manifest_unit.unit_key]
                if supplied_external != source_unit.external_input_hash:
                    continue
                if any(
                    key not in reused_outputs
                    for key in manifest_unit.dependency_keys
                ):
                    continue
                if not self._bound_input_matches(source_unit, source_by_key):
                    continue

                dependency_outputs = tuple(
                    reused_outputs[dependency.unit_key]
                    for dependency in sorted(
                        (
                            successor_by_key[key]
                            for key in manifest_unit.dependency_keys
                        ),
                        key=lambda item: item.ordinal,
                    )
                )
                successor_bound_input = effective_input_hash(
                    manifest_unit.declared_input_hash,
                    dependency_outputs,
                    supplied_external,
                )
                if successor_bound_input != source_unit.bound_input_hash:
                    continue

                self._jobs.reuse_unit(
                    successor_by_key[manifest_unit.unit_key],
                    source_job_id=source_job_id,
                    external_input_hash=supplied_external,
                    bound_input_hash=successor_bound_input,
                    output_hash=source_unit.output_hash,
                    reused_at=self._clock(),
                )
                reused.append(manifest_unit.unit_key)
                reused_outputs[manifest_unit.unit_key] = source_unit.output_hash

            if len(reused) == len(normalized.units):
                self._transition(
                    self._jobs.get(successor_id), JobStatus.RUNNING
                )
                self.succeed_job_in_transaction(successor_id)
            return successor_id, self._plan(successor_id, tuple(reused))

    def begin_unit(
        self,
        job_id: int,
        unit_key: str,
        external_input_hash: str | None = None,
    ) -> JobUnit:
        if external_input_hash is not None:
            self._validate_hash(
                external_input_hash, "UNSAFE_EXTERNAL_INPUT_HASH"
            )
        input_changed = False
        with transaction(self._conn):
            job = self._jobs.get(job_id)
            unit = self._jobs.get_unit(job_id, unit_key)
            if unit.status is not UnitStatus.PENDING:
                raise DomainError(
                    "JOB_UNIT_NOT_PENDING", "only a pending unit can begin"
                )
            if job.status not in {
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                JobStatus.RETRYING,
            }:
                raise DomainError(
                    "JOB_NOT_RUNNABLE", "job status does not allow a unit to begin"
                )

            units_by_key = {
                item.unit_key: item for item in self._jobs.list_units(job_id)
            }
            dependency_outputs: list[str] = []
            dependencies = sorted(
                (units_by_key[key] for key in unit.dependency_keys),
                key=lambda item: item.ordinal,
            )
            for dependency in dependencies:
                if (
                    dependency.status is not UnitStatus.SUCCESS
                    or dependency.output_hash is None
                ):
                    raise DomainError(
                        "UNIT_DEPENDENCY_NOT_SUCCESS",
                        "all declared dependencies must be successful",
                    )
                dependency_outputs.append(dependency.output_hash)

            computed_input = effective_input_hash(
                unit.declared_input_hash,
                dependency_outputs,
                external_input_hash,
            )
            if unit.bound_input_hash is not None and (
                unit.external_input_hash != external_input_hash
                or unit.bound_input_hash != computed_input
            ):
                if job.status in {JobStatus.RUNNING, JobStatus.RETRYING}:
                    self._transition(job, JobStatus.FAILED)
                input_changed = True
            else:
                if job.status in {JobStatus.QUEUED, JobStatus.RETRYING}:
                    self._transition(job, JobStatus.RUNNING)
                self._jobs.start_unit(
                    unit,
                    external_input_hash=external_input_hash,
                    bound_input_hash=computed_input,
                    started_at=self._clock(),
                )
        if input_changed:
            raise DomainError(
                "UNIT_INPUT_CHANGED",
                "a retried unit must use its originally bound input",
            )
        return self.unit(job_id, unit_key)

    def request_pause(self, job_id: int) -> JobStatus:
        with transaction(self._conn):
            job = self._jobs.get(job_id)
            self._transition(job, JobStatus.PAUSE_REQUESTED)
            if self._jobs.running_unit_count(job_id) == 0:
                self._transition(
                    self._jobs.get(job_id), JobStatus.PAUSED
                )
        return self.status(job_id)

    def request_stop(self, job_id: int) -> JobStatus:
        with transaction(self._conn):
            job = self._jobs.get(job_id)
            if job.status is JobStatus.RUNNING:
                self._transition(job, JobStatus.CANCEL_REQUESTED)
                if self._jobs.running_unit_count(job_id) == 0:
                    self._transition(
                        self._jobs.get(job_id), JobStatus.STOPPED
                    )
            elif job.status in {JobStatus.QUEUED, JobStatus.PAUSED}:
                self._transition(job, JobStatus.STOPPED)
            else:
                self._raise_invalid_transition(job.status, JobStatus.STOPPED)
        return self.status(job_id)

    def status(self, job_id: int) -> JobStatus:
        return self._jobs.get(job_id).status

    def unit(self, job_id: int, unit_key: str) -> JobUnit:
        return self._jobs.get_unit(job_id, unit_key)

    def complete_unit(
        self, job_id: int, unit_key: str, output_hash: str
    ) -> None:
        with transaction(self._conn):
            if self._jobs.get(job_id).kind is JobKind.ANALYSIS_SCOPE:
                raise DomainError(
                    "ANALYSIS_TRANSACTION_REQUIRED",
                    "analysis success requires a caller-owned artifact transaction",
                )
            self.complete_unit_in_transaction(job_id, unit_key, output_hash)

    def complete_unit_in_transaction(
        self, job_id: int, unit_key: str, output_hash: str
    ) -> None:
        self._require_transaction()
        self._validate_hash(output_hash, "UNSAFE_ARTIFACT_HASH")
        unit = self._jobs.get_unit(job_id, unit_key)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "JOB_UNIT_NOT_RUNNING", "only a running unit can succeed"
            )
        job = self._jobs.get(job_id)
        if job.status not in {
            JobStatus.RUNNING,
            JobStatus.PAUSE_REQUESTED,
            JobStatus.CANCEL_REQUESTED,
        }:
            raise DomainError(
                "JOB_NOT_RUNNING", "job status does not allow unit completion"
            )
        self._jobs.complete_unit(unit, output_hash, self._clock())
        self._settle_safe_boundary(job_id)

    def fail_unit(self, job_id: int, unit_key: str, error_code: str) -> None:
        with transaction(self._conn):
            self.fail_unit_in_transaction(job_id, unit_key, error_code)

    def fail_unit_in_transaction(
        self, job_id: int, unit_key: str, error_code: str
    ) -> None:
        self._require_transaction()
        if not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(
            error_code
        ):
            raise DomainError(
                "UNSAFE_ERROR_CODE", "unit failure requires a safe error code"
            )
        unit = self._jobs.get_unit(job_id, unit_key)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "JOB_UNIT_NOT_RUNNING", "only a running unit can fail"
            )
        job = self._jobs.get(job_id)
        if job.status not in {
            JobStatus.RUNNING,
            JobStatus.PAUSE_REQUESTED,
            JobStatus.CANCEL_REQUESTED,
        }:
            raise DomainError(
                "JOB_NOT_RUNNING", "job status does not allow unit failure"
            )
        self._jobs.fail_unit(unit, error_code, self._clock())
        current = self._jobs.get(job_id)
        if current.status in {
            JobStatus.RUNNING,
            JobStatus.PAUSE_REQUESTED,
        }:
            self._transition(current, JobStatus.FAILED)
        else:
            self._settle_safe_boundary(job_id)

    def succeed_job_in_transaction(self, job_id: int) -> None:
        self._require_transaction()
        job = self._jobs.get(job_id)
        self._stored_manifest(job)
        units = self._jobs.list_units(job_id)
        units_by_key = {unit.unit_key: unit for unit in units}
        if not units or any(unit.status is not UnitStatus.SUCCESS for unit in units):
            raise DomainError(
                "ALL_UNITS_MUST_SUCCEED",
                "every manifest unit must succeed before the job",
            )
        if any(not self._bound_input_matches(unit, units_by_key) for unit in units):
            raise DomainError(
                "UNIT_INPUT_HASH_MISMATCH",
                "a successful unit no longer matches its bound input",
            )
        self._transition(job, JobStatus.SUCCEEDED)

    def require_upstream_success(
        self, job_id: int, promotion_unit_key: str
    ) -> None:
        job = self._jobs.get(job_id)
        self._stored_manifest(job)
        units = self._jobs.list_units(job_id)
        units_by_key = {unit.unit_key: unit for unit in units}
        promotion = units_by_key.get(promotion_unit_key)
        if promotion is None:
            raise DomainError(
                "PROMOTION_UNIT_NOT_FOUND", "promotion unit is not in the manifest"
            )
        upstream = tuple(
            unit for unit in units if unit.ordinal < promotion.ordinal
        )
        if any(
            unit.status is not UnitStatus.SUCCESS or unit.output_hash is None
            for unit in upstream
        ):
            raise DomainError(
                "UPSTREAM_UNITS_INCOMPLETE",
                "every pre-promotion unit must be successful",
            )
        if any(
            not self._bound_input_matches(unit, units_by_key) for unit in upstream
        ):
            raise DomainError(
                "UPSTREAM_INPUT_HASH_MISMATCH",
                "a pre-promotion unit no longer matches its bound input",
            )

    def resume(
        self, job_id: int, artifact_hashes: Mapping[str, str]
    ) -> ResumePlan:
        for artifact_hash in artifact_hashes.values():
            self._validate_hash(artifact_hash, "UNSAFE_ARTIFACT_HASH")
        with transaction(self._conn):
            job = self._jobs.get(job_id)
            self._stored_manifest(job)
            if job.status in {JobStatus.STOPPED, JobStatus.CANCEL_REQUESTED}:
                raise DomainError(
                    "STOPPED_JOB_REQUIRES_SUCCESSOR",
                    "a stopped job can continue only through a successor",
                )
            if job.status is JobStatus.PAUSE_REQUESTED:
                raise DomainError(
                    "PAUSE_BOUNDARY_NOT_REACHED",
                    "a pause request can resume only after its safe boundary",
                )

            units = self._jobs.list_units(job_id)
            units_by_key = {unit.unit_key: unit for unit in units}
            if job.status is JobStatus.SUCCEEDED:
                if not all(
                    unit.status is UnitStatus.SUCCESS
                    and unit.output_hash is not None
                    and artifact_hashes.get(unit.unit_key) == unit.output_hash
                    and self._bound_input_matches(unit, units_by_key)
                    for unit in units
                ):
                    raise DomainError(
                        "SUCCEEDED_JOB_ARTIFACT_MISMATCH",
                        "a succeeded job cannot be mutated during resume",
                    )
                return self._plan(
                    job_id, tuple(unit.unit_key for unit in units)
                )
            if any(unit.status is UnitStatus.RUNNING for unit in units):
                raise DomainError(
                    "INTERRUPTED_RECOVERY_REQUIRED",
                    "running attempts require explicit interrupted recovery",
                )

            if job.status is JobStatus.FAILED:
                self._transition(job, JobStatus.RETRYING)
            elif job.status is JobStatus.PAUSED:
                self._transition(job, JobStatus.RUNNING)
            elif job.status not in {
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                JobStatus.RETRYING,
            }:
                raise DomainError(
                    "JOB_NOT_RESUMABLE", "job status cannot be resumed"
                )

            return self._reset_and_plan(
                job_id,
                units,
                artifact_hashes,
                reset_running=False,
                reset_failed=True,
            )

    def recover_interrupted(
        self, job_id: int, artifact_hashes: Mapping[str, str]
    ) -> ResumePlan:
        for artifact_hash in artifact_hashes.values():
            self._validate_hash(artifact_hash, "UNSAFE_ARTIFACT_HASH")

        stopped = False
        with transaction(self._conn):
            job = self._jobs.get(job_id)
            self._stored_manifest(job)
            units = self._jobs.list_units(job_id)

            if job.status is JobStatus.CANCEL_REQUESTED:
                plan = self._reset_and_plan(
                    job_id,
                    units,
                    artifact_hashes,
                    reset_running=True,
                    reset_failed=True,
                )
                self._transition(job, JobStatus.STOPPED)
                stopped = True
            elif job.status is JobStatus.PAUSE_REQUESTED:
                plan = self._reset_and_plan(
                    job_id,
                    units,
                    artifact_hashes,
                    reset_running=True,
                    reset_failed=True,
                )
                self._transition(job, JobStatus.PAUSED)
            elif job.status is JobStatus.FAILED:
                self._transition(job, JobStatus.RETRYING)
                plan = self._reset_and_plan(
                    job_id,
                    units,
                    artifact_hashes,
                    reset_running=True,
                    reset_failed=True,
                )
            elif job.status is JobStatus.RUNNING:
                plan = self._reset_and_plan(
                    job_id,
                    units,
                    artifact_hashes,
                    reset_running=True,
                    reset_failed=True,
                )
            else:
                raise DomainError(
                    "JOB_NOT_RECOVERABLE",
                    "job status has no interrupted worker to recover",
                )

        if stopped:
            raise DomainError(
                "STOPPED_JOB_REQUIRES_SUCCESSOR",
                "a stopped job can continue only through a successor",
            )
        return plan

    def _reset_and_plan(
        self,
        job_id: int,
        units: tuple[JobUnit, ...],
        artifact_hashes: Mapping[str, str],
        *,
        reset_running: bool,
        reset_failed: bool,
    ) -> ResumePlan:
        self._require_transaction()
        reset_at = self._clock()
        for unit in units:
            should_reset = (
                unit.status is UnitStatus.RUNNING and reset_running
            ) or (unit.status is UnitStatus.FAILED and reset_failed)
            if should_reset:
                self._jobs.reset_unit(
                    unit,
                    reason=(
                        "interrupted"
                        if unit.status is UnitStatus.RUNNING
                        else "retry"
                    ),
                    reset_at=reset_at,
                )

        units_by_key = {unit.unit_key: unit for unit in units}
        reused: list[str] = []
        reused_set: set[str] = set()
        for unit in units:
            if unit.status is not UnitStatus.SUCCESS:
                continue
            reusable = (
                unit.output_hash is not None
                and artifact_hashes.get(unit.unit_key) == unit.output_hash
                and all(key in reused_set for key in unit.dependency_keys)
                and self._bound_input_matches(unit, units_by_key)
            )
            if reusable:
                reused.append(unit.unit_key)
                reused_set.add(unit.unit_key)
            else:
                self._jobs.reset_unit(
                    unit, reason="verification_mismatch", reset_at=reset_at
                )

        return self._plan(job_id, tuple(reused))

    def progress(self, job_id: int) -> JobProgress:
        job = self._jobs.get(job_id)
        units = self._jobs.list_units(job_id)
        stages = tuple(
            StageProgress(
                stage=stage,
                completed=sum(
                    unit.status is UnitStatus.SUCCESS
                    for unit in units
                    if unit.stage is stage
                ),
                total=sum(unit.stage is stage for unit in units),
            )
            for stage in STAGE_ORDER
        )
        completed = sum(unit.status is UnitStatus.SUCCESS for unit in units)
        return JobProgress(stages, completed, job.total_units)

    def _validate_manifest(self, manifest: JobManifest) -> JobManifest:
        normalized = JobManifest.build(manifest.kind, manifest.units)
        if manifest.manifest_hash != normalized.manifest_hash:
            raise DomainError(
                "INVALID_MANIFEST_HASH",
                "manifest hash does not match its immutable unit fields",
            )
        return normalized

    def _stored_manifest(self, job: StoredJob) -> JobManifest:
        units = self._jobs.list_units(job.id)
        manifest = JobManifest.build(
            job.kind,
            tuple(
                ManifestUnit(
                    unit.unit_key,
                    unit.stage,
                    unit.ordinal,
                    unit.declared_input_hash,
                    unit.dependency_keys,
                    unit.execution_contract_hash,
                )
                for unit in units
            ),
        )
        if (
            manifest.manifest_hash != job.manifest_hash
            or len(units) != job.total_units
        ):
            raise DomainError(
                "STORED_MANIFEST_MISMATCH",
                "stored units do not match the immutable job manifest",
            )
        return manifest

    @staticmethod
    def _manifest_fields_match(
        unit: JobUnit, manifest_unit: ManifestUnit
    ) -> bool:
        return (
            unit.unit_key == manifest_unit.unit_key
            and unit.stage is manifest_unit.stage
            and unit.ordinal == manifest_unit.ordinal
            and unit.declared_input_hash == manifest_unit.declared_input_hash
            and unit.dependency_keys == manifest_unit.dependency_keys
            and unit.execution_contract_hash
            == manifest_unit.execution_contract_hash
        )

    @staticmethod
    def _bound_input_matches(
        unit: JobUnit, units_by_key: Mapping[str, JobUnit]
    ) -> bool:
        if unit.bound_input_hash is None:
            return False
        dependencies: list[JobUnit] = []
        for dependency_key in unit.dependency_keys:
            dependency = units_by_key.get(dependency_key)
            if (
                dependency is None
                or dependency.status is not UnitStatus.SUCCESS
                or dependency.output_hash is None
            ):
                return False
            dependencies.append(dependency)
        dependency_outputs = [
            dependency.output_hash
            for dependency in sorted(
                dependencies, key=lambda item: item.ordinal
            )
        ]
        return unit.bound_input_hash == effective_input_hash(
            unit.declared_input_hash,
            dependency_outputs,
            unit.external_input_hash,
        )

    def _transition(self, job: StoredJob, to_status: JobStatus) -> None:
        if to_status not in LEGAL_TRANSITIONS.get(job.status, frozenset()):
            self._raise_invalid_transition(job.status, to_status)
        self._jobs.transition_job(job.id, job.status, to_status, self._clock())

    @staticmethod
    def _raise_invalid_transition(
        from_status: JobStatus, to_status: JobStatus
    ) -> None:
        raise DomainError(
            "INVALID_JOB_TRANSITION",
            f"job cannot transition from {from_status.value} to {to_status.value}",
        )

    def _settle_safe_boundary(self, job_id: int) -> None:
        if self._jobs.running_unit_count(job_id) != 0:
            return
        job = self._jobs.get(job_id)
        if job.status is JobStatus.PAUSE_REQUESTED:
            self._transition(job, JobStatus.PAUSED)
        elif job.status is JobStatus.CANCEL_REQUESTED:
            self._transition(job, JobStatus.STOPPED)

    def _plan(
        self, job_id: int, reused_unit_keys: tuple[str, ...]
    ) -> ResumePlan:
        units = self._jobs.list_units(job_id)
        pending = tuple(
            unit.unit_key
            for unit in units
            if unit.status is UnitStatus.PENDING
        )
        return ResumePlan(
            reused_unit_keys=reused_unit_keys,
            pending_unit_keys=pending,
            next_unit_key=pending[0] if pending else None,
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "JOB_TRANSACTION_REQUIRED",
                "job state mutation requires an active caller transaction",
            )

    @staticmethod
    def _validate_hash(value: str, error_code: str) -> None:
        if not isinstance(value, str) or not _SAFE_HASH.fullmatch(value):
            raise DomainError(error_code, "hash metadata must be a safe token")

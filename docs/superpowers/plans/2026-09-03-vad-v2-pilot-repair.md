# VAD v2 Pilot Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce the corrected `vad-v2` execution contract and safely replace only the twenty invalid `vad-v1` presence-pilot jobs with queued `vad-v2` jobs after verified database and runtime-lock backups.

**Architecture:** Keep the normal presence pipeline append-only and place the exceptional mutation behind a one-shot repair service. A migration adds a repair ledger and default-deny delete triggers; the service derives an exact target inventory from immutable rows, binds apply to a canonical preview hash, backs up the database and all three runtime locks, temporarily authorizes only enumerated row identities inside one SQLite transaction, recreates jobs through the normal job-state service, and verifies preserved and changed fingerprints. The CLI exposes a read-only preview and an explicit hash-bound apply command; it never runs the presence worker.

**Tech Stack:** Python 3.14, SQLite 3, Pydantic 2, pytest, PowerShell 5.1/7, existing `market_voice_forecast_ledger` domain/repository/service/CLI layers.

**Spec:** `docs/superpowers/specs/2026-09-03-vad-v2-pilot-repair-design.md`

## Global Constraints

- The only source contract accepted for repair is `vad-v1`; the only destination contract is `vad-v2`.
- The repair target must be derived from database identities and canonical hashes; production row IDs and private paths must never be hard-coded or printed.
- Preview is read-only. Apply requires the exact 64-character lowercase preview hash returned by preview.
- Apply must refuse any target that is not exactly twenty succeeded jobs, seven successful units per job, one successful attempt per unit, the canonical event inventory, one sealed binding set and one binding per job, one manifest/run/segment per job, zero reviews, and three deleted local-artifact rows per job.
- Candidate order, current presence decisions, discovery/video rows, reference/calibration rows, and unrelated data must remain byte-for-byte canonically identical.
- Database and runtime-lock backups are exclusive-create artifacts and must never overwrite an existing path.
- Runtime locks `runtime-lock.campplus.json`, `runtime-lock.wespeaker.json`, and `runtime-lock.json` are updated in that order; only `vad_contract_version` may change.
- All three runtime locks must begin uniformly at `vad-v1` or uniformly at `vad-v2`; mixed versions are rejected before database mutation.
- Normal connections must deny every exceptional delete. Authorization is an exact finite `(table, identity)` set and is cleared on commit, rollback, or exception.
- The database repair is one transaction. No automatic restore may overwrite the live database after a failure.
- The replacement twenty jobs remain `queued`; audio acquisition and worker execution are outside repair acceptance.
- No API keys, credentials, videos, audio, transcripts, production databases, embeddings, models, caches, logs, backup paths, or private hashes may be committed.

---

### Task 1: Freeze the corrected VAD execution identity

**Files:**
- Modify: `src/market_voice_forecast_ledger/domain/voice_verification.py`
- Modify: `src/market_voice_forecast_ledger/services/voice_verification.py`
- Modify: `src/market_voice_forecast_ledger/voice/adapter_main.py`
- Modify: `tests/backend/voice_fakes.py`
- Modify: `tests/backend/unit/test_voice_protocol.py`
- Modify: `tests/backend/integration/test_presence_pilot.py`
- Modify: `tests/backend/integration/test_voice_verification_jobs.py`

**Interfaces:**
- Produces: `PRESENCE_VAD_CONTRACT_VERSION: Final[str] = "vad-v2"` in the domain module.
- Consumes: `VoiceManifestSnapshot.vad_contract_version` and `AdapterRequest.vad_contract_version`.
- Guarantees: pilot manifests, adapter requests/responses, runtime-attested workers, and test fakes use the same current contract identity.

- [x] **Step 1: Write failing contract-identity tests**

```python
def test_current_presence_vad_contract_is_v2() -> None:
    assert PRESENCE_VAD_CONTRACT_VERSION == "vad-v2"

def test_adapter_rejects_legacy_vad_contract(valid_request: AdapterRequest) -> None:
    legacy = valid_request.model_copy(update={"vad_contract_version": "vad-v1"})
    with pytest.raises(DomainError, match="VOICE_ADAPTER_CONTRACT_MISMATCH"):
        _PresenceEngine(legacy, fake_backend()).run()
```

- [x] **Step 2: Run the focused tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_voice_protocol.py tests/backend/integration/test_presence_pilot.py -q`

Expected: failure because the current constant is `vad-v1` and the adapter accepts that identity.

- [x] **Step 3: Move the current constant to the domain boundary and enforce it**

```python
PRESENCE_VAD_CONTRACT_VERSION: Final = "vad-v2"

if request.vad_contract_version != PRESENCE_VAD_CONTRACT_VERSION:
    raise DomainError(
        "VOICE_ADAPTER_CONTRACT_MISMATCH",
        "voice adapter contract identity is invalid",
    )
```

Import the constant in the service and adapter; delete the service-local `vad-v1` declaration. Update only current-pipeline test factories to `vad-v2`; historical PC-transfer manifest fixtures remain explicit `vad-v1` fixtures.

- [x] **Step 4: Run focused tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_voice_protocol.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py -q`

Expected: all tests pass.

- [x] **Step 5: Commit the contract change**

```powershell
git add src/market_voice_forecast_ledger/domain/voice_verification.py src/market_voice_forecast_ledger/services/voice_verification.py src/market_voice_forecast_ledger/voice/adapter_main.py tests/backend/voice_fakes.py tests/backend/unit/test_voice_protocol.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py
git commit -m "fix: version corrected presence vad contract"
```

Execution evidence (2026-09-04): two consumer-behavior regressions failed before
the source change; the corrected tree passed 234 focused tests in 112.76 seconds.
The public adapter entrypoint keeps its existing safe `VOICE_ADAPTER_PROCESS_FAILED`
error and rejects the legacy identity before backend initialization. Historical
explicit `vad-v1` fixtures remain available; the default runtime fake is `vad-v2`.
The finite adapter import allowlist now includes the shared domain constant.

---

### Task 2: Add the one-shot ledger and default-deny database guards

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0021_presence_vad_repair.sql`
- Modify: `src/market_voice_forecast_ledger/db/connection.py`
- Create: `tests/backend/integration/test_presence_vad_repair_guards.py`
- Modify: `tests/backend/integration/test_presence_architecture.py`

**Interfaces:**
- Produces: SQLite scalar function `presence_vad_repair_delete_authorized(table_name, identity) -> int` registered as default `0`.
- Produces: table `voice_vad_repairs` with a unique `(from_vad_contract_version, to_vad_contract_version)` transition.
- Produces: guarded delete triggers for the exact repair-owned tables.

- [x] **Step 1: Write migration and guard tests**

```python
def test_normal_connection_cannot_delete_repair_rows(migrated_conn):
    fixture = seed_succeeded_v1_job(migrated_conn)
    for table, where, values in fixture.delete_probes():
        with pytest.raises(sqlite3.IntegrityError, match="PRESENCE_VAD_REPAIR_REQUIRED"):
            migrated_conn.execute(f"DELETE FROM {table} WHERE {where}", values)

def test_repair_transition_is_unique(migrated_conn):
    insert_repair_ledger(migrated_conn, "vad-v1", "vad-v2")
    with pytest.raises(sqlite3.IntegrityError):
        insert_repair_ledger(migrated_conn, "vad-v1", "vad-v2")
```

- [x] **Step 2: Run the guard test and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_vad_repair_guards.py -q`

Expected: failure because migration `0021` and the UDF do not exist.

- [x] **Step 3: Implement the schema boundary**

Create a ledger with safe-token/hash checks, old/new target counts, candidate-order hash, target fingerprint, preserved fingerprint, database-backup SHA-256, runtime-backup fingerprint, and timestamps. Replace only the existing no-delete triggers required by the repair with triggers shaped as:

```sql
CREATE TRIGGER voice_verification_runs_no_delete
BEFORE DELETE ON voice_verification_runs
WHEN presence_vad_repair_delete_authorized('voice_verification_runs', OLD.id) != 1
BEGIN SELECT RAISE(ABORT, 'PRESENCE_VAD_REPAIR_REQUIRED'); END;
```

For composite identities, pass canonical text such as `CAST(OLD.job_id AS TEXT) || ':' || OLD.unit_key`; never authorize a table-wide wildcard. Register the UDF with `lambda *_: 0` in `open_database`.

- [x] **Step 4: Run migration, architecture, and guard tests**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_vad_repair_guards.py tests/backend/integration/test_presence_architecture.py tests/backend/integration/test_database_foundation.py -q`

Expected: all tests pass and the voice-table inventory includes exactly `voice_vad_repairs` in addition to the previous eight tables.

- [x] **Step 5: Commit the schema boundary**

```powershell
git add src/market_voice_forecast_ledger/db/connection.py src/market_voice_forecast_ledger/db/migrations/0021_presence_vad_repair.sql tests/backend/integration/test_presence_vad_repair_guards.py tests/backend/integration/test_presence_architecture.py
git commit -m "feat: guard one-shot presence repair"
```

Execution evidence (2026-09-04): four missing-capability tests failed before the
migration; after implementation, 41 guard/architecture/database tests passed.
Existing delete error codes remain unchanged; only the new jobs guard uses
`PRESENCE_VAD_REPAIR_REQUIRED`. Authorization uses `IS NOT 1` so NULL denies too.
The exact offline-wheel migration inventory in
`tests/backend/integration/test_database_foundation.py` now includes `0021`.
The separate work-state suite passed 260 tests with zero failures.

---

### Task 3: Model and fingerprint the exact repair target

**Files:**
- Create: `src/market_voice_forecast_ledger/domain/presence_repair.py`
- Create: `src/market_voice_forecast_ledger/repositories/presence_repair.py`
- Create: `tests/backend/unit/test_presence_repair_domain.py`
- Create: `tests/backend/integration/test_presence_repair_inventory.py`

**Interfaces:**
- Produces: `RepairRowIdentity(table: str, identity: str)`.
- Produces: `PresenceRepairJob(job_id: int, candidate_id: int, snapshot: VoiceManifestSnapshot, manifest_hash: str)`.
- Produces: `PresenceRepairTarget(jobs: tuple[PresenceRepairJob, ...], counts: Mapping[str, int], candidate_order_hash: str, target_fingerprint: str, preserved_fingerprint: str)`.
- Produces: `PresenceRepairPreview(from_vad_contract_version: str, to_vad_contract_version: str, target: PresenceRepairTarget, preview_hash: str)`.
- Produces: `PresenceRepairResult(old_job_ids: tuple[int, ...], new_job_ids: tuple[int, ...], candidate_ids: tuple[int, ...], to_vad_contract_version: str)`.
- Produces: `PresenceRepairRepository.read_target(from_contract: str, to_contract: str) -> PresenceRepairTarget`.

- [x] **Step 1: Write canonical hashing and exact-inventory tests**

```python
def test_preview_hash_binds_transition_and_all_target_fingerprints(target):
    first = build_presence_repair_preview("vad-v1", "vad-v2", target)
    changed = replace(target, candidate_order_hash="f" * 64)
    second = build_presence_repair_preview("vad-v1", "vad-v2", changed)
    assert first.preview_hash != second.preview_hash

@pytest.mark.parametrize("mutation", EXACT_GATE_MUTATIONS)
def test_inventory_rejects_any_noncanonical_target(migrated_conn, mutation):
    seed_twenty_succeeded_v1_jobs(migrated_conn)
    mutation(migrated_conn)
    with pytest.raises(DomainError, match="PRESENCE_REPAIR_TARGET_INVALID"):
        PresenceRepairRepository(migrated_conn).read_target("vad-v1", "vad-v2")
```

- [x] **Step 2: Run the domain and inventory tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_presence_repair_domain.py tests/backend/integration/test_presence_repair_inventory.py -q`

Expected: import failure because the new domain and repository modules do not exist.

- [x] **Step 3: Implement canonical target reading**

Use explicit column lists and stable `ORDER BY` clauses. Rebuild each `VoiceManifestSnapshot`, call `build_presence_job_manifest`, and require both stored manifest hashes to match the rebuilt value. Compute hashes with existing `canonical_json` and `sha256_text`; exclude paths and secret-bearing data. Read and hash all current presence decisions, active references/features, active calibration, candidate/video/profile identity, and unrelated table counts as the preserved fingerprint.

- [x] **Step 4: Run the tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_presence_repair_domain.py tests/backend/integration/test_presence_repair_inventory.py -q`

Expected: all exact-gate mutations fail closed and the canonical twenty-job fixture passes.

- [x] **Step 5: Commit exact inventory support**

```powershell
git add src/market_voice_forecast_ledger/domain/presence_repair.py src/market_voice_forecast_ledger/repositories/presence_repair.py tests/backend/unit/test_presence_repair_domain.py tests/backend/integration/test_presence_repair_inventory.py
git commit -m "feat: fingerprint presence repair target"
```

---

Execution evidence (2026-09-04): the real synthetic twenty-job worker flow passed
8 inventory tests, including seven fail-closed mutations. Hash binding is tested
through the inventory consumer instead of a separate trivial domain test file.
The fixture uses one clock for job creation and execution so canonical event
ordering remains meaningful. All normal table columns are typed and hashed in
stable row order; only exact owned identities are excluded from the preserved
fingerprint. Cutover bindings use candidate IDs, not legacy eligibility IDs.

### Task 4: Back up and upgrade all runtime locks

**Files:**
- Modify: `src/market_voice_forecast_ledger/voice/runtime.py`
- Create: `src/market_voice_forecast_ledger/voice/runtime_upgrade.py`
- Create: `tests/backend/unit/test_voice_runtime_upgrade.py`

**Interfaces:**
- Produces: `probe_runtime_version(command: tuple[str, ...]) -> str` using fixed argv and `shell=False`.
- Produces: `RuntimeLockUpgradeResult(backup_directory: Path, backup_fingerprint: str, before_contract: str, after_contract: str, attestations: tuple[RuntimeAttestation, ...])`.
- Produces: `upgrade_runtime_locks(settings: Settings, *, backup_directory: Path, from_contract: str, to_contract: str, version_probe: VersionProbe) -> RuntimeLockUpgradeResult`.

- [x] **Step 1: Write lock-transition tests**

```python
def test_upgrade_changes_only_vad_identity_and_attests_all_three(runtime_fixture, tmp_path):
    before = runtime_fixture.canonical_locks()
    result = upgrade_runtime_locks(
        runtime_fixture.settings,
        backup_directory=tmp_path / "repair-backup",
        from_contract="vad-v1",
        to_contract="vad-v2",
        version_probe=runtime_fixture.probe,
    )
    after = runtime_fixture.canonical_locks()
    assert result.after_contract == "vad-v2"
    assert strip_vad(after) == strip_vad(before)
    assert runtime_fixture.backup_locks() == before

def test_upgrade_rejects_mixed_versions_before_writing(runtime_fixture, tmp_path):
    runtime_fixture.set_contracts(("vad-v1", "vad-v2", "vad-v1"))
    with pytest.raises(DomainError, match="PRESENCE_REPAIR_RUNTIME_INVALID"):
        upgrade_runtime_locks(
            runtime_fixture.settings,
            backup_directory=tmp_path / "repair-backup",
            from_contract="vad-v1",
            to_contract="vad-v2",
            version_probe=runtime_fixture.probe,
        )
    assert not (tmp_path / "repair-backup").exists()
```

- [x] **Step 2: Run the runtime-upgrade tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_voice_runtime_upgrade.py -q`

Expected: import failure because `runtime_upgrade` does not exist.

- [x] **Step 3: Implement exclusive backup, atomic replacement, and re-attestation**

Parse each lock with the existing strict lock validator, attest all three, compare canonical documents after removing only `vad_contract_version`, create the backup directory and files with exclusive-create semantics, flush and reread hashes, write each replacement to a sibling temporary file, flush, and `Path.replace` candidate locks before the active lock. If all locks already say `vad-v2`, verify and return without rewriting; still require a fresh backup directory for the current apply attempt.

- [x] **Step 4: Run the runtime tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_voice_runtime_upgrade.py tests/backend/unit/test_voice_runtime.py tests/backend/unit/test_pc_transfer_runtime_rebuild.py -q`

Expected: all tests pass, including mixed-version, backup-collision, partial-replace, all-v2 idempotence, and artifact/probe mismatch cases.

- [x] **Step 5: Commit runtime-lock upgrade support**

```powershell
git add src/market_voice_forecast_ledger/voice/runtime.py src/market_voice_forecast_ledger/voice/runtime_upgrade.py tests/backend/unit/test_voice_runtime_upgrade.py
git commit -m "feat: upgrade attested presence runtime locks"
```

---

Execution evidence (2026-09-04): runtime-upgrade, existing runtime, offline rebuild,
and architecture coverage passed 80 tests. Backup and replacement are separate
APIs, allowing the verified database snapshot to occur between them. The existing
pc_transfer.runtime_rebuild.probe_version is reused; runtime.py needs no new
probe. Both candidate locks must share every non-model field with the active
lock, whose model must match exactly one candidate. Existing backups are never
replaced; partial replacement preserves all originals and leaves the active
lock last.

### Task 5: Implement preview and verified database backup

**Files:**
- Create: `src/market_voice_forecast_ledger/services/presence_repair.py`
- Create: `tests/backend/integration/test_presence_repair_preview.py`
- Modify: `tests/backend/integration/test_pc_transfer_snapshot.py`

**Interfaces:**
- Produces: `PresenceRepairService.preview() -> PresenceRepairPreview` with no filesystem or database writes.
- Produces: `PresenceRepairService(conn: sqlite3.Connection, settings: Settings, *, clock: Callable[[], datetime], backup_root: Path, version_probe: VersionProbe, fault_hook: Callable[[str], None] | None = None)`.
- Consumes: `create_database_snapshot(source, destination, expected_migrations)` and `validate_database_snapshot` from `pc_transfer.snapshot`.
- Produces privately: `_create_verified_database_backup(preview, backup_path) -> DatabaseSnapshotGuard`.

- [x] **Step 1: Write read-only preview and backup tests**

```python
def test_preview_is_read_only(migrated_db, repair_service):
    before = database_file_hash(migrated_db)
    preview = repair_service.preview()
    assert preview.target.counts["jobs"] == 20
    assert database_file_hash(migrated_db) == before

def test_backup_collision_leaves_live_database_unchanged(repair_service, backup_path):
    backup_path.touch()
    before = live_fingerprint(repair_service)
    with pytest.raises(DomainError, match="PRESENCE_REPAIR_BACKUP_EXISTS"):
        repair_service.apply(repair_service.preview().preview_hash)
    assert live_fingerprint(repair_service) == before
```

- [x] **Step 2: Run preview tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_preview.py -q`

Expected: import failure because the repair service does not exist.

- [x] **Step 3: Implement preview and reuse the snapshot verifier**

Validate lowercase hashes and exact transition tokens. Preview calls only `read_target` and canonical hashing. Backup is created before `BEGIN IMMEDIATE`, must target an absent path, and must be reopened through a separate connection for integrity, foreign-key, migration inventory, target fingerprint, and final SHA-256 reread verification.

- [x] **Step 4: Run preview and snapshot tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_preview.py tests/backend/integration/test_pc_transfer_snapshot.py -q`

Expected: all tests pass and preview produces no database, lock, or backup writes.

- [x] **Step 5: Commit preview and backup orchestration**

```powershell
git add src/market_voice_forecast_ledger/services/presence_repair.py tests/backend/integration/test_presence_repair_preview.py tests/backend/integration/test_pc_transfer_snapshot.py
git commit -m "feat: preview and back up presence repair"
```

---

Execution evidence (2026-09-04): 11 preview/snapshot tests passed. The service
compares the complete preview before and after a closed, verified online backup.
A regression proved that the existing snapshot helper could remove a destination
created after preflight; exclusive creation now establishes ownership before
cleanup is permitted. Persistent DB/WAL/lock bytes remain unchanged by preview;
SQLite's volatile shared-memory reader marks are excluded from byte comparison.
SQLite's internal temp database is allowed without accepting attached databases.

### Task 6: Execute the exact repair transaction

**Files:**
- Modify: `src/market_voice_forecast_ledger/repositories/presence_repair.py`
- Modify: `src/market_voice_forecast_ledger/services/presence_repair.py`
- Create: `tests/backend/integration/test_presence_repair_apply.py`
- Create: `tests/backend/e2e/test_presence_repair_flow.py`

As-built file additions: repositories/jobs.py and services/job_state.py accept an
optional validated requested_job_id. Repair allocates IDs above the original
maximum, preventing SQLite from reusing the removed highest job IDs.

**Interfaces:**
- Produces: `PresenceRepairRepository.authorize(rows: tuple[RepairRowIdentity, ...]) -> AbstractContextManager[None]`.
- Produces: `PresenceRepairRepository.delete_target(target: PresenceRepairTarget) -> None`, verifying every `rowcount`.
- Produces: `PresenceRepairRepository.add_completion(*, preview: PresenceRepairPreview, old_job_ids: tuple[int, ...], new_job_ids: tuple[int, ...], database_backup_sha256: str, runtime_backup_fingerprint: str, completed_at: str) -> int`.
- Produces: `PresenceRepairService.apply(expected_preview_hash: str) -> PresenceRepairResult`.
- Consumes: `JobStateService.create_video_pipeline_in_transaction(manifest: JobManifest, candidate_ids: Sequence[int], created_at: str | None = None) -> int`.

- [x] **Step 1: Write apply, rollback, and one-shot tests**

```python
def test_apply_replaces_only_twenty_target_jobs(repair_harness):
    preview = repair_harness.service.preview()
    before = repair_harness.preserved_fingerprint()
    result = repair_harness.service.apply(preview.preview_hash)
    assert len(result.old_job_ids) == len(result.new_job_ids) == 20
    assert repair_harness.contract_counts() == {"vad-v1": 0, "vad-v2": 20}
    assert repair_harness.queued_candidate_order() == repair_harness.original_candidate_order
    assert repair_harness.preserved_fingerprint() == before

@pytest.mark.parametrize("fault", REPAIR_TRANSACTION_FAULTS)
def test_apply_rolls_back_every_database_change(repair_harness, fault):
    repair_harness.inject(fault)
    preview = repair_harness.service.preview()
    before = repair_harness.database_fingerprint()
    with pytest.raises(DomainError):
        repair_harness.service.apply(preview.preview_hash)
    assert repair_harness.database_fingerprint() == before
    assert repair_harness.normal_delete_is_denied()
```

- [x] **Step 2: Run apply tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_apply.py tests/backend/e2e/test_presence_repair_flow.py -q`

Expected: failure because apply and deletion authorization are not implemented.

- [x] **Step 3: Implement finite authorization and one transaction**

After verified runtime/database backups and runtime-lock upgrade, issue `BEGIN IMMEDIATE`, reread the target, require the same preview hash, and set a closure-backed UDF that returns `1` only for exact authorized tuples. Delete in schema-derived child-to-parent order, verify each count, reconstruct each snapshot with only `vad_contract_version="vad-v2"`, create one queued job/binding/manifest at a time through the normal service/repository path, verify old/new/preserved fingerprints, insert one ledger row, clear the UDF, and commit. In every exception path clear the UDF before rollback.

- [x] **Step 4: Run apply and E2E tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_apply.py tests/backend/e2e/test_presence_repair_flow.py tests/backend/integration/test_presence_vad_repair_guards.py -q`

Expected: all success, drift, unauthorized-delete, fault-injection, rollback, and rerun-rejection tests pass.

- [x] **Step 5: Commit transactional repair**

```powershell
git add src/market_voice_forecast_ledger/repositories/presence_repair.py src/market_voice_forecast_ledger/services/presence_repair.py tests/backend/integration/test_presence_repair_apply.py tests/backend/e2e/test_presence_repair_flow.py
git commit -m "feat: repair invalid presence pilot atomically"
```

---

Execution evidence (2026-09-04): 27 apply/guard tests passed. Eight transaction
fault boundaries roll back after reconnect; authorization is exact, single-use,
and revoked at commit/rollback as well as context exit. Backup corruption is
rechecked before BEGIN IMMEDIATE. Postcommit failure retains the committed result
and backup, reports failure, and does not restore automatically. The integration
success case crosses the full backup/runtime/service/repository boundary; the
additional CLI/worker synthetic E2E is tracked with Task 8.

### Task 7: Expose a strict production CLI

**Files:**
- Modify: `src/market_voice_forecast_ledger/cli.py`
- Create: `tests/backend/integration/test_presence_repair_cli.py`
- Modify: `tests/backend/integration/test_cli.py`

**Interfaces:**
- Produces: `presence pilot repair preview`.
- Produces: `presence pilot repair apply --expected-preview-hash PREVIEW_HASH`, where `PREVIEW_HASH` is exactly 64 lowercase hexadecimal characters.
- Produces: `_run_production_presence_repair(command, expected_preview_hash=None)`, opening preview through a SQLite `mode=ro`/`query_only` connection and opening apply through the normal writable connection. Apply installs schema-only migration `0021` before rebuilding and matching the same data-bound preview; preview itself never installs migrations or writes WAL state.
- Preserves: injectable `presence_repair_service_factory` for tests.

- [x] **Step 1: Write parser, output, and error-boundary tests**

```python
def test_repair_preview_prints_only_public_counts_and_hash(capsys, repair_factory):
    assert main(["presence", "pilot", "repair", "preview"], presence_repair_service_factory=repair_factory) == 0
    output = capsys.readouterr().out
    assert "20" in output and repair_factory.preview_hash in output
    assert repair_factory.private_path not in output

def test_repair_apply_requires_exact_preview_hash(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(["presence", "pilot", "repair", "apply"])
```

- [x] **Step 2: Run CLI tests and confirm RED**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_cli.py tests/backend/integration/test_cli.py -q`

Expected: parser failure because the repair subcommands do not exist.

- [x] **Step 3: Implement the production wiring and public output**

Preview prints only transition, target count, and preview hash. Apply prints only completion count and destination contract. The read-only preview accepts migration inventory `0020` or `0021`; apply installs `0021`, then requires the data-bound preview hash to remain identical before any backup or delete. Map every repair failure to fixed safe codes such as `PRESENCE_REPAIR_TARGET_INVALID`, `PRESENCE_REPAIR_PREVIEW_CHANGED`, `PRESENCE_REPAIR_BACKUP_FAILED`, `PRESENCE_REPAIR_RUNTIME_INVALID`, and `PRESENCE_REPAIR_ALREADY_APPLIED`; never print exceptions, SQL, absolute paths, or hashes other than the preview token intentionally returned to the local operator.

- [x] **Step 4: Run CLI tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_repair_cli.py tests/backend/integration/test_cli.py tests/backend/integration/test_presence_cli.py -q`

Expected: all tests pass.

- [x] **Step 5: Commit the repair CLI**

```powershell
git add src/market_voice_forecast_ledger/cli.py tests/backend/integration/test_presence_repair_cli.py tests/backend/integration/test_cli.py
git commit -m "feat: add guarded presence repair cli"
```

---

Execution evidence (2026-09-04): 75 CLI tests passed, including the real production
wiring against a synthetic populated 0020 database. Preview leaves 0020 intact;
apply installs only 0021, retains the same preview hash, and creates 20 queued
jobs without executing a worker. Duplicate/abbreviated/unknown arguments and
private exception text are rejected at the public boundary.

### Task 8: Close architecture and regression coverage

**Files:**
- Modify: `tests/backend/integration/test_presence_architecture.py`
- Modify: `tests/backend/e2e/test_presence_verification_flow.py`
- Modify: `tests/backend/integration/test_presence_real_smoke.py`
- Modify: `tests/backend/README.md`

**Interfaces:**
- Consumes: the repair repository as the sole exceptional SQL writer.
- Guarantees: no general presence module gains direct SQL authority, the adapter import boundary remains finite, and the existing worker flow uses only `vad-v2`.

- [ ] **Step 1: Add failing architecture and regression cases**

```python
def test_only_repair_repository_writes_repair_owned_tables() -> None:
    assert repair_sql_write_violations() == ()

def test_post_repair_worker_reads_v2_manifest_only(repair_harness) -> None:
    repair_harness.apply()
    job = repair_harness.first_queued_job()
    assert job.snapshot.vad_contract_version == "vad-v2"
```

- [ ] **Step 2: Run the architecture/E2E tests and confirm RED where coverage is missing**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/integration/test_presence_architecture.py tests/backend/e2e/test_presence_verification_flow.py -q`

Expected: the new ownership assertion fails until the finite allowlist is updated.

- [ ] **Step 3: Add only the exact repair repository ownership exception**

Allow `repositories/presence_repair.py` to write only the repair-owned tables enumerated by the design. Keep `presence_decisions`, candidates, discovery, videos, reference, calibration, analysis, statements, forecasts, and heatmaps outside the allowlist. Document the opt-in real smoke as read/execute validation, not a repair prerequisite.

- [ ] **Step 4: Run presence and PC-transfer regression suites**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m pytest tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py tests/backend/unit/test_voice_runtime_upgrade.py tests/backend/integration/test_presence_architecture.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_presence_repair_inventory.py tests/backend/integration/test_presence_repair_apply.py tests/backend/integration/test_presence_repair_cli.py tests/backend/e2e/test_presence_repair_flow.py tests/backend/e2e/test_pc_transfer_round_trip.py -q`

Expected: all tests pass; real-provider tests remain explicitly opt-in.

- [ ] **Step 5: Commit regression coverage**

```powershell
git add tests/backend/integration/test_presence_architecture.py tests/backend/e2e/test_presence_verification_flow.py tests/backend/integration/test_presence_real_smoke.py tests/backend/README.md
git commit -m "test: close presence repair boundaries"
```

---

### Task 9: Verify and freeze the implementation

**Files:**
- Modify only if evidence requires it: files already listed in Tasks 1–8.

**Interfaces:**
- Produces: a clean, fully tested candidate commit before production mutation.

- [ ] **Step 1: Run compile and the full backend suite**

Run: `$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m compileall -q src tests/backend; .\.venv\Scripts\python.exe -m pytest tests/backend -q`

Expected: zero failures; only documented capability/opt-in skips.

- [ ] **Step 2: Run work-state and state-document validation**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1`

Expected: all tests pass and state documents are structurally valid.

- [ ] **Step 3: Run public-safety and diff checks**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree`

Run: `git diff --check; git status --short`

Expected: no private artifact, binary, whitespace error, or unrelated path is present.

- [ ] **Step 4: Build and install the exact project wheel locally**

Build the wheel with the existing project build command, install it into `.venv` without upgrading unrelated packages, then run `pip check` and import the installed package. The generated wheel remains ignored and is not staged.

- [ ] **Step 5: Keep the verified implementation frozen**

If Step 1–4 exposes a defect, return to the owning task, add a failing regression test, make that test pass, stage the exact files listed in that task, and repeat all of Task 9. When Step 1–4 passes, make no further source edit before the production preflight.

---

### Task 10: Apply the repair to production with stage gates

**Files:**
- Read/write private local state only: the production SQLite database and the three runtime lock files located through `default_settings()`.
- Create private backups only under the repair backup directory selected by the service.
- Do not modify tracked repository files during this task.

**Interfaces:**
- Consumes: installed CLI from Task 7.
- Produces privately: verified database backup, verified three-lock backup, one ledger row, zero `vad-v1` target jobs, and twenty queued `vad-v2` jobs.

- [ ] **Step 1: Confirm writer quiescence and perform read-only preflight**

Verify the scheduled collection task is not currently running, no application worker holds an active write operation, production DB identity matches settings, migration inventory ends at `0020` or the schema-only `0021`, `integrity_check` is `ok`, `foreign_key_check` returns zero rows, no repair ledger exists when `0021` is already present, runtime locks are uniformly `vad-v1` or uniformly `vad-v2`, and preview finds exactly twenty canonical target jobs. The apply command may install `0021`; immediately after installation it must recheck integrity, foreign keys, and the same preview hash before creating backups.

- [ ] **Step 2: Run the public preview**

Run: `.\.venv\Scripts\market-voice-forecast-ledger.exe presence pilot repair preview`

Expected: a public-safe line containing target count `20`, transition `vad-v1` to `vad-v2`, and one lowercase 64-character preview hash. Retain the token only for the immediately following apply.

- [ ] **Step 3: Apply with the exact preview token**

Run the preview and apply in the same PowerShell process so the token is not copied incorrectly:

```powershell
$presenceRepairPreviewOutput = & .\.venv\Scripts\market-voice-forecast-ledger.exe presence pilot repair preview
$presenceRepairPreviewHash = [regex]::Match(($presenceRepairPreviewOutput -join "`n"), '(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])').Value
if ($presenceRepairPreviewHash.Length -ne 64) { throw 'PRESENCE_REPAIR_PREVIEW_OUTPUT_INVALID' }
& .\.venv\Scripts\market-voice-forecast-ledger.exe presence pilot repair apply --expected-preview-hash $presenceRepairPreviewHash
```

Expected: completion reports twenty replacement jobs. The service must have verified exclusive database/runtime backups before opening the repair transaction.

- [ ] **Step 4: Verify production postconditions from a new connection**

Verify `integrity_check=ok`, no foreign-key violations, migration `0021`, one ledger transition, zero target `vad-v1` manifests/jobs/runs/segments/artifact rows, twenty queued `vad-v2` jobs with seven pending units and one `job_created` event each, identical candidate order, identical current-decision/reference/calibration/preserved fingerprints, normal delete rejection, and all three runtime locks attested as `vad-v2` with every non-VAD field unchanged from backup.

- [ ] **Step 5: Retain recovery sources**

Do not delete the database backup, runtime-lock backup directory, Google Drive transfer ZIP, Downloads copy, or old-PC data. Do not run the twenty queued jobs as part of repair acceptance.

---

### Task 11: Record completion and synchronize the branch

**Files:**
- Modify: `docs/project/status.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/project/decisions.md` only if the runtime-lock clarification requires a durable decision entry.
- Modify: `docs/superpowers/plans/2026-09-03-vad-v2-pilot-repair.md` to check completed steps.

**Interfaces:**
- Produces: public-safe durable state that reports verified counts but no private paths, secrets, or private hashes.

- [ ] **Step 1: Update state documents from fresh evidence**

Record `vad-v2` introduction, exact twenty-job replacement, one-shot ledger, backup retention, runtime-lock attestation, full test results, and the fact that the replacement jobs remain queued. Do not claim worker completion or application completion.

- [ ] **Step 2: Validate documentation and public safety**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree`

Expected: both pass.

- [ ] **Step 3: Stage only exact tracked files and validate staged content**

Run: `git status --short; git diff --check; git diff --cached --check`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode Staged`

Expected: no unrelated path and no private artifact is staged.

- [ ] **Step 4: Commit completion state**

```powershell
git add docs/project/status.md docs/project/plan.md docs/project/decisions.md docs/superpowers/plans/2026-09-03-vad-v2-pilot-repair.md
git commit -m "docs: record guarded vad v2 repair"
```

If `docs/project/decisions.md` has no material change, omit it from `git add`.

- [ ] **Step 5: Push and verify live remote equality**

Run: `git push origin feature/presence-verification`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1`

Expected: local `HEAD`, upstream tracking ref, and live remote branch SHA are identical, and the working tree is clean.

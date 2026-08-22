# Semi-automatic Presence Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-local, CPU-only workflow that proposes whether each of 20 fixed pilot videos contains the configured person, while allowing only an explicit human review to change `presence_unverified` to `presence_confirmed` or `presence_rejected`.

**Architecture:** The main Python package owns SQLite identity, immutable manifests, durable `video_pipeline` jobs, review transactions, and safe CLI output. A private child process owns media decoding, VAD, and sherpa-onnx speaker scoring through a strict JSON protocol; normal tests replace every downloader, ffmpeg, and model boundary with fakes. Reference enrollment, model calibration, pilot selection, execution, cleanup, and review are separate services with immutable hashes between them.

**Tech Stack:** Python 3.14.6, SQLite migrations, `sherpa-onnx==1.13.4` CPU runtime, yt-dlp `2026.08.19`, Deno `2.9.5`, FFmpeg `9.0.1`, Pydantic v2 strict models, pytest, PowerShell public-safety gates.

**Spec:** `docs/superpowers/specs/2026-08-22-presence-verification-design.md`

## Global Constraints

- Preserve the existing person-only discovery model and existing `presence_decisions` states/origin values; migrations `0001` through `0019` remain byte-identical.
- Add only migration `0020_presence_verification.sql`; it extends the final schema without resetting or reinterpreting existing YouTube collection rows.
- The model creates only `likely_present`, `likely_absent`, or `needs_review`; only `confirm` and `reject` reviews create a new presence decision.
- The first pilot is exactly four active profiles times five candidates, for exactly 20 immutable one-candidate `video_pipeline` jobs.
- Do not auto-create presence jobs from daily YouTube collection; only `presence pilot create` may create the pilot.
- Do not implement transcription, diarization labels, transcript speaker assignment, Codex analysis, heatmaps, React UI, or cloud speech/biometric APIs.
- Main runtime stays Python 3.14.6; the private audio runtime pins `sherpa-onnx==1.13.4` and accepts only the Windows x64 CPython 3.14 wheel SHA-256 `cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06`.
- Pin yt-dlp to official release `2026.08.19`; accept only `yt-dlp.exe` SHA-256 `66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a`.
- Pin the yt-dlp JavaScript runtime to Deno `2.9.5`; accept only Windows x64 `deno.exe` SHA-256 `98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaee9e409ccb3b9fd` and pass its absolute path explicitly.
- Require FFmpeg `9.0.1`; because FFmpeg publishes source rather than an official Windows executable, validate and privately record the selected executable's absolute path, version output, and SHA-256 before use.
- Compare `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` and `wespeaker_zh_cnceleb_resnet34.onnx`; require operator-recorded SHA-256 for each downloaded model and CPU provider only.
- Runtime, models, caches, audio, embeddings, database, logs, and provider response data stay under the private data directory and must never be staged or committed.
- All normal tests use fake downloader, fake ffmpeg, and fake adapter; real media/model tests require an explicit opt-in environment flag and user approval.
- Use fixed safe error codes and generic messages. Never expose provider body/header, native exception, stdout/stderr, private path, audio content, or embedding bytes through CLI/API/log/assertion text.
- Use `BEGIN IMMEDIATE` for review pointer changes and other competing logical identities; add SQLite UPDATE/DELETE/OR-REPLACE guards that remain effective with foreign keys and recursive triggers disabled.
- Use the existing `JobKind.VIDEO_PIPELINE`, `JobStateService`, candidate binding tables, `RetentionService`, and existing job stages. Do not add a new job kind or job stage.
- Dependency verification sources: [sherpa-onnx 1.13.4 on PyPI](https://pypi.org/project/sherpa-onnx/), [yt-dlp 2026.08.19 release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19), [Deno 2.9.5 release](https://github.com/denoland/deno/releases/tag/v2.9.5), and [FFmpeg 9.0.1 download page](https://ffmpeg.org/download.html).

## File Map

### New production files

- `src/market_voice_forecast_ledger/db/migrations/0020_presence_verification.sql` — private voice tables, ownership checks, indexes, append-only/collision guards.
- `src/market_voice_forecast_ledger/domain/voice_verification.py` — immutable domain commands/results, calibration, proposal bands, manifest unit constants and hash builders.
- `src/market_voice_forecast_ledger/repositories/voice_verification.py` — canonical reads/writes and corruption validation for references, manifests, runs, segments, and reviews.
- `src/market_voice_forecast_ledger/services/voice_reference.py` — approved clip enrollment, model calibration, profile/feature activation.
- `src/market_voice_forecast_ledger/services/voice_verification.py` — deterministic pilot selection, one-candidate job creation, run persistence, and human review.
- `src/market_voice_forecast_ledger/voice/__init__.py` — public protocol exports only.
- `src/market_voice_forecast_ledger/voice/protocol.py` — strict child-process request/response schemas and canonical hashes.
- `src/market_voice_forecast_ledger/voice/runtime.py` — executable/model attestation and private runtime lock validation.
- `src/market_voice_forecast_ledger/voice/media.py` — fixed-argv yt-dlp/FFmpeg execution and private path rules.
- `src/market_voice_forecast_ledger/voice/process.py` — secret-safe subprocess transport for the adapter.
- `src/market_voice_forecast_ledger/voice/adapter_main.py` — stdin/stdout JSON adapter entrypoint using sherpa-onnx CPU provider.
- `src/market_voice_forecast_ledger/workers/presence_verification.py` — one-wake durable worker and crash recovery.
- `tests/backend/voice_fakes.py` — deterministic fake downloader, ffmpeg, adapter, clock, and fixture hashes.

### Modified production files

- `src/market_voice_forecast_ledger/config.py` — derived private voice runtime/model/audio paths.
- `src/market_voice_forecast_ledger/cli.py` — strict `presence` command tree and dependency seams.
- `pyproject.toml` — optional `voice` dependency pin for the isolated runtime.

### New tests

- `tests/backend/unit/test_voice_calibration.py`
- `tests/backend/unit/test_voice_protocol.py`
- `tests/backend/unit/test_voice_runtime.py`
- `tests/backend/unit/test_voice_media.py`
- `tests/backend/integration/test_voice_reference_enrollment.py`
- `tests/backend/integration/test_presence_pilot.py`
- `tests/backend/integration/test_voice_verification_jobs.py`
- `tests/backend/integration/test_presence_reviews.py`
- `tests/backend/integration/test_presence_cli.py`
- `tests/backend/integration/test_presence_architecture.py`
- `tests/backend/integration/test_presence_real_smoke.py`
- `tests/backend/e2e/test_presence_verification_flow.py`

### Modified tests and documentation

- `tests/backend/integration/test_append_only_insert_guards.py` — new immutable-table mutation matrix.
- `tests/backend/integration/test_collection_model_cutover.py` — final-schema and historical-migration checks.
- `README.md`, `tests/backend/README.md`, `docs/project/status.md`, `docs/project/requirements.md`, `docs/project/decisions.md`, `docs/project/plan.md` — as-built commands, privacy boundary, state, and verification evidence.

---

### Task 1: Voice Domain Contract and Migration 0020

**Files:**
- Create: `src/market_voice_forecast_ledger/domain/voice_verification.py`
- Create: `src/market_voice_forecast_ledger/db/migrations/0020_presence_verification.sql`
- Test: `tests/backend/unit/test_voice_calibration.py`
- Test: `tests/backend/integration/test_collection_model_cutover.py`
- Test: `tests/backend/integration/test_append_only_insert_guards.py`

**Interfaces:**
- Consumes: `SpeakerThresholdConfig`, `ScoreRule`, `JobManifest`, `ManifestUnit`, `JobKind.VIDEO_PIPELINE`, and existing presence/candidate tables.
- Produces: `VoiceProposal`, `ReviewAction`, `ReferenceClipCommand`, `CalibrationSample`, `VoiceCalibration`, `VoiceManifestSnapshot`, `VoiceSegmentScore`, `VoiceRunResult`, `calibrate_thresholds(samples)`, `classify_presence_score(score, calibration)`, and `build_presence_job_manifest(snapshot)`.

- [ ] **Step 1: Write failing domain and migrated-schema tests**

```python
def test_calibration_requires_global_separation() -> None:
    samples = (
        CalibrationSample(subject_id=1, sample_kind="held_out_positive", score=0.71),
        CalibrationSample(subject_id=1, sample_kind="negative", score=0.72),
    )
    with pytest.raises(DomainError, match="VOICE_MODEL_NOT_SEPARABLE"):
        calibrate_thresholds(samples)


def test_presence_tables_reject_replace_with_logical_identity(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA recursive_triggers=OFF")
    insert_canonical_reference_clip(conn, clip_id=1, profile_id=1, ordinal=1)
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_VOICE_REFERENCE"):
        insert_canonical_reference_clip(conn, clip_id=2, profile_id=1, ordinal=1, replace=True)
```

- [ ] **Step 2: Run the RED gate**

Run: `python -m pytest tests/backend/unit/test_voice_calibration.py tests/backend/integration/test_collection_model_cutover.py tests/backend/integration/test_append_only_insert_guards.py -q`

Expected: collection fails because `domain.voice_verification` and migration `0020` do not exist.

- [ ] **Step 3: Implement the immutable domain contract**

```python
class VoiceProposal(StrEnum):
    LIKELY_PRESENT = "likely_present"
    LIKELY_ABSENT = "likely_absent"
    NEEDS_REVIEW = "needs_review"


class ReviewAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    HOLD = "hold"


def calibrate_thresholds(samples: Sequence[CalibrationSample]) -> VoiceCalibration:
    positives = tuple(item.score for item in samples if item.sample_kind == "held_out_positive")
    negatives = tuple(item.score for item in samples if item.sample_kind == "negative")
    if not positives or not negatives or any(not isfinite(value) for value in (*positives, *negatives)):
        raise DomainError("VOICE_CALIBRATION_INVALID", "calibration samples are invalid")
    minimum_positive = min(positives)
    maximum_negative = max(negatives)
    if minimum_positive <= maximum_negative:
        raise DomainError("VOICE_MODEL_NOT_SEPARABLE", "voice model is not separable")
    return VoiceCalibration(
        subject_boundary=minimum_positive,
        interviewer_boundary=maximum_negative,
        margin=minimum_positive - maximum_negative,
    )
```

`build_presence_job_manifest()` must create seven contiguous units with keys and stages exactly:

```python
PRESENCE_UNITS = (
    ("video:validate", JobStage.VIDEO_METADATA),
    ("audio:acquire", JobStage.AUDIO_ACQUISITION),
    ("audio:normalize", JobStage.AUDIO_ACQUISITION),
    ("voice:vad", JobStage.SPEAKER_ASSIGNMENT),
    ("voice:score", JobStage.SPEAKER_ASSIGNMENT),
    ("voice:proposal", JobStage.SPEAKER_ASSIGNMENT),
    ("audio:cleanup", JobStage.SPEAKER_ASSIGNMENT),
)
```

Each unit depends on the immediately preceding unit, and each `execution_contract_hash` is the canonical hash of the unit key plus the frozen adapter/model/VAD/selection versions.

- [ ] **Step 4: Add migration 0020 with exact constraints**

Create the six tables from the design. `voice_reference_clips.clip_kind` is constrained to `enrollment`, `held_out_positive`, or `negative`; `ordinal` is contiguous within each profile and the service fixes the initial six roles to two enrollment, one held-out positive, and three negatives. Use these independent logical identities:

```sql
UNIQUE(reference_profile_id, ordinal)                 -- voice_reference_clips
UNIQUE(reference_profile_id)                          -- voice_reference_features
UNIQUE(job_id), UNIQUE(candidate_id, manifest_hash)   -- voice_verification_manifests
UNIQUE(job_id), UNIQUE(candidate_id, output_hash)     -- voice_verification_runs
UNIQUE(run_id, ordinal)                               -- voice_verification_segments
UNIQUE(run_id)                                        -- voice_verification_reviews
```

Add exact CHECK constraints for proposal/action enums, contiguous-positive ordinals, `start_ms < end_ms`, safe token/hash lengths, bounded reason length `1..240`, and finite scores using `raw_match_score = raw_match_score` plus absolute bound `<= 1.0e6`. Add UPDATE, DELETE, primary-key replace, and every listed logical-identity replace trigger with stable codes `IMMUTABLE_VOICE_REFERENCE`, `IMMUTABLE_VOICE_MANIFEST`, `IMMUTABLE_VOICE_RUN`, and `IMMUTABLE_VOICE_REVIEW`. Add owner triggers linking manifest/job/candidate/video/profile, segment/run, review/run, and review prior decision/candidate.

- [ ] **Step 5: Run domain/schema tests and all historical migration checks**

Run: `python -m pytest tests/backend/unit/test_voice_calibration.py tests/backend/integration/test_collection_model_cutover.py tests/backend/integration/test_append_only_insert_guards.py tests/backend/integration/test_database_foundation.py -q`

Expected: PASS; migrations `0001` through `0019` have unchanged hashes; fresh schema includes `0020`.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/domain/voice_verification.py src/market_voice_forecast_ledger/db/migrations/0020_presence_verification.sql tests/backend/unit/test_voice_calibration.py tests/backend/integration/test_collection_model_cutover.py tests/backend/integration/test_append_only_insert_guards.py
git commit -m "feat: define presence verification records"
```

### Task 2: Canonical Voice Repository

**Files:**
- Create: `src/market_voice_forecast_ledger/repositories/voice_verification.py`
- Test: `tests/backend/integration/test_voice_reference_enrollment.py`
- Test: `tests/backend/integration/test_voice_verification_jobs.py`

**Interfaces:**
- Consumes: Task 1 dataclasses and tables; caller-owned SQLite transactions for multi-row writes.
- Produces: `VoiceVerificationRepository` methods `add_reference_clip`, `add_reference_feature`, `get_reference_bundle`, `add_manifest`, `get_manifest_for_job`, `add_run_with_segments`, `get_run`, `list_pending_reviews`, and `add_review_and_decision`.

- [ ] **Step 1: Write corruption-sensitive repository tests**

```python
def test_get_run_recomputes_segment_and_output_hashes(db) -> None:
    run_id = persist_canonical_run(db)
    disable_immutable_trigger_and_tamper_segment_score(db, run_id, 999.0)
    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        VoiceVerificationRepository(db).get_run(run_id)


def test_review_write_requires_caller_transaction(db) -> None:
    with pytest.raises(DomainError, match="TRANSACTION_REQUIRED"):
        VoiceVerificationRepository(db).add_review_and_decision(valid_review_command())
```

- [ ] **Step 2: Run repository RED**

Run: `python -m pytest tests/backend/integration/test_voice_reference_enrollment.py tests/backend/integration/test_voice_verification_jobs.py -q`

Expected: FAIL because `repositories.voice_verification` is missing.

- [ ] **Step 3: Implement repository writes with prevalidation**

```python
class VoiceVerificationRepository:
    def add_run_with_segments(
        self,
        result: VoiceRunResult,
        segments: tuple[VoiceSegmentScore, ...],
        *,
        completed_at: datetime,
    ) -> int:
        self._require_transaction()
        canonical = canonicalize_run(result, segments, completed_at=completed_at)
        # Validate all rows before the first INSERT.
        run_id = self._insert_run(canonical.run)
        for segment in canonical.segments:
            self._insert_segment(run_id, segment)
        return run_id
```

Every read must recompute canonical hashes and verify: exact SQLite types, UTC timestamps, manifest owner binding, current and frozen decision identities, reference profile/feature hash, threshold/model/adapter identities, contiguous segment ordinals, ordered nonoverlapping bounds, finite score, run output hash, and review evidence linkage. Do not heal a missing or corrupt row.

- [ ] **Step 4: Implement atomic review repository write**

`add_review_and_decision(command)` inserts exactly one review. For `hold`, it inserts no decision and performs no pointer update. For `confirm`/`reject`, it inserts a new `presence_decisions` row with `decision_origin='voice_verification'`, `evidence_ref=str(review_id)`, `evidence_hash=review_hash`, recomputes `decision_hash`, and updates `subject_video_candidates.current_presence_decision_id` only from the manifest-frozen prior ID to the new same-candidate decision.

- [ ] **Step 5: Run repository modules**

Run: `python -m pytest tests/backend/integration/test_voice_reference_enrollment.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_append_only_insert_guards.py -q`

Expected: PASS, including rollback after injected failure between run and segment insertion, and after review insertion but before pointer update.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/repositories/voice_verification.py tests/backend/integration/test_voice_reference_enrollment.py tests/backend/integration/test_voice_verification_jobs.py
git commit -m "feat: persist canonical voice verification"
```

### Task 3: Strict Adapter Protocol and Runtime Attestation

**Files:**
- Create: `src/market_voice_forecast_ledger/voice/__init__.py`
- Create: `src/market_voice_forecast_ledger/voice/protocol.py`
- Create: `src/market_voice_forecast_ledger/voice/runtime.py`
- Modify: `src/market_voice_forecast_ledger/config.py`
- Modify: `pyproject.toml`
- Test: `tests/backend/unit/test_voice_protocol.py`
- Test: `tests/backend/unit/test_voice_runtime.py`

**Interfaces:**
- Consumes: private paths derived from `Settings.data_dir` and Task 1 domain types.
- Produces: strict `AdapterRequest`, `AdapterResponse`, `AdapterSegment`, `RuntimeAttestation`, `encode_request`, `decode_response`, `attest_runtime`, and settings properties `voice_runtime_dir`, `voice_model_dir`, `voice_work_dir`.

- [ ] **Step 1: Write strict protocol and attestation tests**

```python
@pytest.mark.parametrize("mutation", ["unknown_field", "nan_score", "reordered", "wrong_hash", "wrong_model"])
def test_adapter_response_fails_closed(mutation: str) -> None:
    payload = mutate_valid_response(mutation)
    with pytest.raises(DomainError, match="VOICE_ADAPTER_RESPONSE_INVALID"):
        decode_response(payload, expected_request=valid_request())


def test_runtime_requires_exact_hash_and_cpu_provider(tmp_path: Path) -> None:
    with pytest.raises(DomainError, match="VOICE_RUNTIME_INVALID"):
        attest_runtime(runtime_fixture(tmp_path, provider="CUDAExecutionProvider"))
```

- [ ] **Step 2: Run protocol/runtime RED**

Run: `python -m pytest tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py -q`

Expected: FAIL because the `voice` package is missing.

- [ ] **Step 3: Implement strict Pydantic models and canonical transport hashes**

```python
class AdapterSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    ordinal: int = Field(ge=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    raw_score: float
    evidence_hash: str

    @model_validator(mode="after")
    def validate_range(self) -> "AdapterSegment":
        if self.start_ms >= self.end_ms or not math.isfinite(self.raw_score):
            raise ValueError("invalid segment")
        return self
```

`AdapterRequest.reference_feature_b64` carries the reference feature only through the private stdin channel, is decoded into a mutable buffer with an exact configured byte-length limit, and is overwritten after adapter initialization. `decode_response()` must reject duplicate JSON keys, unknown fields, non-UTF-8, payloads over 1 MiB, more than 10,000 segments, noncontiguous ordinals, overlapping/out-of-order segments, identity mismatch, and canonical output hash mismatch. All raised `DomainError` messages remain constant.

- [ ] **Step 4: Implement runtime attestation**

`RuntimeAttestation` must contain resolved absolute Python/yt-dlp/Deno/FFmpeg/model/VAD paths, hashes, versions, `provider='CPUExecutionProvider'`, adapter contract version, and VAD contract version. `attest_runtime()` resolves paths, rejects symlink/reparse escape from private roots, hashes files, invokes version probes with fixed argv and suppressed output, and compares against configured allowlists. The private runtime lock is read from `Settings.voice_runtime_dir / 'runtime-lock.json'` and is never a repository file.

Add this optional dependency group:

```toml
[project.optional-dependencies]
voice = ["sherpa-onnx==1.13.4"]
```

- [ ] **Step 5: Run protocol/runtime and public-safety tests**

Run: `python -m pytest tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py -q`

Run: `pwsh -NoProfile -File scripts/work-state/check-public-safety.ps1 -Mode WorkingTree`

Expected: PASS; no private runtime file is visible to Git.

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml src/market_voice_forecast_ledger/config.py src/market_voice_forecast_ledger/voice/__init__.py src/market_voice_forecast_ledger/voice/protocol.py src/market_voice_forecast_ledger/voice/runtime.py tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py
git commit -m "feat: attest private voice runtime"
```

### Task 4: Safe Media Acquisition and Adapter Process

**Files:**
- Create: `src/market_voice_forecast_ledger/voice/media.py`
- Create: `src/market_voice_forecast_ledger/voice/process.py`
- Create: `src/market_voice_forecast_ledger/voice/adapter_main.py`
- Create: `tests/backend/voice_fakes.py`
- Test: `tests/backend/unit/test_voice_media.py`
- Test: `tests/backend/unit/test_voice_protocol.py`

**Interfaces:**
- Consumes: `RuntimeAttestation`, `AdapterRequest`, `AdapterResponse`, and private work root.
- Produces: `MediaAcquirer.acquire(video_id, target_dir) -> AcquiredMedia`, `MediaNormalizer.normalize(source, target) -> NormalizedAudio`, and `VoiceAdapterProcess.score(request) -> AdapterResponse`.

- [ ] **Step 1: Write fixed-argv, safe-path, timeout, and nonleak tests**

```python
def test_downloader_uses_fixed_public_watch_url_and_no_shell(fake_runner, tmp_path) -> None:
    result = MediaAcquirer(fake_runner, attestation()).acquire("abcdefghijk", tmp_path)
    assert fake_runner.calls == [(
        YT_DLP_PATH,
        "--no-playlist", "--no-write-info-json", "--no-write-thumbnail",
        "--no-write-subs", "--no-write-auto-subs", "-f", "bestaudio",
        "-o", str(result.path), "https://www.youtube.com/watch?v=abcdefghijk",
    )]
    assert fake_runner.shell_values == [False]


def test_adapter_failure_never_exposes_private_values(private_sentinels) -> None:
    with pytest.raises(DomainError) as caught:
        failing_process(private_sentinels).score(valid_request())
    assert all(value not in str(caught.value) for value in private_sentinels)
```

- [ ] **Step 2: Run media/process RED**

Run: `python -m pytest tests/backend/unit/test_voice_media.py tests/backend/unit/test_voice_protocol.py -q`

Expected: FAIL because media/process adapters are missing.

- [ ] **Step 3: Implement fixed media commands**

Use `shell=False`, resolved executable paths from attestation, hidden windows, bounded timeout, stdout/stderr discarded, and these command shapes:

```python
yt_dlp_argv = (
    str(attestation.yt_dlp_path),
    "--no-playlist", "--no-write-info-json", "--no-write-thumbnail",
    "--no-write-subs", "--no-write-auto-subs", "-f", "bestaudio",
    "--js-runtimes", f"deno:{attestation.deno_path}",
    "-o", str(download_path), canonical_watch_url(video_id),
)
ffmpeg_argv = (
    str(attestation.ffmpeg_path), "-nostdin", "-hide_banner", "-loglevel", "error",
    "-y", "-i", str(download_path), "-vn", "-ac", "1", "-ar", "16000",
    "-c:a", "pcm_s16le", str(wav_path),
)
```

Before and after each call, resolve the target and require it to be a non-reparse child of the per-job private work directory. Verify nonempty output and SHA-256. Reject every unexpected file in the work directory.

- [ ] **Step 4: Implement child process and sherpa adapter entrypoint**

The main process invokes the isolated Python executable with `-I -m market_voice_forecast_ledger.voice.adapter_main`, sends one JSON request on stdin, reads at most 1 MiB stdout, discards stderr, and enforces timeout. Pass an allowlisted environment containing only Windows runtime variables plus `PYTHONNOUSERSITE=1` and `PYTHONUTF8=1`; omit credentials, API keys, proxy variables, and browser state. `adapter_main` installs a deny-by-default Python socket factory before loading the attested CPU provider, WAV, model, VAD model, and supplied reference feature; it writes exactly one response JSON object. It never imports project database, credentials, YouTube client, service, API, or analysis modules.

- [ ] **Step 5: Run mutation and architecture boundary tests**

Run: `python -m pytest tests/backend/unit/test_voice_media.py tests/backend/unit/test_voice_protocol.py -q`

Expected: PASS for timeout, nonzero exit, oversized output, malformed JSON, path escape, reparse point, extra file, and sentinel nonleak cases.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/voice/media.py src/market_voice_forecast_ledger/voice/process.py src/market_voice_forecast_ledger/voice/adapter_main.py tests/backend/voice_fakes.py tests/backend/unit/test_voice_media.py tests/backend/unit/test_voice_protocol.py
git commit -m "feat: isolate local voice processing"
```

### Task 5: Reference Enrollment and Model Calibration

**Files:**
- Create: `src/market_voice_forecast_ledger/services/voice_reference.py`
- Test: `tests/backend/integration/test_voice_reference_enrollment.py`
- Test: `tests/backend/unit/test_voice_calibration.py`

**Interfaces:**
- Consumes: Task 2 repository, Task 3 runtime attestation, Task 4 media/adapter, existing subjects/videos.
- Produces: `VoiceReferenceService.approve_clip(command)`, `list_candidates(subject_id)`, `calibrate(model_candidates) -> CalibrationResult`, and `activate_calibration(result)`; approved pre-calibration clip metadata is durably represented by append-only `audit_events` until the selected reference profile exists.

- [ ] **Step 1: Write approval, enrollment, and calibration tests**

```python
def test_approve_clip_requires_existing_person_video_and_valid_range(db) -> None:
    service = reference_service(db)
    with pytest.raises(DomainError, match="VOICE_REFERENCE_INVALID"):
        service.approve_clip(ReferenceClipCommand(subject_id=1, video_id=999, start_ms=20_000, end_ms=10_000, actor="local_user", reason="clear solo speech"))


def test_calibration_selects_widest_separable_model() -> None:
    result = service_with_fake_scores({"campplus": (0.80, 0.30), "resnet34": (0.75, 0.10)}).calibrate(MODEL_CANDIDATES)
    assert result.model_name == "wespeaker_zh_cnceleb_resnet34.onnx"
    assert result.subject_boundary == 0.75
    assert result.interviewer_boundary == 0.10
```

- [ ] **Step 2: Run reference/calibration RED**

Run: `python -m pytest tests/backend/integration/test_voice_reference_enrollment.py tests/backend/unit/test_voice_calibration.py -q`

Expected: FAIL because `VoiceReferenceService` is missing.

- [ ] **Step 3: Implement clip approval and enrollment checks**

Validate exact positive integer IDs, person subject ownership, existing video, `0 <= start_ms < end_ms`, clip duration `3..120` seconds, fixed actor `local_user`, and reason using the repository audit-safe reason validator. Before model selection, append one `VOICE_REFERENCE_CLIP_APPROVED` audit event containing only subject/video/range/role and the canonical approval hash. Assign the first six approvals per subject to fixed slots: enrollment ordinals 1–2, held-out positive ordinal 3, and negative ordinals 4–6; reject a seventh approval in the first calibration version. Require enrollment duration to total at least 30 seconds, and require each negative video to belong to a different person candidate. During activation, copy these approved slots into immutable `voice_reference_clips` rows under the newly created selected-model reference profile.

- [ ] **Step 4: Implement deterministic two-model calibration**

For each attested model, compute one in-memory enrollment feature per person, score every held-out positive and negative, run Task 1 `calibrate_thresholds`, and discard models that raise `VOICE_MODEL_NOT_SEPARABLE`. Choose maximum margin, breaking exact margin ties by lower measured 20-candidate dry-run CPU milliseconds, then lexicographic model name. `activate_calibration()` deactivates old profile/config rows, inserts a new `speaker_threshold_configs` version, four new `voice_reference_profiles`, the approved clip rows, and four features in one transaction; its narrowly authorized transitions are reset in `finally`, and rollback restores the previous active rows. If every model is nonseparable, it performs no activation writes. Every acquired reference audio artifact is registered before scoring and deleted through `RetentionService` before activation commits.

- [ ] **Step 5: Run reference/calibration and rollback tests**

Run: `python -m pytest tests/backend/integration/test_voice_reference_enrollment.py tests/backend/unit/test_voice_calibration.py -q`

Expected: PASS for insufficient clips, overlapping category, corrupt feature hash, invalid model hash, tie-break, nonseparable all-model failure, and injected activation rollback.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/services/voice_reference.py tests/backend/integration/test_voice_reference_enrollment.py tests/backend/unit/test_voice_calibration.py
git commit -m "feat: enroll calibrated voice references"
```

### Task 6: Deterministic 20-Candidate Pilot and Durable Jobs

**Files:**
- Create: `src/market_voice_forecast_ledger/services/voice_verification.py`
- Test: `tests/backend/integration/test_presence_pilot.py`
- Test: `tests/backend/integration/test_voice_verification_jobs.py`

**Interfaces:**
- Consumes: active discovery profiles, canonical candidates/observations, active reference/calibration, `JobStateService.create_video_pipeline(manifest, candidate_ids)`.
- Produces: `PresenceVerificationService.preview_pilot() -> PilotPreview`, `create_pilot(expected_preview_hash) -> PilotCreation`, `save_proposal(job_id, response) -> int`, `list_pending_reviews()`, `show_review(run_id)`, and `review(command)`; the preview hash is an internal same-process guard, not a separate CLI contract.

- [ ] **Step 1: Write exact selection and atomic job creation tests**

```python
def test_pilot_selects_exact_five_per_active_profile(db) -> None:
    seed_selection_fixture(db)
    preview = PresenceVerificationService(db, fakes()).preview_pilot()
    assert len(preview.candidates) == 20
    assert Counter(item.profile_id for item in preview.candidates).values() == {5}
    assert source_kinds(preview, profile_with_seed=1) == ("seed_uploads", "seed_uploads", "cross_channel_search", "cross_channel_search", "cross_channel_search")


def test_pilot_creation_rolls_back_all_jobs_on_twentieth_failure(db) -> None:
    with pytest.raises(InjectedFailure):
        service_with_injected_job_failure(db, ordinal=20).create_pilot(preview_hash(db))
    assert count_rows(db, "voice_verification_manifests") == 0
    assert count_rows(db, "jobs") == 0
```

- [ ] **Step 2: Run pilot RED**

Run: `python -m pytest tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py -q`

Expected: FAIL because pilot methods are missing.

- [ ] **Step 3: Implement deterministic selection**

Candidate eligibility is: active profile, current `presence_unverified`, canonical first observation, and no active `video_pipeline` binding. Sort newest by `(published_at DESC, candidate_id ASC)` and oldest by `(published_at ASC, candidate_id ASC)`. For profiles with seeds select newest two seed, newest two search, then oldest remaining; without seeds select newest four search then oldest remaining. Backfill shortages from remaining eligible rows by newest order. Require exactly four profiles and five unique candidates each; otherwise raise `PRESENCE_PILOT_INSUFFICIENT` before writes.

- [ ] **Step 4: Implement preview hash and 20-job creation**

Preview freezes every candidate/video/profile/current-decision/reference/config/model/contract identity. `create_pilot(expected_preview_hash)` runs `BEGIN IMMEDIATE`, recomputes preview, rejects drift, creates exactly one seven-unit `JobManifest` and one `video_pipeline` job per candidate, binds only that candidate, inserts one immutable voice manifest, and commits all 20 or none.

- [ ] **Step 5: Run pilot/job and existing binding tests**

Run: `python -m pytest tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_video_candidate_bindings.py tests/backend/integration/test_job_checkpoints.py -q`

Expected: PASS for duplicate selection, active-job exclusion, source shortage backfill, stale preview, changed decision, rejected candidate, foreign reference, and crash rollback.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/services/voice_verification.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py
git commit -m "feat: create presence verification pilot"
```

### Task 7: Recoverable Presence Worker and Cleanup

**Files:**
- Create: `src/market_voice_forecast_ledger/workers/presence_verification.py`
- Test: `tests/backend/integration/test_voice_verification_jobs.py`
- Test: `tests/backend/e2e/test_presence_verification_flow.py`

**Interfaces:**
- Consumes: Tasks 3–6, `JobStateService.begin_unit`, `complete_unit_in_transaction`, `fail_unit`, `recover_interrupted`, `RetentionRepository.add_audio_artifact`, and `RetentionService.delete_audio`.
- Produces: `PresenceVerificationWorker.run_once() -> PresenceWorkerSummary` and `recover_job(job_id) -> ResumePlan`.

- [ ] **Step 1: Write unit-by-unit crash/recovery and cleanup tests**

```python
@pytest.mark.parametrize("crash_after", PRESENCE_UNIT_KEYS)
def test_worker_restarts_only_unverified_suffix(db, crash_after: str) -> None:
    first = worker_that_crashes_after(db, crash_after)
    with pytest.raises(SimulatedCrash):
        first.run_once()
    second = normal_worker(db)
    summary = second.run_once()
    assert summary.succeeded_jobs == 1
    assert_success_units_reused_only_when_artifact_hashes_verify(db)


def test_cleanup_failure_keeps_job_failed_and_records_retry(db) -> None:
    summary = worker_with_permission_cleanup_failure(db).run_once()
    assert summary.failed_code == "AUDIO_DELETE_PERMISSION"
    assert current_presence_state(db) == "presence_unverified"
```

- [ ] **Step 2: Run worker RED**

Run: `python -m pytest tests/backend/integration/test_voice_verification_jobs.py tests/backend/e2e/test_presence_verification_flow.py -q`

Expected: FAIL because `PresenceVerificationWorker` is missing.

- [ ] **Step 3: Implement one-wake FIFO execution**

Claim the lowest queued/retrying presence job ID under `BEGIN IMMEDIATE`. For each unit, compute the external input hash from immutable manifest plus the actual acquired/normalized/model/reference artifact hashes. Complete each unit only after its concrete artifact is durable and canonically reread. For `voice:proposal`, insert run plus all segments and complete the unit in the same transaction. Unknown or private exception details map to `VOICE_PROCESSING_FAILED`.

- [ ] **Step 4: Implement cleanup and recovery**

Register downloaded and normalized paths as `local_artifacts` immediately after creation. Cleanup calls `RetentionService.delete_audio()` for every artifact, verifies both files absent and rows `deleted`, then completes `audio:cleanup`. On restart, reuse a success unit only after recalculating its artifact hash; reset the first mismatched unit and all suffix units. Never adopt partial segments or an uncommitted adapter response.

- [ ] **Step 5: Run recovery, retention, and E2E tests**

Run: `python -m pytest tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_retention.py tests/backend/e2e/test_presence_verification_flow.py -q`

Expected: PASS for all seven crash points, input drift, corrupt stored run, process timeout, missing speech, cleanup permission failure/retry, stop/pause boundaries, and no automatic presence decision.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/workers/presence_verification.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/e2e/test_presence_verification_flow.py
git commit -m "feat: execute durable presence verification"
```

### Task 8: Human Review Transaction

**Files:**
- Modify: `src/market_voice_forecast_ledger/services/voice_verification.py`
- Test: `tests/backend/integration/test_presence_reviews.py`

**Interfaces:**
- Consumes: `VoiceVerificationRepository.add_review_and_decision`, immutable successful run, manifest-frozen prior decision.
- Produces: `ReviewCommand(run_id: int, action: ReviewAction, reason: str, actor: Literal['local_user'])`, `ReviewResult`, and public-safe `ReviewDetail`.

- [ ] **Step 1: Write review authority and stale-state tests**

```python
def test_model_proposal_never_changes_presence_pointer(db) -> None:
    run_id = create_successful_likely_present_run(db)
    assert review_service(db).show_review(run_id).proposal == VoiceProposal.LIKELY_PRESENT
    assert current_presence_state(db) == "presence_unverified"


@pytest.mark.parametrize("action,state", [("confirm", "presence_confirmed"), ("reject", "presence_rejected")])
def test_human_review_atomically_changes_pointer(db, action: str, state: str) -> None:
    result = review_service(db).review(ReviewCommand(run_id=successful_run(db), action=ReviewAction(action), reason="listened to the cited segment", actor="local_user"))
    assert result.current_state == state
```

- [ ] **Step 2: Run review RED**

Run: `python -m pytest tests/backend/integration/test_presence_reviews.py -q`

Expected: FAIL because review methods are missing.

- [ ] **Step 3: Implement strict review validation and transaction**

Before mutation verify successful job, completed cleanup, canonical manifest/run/segments, exact candidate/video/profile/reference/config identities, current pointer equal to frozen prior decision, and no existing review. Validate reason as `1..240` characters using the same public-safe audit grammar. Execute repository review write inside `BEGIN IMMEDIATE`; catch storage drift as `PRESENCE_REVIEW_STALE` without pointer movement.

- [ ] **Step 4: Implement public-safe review detail**

`show_review()` returns only person display name, canonical `https://www.youtube.com/watch?v=<id>`, YouTube video ID, segment start/end milliseconds, scores rounded to four decimals, proposal, model/version, adapter version, and threshold version. It omits local paths, feature bytes/hashes not needed for display, provider values, and command output.

- [ ] **Step 5: Run review and privacy tests**

Run: `python -m pytest tests/backend/integration/test_presence_reviews.py tests/backend/integration/test_private_boundary.py -q`

Expected: PASS for confirm/reject/hold, duplicate review, stale pointer, foreign candidate, corrupt rows, injected rollback, unsafe reason, and sentinel nonleak.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/services/voice_verification.py tests/backend/integration/test_presence_reviews.py
git commit -m "feat: review presence proposals"
```

### Task 9: Strict Presence CLI

**Files:**
- Modify: `src/market_voice_forecast_ledger/cli.py`
- Test: `tests/backend/integration/test_presence_cli.py`

**Interfaces:**
- Consumes: reference, pilot, worker, and review services through injectable factories.
- Produces: the exact `presence` command tree from the design, JSON-free bounded human-readable output, and exit codes `0` success, `2` grammar error, `1` safe domain failure.

- [ ] **Step 1: Write parser and safe-output tests**

```python
@pytest.mark.parametrize("argv", [
    ["presence", "pilot", "create", "--unknown"],
    ["presence", "review", "confirm", "1", "--reason", "a", "--reason", "b"],
    ["presence", "wor", "--once"],
])
def test_presence_cli_rejects_unknown_abbreviated_and_duplicate_options(argv) -> None:
    assert run_cli(argv, fakes()).exit_code == 2


def test_review_show_has_public_fields_only(private_sentinels) -> None:
    result = run_cli(["presence", "review", "show", "1"], fake_review_detail(private_sentinels))
    assert result.exit_code == 0
    assert all(value not in result.output for value in private_sentinels)
```

- [ ] **Step 2: Run CLI RED**

Run: `python -m pytest tests/backend/integration/test_presence_cli.py tests/backend/integration/test_cli.py -q`

Expected: FAIL with parser errors because the `presence` root is absent.

- [ ] **Step 3: Add the exact command grammar**

Add:

```text
presence reference list-candidates
presence reference approve --subject-id N --video-id N --start-ms N --end-ms N
presence calibrate
presence pilot create
presence worker --once
presence review list
presence review show RUN_ID
presence review confirm RUN_ID --reason TEXT
presence review reject RUN_ID --reason TEXT
presence review hold RUN_ID --reason TEXT
```

Use the existing `_SafeArgumentParser` and `_SingleUseAction`, require exact positive integer parsing, and lazily construct only the selected dependency. `reference approve` supplies the fixed audit reason `approved_reference_clip`. `pilot create` computes `preview_pilot()`, immediately passes that same preview hash to `create_pilot()`, and prints only the resulting exact job count. Never initialize the model runtime for list/show/review commands.

- [ ] **Step 4: Implement stable output/error mapping**

Use constant result prefixes such as `Presence pilot created: 20 jobs.`, `Presence review recorded: hold.`, and fixed allowlisted errors. `reference list-candidates` and `review show` may print canonical public watch URLs and bounded identifiers only. Catch unexpected exceptions as `Presence command failed.` without interpolation.

- [ ] **Step 5: Run new and existing CLI/private tests**

Run: `python -m pytest tests/backend/integration/test_presence_cli.py tests/backend/integration/test_cli.py tests/backend/integration/test_private_boundary.py -q`

Expected: PASS; existing YouTube credential/scheduler/worker commands remain unchanged.

- [ ] **Step 6: Commit**

```powershell
git add src/market_voice_forecast_ledger/cli.py tests/backend/integration/test_presence_cli.py
git commit -m "feat: add presence verification CLI"
```

### Task 10: Architecture Guards and Complete Synthetic E2E

**Files:**
- Create: `tests/backend/integration/test_presence_architecture.py`
- Create: `tests/backend/e2e/test_presence_verification_flow.py`
- Modify: `tests/backend/voice_fakes.py`

**Interfaces:**
- Consumes: all production interfaces from Tasks 1–9.
- Produces: executable final-schema/AST guards and exact four-person × five-candidate acceptance fixture.

- [ ] **Step 1: Write architecture mutation controls**

```python
def test_adapter_cannot_import_database_network_credentials_or_analysis() -> None:
    violations = runtime_import_violations(VOICE_ADAPTER_FILES)
    assert violations == ()


def test_only_review_service_can_write_confirmed_or_rejected_presence() -> None:
    assert presence_decision_writer_calls() == (
        "repositories/voice_verification.py:VoiceVerificationRepository.add_review_and_decision",
    )
```

Mutation controls must prove detection of function-local and relative imports, aliased imports, indirect confirmed/rejected literals, adapter network/subprocess expansion, and new transcript/speaker/analysis writers.

- [ ] **Step 2: Write the exact synthetic E2E inventory**

Build four active persons with five deterministic candidates each, fake approved reference sets, separable calibration, 20 fake adapter responses spanning all three proposals, crash one job and resume it, then apply confirm, reject, and hold reviews. Assert:

```python
assert table_count(db, "voice_verification_manifests") == 20
assert table_count(db, "voice_verification_runs") == 20
assert model_only_presence_changes(db) == 0
assert confirmed_or_rejected_changes(db) == confirm_review_count + reject_review_count
assert hold_pointer_changes(db) == 0
assert remaining_audio_artifacts(db) == 0
assert table_count(db, "transcript_segments") == 0
assert table_count(db, "speaker_assignments") == 0
assert table_count(db, "analysis_runs") == 0
```

- [ ] **Step 3: Run E2E/architecture RED against deliberate mutations**

Run: `python -m pytest tests/backend/integration/test_presence_architecture.py tests/backend/e2e/test_presence_verification_flow.py -q`

Expected: mutation fixtures fail when a model writes a decision, adapter imports DB, a 21st job appears, or a hidden audio file remains; unmutated fixture passes.

- [ ] **Step 4: Complete the guards and fixture helpers**

Use real migrations, real repositories/services/job transitions/review transaction, and only fake external media/model boundaries. Inventory all jobs, units, decisions, artifacts, transcripts, assignments, and analysis rows rather than querying only expected IDs.

- [ ] **Step 5: Run the full focused acceptance set**

Run: `python -m pytest tests/backend/unit/test_voice_calibration.py tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py tests/backend/unit/test_voice_media.py tests/backend/integration/test_voice_reference_enrollment.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_presence_reviews.py tests/backend/integration/test_presence_cli.py tests/backend/integration/test_presence_architecture.py tests/backend/e2e/test_presence_verification_flow.py -q`

Expected: PASS with no skip in the synthetic set.

- [ ] **Step 6: Commit**

```powershell
git add tests/backend/voice_fakes.py tests/backend/integration/test_presence_architecture.py tests/backend/e2e/test_presence_verification_flow.py
git commit -m "test: verify synthetic presence workflow"
```

### Task 11: Opt-in Real Runtime, Reference Research, and Pilot Acceptance

**Files:**
- Create: `tests/backend/integration/test_presence_real_smoke.py`
- Modify: `tests/backend/README.md`

**Interfaces:**
- Consumes: user-installed private runtime/model artifacts, public YouTube URLs researched at execution time, explicit user approval, and the production CLI.
- Produces: one disabled-by-default smoke gate, approved reference clip records, one active model/config/reference version, and a reviewed 20-candidate pilot evidence summary stored only in the private database/report.

- [ ] **Step 1: Write the opt-in smoke guard before touching real resources**

```python
def test_real_presence_runtime() -> None:
    if os.environ.get("MVFL_RUN_REAL_VOICE_SMOKE") != "1":
        pytest.skip("real presence voice smoke not requested")
    config = load_private_smoke_config()
    if not validate_safe_config_shape(config):
        pytest.fail("real presence voice smoke configuration invalid", pytrace=False)
    run_real_smoke_without_private_assert_values(config)
```

Add AST tests that reject rewritten `assert` statements and nonconstant failure messages inside the opt-in path.

- [ ] **Step 2: Run the normal skip gate**

Run: `python -m pytest tests/backend/integration/test_presence_real_smoke.py -q -rs`

Expected: exactly one skip, `real presence voice smoke not requested`; no network/model/native call.

- [ ] **Step 3: Install and attest the private runtime only after user approval**

Use the repository Python to create `Settings.voice_runtime_dir`, install the exact sherpa wheel with `--no-index --find-links <private-wheel-dir> --require-hashes`, place yt-dlp/Deno/FFmpeg/models under private roots, calculate every SHA-256, and write the private runtime lock. Run the opt-in smoke's `attest_runtime()` check only after the lock is complete; the check returns a fixed success line and does not print paths or hashes.

- [ ] **Step 4: Research and obtain user approval for reference clips**

For each of the four persons, research public videos and present exactly two enrollment clips totaling at least 30 seconds, one held-out positive, and three negatives with canonical watch URL plus `MM:SS–MM:SS`. Present them in that six-slot order because approval assigns roles deterministically. Do not append an approval audit event until the user confirms listening. After approval, invoke `presence reference approve` once per clip; the command uses fixed actor `local_user` and fixed reason `approved_reference_clip`.

- [ ] **Step 5: Run two-model calibration and stop if nonseparable**

Set `MVFL_RUN_REAL_VOICE_SMOKE=1` only in the current process, run the opt-in smoke for each model, then run `presence calibrate`. Accept an active config only if global minimum positive exceeds global maximum negative. If neither model qualifies, remove the environment flag, keep all presence pointers unchanged, report `VOICE_MODEL_NOT_SEPARABLE`, and return to design review.

- [ ] **Step 6: Create, execute, and manually review the exact pilot**

Run `presence pilot create`; the command performs preview/revalidation atomically and must report exactly 20 jobs. Inspect the resulting immutable 4×5 manifest inventory before executing workers. Repeatedly run `presence worker --once` until all 20 jobs are terminal. For every successful run, use `presence review show`, listen to cited public segments, and explicitly run confirm/reject/hold. Verify all ten Pilot Acceptance conditions from the spec by canonical DB reads; do not expand beyond 20.

- [ ] **Step 7: Clear opt-in state and run private-output audit**

Remove `MVFL_RUN_REAL_VOICE_SMOKE` and private config environment variables. Search captured bounded CLI output for known secret/path/provider/audio sentinels; require zero matches. Require zero temp audio files and no new transcript/speaker/analysis rows.

- [ ] **Step 8: Commit only test/document changes**

```powershell
git add tests/backend/integration/test_presence_real_smoke.py tests/backend/README.md
git commit -m "test: add opt-in presence runtime smoke"
```

Do not stage the private runtime lock, models, audio, database, calibration report containing raw scores, or researched operator notes.

### Task 12: Documentation, Full Verification, Independent Review, and Branch Handoff

**Files:**
- Modify: `README.md`
- Modify: `tests/backend/README.md`
- Modify: `docs/project/status.md`
- Modify: `docs/project/requirements.md`
- Modify: `docs/project/decisions.md`
- Modify: `docs/project/plan.md`

**Interfaces:**
- Consumes: verified as-built behavior and exact gate totals from Tasks 1–11.
- Produces: truthful operator commands, privacy boundary, current-state record, and review-ready branch.

- [ ] **Step 1: Update as-built documentation with observed evidence only**

Document the exact CLI tree, private runtime setup boundary, disabled real-smoke procedure, 20-candidate rollout cap, manual decision authority, cleanup rules, and non-goals. State actual focused/full counts only after the commands below finish; distinguish synthetic success, disabled real smoke, and any user-approved real pilot result.

- [ ] **Step 2: Run self-review scans before the slow suite**

Run:

```powershell
rg -n "presence_confirmed|presence_rejected" src/market_voice_forecast_ledger
git diff --check
python -m compileall -q src tests/backend
pwsh -NoProfile -File scripts/work-state/check-state-docs.ps1
pwsh -NoProfile -File scripts/work-state/check-public-safety.ps1 -Mode WorkingTree
```

Expected: placeholder scan empty; decision-writer scan contains schema/domain constants and the single review repository writer only; all gates exit 0.

- [ ] **Step 3: Run the exact focused gate**

Run: `python -m pytest tests/backend/unit/test_voice_calibration.py tests/backend/unit/test_voice_protocol.py tests/backend/unit/test_voice_runtime.py tests/backend/unit/test_voice_media.py tests/backend/integration/test_voice_reference_enrollment.py tests/backend/integration/test_presence_pilot.py tests/backend/integration/test_voice_verification_jobs.py tests/backend/integration/test_presence_reviews.py tests/backend/integration/test_presence_cli.py tests/backend/integration/test_presence_architecture.py tests/backend/integration/test_presence_real_smoke.py tests/backend/e2e/test_presence_verification_flow.py -q -rs`

Expected: all synthetic tests pass and exactly one real-smoke skip when opt-in is absent.

- [ ] **Step 4: Run full repository verification once on the final tree**

Run:

```powershell
python -m pytest tests/backend -q
python -m compileall -q src tests/backend
pwsh -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite All
pwsh -NoProfile -File scripts/work-state/check-state-docs.ps1
pwsh -NoProfile -File scripts/work-state/check-public-safety.ps1 -Mode WorkingTree
git diff --check
pwsh -NoProfile -File scripts/test-backend.ps1
```

Expected: zero failures; only established Windows capability skips plus the named opt-in real-smoke skip are acceptable.

- [ ] **Step 5: Request independent code review**

Provide the reviewer the approved spec, this plan, frozen diff, exact changed-path inventory, focused/full evidence, and these mandatory review targets: model cannot write decisions; migration collision guards; historical candidate snapshot handling; runtime/path nonleak; crash/recovery artifact verification; exact 20-job inventory; zero transcript/speaker/analysis writes.

- [ ] **Step 6: Stage exact reviewed paths and run staged safety**

Use explicit `git add <path>` for each reviewed path; never use `git add .`. Then run:

```powershell
pwsh -NoProfile -File scripts/work-state/check-public-safety.ps1 -Mode Staged
git diff --cached --check
git status --short
```

Expected: only approved implementation/test/doc paths are staged; no private file appears.

- [ ] **Step 7: Commit the final documentation/review fixes**

```powershell
git add README.md tests/backend/README.md docs/project/status.md docs/project/requirements.md docs/project/decisions.md docs/project/plan.md
git commit -m "docs: record presence verification workflow"
```

- [ ] **Step 8: Verify clean branch and present integration options**

Run: `git status --short; git log --oneline --decorate -12; git diff main...HEAD --check`

Expected: clean worktree/index and review-approved commits only. Use `superpowers:finishing-a-development-branch`; do not push, merge, create a PR, or delete the branch/worktree without the user's explicit choice.

## Plan Self-Review

- **Spec coverage:** Tasks 1–2 cover data/immutability; Tasks 3–4 cover isolation/protocol/media/privacy; Task 5 covers approved references and two-model calibration; Task 6 covers exact pilot selection and immutable one-candidate jobs; Task 7 covers durable units/recovery/cleanup; Task 8 preserves human-only decision authority; Task 9 covers strict CLI; Task 10 covers synthetic acceptance and architecture; Task 11 covers opt-in real evidence and user listening; Task 12 covers documentation, gates, review, and handoff. All design sections map to at least one task.
- **Placeholder scan:** The plan contains no deferred implementation markers. Runtime values that cannot be public repository constants—operator-selected FFmpeg binary hash, model artifact hashes, public reference URLs, and human review outcomes—have exact collection, validation, approval, and failure procedures.
- **Type consistency:** `VoiceProposal`, `ReviewAction`, `VoiceCalibration`, `VoiceManifestSnapshot`, `VoiceRunResult`, `VoiceVerificationRepository`, `VoiceReferenceService`, `PresenceVerificationService`, and `PresenceVerificationWorker` are defined once and consumed under the same names. Existing job calls match `JobStateService.create_video_pipeline(manifest, candidate_ids)`, `begin_unit(job_id, unit_key, external_input_hash)`, and `complete_unit_in_transaction(job_id, unit_key, output_hash)`.

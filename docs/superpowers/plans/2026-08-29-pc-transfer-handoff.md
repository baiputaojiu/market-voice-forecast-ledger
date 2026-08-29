# PC Transfer and Codex Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a reproducible Windows-to-Windows handoff that checkpoints the current feature branch to GitHub, transports every required non-Git private asset in one self-verifying Google Drive ZIP, and resumes the exact unfinished VAD work on the new PC.

**Architecture:** GitHub remains the only canonical development state. A standard-library Python package creates a quiescent SQLite snapshot, inventories a deliberately finite set of portable runtime inputs and private operator records, writes a canonical manifest, and verifies the completed ZIP before atomic publication; import verifies the Git checkpoint and the entire archive before placing anything in an empty local data root. The path-bound voice virtual environment is rebuilt and attested on the new PC, while credentials and Task Scheduler state are restored interactively outside the archive; Codex Handoff carries the chat when available but is not a recovery dependency.

**Tech Stack:** Windows 11, Python 3.14.6 standard library (`argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `shutil`, `sqlite3`, `subprocess`, `tempfile`, `venv`, `zipfile`), existing SQLite migrations, existing voice runtime attestation, pytest, PowerShell work-state tests, Git/GitHub, Google Drive Desktop.

**Spec:** `docs/superpowers/specs/2026-08-29-pc-transfer-handoff-design.md`

## Global Constraints

- GitHub is the sole canonical source for code, tests, requirements, decisions, plans, status, and the resumable work checkpoint.
- Google Drive is a temporary one-migration transport for one completed ZIP, never a live data directory, working tree, canonical copy, or continuous backup.
- Codex Handoff is a convenience path for chat and Git state, not a success condition; GitHub plus the ZIP must be sufficient to resume.
- Preserve the current `feature/presence-verification` branch and push that branch normally. Do not merge to `main`, rewrite history, or force-push for migration.
- A bundle may be finalized only from a clean tree whose `HEAD` exactly matches the live upstream branch SHA.
- Freeze the old PC before snapshotting: no app or worker process, no `running`, `pause_requested`, or `cancel_requested` job, no undeleted local artifact, empty `temp-audio`, and the old 06:00 task removed after its schedule is recorded.
- Snapshot SQLite with `sqlite3.Connection.backup`; never copy the live database file or its WAL, SHM, or journal companions.
- Include the database snapshot, fixed ONNX models, exact wheelhouse and requirements lock, Deno 2.9.5, FFmpeg 9.0.1, yt-dlp 2026.08.19, a wheel built from the manifest commit, and private presence-verification operator state.
- Exclude the repository, `.codex`, credentials, `voice-runtime`, `archive`, `task11-work`, temporary audio, logs, caches, deleted artifact files, and SQLite sidecars.
- Store only normalized relative POSIX member paths. Reject absolute paths, drive prefixes, `..`, backslashes, empty segments, duplicate entries, case-fold collisions, links/reparse-point sources, and unknown members.
- Export uses a private staging directory and temporary ZIP, verifies the ZIP from disk, and publishes only by same-directory atomic rename.
- Import fully verifies in a same-volume staging directory and refuses any non-empty final data root or non-empty operator-state destination. It never overwrites, merges, renames, backs up, or deletes an existing destination.
- If runtime reconstruction fails after verified data placement, keep the data and report setup as incomplete; do not delete it or silently fetch substitute versions.
- Credentials are never archived. Migration is not accepted until the YouTube API key is entered through the existing hidden-input CLI and reports `configured`.
- Recreate Task Scheduler only on the new PC and only at the recorded local time; the old PC remains stopped until acceptance.
- Keep the old private data and Drive ZIP after acceptance unless the user later gives a separate explicit deletion instruction.
- Deterministic tests use synthetic databases, repositories, remotes, tools, models, and credentials. Existing real-media/runtime tests remain explicit opt-in.
- Preserve the two current uncommitted VAD files first. Do not bump `vad_contract_version`, delete the 20 invalid pilot runs, or recreate the pilot as part of migration; those remain the first product task after the new PC is accepted.
- Do not restart the discarded complete-Python-semantics architecture-test expansion. Task 10 presence architecture checks stay finite: obvious direct SQL/table access, direct canonical-writer calls, and a short prohibited dynamic-dispatch list; DB constraints, transactions, canonical hash rereads, real SQLite integration, and E2E provide integrity evidence.
- Transfer failures use fixed `PC_TRANSFER_*` error codes and concise messages. Native exceptions, credential values, embedding bytes, private file contents, and subprocess output are not copied into public error text.

## File Map

### New production files

- `src/market_voice_forecast_ledger/pc_transfer/__init__.py` — package marker and stable manifest exports; orchestration imports focused submodules directly.
- `src/market_voice_forecast_ledger/pc_transfer/manifest.py` — strict immutable manifest types, canonical JSON, member-path rules, and bundle identity.
- `src/market_voice_forecast_ledger/pc_transfer/checkpoint.py` — local Git cleanliness/upstream inspection and live remote SHA verification.
- `src/market_voice_forecast_ledger/pc_transfer/snapshot.py` — source quiescence checks, SQLite Backup API snapshot, schema/count/feature validation.
- `src/market_voice_forecast_ledger/pc_transfer/portable.py` — finite portable-input inventory, source-file safety checks, and project-wheel build.
- `src/market_voice_forecast_ledger/pc_transfer/bundle.py` — transactional export, ZIP verification, staged import, and result records.
- `src/market_voice_forecast_ledger/pc_transfer/runtime_rebuild.py` — offline virtual-environment reconstruction and candidate/active lock attestation.
- `src/market_voice_forecast_ledger/pc_transfer/cli.py` — `export`, `verify`, `import`, and `rebuild-runtime` commands.
- `scripts/pc-transfer/pc-transfer.py` — repository-local standard-library bootstrap entry point.

### Modified production files

- `src/market_voice_forecast_ledger/voice/runtime.py` — attest a specifically named candidate lock without changing the existing default.
- `src/market_voice_forecast_ledger/config.py` — expose derived portable-staging paths without changing the default data root.

### New tests

- `tests/backend/unit/test_pc_transfer_manifest.py`
- `tests/backend/unit/test_pc_transfer_portable.py`
- `tests/backend/unit/test_pc_transfer_runtime_rebuild.py`
- `tests/backend/integration/test_pc_transfer_checkpoint.py`
- `tests/backend/integration/test_pc_transfer_snapshot.py`
- `tests/backend/integration/test_pc_transfer_bundle.py`
- `tests/backend/integration/test_pc_transfer_import.py`
- `tests/backend/integration/test_pc_transfer_cli.py`
- `tests/backend/e2e/test_pc_transfer_round_trip.py`

### Modified tests and documentation

- `tests/backend/unit/test_voice_runtime.py` — named-lock attestation contract.
- `.agents/skills/save-work-state/SKILL.md` — route explicit PC-transfer saves through freeze/export after the ordinary remote checkpoint.
- `.agents/skills/resume-work-state/SKILL.md` — route explicit PC-transfer resumes through verify/import/rebuild/credential/schedule acceptance.
- `tests/work-state/scenarios/save-work-state.md`
- `tests/work-state/scenarios/resume-work-state.md`
- `tests/work-state/run-tests.ps1`
- `tests/work-state/README.md`
- `AGENTS.md`
- `.gitignore`
- `scripts/work-state/check-public-safety.ps1`
- `README.md`
- `docs/project/requirements.md`
- `docs/project/decisions.md`
- `docs/project/plan.md`
- `docs/project/status.md`
- `docs/project/public-data-policy.md`

---

### Task 1: Preserve the Current Streaming-VAD Checkpoint

**Files:**
- Verify and commit only: `src/market_voice_forecast_ledger/voice/adapter_main.py`
- Verify and commit only: `tests/backend/unit/test_voice_protocol.py`

**Interfaces:**
- Consumes: the already-present working-tree change that drains VAD results after each fixed-size input window.
- Produces: one clean, pushed-later Git commit named `fix: stream presence VAD input`; no transfer code and no database mutation.

- [ ] **Step 1: Prove the dirty-path set is exactly the known VAD pair**

```powershell
$expected = @(
  'src/market_voice_forecast_ledger/voice/adapter_main.py',
  'tests/backend/unit/test_voice_protocol.py'
)
$actual = @(git status --porcelain=v1 | ForEach-Object { $_.Substring(3).Replace('\', '/') } | Sort-Object)
if (Compare-Object ($expected | Sort-Object) $actual) {
  throw 'Unexpected working-tree paths; stop before staging.'
}
```

Expected: no output and exit code 0. If any other path is present, stop and report it instead of changing or staging it.

- [ ] **Step 2: Inspect the exact existing diff**

Run: `git diff -- src/market_voice_forecast_ledger/voice/adapter_main.py tests/backend/unit/test_voice_protocol.py`

Expected: production drains the recognizer between fixed input windows, and the focused test proves segments are not reduced to the video tail. There must be no `vad-v2` lock change and no database deletion.

- [ ] **Step 3: Run the focused regression test**

Run: `python -m pytest tests/backend/unit/test_voice_protocol.py::test_presence_score_drains_vad_segments_between_windows -q`

Expected: PASS.

- [ ] **Step 4: Run the complete voice protocol unit file**

Run: `python -m pytest tests/backend/unit/test_voice_protocol.py -q`

Expected: PASS.

- [ ] **Step 5: Commit only the preserved checkpoint**

```powershell
git add -- src/market_voice_forecast_ledger/voice/adapter_main.py tests/backend/unit/test_voice_protocol.py
git diff --cached --check
git commit -m "fix: stream presence VAD input"
```

Expected: one focused commit and a clean working tree. Leave `vad_contract_version`, the invalid 20-run pilot, and pilot recreation unchanged.

---

### Task 2: Strict Transfer Manifest Contract

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/__init__.py`
- Create: `src/market_voice_forecast_ledger/pc_transfer/manifest.py`
- Create: `tests/backend/unit/test_pc_transfer_manifest.py`

**Interfaces:**
- Consumes: `canonical_json()` and `sha256_text()` from `market_voice_forecast_ledger.domain.common`, plus `DomainError`.
- Produces: `BundleMember`, `DatabaseSummary`, `RuntimeModel`, `RuntimeSummary`, `TransferManifest`, `encode_manifest(manifest) -> bytes`, `decode_manifest(raw) -> TransferManifest`, `compute_bundle_id(manifest) -> str`, and `validate_member_path(path) -> str`.

- [ ] **Step 1: Write failing round-trip and path-boundary tests**

```python
from dataclasses import replace

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.manifest import (
    BundleMember,
    DatabaseSummary,
    RuntimeModel,
    RuntimeSummary,
    TransferManifest,
    compute_bundle_id,
    decode_manifest,
    encode_manifest,
    validate_member_path,
)


def sample_manifest() -> TransferManifest:
    manifest = TransferManifest(
        schema="market-voice-pc-transfer.v1",
        bundle_id="0" * 64,
        created_at_utc="2026-08-29T03:04:05.000000Z",
        repository_url="https://github.com/example/market-voice-forecast-ledger.git",
        branch="feature/presence-verification",
        commit_sha="1" * 40,
        source_tree_clean=True,
        remote_verified=True,
        schedule_local_time="06:00",
        runtime_rebuild_required=True,
        credential_registration_required=True,
        schedule_install_required=True,
        operator_state_destination=".superpowers/sdd/2026-08-22-presence-verification",
        database=DatabaseSummary(
            integrity_check="ok",
            migrations=("0001_initial.sql", "0020_presence_verification.sql"),
            table_counts=(("jobs", 20), ("voice_reference_features", 4)),
            reference_feature_count=4,
            active_artifact_count=0,
            snapshot_sha256="2" * 64,
        ),
        runtime=RuntimeSummary(
            python_version="3.14.6",
            sherpa_onnx_version="1.13.4",
            yt_dlp_version="2026.08.19",
            deno_version="2.9.5",
            ffmpeg_version="9.0.1",
            vad_version="silero-vad-v5",
            provider="CPUExecutionProvider",
            adapter_contract_version="voice-adapter-v1",
            vad_contract_version="vad-v1",
            models=(
                RuntimeModel(
                    lock_name="runtime-lock.campplus.json",
                    model_name="3dspeaker",
                    model_version="campplus",
                    model_member=(
                        "portable/voice-models/"
                        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
                    ),
                    vad_member="portable/voice-models/silero_vad.onnx",
                    active=True,
                ),
                RuntimeModel(
                    lock_name="runtime-lock.wespeaker.json",
                    model_name="wespeaker",
                    model_version="zh-cnceleb-resnet34",
                    model_member=(
                        "portable/voice-models/"
                        "wespeaker_zh_cnceleb_resnet34.onnx"
                    ),
                    vad_member="portable/voice-models/silero_vad.onnx",
                    active=False,
                ),
            ),
        ),
        members=(
            BundleMember(
                path="data/ledger.sqlite3",
                role="database",
                size_bytes=123,
                sha256="2" * 64,
            ),
            BundleMember(
                path="operator-state/presence-verification/progress.md",
                role="operator-state",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path="portable/voice-install/deno.exe",
                role="runtime-tool",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path="portable/voice-install/ffmpeg.exe",
                role="runtime-tool",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-install/"
                    "market_voice_forecast_ledger-0.1.0-py3-none-any.whl"
                ),
                role="project-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path="portable/voice-install/yt-dlp.exe",
                role="runtime-tool",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-models/"
                    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
                ),
                role="model",
                size_bytes=456,
                sha256="3" * 64,
            ),
            BundleMember(
                path="portable/voice-models/silero_vad.onnx",
                role="model",
                size_bytes=789,
                sha256="4" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-models/"
                    "wespeaker_zh_cnceleb_resnet34.onnx"
                ),
                role="model",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "annotated_types-0.8.0-py3-none-any.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "pydantic-2.13.4-py3-none-any.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "pydantic_core-2.46.4-cp314-cp314-win_amd64.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path="portable/voice-wheelhouse/requirements-runtime.txt",
                role="runtime-requirements",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "sherpa_onnx-1.13.4-cp314-cp314-win_amd64.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "sherpa_onnx_core-1.13.4-py3-none-win_amd64.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "typing_extensions-4.16.0-py3-none-any.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
            BundleMember(
                path=(
                    "portable/voice-wheelhouse/"
                    "typing_inspection-0.4.4-py3-none-any.whl"
                ),
                role="runtime-wheel",
                size_bytes=10,
                sha256="3" * 64,
            ),
        ),
    )
    return replace(manifest, bundle_id=compute_bundle_id(manifest))


def test_manifest_round_trip_is_canonical() -> None:
    manifest = sample_manifest()
    raw = encode_manifest(manifest)
    assert raw.endswith(b"\n")
    assert decode_manifest(raw) == manifest
    assert encode_manifest(decode_manifest(raw)) == raw


@pytest.mark.parametrize(
    "path",
    (
        "/data/ledger.sqlite3",
        "C:/data/ledger.sqlite3",
        "../ledger.sqlite3",
        "data\\ledger.sqlite3",
        "data//ledger.sqlite3",
        "data/./ledger.sqlite3",
        "data/ledger.sqlite3:stream",
        "data/NUL.txt",
        "data/trailing.",
        "data/trailing ",
    ),
)
def test_member_path_rejects_non_portable_forms(path: str) -> None:
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        validate_member_path(path)


def test_manifest_rejects_case_fold_collision() -> None:
    manifest = sample_manifest()
    collision = BundleMember(
        path="DATA/ledger.sqlite3",
        role="database",
        size_bytes=123,
        sha256="2" * 64,
    )
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        encode_manifest(replace(manifest, members=manifest.members + (collision,)))
```

- [ ] **Step 2: Run the RED gate**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_manifest.py -q`

Expected: FAIL because `market_voice_forecast_ledger.pc_transfer.manifest` does not exist.

- [ ] **Step 3: Add the immutable public types**

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class BundleMember:
    path: str
    role: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatabaseSummary:
    integrity_check: str
    migrations: tuple[str, ...]
    table_counts: tuple[tuple[str, int], ...]
    reference_feature_count: int
    active_artifact_count: int
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    lock_name: str
    model_name: str
    model_version: str
    model_member: str
    vad_member: str
    active: bool


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    python_version: str
    sherpa_onnx_version: str
    yt_dlp_version: str
    deno_version: str
    ffmpeg_version: str
    vad_version: str
    provider: str
    adapter_contract_version: str
    vad_contract_version: str
    models: tuple[RuntimeModel, ...]


@dataclass(frozen=True, slots=True)
class TransferManifest:
    schema: str
    bundle_id: str
    created_at_utc: str
    repository_url: str
    branch: str
    commit_sha: str
    source_tree_clean: bool
    remote_verified: bool
    schedule_local_time: str
    runtime_rebuild_required: bool
    credential_registration_required: bool
    schedule_install_required: bool
    operator_state_destination: str
    database: DatabaseSummary
    runtime: RuntimeSummary
    members: tuple[BundleMember, ...]
```

- [ ] **Step 4: Implement finite validation and canonical identity**

Use these exact bounds and vocabularies:

```python
SCHEMA = "market-voice-pc-transfer.v1"
MEMBER_ROLES = frozenset(
    {
        "database",
        "model",
        "runtime-requirements",
        "runtime-wheel",
        "runtime-tool",
        "project-wheel",
        "operator-state",
    }
)
MEMBER_PREFIX_BY_ROLE = {
    "database": "data/",
    "model": "portable/voice-models/",
    "runtime-requirements": "portable/voice-wheelhouse/",
    "runtime-wheel": "portable/voice-wheelhouse/",
    "runtime-tool": "portable/voice-install/",
    "project-wheel": "portable/voice-install/",
    "operator-state": "operator-state/presence-verification/",
}
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
EXACT_MODEL_MEMBERS = frozenset(
    {
        "portable/voice-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "portable/voice-models/silero_vad.onnx",
        "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
    }
)
EXACT_RUNTIME_TOOL_MEMBERS = frozenset(
    {
        "portable/voice-install/deno.exe",
        "portable/voice-install/ffmpeg.exe",
        "portable/voice-install/yt-dlp.exe",
    }
)
EXACT_RUNTIME_WHEEL_MEMBERS = frozenset(
    {
        "portable/voice-wheelhouse/annotated_types-0.8.0-py3-none-any.whl",
        "portable/voice-wheelhouse/pydantic-2.13.4-py3-none-any.whl",
        "portable/voice-wheelhouse/pydantic_core-2.46.4-cp314-cp314-win_amd64.whl",
        "portable/voice-wheelhouse/sherpa_onnx-1.13.4-cp314-cp314-win_amd64.whl",
        "portable/voice-wheelhouse/sherpa_onnx_core-1.13.4-py3-none-win_amd64.whl",
        "portable/voice-wheelhouse/typing_extensions-4.16.0-py3-none-any.whl",
        "portable/voice-wheelhouse/typing_inspection-0.4.4-py3-none-any.whl",
    }
)
```

`validate_member_path()` must use `PurePosixPath`, require `path == parsed.as_posix()`, reject a leading slash, backslash, colon in any segment, empty/`.`/`..` segments, NUL, control characters, a segment ending in a space or dot, Windows device basenames (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, with or without an extension), and paths longer than 512 UTF-8 bytes. Manifest validation must require:

- exact schema;
- lower-case 64-hex bundle/member/database hashes and lower-case 40-hex commit SHA;
- exact microsecond UTC form `YYYY-MM-DDTHH:MM:SS.ffffffZ`;
- `HH:MM` schedule with hour 00–23 and minute 00–59;
- a non-empty repository URL and branch, each at most 512 UTF-8 bytes; URL-like remotes may not contain a password, query, fragment, control character, or whitespace;
- `source_tree_clean`, `remote_verified`, `runtime_rebuild_required`, `credential_registration_required`, and `schedule_install_required` are all exactly `True`;
- sorted unique migration names and table-count names;
- non-negative counts and sizes;
- sorted members by case-folded path, unique exact and case-folded member paths, role/prefix agreement, exactly one `data/ledger.sqlite3`, and its hash equal to `database.snapshot_sha256`;
- role sets exactly equal `EXACT_MODEL_MEMBERS`, `EXACT_RUNTIME_TOOL_MEMBERS`, `EXACT_RUNTIME_WHEEL_MEMBERS`, one `portable/voice-wheelhouse/requirements-runtime.txt`, and one `portable/voice-install/market_voice_forecast_ledger-0.1.0-py3-none-any.whl`; every non-operator member is directly below its fixed directory and operator-state has at least one descendant file;
- exactly two runtime models with lock names `runtime-lock.campplus.json` and `runtime-lock.wespeaker.json`, exactly one active model, and model/VAD paths present with role `model`;
- every runtime/version/contract value must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, and provider must be exactly `CPUExecutionProvider`;
- `operator_state_destination` must equal `.superpowers/sdd/2026-08-22-presence-verification` for schema v1;
- no serialized unknown key at any object level.

Compute identity over the complete canonical object with `bundle_id` replaced by 64 zeros:

```python
def compute_bundle_id(manifest: TransferManifest) -> str:
    normalized = replace(manifest, bundle_id="0" * 64)
    payload = _manifest_to_object(normalized)
    return sha256_text(canonical_json(payload))


def encode_manifest(manifest: TransferManifest) -> bytes:
    _validate_manifest(manifest)
    if manifest.bundle_id != compute_bundle_id(manifest):
        raise DomainError(
            "PC_TRANSFER_MANIFEST_INVALID",
            "transfer manifest is invalid",
        )
    return (canonical_json(_manifest_to_object(manifest)) + "\n").encode("utf-8")


def decode_manifest(raw: bytes) -> TransferManifest:
    if not raw or len(raw) > MAX_MANIFEST_BYTES or raw[-1:] != b"\n":
        raise DomainError(
            "PC_TRANSFER_MANIFEST_INVALID",
            "transfer manifest is invalid",
        )
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "PC_TRANSFER_MANIFEST_INVALID",
            "transfer manifest is invalid",
        ) from exc
    manifest = _manifest_from_object(parsed)
    if encode_manifest(manifest) != raw:
        raise DomainError(
            "PC_TRANSFER_MANIFEST_INVALID",
            "transfer manifest is invalid",
        )
    return manifest
```

Export the nine dataclasses/functions/constants used by later tasks from `pc_transfer/__init__.py`; do not export private parsers.

- [ ] **Step 5: Add malformed-type, unknown-key, ordering, size, runtime-model, and identity tests**

```python
def test_manifest_rejects_unknown_top_level_key() -> None:
    raw = json.loads(encode_manifest(sample_manifest()))
    raw["source_path"] = "C:/Users/example"
    encoded = (canonical_json(raw) + "\n").encode()
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        decode_manifest(encoded)


def test_manifest_rejects_changed_identity() -> None:
    manifest = sample_manifest()
    changed = replace(manifest, branch="feature/other")
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        encode_manifest(changed)


def test_manifest_rejects_two_active_models() -> None:
    manifest = sample_manifest()
    second = replace(manifest.runtime.models[1], active=True)
    runtime = replace(
        manifest.runtime,
        models=(manifest.runtime.models[0], second),
    )
    candidate = replace(manifest, runtime=runtime)
    candidate = replace(candidate, bundle_id=compute_bundle_id(candidate))
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        encode_manifest(candidate)


def test_manifest_rejects_remote_url_with_embedded_credential() -> None:
    manifest = sample_manifest()
    candidate = replace(
        manifest,
        repository_url="https://user:example-token@example.invalid/repo.git",
    )
    candidate = replace(candidate, bundle_id=compute_bundle_id(candidate))
    with pytest.raises(DomainError, match="PC_TRANSFER_MANIFEST_INVALID"):
        encode_manifest(candidate)
```

- [ ] **Step 6: Run manifest tests**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_manifest.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/__init__.py src/market_voice_forecast_ledger/pc_transfer/manifest.py tests/backend/unit/test_pc_transfer_manifest.py
git diff --cached --check
git commit -m "feat: define PC transfer manifest"
```

---

### Task 3: Verified Git Checkpoint and SQLite Snapshot

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/checkpoint.py`
- Create: `src/market_voice_forecast_ledger/pc_transfer/snapshot.py`
- Create: `tests/backend/integration/test_pc_transfer_checkpoint.py`
- Create: `tests/backend/integration/test_pc_transfer_snapshot.py`

**Interfaces:**
- Consumes: `DatabaseSummary` from Task 2, repository migration filenames, and the existing final SQLite schema.
- Produces: `GitCheckpoint`, `inspect_git_checkpoint(repository_root, runner=run_command) -> GitCheckpoint`, `SnapshotResult`, `create_database_snapshot(source, destination, expected_migrations, backup_progress=None) -> SnapshotResult`, and `validate_database_snapshot(path, expected) -> None`.

- [ ] **Step 1: Write failing Git checkpoint tests with a local bare remote**

```python
def test_checkpoint_requires_clean_live_upstream(tmp_path: Path) -> None:
    remote, work = create_pushed_repository(tmp_path, branch="feature/test")
    checkpoint = inspect_git_checkpoint(work)
    assert checkpoint.branch == "feature/test"
    assert checkpoint.commit_sha == git(work, "rev-parse", "HEAD")
    assert checkpoint.remote_sha == checkpoint.commit_sha
    assert checkpoint.repository_url == str(remote.resolve())


def test_checkpoint_rejects_dirty_tree(tmp_path: Path) -> None:
    _, work = create_pushed_repository(tmp_path, branch="feature/test")
    (work / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(DomainError, match="PC_TRANSFER_GIT_UNVERIFIED"):
        inspect_git_checkpoint(work)


def test_checkpoint_rejects_remote_sha_mismatch(tmp_path: Path) -> None:
    remote, work = create_pushed_repository(tmp_path, branch="feature/test")
    advance_remote_from_second_clone(remote, tmp_path / "second", "feature/test")
    with pytest.raises(DomainError, match="PC_TRANSFER_GIT_UNVERIFIED"):
        inspect_git_checkpoint(work)
```

- [ ] **Step 2: Run the Git RED gate**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_checkpoint.py -q`

Expected: FAIL because `pc_transfer.checkpoint` does not exist.

- [ ] **Step 3: Implement exact local/live-remote inspection**

```python
@dataclass(frozen=True, slots=True)
class GitCheckpoint:
    repository_url: str
    branch: str
    commit_sha: str
    upstream: str
    remote_name: str
    remote_sha: str


CommandRunner = Callable[[Path, tuple[str, ...]], str]


def _git_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_GIT_UNVERIFIED",
        "Git checkpoint is not verified",
    )


def run_command(repository_root: Path, command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=repository_root,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _git_error() from exc
    if completed.returncode != 0:
        raise _git_error()
    return completed.stdout.strip()


def inspect_git_checkpoint(
    repository_root: Path,
    runner: CommandRunner = run_command,
) -> GitCheckpoint:
    root = repository_root.resolve(strict=True)
    if runner(root, ("git", "status", "--porcelain=v1", "--untracked-files=all")):
        raise _git_error()
    branch = runner(root, ("git", "symbolic-ref", "--quiet", "--short", "HEAD"))
    commit_sha = runner(root, ("git", "rev-parse", "--verify", "HEAD"))
    upstream = runner(
        root,
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
    )
    remote_name = runner(
        root,
        ("git", "config", "--get", f"branch.{branch}.remote"),
    )
    merge_ref = runner(
        root,
        ("git", "config", "--get", f"branch.{branch}.merge"),
    )
    expected_ref = f"refs/heads/{branch}"
    if merge_ref != expected_ref or upstream != f"{remote_name}/{branch}":
        raise _git_error()
    repository_url = runner(root, ("git", "remote", "get-url", remote_name))
    response = runner(
        root,
        ("git", "ls-remote", "--exit-code", repository_url, expected_ref),
    )
    fields = response.split()
    if len(fields) != 2 or fields[1] != expected_ref:
        raise _git_error()
    remote_sha = fields[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha) or remote_sha != commit_sha:
        raise _git_error()
    return GitCheckpoint(
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
        upstream=upstream,
        remote_name=remote_name,
        remote_sha=remote_sha,
    )
```

`run_command()` must use a fixed argument tuple, `shell=False`, `check=False`, UTF-8 decoding, a 30-second timeout, and return stripped stdout only on exit code 0. Map missing Git, timeout, nonzero exit, detached HEAD, missing upstream, malformed output, and live-remote mismatch to `DomainError("PC_TRANSFER_GIT_UNVERIFIED", "Git checkpoint is not verified")`; do not place stdout/stderr in the error.

- [ ] **Step 4: Run Git checkpoint tests**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_checkpoint.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing snapshot, quiescence, and hash tests**

```python
IMPORTANT_TABLES = (
    "analysis_subjects",
    "videos",
    "subject_video_candidates",
    "presence_decisions",
    "jobs",
    "job_units",
    "job_unit_attempts",
    "job_events",
    "video_pipeline_job_binding_sets",
    "video_pipeline_job_bindings",
    "voice_reference_profiles",
    "voice_reference_clips",
    "voice_reference_features",
    "voice_reference_calibrations",
    "voice_verification_manifests",
    "voice_verification_runs",
    "voice_verification_segments",
    "voice_verification_reviews",
    "local_artifacts",
)


def test_wal_database_is_restored_as_one_standalone_snapshot(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    destination = tmp_path / "snapshot.sqlite3"
    expected_migrations = repository_migration_names()
    result = create_database_snapshot(
        migrated_db,
        destination,
        expected_migrations,
    )
    assert result.database.integrity_check == "ok"
    assert result.database.reference_feature_count == 1
    assert result.database.active_artifact_count == 0
    assert not destination.with_name(destination.name + "-wal").exists()
    assert not destination.with_name(destination.name + "-shm").exists()
    validate_database_snapshot(destination, result.database)


@pytest.mark.parametrize("unsafe_state", ("running_job", "active_artifact"))
def test_snapshot_rejects_non_quiescent_source(
    migrated_db: Path,
    tmp_path: Path,
    unsafe_state: str,
) -> None:
    seed_valid_reference_feature(migrated_db)
    seed_unsafe_state(migrated_db, unsafe_state)
    with pytest.raises(DomainError, match="PC_TRANSFER_SOURCE_NOT_QUIESCENT"):
        create_database_snapshot(
            migrated_db,
            tmp_path / "snapshot.sqlite3",
            repository_migration_names(),
        )


def test_snapshot_rejects_corrupt_reference_feature(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    corrupt_feature_hash_with_guard_bypass(migrated_db)
    with pytest.raises(DomainError, match="PC_TRANSFER_DATABASE_INVALID"):
        create_database_snapshot(
            migrated_db,
            tmp_path / "snapshot.sqlite3",
            repository_migration_names(),
        )
```

- [ ] **Step 6: Run the snapshot RED gate**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_snapshot.py -q`

Expected: FAIL because `pc_transfer.snapshot` does not exist.

- [ ] **Step 7: Implement source and snapshot inspection**

Use repository filenames, not a hard-coded migration count:

```python
IMPORTANT_TABLES = (
    "analysis_subjects",
    "videos",
    "subject_video_candidates",
    "presence_decisions",
    "jobs",
    "job_units",
    "job_unit_attempts",
    "job_events",
    "video_pipeline_job_binding_sets",
    "video_pipeline_job_bindings",
    "voice_reference_profiles",
    "voice_reference_clips",
    "voice_reference_features",
    "voice_reference_calibrations",
    "voice_verification_manifests",
    "voice_verification_runs",
    "voice_verification_segments",
    "voice_verification_reviews",
    "local_artifacts",
)
ACTIVE_JOB_STATES = ("running", "pause_requested", "cancel_requested")


def _database_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_DATABASE_INVALID",
        "transfer database is invalid",
    )


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    path: Path
    database: DatabaseSummary


def _inspect_connection(
    connection: sqlite3.Connection,
    expected_migrations: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...], int, int]:
    integrity_rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
    if integrity_rows != ("ok",):
        raise _database_error()
    migrations = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        )
    )
    if migrations != expected_migrations:
        raise _database_error()
    table_counts = tuple(
        (table, _strict_scalar_count(connection, table))
        for table in IMPORTANT_TABLES
    )
    active_jobs = connection.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE status IN ('running', 'pause_requested', 'cancel_requested')"
    ).fetchone()[0]
    active_artifacts = connection.execute(
        "SELECT COUNT(*) FROM local_artifacts "
        "WHERE status != 'deleted' OR deleted_at IS NULL"
    ).fetchone()[0]
    feature_rows = tuple(
        connection.execute(
            "SELECT embedding_blob, feature_sha256 "
            "FROM voice_reference_features ORDER BY id"
        )
    )
    if not feature_rows:
        raise _database_error()
    for embedding_blob, feature_sha256 in feature_rows:
        if (
            type(embedding_blob) is not bytes
            or type(feature_sha256) is not str
            or hashlib.sha256(embedding_blob).hexdigest() != feature_sha256
        ):
            raise _database_error()
    if active_jobs or active_artifacts:
        raise DomainError(
            "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
            "transfer source is not quiescent",
        )
    return migrations, table_counts, len(feature_rows), active_artifacts
```

`_strict_scalar_count()` must interpolate only a table from the fixed `IMPORTANT_TABLES` tuple, require a single SQLite integer at least zero, and reject missing tables. Open the source using `file:<quoted absolute path>?mode=ro` with `uri=True`, set `row_factory=None`, and never run migrations during export.

- [ ] **Step 8: Implement Backup API copy and source-change detection**

```python
def create_database_snapshot(
    source: Path,
    destination: Path,
    expected_migrations: tuple[str, ...],
    backup_progress: Callable[[int, int, int], None] | None = None,
) -> SnapshotResult:
    if destination.exists() or not source.is_file():
        raise _database_error()
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = source.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            before_version = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            before = _inspect_connection(source_connection, expected_migrations)
            with sqlite3.connect(destination) as snapshot_connection:
                source_connection.backup(
                    snapshot_connection,
                    pages=128,
                    progress=backup_progress,
                    sleep=0.0,
                )
            after_version = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            after = _inspect_connection(source_connection, expected_migrations)
        if before_version != after_version or before != after:
            destination.unlink(missing_ok=True)
            raise DomainError(
                "PC_TRANSFER_SOURCE_CHANGED",
                "transfer source changed during snapshot",
            )
        database = _summarize_closed_snapshot(
            destination,
            expected_migrations,
        )
        return SnapshotResult(path=destination, database=database)
    except DomainError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as exc:
        destination.unlink(missing_ok=True)
        raise _database_error() from exc
```

`_summarize_closed_snapshot()` must reopen the destination with `mode=ro`, repeat `_inspect_connection()`, run `PRAGMA wal_checkpoint` nowhere, close it, reject any `-wal`, `-shm`, or `-journal` sibling, hash the closed file in 1 MiB chunks, and return `DatabaseSummary`. `validate_database_snapshot()` repeats the same checks and requires every field and hash to equal the expected summary.

- [ ] **Step 9: Add deterministic concurrent-source-change and missing-migration tests**

Use the backup progress callback once, from a second connection, to commit a row while the source connection remains open:

```python
def test_snapshot_rejects_source_commit_during_backup(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    changed = False

    def mutate_once(status: int, remaining: int, total: int) -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        with sqlite3.connect(migrated_db) as writer:
            insert_safe_completed_job(writer)

    with pytest.raises(DomainError, match="PC_TRANSFER_SOURCE_CHANGED"):
        create_database_snapshot(
            migrated_db,
            tmp_path / "snapshot.sqlite3",
            repository_migration_names(),
            backup_progress=mutate_once,
        )
    assert not (tmp_path / "snapshot.sqlite3").exists()


def test_snapshot_rejects_migration_identity_mismatch(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    wrong = repository_migration_names()[:-1]
    with pytest.raises(DomainError, match="PC_TRANSFER_DATABASE_INVALID"):
        create_database_snapshot(migrated_db, tmp_path / "snapshot.sqlite3", wrong)
```

- [ ] **Step 10: Run both component suites**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_checkpoint.py tests/backend/integration/test_pc_transfer_snapshot.py -q`

Expected: PASS.

- [ ] **Step 11: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/checkpoint.py src/market_voice_forecast_ledger/pc_transfer/snapshot.py tests/backend/integration/test_pc_transfer_checkpoint.py tests/backend/integration/test_pc_transfer_snapshot.py
git diff --cached --check
git commit -m "feat: verify transfer checkpoint and database"
```

---

### Task 4: Finite Portable Runtime and Operator-State Inventory

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/portable.py`
- Create: `tests/backend/unit/test_pc_transfer_portable.py`
- Modify: `src/market_voice_forecast_ledger/voice/runtime.py`
- Modify: `tests/backend/unit/test_voice_runtime.py`

**Interfaces:**
- Consumes: `Settings`, existing `attest_runtime()`, the two candidate runtime locks, fixed private model/tool paths, the wheelhouse lock, and a repository at the verified commit.
- Produces: `PortableFile`, `PortableInventory`, `build_project_wheel(repository_root, build_root, expected_commit, runner=run_process) -> Path`, `collect_portable_inventory(settings, repository_root, expected_commit, operator_state_dir, build_dir, version_probe, runner=run_process) -> PortableInventory`, and `attest_runtime(..., lock_name="runtime-lock.json")`.

- [ ] **Step 1: Write a failing named-lock attestation test**

```python
def test_attest_runtime_accepts_only_a_finite_candidate_lock(
    valid_runtime: RuntimeFixture,
) -> None:
    candidate = (
        valid_runtime.settings.voice_runtime_dir
        / "runtime-lock.campplus.json"
    )
    candidate.write_bytes(
        (
            valid_runtime.settings.voice_runtime_dir
            / "runtime-lock.json"
        ).read_bytes()
    )
    result = attest_runtime(
        valid_runtime.settings,
        version_probe=valid_runtime.version_probe,
        lock_name="runtime-lock.campplus.json",
    )
    assert result.model_name == valid_runtime.model_name

    with pytest.raises(DomainError, match="VOICE_RUNTIME_INVALID"):
        attest_runtime(
            valid_runtime.settings,
            version_probe=valid_runtime.version_probe,
            lock_name="../runtime-lock.json",
        )
```

- [ ] **Step 2: Run the named-lock RED gate**

Run: `python -m pytest tests/backend/unit/test_voice_runtime.py::test_attest_runtime_accepts_only_a_finite_candidate_lock -q`

Expected: FAIL because `attest_runtime()` does not accept `lock_name`.

- [ ] **Step 3: Extend attestation without weakening the default**

```python
_TRANSFERABLE_LOCK_NAMES = frozenset(
    {
        "runtime-lock.json",
        "runtime-lock.campplus.json",
        "runtime-lock.wespeaker.json",
    }
)


def attest_runtime(
    settings: Settings,
    *,
    version_probe: VersionProbe,
    allowlists: RuntimeAllowlists = RuntimeAllowlists(),
    lock_name: str = "runtime-lock.json",
) -> RuntimeAttestation:
    try:
        if (
            not isinstance(settings, Settings)
            or not isinstance(allowlists, RuntimeAllowlists)
            or type(lock_name) is not str
            or lock_name not in _TRANSFERABLE_LOCK_NAMES
        ):
            raise ValueError("invalid runtime inputs")
        _validate_allowlists(allowlists)
        data_root = _private_root(settings.data_dir)
        runtime_root = _private_child_root(settings.voice_runtime_dir, data_root)
        model_root = _private_child_root(settings.voice_model_dir, data_root)
        lock_path = _private_file(settings.voice_runtime_dir / lock_name, runtime_root)
        lock = _read_lock(lock_path)
        return _attest_validated_lock(
            lock,
            lock_path,
            runtime_root,
            model_root,
            version_probe,
            allowlists,
        )
    except Exception:
        raise _runtime_invalid() from None
```

Extract the present attestation body into `_attest_validated_lock()` byte-for-byte except for receiving the already-read lock and roots. Keep `runtime-lock.json` as the default for every existing caller. Candidate names are a finite migration contract; do not accept a regex or arbitrary filename.

- [ ] **Step 4: Run the complete runtime unit suite**

Run: `python -m pytest tests/backend/unit/test_voice_runtime.py -q`

Expected: PASS, including all existing default-lock corruption cases.

- [ ] **Step 5: Write failing portable-inventory tests**

```python
EXPECTED_MEMBER_PATHS = {
    "portable/voice-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "portable/voice-models/silero_vad.onnx",
    "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
    "portable/voice-wheelhouse/requirements-runtime.txt",
    "portable/voice-install/deno.exe",
    "portable/voice-install/ffmpeg.exe",
    "portable/voice-install/yt-dlp.exe",
    "operator-state/presence-verification/progress.md",
}


def test_inventory_contains_only_rebuild_inputs(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    inventory = collect_portable_inventory(
        settings=portable_source.settings,
        repository_root=portable_source.repository_root,
        expected_commit=portable_source.commit_sha,
        operator_state_dir=portable_source.operator_state_dir,
        build_dir=tmp_path / "build",
        version_probe=portable_source.version_probe,
        runner=portable_source.runner,
    )
    paths = {item.path for item in inventory.files}
    assert EXPECTED_MEMBER_PATHS <= paths
    assert sum(path.endswith(".whl") for path in paths) == 8
    assert not any("voice-runtime" in path for path in paths)
    assert not any("archive" in path for path in paths)
    assert not any("task11-work" in path for path in paths)
    assert not any("project-wheel-old" in path for path in paths)
    assert len([item for item in inventory.files if item.role == "project-wheel"]) == 1
    assert len([model for model in inventory.runtime.models if model.active]) == 1


def test_inventory_rejects_reparse_or_symlink_source(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = portable_source.settings.voice_model_dir / "silero_vad.onnx"
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original(path),
    )
    with pytest.raises(DomainError, match="PC_TRANSFER_PORTABLE_INVALID"):
        collect_portable_inventory(
            settings=portable_source.settings,
            repository_root=portable_source.repository_root,
            expected_commit=portable_source.commit_sha,
            operator_state_dir=portable_source.operator_state_dir,
            build_dir=tmp_path / "build",
            version_probe=portable_source.version_probe,
            runner=portable_source.runner,
        )
```

- [ ] **Step 6: Run the portable RED gate**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_portable.py -q`

Expected: FAIL because `pc_transfer.portable` does not exist.

- [ ] **Step 7: Implement the finite source map and source safety**

```python
MODEL_FILES = (
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "silero_vad.onnx",
    "wespeaker_zh_cnceleb_resnet34.onnx",
)
CANDIDATE_LOCKS = (
    "runtime-lock.campplus.json",
    "runtime-lock.wespeaker.json",
)
TOOL_FILES = (
    (
        Path("voice-work/install/deno-2.9.5/deno.exe"),
        "portable/voice-install/deno.exe",
    ),
    (
        Path(
            "voice-work/install/ffmpeg-9.0.1/"
            "ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe"
        ),
        "portable/voice-install/ffmpeg.exe",
    ),
    (
        Path("voice-work/install/yt-dlp.exe"),
        "portable/voice-install/yt-dlp.exe",
    ),
)
MAX_PORTABLE_FILES = 20_000
MAX_PORTABLE_BYTES = 4 * 1024 * 1024 * 1024
EXPECTED_REQUIREMENT_LINES = (
    "annotated-types==0.8.0 --hash=sha256:f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0",
    "pydantic==2.13.4 --hash=sha256:45a282cde31d808236fd7ea9d919b128653c8b38b393d1c4ab335c62924d9aba",
    "pydantic-core==2.46.4 --hash=sha256:811ff8e9c313ab425368bcbb36e5c4ebd7108c2bbf4e4089cfbb0b01eff63fac",
    "typing-extensions==4.16.0 --hash=sha256:481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
    "typing-inspection==0.4.4 --hash=sha256:65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147",
    "sherpa-onnx==1.13.4 --hash=sha256:cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06",
    "sherpa-onnx-core==1.13.4 --hash=sha256:0a6949cf0fd83adb9fbcfdf5c27b8907a57f7b48626db703c7f6037be9b61764",
)


@dataclass(frozen=True, slots=True)
class PortableFile:
    source: Path
    path: str
    role: str


@dataclass(frozen=True, slots=True)
class PortableInventory:
    files: tuple[PortableFile, ...]
    runtime: RuntimeSummary


class ProcessResult(Protocol):
    returncode: int


ProcessRunner = Callable[[tuple[str, ...], Path], ProcessResult]


def run_process(command: tuple[str, ...], working_directory: Path) -> ProcessResult:
    environment = os.environ.copy()
    completed = subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        shell=False,
        check=False,
        capture_output=True,
        timeout=300,
    )
    return completed


def _portable_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_PORTABLE_INVALID",
        "portable transfer input is invalid",
    )


def _require_regular_source(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = path.resolve(strict=True)
    candidate.relative_to(resolved_root)
    relative_parts = candidate.relative_to(resolved_root).parts
    current = resolved_root
    for part in relative_parts:
        current = current / part
        attributes = getattr(current.lstat(), "st_file_attributes", 0)
        if current.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise _portable_error()
    if not candidate.is_file():
        raise _portable_error()
    return candidate
```

Only the three `MODEL_FILES`, direct wheelhouse `*.whl` files other than `market_voice_forecast_ledger-*.whl`, `requirements-runtime.txt`, the three exact `TOOL_FILES`, the newly built wheel, and recursively enumerated regular files under the one passed operator-state directory may enter the inventory. Require exactly the seven dependency-wheel filenames from Task 2, exactly one requirements file, UTF-8 decoding, and `tuple(requirements_text.splitlines()) == EXPECTED_REQUIREMENT_LINES`. Traverse operator state with `os.walk(..., followlinks=False)`, reject every linked/reparse directory or file, map it below `operator-state/presence-verification/`, and require at least `progress.md`. Sort by case-folded member path and enforce count/total-byte bounds.

- [ ] **Step 8: Build exactly one wheel from the verified checkout**

```python
def build_project_wheel(
    repository_root: Path,
    build_root: Path,
    expected_commit: str,
    runner: ProcessRunner = run_process,
) -> Path:
    if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
        raise _portable_error()
    build_root.mkdir(parents=True, exist_ok=False)
    source_root = build_root / "source"
    output_dir = build_root / "wheel"
    output_dir.mkdir()
    commands = (
        (
            "git",
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            str(repository_root.resolve(strict=True)),
            str(source_root),
        ),
        (
            "git",
            "-C",
            str(source_root),
            "checkout",
            "--quiet",
            "--detach",
            expected_commit,
        ),
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output_dir),
            str(source_root),
        ),
    )
    for command in commands:
        try:
            completed = runner(command, build_root)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _portable_error() from exc
        if completed.returncode != 0:
            raise _portable_error()
    wheels = tuple(output_dir.glob("market_voice_forecast_ledger-*.whl"))
    if len(wheels) != 1 or tuple(output_dir.iterdir()) != wheels:
        raise _portable_error()
    return _require_regular_source(wheels[0], output_dir)
```

The detached local clone ensures ignored build artifacts in the working checkout cannot enter the wheel; no remote is contacted. `run_process()` uses `shell=False`, no environment secrets in output, a 300-second timeout, and maps every failure to `PC_TRANSFER_PORTABLE_INVALID`. The inventory maps this file to `portable/voice-install/<wheel filename>` with role `project-wheel`; it never reuses any pre-existing project wheel from `voice-wheelhouse` or `voice-work/install/project-wheel-*`.

- [ ] **Step 9: Derive runtime summary from attested active and candidate locks**

```python
def _runtime_summary(
    settings: Settings,
    version_probe: VersionProbe,
) -> RuntimeSummary:
    active = attest_runtime(settings, version_probe=version_probe)
    candidates = tuple(
        (
            lock_name,
            attest_runtime(
                settings,
                version_probe=version_probe,
                lock_name=lock_name,
            ),
        )
        for lock_name in CANDIDATE_LOCKS
    )
    active_matches = tuple(
        item.model_sha256 == active.model_sha256
        and item.model_name == active.model_name
        and item.model_version == active.model_version
        and item.vad_sha256 == active.vad_sha256
        and item.adapter_contract_version == active.adapter_contract_version
        and item.vad_contract_version == active.vad_contract_version
        for _, item in candidates
    )
    if active_matches.count(True) != 1:
        raise _portable_error()
    shared = (
        active.python_version,
        active.sherpa_onnx_version,
        active.yt_dlp_version,
        active.deno_version,
        active.ffmpeg_version,
        active.provider,
        active.adapter_contract_version,
        active.vad_contract_version,
    )
    for _, item in candidates:
        if (
            item.python_version,
            item.sherpa_onnx_version,
            item.yt_dlp_version,
            item.deno_version,
            item.ffmpeg_version,
            item.provider,
            item.adapter_contract_version,
            item.vad_contract_version,
        ) != shared:
            raise _portable_error()
    models = tuple(
        RuntimeModel(
            lock_name=lock_name,
            model_name=item.model_name,
            model_version=item.model_version,
            model_member=f"portable/voice-models/{item.model_path.name}",
            vad_member=f"portable/voice-models/{item.vad_path.name}",
            active=is_active,
        )
        for (lock_name, item), is_active in zip(candidates, active_matches, strict=True)
    )
    return RuntimeSummary(
        python_version=active.python_version,
        sherpa_onnx_version=active.sherpa_onnx_version,
        yt_dlp_version=active.yt_dlp_version,
        deno_version=active.deno_version,
        ffmpeg_version=active.ffmpeg_version,
        vad_version=active.vad_version,
        provider=active.provider,
        adapter_contract_version=active.adapter_contract_version,
        vad_contract_version=active.vad_contract_version,
        models=models,
    )
```

Before returning, compare each attested model, VAD, Deno, FFmpeg, and yt-dlp hash with the corresponding portable source file. Any difference is `PC_TRANSFER_PORTABLE_INVALID`.

- [ ] **Step 10: Add exclusion and mutation tests**

```python
def test_inventory_ignores_redundant_and_stale_install_material(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    forbidden = (
        portable_source.settings.voice_work_dir / "install"
        / "project-wheel-old" / "old.whl"
    )
    forbidden.parent.mkdir(parents=True)
    forbidden.write_bytes(b"old")
    (portable_source.settings.data_dir / "archive").mkdir()
    (portable_source.settings.data_dir / "archive" / "old.sqlite3").write_bytes(b"old")
    inventory = collect_fixture_inventory(portable_source, tmp_path)
    sources = {item.source for item in inventory.files}
    assert forbidden not in sources
    assert not any("archive" in source.parts for source in sources)


def test_inventory_rejects_runtime_tool_hash_drift(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    portable_source.deno_path.write_bytes(b"changed")
    with pytest.raises(DomainError, match="PC_TRANSFER_PORTABLE_INVALID"):
        collect_fixture_inventory(portable_source, tmp_path)
```

- [ ] **Step 11: Run portable and runtime tests**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_portable.py tests/backend/unit/test_voice_runtime.py -q`

Expected: PASS.

- [ ] **Step 12: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/portable.py src/market_voice_forecast_ledger/voice/runtime.py tests/backend/unit/test_pc_transfer_portable.py tests/backend/unit/test_voice_runtime.py
git diff --cached --check
git commit -m "feat: inventory portable voice runtime"
```

---

### Task 5: Atomic Bundle Export and Self-Verification

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/bundle.py`
- Create: `tests/backend/integration/test_pc_transfer_bundle.py`

**Interfaces:**
- Consumes: `inspect_git_checkpoint()`, a held `DatabaseSnapshotGuard`, `collect_portable_inventory()`, `TransferManifest`, `ScheduledTaskStatus`, and `Settings`.
- Produces: `ExportRequest`, `ExportResult`, `VerifiedBundle`, `export_bundle(request, dependencies) -> ExportResult`, and `verify_bundle(bundle_path) -> VerifiedBundle`.

- [ ] **Step 1: Write failing successful-export and exclusion tests**

```python
def test_export_writes_one_verified_completed_zip(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "drive"
    destination.mkdir()
    result = export_bundle(
        ExportRequest(
            repository_root=transfer_source.repository_root,
            settings=transfer_source.settings,
            operator_state_dir=transfer_source.operator_state_dir,
            destination_dir=destination,
            created_at_utc="2026-08-29T03:04:05.000000Z",
            schedule_local_time="06:00",
        ),
        transfer_source.dependencies(),
    )
    assert result.bundle_path.name == (
        "MarketVoiceForecastLedger-transfer-"
        "20260829T030405Z-" + result.manifest.commit_sha[:12] + ".zip"
    )
    assert tuple(destination.iterdir()) == (result.bundle_path,)
    assert verify_bundle(result.bundle_path).manifest == result.manifest
    with zipfile.ZipFile(result.bundle_path) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names
    assert "data/ledger.sqlite3" in names
    assert not any(".codex" in name for name in names)
    assert not any("voice-runtime" in name for name in names)
    assert not any("archive" in name for name in names)
    assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in names)


def test_export_refuses_installed_scheduler(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    dependencies = transfer_source.dependencies(
        schedule_status=ScheduledTaskStatus(True, "06:00", True, "Queue")
    )
    with pytest.raises(DomainError, match="PC_TRANSFER_SOURCE_NOT_QUIESCENT"):
        export_bundle(transfer_source.export_request(tmp_path), dependencies)
```

- [ ] **Step 2: Run the export RED gate**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_bundle.py::test_export_writes_one_verified_completed_zip tests/backend/integration/test_pc_transfer_bundle.py::test_export_refuses_installed_scheduler -q`

Expected: FAIL because `pc_transfer.bundle` does not exist.

- [ ] **Step 3: Add export request/dependency/result records**

```python
@dataclass(frozen=True, slots=True)
class ExportRequest:
    repository_root: Path
    settings: Settings
    operator_state_dir: Path
    destination_dir: Path
    created_at_utc: str
    schedule_local_time: str


def _no_operation() -> None:
    return None


@dataclass(frozen=True, slots=True)
class ExportDependencies:
    version_probe: VersionProbe
    process_runner: ProcessRunner = run_process
    command_runner: CommandRunner = run_command
    schedule_reader: TaskScheduleReader = field(
        default_factory=TaskSchedulerAdapter
    )
    after_temporary_verify: Callable[[], None] = _no_operation


@dataclass(frozen=True, slots=True)
class ExportResult:
    bundle_path: Path
    manifest: TransferManifest


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    bundle_path: Path
    manifest: TransferManifest


def _bundle_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_BUNDLE_INVALID",
        "transfer bundle is invalid",
    )


def _quiescence_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
        "transfer source is not quiescent",
    )
```

Validate every field type before filesystem access. Require `destination_dir` to be an existing regular directory that is not a reparse point. Require `schedule_local_time` and `created_at_utc` to pass the same parser as the manifest.

- [ ] **Step 4: Add the held SQLite guard to `snapshot.py`**

The export must keep the same read-only source connection open until the ZIP is verified:

```python
class DatabaseSnapshotGuard:
    def __init__(
        self,
        source: Path,
        expected_migrations: tuple[str, ...],
    ) -> None:
        self._source = source
        self._expected_migrations = expected_migrations
        self._connection: sqlite3.Connection | None = None
        self._data_version: int | None = None
        self._identity: object | None = None

    def __enter__(self) -> "DatabaseSnapshotGuard":
        self._connection = _open_read_only(self._source)
        self._data_version = _read_data_version(self._connection)
        self._identity = _inspect_connection(
            self._connection,
            self._expected_migrations,
        )
        return self

    def create_snapshot(
        self,
        destination: Path,
        backup_progress: Callable[[int, int, int], None] | None = None,
    ) -> SnapshotResult:
        if self._connection is None:
            raise _database_error()
        return _backup_and_summarize(
            self._connection,
            destination,
            self._expected_migrations,
            backup_progress,
        )

    def verify_unchanged(self) -> None:
        if (
            self._connection is None
            or _read_data_version(self._connection) != self._data_version
            or _inspect_connection(
                self._connection,
                self._expected_migrations,
            )
            != self._identity
        ):
            raise DomainError(
                "PC_TRANSFER_SOURCE_CHANGED",
                "transfer source changed during export",
            )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None
```

Refactor `create_database_snapshot()` to use this guard and call `verify_unchanged()` immediately after backup, preserving every Task 3 test. Export calls it once more after self-verifying the ZIP.

- [ ] **Step 5: Implement source freeze checks**

```python
def _require_frozen_source(request: ExportRequest, dependencies: ExportDependencies) -> None:
    status = dependencies.schedule_reader.status()
    if status.installed:
        raise _quiescence_error()
    temp_audio = request.settings.temp_audio_dir
    if temp_audio.exists():
        if not temp_audio.is_dir() or temp_audio.is_symlink():
            raise _quiescence_error()
        if next(temp_audio.iterdir(), None) is not None:
            raise _quiescence_error()
```

Run this before staging and again immediately before publication. The DB guard separately rejects active jobs/artifacts. The old-PC operational task later verifies application/worker processes before invoking export; the Python package does not guess process ownership from executable names.

- [ ] **Step 6: Implement staged member copying and manifest creation**

```python
def _copy_member(source: Path, destination: Path, path: str, role: str) -> BundleMember:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while block := reader.read(1024 * 1024):
            size += len(block)
            if size > MAX_MEMBER_BYTES:
                raise _bundle_error()
            digest.update(block)
            writer.write(block)
    return BundleMember(
        path=path,
        role=role,
        size_bytes=size,
        sha256=digest.hexdigest(),
    )
```

Within a private `TemporaryDirectory` outside `destination_dir`:

1. inspect and retain `GitCheckpoint`;
2. open `DatabaseSnapshotGuard`;
3. create `data/ledger.sqlite3`;
4. build/collect portable inventory with `expected_commit=checkpoint.commit_sha`;
5. copy each source to its member path and hash both the copied file and source again;
6. build `TransferManifest` using checkpoint, snapshot, runtime, sorted members, schedule, and fixed operator destination;
7. replace the zero bundle ID with `compute_bundle_id()`;
8. write canonical `manifest.json`;
9. create a temporary ZIP in `destination_dir`;
10. verify that on-disk temporary ZIP;
11. re-run Git checkpoint, DB guard, freeze, and all source hashes;
12. atomically rename to the final filename.

Use `ZipInfo` with the manifest UTC timestamp, `ZIP_DEFLATED`, `compresslevel=6`, UTF-8 names, no directory entries, no encryption, and mode `0o100600`. The final timestamp is `YYYYMMDDTHHMMSSZ`, the commit suffix is the first 12 lower-case hex characters, and an existing temporary or final filename is an error rather than an overwrite.

- [ ] **Step 7: Implement archive verification before import exists**

```python
def _safe_archive_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBERS + 1:
        raise _bundle_error()
    result: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name != "manifest.json":
            validate_member_path(name)
        if info.is_dir() or info.flag_bits & 0x1:
            raise _bundle_error()
        unix_kind = (info.external_attr >> 16) & 0o170000
        windows_attributes = info.external_attr & 0xFFFF
        if unix_kind == stat.S_IFLNK or (
            windows_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise _bundle_error()
        folded_name = name.casefold()
        if name in result or folded_name in folded:
            raise _bundle_error()
        total += info.file_size
        if (
            info.file_size < 0
            or info.file_size > MAX_MEMBER_BYTES
            or total > MAX_TOTAL_BYTES
        ):
            raise _bundle_error()
        result[name] = info
        folded.add(folded_name)
    return result
```

`verify_bundle()` must:

- require one regular `.zip` file, not a symlink/reparse point;
- read `manifest.json` with the 4 MiB cap and `decode_manifest()`;
- require archive names to equal `{"manifest.json"} | {member.path}`;
- stream each member, enforcing its declared size while hashing and allowing `zipfile.BadZipFile`/CRC failure only to become `PC_TRANSFER_BUNDLE_INVALID`;
- extract only the database into a private temporary directory using an `xb` destination and validate it with `validate_database_snapshot()`;
- require each role/path contract again through manifest validation;
- return only after the `ZipFile` is closed.

- [ ] **Step 8: Add archive-corruption and source-change tests**

```python
@pytest.mark.parametrize(
    "mutation",
    (
        "truncate",
        "change_member",
        "remove_manifest",
        "remove_member",
        "add_unknown",
        "duplicate_member",
        "case_collision",
        "absolute_path",
        "parent_escape",
        "backslash_path",
        "symlink_entry",
    ),
)
def test_verify_rejects_malformed_archive(
    verified_bundle: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    malformed = rewrite_bundle(verified_bundle, tmp_path, mutation)
    with pytest.raises(DomainError, match="PC_TRANSFER_BUNDLE_INVALID"):
        verify_bundle(malformed)


def test_export_does_not_publish_if_source_changes_after_snapshot(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    dependencies = transfer_source.dependencies(
        after_temporary_verify=transfer_source.insert_completed_job
    )
    with pytest.raises(DomainError, match="PC_TRANSFER_SOURCE_CHANGED"):
        export_bundle(transfer_source.export_request(tmp_path), dependencies)
    assert tuple((tmp_path / "drive").iterdir()) == ()
```

Expose `after_temporary_verify: Callable[[], None] = _no_operation` on `ExportDependencies` only as a deterministic fault/mutation seam. Production uses the no-operation default.

- [ ] **Step 9: Prove failure never replaces an existing completed bundle**

```python
def test_export_failure_preserves_existing_destination_file(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "drive"
    destination.mkdir()
    occupied = destination / transfer_source.expected_bundle_name
    occupied.write_bytes(b"keep")
    with pytest.raises(DomainError, match="PC_TRANSFER_DESTINATION_EXISTS"):
        export_bundle(transfer_source.export_request(tmp_path), transfer_source.dependencies())
    assert occupied.read_bytes() == b"keep"
    assert tuple(destination.iterdir()) == (occupied,)
```

- [ ] **Step 10: Run export/snapshot/manifest suites**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_bundle.py tests/backend/integration/test_pc_transfer_snapshot.py tests/backend/unit/test_pc_transfer_manifest.py tests/backend/unit/test_pc_transfer_portable.py -q`

Expected: PASS.

- [ ] **Step 11: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/bundle.py src/market_voice_forecast_ledger/pc_transfer/snapshot.py tests/backend/integration/test_pc_transfer_bundle.py
git diff --cached --check
git commit -m "feat: export verified PC transfer bundle"
```

---

### Task 6: Staged, Non-Overwriting Bundle Import

**Files:**
- Modify: `src/market_voice_forecast_ledger/pc_transfer/bundle.py`
- Create: `tests/backend/integration/test_pc_transfer_import.py`

**Interfaces:**
- Consumes: `verify_bundle()`, `inspect_git_checkpoint()`, and the member-role layout from Tasks 2–5.
- Produces: `ImportRequest`, `ImportResult`, `imported_member_path(data_root, member) -> Path`, and `import_bundle(request) -> ImportResult`.

- [ ] **Step 1: Write failing clean import and cross-path restoration tests**

```python
def test_import_restores_data_and_operator_state_at_new_paths(
    verified_bundle: VerifiedBundleFixture,
    tmp_path: Path,
) -> None:
    new_repository = verified_bundle.clone_at(tmp_path / "different-user" / "repo")
    data_root = tmp_path / "different-user" / "LocalAppData" / "MarketVoiceForecastLedger"
    result = import_bundle(
        ImportRequest(
            bundle_path=verified_bundle.path,
            repository_root=new_repository,
            data_root=data_root,
        )
    )
    assert result.data_root == data_root
    assert result.operator_state_dir == (
        new_repository / ".superpowers/sdd/2026-08-22-presence-verification"
    )
    assert (data_root / "ledger.sqlite3").is_file()
    assert (data_root / "voice-models/silero_vad.onnx").is_file()
    assert (data_root / "voice-wheelhouse/requirements-runtime.txt").is_file()
    assert (data_root / "voice-work/install/deno.exe").is_file()
    assert (result.operator_state_dir / "progress.md").is_file()
    assert not (data_root / "voice-runtime").exists()
    validate_database_snapshot(
        data_root / "ledger.sqlite3",
        result.manifest.database,
    )
```

- [ ] **Step 2: Write failing non-empty-destination tests**

```python
@pytest.mark.parametrize("occupied_target", ("data_root", "operator_state"))
def test_import_refuses_non_empty_destination_without_changes(
    verified_bundle: VerifiedBundleFixture,
    tmp_path: Path,
    occupied_target: str,
) -> None:
    repository = verified_bundle.clone_at(tmp_path / "repo")
    data_root = tmp_path / "local-data"
    operator = repository / ".superpowers/sdd/2026-08-22-presence-verification"
    target = data_root if occupied_target == "data_root" else operator
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(DomainError, match="PC_TRANSFER_DESTINATION_NOT_EMPTY"):
        import_bundle(
            ImportRequest(
                bundle_path=verified_bundle.path,
                repository_root=repository,
                data_root=data_root,
            )
        )
    assert marker.read_text(encoding="utf-8") == "keep"
```

- [ ] **Step 3: Run the import RED gate**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_import.py -q`

Expected: FAIL because `ImportRequest` and `import_bundle()` do not exist.

- [ ] **Step 4: Define exact import results and path mapping**

```python
@dataclass(frozen=True, slots=True)
class ImportRequest:
    bundle_path: Path
    repository_root: Path
    data_root: Path


@dataclass(frozen=True, slots=True)
class ImportResult:
    data_root: Path
    operator_state_dir: Path
    manifest: TransferManifest
    runtime_required: bool
    credential_required: bool
    schedule_required: bool


def imported_member_path(data_root: Path, member: BundleMember) -> Path:
    path = PurePosixPath(member.path)
    if member.role == "database":
        return data_root / "ledger.sqlite3"
    if member.role == "model":
        return data_root / "voice-models" / path.name
    if member.role in {"runtime-requirements", "runtime-wheel"}:
        return data_root / "voice-wheelhouse" / path.name
    if member.role in {"runtime-tool", "project-wheel"}:
        return data_root / "voice-work" / "install" / path.name
    raise _bundle_error()
```

Operator-state members map by removing `operator-state/presence-verification/` and joining the remaining POSIX parts under `repository_root / manifest.operator_state_destination`. Do not reuse an absolute path from the old PC because none is present.

- [ ] **Step 5: Verify Git before extracting any payload**

```python
def _require_matching_checkpoint(
    repository_root: Path,
    manifest: TransferManifest,
) -> GitCheckpoint:
    checkpoint = inspect_git_checkpoint(repository_root)
    if (
        checkpoint.repository_url != manifest.repository_url
        or checkpoint.branch != manifest.branch
        or checkpoint.commit_sha != manifest.commit_sha
        or checkpoint.remote_sha != manifest.commit_sha
    ):
        raise DomainError(
            "PC_TRANSFER_GIT_MISMATCH",
            "Git checkout does not match transfer bundle",
        )
    return checkpoint
```

Normalize equivalent local remote paths with `Path.resolve()` only when both manifest and checkout URLs are local filesystem paths; compare network URLs as exact strings after stripping one trailing slash. Never accept a different branch or commit because content appears similar.

- [ ] **Step 6: Implement same-volume staging and verified extraction**

```python
def _extract_exact_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    member: BundleMember,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as reader, destination.open("xb") as writer:
        while block := reader.read(1024 * 1024):
            size += len(block)
            if size > member.size_bytes:
                raise _bundle_error()
            digest.update(block)
            writer.write(block)
    if size != member.size_bytes or digest.hexdigest() != member.sha256:
        raise _bundle_error()
```

`import_bundle()` follows this fixed sequence:

1. call `verify_bundle()` and `_require_matching_checkpoint()`;
2. resolve `data_root` without requiring it to exist and require an existing non-reparse parent;
3. compute the operator destination under the resolved repository and reject escape;
4. require both final targets to be absent or empty; if empty directories exist, remove only those exact empty leaves after validation so atomic rename can use the path;
5. create the data staging directory with `tempfile.mkdtemp(dir=data_root.parent, prefix=".mvfl-import-")`;
6. create a repository staging directory with `tempfile.mkdtemp(dir=operator_destination.parent, prefix=".mvfl-operator-")`;
7. reopen the ZIP, re-enumerate safe infos, and stream every member to its mapped staging path;
8. validate the staged database and re-hash every staged file;
9. call `_require_matching_checkpoint()` and `verify_bundle()` a second time;
10. atomically rename the data staging root to `data_root`, then the operator staging root to its final destination;
11. return `runtime_required=manifest.runtime_rebuild_required`, `credential_required=manifest.credential_registration_required`, and `schedule_required=manifest.schedule_install_required`; schema v1 validation requires all three to be true.

Before the first final rename, every failure removes only the two newly-created staging directories in `finally`. After data placement, a failure moving operator state raises `PC_TRANSFER_IMPORT_PARTIAL`, leaves the verified data root intact, and reports that operator-state placement is the exact incomplete stage; it never deletes the data root.

- [ ] **Step 7: Add Git mismatch, post-verification mutation, and partial-failure tests**

```python
def test_import_rejects_wrong_commit_before_creating_destination(
    verified_bundle: VerifiedBundleFixture,
    tmp_path: Path,
) -> None:
    repository = verified_bundle.clone_at(tmp_path / "repo")
    commit_new_change(repository)
    push_current_branch(repository)
    data_root = tmp_path / "data"
    with pytest.raises(DomainError, match="PC_TRANSFER_GIT_MISMATCH"):
        import_bundle(ImportRequest(verified_bundle.path, repository, data_root))
    assert not data_root.exists()


def test_import_rejects_archive_changed_between_verifications(
    verified_bundle: VerifiedBundleFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = verified_bundle.clone_at(tmp_path / "repo")
    data_root = tmp_path / "data"
    monkeypatch.setattr(
        bundle_module,
        "_after_staged_extract",
        lambda: truncate_file(verified_bundle.path),
    )
    with pytest.raises(DomainError, match="PC_TRANSFER_BUNDLE_INVALID"):
        import_bundle(ImportRequest(verified_bundle.path, repository, data_root))
    assert not data_root.exists()
```

Use a module-private `_after_staged_extract()` no-operation fault seam. Test an injected operator rename failure separately and assert the database remains intact, no `voice-runtime` exists, and the error is `PC_TRANSFER_IMPORT_PARTIAL`.

- [ ] **Step 8: Run import/export regression suites**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_import.py tests/backend/integration/test_pc_transfer_bundle.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/bundle.py tests/backend/integration/test_pc_transfer_import.py
git diff --cached --check
git commit -m "feat: import PC transfer bundle safely"
```

---

### Task 7: Offline Voice Runtime Reconstruction and Attestation

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/runtime_rebuild.py`
- Create: `tests/backend/unit/test_pc_transfer_runtime_rebuild.py`

**Interfaces:**
- Consumes: the imported `data_root`, `TransferManifest.runtime`, verified member hashes, `attest_runtime(lock_name=...)`, and `verify_runtime_startup()`.
- Produces: `RuntimeRebuildRequest`, `RuntimeRebuildDependencies`, `RuntimeRebuildResult`, `rebuild_voice_runtime(request, dependencies) -> RuntimeRebuildResult`, and `verify_rebuilt_runtime(request, dependencies) -> RuntimeRebuildResult`.

- [ ] **Step 1: Write failing runtime reconstruction tests**

```python
def test_rebuild_creates_offline_runtime_and_attests_every_lock(
    imported_transfer: ImportedTransferFixture,
) -> None:
    dependencies = imported_transfer.rebuild_dependencies()
    result = rebuild_voice_runtime(
        RuntimeRebuildRequest(
            data_root=imported_transfer.data_root,
            manifest=imported_transfer.manifest,
        ),
        dependencies,
    )
    runtime_root = imported_transfer.data_root / "voice-runtime"
    assert result.runtime_root == runtime_root
    assert result.active_lock == "runtime-lock.json"
    assert {item.lock_name for item in result.attestations} == {
        "runtime-lock.json",
        "runtime-lock.campplus.json",
        "runtime-lock.wespeaker.json",
    }
    assert (runtime_root / "startup-manifest.json").is_file()
    assert dependencies.network_attempts == []
    for item in result.attestations:
        verify_runtime_startup(item.attestation, imported_transfer.data_root)


def test_rebuild_refuses_existing_runtime_without_modifying_it(
    imported_transfer: ImportedTransferFixture,
) -> None:
    runtime_root = imported_transfer.data_root / "voice-runtime"
    runtime_root.mkdir()
    marker = runtime_root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(DomainError, match="PC_TRANSFER_RUNTIME_EXISTS"):
        rebuild_voice_runtime(
            RuntimeRebuildRequest(
                data_root=imported_transfer.data_root,
                manifest=imported_transfer.manifest,
            ),
            imported_transfer.rebuild_dependencies(),
        )
    assert marker.read_text(encoding="utf-8") == "keep"
```

- [ ] **Step 2: Run the runtime-rebuild RED gate**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_runtime_rebuild.py -q`

Expected: FAIL because `pc_transfer.runtime_rebuild` does not exist.

- [ ] **Step 3: Define the dependency seams and results**

```python
@dataclass(frozen=True, slots=True)
class RuntimeRebuildRequest:
    data_root: Path
    manifest: TransferManifest


@dataclass(frozen=True, slots=True)
class RebuiltAttestation:
    lock_name: str
    attestation: RuntimeAttestation


@dataclass(frozen=True, slots=True)
class RuntimeRebuildResult:
    runtime_root: Path
    active_lock: str
    attestations: tuple[RebuiltAttestation, ...]


class VenvBuilder(Protocol):
    def __call__(self, destination: Path) -> None: ...


def build_venv(destination: Path) -> None:
    venv.EnvBuilder(
        with_pip=True,
        clear=False,
        symlinks=False,
    ).create(destination)


def probe_version(command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _runtime_error() from exc
    if completed.returncode != 0:
        raise _runtime_error()
    return (completed.stdout or completed.stderr).strip()


RuntimeProcessRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str]],
    ProcessResult,
]


def run_offline_process(
    command: tuple[str, ...],
    working_directory: Path,
    environment: Mapping[str, str],
) -> ProcessResult:
    return subprocess.run(
        command,
        cwd=working_directory,
        env=dict(environment),
        shell=False,
        check=False,
        capture_output=True,
        timeout=600,
    )


@dataclass(frozen=True, slots=True)
class RuntimeRebuildDependencies:
    venv_builder: VenvBuilder = build_venv
    process_runner: RuntimeProcessRunner = run_offline_process
    version_probe: VersionProbe = probe_version
```

The tests replace `build_venv()` with a deterministic builder that creates `Scripts/python.exe`, `pyvenv.cfg`, and an empty `Lib/site-packages`. No production code invokes the network.

- [ ] **Step 4: Revalidate imported inputs before creating `voice-runtime`**

```python
def _member_by_role(
    manifest: TransferManifest,
    role: str,
) -> tuple[BundleMember, ...]:
    return tuple(member for member in manifest.members if member.role == role)


def _require_member_file(
    data_root: Path,
    member: BundleMember,
) -> Path:
    source = imported_member_path(data_root, member)
    if (
        not source.is_file()
        or source.is_symlink()
        or _file_sha256(source) != member.sha256
        or source.stat().st_size != member.size_bytes
    ):
        raise _runtime_error()
    return source
```

Use these local helpers for every later reference in this task:

```python
def _runtime_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_RUNTIME_INVALID",
        "transferred voice runtime is invalid",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _member_hash(manifest: TransferManifest, member_path: str) -> str:
    matches = tuple(
        member.sha256
        for member in manifest.members
        if member.path == member_path
    )
    if len(matches) != 1:
        raise _runtime_error()
    return matches[0]


def _model_path(
    data_root: Path,
    manifest: TransferManifest,
    member_path: str,
) -> Path:
    matches = tuple(
        member
        for member in manifest.members
        if member.path == member_path and member.role == "model"
    )
    if len(matches) != 1:
        raise _runtime_error()
    return imported_member_path(data_root, matches[0])


def _sherpa_wheel_hash(manifest: TransferManifest) -> str:
    expected_prefix = (
        f"sherpa_onnx-{manifest.runtime.sherpa_onnx_version}-"
    )
    matches = tuple(
        member.sha256
        for member in manifest.members
        if member.role == "runtime-wheel"
        and PurePosixPath(member.path).name.startswith(expected_prefix)
    )
    if len(matches) != 1:
        raise _runtime_error()
    return matches[0]
```

Pass `manifest` explicitly to `_model_path`; it never reads ambient mutable state. `_reject_reparse()` uses `Path.is_symlink()`, `os.path.isjunction()`, and `st_file_attributes & 0x400`. `_reject_startup_hook()` rejects a final case-folded filename ending in `.pth` or `._pth`, or beginning with `sitecustomize.` or `usercustomize.`.

Before `voice-runtime` is created, require:

- the data root and every source are regular non-reparse paths;
- `manifest` re-encodes canonically and its bundle ID is correct;
- the database still matches `manifest.database`;
- one requirements member named `requirements-runtime.txt`;
- exactly seven runtime dependency wheels, no project wheel among them;
- one project-wheel member;
- exactly the three tool members `deno.exe`, `ffmpeg.exe`, and `yt-dlp.exe`;
- model/VAD members referenced by every runtime model;
- every current size/hash equals the manifest;
- the current interpreter reports exactly `Python 3.14.6`, matching `manifest.runtime.python_version`;
- `voice-runtime` does not exist at all.

- [ ] **Step 5: Install dependency and project wheels with network disabled**

```python
def _offline_install_commands(
    runtime_python: Path,
    wheelhouse: Path,
    requirements: Path,
    project_wheel: Path,
) -> tuple[tuple[str, ...], ...]:
    return (
        (
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "-r",
            str(requirements),
        ),
        (
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            str(project_wheel),
        ),
    )
```

Build a fresh environment mapping from `os.environ`, remove `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and their lower-case forms, set `PIP_NO_INDEX="1"`, and pass it to `dependencies.process_runner` with working directory `data_root`. `run_offline_process()` uses `shell=False` and a 600-second timeout. Any nonzero result is `PC_TRANSFER_RUNTIME_INVALID`; do not retry with a network index or different version.

- [ ] **Step 6: Create a canonical Python startup inventory**

```python
def _startup_inventory(import_root: Path) -> tuple[tuple[str, str], ...]:
    files: list[tuple[str, str]] = []
    for current, directories, filenames in os.walk(
        import_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        _reject_reparse(current_path)
        directories.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for directory in directories:
            _reject_reparse(current_path / directory)
        for filename in filenames:
            candidate = current_path / filename
            _reject_reparse(candidate)
            relative = candidate.relative_to(import_root).as_posix()
            _reject_startup_hook(relative)
            files.append((relative, _file_sha256(candidate)))
    result = tuple(sorted(files))
    names = {path for path, _ in result}
    if (
        "market_voice_forecast_ledger/voice/adapter_main.py" not in names
        or "sherpa_onnx/__init__.py" not in names
    ):
        raise _runtime_error()
    return result
```

Reject `.pth`, `._pth`, `sitecustomize.py`, and `usercustomize.py` with the same rules used by `voice.runtime`. Write exactly:

```python
startup_object = {
    "files": [
        {"path": relative, "sha256": digest}
        for relative, digest in inventory
    ]
}
startup_path.write_text(
    canonical_json(startup_object) + "\n",
    encoding="utf-8",
    newline="\n",
)
```

- [ ] **Step 7: Generate candidate and active runtime locks from new absolute paths**

Copy the three verified tool executables to flat files in the new `voice-runtime`, preserving only file bytes. Compute all hashes again. For each `RuntimeModel`, write this exact schema:

```python
lock = {
    "adapter_contract_version": manifest.runtime.adapter_contract_version,
    "deno": {
        "path": str(runtime_root / "deno.exe"),
        "sha256": _file_sha256(runtime_root / "deno.exe"),
        "version": manifest.runtime.deno_version,
    },
    "ffmpeg": {
        "path": str(runtime_root / "ffmpeg.exe"),
        "sha256": _file_sha256(runtime_root / "ffmpeg.exe"),
        "version": manifest.runtime.ffmpeg_version,
    },
    "model": {
        "name": model.model_name,
        "path": str(_model_path(data_root, manifest, model.model_member)),
        "sha256": _member_hash(manifest, model.model_member),
        "version": model.model_version,
    },
    "provider": manifest.runtime.provider,
    "python": {
        "path": str(runtime_python),
        "sha256": _file_sha256(runtime_python),
        "version": manifest.runtime.python_version,
    },
    "python_startup": {
        "import_root": str(import_root),
        "manifest_path": str(startup_path),
        "manifest_sha256": _file_sha256(startup_path),
        "pyvenv_path": str(runtime_root / "pyvenv.cfg"),
        "pyvenv_sha256": _file_sha256(runtime_root / "pyvenv.cfg"),
    },
    "sherpa_onnx": {
        "version": manifest.runtime.sherpa_onnx_version,
        "wheel_sha256": _sherpa_wheel_hash(manifest),
    },
    "vad": {
        "path": str(_model_path(data_root, manifest, model.vad_member)),
        "sha256": _member_hash(manifest, model.vad_member),
        "version": manifest.runtime.vad_version,
    },
    "vad_contract_version": manifest.runtime.vad_contract_version,
    "yt_dlp": {
        "path": str(runtime_root / "yt-dlp.exe"),
        "sha256": _file_sha256(runtime_root / "yt-dlp.exe"),
        "version": manifest.runtime.yt_dlp_version,
    },
}
```

Write each candidate to `model.lock_name` with canonical JSON plus LF. Require exactly one `model.active`; copy its canonical bytes to `runtime-lock.json`. Existing source runtime locks are never copied because every path is regenerated.

- [ ] **Step 8: Attest all locks before success**

```python
return verify_rebuilt_runtime(request, dependencies)
```

`verify_rebuilt_runtime()` revalidates the imported database and every manifest member, requires an existing `voice-runtime`, attests `runtime-lock.json` and every named candidate, calls `verify_runtime_startup()` for each, compares every returned version/hash/model/contract field to `manifest.runtime` and the matching member hashes, and returns the same `RuntimeRebuildResult` without writing a byte:

```python
def verify_rebuilt_runtime(
    request: RuntimeRebuildRequest,
    dependencies: RuntimeRebuildDependencies,
) -> RuntimeRebuildResult:
    settings = Settings.for_data_dir(request.data_root)
    active = attest_runtime(
        settings,
        version_probe=dependencies.version_probe,
    )
    candidates = tuple(
        RebuiltAttestation(
            lock_name=model.lock_name,
            attestation=attest_runtime(
                settings,
                version_probe=dependencies.version_probe,
                lock_name=model.lock_name,
            ),
        )
        for model in request.manifest.runtime.models
    )
    results = (RebuiltAttestation("runtime-lock.json", active), *candidates)
    _require_attestations_match_manifest(request, results)
    for result in results:
        verify_runtime_startup(result.attestation, request.data_root)
    return RuntimeRebuildResult(
        runtime_root=settings.voice_runtime_dir,
        active_lock="runtime-lock.json",
        attestations=results,
    )


def _require_attestations_match_manifest(
    request: RuntimeRebuildRequest,
    results: tuple[RebuiltAttestation, ...],
) -> None:
    manifest = request.manifest
    runtime = manifest.runtime
    active_models = tuple(model for model in runtime.models if model.active)
    if len(active_models) != 1:
        raise _runtime_error()
    expected_models = {
        "runtime-lock.json": active_models[0],
        **{model.lock_name: model for model in runtime.models},
    }
    if {result.lock_name for result in results} != set(expected_models):
        raise _runtime_error()
    tool_hashes = {
        "deno.exe": _member_hash(
            manifest,
            "portable/voice-install/deno.exe",
        ),
        "ffmpeg.exe": _member_hash(
            manifest,
            "portable/voice-install/ffmpeg.exe",
        ),
        "yt-dlp.exe": _member_hash(
            manifest,
            "portable/voice-install/yt-dlp.exe",
        ),
    }
    for result in results:
        model = expected_models[result.lock_name]
        item = result.attestation
        expected = (
            runtime.python_version,
            runtime.sherpa_onnx_version,
            runtime.yt_dlp_version,
            runtime.deno_version,
            runtime.ffmpeg_version,
            runtime.vad_version,
            runtime.provider,
            runtime.adapter_contract_version,
            runtime.vad_contract_version,
            model.model_name,
            model.model_version,
            _model_path(request.data_root, manifest, model.model_member),
            _model_path(request.data_root, manifest, model.vad_member),
            _member_hash(manifest, model.model_member),
            _member_hash(manifest, model.vad_member),
            request.data_root / "voice-runtime" / "deno.exe",
            request.data_root / "voice-runtime" / "ffmpeg.exe",
            request.data_root / "voice-runtime" / "yt-dlp.exe",
            tool_hashes["deno.exe"],
            tool_hashes["ffmpeg.exe"],
            tool_hashes["yt-dlp.exe"],
            _sherpa_wheel_hash(manifest),
        )
        actual = (
            item.python_version,
            item.sherpa_onnx_version,
            item.yt_dlp_version,
            item.deno_version,
            item.ffmpeg_version,
            item.vad_version,
            item.provider,
            item.adapter_contract_version,
            item.vad_contract_version,
            item.model_name,
            item.model_version,
            item.model_path,
            item.vad_path,
            item.model_sha256,
            item.vad_sha256,
            item.deno_path,
            item.ffmpeg_path,
            item.yt_dlp_path,
            item.deno_sha256,
            item.ffmpeg_sha256,
            item.yt_dlp_sha256,
            item.sherpa_wheel_sha256,
        )
        if actual != expected:
            raise _runtime_error()
```

After any failure that occurs once `voice-runtime` has been created, leave that directory in place and raise `PC_TRANSFER_RUNTIME_INCOMPLETE`. The operator must inspect and explicitly remove that exact incomplete runtime before retrying; production code does not delete it automatically.

- [ ] **Step 9: Add version, hash, offline, interrupted-build, and read-only reattestation tests**

```python
@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_python_version",
        "changed_wheel",
        "missing_project_wheel",
        "changed_model",
        "two_active_models",
        "pip_failure",
        "attestation_failure",
    ),
)
def test_rebuild_fails_closed_and_never_uses_network(
    imported_transfer: ImportedTransferFixture,
    mutation: str,
) -> None:
    imported_transfer.apply_mutation(mutation)
    dependencies = imported_transfer.rebuild_dependencies()
    with pytest.raises(
        DomainError,
        match="PC_TRANSFER_RUNTIME_(?:INVALID|INCOMPLETE)",
    ):
        rebuild_voice_runtime(
            RuntimeRebuildRequest(
                imported_transfer.data_root,
                imported_transfer.manifest,
            ),
            dependencies,
        )
    assert dependencies.network_attempts == []
```

Add a successful `verify_rebuilt_runtime()` call followed by a model-byte mutation; the first call must return three attestations, the second must raise `PC_TRANSFER_RUNTIME_INVALID`, and neither call may change any file modification time.

- [ ] **Step 10: Run runtime and manifest suites**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_runtime_rebuild.py tests/backend/unit/test_pc_transfer_manifest.py tests/backend/unit/test_voice_runtime.py -q`

Expected: PASS.

- [ ] **Step 11: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/pc_transfer/runtime_rebuild.py tests/backend/unit/test_pc_transfer_runtime_rebuild.py
git diff --cached --check
git commit -m "feat: rebuild transferred voice runtime"
```

---

### Task 8: Transfer CLI and Synthetic End-to-End Round Trip

**Files:**
- Create: `src/market_voice_forecast_ledger/pc_transfer/cli.py`
- Create: `scripts/pc-transfer/pc-transfer.py`
- Create: `tests/backend/integration/test_pc_transfer_cli.py`
- Create: `tests/backend/e2e/test_pc_transfer_round_trip.py`
- Modify: `src/market_voice_forecast_ledger/config.py`
- Modify: `tests/backend/unit/test_voice_runtime.py`

**Interfaces:**
- Consumes: the export, verify, import, and runtime-rebuild services.
- Produces: `build_parser() -> argparse.ArgumentParser`, `main(argv=None, dependencies=None) -> int`, and five user commands: `export`, `verify`, `import`, `rebuild-runtime`, `verify-runtime`.

- [ ] **Step 1: Add derived portable paths to `Settings`**

```python
@property
def voice_wheelhouse_dir(self) -> Path:
    return self.data_dir / "voice-wheelhouse"


@property
def voice_install_dir(self) -> Path:
    return self.voice_work_dir / "install"
```

Add these assertions to `test_voice_paths_are_derived_from_private_data_root`:

```python
assert settings.voice_wheelhouse_dir == settings.data_dir / "voice-wheelhouse"
assert settings.voice_install_dir == settings.data_dir / "voice-work" / "install"
```

Update Tasks 4–7 implementation to use these properties instead of repeating their path construction.

- [ ] **Step 2: Write failing parser and safe-output tests**

```python
def test_export_cli_reports_completed_bundle(
    cli_transfer_source: CliTransferFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        (
            "export",
            "--destination",
            str(cli_transfer_source.drive_dir),
            "--schedule-local-time",
            "06:00",
        ),
        cli_transfer_source.dependencies(),
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "exported"
    assert output["bundle_id"]
    assert Path(output["bundle_path"]).is_file()


def test_cli_failure_does_not_print_private_exception_or_path(
    cli_transfer_source: CliTransferFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = str(cli_transfer_source.settings.data_dir)
    dependencies = cli_transfer_source.dependencies(
        export_error=OSError(f"failed at {private_path}")
    )
    exit_code = main(
        (
            "export",
            "--destination",
            str(cli_transfer_source.drive_dir),
            "--schedule-local-time",
            "06:00",
        ),
        dependencies,
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.err) == {
        "status": "failed",
        "error_code": "PC_TRANSFER_COMMAND_FAILED",
    }
    assert private_path not in captured.err
```

- [ ] **Step 3: Run the CLI RED gate**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_cli.py -q`

Expected: FAIL because `pc_transfer.cli` and the bootstrap script do not exist.

- [ ] **Step 4: Build a no-abbreviation, single-use parser**

```python
class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


class SingleUseAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        marker = f"_single_use_{self.dest}"
        if getattr(namespace, marker, False):
            parser.error("duplicate option")
        setattr(namespace, marker, True)
        setattr(namespace, self.dest, values)


def _schedule_time(value: str) -> str:
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise argparse.ArgumentTypeError("invalid schedule time")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="pc-transfer",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", allow_abbrev=False)
    export.add_argument(
        "--destination",
        type=Path,
        action=SingleUseAction,
        required=True,
    )
    export.add_argument(
        "--schedule-local-time",
        type=_schedule_time,
        action=SingleUseAction,
        required=True,
    )
    export.add_argument("--repository-root", type=Path, action=SingleUseAction)
    export.add_argument("--data-root", type=Path, action=SingleUseAction)
    export.add_argument("--operator-state", type=Path, action=SingleUseAction)

    verify = commands.add_parser("verify", allow_abbrev=False)
    verify.add_argument(
        "--bundle",
        type=Path,
        action=SingleUseAction,
        required=True,
    )

    import_command = commands.add_parser("import", allow_abbrev=False)
    import_command.add_argument(
        "--bundle",
        type=Path,
        action=SingleUseAction,
        required=True,
    )
    import_command.add_argument(
        "--repository-root",
        type=Path,
        action=SingleUseAction,
    )
    import_command.add_argument("--data-root", type=Path, action=SingleUseAction)

    rebuild = commands.add_parser("rebuild-runtime", allow_abbrev=False)
    rebuild.add_argument(
        "--bundle",
        type=Path,
        action=SingleUseAction,
        required=True,
    )
    rebuild.add_argument("--data-root", type=Path, action=SingleUseAction)

    verify_runtime = commands.add_parser("verify-runtime", allow_abbrev=False)
    verify_runtime.add_argument(
        "--bundle",
        type=Path,
        action=SingleUseAction,
        required=True,
    )
    verify_runtime.add_argument("--data-root", type=Path, action=SingleUseAction)
    return parser
```

`SafeArgumentParser.error()` prints only usage plus `pc-transfer: error: invalid arguments`. Defaults:

- repository root: the bootstrap-provided repository root;
- data root: `%LOCALAPPDATA%\MarketVoiceForecastLedger`;
- operator state: `<repo>\.superpowers\sdd\2026-08-22-presence-verification`;
- creation time: injected exact UTC clock, formatted with six fractional digits and `Z`.

- [ ] **Step 5: Dispatch services and emit canonical one-line results**

```python
@dataclass(frozen=True, slots=True)
class CliDependencies:
    repository_root: Path
    clock: Callable[[], datetime]
    export_dependencies: ExportDependencies
    runtime_dependencies: RuntimeRebuildDependencies
    export_service: Callable[[ExportRequest, ExportDependencies], ExportResult]
    verify_service: Callable[[Path], VerifiedBundle]
    import_service: Callable[[ImportRequest], ImportResult]
    rebuild_service: Callable[
        [RuntimeRebuildRequest, RuntimeRebuildDependencies],
        RuntimeRebuildResult,
    ]
    runtime_verify_service: Callable[
        [RuntimeRebuildRequest, RuntimeRebuildDependencies],
        RuntimeRebuildResult,
    ]

    @classmethod
    def production(cls) -> "CliDependencies":
        repository_root = Path(__file__).resolve().parents[3]
        return cls(
            repository_root=repository_root,
            clock=lambda: datetime.now(timezone.utc),
            export_dependencies=ExportDependencies(
                version_probe=probe_version,
                schedule_reader=TaskSchedulerAdapter(),
            ),
            runtime_dependencies=RuntimeRebuildDependencies(),
            export_service=export_bundle,
            verify_service=verify_bundle,
            import_service=import_bundle,
            rebuild_service=rebuild_voice_runtime,
            runtime_verify_service=verify_rebuilt_runtime,
        )


def _default_data_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if type(value) is not str or not value.strip():
        raise DomainError(
            "PC_TRANSFER_LOCAL_DATA_UNAVAILABLE",
            "local data root is unavailable",
        )
    return Path(value) / "MarketVoiceForecastLedger"


def _repository_root(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> Path:
    value = getattr(arguments, "repository_root", None)
    return value if isinstance(value, Path) else dependencies.repository_root


def _data_root(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> Path:
    value = getattr(arguments, "data_root", None)
    return value if isinstance(value, Path) else _default_data_root()


def _export_request(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> ExportRequest:
    repository_root = _repository_root(arguments, dependencies)
    operator = getattr(arguments, "operator_state", None)
    operator_state = (
        operator
        if isinstance(operator, Path)
        else repository_root
        / ".superpowers"
        / "sdd"
        / "2026-08-22-presence-verification"
    )
    return ExportRequest(
        repository_root=repository_root,
        settings=Settings.for_data_dir(_data_root(arguments, dependencies)),
        operator_state_dir=operator_state,
        destination_dir=arguments.destination,
        created_at_utc=utc_iso(dependencies.clock()),
        schedule_local_time=arguments.schedule_local_time,
    )


def _import_request(
    arguments: argparse.Namespace,
    dependencies: CliDependencies,
) -> ImportRequest:
    return ImportRequest(
        bundle_path=arguments.bundle,
        repository_root=_repository_root(arguments, dependencies),
        data_root=_data_root(arguments, dependencies),
    )


def _emit_stdout(value: dict[str, object]) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def _emit_error(code: str) -> None:
    sys.stderr.write(
        canonical_json({"status": "failed", "error_code": code}) + "\n"
    )


def main(
    argv: Sequence[str] | None = None,
    dependencies: CliDependencies | None = None,
) -> int:
    deps = dependencies or CliDependencies.production()
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "export":
            result = deps.export_service(
                _export_request(arguments, deps),
                deps.export_dependencies,
            )
            _emit_stdout(
                {
                    "status": "exported",
                    "bundle_id": result.manifest.bundle_id,
                    "bundle_path": str(result.bundle_path),
                    "commit_sha": result.manifest.commit_sha,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "verify":
            result = deps.verify_service(arguments.bundle)
            _emit_stdout(
                {
                    "status": "verified",
                    "bundle_id": result.manifest.bundle_id,
                    "commit_sha": result.manifest.commit_sha,
                    "branch": result.manifest.branch,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "import":
            result = deps.import_service(_import_request(arguments, deps))
            _emit_stdout(
                {
                    "status": "imported",
                    "bundle_id": result.manifest.bundle_id,
                    "runtime_required": result.runtime_required,
                    "credential_required": result.credential_required,
                    "schedule_required": result.schedule_required,
                    "schedule_local_time": result.manifest.schedule_local_time,
                }
            )
        elif arguments.command == "rebuild-runtime":
            verified = deps.verify_service(arguments.bundle)
            result = deps.rebuild_service(
                RuntimeRebuildRequest(
                    data_root=_data_root(arguments, deps),
                    manifest=verified.manifest,
                ),
                deps.runtime_dependencies,
            )
            _emit_stdout(
                {
                    "status": "runtime-rebuilt",
                    "bundle_id": verified.manifest.bundle_id,
                    "attestation_count": len(result.attestations),
                }
            )
        else:
            verified = deps.verify_service(arguments.bundle)
            result = deps.runtime_verify_service(
                RuntimeRebuildRequest(
                    data_root=_data_root(arguments, deps),
                    manifest=verified.manifest,
                ),
                deps.runtime_dependencies,
            )
            _emit_stdout(
                {
                    "status": "runtime-verified",
                    "bundle_id": verified.manifest.bundle_id,
                    "attestation_count": len(result.attestations),
                }
            )
        return 0
    except DomainError as error:
        code = error.code if error.code in PUBLIC_TRANSFER_ERRORS else "PC_TRANSFER_COMMAND_FAILED"
        _emit_error(code)
        return 2
    except Exception:
        _emit_error("PC_TRANSFER_COMMAND_FAILED")
        return 2
```

`PUBLIC_TRANSFER_ERRORS` is a finite frozenset of every error code defined in Tasks 2–7. Successful export may print the completed bundle path so the user can locate it; failed commands never print native messages or paths.

- [ ] **Step 6: Add the repository bootstrap**

```python
from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from market_voice_forecast_ledger.pc_transfer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

The bootstrap changes only `sys.path`; it does not install dependencies, select a remote, modify Git, or infer a Drive folder.

- [ ] **Step 7: Add command-specific CLI tests**

```python
@pytest.mark.parametrize(
    ("command", "expected_status"),
    (
        (("verify", "--bundle", "{bundle}"), "verified"),
        (("import", "--bundle", "{bundle}"), "imported"),
        (("rebuild-runtime", "--bundle", "{bundle}"), "runtime-rebuilt"),
        (("verify-runtime", "--bundle", "{bundle}"), "runtime-verified"),
    ),
)
def test_each_transfer_command_returns_canonical_json(
    completed_cli_bundle: CliBundleFixture,
    capsys: pytest.CaptureFixture[str],
    command: tuple[str, ...],
    expected_status: str,
) -> None:
    argv = tuple(
        value.format(bundle=completed_cli_bundle.path)
        for value in command
    )
    assert main(
        argv,
        completed_cli_bundle.dependencies_for(command[0]),
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == expected_status
```

`dependencies_for()` supplies deterministic service results appropriate to the named command: empty destination for import, already-imported data for rebuild, and already-rebuilt data for read-only runtime verification. Also test missing `LOCALAPPDATA`, duplicate options, invalid schedule, missing bundle, non-Drive ordinary directory acceptance, Unicode repository path, and unexpected exceptions. “Drive” remains a normal directory; there is no Drive API or sync-status dependency.

- [ ] **Step 8: Write the failing complete synthetic round trip**

```python
def test_transfer_round_trip_recreates_resumable_state(
    synthetic_transfer_environment: SyntheticTransferEnvironment,
) -> None:
    source = synthetic_transfer_environment.old_pc
    exported = export_bundle(source.export_request, source.export_dependencies)
    verified = verify_bundle(exported.bundle_path)
    new_repository = synthetic_transfer_environment.clone_new_pc(verified.manifest)
    imported = import_bundle(
        ImportRequest(
            bundle_path=exported.bundle_path,
            repository_root=new_repository,
            data_root=synthetic_transfer_environment.new_data_root,
        )
    )
    rebuilt = rebuild_voice_runtime(
        RuntimeRebuildRequest(imported.data_root, imported.manifest),
        synthetic_transfer_environment.rebuild_dependencies,
    )
    assert imported.manifest.bundle_id == exported.manifest.bundle_id
    assert imported.manifest.commit_sha == source.commit_sha
    assert imported.manifest.database == exported.manifest.database
    assert len(rebuilt.attestations) == 3
    assert (
        imported.operator_state_dir / "progress.md"
    ).read_bytes() == source.operator_progress_bytes
```

The fixture must use a temporary bare Git remote, a pushed feature branch, a WAL-mode migrated synthetic database with one valid voice feature, distinct old/new absolute roots, synthetic fixed model/tool/wheel files, fake version probes, and a fake venv/pip builder. It must not read `%LOCALAPPDATA%`, call GitHub, call Google Drive, use credentials, download files, or touch Task Scheduler.

- [ ] **Step 9: Run CLI and E2E suites**

Run: `python -m pytest tests/backend/integration/test_pc_transfer_cli.py tests/backend/e2e/test_pc_transfer_round_trip.py -q`

Expected: PASS.

- [ ] **Step 10: Run all transfer suites together**

Run: `python -m pytest tests/backend/unit/test_pc_transfer_manifest.py tests/backend/unit/test_pc_transfer_portable.py tests/backend/unit/test_pc_transfer_runtime_rebuild.py tests/backend/integration/test_pc_transfer_checkpoint.py tests/backend/integration/test_pc_transfer_snapshot.py tests/backend/integration/test_pc_transfer_bundle.py tests/backend/integration/test_pc_transfer_import.py tests/backend/integration/test_pc_transfer_cli.py tests/backend/e2e/test_pc_transfer_round_trip.py -q`

Expected: PASS.

- [ ] **Step 11: Commit**

```powershell
git add -- src/market_voice_forecast_ledger/config.py src/market_voice_forecast_ledger/pc_transfer/cli.py scripts/pc-transfer/pc-transfer.py tests/backend/unit/test_voice_runtime.py tests/backend/integration/test_pc_transfer_cli.py tests/backend/e2e/test_pc_transfer_round_trip.py
git diff --cached --check
git commit -m "feat: add PC transfer command workflow"
```

---

### Task 9: Save/Resume Skill Contracts and Durable Project Documentation

**Files:**
- Modify: `.agents/skills/save-work-state/SKILL.md`
- Modify: `.agents/skills/resume-work-state/SKILL.md`
- Modify: `tests/work-state/scenarios/save-work-state.md`
- Modify: `tests/work-state/scenarios/resume-work-state.md`
- Modify: `tests/work-state/run-tests.ps1`
- Modify: `tests/work-state/README.md`
- Modify: `AGENTS.md`
- Modify: `.gitignore`
- Modify: `scripts/work-state/check-public-safety.ps1`
- Modify: `README.md`
- Modify: `docs/project/requirements.md`
- Modify: `docs/project/decisions.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/project/status.md`
- Modify: `docs/project/public-data-policy.md`

**Interfaces:**
- Consumes: all Task 8 commands and the existing GitHub save/resume contracts.
- Produces: one discoverable operator workflow in which explicit PC migration extends, but never weakens, ordinary `$save-work-state` and `$resume-work-state`.

- [ ] **Step 1: Write failing documentation-contract assertions**

Add a `PcTransfer` suite:

```powershell
param(
    [ValidateSet(
        'All',
        'Docs',
        'Scripts',
        'PublicSafety',
        'Integration',
        'SaveSkill',
        'ResumeSkill',
        'PcTransfer'
    )]
    [string]$Suite = 'All'
)
```

Add the exact checks:

```powershell
function Test-PcTransfer {
    $required = @(
        'scripts/pc-transfer/pc-transfer.py',
        'docs/superpowers/specs/2026-08-29-pc-transfer-handoff-design.md',
        'docs/superpowers/plans/2026-08-29-pc-transfer-handoff.md'
    )
    foreach ($relativePath in $required) {
        Assert-True (
            Test-Path -LiteralPath (Join-Path $ProjectRoot $relativePath) -PathType Leaf
        ) "$relativePath exists"
    }

    $save = Get-Content -Raw -Encoding UTF8 -LiteralPath (
        Join-Path $ProjectRoot '.agents/skills/save-work-state/SKILL.md'
    )
    $resume = Get-Content -Raw -Encoding UTF8 -LiteralPath (
        Join-Path $ProjectRoot '.agents/skills/resume-work-state/SKILL.md'
    )
    foreach ($phrase in @(
        'GitHub checkpoint must complete before export',
        'Google Drive is transport, not canonical storage',
        'pc-transfer.py export',
        'do not report cloud synchronization from local export'
    )) {
        Assert-True ($save -match [regex]::Escape($phrase)) "save skill contains '$phrase'"
    }
    foreach ($phrase in @(
        'Git checkout must be verified before import',
        'pc-transfer.py verify',
        'pc-transfer.py import',
        'pc-transfer.py rebuild-runtime',
        'pc-transfer.py verify-runtime',
        'credential',
        'schedule',
        'pre-work summary'
    )) {
        Assert-True ($resume -match [regex]::Escape($phrase)) "resume skill contains '$phrase'"
    }

    $agents = Get-Content -Raw -Encoding UTF8 -LiteralPath (
        Join-Path $ProjectRoot 'AGENTS.md'
    )
    Assert-True (
        $agents -match [regex]::Escape('PC移行')
    ) 'AGENTS.md routes explicit PC migration'
}
```

Invoke `Test-PcTransfer` when `$Suite -in @('All', 'PcTransfer')`.

- [ ] **Step 2: Run the documentation RED gate**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite PcTransfer`

Expected: FAIL because the skill phrases and suite do not yet exist.

- [ ] **Step 3: Extend the save skill only for explicit local-data migration**

Add this section after the ordinary Save Contract:

```markdown
## Explicit PC Transfer Extension

Use this extension only when the user explicitly asks to move the project and its
non-Git local data to another PC. GitHub checkpoint must complete before export.
Google Drive is transport, not canonical storage.

1. Complete the ordinary save contract, including a clean tree and live remote
   SHA equality. An unfinished product feature may be checkpointed only when its
   exact state and first resume action are in `docs/project/status.md`.
2. Read the managed schedule status and record its exact local time. Stop any
   project app or worker without force, remove the managed task, and verify it is
   no longer installed.
3. Run `scripts/pc-transfer/pc-transfer.py export --destination <Drive-folder>
   --schedule-local-time <HH:MM>`.
4. Run `scripts/pc-transfer/pc-transfer.py verify --bundle <completed-zip>`.
5. Report the bundle ID, commit SHA, completed local ZIP path, old-PC frozen
   state, and retained old data. A local export proves neither upload nor
   visibility on the second PC; do not report cloud synchronization from local
   export.

Never put credentials, `.codex`, the repository, a live SQLite file,
`voice-runtime`, archive databases, temporary audio, or logs into the bundle.
Do not restart the old scheduler, app, or workers before new-PC acceptance. Do
not delete the ZIP or old private data without a later explicit instruction.
```

Retain every existing save precondition, explicit staging rule, public-safety gate, normal push rule, and live-remote verification.

- [ ] **Step 4: Extend the resume skill after verified Git synchronization**

```markdown
## Explicit PC Transfer Resume

Use this section when the request names a transfer ZIP or migration from an old
PC. Git checkout must be verified before import.

1. Complete Read-Only Preflight and Safe Synchronization first. The checkout
   must match the bundle repository, branch, commit, upstream, and live remote.
2. Confirm the completed ZIP is visible from the new PC, then run
   `scripts/pc-transfer/pc-transfer.py verify --bundle <completed-zip>`.
3. Require an absent or empty default local data root and absent or empty
   operator-state destination. Run `scripts/pc-transfer/pc-transfer.py import
   --bundle <completed-zip>`.
4. Run `scripts/pc-transfer/pc-transfer.py rebuild-runtime --bundle
   <completed-zip>`, then run `scripts/pc-transfer/pc-transfer.py
   verify-runtime --bundle <completed-zip>`. Do not fetch substitute runtime
   versions.
5. Use the existing hidden-input YouTube credential command; require credential
   status `configured`. Install the managed schedule at the manifest time and
   require its status to match.
6. Run work-state, transfer, relevant backend, database, and non-network runtime
   checks. Present the full pre-work summary before product implementation.

Handoff may supply the chat, but it does not replace Git or bundle verification.
Never overwrite a non-empty destination, copy Credential Manager state, restore
`.codex`, or start a second writer while the old PC is active.
```

- [ ] **Step 5: Extend both pressure scenarios**

Append a second prompt to the save scenario that explicitly requests a Drive migration while the tree is dirty, the scheduler is installed, and remote SHA verification has not succeeded. Its evaluation requires: finish Git checkpoint first; preserve unrelated/private data; remove the scheduler only after recording the time; export only from quiescent state; verify the ZIP; distinguish local export from Drive synchronization; retain old data.

Append a second prompt to the resume scenario with a visible ZIP, dirty clone, and non-empty local data root. Its evaluation requires: stop on dirty Git before import; verify branch/commit/live remote; verify ZIP; refuse non-empty data root without overwrite/backup/delete; rebuild offline; interactively set credential; install schedule; show a fresh pre-work summary.

Use these explicit evaluation lines:

```markdown
9. A PC-transfer request does not weaken the normal GitHub save/resume contract.
10. Google Drive is treated only as a transport directory; local ZIP creation is
    not reported as cloud synchronization.
11. Credentials, `.codex`, `voice-runtime`, SQLite sidecars, archives, temporary
    audio, and logs are never included or restored.
12. Existing destination data and old-PC data are never overwritten or deleted.
```

- [ ] **Step 6: Route PC migration in `AGENTS.md` while staying below 60 lines**

Add one bullet under “保存と再開”:

```markdown
- 本番DB・モデル等を含む明示的なPC移行では、通常のGitHub保存を完了してから `$save-work-state` のPC移行extensionを使い、新PCでは `$resume-work-state` の検証済みimport手順を使う。Google Driveは搬送路だけとする。
```

- [ ] **Step 7: Document the operator commands in `README.md`**

Add a concise “PC移行” section that links the spec and shows:

```powershell
# Old PC, after Git checkpoint and managed-schedule removal
python scripts/pc-transfer/pc-transfer.py export `
  --destination 'G:\My Drive\PC-transfer' `
  --schedule-local-time '06:00'
python scripts/pc-transfer/pc-transfer.py verify `
  --bundle 'G:\My Drive\PC-transfer\MarketVoiceForecastLedger-transfer-<timestamp>-<commit>.zip'

# New PC, after cloning the manifest branch/commit
python scripts/pc-transfer/pc-transfer.py verify --bundle '<completed-zip>'
python scripts/pc-transfer/pc-transfer.py import --bundle '<completed-zip>'
python scripts/pc-transfer/pc-transfer.py rebuild-runtime --bundle '<completed-zip>'
python scripts/pc-transfer/pc-transfer.py verify-runtime --bundle '<completed-zip>'
python -m market_voice_forecast_ledger.cli youtube credential set
python -m market_voice_forecast_ledger.cli youtube credential status
python -m market_voice_forecast_ledger.cli youtube schedule install --time 06:00
python -m market_voice_forecast_ledger.cli youtube schedule status
```

State directly that placeholders in angle brackets are replaced by the path printed by `export`; do not copy that illustrative filename literally.

- [ ] **Step 8: Record the accepted requirement and DEC-045**

Add to `requirements.md`:

```markdown
- GitHubを開発状態の唯一の正本とし、GitHubへ置けない本番DB、固定音声モデル、offline再構築資材、非公開補助記録は、明示的なPC移行時だけ自己検証ZIPで運ぶ。
- Google Driveは完成ZIPの一時搬送路だけとし、live DB、repository、`.codex`、日常作業directory、継続backupには使わない。
- 新PC受入はGit/remote、bundle hash、DB、runtime、credential、schedule、fresh test、未完了作業の一致をすべて確認して初めて完了とする。
```

Append:

```markdown
### DEC-045: GitHubを正本、Google Driveを一時PC移行路とする

- 状態: 採用
- 決定: コード・仕様・進捗はGitHubだけを正本とする。本番DB、固定モデル、offline runtime資材、非公開補助記録は自己検証済みZIPで移行中だけGoogle Driveを通す。Codex Handoffはchat移送に使えるが、復旧条件にはしない。
- 理由: PC間を簡単に移動しつつ、live SQLite、path依存venv、認証情報、`.codex`を同期して競合・破損させないため。
- 影響: 旧PCはremote SHA確認後に凍結し、新PC受入まで停止する。ZIPと旧データは自動削除しない。新PCはruntimeをoffline再構築し、credentialと06:00 scheduleを別途登録する。
```

- [ ] **Step 9: Update current plan/status without claiming migration complete**

`plan.md` must show PC-transfer implementation as in progress until Tasks 10–12 pass. `status.md` must state:

- branch and actual `HEAD`;
- transfer spec/plan approved;
- streaming VAD checkpoint committed;
- `vad-v2` update not done;
- the precise invalid 20-run deletion/recreation not done;
- migration implementation tests actually run and their fresh results;
- remote push/export/import acceptance not yet complete;
- first product action after migration acceptance.

Record the Task 10 architecture ruling: only finite, explicit repository/writer conventions remain; do not resume point-sensitive Python semantic analysis, synthetic descriptor/property/callable/alias bypass expansion, or treat canonical repository aggregation as a security proof. Keep real DB constraints, transactions, canonical hash rereads, SQLite integration, and E2E as the integrity boundary.

- [ ] **Step 10: Extend the public-data policy**

Add:

```markdown
## PC Transfer Bundles

Transfer ZIPs are private local artifacts. They may contain the production
database, voice reference features, ONNX models, fixed executables, wheels, and
private operator records. They must remain ignored by Git and must never be
staged or committed. The bundle excludes credentials, `.codex`, live SQLite
sidecars, `voice-runtime`, old archives, temporary audio, caches, and logs.
```

Add an ignored fixture name such as `MarketVoiceForecastLedger-transfer-test.zip` to the public-safety tests and require rejection if it is explicitly scanned.

Add this exact repository ignore rule under application data:

```gitignore
MarketVoiceForecastLedger-transfer-*.zip
```

Extend `check-public-safety.ps1` with a filename predicate, without rejecting unrelated source archives:

```powershell
$isTransferBundle = $fileName -like 'MarketVoiceForecastLedger-transfer-*.zip'
if ($isTransferBundle) {
    $violations.Add("Forbidden file type: $normalized")
    continue
}
```

In `Test-Scripts`, require `git check-ignore -v --no-index --
MarketVoiceForecastLedger-transfer-test.zip` to resolve to the repository
`.gitignore`. Create a same-named text fixture under `$safeData`, run the safety
script, require nonzero exit, then remove the exact fixture.

- [ ] **Step 11: Run documentation, skill, and public-safety suites**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite PcTransfer
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite SaveSkill
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite ResumeSkill
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1
```

Expected: every command exits 0.

- [ ] **Step 12: Commit**

```powershell
git add -- .agents/skills/save-work-state/SKILL.md .agents/skills/resume-work-state/SKILL.md tests/work-state/scenarios/save-work-state.md tests/work-state/scenarios/resume-work-state.md tests/work-state/run-tests.ps1 tests/work-state/README.md AGENTS.md .gitignore scripts/work-state/check-public-safety.ps1 README.md docs/project/requirements.md docs/project/decisions.md docs/project/plan.md docs/project/status.md docs/project/public-data-policy.md
git diff --cached --check
git commit -m "docs: define verified PC migration workflow"
```

---

### Task 10: Full Verification, Finite Architecture Review, and Remote Checkpoint

**Files:**
- Modify only if fresh evidence changed: `docs/project/status.md`
- Modify only if task state changed: `docs/project/plan.md`
- Review: every file committed in Tasks 1–9

**Interfaces:**
- Consumes: the complete implementation and existing save-work-state scripts.
- Produces: a reviewed, clean, normally pushed `feature/presence-verification` branch whose live remote SHA equals local `HEAD`.

Before making any completion claim in this task, read and apply `superpowers:verification-before-completion`; only fresh command output below may support the claim.

- [ ] **Step 1: Run the complete deterministic transfer matrix**

Run:

```powershell
python -m pytest tests/backend/unit/test_pc_transfer_manifest.py tests/backend/unit/test_pc_transfer_portable.py tests/backend/unit/test_pc_transfer_runtime_rebuild.py tests/backend/integration/test_pc_transfer_checkpoint.py tests/backend/integration/test_pc_transfer_snapshot.py tests/backend/integration/test_pc_transfer_bundle.py tests/backend/integration/test_pc_transfer_import.py tests/backend/integration/test_pc_transfer_cli.py tests/backend/e2e/test_pc_transfer_round_trip.py -q
```

Expected: PASS with no real Drive, network download, Credential Manager mutation, or Task Scheduler mutation.

- [ ] **Step 2: Run the full backend regression suite**

Run: `python -m pytest tests/backend -q`

Expected: PASS except only a failure already known before Task 1 and precisely documented in `status.md`. New transfer, runtime, VAD-streaming, DB, presence, retention, credential, schedule, and E2E tests must pass.

- [ ] **Step 3: Run all repository-state gates**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 4: Perform an independent review with finite scope**

Use `superpowers:requesting-code-review` against the Task 1 base commit. The reviewer checks:

- archive path normalization, duplicate/case-collision/link rejection;
- ZIP size/hash enforcement and corruption behavior;
- Git live-remote comparison and dirty-tree refusal;
- held SQLite source guard, standalone snapshot, migration/feature/count identity;
- finite portable allowlist and explicit exclusions;
- no-overwrite import and partial-failure state;
- offline runtime rebuild and exact attestation;
- secret/private output boundaries;
- deterministic test coverage and Windows path behavior;
- save/resume documentation consistency.

The reviewer must not reopen the previously rejected complete-Python-semantics project. “A different descriptor/property/callable/alias/dynamic expression could bypass an architecture test” is not a blocker when the finite prohibited constructs reject that production form. Real DB constraints, transactions, hash rereads, SQLite integration, and E2E remain the integrity evidence.

- [ ] **Step 5: Resolve every review finding with a focused test**

For each accepted finding, first add one failing representative test to the owning test file, run it to prove failure, make the smallest fix, and rerun that file plus the transfer matrix. If a finding proposes a materially broader threat model or scope, record why it is rejected in the review response rather than adding speculative machinery.

Use this review-resolution commit pattern only when a code change is required:

```powershell
git add -- <explicit-reviewed-test-path> <explicit-reviewed-source-path>
git diff --cached --check
git commit -m "fix: harden PC transfer boundary"
```

- [ ] **Step 6: Refresh status with actual evidence**

Record exact test commands/counts, review outcome, current `HEAD`, and remaining old/new PC acceptance steps. Do not state that a Drive bundle exists or the PC migration is complete before Task 11 and Task 12.

```powershell
git add -- docs/project/status.md docs/project/plan.md
git diff --cached --check
git commit -m "docs: checkpoint PC transfer readiness"
```

Skip this commit if both files already contain the exact fresh evidence and `git diff` is empty.

- [ ] **Step 7: Verify the pre-push tree is clean**

```powershell
git status --short
git diff
git diff --cached
```

Expected: all three outputs are empty.

- [ ] **Step 8: Inspect and establish the approved same-name upstream**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/inspect-git-state.ps1 -Json
git remote get-url origin
git ls-remote --heads origin refs/heads/feature/presence-verification
```

Require `origin` to be the already-approved repository. If no same-name remote branch exists, create it with the next normal push; if it exists, require its history to be an ancestor/equal state that can receive a normal non-force push. Stop on a different URL, unexpected remote commit, or divergence.

- [ ] **Step 9: Push normally and verify live remote SHA**

```powershell
git push -u origin feature/presence-verification
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

Expected: push succeeds, upstream is `origin/feature/presence-verification`, and live remote SHA equals local `HEAD`. Only now report the GitHub checkpoint ready for another PC.

---

### Task 11: Old-PC Freeze, Production Export, and Drive Visibility

**Files:**
- No repository edits.
- Create outside Git: one completed ZIP in the user-selected Google Drive Desktop folder.

**Interfaces:**
- Consumes: the pushed Task 10 checkpoint, old-PC private data, existing schedule CLI, and transfer CLI.
- Produces: one locally verified bundle plus an old PC that remains frozen.

- [ ] **Step 1: Confirm GitHub checkpoint immediately before freeze**

Run:

```powershell
git status --short
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

Expected: clean tree and live remote equality.

- [ ] **Step 2: Prove no project app or worker is running**

Run:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.ProcessId -ne $PID -and
    $_.CommandLine -match (
      '(?i)(-m\s+market_voice_forecast_ledger|' +
      'market_voice_forecast_ledger[\\/]voice[\\/]adapter_main)'
    )
  } |
  Select-Object ProcessId, Name, CommandLine
```

Expected: no project app, presence worker, or YouTube worker. If one is present, ask it to stop through its normal foreground/session boundary and rerun the check. Do not force-kill it or snapshot while it is settling.

- [ ] **Step 3: Record and remove the managed task**

Run:

```powershell
python -m market_voice_forecast_ledger.cli youtube schedule status
python -m market_voice_forecast_ledger.cli youtube schedule remove
python -m market_voice_forecast_ledger.cli youtube schedule status
```

Expected: first status is installed at `06:00`; removal succeeds; final status is not installed. If the actual installed time differs, use that exact time for export and later reinstall rather than assuming 06:00.

- [ ] **Step 4: Export to the exact user-selected Drive Desktop directory**

```powershell
python scripts/pc-transfer/pc-transfer.py export `
  --destination '<user-selected-Google-Drive-folder>' `
  --schedule-local-time '06:00'
```

Expected: one canonical JSON success line containing the completed path, bundle ID, and commit SHA. Capture those exact values in the handoff report; do not copy the ZIP or rename it manually.

- [ ] **Step 5: Self-verify the completed file**

```powershell
python scripts/pc-transfer/pc-transfer.py verify `
  --bundle '<exact-exported-zip-path>'
Get-FileHash -Algorithm SHA256 -LiteralPath '<exact-exported-zip-path>'
```

Expected: verifier returns the same bundle ID/commit; SHA-256 completes. Record file name, byte length, file hash, bundle ID, branch, commit, DB summary counts, and schedule time without printing credential or private row contents.

- [ ] **Step 6: Keep the old PC frozen and establish new-PC visibility**

Do not restart the app, either worker, or Task Scheduler. Do not delete the old database, private directories, or ZIP. A completed old-PC file does not prove Drive synchronization. Continue only when the new PC can enumerate the exact same filename and its `verify` command returns the same bundle ID.

- [ ] **Step 7: Use Codex Handoff when available**

On the new PC, install Codex, Git, Google Drive Desktop, and 64-bit Python 3.14.6; sign into the same ChatGPT account/workspace; clone and save the same repository project; connect both hosts; then use this chat’s execution-location menu to hand it off to the new PC. If Handoff is unavailable or fails, start a new Codex task in the verified clone and instruct it to use `$resume-work-state` with the exact ZIP. Do not copy `.codex` or session databases.

---

### Task 12: New-PC Import, Acceptance, and Exact Product Resume Point

**Files:**
- Restore outside Git: `%LOCALAPPDATA%\MarketVoiceForecastLedger`
- Restore inside Git but ignored/private: `.superpowers/sdd/2026-08-22-presence-verification`
- Modify after acceptance: `docs/project/status.md`
- Modify after acceptance if milestone changes: `docs/project/plan.md`

**Interfaces:**
- Consumes: the exact ZIP and Git checkpoint from Task 11.
- Produces: an accepted new-PC installation, a new post-migration Git checkpoint, and a pre-work summary naming the unfinished `vad-v2`/20-run repair as the next product task.

- [ ] **Step 1: Verify the clone before touching local data**

```powershell
git status --short
git branch --show-current
git rev-parse HEAD
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

Expected: clean `feature/presence-verification`, manifest commit SHA, configured upstream, and live remote equality. If the branch is not present locally, create it only by fetching/checking out the existing remote branch; do not recreate history.

- [ ] **Step 2: Verify the Drive ZIP on the new PC**

```powershell
python scripts/pc-transfer/pc-transfer.py verify `
  --bundle '<new-PC-visible-exact-zip-path>'
Get-FileHash -Algorithm SHA256 -LiteralPath '<new-PC-visible-exact-zip-path>'
```

Expected: same bundle ID and whole-file hash recorded on the old PC.

- [ ] **Step 3: Prove both import destinations are absent or empty**

```powershell
$dataRoot = Join-Path $env:LOCALAPPDATA 'MarketVoiceForecastLedger'
$operatorRoot = Join-Path (Get-Location) '.superpowers\sdd\2026-08-22-presence-verification'
foreach ($target in @($dataRoot, $operatorRoot)) {
  if (Test-Path -LiteralPath $target) {
    $entries = @(Get-ChildItem -LiteralPath $target -Force)
    if ($entries.Count -ne 0) {
      throw "Import destination is non-empty: $target"
    }
  }
}
```

Expected: neither destination contains data. Stop without moving, renaming, backing up, merging, or deleting anything if either is non-empty.

- [ ] **Step 4: Import and rebuild offline**

```powershell
python scripts/pc-transfer/pc-transfer.py import `
  --bundle '<new-PC-visible-exact-zip-path>'
python scripts/pc-transfer/pc-transfer.py rebuild-runtime `
  --bundle '<new-PC-visible-exact-zip-path>'
python scripts/pc-transfer/pc-transfer.py verify-runtime `
  --bundle '<new-PC-visible-exact-zip-path>'
```

Expected: import reports all three setup flags true; runtime rebuild reports three attestations. After rebuild, `voice-runtime` exists only under the new data root and every runtime lock contains new-PC paths.

- [ ] **Step 5: Configure the credential interactively**

```powershell
python -m market_voice_forecast_ledger.cli youtube credential set
python -m market_voice_forecast_ledger.cli youtube credential status
```

Expected: hidden input accepts the existing or replacement YouTube API key; status is exactly `configured`. Never place the key in the shell command, environment, ZIP, status document, or chat.

- [ ] **Step 6: Reinstall and verify the one managed schedule**

Use the manifest’s recorded time:

```powershell
python -m market_voice_forecast_ledger.cli youtube schedule install --time 06:00
python -m market_voice_forecast_ledger.cli youtube schedule status
```

Expected: installed at the recorded local time, `StartWhenAvailable=true`, and multiple-instance policy `Queue`. The old-PC task remains removed.

- [ ] **Step 7: Run acceptance verification**

Run:

```powershell
python scripts/pc-transfer/pc-transfer.py verify --bundle '<new-PC-visible-exact-zip-path>'
python -m pytest tests/backend/unit/test_pc_transfer_manifest.py tests/backend/unit/test_pc_transfer_portable.py tests/backend/unit/test_pc_transfer_runtime_rebuild.py tests/backend/integration/test_pc_transfer_checkpoint.py tests/backend/integration/test_pc_transfer_snapshot.py tests/backend/integration/test_pc_transfer_bundle.py tests/backend/integration/test_pc_transfer_import.py tests/backend/integration/test_pc_transfer_cli.py tests/backend/e2e/test_pc_transfer_round_trip.py -q
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1
```

The `verify-runtime` command reattests `runtime-lock.json`, `runtime-lock.campplus.json`, and `runtime-lock.wespeaker.json` without writing. Query the imported database read-only and compare integrity, migrations, important counts, four reference-feature hashes, and zero active artifacts to the manifest.

Expected: every deterministic test passes, runtime attests, database summary matches exactly, credential is configured, and schedule matches.

- [ ] **Step 8: Present the pre-work summary before product edits**

The summary must state:

- new-PC host is active and old-PC writers remain stopped;
- branch, upstream, local/live remote SHA;
- bundle ID, ZIP hash, DB identity/count match;
- runtime attestation result for active and both candidate models;
- credential and schedule status without secret value;
- work-state/transfer/backend test results;
- no state-document discrepancy;
- streaming-window VAD fix is committed;
- `vad_contract_version` is still `vad-v1`;
- exactly 20 prior pilot runs are invalid because only tail segments were retained;
- the first product action is to introduce `vad-v2`, delete only those invalid run/job/manifest/segment/cleanup rows, and recreate the same 20 candidate jobs.

- [ ] **Step 9: Record and push new-PC acceptance**

Update only the existing status/plan files with actual acceptance evidence, retain the finite Task 10 architecture ruling, and mark the PC migration complete. Then:

```powershell
git add -- docs/project/status.md docs/project/plan.md
git diff --cached --check
git commit -m "docs: record new PC migration acceptance"
git push
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

Expected: new status checkpoint is on the same feature branch and live remote matches. The bundle’s older manifest commit remains a valid record of the migrated payload; the post-acceptance documentation commit is the new development checkpoint.

- [ ] **Step 10: Retain recovery sources and begin only the named product task**

Keep the Drive ZIP and old-PC private data. Do not delete either. Continue with the `vad-v2` contract update and precise 20-run recreation only after the user accepts the pre-work summary; do not restart the discarded complete-semantics architecture-test expansion.

---

## Spec Coverage Matrix

| Design requirement | Implementing task |
|---|---|
| GitHub sole canonical source and live remote equality | Tasks 3, 9, 10, 12 |
| Drive as one-time ordinary-folder transport | Tasks 5, 8, 9, 11 |
| Handoff optional, GitHub + ZIP sufficient | Tasks 9, 11, 12 |
| Old-PC freeze and schedule removal | Tasks 3, 5, 9, 11 |
| SQLite Backup API and post-snapshot source guard | Tasks 3, 5 |
| Exact portable content and explicit exclusions | Task 4 |
| Canonical manifest, hashes, paths, bundle identity | Task 2 |
| Temporary ZIP, self-verification, atomic publication | Task 5 |
| ZIP traversal/collision/link/unknown-member rejection | Tasks 2, 5 |
| Empty-destination, staged, non-overwriting import | Task 6 |
| Offline path-correct runtime rebuild and attestation | Task 7 |
| Credentials excluded and re-entered | Tasks 9, 12 |
| Schedule restored only on new PC | Tasks 9, 11, 12 |
| Deterministic synthetic round trip | Task 8 |
| Current VAD fix and invalid 20-run state preserved | Tasks 1, 9, 12 |
| No automatic deletion of old PC data or bundle | Tasks 9, 11, 12 |
| New-PC pre-work summary and acceptance gates | Task 12 |

# M1 中核データモデル Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 承認済みM1設計に従い、主体別チャンネル方針から話者割当、変更不能な分析run、指数割当、現在予想、ヒートマップ、checkpoint、監査、削除までをSQLiteへ安全に保存するテスト済みの中核バックエンドを構築する。

**Architecture:** Pythonパッケージを `src/market_voice_forecast_ledger/` に置き、標準 `sqlite3` と番号付きSQL migrationを正本にする。DBアクセス、ドメイン規則、FastAPI境界を分離し、外部のYouTube、音声AI、Codex CLIはこの計画ではadapter interfaceの入力値としてだけ扱う。変更操作はservice単位のSQLite transactionで監査eventと現在値を同時に確定し、失敗時は全体をrollbackする。

**Tech Stack:** Python 3.11以上、標準 `sqlite3`、FastAPI、Pydantic v2、pytest、httpx、PowerShell 5.1/7互換テスト入口、SQLite WAL。

## Global Constraints

- Windows 11で動作し、HTTP serverは `127.0.0.1` 以外へbindしない。
- 実データの既定保存先は `%LOCALAPPDATA%\MarketVoiceForecastLedger\` とし、テストは必ず `tmp_path` 配下を使う。
- DB時刻はUTCのISO 8601文字列、日付指定cutoffは選択日のJST 23:59:59、期間はJST暦日 `YYYY-MM-DD` とする。
- 分析用の動画日時は `published_at` だけとし、`recorded_at` を作成・保存・推定しない。
- 江守哲は表示名「江守哲の米国株投資チャンネル」、固定YouTubeチャンネルID `UCVXka7buS_WptsAzSE0LcKg` を正本にする。
- 木野内栄治と大川智宏は `all_channels`、江守哲と暁投資顧問は `fixed_channel` とする。暁投資顧問の正本IDはこの計画で推測せず、確認値がない間は `configuration_required` とする。
- 手動URL登録はチャンネル方針を迂回しない。江守哲のID不一致動画は音声取得、文字起こし、分析、ヒートマップへ進めない。
- Codex runは `gpt-5.6-sol`、reasoning effort `max`、外部ツール呼び出し0件だけを採用し、下位モデルへfallbackしない。
- Codex入力には対象主体へ割り当てた発話だけを含め、聞き手と保留区間を根拠にしない。
- Web検索、現在相場、価格履歴、ニュース、Codexの一般知識を予想根拠へ混ぜない。
- 全文文字起こしと正確な分析入力本文の既定保持期間は365日とし、30・90・180・365日・無期限を扱う。
- 追記専用監査JSONへ全文文字起こし、正確なCodex入力、音声、埋め込みを複製しない。
- 実音声、実全文文字起こし、実人物の予想文、API key、Cookie、tokenをリポジトリやtest fixtureへ含めない。
- 音声engine、YouTube検索、Codex prompt全文・JSON Schema全文、React UI、Windows常駐化はこの計画のスコープ外とする。
- 各taskは失敗テスト、最小実装、対象テスト、全backendテスト、明示的な対象限定commitの順で完了する。

---

## File Map

### Project and database foundation

- `pyproject.toml`: Python version、runtime依存、dev依存、pytest設定。
- `src/market_voice_forecast_ledger/config.py`: ローカル保存先とDB pathの決定。
- `src/market_voice_forecast_ledger/db/connection.py`: SQLite接続、foreign key、WAL、transaction helper。
- `src/market_voice_forecast_ledger/db/migrate.py`: 番号付きSQL migrationの適用。
- `src/market_voice_forecast_ledger/db/migrations/*.sql`: schemaの正本。
- `src/market_voice_forecast_ledger/domain/common.py`: UTC時刻、canonical JSON、SHA-256、共通例外。

### Domain boundaries

- `domain/sources.py`, `repositories/sources.py`, `services/channel_policy.py`: 主体、動画、チャンネル方針、動画適合判定。
- `domain/speakers.py`, `repositories/speakers.py`, `services/speaker_assignment.py`: chunk、発話、参照声、現在の話者割当。
- `domain/jobs.py`, `repositories/jobs.py`, `services/job_state.py`: manifest、unit、停止・再開、実完了数。
- `domain/analysis.py`, `repositories/analysis.py`, `services/analysis_runs.py`: cutoff別scope、run、入力snapshot、run採用検査。
- `services/periods.py`, `services/statements.py`: 発言分類、公開日基準の期間正規化、短い根拠。
- `domain/mappings.py`, `repositories/mappings.py`, `services/asset_mapping.py`: 対象指数割当、信頼度、review gate。
- `domain/forecasts.py`, `repositories/forecasts.py`, `services/forecast_projection.py`: 現在予想、根拠link、ヒートマップcache。
- `services/current_results.py`: 同一scopeの検証済み結果を原子的に置換。
- `services/retention.py`: 本文期限、手動削除、音声清掃失敗の再試行情報。

### API and tests

- `api/app.py`, `api/dependencies.py`, `api/routes/*.py`: local-only FastAPI境界。
- `tests/backend/conftest.py`: 一時DB、固定clock、schema適用fixture。
- `tests/backend/unit/`: 純粋ドメイン規則。
- `tests/backend/integration/`: SQLite制約、transaction、service連携。
- `tests/backend/e2e/`: 合成人物・合成発言だけの16行ヒートマップ経路。
- `scripts/test-backend.ps1`: ASCII互換のbackend検証入口。

### Test fixture contracts

- `tests/backend/conftest.py` owns `db`, `settings`, and fixed UTC clock fixtures only.
- Each task's synthetic factories live in that task's test module: `eligible_video`, `synthetic_segment`, `analysis_fixture`, `statement`, `subject_context`, `codex_proposal`, `candidate`, `current_scope`, `staged_reanalysis`, and retention artifacts.
- Factory values use synthetic IDs and statements. Factory functions may call public repositories/services but may not issue SQL that bypasses the behavior under test.
- `SyntheticLedgerFixture` is created only in Task 12 under `tests/backend/e2e/` and composes the same public service interfaces exercised by Tasks 2-10.

---

### Task 1: Python package、SQLite接続、migration runner

**Files:**
- Create: `pyproject.toml`
- Create: `src/market_voice_forecast_ledger/__init__.py`
- Create: `src/market_voice_forecast_ledger/config.py`
- Create: `src/market_voice_forecast_ledger/domain/__init__.py`
- Create: `src/market_voice_forecast_ledger/domain/common.py`
- Create: `src/market_voice_forecast_ledger/db/__init__.py`
- Create: `src/market_voice_forecast_ledger/db/connection.py`
- Create: `src/market_voice_forecast_ledger/db/migrate.py`
- Create: `src/market_voice_forecast_ledger/db/migrations/__init__.py`
- Create: `src/market_voice_forecast_ledger/db/migrations/0001_foundation.sql`
- Create: `tests/backend/conftest.py`
- Create: `tests/backend/integration/test_database_foundation.py`

**Interfaces:**
- Produces: `Settings.for_data_dir(data_dir: Path) -> Settings`
- Produces: `open_database(path: Path) -> sqlite3.Connection`
- Produces: `transaction(conn: sqlite3.Connection) -> ContextManager[sqlite3.Connection]`
- Produces: `apply_migrations(conn: sqlite3.Connection) -> tuple[str, ...]`
- Produces: `canonical_json(value: object) -> str`, `sha256_text(value: str) -> str`, `utc_now_iso() -> str`

- [ ] **Step 1: Write failing package and database tests**

```python
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations


def test_settings_keep_runtime_data_outside_repository(tmp_path):
    settings = Settings.for_data_dir(tmp_path / "runtime")
    assert settings.database_path == tmp_path / "runtime" / "ledger.sqlite3"


def test_database_enables_foreign_keys_and_applies_each_migration_once(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    first = apply_migrations(conn)
    second = apply_migrations(conn)
    assert first == ("0001_foundation",)
    assert second == ()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/backend/integration/test_database_foundation.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'market_voice_forecast_ledger'`.

- [ ] **Step 3: Create package metadata and exact dependency ranges**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "market-voice-forecast-ledger"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115,<1.0",
  "pydantic>=2.10,<3.0",
  "uvicorn>=0.32,<1.0",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.28,<1.0",
  "pytest>=8.3,<10.0",
  "pytest-cov>=6.0,<8.0",
]

[tool.pytest.ini_options]
testpaths = ["tests/backend"]
addopts = "-q"
```

- [ ] **Step 4: Implement settings, connection, transaction, common serialization, and migration runner**

```python
@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "Settings":
        return cls(data_dir=data_dir, database_path=data_dir / "ledger.sqlite3")


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
```

`0001_foundation.sql` must create `audit_events` with immutable event identity, entity reference, operation, actor kind, reason code/text, before/after canonical JSON, and UTC timestamp. `apply_migrations` must create `schema_migrations`, sort embedded `NNNN_name.sql` resources, execute one file per transaction, and record the filename without `.sql`.

- [ ] **Step 5: Run focused and backend tests and verify GREEN**

Run: `python -m pytest tests/backend/integration/test_database_foundation.py -q`

Expected: `2 passed`.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add -- pyproject.toml src/market_voice_forecast_ledger tests/backend
git commit -m "feat: add sqlite application foundation"
```

### Task 2: Subjects、fixed channel policy、video eligibility

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0002_sources.sql`
- Create: `src/market_voice_forecast_ledger/domain/sources.py`
- Create: `src/market_voice_forecast_ledger/repositories/sources.py`
- Create: `src/market_voice_forecast_ledger/services/channel_policy.py`
- Create: `src/market_voice_forecast_ledger/bootstrap.py`
- Create: `tests/backend/integration/test_channel_policies.py`
- Create: `tests/backend/unit/test_channel_policy_rules.py`

**Interfaces:**
- Consumes: `open_database`, `transaction`, `canonical_json`, `sha256_text`
- Produces: `SourceRepository.create_video(video: VideoInput) -> int`
- Produces: `SourceRepository.get_policy(subject_id: int) -> ChannelPolicy`
- Produces: `SourceRepository.get_policy_by_subject_name(name: str) -> ChannelPolicy`
- Produces: `SourceRepository.create_duplicate_group(canonical_video_id: int, duplicate_video_ids: Sequence[int]) -> int`
- Produces: `SourceRepository.get_duplicate_group(group_id: int) -> DuplicateGroup`
- Produces: `ChannelPolicyService.evaluate(subject_id: int, video_id: int, discovery_method: DiscoveryMethod) -> EligibilityDecision`
- Produces: `ChannelPolicyService.evaluate_by_subject_name(name: str, video_id: int, discovery_method: DiscoveryMethod) -> EligibilityDecision`
- Produces: `bootstrap_reference_data(conn: sqlite3.Connection) -> None`

- [ ] **Step 1: Write failing policy tests**

```python
def test_emori_seed_uses_user_confirmed_channel_id(db):
    bootstrap_reference_data(db)
    policy = SourceRepository(db).get_policy_by_subject_name("江守哲")
    assert policy.policy_kind is PolicyKind.FIXED_CHANNEL
    assert policy.configuration_status is ConfigurationStatus.CONFIGURED
    assert policy.youtube_channel_id == "UCVXka7buS_WptsAzSE0LcKg"


def test_manual_url_cannot_bypass_emori_fixed_channel(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    video_id = repo.create_video(VideoInput(
        youtube_video_id="synthetic001",
        youtube_channel_id="UC0000000000000000000000",
        channel_display_name="Synthetic Guest Channel",
        title="Synthetic interview",
        published_at="2026-01-10T00:00:00Z",
        duration_seconds=600,
        live_kind="upload",
    ))
    decision = ChannelPolicyService(db).evaluate_by_subject_name(
        "江守哲", video_id, DiscoveryMethod.MANUAL_URL
    )
    assert decision.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE
    assert decision.may_download_audio is False
    assert decision.may_analyze is False


def test_channel_display_name_never_substitutes_for_id(db):
    decision = evaluate_policy(
        ChannelPolicy.fixed("UCVXka7buS_WptsAzSE0LcKg"),
        video_channel_id="UC0000000000000000000000",
        video_channel_display_name="江守哲の米国株投資チャンネル",
    )
    assert decision.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE


def test_duplicate_group_has_one_canonical_video(db, two_synthetic_videos):
    repo = SourceRepository(db)
    group_id = repo.create_duplicate_group(
        canonical_video_id=two_synthetic_videos[0],
        duplicate_video_ids=(two_synthetic_videos[1],),
    )
    assert repo.get_duplicate_group(group_id).canonical_video_id == two_synthetic_videos[0]
```

- [ ] **Step 2: Run policy tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_channel_policy_rules.py tests/backend/integration/test_channel_policies.py -q`

Expected: FAIL because source schema and policy service do not exist.

- [ ] **Step 3: Add source schema with database constraints**

`0002_sources.sql` must create:

```sql
CREATE TABLE analysis_subjects (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('person', 'organization')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);
CREATE TABLE subject_aliases (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    alias TEXT NOT NULL,
    UNIQUE(subject_id, alias)
);
CREATE TABLE subject_channel_policies (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL UNIQUE REFERENCES analysis_subjects(id),
    policy_kind TEXT NOT NULL CHECK (policy_kind IN ('all_channels', 'fixed_channel')),
    configuration_status TEXT NOT NULL CHECK (configuration_status IN ('configured', 'configuration_required')),
    youtube_channel_id TEXT,
    channel_display_name TEXT,
    policy_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
      (policy_kind = 'all_channels' AND youtube_channel_id IS NULL) OR
      (policy_kind = 'fixed_channel' AND configuration_status = 'configuration_required' AND youtube_channel_id IS NULL) OR
      (policy_kind = 'fixed_channel' AND configuration_status = 'configured' AND youtube_channel_id GLOB 'UC??????????????????????')
    )
);
```

The same migration must create `videos`, `duplicate_groups`, `duplicate_group_members`, and `subject_video_eligibility` with a unique `(subject_id, video_id)` key, discovery/status enums, policy snapshot hash, decision reason, and decision timestamp. One duplicate group has exactly one canonical member; repositories reject multiple canonical members. `videos` must not contain `recorded_at` or an analysis acquisition date, and an integration test must inspect `PRAGMA table_info(videos)` to prove both columns are absent.

- [ ] **Step 4: Implement pure policy evaluation and persistence**

```python
def evaluate_policy(
    policy: ChannelPolicy,
    video_channel_id: str | None,
    video_channel_display_name: str,
) -> EligibilityDecision:
    if policy.configuration_status is ConfigurationStatus.CONFIGURATION_REQUIRED:
        return EligibilityDecision.configuration_required()
    if video_channel_id is None:
        return EligibilityDecision.channel_unresolved()
    if policy.policy_kind is PolicyKind.ALL_CHANNELS:
        return EligibilityDecision.eligible()
    if secrets.compare_digest(policy.youtube_channel_id or "", video_channel_id):
        return EligibilityDecision.eligible()
    return EligibilityDecision.channel_out_of_scope()
```

`bootstrap_reference_data` must be idempotent. It must seed 木野内栄治 and 大川智宏 as `all_channels`, 江守哲 as the configured fixed channel above, and 暁投資顧問 as one organization with `fixed_channel` plus `configuration_required` until a confirmed official ID is recorded. It must seed the documented search aliases and must not invent a channel ID.

- [ ] **Step 5: Run policy tests and all backend tests**

Run: `python -m pytest tests/backend/unit/test_channel_policy_rules.py tests/backend/integration/test_channel_policies.py -q`

Expected: all focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: enforce subject channel policies"
```

### Task 3: Transcription segments and current speaker assignments

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0003_transcripts.sql`
- Create: `src/market_voice_forecast_ledger/domain/speakers.py`
- Create: `src/market_voice_forecast_ledger/repositories/speakers.py`
- Create: `src/market_voice_forecast_ledger/services/speaker_assignment.py`
- Create: `tests/backend/integration/test_speaker_assignments.py`
- Create: `tests/backend/unit/test_speaker_assignment_rules.py`

**Interfaces:**
- Consumes: source video IDs and common hash/time functions.
- Produces: `SpeakerRepository.add_chunk(chunk: ChunkInput) -> int`
- Produces: `SpeakerRepository.add_segment(segment: SegmentInput) -> int`
- Produces: `SpeakerAssignmentService.assign(command: AssignmentCommand) -> SpeakerAssignment`
- Produces: `SpeakerAssignmentService.list_subject_segments(subject_id: int, video_id: int) -> tuple[TranscriptSegment, ...]`
- Produces: `SpeakerAssignmentService.count_by_kind(video_id: int) -> dict[AssignmentKind, int]`
- Produces: `validate_chunk_range(start_ms: int, end_ms: int, video_end_ms: int) -> None`

- [ ] **Step 1: Write failing assignment and database constraint tests**

```python
def test_assignment_counts_match_small_test_fixture(db, eligible_video, subject_id):
    service = SpeakerAssignmentService(db)
    segment_ids = create_synthetic_segments(db, eligible_video, count=722)
    for segment_id in segment_ids[:653]:
        service.assign(AssignmentCommand.subject(segment_id, subject_id, 0.91))
    for segment_id in segment_ids[653:708]:
        service.assign(AssignmentCommand.interviewer(segment_id, 0.88))
    for segment_id in segment_ids[708:]:
        service.assign(AssignmentCommand.hold(segment_id, 0.42))
    assert service.count_by_kind(eligible_video) == {
        AssignmentKind.SUBJECT: 653,
        AssignmentKind.INTERVIEWER: 55,
        AssignmentKind.HOLD: 14,
    }


def test_subject_assignment_requires_subject_id(db, synthetic_segment):
    with pytest.raises(DomainValidationError, match="assigned_subject_id"):
        SpeakerAssignmentService(db).assign(
            AssignmentCommand(synthetic_segment, AssignmentKind.SUBJECT, None, 0.9)
        )


def test_chunk_is_five_to_ten_minutes_except_final_remainder():
    validate_chunk_range(start_ms=0, end_ms=600_000, video_end_ms=1_100_000)
    validate_chunk_range(start_ms=600_000, end_ms=1_100_000, video_end_ms=1_100_000)
    with pytest.raises(DomainValidationError, match="chunk duration"):
        validate_chunk_range(start_ms=0, end_ms=120_000, video_end_ms=1_100_000)
```

- [ ] **Step 2: Run speaker tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_speaker_assignment_rules.py tests/backend/integration/test_speaker_assignments.py -q`

Expected: FAIL because transcript and assignment modules do not exist.

- [ ] **Step 3: Add transcript, reference profile, and assignment schema**

`0003_transcripts.sql` must create `transcription_chunks`, `transcript_segments`, `voice_reference_profiles`, and `speaker_assignments`. Enforce `start_ms < end_ms`, one current assignment per segment, `assigned_subject_id` only for `subject`, text hash retention when `text_body` becomes NULL, and unique chunk/segment sequence numbers within a video. Service validation enforces 5-10 minute chunks while allowing only the final remainder below 5 minutes. Reference profiles store model/adapter versions and feature-file hash, while actual audio/embedding files remain outside SQLite and Git.

- [ ] **Step 4: Implement assignment validation without forecast logic**

```python
def validate_assignment(command: AssignmentCommand) -> None:
    if command.assignment_kind is AssignmentKind.SUBJECT and command.assigned_subject_id is None:
        raise DomainValidationError("subject assignment requires assigned_subject_id")
    if command.assignment_kind is not AssignmentKind.SUBJECT and command.assigned_subject_id is not None:
        raise DomainValidationError("non-subject assignment forbids assigned_subject_id")
    if not 0.0 <= command.raw_match_score <= 1.0:
        raise DomainValidationError("raw_match_score must be between 0 and 1")
```

The repository must store engine name/version, model name/version, reference profile ID, automatic/manual origin, assignment evidence hash, and UTC update time. It must not store direction, asset, period, or forecast fields.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_speaker_assignment_rules.py tests/backend/integration/test_speaker_assignments.py -q`

Expected: focused tests pass, including 653/55/14 counts.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: store transcript speaker assignments"
```

### Task 4: Deterministic jobs, checkpoints, pause, stop, and progress

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0004_jobs.sql`
- Create: `src/market_voice_forecast_ledger/domain/jobs.py`
- Create: `src/market_voice_forecast_ledger/repositories/jobs.py`
- Create: `src/market_voice_forecast_ledger/services/job_state.py`
- Create: `tests/backend/integration/test_job_checkpoints.py`
- Create: `tests/backend/unit/test_job_state_machine.py`

**Interfaces:**
- Produces: `JobStateService.create_job(kind: JobKind, units: Sequence[UnitSpec]) -> Job`
- Produces: `JobStateService.start(job_id: int) -> Job`
- Produces: `JobStateService.get_job(job_id: int) -> Job`
- Produces: `JobStateService.get_unit(unit_id: int) -> JobUnit`
- Produces: `JobStateService.request_pause(job_id: int) -> Job`
- Produces: `JobStateService.request_stop(job_id: int) -> Job`
- Produces: `JobStateService.complete_unit(unit_id: int, output_hash: str, outcome: UnitOutcome = UnitOutcome.COMPLETED) -> None`
- Produces: `JobStateService.list_units(job_id: int) -> tuple[JobUnit, ...]`
- Produces: `JobStateService.resume(job_id: int) -> tuple[JobUnit, ...]`
- Produces: `JobStateService.progress(job_id: int) -> ProgressSnapshot`

- [ ] **Step 1: Write failing state-machine and checkpoint tests**

```python
def test_resume_starts_at_fifth_chunk_after_four_verified_chunks(db):
    service = JobStateService(db)
    units = [UnitSpec("transcription", f"chunk-{index}", f"input-{index}") for index in range(1, 9)]
    job = service.create_job(JobKind.VIDEO_PIPELINE, units)
    service.start(job.id)
    for unit in service.list_units(job.id)[:4]:
        service.complete_unit(unit.id, f"output-{unit.sequence}")
    service.request_pause(job.id)
    remaining = service.resume(job.id)
    assert [unit.sequence for unit in remaining] == [5, 6, 7, 8]


def test_progress_uses_real_completed_unit_counts(db):
    service = JobStateService(db)
    job = service.create_job(JobKind.ANALYSIS, [
        UnitSpec("codex_analysis", "batch-1", "a"),
        UnitSpec("asset_mapping", "batch-1", "b"),
        UnitSpec("heatmap_update", "scope-1", "c"),
    ])
    service.start(job.id)
    service.complete_unit(service.list_units(job.id)[0].id, "result-a")
    assert service.progress(job.id) == ProgressSnapshot(completed=1, total=3, percent=33)


def test_review_required_output_does_not_fail_job(db):
    service = JobStateService(db)
    job = service.create_job(JobKind.ANALYSIS, [UnitSpec("asset_mapping", "review-1", "input")])
    service.start(job.id)
    unit = service.list_units(job.id)[0]
    service.complete_unit(unit.id, "review-output", outcome=UnitOutcome.REVIEW_REQUIRED)
    assert service.get_job(job.id).status is JobStatus.SUCCEEDED
    assert service.get_unit(unit.id).outcome is UnitOutcome.REVIEW_REQUIRED
```

- [ ] **Step 2: Run job tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py -q`

Expected: FAIL because jobs schema and state service do not exist.

- [ ] **Step 3: Add job schema and deterministic manifest hashing**

`0004_jobs.sql` must create `jobs` and `job_units` with the exact state enum from the spec, unique `(job_id, sequence)`, immutable input hash, nullable verified output hash, attempt count, safe error code, and start/end timestamps. `stage_kind` is constrained to `video_metadata`, `audio_download`, `transcription`, `speaker_assignment`, `subject_extraction`, `codex_analysis`, `asset_mapping`, or `heatmap_update`; outcome is `completed` or `review_required`. The manifest hash must be `sha256_text(canonical_json(unit_specs))` and stored before execution.

- [ ] **Step 4: Implement legal transitions and safe-boundary behavior**

```python
LEGAL_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.STOPPED},
    JobStatus.RUNNING: {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED, JobStatus.SUCCEEDED},
    JobStatus.PAUSE_REQUESTED: {JobStatus.PAUSED, JobStatus.FAILED},
    JobStatus.PAUSED: {JobStatus.RUNNING, JobStatus.STOPPED},
    JobStatus.CANCEL_REQUESTED: {JobStatus.STOPPED, JobStatus.FAILED},
    JobStatus.FAILED: {JobStatus.RETRYING},
    JobStatus.RETRYING: {JobStatus.RUNNING, JobStatus.FAILED},
    JobStatus.STOPPED: set(),
    JobStatus.SUCCEEDED: set(),
}
```

Completing a unit must validate the output hash and set output plus succeeded state in one transaction. `resume` must reuse only units whose current input hash and stored output hash match; stop creates no automatic restart. Review-required domain results are successful unit outputs, not failed jobs.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: add resumable pipeline jobs"
```

### Task 5: Cutoff scopes, immutable analysis runs, and fail-closed input building

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0005_analysis.sql`
- Create: `src/market_voice_forecast_ledger/domain/analysis.py`
- Create: `src/market_voice_forecast_ledger/repositories/analysis.py`
- Create: `src/market_voice_forecast_ledger/services/analysis_runs.py`
- Create: `tests/backend/integration/test_analysis_run_inputs.py`
- Create: `tests/backend/unit/test_analysis_run_validation.py`

**Interfaces:**
- Consumes: eligible videos, subject speaker assignments, current policy hash, transcript text/hash.
- Produces: `AnalysisRunService.get_or_create_scope(subject_id: int, selected_date: date) -> AnalysisScope`
- Produces: `AnalysisRunService.start_run(scope_id: int, prompt_version: str, schema_version: str) -> AnalysisRun`
- Produces: `AnalysisRunService.accept_output(run_id: int, result: RunResultMetadata) -> None`
- Produces: `AnalysisRunService.reject_output(run_id: int, error_code: str) -> None`
- Produces: `validate_run_metadata(model: str, reasoning_effort: str, external_tool_calls: int, schema_valid: bool) -> RunAcceptance`
- Produces: `validate_output_references(run_segment_ids: Collection[int], referenced_segment_ids: Collection[int]) -> None`

- [ ] **Step 1: Write failing cutoff, isolation, and model-boundary tests**

```python
def test_run_input_contains_only_eligible_subject_segments_before_cutoff(db, analysis_fixture):
    run = AnalysisRunService(db).start_run(
        analysis_fixture.scope_id,
        prompt_version="forecast-v1",
        schema_version="statement-v1",
    )
    assert run.segment_ids == (analysis_fixture.subject_segment_before_cutoff,)
    assert analysis_fixture.interviewer_segment not in run.segment_ids
    assert analysis_fixture.hold_segment not in run.segment_ids
    assert analysis_fixture.segment_after_cutoff not in run.segment_ids
    assert analysis_fixture.out_of_scope_channel_segment not in run.segment_ids
    assert analysis_fixture.duplicate_repost_segment not in run.segment_ids


@pytest.mark.parametrize(
    ("model", "effort", "tool_calls", "accepted"),
    [
        ("gpt-5.6-sol", "max", 0, True),
        ("gpt-5.6-sol", "high", 0, False),
        ("gpt-5.6-sol", "max", 1, False),
        ("lower-model", "max", 0, False),
    ],
)
def test_run_acceptance_is_fail_closed(model, effort, tool_calls, accepted):
    result = validate_run_metadata(model, effort, tool_calls, schema_valid=True)
    assert result.accepted is accepted


def test_output_cannot_reference_segment_outside_run():
    with pytest.raises(DomainValidationError, match="outside immutable run input"):
        validate_output_references({10, 11}, {10, 999})
```

- [ ] **Step 2: Run analysis-run tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_analysis_run_validation.py tests/backend/integration/test_analysis_run_inputs.py -q`

Expected: FAIL because analysis scope and run modules do not exist.

- [ ] **Step 3: Add analysis schema and immutable snapshot constraints**

`0005_analysis.sql` must create `analysis_scopes`, `analysis_runs`, `analysis_run_segments`, and `analysis_input_snapshots`. Enforce unique `(subject_id, cutoff_at_jst)`, append-only runs, unique run segment order, one input snapshot per run, and text deletion fields that retain SHA-256. `analysis_run_segments` must store the accepted assignment kind/subject/update time/evidence hash and policy ID/hash/status from run start.

- [ ] **Step 4: Implement JST cutoff and deterministic input snapshot creation**

```python
JST = ZoneInfo("Asia/Tokyo")


def cutoff_for_selected_date(selected_date: date) -> datetime:
    return datetime.combine(selected_date, time(23, 59, 59), tzinfo=JST)


def validate_run_metadata(
    model: str,
    reasoning_effort: str,
    external_tool_calls: int,
    schema_valid: bool,
) -> RunAcceptance:
    accepted = (
        model == "gpt-5.6-sol"
        and reasoning_effort == "max"
        and external_tool_calls == 0
        and schema_valid
    )
    return RunAcceptance(accepted=accepted, error_code=None if accepted else "RUN_BOUNDARY_REJECTED")
```

`start_run` must query only `published_at <= cutoff`, `subject_video_eligibility = eligible`, canonical members of duplicate groups, and `speaker_assignments = subject` matching the scope subject. It must order by publication time, video ID, and segment start, serialize the exact metadata/body with canonical JSON, compute one input hash, and insert run, segment snapshots, and input snapshot in one transaction. Empty input must create a failed run with safe code `NO_ELIGIBLE_SUBJECT_SEGMENTS`.

`accept_output` must reject JSON Schema failure, any source video/segment not present in the immutable run snapshot, any external tool call, and any model/effort mismatch before current tables are touched.

- [ ] **Step 5: Verify immutability and rerun coexistence**

Add integration assertions that different cutoff dates coexist, the same cutoff creates another run under the same scope, later speaker changes do not mutate old run snapshots, and direct UPDATE of immutable snapshot metadata is rejected by repository methods.

Run: `python -m pytest tests/backend/unit/test_analysis_run_validation.py tests/backend/integration/test_analysis_run_inputs.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: freeze cutoff analysis inputs"
```

### Task 6: Statement classification, public-date period normalization, and short evidence

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0006_statements.sql`
- Create: `src/market_voice_forecast_ledger/domain/statements.py`
- Create: `src/market_voice_forecast_ledger/services/periods.py`
- Create: `src/market_voice_forecast_ledger/services/statements.py`
- Create: `tests/backend/unit/test_period_normalization.py`
- Create: `tests/backend/unit/test_statement_validation.py`
- Create: `tests/backend/integration/test_current_statements.py`

**Interfaces:**
- Consumes: validated run ID and `StatementDraft` produced by a future Codex adapter.
- Produces: `normalize_period(expression: str, published_at: datetime) -> NormalizedPeriod`
- Produces: `parse_explicit_period(expression: str) -> NormalizedPeriod | None`
- Produces: `validate_statement(draft: StatementDraft) -> ValidatedStatement`
- Produces: `StatementService.stage_for_run(run_id: int, drafts: Sequence[StatementDraft]) -> tuple[ValidatedStatement, ...]`

- [ ] **Step 1: Write failing classification and period tests**

```python
def test_next_week_uses_published_at_jst():
    period = normalize_period("来週", datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc))
    assert period.time_basis == "published_at"
    assert period.start_date == date(2026, 8, 17)
    assert period.end_date == date(2026, 8, 23)


def test_explicit_month_week_overrides_relative_parser():
    period = parse_explicit_period("2026年9月第1週")
    assert period.start_date == date(2026, 9, 1)
    assert period.end_date == date(2026, 9, 7)


def test_turning_point_is_not_coerced_to_up():
    statement = validate_statement(StatementDraft(
        statement_type="future_forecast",
        condition_kind="conditional",
        condition_text="景気後退が回避される場合",
        direction_kind="turning_point",
        turning_point_kind="bottom",
        period_expression="来月",
        evidence="市場が底入れする可能性",
        original_subject_expression="株式市場",
    ))
    assert statement.direction_kind == "turning_point"


def test_conditional_statement_requires_condition_text():
    with pytest.raises(DomainValidationError, match="condition_text"):
        validate_statement(conditional_draft(condition_text=None))
```

- [ ] **Step 2: Run statement tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_period_normalization.py tests/backend/unit/test_statement_validation.py tests/backend/integration/test_current_statements.py -q`

Expected: FAIL because period and statement services do not exist.

- [ ] **Step 3: Add current statement schema**

`0006_statements.sql` must create a run-scoped staging table and `current_statements`. Store source run/video/segment/time range, short evidence, original subject expression, statement/condition/direction/turning-point enums, original period expression, normalized dates, `time_basis = published_at`, and unknown-period flag. Enforce condition text for conditional statements, turning-point subtype only for turning points, and evidence length at most 300 Unicode code points in service validation.

- [ ] **Step 4: Implement exact period rules**

```python
def add_months(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 + months
    return date(absolute_month // 12, absolute_month % 12 + 1, 1)


def parse_explicit_period(expression: str) -> NormalizedPeriod | None:
    year_match = re.fullmatch(r"(\d{4})年", expression)
    if year_match:
        year = int(year_match.group(1))
        return NormalizedPeriod(date(year, 1, 1), date(year, 12, 31), "published_at", False)
    month_match = re.fullmatch(r"(\d{4})年(\d{1,2})月", expression)
    if month_match:
        start = date(int(month_match.group(1)), int(month_match.group(2)), 1)
        return NormalizedPeriod(start, add_months(start, 1) - timedelta(days=1), "published_at", False)
    week_match = re.fullmatch(r"(\d{4})年(\d{1,2})月第([1-5])週", expression)
    if week_match:
        start = date(int(week_match.group(1)), int(week_match.group(2)), 1) + timedelta(
            days=(int(week_match.group(3)) - 1) * 7
        )
        month_end = add_months(date(start.year, start.month, 1), 1) - timedelta(days=1)
        return NormalizedPeriod(start, min(start + timedelta(days=6), month_end), "published_at", False)
    return None


def normalize_period(expression: str, published_at: datetime) -> NormalizedPeriod:
    explicit = parse_explicit_period(expression)
    if explicit is not None:
        return explicit
    base = published_at.astimezone(JST).date()
    if expression == "今週":
        start = base - timedelta(days=base.weekday())
        return NormalizedPeriod(start, start + timedelta(days=6), "published_at", False)
    if expression == "来週":
        start = base - timedelta(days=base.weekday()) + timedelta(days=7)
        return NormalizedPeriod(start, start + timedelta(days=6), "published_at", False)
    if expression == "再来週":
        start = base - timedelta(days=base.weekday()) + timedelta(days=14)
        return NormalizedPeriod(start, start + timedelta(days=6), "published_at", False)
    if expression in {"今月", "来月", "再来月"}:
        offset = {"今月": 0, "来月": 1, "再来月": 2}[expression]
        start = add_months(date(base.year, base.month, 1), offset)
        return NormalizedPeriod(start, add_months(start, 1) - timedelta(days=1), "published_at", False)
    if expression == "半年後":
        target = add_months(date(base.year, base.month, 1), 6)
        return NormalizedPeriod(target, add_months(target, 1) - timedelta(days=1), "published_at", False)
    return NormalizedPeriod(None, None, "published_at", True)
```

Explicit year, year/month, and year/month/week expressions are parsed before relative expressions. A future Codex adapter may also provide explicit start/end dates, which `validate_statement` must range-check without reinterpreting. Vague phrases such as `しばらく` must return unknown period. No branch may consult `created_at`, acquisition time, recording time, current wall clock, or market data.

- [ ] **Step 5: Verify no-evidence versus unknown and run all tests**

Add tests proving that no related statement creates no row, while an explicit unclassifiable statement creates direction `unknown`; `flat` remains distinct; evidence longer than 300 code points is rejected rather than silently truncated.

Run: `python -m pytest tests/backend/unit/test_period_normalization.py tests/backend/unit/test_statement_validation.py tests/backend/integration/test_current_statements.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 6**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: normalize forecast statements"
```

### Task 7: Asset mapping rules, confidence ceiling, and review gate

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0007_mappings.sql`
- Create: `src/market_voice_forecast_ledger/domain/mappings.py`
- Create: `src/market_voice_forecast_ledger/repositories/mappings.py`
- Create: `src/market_voice_forecast_ledger/services/asset_mapping.py`
- Create: `tests/backend/unit/test_asset_mapping_rules.py`
- Create: `tests/backend/integration/test_mapping_reviews.py`

**Interfaces:**
- Produces: `AssetMappingService.evaluate(statement: ValidatedStatement, context: SubjectContext, codex: CodexMappingProposal) -> tuple[MappingDecision, ...]`
- Produces: `AssetMappingService.review(mapping_id: int, action: ReviewAction, target_asset: Asset | None, reason: str) -> MappingReview`
- Produces: `effective_heatmap_eligibility(mapping_id: int) -> bool`
- Produces: `evaluate_mapping(statement: ValidatedStatement, context: SubjectContext, codex: CodexMappingProposal) -> tuple[MappingDecision, ...]`
- Produces: `lower_confidence(left: Confidence, right: Confidence) -> Confidence`

- [ ] **Step 1: Write failing deterministic mapping tests**

```python
def test_japan_equity_market_maps_to_nikkei_and_topix_as_inferred():
    decisions = evaluate_mapping(
        statement=statement(original_subject_expression="日本株"),
        context=subject_context(direct_mentions=(), nearby_markets=("japan_equity",), competing_markets=()),
        codex=codex_proposal(Confidence.HIGH),
    )
    assert [(d.asset, d.mapping_kind, d.final_confidence) for d in decisions] == [
        (Asset.NIKKEI_225, MappingKind.INFERRED, Confidence.HIGH),
        (Asset.TOPIX, MappingKind.INFERRED, Confidence.HIGH),
    ]


def test_interviewer_only_clue_is_unresolved():
    decisions = evaluate_mapping(
        statement=statement(original_subject_expression="株式市場"),
        context=subject_context(interviewer_markets=("us_equity",)),
        codex=codex_proposal(Confidence.HIGH),
    )
    assert decisions[0].final_confidence is Confidence.UNRESOLVED


def test_lower_of_codex_and_rule_confidence_is_ceiling():
    final_confidence = lower_confidence(Confidence.HIGH, Confidence.LOW)
    assert final_confidence is Confidence.LOW
```

- [ ] **Step 2: Run mapping tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_mapping_reviews.py -q`

Expected: FAIL because mapping modules do not exist.

- [ ] **Step 3: Add mapping and append-only review schema**

`0007_mappings.sql` must create run staging mappings, `current_asset_mappings`, and append-only `mapping_reviews`. Store source expression, asset enum (`nikkei_225`, `topix`, `sp500`, `xau_usd`), direct/inferred kind, reason, Codex confidence, rule confidence, final confidence, mismatch flag, validated rule-evidence JSON, review action/reason/actor/time, and effective eligibility without overwriting computed confidence.

- [ ] **Step 4: Implement explicit rule evidence and confidence ordering**

```python
CONFIDENCE_RANK = {
    Confidence.UNRESOLVED: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def lower_confidence(left: Confidence, right: Confidence) -> Confidence:
    return left if CONFIDENCE_RANK[left] <= CONFIDENCE_RANK[right] else right


MARKET_DEFAULTS = {
    "japan_equity": (Asset.NIKKEI_225, Asset.TOPIX),
    "us_equity": (Asset.SP500,),
}
```

Rule evidence must explicitly record direct subject mentions, nearby subject mentions, competing markets, and interviewer-only clues. Direct index mention yields `direct`; market-wide conversion yields `inferred`; ambiguous `株式市場` needs consistent nearby subject context for `medium`; competing or contradictory subject context yields `low` or `unresolved`. XAU/USD must remain absent when no related statement exists.

- [ ] **Step 5: Implement review gate and audit assertions**

`low` and `unresolved` must be ineligible until an `approve` or `correct` review with non-empty reason exists. `reject` remains ineligible. Correction must record old/new asset without mutating calculated confidence. Tests must assert actor, reason, old/new values, and UTC time are present in both `mapping_reviews` and `audit_events`.

Run: `python -m pytest tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_mapping_reviews.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 7**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: apply auditable asset mapping rules"
```

### Task 8: Current forecasts, conflict preservation, and rebuildable heatmap cache

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0008_forecasts.sql`
- Create: `src/market_voice_forecast_ledger/domain/forecasts.py`
- Create: `src/market_voice_forecast_ledger/repositories/forecasts.py`
- Create: `src/market_voice_forecast_ledger/services/forecast_projection.py`
- Create: `tests/backend/unit/test_forecast_selection.py`
- Create: `tests/backend/integration/test_heatmap_projection.py`

**Interfaces:**
- Produces: `ForecastProjectionService.build(scope_id: int, staged_run_id: int) -> ForecastProjection`
- Produces: `ForecastProjectionService.list_heatmap(scope_id: int) -> tuple[HeatmapCell, ...]`
- Produces: `ForecastProjectionService.delete_heatmap_cache(scope_id: int) -> None`
- Produces: `ForecastProjectionService.rebuild_heatmap(scope_id: int) -> tuple[HeatmapCell, ...]`
- Produces: `select_current_view(candidates: Sequence[ForecastCandidate]) -> ForecastSelection`
- Produces: `project_forecasts(candidates: Sequence[ForecastCandidate]) -> ForecastProjection`

- [ ] **Step 1: Write failing forecast and cache tests**

```python
def test_conflicting_up_and_down_are_not_averaged_to_flat():
    selection = select_current_view([
        candidate(direction="up", published_at="2026-08-01T00:00:00Z", mapping_kind="inferred"),
        candidate(direction="down", published_at="2026-08-08T00:00:00Z", mapping_kind="direct"),
    ])
    assert selection.selected.direction == "down"
    assert selection.has_view_change is True
    assert selection.selected.direction != "flat"


def test_conditional_forecast_uses_separate_layer():
    projection = project_forecasts([unconditional_up(), conditional_down("景気後退の場合")])
    assert {forecast.layer for forecast in projection.forecasts} == {"base", "conditional"}
    assert projection.by_layer("conditional").condition_text == "景気後退の場合"


def test_heatmap_can_be_rebuilt_after_cache_delete(db, projected_scope):
    service = ForecastProjectionService(db)
    expected = service.list_heatmap(projected_scope)
    service.delete_heatmap_cache(projected_scope)
    rebuilt = service.rebuild_heatmap(projected_scope)
    assert rebuilt == expected
```

- [ ] **Step 2: Run forecast tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_heatmap_projection.py -q`

Expected: FAIL because forecast modules do not exist.

- [ ] **Step 3: Add forecast, evidence link, and heatmap cache schema**

`0008_forecasts.sql` must create run staging forecasts, `current_forecasts`, `forecast_statement_links`, and `heatmap_cells`. Enforce one current row per `(scope, asset, start_date, end_date, layer, selection_key)`, non-empty condition text for conditional layer, and cache uniqueness by scope/subject/asset/period/layer. Store `stale`, `heatmap_eligible`, exclusion reason, final confidence, evidence count, view-change flag, and selected source publication time.

- [ ] **Step 4: Implement deterministic current-view selection**

```python
def candidate_rank(candidate: ForecastCandidate) -> tuple[datetime, int, int]:
    directness = 1 if candidate.mapping_kind == "direct" else 0
    period_specificity = 1 if not candidate.unknown_period else 0
    return (candidate.published_at, directness, period_specificity)


def select_current_view(candidates: Sequence[ForecastCandidate]) -> ForecastSelection:
    selected = max(candidates, key=candidate_rank)
    directions = {candidate.direction for candidate in candidates}
    return ForecastSelection(
        selected=selected,
        has_view_change=len(directions) > 1,
        conflicting_candidate_ids=tuple(
            candidate.id for candidate in candidates if candidate.direction != selected.direction
        ),
    )
```

The projection must exclude non-future statements, unknown periods, and unreviewed low/unresolved mappings from the main heatmap. It must keep turning points, flat, unknown, and absence as distinct states. Heatmap rows must cover the four configured subjects by four assets; missing evidence produces an empty cell, not an `unknown` forecast.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_heatmap_projection.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 8**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: project current forecast heatmaps"
```

### Task 9: Audited corrections, stale scopes, and atomic current-result replacement

**Files:**
- Create: `src/market_voice_forecast_ledger/services/current_results.py`
- Modify: `src/market_voice_forecast_ledger/services/channel_policy.py`
- Modify: `src/market_voice_forecast_ledger/services/speaker_assignment.py`
- Modify: `src/market_voice_forecast_ledger/repositories/analysis.py`
- Modify: `src/market_voice_forecast_ledger/repositories/forecasts.py`
- Create: `tests/backend/integration/test_stale_transitions.py`
- Create: `tests/backend/integration/test_atomic_result_replacement.py`
- Create: `tests/backend/integration/test_audit_redaction.py`

**Interfaces:**
- Consumes: current policies, assignments, run staging statements/mappings/forecasts/cells.
- Produces: `SpeakerAssignmentService.correct(command: SpeakerCorrection) -> SpeakerAssignment`
- Produces: `ChannelPolicyService.update_policy(command: PolicyChange) -> ChannelPolicy`
- Produces: `CurrentResultService.replace_scope(run_id: int) -> ReplacementSummary`

- [ ] **Step 1: Write failing stale and rollback tests**

```python
def test_speaker_correction_audits_and_marks_dependent_scope_stale(db, analyzed_segment):
    corrected = SpeakerAssignmentService(db).correct(SpeakerCorrection(
        segment_id=analyzed_segment.segment_id,
        assignment_kind=AssignmentKind.HOLD,
        assigned_subject_id=None,
        actor="user",
        reason="参照声を再確認したため",
    ))
    assert corrected.assignment_kind is AssignmentKind.HOLD
    assert AnalysisRepository(db).get_scope(analyzed_segment.scope_id).status is ScopeStatus.STALE
    event = latest_audit_event(db, "speaker_assignment", analyzed_segment.segment_id)
    assert event.before_json["assignment_kind"] == "subject"
    assert event.after_json["assignment_kind"] == "hold"


def test_failed_reanalysis_keeps_old_current_results(db, current_scope, staged_reanalysis):
    old = snapshot_current_results(db, current_scope)
    db.execute("""
        CREATE TRIGGER fail_current_forecast_insert
        BEFORE INSERT ON current_forecasts
        BEGIN
            SELECT RAISE(ABORT, 'injected replacement failure');
        END
    """)
    with pytest.raises(sqlite3.IntegrityError):
        CurrentResultService(db).replace_scope(staged_reanalysis.run_id)
    assert snapshot_current_results(db, current_scope) == old
```

- [ ] **Step 2: Run transition tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_stale_transitions.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_audit_redaction.py -q`

Expected: FAIL because correction and atomic replacement services are incomplete.

- [ ] **Step 3: Implement correction transactions**

```python
def correct(self, command: SpeakerCorrection) -> SpeakerAssignment:
    with transaction(self._conn):
        before = self._repo.get_assignment(command.segment_id)
        after = self._repo.replace_assignment(command)
        self._audit.append(
            entity_type="speaker_assignment",
            entity_id=command.segment_id,
            operation="correct",
            actor_kind=command.actor,
            reason_code="USER_CORRECTION",
            reason_text=command.reason,
            before=before.audit_view(),
            after=after.audit_view(),
        )
        self._analysis.mark_scopes_using_segment_stale(command.segment_id)
    return after
```

Channel policy change must use the same pattern: update current policy, audit before/after, reevaluate every affected video, stop newly out-of-scope pending units, and mark scopes that used formerly eligible videos stale. It must not delete old runs or current forecasts.

- [ ] **Step 4: Implement one-transaction result replacement**

`CurrentResultService.replace_scope` must verify the run is accepted, snapshot old current statement/mapping/forecast summaries without full text, delete and insert the scope's current normalized rows, rebuild affected heatmap cells, set scope `current`, and append one replacement audit event in a single transaction. Different cutoff scopes must never be updated. A validation failure or injected DB failure must leave the prior current set unchanged.

- [ ] **Step 5: Enforce audit redaction**

```python
FORBIDDEN_AUDIT_KEYS = {
    "text_body",
    "input_text",
    "audio_path",
    "embedding",
    "prompt_body",
}


def validate_audit_payload(value: object) -> None:
    for key in walk_mapping_keys(value):
        if key in FORBIDDEN_AUDIT_KEYS:
            raise DomainValidationError(f"audit payload contains forbidden key: {key}")
```

Tests must prove that hashes, IDs, timestamps, short evidence, reason, actor, and before/after classifications remain while full transcript and exact input text are rejected.

- [ ] **Step 6: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_stale_transitions.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_audit_redaction.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 7: Commit Task 9**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: audit corrections and replace results atomically"
```

### Task 10: Retention, manual deletion preview, and audio cleanup retry

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0009_retention.sql`
- Create: `src/market_voice_forecast_ledger/services/retention.py`
- Create: `tests/backend/unit/test_retention_policy.py`
- Create: `tests/backend/integration/test_text_deletion.py`
- Create: `tests/backend/integration/test_audio_cleanup.py`

**Interfaces:**
- Produces: `RetentionService.preview_text_deletion(command: DeleteTextCommand) -> DeletionPreview`
- Produces: `RetentionService.delete_text(command: DeleteTextCommand) -> DeletionResult`
- Produces: `RetentionService.purge_expired(now: datetime) -> DeletionResult`
- Produces: `RetentionService.delete_audio(artifact_id: int) -> AudioDeletionResult`

- [ ] **Step 1: Write failing retention and cleanup tests**

```python
@pytest.mark.parametrize("days", [30, 90, 180, 365, None])
def test_supported_retention_values(days):
    policy = RetentionPolicy(days=days)
    assert policy.days == days


def test_expired_text_deletion_keeps_hash_and_forecast(db, expired_analyzed_segment):
    result = RetentionService(db).purge_expired(now=datetime(2026, 8, 14, tzinfo=timezone.utc))
    segment = SpeakerRepository(db).get_segment(expired_analyzed_segment.segment_id)
    assert segment.text_body is None
    assert segment.text_sha256 == expired_analyzed_segment.text_sha256
    assert ForecastRepository(db).count_current(expired_analyzed_segment.scope_id) == 1
    assert result.deleted_transcript_count == 1


def test_audio_delete_failure_can_be_retried(db, tmp_path, monkeypatch):
    artifact = create_audio_artifact(db, tmp_path / "temporary.wav")
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError))
    failed = RetentionService(db).delete_audio(artifact.id)
    assert failed.error_code == "AUDIO_DELETE_PERMISSION"
    assert failed.retryable is True
```

- [ ] **Step 2: Run retention tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_retention_policy.py tests/backend/integration/test_text_deletion.py tests/backend/integration/test_audio_cleanup.py -q`

Expected: FAIL because retention service and local artifact schema do not exist.

- [ ] **Step 3: Add retention settings and local artifact tracking**

`0009_retention.sql` must create one `retention_settings` row with default `365`, constrain value to `30`, `90`, `180`, `365`, or NULL for unlimited, and create `local_artifacts` for local audio cleanup status. Local paths remain in the private SQLite DB and must never enter audit JSON or API error text.

- [ ] **Step 4: Implement preview-before-delete and hash-preserving purge**

```python
ALLOWED_RETENTION_DAYS = {30, 90, 180, 365, None}


def expiry_for(created_at: datetime, days: int | None) -> datetime | None:
    if days not in ALLOWED_RETENTION_DAYS:
        raise DomainValidationError("unsupported retention period")
    return None if days is None else created_at + timedelta(days=days)
```

`preview_text_deletion` must return affected video count, transcript count, analysis-input count, and a `full_reproduction_will_be_lost` flag without changing data. `delete_text` must require the preview token/hash, set transcript and input bodies to NULL, set deletion timestamps, preserve hashes/IDs/times/results/short evidence, and append a deletion audit event without body text. Reading or reanalysis must not extend expiration automatically.

- [ ] **Step 5: Implement audio cleanup retry through jobs**

Successful audio deletion sets artifact status `deleted` and `deleted_at`. Failure stores only a safe error code and retry count, marks the cleanup unit failed, and permits a later cleanup job to retry. It must never report successful pipeline cleanup while an artifact remains undeleted.

Run: `python -m pytest tests/backend/unit/test_retention_policy.py tests/backend/integration/test_text_deletion.py tests/backend/integration/test_audio_cleanup.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 10**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: enforce private data retention"
```

### Task 11: Local-only FastAPI boundary

**Files:**
- Create: `src/market_voice_forecast_ledger/api/__init__.py`
- Create: `src/market_voice_forecast_ledger/api/app.py`
- Create: `src/market_voice_forecast_ledger/api/dependencies.py`
- Create: `src/market_voice_forecast_ledger/api/routes/__init__.py`
- Create: `src/market_voice_forecast_ledger/api/routes/health.py`
- Create: `src/market_voice_forecast_ledger/api/routes/subjects.py`
- Create: `src/market_voice_forecast_ledger/api/routes/scopes.py`
- Create: `src/market_voice_forecast_ledger/api/routes/jobs.py`
- Create: `src/market_voice_forecast_ledger/api/routes/reviews.py`
- Create: `src/market_voice_forecast_ledger/api/routes/retention.py`
- Create: `src/market_voice_forecast_ledger/cli.py`
- Create: `tests/backend/integration/test_api_boundary.py`

**Interfaces:**
- Produces: `create_app(settings: Settings) -> FastAPI`
- Produces: `GET /api/health`, `GET /api/subjects`, `GET /api/scopes/{scope_id}/heatmap`, `GET /api/jobs/{job_id}`, `POST /api/mappings/{mapping_id}/reviews`, `POST /api/retention/preview`, `POST /api/retention/delete`
- Produces: `python -m market_voice_forecast_ledger.cli serve --host 127.0.0.1 --port 8765`

- [ ] **Step 1: Write failing API response and local-host tests**

```python
def test_health_and_subject_responses_do_not_expose_private_text(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    subjects = client.get("/api/subjects").json()
    serialized = json.dumps(subjects, ensure_ascii=False)
    assert "text_body" not in serialized
    assert "input_text" not in serialized
    assert "audio_path" not in serialized


def test_server_rejects_non_loopback_host():
    with pytest.raises(DomainValidationError, match="127.0.0.1"):
        validate_bind_host("0.0.0.0")


def test_mapping_review_requires_reason(client, reviewable_mapping):
    response = client.post(
        f"/api/mappings/{reviewable_mapping}/reviews",
        json={"action": "approve", "reason": ""},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_api_boundary.py -q`

Expected: FAIL because the FastAPI application does not exist.

- [ ] **Step 3: Implement dependency lifetime and response models**

```python
def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Market Voice Forecast Ledger", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(subjects.router, prefix="/api")
    app.include_router(scopes.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(reviews.router, prefix="/api")
    app.include_router(retention.router, prefix="/api")
    return app


def validate_bind_host(host: str) -> str:
    if host != "127.0.0.1":
        raise DomainValidationError("server host must be 127.0.0.1")
    return host
```

Each request must open and close its own SQLite connection. Routes may return short evidence, hashes, IDs, classification, source video/timestamp, progress counts, and safe errors; they must not return full transcript, exact analysis input, local artifact paths, API keys, prompt bodies, or stack traces.

- [ ] **Step 4: Implement endpoint validation and transactional writes**

Pydantic request models must constrain review actions, require non-empty reasons, validate deletion preview tokens, and reject unknown fields. Write routes must call domain services rather than issuing SQL directly. Exception handlers must map domain validation to 422, missing entity to 404, conflict/stale input to 409, and unexpected failure to 500 with `{"error": "INTERNAL_ERROR"}` only.

- [ ] **Step 5: Run API and full backend tests**

Run: `python -m pytest tests/backend/integration/test_api_boundary.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all backend tests pass.

- [ ] **Step 6: Commit Task 11**

```powershell
git add -- src/market_voice_forecast_ledger tests/backend
git commit -m "feat: expose local forecast ledger api"
```

### Task 12: Synthetic end-to-end fixture, Windows test entry, and handoff docs

**Files:**
- Create: `tests/backend/e2e/test_synthetic_heatmap_flow.py`
- Create: `tests/backend/README.md`
- Create: `scripts/test-backend.ps1`
- Modify: `README.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/project/status.md`

**Interfaces:**
- Consumes: all Task 1-11 public interfaces.
- Produces: one command that verifies schema, domain rules, transactions, API, and a synthetic 4-subject × 4-asset heatmap.

- [ ] **Step 1: Write the failing synthetic end-to-end test**

```python
def test_saved_synthetic_input_reaches_sixteen_row_heatmap(db):
    fixture = SyntheticLedgerFixture(db).create_four_subjects_and_eligible_videos()
    fixture.add_subject_statement(
        subject_key="analyst_a",
        evidence="日本株は来月に底入れする可能性がある",
        subject_expression="日本株",
        direction="turning_point",
        period="来月",
    )
    fixture.add_subject_statement(
        subject_key="analyst_b",
        evidence="米国株は来年に上向く",
        subject_expression="米国株",
        direction="up",
        period="2027年",
    )
    result = fixture.run_without_external_tools(cutoff=date(2026, 8, 14))
    rows = result.heatmap_rows
    assert len(rows) == 16
    assert result.external_tool_calls == 0
    assert result.find("analyst_a", "nikkei_225").direction == "turning_point"
    assert result.find("analyst_a", "topix").mapping_kind == "inferred"
    assert result.find("analyst_b", "sp500").direction == "up"
    assert result.find("analyst_a", "xau_usd").is_empty is True
```

The fixture must use synthetic channel IDs, synthetic video IDs, synthetic names, and synthetic statements. It must bypass network, audio, and Codex processes by inserting a validated run output through the same public staging interface used by a future adapter.

- [ ] **Step 2: Run E2E test and verify RED**

Run: `python -m pytest tests/backend/e2e/test_synthetic_heatmap_flow.py -q`

Expected: FAIL until missing fixture orchestration or integration defects are implemented.

- [ ] **Step 3: Implement only missing orchestration needed by the E2E path**

The orchestration must call, in order: source/video eligibility, transcript segment creation, speaker assignment, scope/run snapshot, validated statement staging, period normalization, asset mapping, forecast projection, atomic replacement, and heatmap read. It must not add direct SQL shortcuts to the test.

- [ ] **Step 4: Add an ASCII-compatible Windows verification command**

```powershell
$ErrorActionPreference = 'Stop'
python -m pytest tests/backend -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m compileall -q src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite All
exit $LASTEXITCODE
```

Save this exact executable logic to `scripts/test-backend.ps1` using ASCII-compatible messages only.

- [ ] **Step 5: Document setup, private data boundary, and verified commands**

`tests/backend/README.md` and root `README.md` must document:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests/backend -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

The documentation must state that real transcripts, audio, embeddings, SQLite DBs, runtime logs, and credentials live outside the repository. Update `docs/project/plan.md` and `docs/project/status.md` with actual completed tasks, actual test counts, remaining work, and the next approved subproject; do not predeclare unrun results.

- [ ] **Step 6: Run final verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1`

Expected: backend pytest, Python compileall, and existing 119 work-state tests all exit 0.

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree`

Expected: public safety check passes with no DB, audio, transcript, credential, secret, or oversized artifact.

- [ ] **Step 7: Commit Task 12**

```powershell
git add -- README.md scripts/test-backend.ps1 tests/backend docs/project/plan.md docs/project/status.md
git commit -m "test: verify synthetic core data flow"
```

---

## Completion Criteria

- All nine numbered migrations apply once to a new SQLite DB and leave foreign keys enabled.
- The four default analysis subjects exist; 江守哲 is configured with `UCVXka7buS_WptsAzSE0LcKg`; no unconfirmed 暁投資顧問 channel ID is invented.
- Manual URL registration cannot bypass fixed-channel rules.
- Duplicate reposts remain stored but only the canonical video contributes analysis input.
- Subject, interviewer, and hold assignments are stored separately and only subject segments enter analysis inputs.
- Different cutoff scopes coexist; same-cutoff reruns preserve immutable run history and replace current results only after validation.
- Relative periods use `published_at` in JST; `recorded_at` does not exist in the schema.
- Turning point, flat, unknown, and no evidence remain distinct.
- Codex confidence cannot override lower application-rule confidence; low/unresolved require audited review.
- Conflicting forecasts are not averaged to flat; conditional forecasts use a separate layer.
- Current results, audit event, and heatmap update atomically or not at all.
- Progress is derived from completed manifest units; pause, stop, retry, and checkpoint reuse follow legal transitions.
- Text deletion preserves hashes and current forecasts; audio deletion failures remain retryable.
- API responses exclude private body text and local paths; server host validation allows only `127.0.0.1`.
- Synthetic E2E yields 16 subject/asset rows with empty XAU/USD where no evidence exists.
- Backend tests, compileall, existing 119 work-state tests, state-document checks, and public-safety checks pass from the documented command.

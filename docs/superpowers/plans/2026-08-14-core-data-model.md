# M2 中核バックエンド Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 承認済みM1設計に従い、主体別チャンネル方針、話者割当、変更不能な分析run、発言分類、期間、指数割当、現在予想、ヒートマップ、checkpoint、監査、保持・削除をSQLiteへ安全に保存するテスト済みのM2中核バックエンドを構築する。

**Architecture:** Python packageを `src/market_voice_forecast_ledger/` に置き、標準 `sqlite3` と追記型の番号付きSQL migrationをschemaの正本にする。DB repository、純粋なドメイン規則、transactional service、FastAPI境界を分離し、YouTube検索、音声処理、Codex CLI、React UIは後続adapterがこの中核へ入力する。run由来の事実は追記専用、修正可能な現在値は監査eventと同一transactionで更新し、現在結果は検証済みrunからだけ原子的に置換する。

**Tech Stack:** Python 3.11以上、標準 `sqlite3`、FastAPI 0.115以上1.0未満、Pydantic 2.10以上3.0未満、pytest 8.3以上10.0未満、httpx 0.28以上1.0未満、PowerShell 5.1/7互換、SQLite WAL。

**Plan revision:** 2026-08-15 JST。初回12タスク草案を置換し、承認済みM1書面設計から19個の独立レビュー単位へ再構成した。

## Global Constraints

- 実装開始時は `superpowers:using-git-worktrees` で隔離作業領域を確認・作成し、この計画のユーザー承認前にTask 1を開始しない。
- Windows 11で動作し、実データの既定保存先は `%LOCALAPPDATA%\MarketVoiceForecastLedger\`、テストデータは必ずpytestの `tmp_path` 配下に置く。
- HTTP serverは `127.0.0.1` だけへbindする。MVPはローカルトークン、`Origin`検査、Windowsアカウント認証を持たず、信頼できる単独利用PCを前提とする。状態変更へGETを使わない。
- DB時刻はUTCのISO 8601、日付指定cutoffは選択日のJST 23:59:59、期間はJST暦日の `YYYY-MM-DD` とする。
- 分析用の動画日時はYouTubeの `published_at` だけとし、`recorded_at` や分析用取得日を作成・保存・推測しない。
- 木野内栄治と大川智宏は `all_channels`、江守哲は固定ID `UCVXka7buS_WptsAzSE0LcKg`、暁投資顧問は固定ID `UCOfzLmXpI3qmZfV7_Cs1sYA` とする。チャンネル表示名を正本にしない。
- 手動URL登録はチャンネル方針を迂回しない。江守哲の固定ID不一致動画はメタデータ以外の音声取得、文字起こし、分析、ヒートマップへ進めない。
- 木野内栄治、江守哲、大川智宏では本人割当区間だけを予想分析へ渡し、聞き手は文脈、保留はレビュー待ちとする。暁投資顧問は適合済み公式チャンネル内の全発話を組織主体の入力にする。
- 元動画、切り抜き、Shorts、再投稿を重複判定・統合・除外しない。YouTube動画IDが異なる出典は、同内容でも独立した動画、発言、根拠、集計対象にする。
- MVPの話者照合は1音声モデルへ固定し、モデル名、モデル版、生スコア、閾値設定版を保存する。モデル横断の0～1正規化と複数モデル対応を実装しない。
- Codex runは `gpt-5.6-sol`、reasoning effort `max`、外部ツール呼び出し0件だけを採用し、下位モデルへfallbackしない。Web検索、相場データ、ニュース、Codex一般知識、shellを予想根拠へ混ぜない。
- 発言は `future_forecast`、`current_analysis`、`past_result_analysis`、`general_statement` に分け、将来予想だけを主ヒートマップ候補にする。
- `turning_point`、`flat`、`unknown`、関連発言なしの空欄を別状態にし、条件付き予想は無条件予想と別layerにする。
- 期間の明示年月日は `explicit_statement`、相対期間は `published_at` とする。「何月第1週」はその月の1日を含む月～日とし、時期不明は承認後だけ専用列へ入れる。
- 指数割当信頼度は `high`、`medium`、`low`、`unresolved`。Codex自己評価とアプリ規則の低い側を自動採用上限とし、`low` と `unresolved` は監査付きレビューを必須にする。
- 監査event、analysis run、run event、mapping review、period reviewはサービス層とSQLite triggerの両方で追記専用にする。分析入力snapshotは本文の非NULLからNULLへの期限削除と削除日時設定だけを例外として許可する。
- 全文文字起こしと正確な分析入力本文の既定保持期間は365日。30、90、180、365日、無期限を扱い、監査JSONへ本文、音声path、埋め込み、prompt本文を複製しない。
- 音声自動削除は専用一時音声folder内の解決済み絶対pathだけに限定し、範囲外pathを拒否・監査する。
- 実音声、実全文文字起こし、実人物の予想文、DB、埋め込み、API key、Cookie、token、cache、logをrepositoryやtest fixtureへ含めない。
- YouTube検索・網羅性評価、音声取得実装、文字起こしengine、音声モデル製品選定、Codex prompt全文・CLI adapter、React UI、Windows常駐化はこの計画のscope外とする。
- 各Taskは失敗テスト、RED確認、最小実装、focused test、全backend test、対象限定commitの順で完了する。`git add .` を使わない。

---

## File Map

### Foundation

- `pyproject.toml`: Python version、runtime/dev依存、pytest設定。
- `src/market_voice_forecast_ledger/config.py`: runtime data directory、DB、専用一時音声folder。
- `src/market_voice_forecast_ledger/domain/errors.py`: 安全なdomain error code。
- `src/market_voice_forecast_ledger/domain/enums.py`: schemaと共有する文字列enum。
- `src/market_voice_forecast_ledger/domain/common.py`: canonical JSON、SHA-256、UTC/JST helper。
- `src/market_voice_forecast_ledger/db/connection.py`: SQLite connectionとtransaction。
- `src/market_voice_forecast_ledger/db/migrate.py`: package resourceの番号付きSQL migration runner。
- `src/market_voice_forecast_ledger/db/migrations/0001_*.sql`～`0015_*.sql`: 追記型schema履歴。

### Repositories and services

- `repositories/audit.py`, `services/audit.py`: 追記専用監査と本文混入拒否。
- `domain/sources.py`, `repositories/sources.py`, `services/channel_policy.py`, `bootstrap.py`: 主体、動画、チャンネル方針、動画適合性。
- `domain/speakers.py`, `repositories/speakers.py`, `services/speaker_assignment.py`: chunk、発話、固定音声モデル、個人話者割当、暁投資顧問の組織割当。
- `domain/jobs.py`, `repositories/jobs.py`, `services/job_state.py`: 決定的manifest、unit、pause/stop/retry、実完了数。
- `domain/analysis.py`, `repositories/analysis.py`, `services/analysis_runs.py`, `services/codex_contract.py`: cutoff scope、run、入力snapshot、Codex出力境界。
- `domain/statements.py`, `repositories/statements.py`, `services/statements.py`: 4分類、複数根拠、本文連続部分検査。
- `domain/periods.py`, `repositories/periods.py`, `services/periods.py`: 明示・相対・時期不明期間とreview。
- `domain/mappings.py`, `repositories/mappings.py`, `services/asset_mapping.py`, `services/mapping_review.py`: 指数割当、規則信頼度、review gate。
- `domain/forecasts.py`, `repositories/forecasts.py`, `services/forecast_projection.py`, `services/current_results.py`: 見解相違、見解変更、現在結果の原子的置換。
- `services/corrections.py`: 話者・チャンネル方針修正とscope stale化。
- `repositories/heatmap.py`, `services/heatmap.py`: 再生成可能な週・月cacheと時期不明列。
- `repositories/retention.py`, `services/retention.py`: 本文保持、削除preview、専用folder限定の音声清掃。

### API and verification

- `api/app.py`, `api/dependencies.py`, `api/routes/*.py`: loopback-only FastAPI、private fieldを除外したread/write model。
- `cli.py`: `serve --host 127.0.0.1 --port 8765` のhost検査付き入口。
- `tests/backend/conftest.py`: 一時DB、固定clock、migration適用fixtureだけを共有する。
- `tests/backend/unit/`: DBを使わない規則テスト。
- `tests/backend/integration/`: SQLite制約、trigger、transaction、service連携テスト。
- `tests/backend/e2e/`: 合成人物・合成発言だけの4主体×4資産flow。
- `scripts/test-backend.ps1`: backend、compileall、既存work-state suiteのASCII互換入口。

Test factoryは各test module内に置き、実在人物の予想文・音声・全文文字起こしを使わない。E2Eだけが `tests/backend/e2e/synthetic_fixture.py` に合成orchestrationを持ち、public serviceを通さない直接SQL shortcutを禁止する。

---

### Task 1: Python package、共通型、SQLite migration基盤

**Files:**
- Create: `pyproject.toml`
- Create: `src/market_voice_forecast_ledger/__init__.py`
- Create: `src/market_voice_forecast_ledger/config.py`
- Create: `src/market_voice_forecast_ledger/domain/__init__.py`
- Create: `src/market_voice_forecast_ledger/domain/errors.py`
- Create: `src/market_voice_forecast_ledger/domain/enums.py`
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
- Produces: `canonical_json(value: object) -> str`
- Produces: `sha256_text(value: str) -> str`
- Produces: `utc_iso(value: datetime) -> str`
- Produces: `cutoff_at_jst(day: date) -> datetime`
- Produces: `DomainError(code: str, message: str)`

- [ ] **Step 1: Write failing foundation tests**

```python
def test_settings_keep_runtime_data_outside_repository(tmp_path):
    settings = Settings.for_data_dir(tmp_path / "runtime")
    assert settings.database_path == tmp_path / "runtime" / "ledger.sqlite3"
    assert settings.temp_audio_dir == tmp_path / "runtime" / "temp-audio"


def test_migrations_apply_once_and_connection_enables_safety_pragmas(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert apply_migrations(conn) == ("0001_foundation",)
    assert apply_migrations(conn) == ()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/backend/integration/test_database_foundation.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'market_voice_forecast_ledger'`.

- [ ] **Step 3: Add package metadata and the foundation implementation**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "market-voice-forecast-ledger"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.115,<1.0", "pydantic>=2.10,<3.0", "uvicorn>=0.32,<1.0"]

[project.optional-dependencies]
dev = ["httpx>=0.28,<1.0", "pytest>=8.3,<10.0", "pytest-cov>=6.0,<8.0"]

[tool.pytest.ini_options]
testpaths = ["tests/backend"]
addopts = "-q"
```

```python
@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    temp_audio_dir: Path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "Settings":
        return cls(data_dir, data_dir / "ledger.sqlite3", data_dir / "temp-audio")


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

Define the shared `StrEnum` values exactly once in `domain/enums.py`:

```python
class SubjectKind(StrEnum): PERSON = "person"; ORGANIZATION = "organization"
class PolicyKind(StrEnum): ALL_CHANNELS = "all_channels"; FIXED_CHANNEL = "fixed_channel"
class ConfigurationStatus(StrEnum): CONFIGURED = "configured"; CONFIGURATION_REQUIRED = "configuration_required"
class DiscoveryMethod(StrEnum): AUTO_SEARCH = "auto_search"; MANUAL_URL = "manual_url"
class EligibilityStatus(StrEnum): ELIGIBLE = "eligible"; CHANNEL_OUT_OF_SCOPE = "channel_out_of_scope"; CONFIGURATION_REQUIRED = "configuration_required"; CHANNEL_UNRESOLVED = "channel_unresolved"
class AssignmentKind(StrEnum): SUBJECT = "subject"; INTERVIEWER = "interviewer"; HOLD = "hold"
class AssignmentOrigin(StrEnum): AUTO_VOICE = "auto_voice"; MANUAL = "manual"; CHANNEL_ORGANIZATION = "channel_organization"
class JobStage(StrEnum): VIDEO_METADATA = "video_metadata"; AUDIO_ACQUISITION = "audio_acquisition"; TRANSCRIPTION = "transcription"; SPEAKER_ASSIGNMENT = "speaker_assignment"; ANALYSIS_INPUT_EXTRACTION = "analysis_input_extraction"; CODEX_ANALYSIS = "codex_analysis"; ASSET_MAPPING = "asset_mapping"; HEATMAP_UPDATE = "heatmap_update"
class JobStatus(StrEnum): QUEUED = "queued"; RUNNING = "running"; PAUSE_REQUESTED = "pause_requested"; PAUSED = "paused"; CANCEL_REQUESTED = "cancel_requested"; STOPPED = "stopped"; FAILED = "failed"; RETRYING = "retrying"; SUCCEEDED = "succeeded"
class AnalysisRunStatus(StrEnum): STARTED = "started"; TRANSPORT_VALIDATED = "transport_validated"; FAILED = "failed"; ACCEPTED = "accepted"
class ScopeStatus(StrEnum): READY = "ready"; RUNNING = "running"; CURRENT = "current"; STALE = "stale"; FAILED = "failed"
class StatementType(StrEnum): FUTURE_FORECAST = "future_forecast"; CURRENT_ANALYSIS = "current_analysis"; PAST_RESULT_ANALYSIS = "past_result_analysis"; GENERAL_STATEMENT = "general_statement"
class ForecastBasis(StrEnum): DIRECT = "direct"; INFERRED = "inferred_from_subject_statements"
class ConditionKind(StrEnum): UNCONDITIONAL = "unconditional"; CONDITIONAL = "conditional"
class DirectionKind(StrEnum): STRONG_UP = "strong_up"; UP = "up"; FLAT = "flat"; DOWN = "down"; STRONG_DOWN = "strong_down"; TURNING_POINT = "turning_point"; UNKNOWN = "unknown"
class TurningPointKind(StrEnum): BOTTOM = "bottom"; TOP = "top"; OTHER = "other"
class TimeBasis(StrEnum): EXPLICIT_STATEMENT = "explicit_statement"; PUBLISHED_AT = "published_at"
class PeriodReviewDecision(StrEnum): APPROVE_UNKNOWN = "approve_unknown"; REJECT = "reject"
class Asset(StrEnum): NIKKEI_225 = "nikkei_225"; TOPIX = "topix"; SP500 = "sp500"; XAU_USD = "xau_usd"
class MappingKind(StrEnum): DIRECT = "direct"; INFERRED = "inferred"
class Confidence(StrEnum): HIGH = "high"; MEDIUM = "medium"; LOW = "low"; UNRESOLVED = "unresolved"
class MappingReviewDecision(StrEnum): APPROVE = "approve"; CORRECT = "correct"; REJECT = "reject"
class ViewRelation(StrEnum): CURRENT = "current"; CHANGED = "changed"; DISAGREEMENT = "disagreement"
class HeatmapGranularity(StrEnum): WEEK = "week"; MONTH = "month"
```

`apply_migrations` creates `schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`, sorts embedded `NNNN_name.sql` resources, runs one file per `transaction`, and records the filename without `.sql`. `0001_foundation.sql` creates `app_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)` so a fresh DB has a concrete first migration.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_database_foundation.py -q`

Expected: `2 passed`.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- pyproject.toml src/market_voice_forecast_ledger tests/backend
git commit -m "feat: add sqlite backend foundation"
```

### Task 2: 追記専用監査eventと本文混入拒否

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0002_audit.sql`
- Create: `src/market_voice_forecast_ledger/repositories/__init__.py`
- Create: `src/market_voice_forecast_ledger/repositories/audit.py`
- Create: `src/market_voice_forecast_ledger/services/__init__.py`
- Create: `src/market_voice_forecast_ledger/services/audit.py`
- Create: `tests/backend/unit/test_audit_payload.py`
- Create: `tests/backend/integration/test_audit_append_only.py`

**Interfaces:**
- Consumes: `transaction`, `canonical_json`, `utc_iso`, `DomainError`
- Produces: `AuditEventInput`
- Produces: `AuditRepository.append(event: AuditEventInput) -> int`
- Produces: `AuditRepository.list_for_entity(entity_type: str, entity_id: str) -> tuple[AuditEvent, ...]`
- Produces: `validate_audit_payload(value: object) -> None`

- [ ] **Step 1: Write failing audit tests**

```python
def test_audit_table_rejects_update_and_delete(db):
    event_id = AuditRepository(db).append(AuditEventInput.synthetic())
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("UPDATE audit_events SET reason_code='changed' WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM audit_events WHERE id=?", (event_id,))


@pytest.mark.parametrize("key", ["text_body", "input_text", "audio_path", "embedding", "prompt_body"])
def test_audit_payload_rejects_private_body_keys(key):
    with pytest.raises(DomainError) as error:
        validate_audit_payload({key: "private"})
    assert error.value.code == "AUDIT_PRIVATE_FIELD"
```

- [ ] **Step 2: Run audit tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_audit_payload.py tests/backend/integration/test_audit_append_only.py -q`

Expected: FAIL because audit migration and repository do not exist.

- [ ] **Step 3: Add exact append-only schema and service guard**

```sql
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    scope_id INTEGER,
    operation TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'system')),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
```

```python
FORBIDDEN_AUDIT_KEYS = {"text_body", "input_text", "audio_path", "embedding", "prompt_body"}

def validate_audit_payload(value: object) -> None:
    for key in walk_mapping_keys(value):
        if key in FORBIDDEN_AUDIT_KEYS:
            raise DomainError("AUDIT_PRIVATE_FIELD", f"forbidden audit key: {key}")
```

`AuditRepository` exposes no update/delete method. `append` validates both JSON payloads, serializes with `canonical_json`, and inserts inside the caller's transaction. Safe audit views may contain IDs, hashes, classifications, timestamps, short evidence, actor, and reason.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_audit_payload.py tests/backend/integration/test_audit_append_only.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0002_audit.sql src/market_voice_forecast_ledger/repositories src/market_voice_forecast_ledger/services tests/backend
git commit -m "feat: add append-only audit events"
```

### Task 3: 分析主体、正本チャンネル方針、動画metadata

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0003_sources.sql`
- Create: `src/market_voice_forecast_ledger/domain/sources.py`
- Create: `src/market_voice_forecast_ledger/repositories/sources.py`
- Create: `src/market_voice_forecast_ledger/bootstrap.py`
- Create: `tests/backend/integration/test_source_schema.py`
- Create: `tests/backend/integration/test_reference_data.py`

**Interfaces:**
- Consumes: `transaction`, `canonical_json`, `sha256_text`
- Produces: `VideoInput`, `VideoRecord`, `ChannelPolicy`, `SubjectRecord`
- Produces: `SourceRepository.upsert_video(video: VideoInput) -> int`
- Produces: `SourceRepository.create_subject(name: str, kind: SubjectKind, aliases: Sequence[str] = ()) -> int`
- Produces: `SourceRepository.create_policy(subject_id: int, policy: ChannelPolicy) -> int`
- Produces: `SourceRepository.get_video(video_id: int) -> VideoRecord`
- Produces: `SourceRepository.get_policy(subject_id: int) -> ChannelPolicy`
- Produces: `SourceRepository.get_policy_by_subject_name(name: str) -> ChannelPolicy`
- Produces: `SourceRepository.count_videos() -> int`
- Produces: `bootstrap_reference_data(conn: sqlite3.Connection) -> None`

- [ ] **Step 1: Write failing source and seed tests**

```python
def test_confirmed_channel_ids_are_seeded(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    assert repo.get_policy_by_subject_name("江守哲").youtube_channel_id == "UCVXka7buS_WptsAzSE0LcKg"
    assert repo.get_policy_by_subject_name("暁投資顧問").youtube_channel_id == "UCOfzLmXpI3qmZfV7_Cs1sYA"
    assert repo.get_policy_by_subject_name("木野内栄治").policy_kind is PolicyKind.ALL_CHANNELS
    assert repo.get_policy_by_subject_name("大川智宏").policy_kind is PolicyKind.ALL_CHANNELS


def test_video_schema_has_no_recorded_or_analysis_acquisition_date(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
    assert "published_at" in columns
    assert "recorded_at" not in columns
    assert "acquired_at" not in columns
```

- [ ] **Step 2: Run source tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_source_schema.py tests/backend/integration/test_reference_data.py -q`

Expected: FAIL because source schema and bootstrap do not exist.

- [ ] **Step 3: Add source schema without duplicate/canonical tables**

`0003_sources.sql` creates these exact tables and constraints:

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
    updated_at TEXT NOT NULL
);
CREATE TABLE videos (
    id INTEGER PRIMARY KEY,
    youtube_video_id TEXT NOT NULL UNIQUE,
    youtube_channel_id TEXT,
    channel_display_name TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
    live_kind TEXT NOT NULL CHECK (live_kind IN ('upload', 'live'))
);
CREATE TABLE subject_video_eligibility (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    discovery_method TEXT NOT NULL CHECK (discovery_method IN ('auto_search', 'manual_url')),
    status TEXT NOT NULL CHECK (status IN ('eligible', 'channel_out_of_scope', 'configuration_required', 'channel_unresolved')),
    policy_id INTEGER NOT NULL REFERENCES subject_channel_policies(id),
    policy_hash TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(subject_id, video_id)
);
```

Add CHECK logic requiring a configured fixed policy to have a `UC`-prefixed 24-character ID and an all-channel policy to have no fixed ID. Do not create `duplicate_groups`, `duplicate_group_members`, `canonical_video_id`, or an analysis-exclusion flag.

- [ ] **Step 4: Seed the four subjects and aliases idempotently**

`bootstrap_reference_data` inserts 木野内栄治 (`木野内英二` alias), 暁投資顧問 organization, 江守哲, 大川智宏 (`大川智ひろ` alias), and the four confirmed policies. Calling it twice must not create another row or change a user-modified policy.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_source_schema.py tests/backend/integration/test_reference_data.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0003_sources.sql src/market_voice_forecast_ledger/domain/sources.py src/market_voice_forecast_ledger/repositories/sources.py src/market_voice_forecast_ledger/bootstrap.py tests/backend
git commit -m "feat: add subjects channels and videos"
```

### Task 4: チャンネル適合判定、手動URL非迂回、重複非排除

**Files:**
- Create: `src/market_voice_forecast_ledger/services/channel_policy.py`
- Create: `tests/backend/unit/test_channel_policy_rules.py`
- Create: `tests/backend/integration/test_video_eligibility.py`

**Interfaces:**
- Consumes: `SourceRepository`, `ChannelPolicy`, `VideoRecord`, `AuditRepository`
- Produces: `EligibilityDecision(status, may_download_audio, may_analyze, reason)`
- Produces: `evaluate_policy(policy: ChannelPolicy, video_channel_id: str | None) -> EligibilityDecision`
- Produces: `ChannelPolicyService.evaluate(subject_id: int, video_id: int, discovery_method: DiscoveryMethod) -> EligibilityDecision`
- Produces: `ChannelPolicyService.evaluate_by_subject_name(name: str, video_id: int, discovery_method: DiscoveryMethod) -> EligibilityDecision`

- [ ] **Step 1: Write failing eligibility tests**

```python
def test_manual_url_cannot_bypass_emori_fixed_channel(db, synthetic_other_channel_video):
    decision = ChannelPolicyService(db).evaluate_by_subject_name(
        "江守哲", synthetic_other_channel_video, DiscoveryMethod.MANUAL_URL
    )
    assert decision.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE
    assert decision.may_download_audio is False
    assert decision.may_analyze is False


def test_original_clip_short_and_repost_are_independent(db, four_distinct_video_ids):
    service = ChannelPolicyService(db)
    decisions = [service.evaluate_by_subject_name("木野内栄治", video_id, DiscoveryMethod.AUTO_SEARCH)
                 for video_id in four_distinct_video_ids]
    assert [item.status for item in decisions] == [EligibilityStatus.ELIGIBLE] * 4
    assert SourceRepository(db).count_videos() == 4
```

- [ ] **Step 2: Run eligibility tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_channel_policy_rules.py tests/backend/integration/test_video_eligibility.py -q`

Expected: FAIL because channel policy service does not exist.

- [ ] **Step 3: Implement pure fail-closed policy evaluation and persistence**

```python
def evaluate_policy(policy: ChannelPolicy, video_channel_id: str | None) -> EligibilityDecision:
    if policy.configuration_status is ConfigurationStatus.CONFIGURATION_REQUIRED:
        return EligibilityDecision.blocked(EligibilityStatus.CONFIGURATION_REQUIRED, "CHANNEL_CONFIGURATION_REQUIRED")
    if video_channel_id is None:
        return EligibilityDecision.blocked(EligibilityStatus.CHANNEL_UNRESOLVED, "VIDEO_CHANNEL_UNRESOLVED")
    if policy.policy_kind is PolicyKind.ALL_CHANNELS:
        return EligibilityDecision.allowed("ALL_CHANNELS")
    if video_channel_id != policy.youtube_channel_id:
        return EligibilityDecision.blocked(EligibilityStatus.CHANNEL_OUT_OF_SCOPE, "FIXED_CHANNEL_MISMATCH")
    return EligibilityDecision.allowed("FIXED_CHANNEL_MATCH")
```

The service persists the current `(subject_id, video_id)` decision with policy ID/hash and discovery method. Display name never participates. It creates no duplicate group, canonical source, merge, or exclusion decision; every distinct YouTube video ID proceeds independently when eligible.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_channel_policy_rules.py tests/backend/integration/test_video_eligibility.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add -- src/market_voice_forecast_ledger/services/channel_policy.py tests/backend
git commit -m "feat: enforce subject channel policies"
```

### Task 5: Transcript、固定音声モデル、個人話者割当、組織割当

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0004_speakers.sql`
- Create: `src/market_voice_forecast_ledger/domain/speakers.py`
- Create: `src/market_voice_forecast_ledger/repositories/speakers.py`
- Create: `src/market_voice_forecast_ledger/services/speaker_assignment.py`
- Create: `tests/backend/unit/test_speaker_thresholds.py`
- Create: `tests/backend/integration/test_speaker_assignments.py`
- Create: `tests/backend/integration/test_akatsuki_organization_assignment.py`

**Interfaces:**
- Consumes: `SourceRepository`, `EligibilityStatus`, `sha256_text`, `transaction`
- Produces: `ScoreRule(operator: Literal['gte', 'lte'], boundary: float)`
- Produces: `SpeakerThresholdConfig(version, model_name, model_version, subject_rule, interviewer_rule)`
- Produces: `classify_raw_score(raw_score: float, config: SpeakerThresholdConfig) -> AssignmentKind`
- Produces: `SpeakerRepository.add_chunk(...) -> int`
- Produces: `SpeakerRepository.add_segment(...) -> int`
- Produces: `SpeakerRepository.get_segment(segment_id: int) -> TranscriptSegment`
- Produces: `SpeakerRepository.list_assignments(segment_ids: Sequence[int]) -> tuple[SpeakerAssignment, ...]`
- Produces: `SpeakerAssignmentService.record_personal(command: PersonalAssignmentCommand) -> SpeakerAssignment`
- Produces: `SpeakerAssignmentService.assign_organization_video(subject_id: int, video_id: int) -> tuple[int, ...]`

- [ ] **Step 1: Write failing speaker tests**

```python
def test_raw_score_is_not_normalized_and_border_band_is_hold():
    config = SpeakerThresholdConfig(
        version="synthetic-threshold-v1",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.50),
        interviewer_rule=ScoreRule("lte", 0.50),
    )
    assert classify_raw_score(1.73, config) is AssignmentKind.SUBJECT
    assert classify_raw_score(0.90, config) is AssignmentKind.HOLD
    assert classify_raw_score(0.25, config) is AssignmentKind.INTERVIEWER


def test_akatsuki_assigns_every_official_channel_segment_to_organization(db, akatsuki_video_with_three_roles):
    ids = SpeakerAssignmentService(db).assign_organization_video(
        akatsuki_video_with_three_roles.subject_id,
        akatsuki_video_with_three_roles.video_id,
    )
    rows = SpeakerRepository(db).list_assignments(ids)
    assert {row.assignment_kind for row in rows} == {AssignmentKind.SUBJECT}
    assert {row.assignment_origin for row in rows} == {AssignmentOrigin.CHANNEL_ORGANIZATION}
```

- [ ] **Step 2: Run speaker tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_speaker_thresholds.py tests/backend/integration/test_speaker_assignments.py tests/backend/integration/test_akatsuki_organization_assignment.py -q`

Expected: FAIL because speaker schema and services do not exist.

- [ ] **Step 3: Add speaker schema with one active model contract**

`0004_speakers.sql` creates:

- `transcription_chunks(video_id, chunk_no, start_ms, end_ms, input_hash, output_hash, status)` with unique video/chunk number and `start_ms < end_ms`.
- `transcript_segments(video_id, chunk_id, segment_no, start_ms, end_ms, text_body, text_sha256, anonymous_speaker_id, transcript_created_at, expires_at, text_deleted_at)` with unique video/segment number.
- `speaker_threshold_configs(version PRIMARY KEY, model_name, model_version, subject_operator, subject_boundary, interviewer_operator, interviewer_boundary, created_at, is_active)` with at most one active row enforced by a partial unique index.
- `voice_reference_profiles(subject_id, model_name, model_version, adapter_version, feature_hash, threshold_config_version, created_at, is_active)`; actual feature files and embeddings are not stored in SQLite.
- `speaker_assignments(segment_id PRIMARY KEY, assignment_kind, assigned_subject_id, assignment_origin, raw_match_score, model_name, model_version, threshold_config_version, evidence_hash, assigned_at)`.

Personal assignments require model name/version, raw score, and threshold version. Organization assignments require `assignment_origin='channel_organization'`, the organization subject ID, and NULL voice score/model fields. Do not add a normalized score column.

- [ ] **Step 4: Implement personal and organization assignment rules**

```python
def classify_raw_score(raw_score: float, config: SpeakerThresholdConfig) -> AssignmentKind:
    if config.subject_rule.matches(raw_score):
        return AssignmentKind.SUBJECT
    if config.interviewer_rule.matches(raw_score):
        return AssignmentKind.INTERVIEWER
    return AssignmentKind.HOLD
```

`record_personal` rejects a model/version mismatch with the one active threshold config and persists the computed kind, raw score, threshold version, and evidence hash. `assign_organization_video` requires an organization subject, an `eligible` video decision whose policy hash still matches, then assigns every transcript segment in that video to the organization without voice comparison.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_speaker_thresholds.py tests/backend/integration/test_speaker_assignments.py tests/backend/integration/test_akatsuki_organization_assignment.py -q`

Expected: focused tests pass, including a raw score greater than 1 and a `hold` border band.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0004_speakers.sql src/market_voice_forecast_ledger/domain/speakers.py src/market_voice_forecast_ledger/repositories/speakers.py src/market_voice_forecast_ledger/services/speaker_assignment.py tests/backend
git commit -m "feat: add speaker and organization assignments"
```

### Task 6: 決定的job manifest、checkpoint、pause・stop・retry、段階別進捗

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0005_jobs.sql`
- Create: `src/market_voice_forecast_ledger/domain/jobs.py`
- Create: `src/market_voice_forecast_ledger/repositories/jobs.py`
- Create: `src/market_voice_forecast_ledger/services/job_state.py`
- Create: `tests/backend/unit/test_job_state_machine.py`
- Create: `tests/backend/integration/test_job_checkpoints.py`
- Create: `tests/backend/integration/test_job_progress.py`

**Interfaces:**
- Produces: `JobManifest.build(units: Sequence[ManifestUnit]) -> JobManifest`
- Produces: `JobStateService.create(manifest: JobManifest) -> int`
- Produces: `JobStateService.request_pause(job_id: int) -> JobStatus`
- Produces: `JobStateService.request_stop(job_id: int) -> JobStatus`
- Produces: `JobStateService.complete_unit(job_id: int, unit_key: str, output_hash: str) -> None`
- Produces: `JobStateService.resume(job_id: int, artifact_hashes: Mapping[str, str]) -> ResumePlan`
- Produces: `JobStateService.progress(job_id: int) -> JobProgress`

- [ ] **Step 1: Write failing job and progress tests**

```python
def test_video_metadata_and_audio_are_separate_progress_stages(db):
    job_id = create_synthetic_job(db, stages=(JobStage.VIDEO_METADATA, JobStage.AUDIO_ACQUISITION))
    JobStateService(db).complete_unit(job_id, "video:one", "meta-hash")
    progress = JobStateService(db).progress(job_id)
    assert progress.stage(JobStage.VIDEO_METADATA).completed == 1
    assert progress.stage(JobStage.AUDIO_ACQUISITION).completed == 0


def test_resume_reuses_only_success_units_with_matching_artifact_hash(db, eight_chunk_job):
    mark_first_four_chunks_complete(db, eight_chunk_job)
    plan = JobStateService(db).resume(eight_chunk_job, stored_hashes_for_first_four())
    assert plan.next_unit_key == "transcription:chunk:5"
    assert plan.reused_unit_count == 4
```

- [ ] **Step 2: Run job tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_job_progress.py -q`

Expected: FAIL because job schema and state service do not exist.

- [ ] **Step 3: Add job schema and legal transitions**

`0005_jobs.sql` creates `jobs`, `job_units`, and `job_events`. `jobs` stores immutable manifest hash and total units; current job status may change only through the service. `job_units` has unique `(job_id, unit_key)`, stage, ordinal, input hash, output hash, status, attempt count, safe error code, started/finished timestamps. `job_events` is append-only with UPDATE/DELETE triggers.

```python
STAGE_ORDER = (
    JobStage.VIDEO_METADATA,
    JobStage.AUDIO_ACQUISITION,
    JobStage.TRANSCRIPTION,
    JobStage.SPEAKER_ASSIGNMENT,
    JobStage.ANALYSIS_INPUT_EXTRACTION,
    JobStage.CODEX_ANALYSIS,
    JobStage.ASSET_MAPPING,
    JobStage.HEATMAP_UPDATE,
)

LEGAL_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.STOPPED},
    JobStatus.RUNNING: {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED, JobStatus.SUCCEEDED},
    JobStatus.PAUSE_REQUESTED: {JobStatus.PAUSED},
    JobStatus.PAUSED: {JobStatus.RUNNING, JobStatus.STOPPED},
    JobStatus.CANCEL_REQUESTED: {JobStatus.STOPPED},
    JobStatus.FAILED: {JobStatus.RETRYING},
    JobStatus.RETRYING: {JobStatus.RUNNING, JobStatus.FAILED},
}
```

Unit output and success status commit in one transaction. Resume verifies both input and artifact output hashes. A pause resumes the same job after a safe boundary; a stopped job creates a successor and may reuse only verified successful units. `review_required` is a result flag, not job failure. Progress is `completed / manifest total` per stage; no synthetic timer, weighting, or ETA.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_job_progress.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0005_jobs.sql src/market_voice_forecast_ledger/domain/jobs.py src/market_voice_forecast_ledger/repositories/jobs.py src/market_voice_forecast_ledger/services/job_state.py tests/backend
git commit -m "feat: add resumable job checkpoints"
```

### Task 7: Cutoff scope、追記専用analysis run、変更不能入力snapshot

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0006_analysis_runs.sql`
- Create: `src/market_voice_forecast_ledger/domain/analysis.py`
- Create: `src/market_voice_forecast_ledger/repositories/analysis.py`
- Create: `src/market_voice_forecast_ledger/services/analysis_runs.py`
- Create: `tests/backend/integration/test_analysis_input_boundaries.py`
- Create: `tests/backend/integration/test_analysis_append_only.py`
- Create: `tests/backend/integration/test_cutoff_scopes.py`

**Interfaces:**
- Consumes: `cutoff_at_jst`, `SourceRepository`, `SpeakerRepository`, `transaction`
- Produces: `AnalysisRunSettings(model, reasoning_effort, prompt_version, schema_version, information_boundary_version)`
- Produces: `AnalysisRunSettings.required() -> AnalysisRunSettings`
- Produces: `BeginAnalysisRun(subject_id: int, cutoff_day: date, settings: AnalysisRunSettings)`
- Produces: `AnalysisRunService.begin(command: BeginAnalysisRun) -> AnalysisRun`
- Produces: `AnalysisRepository.get_input_segments(run_id: int) -> tuple[RunSegment, ...]`
- Produces: `AnalysisRepository.get_effective_run_status(run_id: int) -> AnalysisRunStatus`
- Produces: `AnalysisRepository.append_run_event(run_id: int, status: AnalysisRunStatus, error_code: str | None) -> int`

- [ ] **Step 1: Write failing cutoff and input-boundary tests**

```python
def test_person_scope_excludes_interviewer_hold_and_post_cutoff(db, personal_input_fixture):
    run = AnalysisRunService(db).begin(BeginAnalysisRun(
        personal_input_fixture.subject_id,
        date(2026, 8, 14),
        AnalysisRunSettings.required(),
    ))
    segments = AnalysisRepository(db).get_input_segments(run.id)
    assert [item.segment_id for item in segments] == personal_input_fixture.subject_segments_before_cutoff


def test_akatsuki_scope_includes_all_organization_segments(db, akatsuki_input_fixture):
    run = AnalysisRunService(db).begin(BeginAnalysisRun(
        akatsuki_input_fixture.subject_id, date(2026, 8, 14), AnalysisRunSettings.required()
    ))
    assert len(AnalysisRepository(db).get_input_segments(run.id)) == 3


def test_distinct_repost_video_segments_are_not_deduplicated(db, two_video_same_text_fixture):
    run = AnalysisRunService(db).begin(two_video_same_text_fixture.command)
    assert len(AnalysisRepository(db).get_input_segments(run.id)) == 2
```

`AnalysisRunSettings.required()` returns model `gpt-5.6-sol`, reasoning effort `max`, prompt contract version `m2-core-prompt-contract-v1`, schema version `m2-analysis-output-v1`, and information boundary version `stored-statements-only-v1`. A caller cannot override model or effort through an alternate constructor without later Task 8 rejection.

- [ ] **Step 2: Run analysis-boundary tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_analysis_append_only.py tests/backend/integration/test_cutoff_scopes.py -q`

Expected: FAIL because scope/run schema and builder do not exist.

- [ ] **Step 3: Add immutable run schema and snapshot exception trigger**

`0006_analysis_runs.sql` creates:

- `analysis_scopes(subject_id, cutoff_at_jst, status, stale_reason, UNIQUE(subject_id, cutoff_at_jst))`.
- `analysis_runs(scope_id, model, reasoning_effort, prompt_version, schema_version, information_boundary_version, input_hash, started_at)` with UPDATE/DELETE append-only triggers.
- `analysis_run_events(run_id, status, safe_error_code, created_at)` with UPDATE/DELETE append-only triggers; effective status is the newest event by ID.
- `analysis_run_segments(run_id, segment_id, ordinal, video_id, published_at, policy_id, policy_hash, assignment_kind, assigned_subject_id, assignment_updated_at, assignment_evidence_hash, UNIQUE(run_id, ordinal), UNIQUE(run_id, segment_id))` with UPDATE/DELETE triggers.
- `analysis_input_snapshots(run_id UNIQUE, input_text, metadata_json, input_sha256, snapshot_created_at, expires_at, text_deleted_at)`.

The snapshot UPDATE trigger allows only `input_text: non-NULL -> NULL` together with `text_deleted_at: NULL -> non-NULL` while every other column remains identical; it rejects all other UPDATE and every DELETE.

- [ ] **Step 4: Implement fail-closed run input construction**

`begin` derives cutoff as JST 23:59:59, reuses or creates the `(subject, cutoff)` scope, and selects only videos with `published_at <= cutoff` plus current `eligible` policy/hash. Personal subjects require current `subject` assignment to that same subject. 暁投資顧問 requires `channel_organization` assignment. It snapshots policy and assignment evidence, orders by `published_at`, YouTube video ID, segment ordinal, builds exact input text and SHA-256, then inserts a `started` run event. It never consults duplicate similarity or acquisition timestamps.

- [ ] **Step 5: Prove append-only enforcement and coexistence**

Add tests showing raw SQL UPDATE/DELETE fails for runs/events/segments, input snapshot content cannot be edited, different cutoff scopes coexist, and rerunning one scope creates a new run without changing another scope.

- [ ] **Step 6: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_analysis_append_only.py tests/backend/integration/test_cutoff_scopes.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 7: Commit Task 7**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0006_analysis_runs.sql src/market_voice_forecast_ledger/domain/analysis.py src/market_voice_forecast_ledger/repositories/analysis.py src/market_voice_forecast_ledger/services/analysis_runs.py tests/backend
git commit -m "feat: freeze cutoff analysis inputs"
```

### Task 8: Codex構造化出力contractとfail-closed run検査

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0007_analysis_outputs.sql`
- Create: `src/market_voice_forecast_ledger/services/codex_contract.py`
- Create: `tests/backend/unit/test_codex_contract.py`
- Create: `tests/backend/integration/test_analysis_output_acceptance.py`

**Interfaces:**
- Consumes: `AnalysisRepository`, `canonical_json`, `sha256_text`, Pydantic v2
- Produces: `EvidenceProposal(segment_id: int, excerpt: str)`
- Produces: `StatementProposal`
- Produces: `AnalysisEnvelope(run_id: int, statements: tuple[StatementProposal, ...])`
- Produces: `CodexRunReceipt(model, reasoning_effort, tool_call_count, boundary_mode)`
- Produces: `CodexContractService.validate_and_store(run_id: int, output_json: str, receipt: CodexRunReceipt) -> ValidatedAnalysisOutput`

- [ ] **Step 1: Write failing contract tests**

```python
@pytest.mark.parametrize(
    ("receipt", "code"),
    [
        (CodexRunReceipt("lower-model", "max", 0, "stored_statements_only"), "CODEX_MODEL_MISMATCH"),
        (CodexRunReceipt("gpt-5.6-sol", "high", 0, "stored_statements_only"), "CODEX_REASONING_MISMATCH"),
        (CodexRunReceipt("gpt-5.6-sol", "max", 1, "stored_statements_only"), "CODEX_TOOL_CALL_DETECTED"),
        (CodexRunReceipt("gpt-5.6-sol", "max", 0, "augmented"), "CODEX_BOUNDARY_MISMATCH"),
    ],
)
def test_invalid_receipt_is_rejected(db, started_run, valid_output_json, receipt, code):
    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(started_run, valid_output_json, receipt)
    assert error.value.code == code
```

- [ ] **Step 2: Run contract tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_codex_contract.py tests/backend/integration/test_analysis_output_acceptance.py -q`

Expected: FAIL because output contract and storage do not exist.

- [ ] **Step 3: Define strict Pydantic output models**

```python
class EvidenceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: int
    excerpt: str = Field(min_length=1, max_length=300)


class AssetHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str
    suggested_asset: Asset
    confidence: Confidence


class StatementProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement_type: StatementType
    forecast_basis: ForecastBasis | None
    condition_kind: ConditionKind
    condition_text: str | None
    direction_kind: DirectionKind | None
    turning_point_kind: TurningPointKind | None
    target_expression: str
    period_expression: str | None
    codex_asset_hints: tuple[AssetHint, ...] = ()
    evidence: tuple[EvidenceProposal, ...] = Field(min_length=1)


class AnalysisEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int
    statements: tuple[StatementProposal, ...]
```

The service requires the exact model, `max`, tool count 0, and `boundary_mode='stored_statements_only'`; validates JSON and run ID; rejects unknown fields and any referenced segment outside `analysis_run_segments`; stores canonical output JSON and SHA-256 in `analysis_run_outputs`; appends `transport_validated` or a safe failure event. `analysis_run_outputs` and its run foreign key are UPDATE/DELETE protected. It does not mark the run fully accepted until Tasks 9–14 validate and project the result.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_codex_contract.py tests/backend/integration/test_analysis_output_acceptance.py -q`

Expected: focused tests pass, including unknown field, malformed JSON, foreign run ID, and invented segment rejection.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 8**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0007_analysis_outputs.sql src/market_voice_forecast_ledger/services/codex_contract.py tests/backend
git commit -m "feat: validate codex analysis outputs"
```

### Task 9: 4種類の発言分類と複数の原文根拠link

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0008_statements.sql`
- Create: `src/market_voice_forecast_ledger/domain/statements.py`
- Create: `src/market_voice_forecast_ledger/repositories/statements.py`
- Create: `src/market_voice_forecast_ledger/services/statements.py`
- Create: `tests/backend/unit/test_statement_validation.py`
- Create: `tests/backend/integration/test_statement_evidence.py`

**Interfaces:**
- Consumes: `ValidatedAnalysisOutput`, `AnalysisRepository`, `SpeakerRepository`
- Produces: `NormalizedStatement`, `EvidenceLink`
- Produces: `StatementService.normalize_and_store(run_id: int) -> tuple[NormalizedStatement, ...]`
- Produces: `StatementRepository.list_run_statements(run_id: int) -> tuple[NormalizedStatement, ...]`

- [ ] **Step 1: Write failing classification and evidence tests**

```python
def test_four_statement_types_are_distinct(db, output_with_four_types):
    rows = StatementService(db).normalize_and_store(output_with_four_types.run_id)
    assert {row.statement_type for row in rows} == {
        StatementType.FUTURE_FORECAST,
        StatementType.CURRENT_ANALYSIS,
        StatementType.PAST_RESULT_ANALYSIS,
        StatementType.GENERAL_STATEMENT,
    }


def test_one_statement_can_link_ordered_subject_segments(db, two_segment_forecast_output):
    row = StatementService(db).normalize_and_store(two_segment_forecast_output.run_id)[0]
    assert [link.segment_id for link in row.evidence_links] == two_segment_forecast_output.segment_ids


def test_free_summary_not_present_in_transcript_is_rejected(db, invented_excerpt_output):
    with pytest.raises(DomainError) as error:
        StatementService(db).normalize_and_store(invented_excerpt_output.run_id)
    assert error.value.code == "EVIDENCE_NOT_CONTIGUOUS_SOURCE_TEXT"
```

- [ ] **Step 2: Run statement tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_statement_validation.py tests/backend/integration/test_statement_evidence.py -q`

Expected: FAIL because normalized statement schema and service do not exist.

- [ ] **Step 3: Add immutable run statement schema**

`0008_statements.sql` creates `analysis_statements` with run ID, source video ID, four-value statement type, nullable forecast basis, condition kind/text, direction, turning-point subtype, original target expression, original period expression, and created timestamp. It creates `analysis_statement_evidence_links(statement_id, ordinal, run_segment_id, excerpt, start_ms, end_ms, UNIQUE(statement_id, ordinal), UNIQUE(statement_id, run_segment_id))`. Both tables reject UPDATE/DELETE.

- [ ] **Step 4: Implement semantic and evidence validation**

```python
def validate_statement(proposal: StatementProposal) -> None:
    if proposal.statement_type is StatementType.FUTURE_FORECAST and proposal.forecast_basis is None:
        raise DomainError("FORECAST_BASIS_REQUIRED", "future forecast requires a basis")
    if proposal.statement_type is StatementType.FUTURE_FORECAST and proposal.direction_kind is None:
        raise DomainError("FORECAST_DIRECTION_REQUIRED", "future forecast requires a direction")
    if proposal.statement_type is not StatementType.FUTURE_FORECAST and proposal.forecast_basis is not None:
        raise DomainError("FORECAST_BASIS_NOT_ALLOWED", "non-forecast cannot have a forecast basis")
    if proposal.condition_kind is ConditionKind.CONDITIONAL and not proposal.condition_text:
        raise DomainError("CONDITION_TEXT_REQUIRED", "conditional forecast requires text")
    if proposal.direction_kind is DirectionKind.TURNING_POINT and proposal.turning_point_kind is None:
        raise DomainError("TURNING_POINT_KIND_REQUIRED", "turning point subtype required")
```

`analysis_statements.direction_kind` is nullable for current analysis, past-result analysis, and general statements. A non-future statement may retain an observed direction for display, but it remains ineligible for forecast projection.

For every evidence link, load the exact run segment and current retained transcript text, require `excerpt in text_body`, preserve proposal order, and require at most 300 Unicode code points. Personal run evidence must be a subject assignment for that person; organization run evidence must be a `channel_organization` assignment. The service stores all four statement types, but marks only future forecasts as downstream heatmap candidates.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_statement_validation.py tests/backend/integration/test_statement_evidence.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 9**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0008_statements.sql src/market_voice_forecast_ledger/domain/statements.py src/market_voice_forecast_ledger/repositories/statements.py src/market_voice_forecast_ledger/services/statements.py tests/backend
git commit -m "feat: store classified statements and evidence"
```

### Task 10: 明示・公開日基準・時期不明の期間とreview

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0009_periods.sql`
- Create: `src/market_voice_forecast_ledger/domain/periods.py`
- Create: `src/market_voice_forecast_ledger/repositories/periods.py`
- Create: `src/market_voice_forecast_ledger/services/periods.py`
- Create: `tests/backend/unit/test_period_normalization.py`
- Create: `tests/backend/integration/test_period_reviews.py`

**Interfaces:**
- Consumes: `NormalizedStatement`, source video `published_at`, `AuditRepository`
- Produces: `NormalizedPeriod(start_date, end_date, time_basis, source_expression, is_unknown)`
- Produces: `normalize_period(expression: str | None, published_at: datetime) -> NormalizedPeriod`
- Produces: `PeriodService.normalize_run(run_id: int) -> tuple[NormalizedPeriod, ...]`
- Produces: `PeriodReviewService.review(period_id: int, decision: PeriodReviewDecision, actor: str, reason: str) -> int`
- Produces: `PeriodReviewService.effective(period_id: int) -> EffectivePeriodReview | None`

- [ ] **Step 1: Write failing period tests**

```python
def test_month_first_week_contains_month_first_day_and_may_cross_month():
    result = normalize_period("2026年9月第1週", datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result.start_date == date(2026, 8, 31)
    assert result.end_date == date(2026, 9, 6)
    assert result.time_basis is TimeBasis.EXPLICIT_STATEMENT


def test_relative_next_week_uses_published_at_in_jst():
    result = normalize_period("来週", datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc))
    assert result.start_date == date(2026, 8, 17)
    assert result.end_date == date(2026, 8, 23)
    assert result.time_basis is TimeBasis.PUBLISHED_AT


def test_unknown_period_is_not_eligible_until_approved(db, unknown_period):
    assert PeriodReviewService(db).effective(unknown_period.id) is None
    PeriodReviewService(db).review(unknown_period.id, PeriodReviewDecision.APPROVE_UNKNOWN, "user", "時期不明列で表示")
    assert PeriodReviewService(db).effective(unknown_period.id).approved_for_unknown_column is True
```

- [ ] **Step 2: Run period tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_period_normalization.py tests/backend/integration/test_period_reviews.py -q`

Expected: FAIL because period schema and normalizer do not exist.

- [ ] **Step 3: Implement deterministic period normalization**

```python
def first_week_of_month(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    monday = first - timedelta(days=first.weekday())
    return monday, monday + timedelta(days=6)


def relative_week(published_at: datetime, offset: int) -> tuple[date, date]:
    local_day = published_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
    monday = local_day - timedelta(days=local_day.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


def add_months(day: date, offset: int) -> date:
    month_index = day.year * 12 + day.month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))
```

Support exact explicit `YYYY年`, `YYYY年M月`, `YYYY年M月第1週`, and exact relative `今週`, `来週`, `再来週`, `今月`, `来月`, `再来月`, `半年後`. Explicit forms use `explicit_statement`; relative forms use `published_at` and store the actual source timestamp. Expressions such as `しばらく`, `当面`, `近いうち`, missing periods, and unsupported parses return `is_unknown=True` without invented dates.

- [ ] **Step 4: Add append-only period storage and review history**

`0009_periods.sql` creates immutable `analysis_statement_periods(statement_id UNIQUE, source_expression, start_date, end_date, time_basis, basis_published_at, is_unknown)` and append-only `period_reviews(period_id, decision CHECK approve_unknown/reject, actor, reason, created_at)`, each with UPDATE/DELETE triggers. The newest review ID is effective; it never rewrites the calculated unknown row. Reject requires exclusion; approve routes only to the special unknown column.

`PeriodReviewService.review` inserts the review and a safe `audit_events` record with period ID, decision, actor, and reason in one transaction. A failure in either insert rolls back both.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_period_normalization.py tests/backend/integration/test_period_reviews.py -q`

Expected: focused tests pass, including explicit year vs published-date labeling and append-only review rejection of raw UPDATE/DELETE.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 10**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0009_periods.sql src/market_voice_forecast_ledger/domain/periods.py src/market_voice_forecast_ledger/repositories/periods.py src/market_voice_forecast_ledger/services/periods.py tests/backend
git commit -m "feat: normalize and review forecast periods"
```

### Task 11: 指数割当規則、直接・推定、Codex信頼度上限

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0010_asset_mappings.sql`
- Create: `src/market_voice_forecast_ledger/domain/mappings.py`
- Create: `src/market_voice_forecast_ledger/repositories/mappings.py`
- Create: `src/market_voice_forecast_ledger/services/asset_mapping.py`
- Create: `tests/backend/unit/test_asset_mapping_rules.py`
- Create: `tests/backend/integration/test_asset_mapping_storage.py`

**Interfaces:**
- Consumes: `NormalizedStatement`, `StatementContext`, Codex asset hints
- Produces: `AssetMapping(asset, mapping_kind, reason_code, codex_confidence, rule_confidence, final_confidence, rule_evidence)`
- Produces: `min_confidence(left: Confidence, right: Confidence) -> Confidence`
- Produces: `map_statement(statement: NormalizedStatement, context: StatementContext) -> tuple[AssetMapping, ...]`
- Produces: `AssetMappingService.map_run(run_id: int) -> tuple[AssetMapping, ...]`
- Produces: `MappingRepository.list_run_mappings(run_id: int) -> tuple[AssetMapping, ...]`

- [ ] **Step 1: Write failing deterministic mapping tests**

```python
def test_japanese_equity_expression_maps_to_two_inferred_assets():
    mappings = map_statement(synthetic_statement("日本株", context_market="japan"))
    assert {(row.asset, row.mapping_kind) for row in mappings} == {
        (Asset.NIKKEI_225, MappingKind.INFERRED),
        (Asset.TOPIX, MappingKind.INFERRED),
    }


def test_interviewer_only_market_hint_cannot_raise_personal_confidence():
    mappings = map_statement(synthetic_statement(
        "株式市場", subject_market=None, interviewer_market="us", subject_kind=SubjectKind.PERSON
    ))
    assert mappings[0].final_confidence is Confidence.UNRESOLVED


def test_absent_gold_statement_creates_no_xau_mapping():
    mappings = map_statement(synthetic_statement("米国株", context_market="us"))
    assert Asset.XAU_USD not in {row.asset for row in mappings}
```

- [ ] **Step 2: Run mapping tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_asset_mapping_storage.py -q`

Expected: FAIL because mapping types, rules, and schema do not exist.

- [ ] **Step 3: Implement app-rule evidence and confidence ceiling**

```python
CONFIDENCE_ORDER = {
    Confidence.UNRESOLVED: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}

def min_confidence(left: Confidence, right: Confidence) -> Confidence:
    return left if CONFIDENCE_ORDER[left] <= CONFIDENCE_ORDER[right] else right
```

Rules are applied only to stored adopted-subject context:

- Exact `日経平均`, `TOPIX`, `S&P 500`, `金`/`XAU/USD` references create `direct` mappings; application confidence is high unless adopted-subject statements contain a competing market.
- `日本株` creates inferred Nikkei 225 and TOPIX candidates; `米国株` creates inferred S&P 500; explicit market with no competitor may be high.
- Generic `株式市場` is medium only when surrounding adopted-subject statements consistently identify one market, low when a candidate remains but competition exists, unresolved when the market cannot be selected.
- Personal interviewer context may lower or explain unresolved status but may not produce high/medium. 暁投資顧問's eligible organization-assigned statements are adopted-subject statements, regardless of speaker role.
- Final confidence is the lower of Codex and application confidence, and a disagreement flag is stored when they differ.

- [ ] **Step 4: Add immutable mapping schema**

`0010_asset_mappings.sql` creates `analysis_asset_mappings(run_id, statement_id, original_expression, asset, mapping_kind, conversion_reason, codex_confidence, rule_confidence, final_confidence, confidence_disagrees, rule_evidence_json, source_video_id)` with one row per statement/asset and UPDATE/DELETE triggers. `rule_evidence_json` stores only segment IDs, evidence kind, market codes, and boolean competition results, not transcript bodies.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_asset_mapping_storage.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 11**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0010_asset_mappings.sql src/market_voice_forecast_ledger/domain/mappings.py src/market_voice_forecast_ledger/repositories/mappings.py src/market_voice_forecast_ledger/services/asset_mapping.py tests/backend
git commit -m "feat: apply auditable asset mapping rules"
```

### Task 12: Low・unresolved指数割当の承認・修正・却下

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0011_mapping_reviews.sql`
- Create: `src/market_voice_forecast_ledger/services/mapping_review.py`
- Create: `tests/backend/integration/test_mapping_reviews.py`

**Interfaces:**
- Consumes: `MappingRepository`, `AuditRepository`, `transaction`
- Produces: `MappingReviewCommand(mapping_id, decision, actor, reason, corrected_asset)`
- Produces: `MappingReviewService.review(command: MappingReviewCommand) -> int`
- Produces: `MappingReviewService.effective(mapping_id: int) -> EffectiveMappingDecision`

- [ ] **Step 1: Write failing mapping-review tests**

```python
def test_low_mapping_is_ineligible_until_review(db, low_mapping):
    before = MappingReviewService(db).effective(low_mapping.id)
    assert before.heatmap_eligible is False
    MappingReviewService(db).review(MappingReviewCommand(
        low_mapping.id, MappingReviewDecision.APPROVE, "user", "周辺発言を確認", None
    ))
    assert MappingReviewService(db).effective(low_mapping.id).heatmap_eligible is True


def test_correct_requires_new_asset_and_reason(db, unresolved_mapping):
    with pytest.raises(DomainError) as error:
        MappingReviewService(db).review(MappingReviewCommand(
            unresolved_mapping.id, MappingReviewDecision.CORRECT, "user", "", None
        ))
    assert error.value.code == "MAPPING_REVIEW_INVALID"
```

- [ ] **Step 2: Run mapping-review tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_mapping_reviews.py -q`

Expected: FAIL because mapping review schema and service do not exist.

- [ ] **Step 3: Add append-only review history and effective decision**

`0011_mapping_reviews.sql` creates `mapping_reviews(mapping_id, decision CHECK approve/correct/reject, actor, reason, before_asset, after_asset, created_at)` with UPDATE/DELETE triggers. `approve` keeps the calculated asset, `correct` requires a different valid asset, and `reject` makes it ineligible. A non-empty reason is mandatory. Latest review ID is effective; earlier events remain. The calculated mapping and confidence never change.

`MappingReviewService.review` inserts the review and a safe `audit_events` record with mapping ID, before/after asset, decision, actor, and reason in one transaction. A failure in either insert rolls back both.

```python
def effective(self, mapping_id: int) -> EffectiveMappingDecision:
    mapping = self._mappings.get(mapping_id)
    review = self._reviews.latest(mapping_id)
    if mapping.final_confidence in {Confidence.HIGH, Confidence.MEDIUM} and review is None:
        return EffectiveMappingDecision(mapping.asset, True, "AUTO_CONFIDENCE")
    if review is None:
        return EffectiveMappingDecision(mapping.asset, False, "REVIEW_REQUIRED")
    return apply_review(mapping, review)
```

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_mapping_reviews.py -q`

Expected: focused tests pass and raw SQL UPDATE/DELETE of a review fails with `APPEND_ONLY`.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 12**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0011_mapping_reviews.sql src/market_voice_forecast_ledger/services/mapping_review.py tests/backend
git commit -m "feat: review low confidence asset mappings"
```

### Task 13: 将来予想候補、見解相違・変更、現在候補選択

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0012_forecast_projections.sql`
- Create: `src/market_voice_forecast_ledger/domain/forecasts.py`
- Create: `src/market_voice_forecast_ledger/repositories/forecasts.py`
- Create: `src/market_voice_forecast_ledger/services/forecast_projection.py`
- Create: `tests/backend/unit/test_forecast_selection.py`
- Create: `tests/backend/integration/test_forecast_projection.py`

**Interfaces:**
- Consumes: statement, period, effective mapping, source video, review services
- Produces: `ForecastCandidate`, `ProjectedForecast`, `ForecastDirectionEvidence`
- Produces: `select_current(candidates: Sequence[ForecastCandidate]) -> ProjectedForecast`
- Produces: `ForecastProjectionService.project_run(run_id: int, trigger_kind: ProjectionTrigger) -> ForecastProjectionBatch`
- Produces: `ForecastRepository.list_batch_forecasts(batch_id: int) -> tuple[ProjectedForecast, ...]`

- [ ] **Step 1: Write failing projection tests**

```python
def test_non_future_statement_never_becomes_forecast():
    candidates = build_candidates(statement_type=StatementType.CURRENT_ANALYSIS)
    assert candidates == ()


def test_same_video_opposite_directions_form_disagreement_even_if_one_is_direct():
    result = select_current(same_video_candidates(DirectionKind.UP, DirectionKind.DOWN, bases=(ForecastBasis.DIRECT, ForecastBasis.INFERRED)))
    assert result.view_relation is ViewRelation.DISAGREEMENT
    assert set(result.directions) == {DirectionKind.UP, DirectionKind.DOWN}


def test_later_opposite_video_changes_current_view():
    result = select_current(original_down_then_later_up())
    assert result.primary_direction is DirectionKind.UP
    assert result.view_relation is ViewRelation.CHANGED


def test_same_direction_repost_increases_independent_evidence_count():
    result = select_current(original_up_then_repost_up())
    assert result.evidence_count == 2
```

- [ ] **Step 2: Run forecast tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_forecast_projection.py -q`

Expected: FAIL because forecast projection does not exist.

- [ ] **Step 3: Implement candidate gating and conflict preservation**

Only `future_forecast` statements with an effective eligible mapping enter. Conditional and unconditional candidates are grouped separately. Unknown period requires latest `approve_unknown`; rejected or unreviewed unknown is excluded. `low`/`unresolved` mapping requires an effective approve/correct review. Turning points remain turning points, and `unknown` does not become flat.

```python
def candidate_rank(candidate: ForecastCandidate) -> tuple[datetime, int, int]:
    return (
        candidate.published_at,
        1 if candidate.forecast_basis is ForecastBasis.DIRECT else 0,
        candidate.period_specificity,
    )
```

Before ranking videos, coalesce opposing directions in the same video, asset, effective period/unknown column, and condition layer into one `disagreement` candidate retaining every direction and evidence link. Compare that candidate with other videos by newest `published_at`, then direct basis, then period specificity. Use YouTube video ID and immutable statement ID only as a stable serialization tiebreaker after all semantic priorities are equal; keep every tied alternative as counterevidence. A later selected video with an opposing earlier direction is `changed`. Do not average any conflict to flat.

Set `period_specificity` to exact day 4, named week 3, calendar month 2, calendar year 1, unknown 0. After selecting the current direction or disagreement set, count every independent eligible evidence link supporting those selected directions across original videos, clips, Shorts, and reposts; retain opposing older evidence as counterevidence rather than adding it to the supporting count.

- [ ] **Step 4: Add immutable run forecast projection schema**

`0012_forecast_projections.sql` creates append-only `forecast_projection_batches(run_id, trigger_kind CHECK initial/mapping_review/period_review, latest_mapping_review_id, latest_period_review_id, created_at)`, `analysis_forecasts(projection_batch_id, run_id, asset, mapping_kind, period_start, period_end, unknown_period, condition_kind, condition_text, view_relation, primary_direction, directions_json, confidence, evidence_count, selected_published_at, stable_selection_key, heatmap_eligible, exclusion_reason)`, and `analysis_forecast_statement_links(forecast_id, statement_id, relation_kind)`. All three reject UPDATE/DELETE. `directions_json` is canonical and contains at least two distinct directions for disagreement. Each projection call creates a new batch and never edits an earlier batch, so a later review can deterministically reproject the same Codex run.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_forecast_projection.py -q`

Expected: focused tests pass, including conditional separation, turning point preservation, empty XAU/USD, and no duplicate suppression.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 13**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0012_forecast_projections.sql src/market_voice_forecast_ledger/domain/forecasts.py src/market_voice_forecast_ledger/repositories/forecasts.py src/market_voice_forecast_ledger/services/forecast_projection.py tests/backend
git commit -m "feat: project current forecast candidates"
```

### Task 14: 検証済みrunから現在結果を原子的に置換

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0013_current_results.sql`
- Create: `src/market_voice_forecast_ledger/services/current_results.py`
- Create: `src/market_voice_forecast_ledger/services/review_application.py`
- Create: `tests/backend/integration/test_atomic_result_replacement.py`
- Create: `tests/backend/integration/test_current_scope_isolation.py`
- Create: `tests/backend/integration/test_review_application.py`

**Interfaces:**
- Consumes: `AnalysisRepository`, `StatementRepository`, `MappingRepository`, `ForecastRepository`, `AuditRepository`, `transaction`
- Produces: `CurrentResultService.replace_scope(run_id: int, projection_batch_id: int) -> CurrentResultSummary`
- Produces: `CurrentResultService.get_scope(scope_id: int) -> CurrentResultSummary`
- Produces: `ReviewApplicationService.apply_mapping(command: MappingReviewCommand) -> ReviewApplicationResult`
- Produces: `ReviewApplicationService.apply_period(period_id: int, decision: PeriodReviewDecision, actor: str, reason: str) -> ReviewApplicationResult`

- [ ] **Step 1: Write failing atomic replacement tests**

```python
def test_failed_replacement_leaves_previous_current_result(db, current_scope, accepted_candidate_run, monkeypatch):
    before = CurrentResultService(db).get_scope(current_scope.id)
    monkeypatch.setattr(CurrentResultService, "_copy_forecasts", Mock(side_effect=sqlite3.OperationalError("synthetic")))
    with pytest.raises(sqlite3.OperationalError):
        CurrentResultService(db).replace_scope(accepted_candidate_run.id, accepted_candidate_run.projection_batch_id)
    assert CurrentResultService(db).get_scope(current_scope.id) == before


def test_replacing_one_cutoff_does_not_change_another(db, two_cutoff_scopes, replacement_run):
    other_before = CurrentResultService(db).get_scope(two_cutoff_scopes.other.id)
    CurrentResultService(db).replace_scope(replacement_run.id, replacement_run.projection_batch_id)
    assert CurrentResultService(db).get_scope(two_cutoff_scopes.other.id) == other_before


def test_review_after_acceptance_reprojects_without_new_codex_run(db, accepted_scope_with_low_mapping):
    run_count_before = AnalysisRepository(db).count_runs(accepted_scope_with_low_mapping.scope_id)
    result = ReviewApplicationService(db).apply_mapping(accepted_scope_with_low_mapping.approve_command)
    assert result.applied_to_current is True
    assert AnalysisRepository(db).count_runs(accepted_scope_with_low_mapping.scope_id) == run_count_before
    assert result.current_summary.eligible_mapping_count == 1
```

- [ ] **Step 2: Run replacement tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_current_scope_isolation.py tests/backend/integration/test_review_application.py -q`

Expected: FAIL because current projection tables and service do not exist.

- [ ] **Step 3: Add current projection tables**

`0013_current_results.sql` creates `current_statements(scope_id, analysis_statement_id, source_run_id)`, `current_asset_mappings(scope_id, analysis_mapping_id, source_run_id, effective_asset, effective_eligibility)`, and `current_forecasts(scope_id, analysis_forecast_id, source_run_id, projection_batch_id)`. Keys are unique within a scope; foreign keys point to immutable run results and a projection batch. Current tables are replaceable only through `CurrentResultService`; direct repository mutation methods stay private.

- [ ] **Step 4: Implement one-transaction replacement and run acceptance**

```python
def replace_scope(self, run_id: int, projection_batch_id: int) -> CurrentResultSummary:
    self._validate_complete_projection(run_id, projection_batch_id)
    run = self._analysis.get_run(run_id)
    with transaction(self._conn):
        before = self._current.safe_summary(run.scope_id)
        self._current.delete_scope_rows(run.scope_id)
        self._copy_statements(run_id, run.scope_id)
        self._copy_mappings(run_id, run.scope_id)
        self._copy_forecasts(projection_batch_id, run.scope_id)
        self._analysis.set_scope_current(run.scope_id)
        if self._analysis.get_effective_run_status(run_id) is not AnalysisRunStatus.ACCEPTED:
            self._analysis.append_run_event(run_id, AnalysisRunStatus.ACCEPTED, None)
        after = self._current.safe_summary(run.scope_id)
        self._audit.append(AuditEventInput.result_replaced(run.scope_id, before, after))
    return after
```

Validation requires stored transport output, normalized statements/evidence, periods, mappings, and forecasts for that run, plus no failure event. Audit summaries contain IDs, hashes, counts, and classifications only. Any validation or DB failure leaves the old current rows and prior scope status unchanged.

- [ ] **Step 5: Apply reviews to an accepted current run without rerunning Codex**

`ReviewApplicationService` validates and appends the mapping or period review plus its audit event. If the reviewed item belongs to the currently accepted run for its scope, the same outer transaction creates a new `forecast_projection_batch` using the latest review IDs and calls the transaction-internal current replacement method. If it belongs to an older run, it records the review but returns `applied_to_current=False`. A projection or replacement failure rolls back the review, audit event, projection batch, and current changes together.

- [ ] **Step 6: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_current_scope_isolation.py tests/backend/integration/test_review_application.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 7: Commit Task 14**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0013_current_results.sql src/market_voice_forecast_ledger/services/current_results.py src/market_voice_forecast_ledger/services/review_application.py tests/backend
git commit -m "feat: replace current results atomically"
```

### Task 15: 話者・チャンネル方針修正、監査、依存scope stale化

**Files:**
- Create: `src/market_voice_forecast_ledger/services/corrections.py`
- Modify: `src/market_voice_forecast_ledger/repositories/sources.py`
- Modify: `src/market_voice_forecast_ledger/repositories/speakers.py`
- Modify: `src/market_voice_forecast_ledger/repositories/analysis.py`
- Create: `tests/backend/integration/test_speaker_corrections.py`
- Create: `tests/backend/integration/test_channel_policy_corrections.py`
- Create: `tests/backend/integration/test_stale_transitions.py`

**Interfaces:**
- Consumes: `AuditRepository`, source/speaker/analysis repositories, `ChannelPolicyService`, `transaction`
- Produces: `SpeakerCorrectionService.correct(command: SpeakerCorrection) -> SpeakerAssignment`
- Produces: `ChannelPolicyCorrectionService.change(command: ChannelPolicyChange) -> ChannelPolicy`
- Produces: `AnalysisRepository.mark_scopes_using_segment_stale(segment_id: int, reason: str) -> tuple[int, ...]`
- Produces: `AnalysisRepository.mark_scopes_using_policy_stale(policy_id: int, reason: str) -> tuple[int, ...]`

- [ ] **Step 1: Write failing correction and stale tests**

```python
def test_speaker_correction_audits_and_marks_dependent_scope_stale(db, analyzed_segment):
    result = SpeakerCorrectionService(db).correct(SpeakerCorrection(
        analyzed_segment.segment_id,
        AssignmentKind.HOLD,
        None,
        "user",
        "声が一致しないため保留",
    ))
    assert result.assignment_kind is AssignmentKind.HOLD
    assert AnalysisRepository(db).get_scope(analyzed_segment.scope_id).status is ScopeStatus.STALE
    assert AuditRepository(db).list_for_entity("speaker_assignment", str(analyzed_segment.segment_id))[-1].operation == "correct"


def test_channel_change_reevaluates_videos_without_deleting_old_results(db, analyzed_policy):
    before = CurrentResultService(db).get_scope(analyzed_policy.scope_id)
    ChannelPolicyCorrectionService(db).change(analyzed_policy.change_to_other_fixed_id())
    assert CurrentResultService(db).get_scope(analyzed_policy.scope_id) == before
    assert AnalysisRepository(db).get_scope(analyzed_policy.scope_id).status is ScopeStatus.STALE
```

- [ ] **Step 2: Run correction tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_speaker_corrections.py tests/backend/integration/test_channel_policy_corrections.py tests/backend/integration/test_stale_transitions.py -q`

Expected: FAIL because correction services and stale queries do not exist.

- [ ] **Step 3: Implement speaker correction transaction**

```python
def correct(self, command: SpeakerCorrection) -> SpeakerAssignment:
    if not command.reason.strip():
        raise DomainError("CORRECTION_REASON_REQUIRED", "speaker correction requires a reason")
    with transaction(self._conn):
        before = self._speakers.get_assignment(command.segment_id)
        after = self._speakers.replace_current(command)
        self._audit.append(AuditEventInput.change(
            "speaker_assignment", str(command.segment_id), "correct", command.actor,
            "SPEAKER_CORRECTION", command.reason, before.audit_view(), after.audit_view()
        ))
        self._analysis.mark_scopes_using_segment_stale(command.segment_id, "SPEAKER_ASSIGNMENT_CHANGED")
    return after
```

The service never rewrites historical run segment snapshots or old run output. It leaves old current forecasts visible with a stale status until a later accepted run replaces them.

- [ ] **Step 4: Implement channel policy change transaction**

Validate fixed IDs with the same schema rule, update the one current policy, audit safe before/after JSON, reevaluate every known subject/video eligibility, stop queued or not-yet-started audio/transcription units newly out of scope, and mark every scope that used the prior policy hash stale. Existing runs, current forecasts, and audit events remain. A per-video approval cannot override `channel_out_of_scope`.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_speaker_corrections.py tests/backend/integration/test_channel_policy_corrections.py tests/backend/integration/test_stale_transitions.py -q`

Expected: focused tests pass, including transaction rollback when audit append is forced to fail.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 15**

```powershell
git add -- src/market_voice_forecast_ledger/services/corrections.py src/market_voice_forecast_ledger/repositories tests/backend
git commit -m "feat: audit corrections and stale scopes"
```

### Task 16: 再生成可能な週・月heatmap cache、16行、時期不明列

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0014_heatmap.sql`
- Create: `src/market_voice_forecast_ledger/repositories/heatmap.py`
- Create: `src/market_voice_forecast_ledger/services/heatmap.py`
- Modify: `src/market_voice_forecast_ledger/services/current_results.py`
- Modify: `src/market_voice_forecast_ledger/services/review_application.py`
- Create: `tests/backend/integration/test_heatmap_cache.py`
- Create: `tests/backend/integration/test_heatmap_unknown_and_disagreement.py`

**Interfaces:**
- Consumes: current forecast projections, subjects, assets, periods
- Produces: `HeatmapService.rebuild_scope(scope_id: int) -> int`
- Produces: `HeatmapService.read_cutoff(cutoff_day: date, granularity: HeatmapGranularity) -> HeatmapView`
- Produces: `HeatmapView.rows: tuple[HeatmapRow, ...]`
- Produces: `HeatmapCell(direction, directions, mapping_kind, confidence, evidence_count, conditional, view_relation, unknown_period)`

- [ ] **Step 1: Write failing heatmap tests**

```python
def test_heatmap_always_has_four_subjects_by_four_assets(db, four_subject_scopes_at_same_cutoff):
    view = HeatmapService(db).read_cutoff(date(2026, 8, 14), HeatmapGranularity.WEEK)
    assert len(view.rows) == 16
    assert {(row.subject_id, row.asset) for row in view.rows} == expected_subject_asset_pairs(db)


def test_approved_unknown_and_disagreement_keep_distinct_display_state(db, scope_with_unknown_and_disagreement):
    view = HeatmapService(db).read_cutoff(date(2026, 8, 14), HeatmapGranularity.MONTH)
    unknown = view.cell("synthetic_subject", Asset.TOPIX, "unknown")
    assert unknown.unknown_period is True
    conflict = view.cell("synthetic_subject", Asset.NIKKEI_225, "2026-09")
    assert conflict.view_relation is ViewRelation.DISAGREEMENT
    assert set(conflict.directions) == {DirectionKind.UP, DirectionKind.DOWN}
```

- [ ] **Step 2: Run heatmap tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_heatmap_cache.py tests/backend/integration/test_heatmap_unknown_and_disagreement.py -q`

Expected: FAIL because heatmap schema and service do not exist.

- [ ] **Step 3: Add rebuildable cache schema and projection**

`0014_heatmap.sql` creates `heatmap_cells(scope_id, subject_id, asset, granularity CHECK week/month, period_key, period_start, period_end, condition_kind, primary_direction, directions_json, mapping_kind, confidence, evidence_count, view_relation, unknown_period, source_forecast_id, UNIQUE(scope_id, subject_id, asset, granularity, period_key, condition_kind))`.

`rebuild_scope` deletes only one subject/cutoff scope's cache and repopulates from its `current_forecasts` in the caller's transaction. `read_cutoff` finds the current scope for each of the four active MVP subjects at the selected JST cutoff and combines them into one 16-row view; a missing subject scope becomes four empty asset rows, not an `unknown` forecast. Week cells use Monday–Sunday absolute keys; month cells use calendar month keys. An approved unknown period uses key `unknown` in both granularities. Unapproved/rejected unknown, non-future statements, and ineligible mappings create no cell. A disagreement retains multiple directions.

- [ ] **Step 4: Make current replacement and cache rebuild atomic**

Modify the transaction-internal current replacement path so `_heatmap.rebuild_scope(scope_id)` runs after current rows are copied and before scope status, run acceptance event, audit event, and commit. `ReviewApplicationService` uses the same path, so a post-analysis mapping/period review updates current results and heatmap in the same transaction. An injected heatmap failure must rollback the review when present, projection batch, current rows, cache, run event, and audit events.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_heatmap_cache.py tests/backend/integration/test_heatmap_unknown_and_disagreement.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_review_application.py -q`

Expected: focused tests pass and deleting all cache rows followed by rebuild produces the same serialized view.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 16**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0014_heatmap.sql src/market_voice_forecast_ledger/repositories/heatmap.py src/market_voice_forecast_ledger/services/heatmap.py src/market_voice_forecast_ledger/services/current_results.py src/market_voice_forecast_ledger/services/review_application.py tests/backend
git commit -m "feat: build comparable forecast heatmaps"
```

### Task 17: 本文保持・削除previewと専用folder限定音声清掃

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0015_retention.sql`
- Create: `src/market_voice_forecast_ledger/repositories/retention.py`
- Create: `src/market_voice_forecast_ledger/services/retention.py`
- Create: `tests/backend/unit/test_retention_policy.py`
- Create: `tests/backend/integration/test_text_deletion.py`
- Create: `tests/backend/integration/test_audio_cleanup.py`

**Interfaces:**
- Consumes: `Settings.temp_audio_dir`, transcript/input repositories, `AuditRepository`, `JobStateService`
- Produces: `RetentionService.preview_text_deletion(command: DeleteTextCommand) -> DeletionPreview`
- Produces: `RetentionService.delete_text(command: DeleteTextCommand) -> DeletionResult`
- Produces: `RetentionService.purge_expired(now: datetime) -> DeletionResult`
- Produces: `RetentionService.delete_audio(artifact_id: int) -> AudioDeletionResult`
- Produces: `is_safe_audio_path(root: Path, candidate: Path) -> bool`

- [ ] **Step 1: Write failing retention and safe-path tests**

```python
@pytest.mark.parametrize("days", [30, 90, 180, 365, None])
def test_supported_retention_values(days):
    assert RetentionPolicy(days).days == days


def test_text_deletion_keeps_hash_current_forecast_and_short_evidence(db, expired_analyzed_text):
    result = RetentionService(db, expired_analyzed_text.settings).purge_expired(expired_analyzed_text.now)
    segment = SpeakerRepository(db).get_segment(expired_analyzed_text.segment_id)
    assert segment.text_body is None
    assert segment.text_sha256 == expired_analyzed_text.text_sha256
    assert CurrentResultService(db).get_scope(expired_analyzed_text.scope_id).forecast_count == 1
    assert result.deleted_transcript_count == 1


def test_audio_path_outside_dedicated_folder_is_refused_and_not_deleted(db, settings, tmp_path):
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"synthetic")
    artifact_id = RetentionRepository(db).add_audio_artifact(outside)
    result = RetentionService(db, settings).delete_audio(artifact_id)
    assert result.error_code == "AUDIO_PATH_OUTSIDE_TEMP_ROOT"
    assert outside.exists()
```

- [ ] **Step 2: Run retention tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_retention_policy.py tests/backend/integration/test_text_deletion.py tests/backend/integration/test_audio_cleanup.py -q`

Expected: FAIL because retention schema and service do not exist.

- [ ] **Step 3: Add retention settings and private artifact tracking**

`0015_retention.sql` creates singleton `retention_settings(retention_days CHECK 30/90/180/365 or NULL)` seeded to 365 and `local_artifacts(kind CHECK audio, local_path, status, retry_count, safe_error_code, created_at, deleted_at)`. Local path is private DB data and never enters audit JSON or API responses.

```python
ALLOWED_RETENTION_DAYS = {30, 90, 180, 365, None}

def expiry_for(created_at: datetime, days: int | None) -> datetime | None:
    if days not in ALLOWED_RETENTION_DAYS:
        raise DomainError("RETENTION_VALUE_INVALID", "unsupported retention period")
    return None if days is None else created_at + timedelta(days=days)
```

- [ ] **Step 4: Implement preview-token text deletion**

Preview returns affected video count, transcript count, analysis input count, `full_reproduction_will_be_lost=True`, and a SHA-256 token over sorted target IDs/hashes. Delete requires the matching unexpired preview token, sets body fields to NULL and deletion timestamps, preserves hashes/IDs/times/current results/short evidence, and appends a body-free audit event. Reading or reanalysis never extends expiry. The Task 7 snapshot trigger and an equivalent transcript trigger must reject every update except this exact deletion transition.

- [ ] **Step 5: Implement path-resolved audio deletion and retry**

```python
def is_safe_audio_path(root: Path, candidate: Path) -> bool:
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == resolved_root:
        return False
    try:
        resolved_candidate.relative_to(resolved_root)
        return True
    except ValueError:
        return False
```

On Windows compare resolved paths case-insensitively as well. Refuse a path outside the root, including junction/symlink escape, record `AUDIO_PATH_OUTSIDE_TEMP_ROOT`, and do not call unlink. Successful deletion marks the artifact deleted. Permission/in-use failure records only a safe code, increments retry count, fails the cleanup unit, and remains retryable by a later cleanup job.

- [ ] **Step 6: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_retention_policy.py tests/backend/integration/test_text_deletion.py tests/backend/integration/test_audio_cleanup.py -q`

Expected: focused tests pass, including symlink/junction escape where supported and audio deletion retry.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 7: Commit Task 17**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0015_retention.sql src/market_voice_forecast_ledger/repositories/retention.py src/market_voice_forecast_ledger/services/retention.py tests/backend
git commit -m "feat: enforce private data retention"
```

### Task 18: Loopback-only FastAPI read/write境界

**Files:**
- Create: `src/market_voice_forecast_ledger/api/__init__.py`
- Create: `src/market_voice_forecast_ledger/api/app.py`
- Create: `src/market_voice_forecast_ledger/api/dependencies.py`
- Create: `src/market_voice_forecast_ledger/api/models.py`
- Create: `src/market_voice_forecast_ledger/api/routes/__init__.py`
- Create: `src/market_voice_forecast_ledger/api/routes/health.py`
- Create: `src/market_voice_forecast_ledger/api/routes/subjects.py`
- Create: `src/market_voice_forecast_ledger/api/routes/heatmaps.py`
- Create: `src/market_voice_forecast_ledger/api/routes/jobs.py`
- Create: `src/market_voice_forecast_ledger/api/routes/reviews.py`
- Create: `src/market_voice_forecast_ledger/api/routes/corrections.py`
- Create: `src/market_voice_forecast_ledger/api/routes/retention.py`
- Create: `src/market_voice_forecast_ledger/cli.py`
- Modify: `README.md`
- Create: `tests/backend/integration/test_api_reads.py`
- Create: `tests/backend/integration/test_api_writes.py`
- Create: `tests/backend/integration/test_api_private_boundary.py`

**Interfaces:**
- Consumes: all Task 1–17 services
- Produces: `create_app(settings: Settings) -> FastAPI`
- Produces: `validate_bind_host(host: str) -> str`
- Produces: `GET /api/health`, `GET /api/subjects`, `GET /api/heatmaps?cutoff=YYYY-MM-DD&granularity=week|month`, `GET /api/jobs/{job_id}`
- Produces: `POST /api/mappings/{mapping_id}/reviews`, `POST /api/periods/{period_id}/reviews`, `POST /api/speakers/{segment_id}/corrections`
- Produces: `POST /api/retention/preview`, `POST /api/retention/delete`
- Produces: `python -m market_voice_forecast_ledger.cli serve --host 127.0.0.1 --port 8765`

- [ ] **Step 1: Write failing API boundary tests**

```python
def test_non_loopback_host_is_rejected():
    with pytest.raises(DomainError) as error:
        validate_bind_host("0.0.0.0")
    assert error.value.code == "NON_LOOPBACK_BIND_FORBIDDEN"


def test_responses_never_expose_private_fields(client):
    payloads = [
        client.get("/api/subjects").json(),
        client.get("/api/heatmaps?cutoff=2026-08-14&granularity=week").json(),
        client.get("/api/jobs/1").json(),
    ]
    serialized = json.dumps(payloads, ensure_ascii=False)
    for forbidden in ("text_body", "input_text", "local_path", "audio_path", "prompt_body", "embedding"):
        assert forbidden not in serialized


def test_state_changes_use_post_and_require_reason(client, reviewable_mapping):
    assert client.get(f"/api/mappings/{reviewable_mapping}/reviews").status_code == 405
    response = client.post(f"/api/mappings/{reviewable_mapping}/reviews", json={"decision": "approve", "reason": ""})
    assert response.status_code == 422
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_api_reads.py tests/backend/integration/test_api_writes.py tests/backend/integration/test_api_private_boundary.py -q`

Expected: FAIL because FastAPI application and routes do not exist.

- [ ] **Step 3: Implement connection lifetime, strict models, and safe errors**

```python
def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Market Voice Forecast Ledger", docs_url=None, redoc_url=None)
    app.state.settings = settings
    for router in (health.router, subjects.router, heatmaps.router, jobs.router,
                   reviews.router, corrections.router, retention.router):
        app.include_router(router, prefix="/api")
    return app


def validate_bind_host(host: str) -> str:
    if host != "127.0.0.1":
        raise DomainError("NON_LOOPBACK_BIND_FORBIDDEN", "server host must be 127.0.0.1")
    return host
```

Each request opens and closes one SQLite connection. Pydantic request/response models use `extra='forbid'`, review/correction reasons are non-empty, and deletion requires a preview token. Routes call services, never SQL. Map domain validation to 422, missing entity to 404, stale/conflict to 409, and unexpected failure to `500 {"error":"INTERNAL_ERROR"}` without stack trace or private values. Responses may include short evidence, hashes, IDs, classifications, source video/timestamp, and progress counts only.

Mapping and period review routes call `ReviewApplicationService`, not the low-level review repository, so a review of the current accepted run reprojects current results and heatmap atomically. Speaker correction routes call `SpeakerCorrectionService` and leave stale results visible until explicit reanalysis.

- [ ] **Step 4: Document the accepted MVP security boundary in API metadata**

Expose `GET /api/health` as `{"status":"ok","bind_boundary":"127.0.0.1","authentication":"none"}`. Add a module docstring and README note stating that loopback is not authentication and same-PC processes/browser requests are outside the MVP protection boundary. Do not add CORS allow-all middleware, token placeholders, or hidden fallback host behavior.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_api_reads.py tests/backend/integration/test_api_writes.py tests/backend/integration/test_api_private_boundary.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 18**

```powershell
git add -- README.md src/market_voice_forecast_ledger/api src/market_voice_forecast_ledger/cli.py tests/backend
git commit -m "feat: expose loopback forecast ledger api"
```

### Task 19: 合成E2E、Windows検証入口、開発状態更新

**Files:**
- Create: `tests/backend/e2e/synthetic_fixture.py`
- Create: `tests/backend/e2e/test_synthetic_heatmap_flow.py`
- Create: `tests/backend/e2e/test_synthetic_review_and_conflict_flow.py`
- Create: `tests/backend/README.md`
- Create: `scripts/test-backend.ps1`
- Modify: `README.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/project/status.md`

**Interfaces:**
- Consumes: all Task 1–18 public service and API interfaces
- Produces: `SyntheticLedgerFixture`
- Produces: one PowerShell command verifying backend tests, compileall, work-state tests, state docs, and public safety

- [ ] **Step 1: Write failing synthetic end-to-end tests**

```python
def test_saved_synthetic_input_reaches_sixteen_row_heatmap(db):
    fixture = SyntheticLedgerFixture(db).create_four_subjects_and_eligible_videos()
    fixture.add_person_forecast("person_a", "日本株は来月に底入れする可能性がある", "日本株", "turning_point", "来月")
    fixture.add_organization_forecast("organization_a", "米国株は2027年に上向く", "米国株", "up", "2027年")
    result = fixture.run_validated_pipeline(cutoff=date(2026, 8, 14))
    assert len(result.week.rows) == 16
    assert result.external_tool_calls == 0
    assert result.find("person_a", Asset.NIKKEI_225).primary_direction is DirectionKind.TURNING_POINT
    assert result.find("person_a", Asset.TOPIX).mapping_kind is MappingKind.INFERRED
    assert result.find("organization_a", Asset.SP500).primary_direction is DirectionKind.UP
    assert result.find("person_a", Asset.XAU_USD).is_empty is True


def test_review_unknown_disagreement_and_repost_evidence_survive_e2e(db):
    fixture = SyntheticLedgerFixture(db).create_review_conflict_case()
    fixture.approve_unknown_period("unknown_forecast", "時期不明列で比較")
    fixture.approve_low_mapping("ambiguous_market", "合成周辺発言を確認")
    result = fixture.run_validated_pipeline(cutoff=date(2026, 8, 14))
    assert result.unknown_cell("person_a", Asset.TOPIX).unknown_period is True
    assert result.conflict_cell("person_b", Asset.SP500).view_relation is ViewRelation.DISAGREEMENT
    assert result.repost_evidence_count("person_a", Asset.NIKKEI_225) == 2
```

- [ ] **Step 2: Run E2E tests and verify RED**

Run: `python -m pytest tests/backend/e2e -q`

Expected: FAIL until the synthetic orchestration exposes every required public path without direct SQL shortcuts.

- [ ] **Step 3: Implement the synthetic orchestration through public services**

The fixture creates four synthetic subjects and policies through `SourceRepository.create_subject` and `create_policy`, without calling production `bootstrap_reference_data`. For each subject it then calls, in order: video save, eligibility, synthetic transcript save, personal or organization assignment, job units, scope/run snapshot, strict Codex output validation with a zero-tool receipt, statement/evidence normalization, period normalization/review, asset mapping/review, forecast projection, and current replacement. Finally it calls `HeatmapService.read_cutoff` for the common cutoff and serializes the API response. It uses synthetic names, IDs, channels, and utterances only; it performs no network, audio, model, Codex, shell, or external tool call.

- [ ] **Step 4: Add ASCII-compatible Windows verification command**

```powershell
$ErrorActionPreference = 'Stop'
python -m pytest tests/backend -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m compileall -q src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree
exit $LASTEXITCODE
```

Save this executable logic to `scripts/test-backend.ps1` with ASCII-compatible executable strings.

- [ ] **Step 5: Document setup and update only verified state**

`tests/backend/README.md` and root `README.md` document:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests/backend -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

They state that real transcripts, audio, embeddings, SQLite DBs, runtime logs, and credentials live outside the repository. Update `docs/project/plan.md` and `docs/project/status.md` with actual completed Task numbers, actual test counts, known gaps, and the next approved subproject. Do not record unrun results or declare M2 complete if any Task remains.

- [ ] **Step 6: Run full backend tests and final verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1`

Expected: backend pytest, Python compileall, existing 119 work-state tests, and working-tree public safety all exit 0.

Run: `git diff --check`

Expected: exit 0 with no output.

- [ ] **Step 7: Commit Task 19**

```powershell
git add -- README.md scripts/test-backend.ps1 tests/backend docs/project/plan.md docs/project/status.md
git commit -m "test: verify synthetic core backend flow"
```

---

## Spec Coverage Matrix

| Approved design area | Implemented and tested in |
|---|---|
| SQLite foundation, UTC/JST, local paths | Task 1 |
| Append-only audit and private-field rejection | Task 2 |
| Four subjects and confirmed channel IDs | Task 3 |
| Fixed/all-channel eligibility, manual URL non-bypass, no deduplication | Task 4 |
| Fixed single voice model metadata, raw score, hold band, Akatsuki organization input | Task 5 |
| Separate metadata/audio progress, checkpoints, pause/stop/retry | Task 6 |
| Cutoff scopes, personal/organization input boundary, immutable run snapshot | Task 7 |
| `gpt-5.6-sol`/`max`/zero-tool contract and schema validation | Task 8 |
| Four statement types, direct vs inferred forecast basis, multi-segment exact evidence | Task 9 |
| Explicit/published periods, month first week, reviewed unknown column | Task 10 |
| Direct/inferred asset mapping and application confidence ceiling | Task 11 |
| Low/unresolved approval, correction, rejection audit history | Task 12 |
| Conditional layer, turning point, disagreement, changed view, repost evidence | Task 13 |
| Same-scope atomic replacement and cutoff isolation | Task 14 |
| Speaker/channel corrections, audit, stale scopes | Task 15 |
| Four-subject × four-asset week/month heatmap and rebuildable cache | Task 16 |
| 30/90/180/365/unlimited retention and safe audio deletion | Task 17 |
| Loopback-only API, no GET mutation, private response boundary | Task 18 |
| Synthetic E2E and Windows verification | Task 19 |

## Completion Criteria

- All 15 numbered migrations apply once, in order, to a new SQLite DB with foreign keys enabled.
- The four default subjects and two user-confirmed fixed channel IDs are correct; manual URLs cannot bypass them.
- Distinct original, clip, Short, and repost video IDs all remain independent and can each contribute evidence.
- Personal subject/interviewer/hold and 暁投資顧問 organization assignment rules are both enforced.
- Speaker model name/version, raw score, threshold config version are stored without common 0–1 normalization; border scores become hold.
- Different cutoff scopes coexist; analysis run history is append-only; input snapshot mutation is limited to body deletion.
- Only exact `gpt-5.6-sol`, `max`, zero-tool, stored-statements-only receipts can proceed.
- Four statement types, direct/inferred forecast basis, conditional layer, turning point, flat, unknown, and no-evidence empty state remain distinct.
- Every displayed short evidence link references an input segment and is a continuous substring of retained transcript text.
- Relative dates use `published_at` in JST, explicit dates are labeled separately, first-week dates may cross months, and approved unknown periods use only the special column.
- App-rule confidence cannot be raised by Codex; personal interviewer-only hints cannot produce high/medium; low/unresolved require append-only review.
- Same-video opposing forecasts retain both directions as disagreement; later opposing videos become changed views; distinct repost evidence is not suppressed.
- Current results, accepted run event, audit event, and heatmap cache update atomically or not at all.
- Corrections preserve prior runs/current results, mark dependent scopes stale, and require reasoned audit events.
- Text deletion preserves hashes, short evidence, and current forecasts; unsafe audio paths are never deleted and safe failures remain retryable.
- API responses exclude private body/path fields, state changes use POST, and server host validation accepts only `127.0.0.1` while documenting the absence of authentication.
- Synthetic E2E produces 16 subject/asset rows, reviewed unknown and disagreement states, independent repost evidence, and empty XAU/USD when no evidence exists.
- `scripts/test-backend.ps1`, `git diff --check`, state-document checks, and public-safety checks all pass before M2中核バックエンド完了を報告する。

# M2 中核バックエンド Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 承認済みM1設計に従い、主体別チャンネル方針、話者割当、変更不能な分析run、発言分類、期間、指数割当、現在予想、ヒートマップ、checkpoint、監査、保持・削除をSQLiteへ安全に保存するテスト済みのM2中核バックエンドを構築する。

**Architecture:** Python packageを `src/market_voice_forecast_ledger/` に置き、標準 `sqlite3` と追記型の番号付きSQL migrationをschemaの正本にする。DB repository、純粋なドメイン規則、transactional service、FastAPI境界を分離し、YouTube検索、音声処理、Codex CLI、React UIは後続adapterがこの中核へ入力する。run由来の事実は追記専用、修正可能な現在値は監査eventと同一transactionで更新し、現在結果は検証済みrunからだけ原子的に置換する。

**Tech Stack:** Python 3.11以上、標準 `sqlite3`、FastAPI 0.115以上1.0未満、Pydantic 2.10以上3.0未満、pytest 8.3以上10.0未満、httpx 0.28以上1.0未満、PowerShell 5.1/7互換、SQLite WAL。

**Plan revision:** 2026-08-15 JST。初回12タスク草案を置換して19個の独立レビュー単位へ再構成し、M2事前フィージビリティ検証後の承認済み修正をTask 1、6～16、19へ反映した。修正書面のユーザー承認後、依存順序、共有型、成果物とunit状態の原子性、公開更新経路、最終反映transaction、process crash試験を再監査した。

**User approval:** 19タスク構成とフィージビリティ修正書面は2026-08-15 JSTに承認済み。M2コード実装の開始は未承認。

**Final plan audit:** 2026-08-15 JST。Task 14はtransaction内の現在行置換部品だけを提供し、Task 16の `promote_completed_run` とreview適用経路だけが現在結果とheatmapを公開更新できる。これにより最終unit・job成功を迂回する更新経路を作らない。

**Execution mode:** 案1のタスク別サブエージェント実装・レビューを採用。ユーザーの明示的な開始指示があるまで、worktree作成、サブエージェント起動、テスト作成、コード実装を行わない。

## Global Constraints

- 実装開始の明示指示を受けた後、`superpowers:using-git-worktrees` で隔離作業領域を確認・作成し、Task 1を開始する。
- Windows 11で動作し、実データの既定保存先は `%LOCALAPPDATA%\MarketVoiceForecastLedger\`、テストデータは必ずpytestの `tmp_path` 配下に置く。
- HTTP serverは `127.0.0.1` だけへbindする。MVPはローカルトークン、`Origin`検査、Windowsアカウント認証を持たず、信頼できる単独利用PCを前提とする。状態変更へGETを使わない。
- DB時刻と内部比較はUTCのISO 8601とする。画面・相対期間・週月境界は固定JST（UTC+9）とし、`ZoneInfo`と`tzdata`へ依存しない。日付指定cutoffはJST翌日0時をUTCへ変換した排他的上限、期間はJST暦日の `YYYY-MM-DD` とする。
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
- Produces: `JST: datetime.timezone`
- Produces: `to_jst(value: datetime) -> datetime`
- Produces: `cutoff_exclusive_utc(day: date) -> datetime`
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


def test_fixed_jst_cutoff_is_next_local_midnight_expressed_in_utc():
    assert JST.utcoffset(None) == timedelta(hours=9)
    assert cutoff_exclusive_utc(date(2026, 8, 14)) == datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)


def test_utc_iso_normalizes_offset_and_uses_fixed_microsecond_precision():
    source = datetime(2026, 8, 15, 0, 0, 0, 123456, tzinfo=JST)
    assert utc_iso(source) == "2026-08-14T15:00:00.123456Z"
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

Define `JST = timezone(timedelta(hours=9), name="JST")` in `domain/common.py`. `utc_iso` and `to_jst` reject naive datetimes. `utc_iso` normalizes to UTC and emits fixed-width `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so SQLite text ordering equals chronological ordering. `cutoff_exclusive_utc` builds the next JST midnight then converts it to UTC. Do not import `zoneinfo`, and do not add `tzdata` to runtime or development dependencies.

Define the shared `StrEnum` values exactly once in `domain/enums.py`:

```python
class SubjectKind(StrEnum): PERSON = "person"; ORGANIZATION = "organization"
class PolicyKind(StrEnum): ALL_CHANNELS = "all_channels"; FIXED_CHANNEL = "fixed_channel"
class ConfigurationStatus(StrEnum): CONFIGURED = "configured"; CONFIGURATION_REQUIRED = "configuration_required"
class DiscoveryMethod(StrEnum): AUTO_SEARCH = "auto_search"; MANUAL_URL = "manual_url"
class EligibilityStatus(StrEnum): ELIGIBLE = "eligible"; CHANNEL_OUT_OF_SCOPE = "channel_out_of_scope"; CONFIGURATION_REQUIRED = "configuration_required"; CHANNEL_UNRESOLVED = "channel_unresolved"
class AssignmentKind(StrEnum): SUBJECT = "subject"; INTERVIEWER = "interviewer"; HOLD = "hold"
class AssignmentOrigin(StrEnum): AUTO_VOICE = "auto_voice"; MANUAL = "manual"; CHANNEL_ORGANIZATION = "channel_organization"
class JobKind(StrEnum): VIDEO_PIPELINE = "video_pipeline"; ANALYSIS_SCOPE = "analysis_scope"
class JobStage(StrEnum): VIDEO_METADATA = "video_metadata"; AUDIO_ACQUISITION = "audio_acquisition"; TRANSCRIPTION = "transcription"; SPEAKER_ASSIGNMENT = "speaker_assignment"; ANALYSIS_INPUT_EXTRACTION = "analysis_input_extraction"; CODEX_ANALYSIS = "codex_analysis"; ASSET_MAPPING = "asset_mapping"; HEATMAP_UPDATE = "heatmap_update"
class JobStatus(StrEnum): QUEUED = "queued"; RUNNING = "running"; PAUSE_REQUESTED = "pause_requested"; PAUSED = "paused"; CANCEL_REQUESTED = "cancel_requested"; STOPPED = "stopped"; FAILED = "failed"; RETRYING = "retrying"; SUCCEEDED = "succeeded"
class UnitStatus(StrEnum): PENDING = "pending"; RUNNING = "running"; SUCCESS = "success"; FAILED = "failed"
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

Expected: `4 passed`.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- pyproject.toml src/market_voice_forecast_ledger/__init__.py src/market_voice_forecast_ledger/config.py src/market_voice_forecast_ledger/domain/__init__.py src/market_voice_forecast_ledger/domain/errors.py src/market_voice_forecast_ledger/domain/enums.py src/market_voice_forecast_ledger/domain/common.py src/market_voice_forecast_ledger/db/__init__.py src/market_voice_forecast_ledger/db/connection.py src/market_voice_forecast_ledger/db/migrate.py src/market_voice_forecast_ledger/db/migrations/__init__.py src/market_voice_forecast_ledger/db/migrations/0001_foundation.sql tests/backend/conftest.py tests/backend/integration/test_database_foundation.py
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
    with transaction(db):
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

`AuditRepository` exposes no update/delete method. `append` requires `conn.in_transaction`, validates both JSON payloads, serializes with `canonical_json`, and inserts without committing inside the caller's transaction. Safe audit views may contain IDs, hashes, classifications, timestamps, short evidence, actor, and reason.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_audit_payload.py tests/backend/integration/test_audit_append_only.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0002_audit.sql src/market_voice_forecast_ledger/repositories/__init__.py src/market_voice_forecast_ledger/repositories/audit.py src/market_voice_forecast_ledger/services/__init__.py src/market_voice_forecast_ledger/services/audit.py tests/backend/unit/test_audit_payload.py tests/backend/integration/test_audit_append_only.py
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
git add -- src/market_voice_forecast_ledger/db/migrations/0003_sources.sql src/market_voice_forecast_ledger/domain/sources.py src/market_voice_forecast_ledger/repositories/sources.py src/market_voice_forecast_ledger/bootstrap.py tests/backend/integration/test_source_schema.py tests/backend/integration/test_reference_data.py
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
git add -- src/market_voice_forecast_ledger/services/channel_policy.py tests/backend/unit/test_channel_policy_rules.py tests/backend/integration/test_video_eligibility.py
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
- Produces: `SpeakerRepository.add_chunk(video_id: int, chunk_no: int, start_ms: int, end_ms: int, input_hash: str, output_hash: str, status: UnitStatus) -> int`
- Produces: `SpeakerRepository.add_segment(video_id: int, chunk_id: int, segment_no: int, start_ms: int, end_ms: int, text_body: str, anonymous_speaker_id: str, transcript_created_at: datetime, expires_at: datetime | None) -> int`
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
git add -- src/market_voice_forecast_ledger/db/migrations/0004_speakers.sql src/market_voice_forecast_ledger/domain/speakers.py src/market_voice_forecast_ledger/repositories/speakers.py src/market_voice_forecast_ledger/services/speaker_assignment.py tests/backend/unit/test_speaker_thresholds.py tests/backend/integration/test_speaker_assignments.py tests/backend/integration/test_akatsuki_organization_assignment.py
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
- Produces: `JobManifest.build(kind: JobKind, units: Sequence[ManifestUnit]) -> JobManifest`
- Produces: `ANALYSIS_INPUT_UNIT_KEY = "analysis-input:freeze"`
- Produces: `STATEMENT_NORMALIZATION_UNIT_KEY = "analysis:normalize-statements"`
- Produces: `PERIOD_NORMALIZATION_UNIT_KEY = "analysis:normalize-periods"`
- Produces: `ASSET_MAPPING_UNIT_KEY = "analysis:map-assets"`
- Produces: `FORECAST_PROJECTION_UNIT_KEY = "analysis:project-forecasts"`
- Produces: `FINAL_PROMOTION_UNIT_KEY = "heatmap:promote-current"`
- Produces: `JobStateService.create(manifest: JobManifest) -> int`
- Produces: `JobStateService.create_successor(source_job_id: int, manifest: JobManifest, artifact_hashes: Mapping[str, str], external_input_hashes: Mapping[str, str | None]) -> tuple[int, ResumePlan]`
- Produces: `JobStateService.begin_unit(job_id: int, unit_key: str, external_input_hash: str | None = None) -> JobUnit`
- Produces: `JobStateService.request_pause(job_id: int) -> JobStatus`
- Produces: `JobStateService.request_stop(job_id: int) -> JobStatus`
- Produces: `JobStateService.status(job_id: int) -> JobStatus`
- Produces: `JobStateService.unit(job_id: int, unit_key: str) -> JobUnit`
- Produces: `JobStateService.complete_unit(job_id: int, unit_key: str, output_hash: str) -> None`
- Produces: `JobStateService.complete_unit_in_transaction(job_id: int, unit_key: str, output_hash: str) -> None`
- Produces: `JobStateService.fail_unit(job_id: int, unit_key: str, error_code: str) -> None`
- Produces: `JobStateService.fail_unit_in_transaction(job_id: int, unit_key: str, error_code: str) -> None`
- Produces: `JobStateService.succeed_job_in_transaction(job_id: int) -> None`
- Produces: `JobStateService.require_upstream_success(job_id: int, promotion_unit_key: str) -> None`
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


def test_analysis_manifest_requires_exactly_one_final_promotion_unit():
    units = (
        ManifestUnit(ANALYSIS_INPUT_UNIT_KEY, JobStage.ANALYSIS_INPUT_EXTRACTION, 1, "input-contract", (), "contract-hash"),
        ManifestUnit("codex:1", JobStage.CODEX_ANALYSIS, 2, None, (ANALYSIS_INPUT_UNIT_KEY,), "contract-hash"),
    )
    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.ANALYSIS_SCOPE, units)
    assert error.value.code == "INVALID_ANALYSIS_MANIFEST"


def test_analysis_manifest_requires_input_freeze_as_first_unit():
    units = (ManifestUnit(FINAL_PROMOTION_UNIT_KEY, JobStage.HEATMAP_UPDATE, 1, None, (), "contract-hash"),)
    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.ANALYSIS_SCOPE, units)
    assert error.value.code == "INVALID_ANALYSIS_MANIFEST"


def test_resume_reuses_only_success_units_with_matching_artifact_hash(db, eight_chunk_job):
    mark_first_four_chunks_complete(db, eight_chunk_job)
    plan = JobStateService(db).resume(eight_chunk_job, stored_hashes_for_first_four())
    assert plan.next_unit_key == "transcription:chunk:5"
    assert len(plan.reused_unit_keys) == 4


def test_success_unit_is_not_reused_when_execution_contract_changes(db, completed_source_job):
    changed = completed_source_job.manifest_with_contract("contract-v2")
    successor_id, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        changed,
        completed_source_job.artifact_hashes,
        completed_source_job.external_input_hashes,
    )
    assert successor_id != completed_source_job.id
    assert "codex:batch:1" not in plan.reused_unit_keys
    assert "codex:batch:1" in plan.pending_unit_keys


def test_successor_invalidates_the_dependent_closure_when_an_upstream_artifact_differs(db, completed_source_job):
    artifacts = completed_source_job.artifact_hashes | {ANALYSIS_INPUT_UNIT_KEY: "different-real-artifact"}
    _, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        completed_source_job.manifest,
        artifacts,
        completed_source_job.external_input_hashes,
    )
    assert ANALYSIS_INPUT_UNIT_KEY in plan.pending_unit_keys
    assert "codex:batch:1" in plan.pending_unit_keys
    assert STATEMENT_NORMALIZATION_UNIT_KEY in plan.pending_unit_keys


def test_retry_rejects_changed_external_input_for_the_same_unit(db, failed_projection_job):
    JobStateService(db).resume(failed_projection_job.id, failed_projection_job.artifact_hashes)
    with pytest.raises(DomainError) as error:
        JobStateService(db).begin_unit(
            failed_projection_job.id,
            FORECAST_PROJECTION_UNIT_KEY,
            external_input_hash="review-state-v2",
        )
    assert error.value.code == "UNIT_INPUT_CHANGED"


def test_interrupted_fifth_unit_restarts_from_pending_after_reopen(db_path, tmp_path, eight_chunk_job):
    first = open_database(db_path)
    mark_first_four_chunks_complete(first, eight_chunk_job)
    JobStateService(first).begin_unit(eight_chunk_job, "transcription:chunk:5")
    first.close()
    partial = tmp_path / "chunk-5.partial.json"
    partial.write_text('{"partial":true}', encoding="utf-8")
    artifact_hashes = stored_hashes_for_first_four() | {
        "transcription:chunk:5": hashlib.sha256(partial.read_bytes()).hexdigest()
    }
    reopened = open_database(db_path)
    plan = JobStateService(reopened).resume(eight_chunk_job, artifact_hashes)
    assert len(plan.reused_unit_keys) == 4
    assert plan.next_unit_key == "transcription:chunk:5"
    assert JobStateService(reopened).unit(eight_chunk_job, "transcription:chunk:5").status is UnitStatus.PENDING
```

- [ ] **Step 2: Run job tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_job_progress.py -q`

Expected: FAIL because job schema and state service do not exist.

- [ ] **Step 3: Add job schema and legal transitions**

`0005_jobs.sql` creates `jobs`, `job_units`, `job_unit_attempts`, and `job_events`. `jobs` stores `job_kind CHECK video_pipeline/analysis_scope`, immutable manifest hash, and total units; current job status may change only through the service. `job_units` has unique `(job_id, unit_key)`, stage, ordinal, declared root-input hash, canonical dependency-key JSON, execution-contract hash covering model/settings/contract versions, once-bound external-input hash, once-bound effective-input hash, output hash, status `pending/running/success/failed`, attempt count, safe error code, started/finished timestamps. `job_unit_attempts` and `job_events` are append-only with UPDATE/DELETE triggers and keep safe attempt/result metadata. An `analysis_scope` manifest must start with exactly one `ANALYSIS_INPUT_UNIT_KEY` at ordinal 1 and end with exactly one `FINAL_PROMOTION_UNIT_KEY`; a `video_pipeline` manifest must contain neither reserved key.

```python
ANALYSIS_INPUT_UNIT_KEY = "analysis-input:freeze"
STATEMENT_NORMALIZATION_UNIT_KEY = "analysis:normalize-statements"
PERIOD_NORMALIZATION_UNIT_KEY = "analysis:normalize-periods"
ASSET_MAPPING_UNIT_KEY = "analysis:map-assets"
FORECAST_PROJECTION_UNIT_KEY = "analysis:project-forecasts"
FINAL_PROMOTION_UNIT_KEY = "heatmap:promote-current"
VIDEO_PIPELINE_STAGES = frozenset({JobStage.VIDEO_METADATA, JobStage.AUDIO_ACQUISITION, JobStage.TRANSCRIPTION, JobStage.SPEAKER_ASSIGNMENT})
ANALYSIS_SCOPE_STAGES = frozenset({JobStage.ANALYSIS_INPUT_EXTRACTION, JobStage.CODEX_ANALYSIS, JobStage.ASSET_MAPPING, JobStage.HEATMAP_UPDATE})


@dataclass(frozen=True)
class ManifestUnit:
    unit_key: str
    stage: JobStage
    ordinal: int
    declared_input_hash: str | None
    dependency_keys: tuple[str, ...]
    execution_contract_hash: str


@dataclass(frozen=True)
class JobManifest:
    kind: JobKind
    units: tuple[ManifestUnit, ...]
    manifest_hash: str

    @classmethod
    def build(cls, kind: JobKind, units: Sequence[ManifestUnit]) -> "JobManifest":
        ordered = tuple(sorted(units, key=lambda item: item.ordinal))
        if not ordered or [item.ordinal for item in ordered] != list(range(1, len(ordered) + 1)):
            raise DomainError("INVALID_MANIFEST_ORDINALS", "unit ordinals must be contiguous from one")
        if len({item.unit_key for item in ordered}) != len(ordered):
            raise DomainError("DUPLICATE_UNIT_KEY", "unit keys must be unique")
        earlier: set[str] = set()
        for item in ordered:
            if len(set(item.dependency_keys)) != len(item.dependency_keys) or any(key not in earlier for key in item.dependency_keys):
                raise DomainError("INVALID_UNIT_DEPENDENCY", "dependencies must be unique earlier units")
            earlier.add(item.unit_key)
        final_count = sum(item.unit_key == FINAL_PROMOTION_UNIT_KEY for item in ordered)
        input_count = sum(item.unit_key == ANALYSIS_INPUT_UNIT_KEY for item in ordered)
        if kind is JobKind.ANALYSIS_SCOPE and (
            input_count != 1
            or ordered[0].unit_key != ANALYSIS_INPUT_UNIT_KEY
            or ordered[0].stage is not JobStage.ANALYSIS_INPUT_EXTRACTION
            or final_count != 1
            or ordered[-1].unit_key != FINAL_PROMOTION_UNIT_KEY
        ):
            raise DomainError("INVALID_ANALYSIS_MANIFEST", "analysis manifest requires reserved first and final units")
        if kind is JobKind.VIDEO_PIPELINE and (input_count or final_count):
            raise DomainError("INVALID_VIDEO_MANIFEST", "video manifest cannot contain analysis-reserved units")
        allowed = ANALYSIS_SCOPE_STAGES if kind is JobKind.ANALYSIS_SCOPE else VIDEO_PIPELINE_STAGES
        if any(item.stage not in allowed for item in ordered):
            raise DomainError("INVALID_JOB_STAGE", "unit stage does not belong to job kind")
        if kind is JobKind.ANALYSIS_SCOPE and ordered[-1].stage is not JobStage.HEATMAP_UPDATE:
            raise DomainError("INVALID_PROMOTION_STAGE", "promotion unit must use heatmap_update stage")
        payload = {"kind": kind.value, "units": [asdict(item) for item in ordered]}
        return cls(kind, ordered, sha256_text(canonical_json(payload)))


@dataclass(frozen=True)
class JobUnit:
    job_id: int
    unit_key: str
    stage: JobStage
    ordinal: int
    status: UnitStatus
    declared_input_hash: str | None
    dependency_keys: tuple[str, ...]
    execution_contract_hash: str
    external_input_hash: str | None
    bound_input_hash: str | None
    output_hash: str | None
    attempt_count: int


@dataclass(frozen=True)
class ResumePlan:
    reused_unit_keys: tuple[str, ...]
    pending_unit_keys: tuple[str, ...]
    next_unit_key: str | None
```

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

`begin_unit` requires every declared dependency to be `success`, reads their exact output hashes in manifest order, and computes `bound_input_hash = sha256(canonical_json({declared_input_hash, dependency_outputs, external_input_hash}))`. It writes `external_input_hash` and `bound_input_hash` only on the first attempt. A retry must recompute the same value or fail with `UNIT_INPUT_CHANGED`, which requires a successor job; this prevents a review/config/input change from being smuggled into an existing unit. Root acquisition/transcription units use a known declared hash and no dependencies. Analysis units form an explicit dependency chain; projection may bind the effective review-state hash as external input.

Unit output validation and `success` status commit in one transaction. Resume changes stale `running` and retryable `failed` units to `pending`, records the prior attempt, ignores partial output, and runs that whole unit from its start. It reuses only `success` units whose bound input hash, execution-contract hash, and artifact output hash all match. `create_successor` creates a new immutable manifest and copies a success unit only when its manifest fields and supplied external-input hash match, its real artifact hash matches, and every dependency has already been reused with the same output hash. If one dependency or contract is not reusable, that unit and its entire dependent closure remain pending even when an old output file exists. Transaction-internal complete/fail methods require `conn.in_transaction` and never commit independently. `fail_unit_in_transaction` records only a safe code and changes the running unit/job state without accepting partial output. `succeed_job_in_transaction` rejects the transition unless every manifest unit, including the final promotion unit, is `success`. Tasks 7～13 use the reserved analysis unit keys to commit each durable stage result with its unit status; Task 16 makes the final promotion unit atomic with current results and heatmap cache. `require_upstream_success` checks every manifest unit before the named final promotion unit and rejects missing, running, failed, or hash-incompatible units. A pause resumes the same job after a safe boundary; a stopped job creates a successor and may reuse only verified successful units. `review_required` is a result flag, not job failure. Progress is `success / manifest total` per stage; no synthetic timer, weighting, or ETA. Focused recovery tests close the SQLite connection after injected failure and verify the persisted state through a new connection.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_job_progress.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 6**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0005_jobs.sql src/market_voice_forecast_ledger/domain/jobs.py src/market_voice_forecast_ledger/repositories/jobs.py src/market_voice_forecast_ledger/services/job_state.py tests/backend/unit/test_job_state_machine.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_job_progress.py
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
- Create: `tests/backend/integration/test_analysis_job_attempts.py`
- Create: `tests/backend/integration/test_cutoff_scopes.py`

**Interfaces:**
- Consumes: JST `cutoff_day`, `cutoff_exclusive_utc`, `SourceRepository`, `SpeakerRepository`, `JobStateService`, all reserved analysis unit keys, `transaction`
- Produces: `AnalysisRunSettings(model, reasoning_effort, prompt_version, schema_version, information_boundary_version)`
- Produces: `AnalysisRunSettings.required() -> AnalysisRunSettings`
- Produces: `AnalysisRunSettings.codex_execution_contract_hash() -> str`
- Produces: `BeginAnalysisRun(subject_id: int, cutoff_day: date, job_id: int, settings: AnalysisRunSettings)`
- Produces: `AnalysisRunService.preview_input_contract(subject_id: int, cutoff_day: date, settings: AnalysisRunSettings) -> str`
- Produces: `AnalysisRunService.begin(command: BeginAnalysisRun) -> AnalysisRun`
- Produces: `AnalysisRunService.attach_successor(run_id: int, successor_job_id: int) -> AnalysisRunJobAttempt`
- Produces: `AnalysisRepository.get_scope(scope_id: int) -> AnalysisScope`
- Produces: `AnalysisRepository.get_run(run_id: int) -> AnalysisRun`
- Produces: `AnalysisRepository.get_active_job_id(run_id: int) -> int`
- Produces: `AnalysisRepository.count_runs(scope_id: int) -> int`
- Produces: `AnalysisRepository.get_input_segments(run_id: int) -> tuple[RunSegment, ...]`
- Produces: `AnalysisRepository.get_effective_run_status(run_id: int) -> AnalysisRunStatus`
- Produces: `AnalysisRepository.append_run_event(run_id: int, status: AnalysisRunStatus, error_code: str | None) -> int`

- [ ] **Step 1: Write failing cutoff and input-boundary tests**

```python
def test_person_scope_excludes_interviewer_hold_and_post_cutoff(db, personal_input_fixture):
    run = AnalysisRunService(db).begin(BeginAnalysisRun(
        personal_input_fixture.subject_id,
        date(2026, 8, 14),
        personal_input_fixture.analysis_job_id,
        AnalysisRunSettings.required(),
    ))
    segments = AnalysisRepository(db).get_input_segments(run.id)
    assert [item.segment_id for item in segments] == personal_input_fixture.subject_segments_before_cutoff
    assert JobStateService(db).unit(
        personal_input_fixture.analysis_job_id,
        ANALYSIS_INPUT_UNIT_KEY,
    ).status is UnitStatus.SUCCESS


def test_akatsuki_scope_includes_all_organization_segments(db, akatsuki_input_fixture):
    run = AnalysisRunService(db).begin(BeginAnalysisRun(
        akatsuki_input_fixture.subject_id,
        date(2026, 8, 14),
        akatsuki_input_fixture.analysis_job_id,
        AnalysisRunSettings.required(),
    ))
    assert len(AnalysisRepository(db).get_input_segments(run.id)) == 3


def test_distinct_repost_video_segments_are_not_deduplicated(db, two_video_same_text_fixture):
    run = AnalysisRunService(db).begin(two_video_same_text_fixture.command)
    assert len(AnalysisRepository(db).get_input_segments(run.id)) == 2


def test_analysis_job_for_different_input_contract_is_rejected(db, personal_input_fixture):
    command = personal_input_fixture.command_with_job_input_hash("different-subject-or-cutoff")
    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).begin(command)
    assert error.value.code == "ANALYSIS_JOB_INPUT_MISMATCH"


def test_stopped_run_attaches_a_successor_only_when_all_durable_successes_are_reused(db, stopped_analysis_run):
    successor_id, plan = JobStateService(db).create_successor(
        stopped_analysis_run.job_id,
        stopped_analysis_run.same_manifest,
        stopped_analysis_run.artifact_hashes,
        stopped_analysis_run.external_input_hashes,
    )
    assert set(stopped_analysis_run.durable_success_unit_keys) <= set(plan.reused_unit_keys)
    AnalysisRunService(db).attach_successor(stopped_analysis_run.id, successor_id)
    assert AnalysisRepository(db).get_active_job_id(stopped_analysis_run.id) == successor_id
```

`AnalysisRunSettings.required()` returns model `gpt-5.6-sol`, reasoning effort `max`, prompt contract version `m2-core-prompt-contract-v1`, schema version `m2-analysis-output-v1`, and information boundary version `stored-statements-only-v1`. `preview_input_contract` and `begin` both reject any settings value unequal to this required contract; Task 8 independently verifies that the executed Codex receipt also matches it.

- [ ] **Step 2: Run analysis-boundary tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_analysis_append_only.py tests/backend/integration/test_analysis_job_attempts.py tests/backend/integration/test_cutoff_scopes.py -q`

Expected: FAIL because scope/run schema and builder do not exist.

- [ ] **Step 3: Add immutable run schema and snapshot exception trigger**

`0006_analysis_runs.sql` creates:

- `analysis_scopes(subject_id, cutoff_day_jst, cutoff_exclusive_utc, status, stale_reason, UNIQUE(subject_id, cutoff_day_jst))`.
- `analysis_runs(scope_id INTEGER NOT NULL REFERENCES analysis_scopes(id), model, reasoning_effort, prompt_version, schema_version, information_boundary_version, input_hash, input_contract_hash, started_at)` with UPDATE/DELETE append-only triggers.
- `analysis_run_job_attempts(run_id INTEGER NOT NULL REFERENCES analysis_runs(id), job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id), attempt_ordinal, source_job_id, attached_at, UNIQUE(run_id, attempt_ordinal))` with UPDATE/DELETE append-only triggers. The highest attempt ordinal is the active immutable job for the run; video-acquisition jobs cannot be attached.
- `analysis_run_events(run_id, status, safe_error_code, created_at)` with UPDATE/DELETE append-only triggers; effective status is the newest event by ID.
- `analysis_run_segments(run_id, segment_id, ordinal, video_id, published_at, policy_id, policy_hash, assignment_kind, assigned_subject_id, assignment_updated_at, assignment_evidence_hash, UNIQUE(run_id, ordinal), UNIQUE(run_id, segment_id))` with UPDATE/DELETE triggers.
- `analysis_input_snapshots(run_id UNIQUE, input_text, metadata_json, input_sha256, snapshot_created_at, expires_at, text_deleted_at)`.

The snapshot UPDATE trigger allows only `input_text: non-NULL -> NULL` together with `text_deleted_at: NULL -> non-NULL` while every other column remains identical; it rejects all other UPDATE and every DELETE.

`AnalysisRepository.get_run` and `get_active_job_id` resolve the highest job-attempt ordinal; returned `AnalysisRun.active_job_id` is derived and is not a mutable column on `analysis_runs`.

- [ ] **Step 4: Implement fail-closed run input construction**

`preview_input_contract` and `begin` use the same pure selection builder. It derives the UTC-exclusive upper bound for the selected JST day, selects only videos with `published_at < cutoff_exclusive_utc` plus current `eligible` policy/hash, applies the personal or organization assignment boundary, orders by `published_at`, YouTube video ID, and segment ordinal, and hashes canonical metadata containing subject ID, cutoff day, policy/assignment evidence, ordered segment IDs and text hashes, exact input-text hash, and settings versions. It does not persist the preview.

The caller creates the matching immutable initial job from the preview and calls `JobStateService.begin_unit(job_id, ANALYSIS_INPUT_UNIT_KEY)` before `begin`. `begin` first requires `settings == AnalysisRunSettings.required()`, then recomputes the contract inside its transaction and verifies that `job_id` is not attached to another run and belongs to an analysis manifest with this exact semantic order: the running input-freeze unit; exactly one `codex:batch:1` unit at `CODEX_ANALYSIS`; statement normalization; period normalization; asset mapping; forecast projection; and the final promotion unit. The four reserved normalization/mapping/projection units use `ASSET_MAPPING`, while the final unit uses `HEATMAP_UPDATE`. The input unit's `declared_input_hash` must equal the recomputed contract, it has no dependencies, and the Codex unit execution-contract hash must equal `settings.codex_execution_contract_hash()`.

The dependency graph is also exact: the Codex batch depends on input freeze; statement normalization depends on the Codex batch; period normalization depends on statements; asset mapping depends on statements and periods; forecast projection depends on mappings and periods and binds the effective review-state hash when it begins; final promotion depends on forecast projection. M2 deliberately fixes one Codex batch because the CLI splitter and context-limit policy are a later approved subproject; the output schema and unit key remain batch-compatible without inventing that policy here. A job prepared for another subject, cutoff, policy/assignment state, input body, model, contract version, or dependency graph fails closed before scope/run insertion. `begin` then reuses or creates the `(subject, cutoff_day_jst)` scope, inserts the run's first `analysis_run_job_attempt`, snapshots policy and assignment evidence plus exact input text and SHA-256, records the same input-contract hash, inserts a `started` run event, and calls `complete_unit_in_transaction` with the snapshot output hash. Those inserts and the input unit's `success` commit together. Personal subjects require current `subject` assignment to that same subject. 暁投資顧問 requires `channel_organization` assignment. It never consults duplicate similarity or acquisition timestamps.

`attach_successor` is allowed only after the active job is `stopped`, or after it is `failed` because a once-bound unit input changed and same-job retry is unsafe. The successor must be created from that active job, its input contract and dependency graph must still match the immutable run, and every source-job unit that already has durable run-owned rows must be present in `reused_unit_keys` with the same bound input/output hashes. It appends a new job-attempt row; it never edits the run or old attempt. If a completed Codex/statement/period/mapping/projection unit would need recomputation, the service rejects with `SUCCESSOR_REQUIRES_NEW_RUN`, and the caller must create a fresh job and call `begin` to make a new run. Failed/running units have no adopted rows because of their transaction boundaries, so a compatible successor may continue from the first pending unit.

- [ ] **Step 5: Prove append-only enforcement and coexistence**

Add tests showing raw SQL UPDATE/DELETE fails for runs/events/segments, input snapshot content cannot be edited, different cutoff scopes coexist, and rerunning one scope creates a new run without changing another scope.

- [ ] **Step 6: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_analysis_append_only.py tests/backend/integration/test_analysis_job_attempts.py tests/backend/integration/test_cutoff_scopes.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 7: Commit Task 7**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0006_analysis_runs.sql src/market_voice_forecast_ledger/domain/analysis.py src/market_voice_forecast_ledger/repositories/analysis.py src/market_voice_forecast_ledger/services/analysis_runs.py tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_analysis_append_only.py tests/backend/integration/test_analysis_job_attempts.py tests/backend/integration/test_cutoff_scopes.py
git commit -m "feat: freeze cutoff analysis inputs"
```

### Task 8: Codex構造化出力contractとfail-closed run検査

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0007_analysis_outputs.sql`
- Create: `src/market_voice_forecast_ledger/services/codex_contract.py`
- Create: `tests/backend/unit/test_codex_contract.py`
- Create: `tests/backend/integration/test_analysis_output_acceptance.py`

**Interfaces:**
- Consumes: `AnalysisRepository`, `JobStateService`, `canonical_json`, `sha256_text`, Pydantic v2
- Produces: `EvidenceProposal(segment_id: int, excerpt: str)`
- Produces: `StatementProposal`
- Produces: `AnalysisEnvelope(run_id: int, batch_key: str, statements: tuple[StatementProposal, ...])`
- Produces: `CodexRunReceipt(model, reasoning_effort, tool_call_count, boundary_mode)`
- Produces: `CodexContractService.validate_and_store(run_id: int, unit_key: str, output_json: str, receipt: CodexRunReceipt) -> ValidatedAnalysisOutput`

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
def test_invalid_receipt_fails_the_running_batch_without_storing_output(db, started_run, valid_output_json, receipt, code):
    with pytest.raises(DomainError) as error:
        CodexContractService(db).validate_and_store(
            started_run.id,
            started_run.running_codex_unit_key,
            valid_output_json,
            receipt,
        )
    assert error.value.code == code
    assert JobStateService(db).unit(
        started_run.job_id,
        started_run.running_codex_unit_key,
    ).status is UnitStatus.FAILED
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_outputs WHERE run_id=?",
        (started_run.id,),
    ).fetchone()[0] == 0


def test_valid_batch_output_and_unit_success_commit_together(db, started_run, valid_output_json):
    result = CodexContractService(db).validate_and_store(
        started_run.id,
        started_run.running_codex_unit_key,
        valid_output_json,
        CodexRunReceipt("gpt-5.6-sol", "max", 0, "stored_statements_only"),
    )
    assert result.unit_key == started_run.running_codex_unit_key
    assert JobStateService(db).unit(
        started_run.job_id,
        started_run.running_codex_unit_key,
    ).status is UnitStatus.SUCCESS
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
    batch_key: str
    statements: tuple[StatementProposal, ...]
```

The service requires the exact model, `max`, tool count 0, and `boundary_mode='stored_statements_only'`; validates JSON, run ID, and `batch_key`; requires the named unit to belong to that run's job, have stage `CODEX_ANALYSIS`, and be `running`; rejects unknown fields and any referenced segment outside `analysis_run_segments`. On success, one transaction stores canonical output JSON and SHA-256 in `analysis_run_outputs`, appends `transport_validated`, and calls `complete_unit_in_transaction`. On validation failure, no output row is written; a separate failure transaction appends the safe run failure event and calls `fail_unit_in_transaction`, then raises the domain error. After `resume`, a corrected retry may append a newer `transport_validated` event, so historical failed attempts remain visible without permanently poisoning the run.

`0007_analysis_outputs.sql` stores one immutable row per `(run_id, unit_key)` with the manifest batch ordinal, canonical output, output hash, and receipt fields; UPDATE/DELETE is rejected. M2 accepts the single manifest key `codex:batch:1`, while the composite key keeps the schema compatible with a later versioned multi-batch contract. It forbids an output row for a non-Codex or foreign job unit and does not mark the run fully accepted until Tasks 9–16 validate, project, and promote the result.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_codex_contract.py tests/backend/integration/test_analysis_output_acceptance.py -q`

Expected: focused tests pass, including unknown field, malformed JSON, foreign run ID, and invented segment rejection.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 5: Commit Task 8**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0007_analysis_outputs.sql src/market_voice_forecast_ledger/services/codex_contract.py tests/backend/unit/test_codex_contract.py tests/backend/integration/test_analysis_output_acceptance.py
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
- Consumes: all ordered `ValidatedAnalysisOutput` batches, `AnalysisRepository`, `SpeakerRepository`, `JobStateService`, `STATEMENT_NORMALIZATION_UNIT_KEY`
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

Before normalization, the caller starts `STATEMENT_NORMALIZATION_UNIT_KEY`. The service requires that unit to be `running`, verifies every manifest Codex batch is `success` and has exactly one stored output, then reads the batches by manifest ordinal. For every evidence link, load the exact run segment and current retained transcript text, require `excerpt in text_body`, preserve batch/proposal/evidence order, and require at most 300 Unicode code points. Personal run evidence must be a subject assignment for that person; organization run evidence must be a `channel_organization` assignment. The service stores all four statement types, but marks only future forecasts as downstream heatmap candidates.

Statement rows, evidence links, their deterministic output hash, and `STATEMENT_NORMALIZATION_UNIT_KEY` success commit in one transaction. A semantic/evidence failure rolls back every row from that unit, marks only that unit failed with a safe code in a failure transaction, and is restartable from the unit beginning. A successful unit is not rerun; resume reuses its immutable rows and output hash.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_statement_validation.py tests/backend/integration/test_statement_evidence.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 9**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0008_statements.sql src/market_voice_forecast_ledger/domain/statements.py src/market_voice_forecast_ledger/repositories/statements.py src/market_voice_forecast_ledger/services/statements.py tests/backend/unit/test_statement_validation.py tests/backend/integration/test_statement_evidence.py
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
- Consumes: `NormalizedStatement`, source video `published_at`, `AuditRepository`, `JobStateService`, `PERIOD_NORMALIZATION_UNIT_KEY`
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


def test_relative_next_week_uses_fixed_jst_across_utc_date_boundary():
    result = normalize_period("来週", datetime(2026, 8, 16, 15, 30, tzinfo=timezone.utc))
    assert to_jst(datetime(2026, 8, 16, 15, 30, tzinfo=timezone.utc)).date() == date(2026, 8, 17)
    assert result.start_date == date(2026, 8, 24)
    assert result.end_date == date(2026, 8, 30)


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
    local_day = to_jst(published_at).date()
    monday = local_day - timedelta(days=local_day.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


def add_months(day: date, offset: int) -> date:
    month_index = day.year * 12 + day.month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))
```

Support exact explicit `YYYY年`, `YYYY年M月`, `YYYY年M月第1週`, and exact relative `今週`, `来週`, `再来週`, `今月`, `来月`, `再来月`, `半年後`. Explicit forms use `explicit_statement`; relative forms convert UTC `published_at` with the Task 1 fixed-JST helper and store the actual source timestamp. Expressions such as `しばらく`, `当面`, `近いうち`, missing periods, and unsupported parses return `is_unknown=True` without invented dates. Windows tests must pass without `ZoneInfo` or `tzdata`.

Before `normalize_run`, the caller starts `PERIOD_NORMALIZATION_UNIT_KEY`. The service requires statement normalization success and a running period unit. All `analysis_statement_periods` rows, their deterministic output hash, and the period unit's `success` commit in one transaction. A parse/storage failure rolls back every period row from that attempt and records only the failed unit/safe code in a failure transaction; a retry starts this unit from the beginning while reusing successful statement rows.

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
git add -- src/market_voice_forecast_ledger/db/migrations/0009_periods.sql src/market_voice_forecast_ledger/domain/periods.py src/market_voice_forecast_ledger/repositories/periods.py src/market_voice_forecast_ledger/services/periods.py tests/backend/unit/test_period_normalization.py tests/backend/integration/test_period_reviews.py
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
- Consumes: `NormalizedStatement`, `StatementContext`, Codex asset hints, `JobStateService`, `ASSET_MAPPING_UNIT_KEY`
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

Before `map_run`, the caller starts `ASSET_MAPPING_UNIT_KEY`. The service requires statement and period units to be `success` and the mapping unit to be `running`. All immutable mapping rows, app-rule evidence, their deterministic output hash, and mapping-unit `success` commit in one transaction. Any rule/storage failure rolls back that unit's mapping rows and records only its failed state/safe code in a failure transaction. `low` and `unresolved` are valid completed mapping outputs with `review_required`; they do not fail the job and remain ineligible until Task 12 review.

- [ ] **Step 4: Add immutable mapping schema**

`0010_asset_mappings.sql` creates `analysis_asset_mappings(run_id, statement_id, original_expression, asset, mapping_kind, conversion_reason, codex_confidence, rule_confidence, final_confidence, confidence_disagrees, rule_evidence_json, source_video_id)` with one row per statement/asset and UPDATE/DELETE triggers. `rule_evidence_json` stores only segment IDs, evidence kind, market codes, and boolean competition results, not transcript bodies.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_asset_mapping_storage.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 11**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0010_asset_mappings.sql src/market_voice_forecast_ledger/domain/mappings.py src/market_voice_forecast_ledger/repositories/mappings.py src/market_voice_forecast_ledger/services/asset_mapping.py tests/backend/unit/test_asset_mapping_rules.py tests/backend/integration/test_asset_mapping_storage.py
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
git add -- src/market_voice_forecast_ledger/db/migrations/0011_mapping_reviews.sql src/market_voice_forecast_ledger/services/mapping_review.py tests/backend/integration/test_mapping_reviews.py
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
- Consumes: statement, period, effective mapping, source video, review services, `JobStateService`, `FORECAST_PROJECTION_UNIT_KEY`
- Produces: `ForecastCandidate`, `PublicationCandidate`, `ResolvedPublicationGroup`, `ProjectedForecast`, `ForecastDirectionEvidence`
- Produces: `resolve_publication_groups(candidates: Sequence[PublicationCandidate]) -> ResolvedPublicationGroup`
- Produces: `select_current(candidates: Sequence[ForecastCandidate]) -> ResolvedPublicationGroup`
- Produces: `ForecastProjectionService.effective_review_state_hash(run_id: int) -> str`
- Produces: `ForecastProjectionService.project_run(run_id: int, trigger_kind: ProjectionTrigger) -> ForecastProjectionBatch`
- Produces, internal only: `ForecastProjectionService._project_run_in_transaction(run_id: int, trigger_kind: ProjectionTrigger) -> ForecastProjectionBatch`
- Produces: `ForecastRepository.list_batch_forecasts(batch_id: int) -> tuple[ProjectedForecast, ...]`

- [ ] **Step 1: Write failing projection tests**

```python
def test_non_future_statement_never_becomes_forecast():
    candidates = build_candidates(statement_type=StatementType.CURRENT_ANALYSIS)
    assert candidates == ()


@pytest.mark.parametrize("upward,downward", [
    (DirectionKind.UP, DirectionKind.DOWN),
    (DirectionKind.STRONG_UP, DirectionKind.DOWN),
    (DirectionKind.UP, DirectionKind.STRONG_DOWN),
])
def test_same_published_at_opposite_families_form_disagreement_across_videos_and_bases(upward, downward):
    result = select_current(same_timestamp_cross_video_candidates(upward, downward, bases=(ForecastBasis.DIRECT, ForecastBasis.INFERRED)))
    assert result.view_relation is ViewRelation.DISAGREEMENT
    assert set(result.directions) == {upward, downward}


def test_later_opposite_video_changes_current_view():
    result = select_current(original_down_then_later_up())
    assert result.primary_direction is DirectionKind.UP
    assert result.view_relation is ViewRelation.CHANGED


def test_later_same_direction_repost_does_not_erase_an_earlier_change():
    result = select_current(original_down_then_up_then_later_up_repost())
    assert result.primary_direction is DirectionKind.UP
    assert result.view_relation is ViewRelation.CHANGED
    assert result.evidence_count == 2


def test_same_timestamp_conflict_does_not_cross_condition_layers():
    result = project_condition_layers(same_timestamp_conditional_up_and_unconditional_down())
    assert result.unconditional.primary_direction is DirectionKind.DOWN
    assert result.conditional.primary_direction is DirectionKind.UP


def test_same_direction_repost_increases_independent_evidence_count():
    result = select_current(original_up_then_repost_up())
    assert result.evidence_count == 2
```

- [ ] **Step 2: Run forecast tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_forecast_projection.py -q`

Expected: FAIL because forecast projection does not exist.

- [ ] **Step 3: Implement candidate gating and conflict preservation**

Only `future_forecast` statements with an effective eligible mapping enter. Conditional and unconditional candidates are grouped separately. Unknown period requires latest `approve_unknown`; rejected or unreviewed unknown is excluded. `low`/`unresolved` mapping requires an effective approve/correct review. Turning points remain turning points, and `unknown` does not become flat.

For the initial projection, the caller computes `effective_review_state_hash(run_id)` from the ordered latest mapping/period review IDs and decisions, then starts `FORECAST_PROJECTION_UNIT_KEY` with that value as `external_input_hash`. `project_run(run_id, ProjectionTrigger.INITIAL)` recomputes and compares the review-state hash, requires successful statement, period, and mapping units plus a running projection unit, opens one transaction, calls `_project_run_in_transaction`, hashes the complete batch and links, and completes the projection unit in that transaction. A review change after the unit was first bound produces `UNIT_INPUT_CHANGED` and requires a successor job rather than mutating this run's unit contract. A projection failure rolls back the entire new batch and records the unit failure/safe code separately. Task 16 review application calls the same internal method with `mapping_review` or `period_review` inside its broader review transaction; that path creates a new immutable batch but does not change the already-successful projection unit or job.

`ForecastProjectionService.project_run` first partitions eligible candidates by subject, asset, comparable effective period/unknown column, and condition layer, then calls `select_current` with one already-scoped comparison group. `select_current` converts each raw `ForecastCandidate` to `PublicationCandidate` and calls `resolve_publication_groups`; the service combines the returned neutral `ResolvedPublicationGroup` with the original group key to create `ProjectedForecast`. Task 16 converts each saved `ProjectedForecast` current direction into the same neutral candidate type while retaining its source forecast ID and original period, then calls the resolver after partitioning by an intersecting display slot. The shared resolver therefore has no dependency on either the original-period row type or the heatmap-cell type.

```python
@dataclass(frozen=True)
class PublicationCandidate:
    published_at: datetime
    direction: DirectionKind
    forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence
    inherited_view_relation: ViewRelation
    evidence_statement_ids: tuple[int, ...]
    inherited_counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]
    stable_order_key: str


@dataclass(frozen=True)
class ResolvedPublicationGroup:
    primary_direction: DirectionKind
    directions: tuple[DirectionKind, ...]
    view_relation: ViewRelation
    selected_published_at: datetime
    selected_forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence
    supporting_statement_ids: tuple[int, ...]
    counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]
    evidence_count: int
    stable_selection_key: str
```

`resolve_publication_groups` groups its already-comparable input only by UTC-normalized `published_at`. Raw Task 13 candidates set `inherited_view_relation=current`; Task 16 adapters copy the saved forecast relation. Treat `+1/+2` as the upward family and `-1/-2` as the downward family. If one publication-time group contains both families, produce one `disagreement` group retaining every direction and evidence link, regardless of video ID, forecast basis, direct/inferred asset mapping, or period specificity. Select the newest publication-time group as current. A disagreement in that newest group takes precedence over `changed`; otherwise, set `changed` when its single upward/downward family is opposite to any older eligible publication-time group, or when any supporting candidate in the newest group already has `inherited_view_relation=changed`. Thus a later same-direction repost does not erase an earlier change. Retain every older group and inherited counterevidence as history/counterevidence. Flat, turning point, unknown, and no-evidence remain distinct and do not automatically become disagreement or changed. Neither adapter nor resolver rewrites source periods.

Detect the upward/downward conflict before applying a representative rank. When a publication-time group has no such conflict, select its representative by direct forecast basis, then period specificity; use `stable_order_key = youtube_video_id + ':' + zero-padded statement_id` only for stable serialization after those semantic ranks. `period_specificity` is deterministic: approved unknown `0`, normalized spans longer than 31 days `1`, spans of 8～31 days `2`, and spans of 1～7 days `3`; a higher value is more specific. A disagreement may still use that rank to fill `primary_direction`, `selected_forecast_basis`, and a stable key for storage, but consumers must render `directions` and `view_relation=disagreement`; the primary value has no authority to suppress the other family.

Build `supporting_statement_ids` from every eligible publication group whose direction or upward/downward family supports the newest selected direction set; therefore an older same-direction original, clip, Short, or repost remains an independent supporting statement. Put older opposing or otherwise nonmatching IDs in `counterevidence_statement_ids`. `evidence_count` is the distinct supporting-ID count, not merely the newest timestamp count. Aggregate `mapping_kind` and `confidence` only from the newest current group, conservatively to `inferred` if any current supporting candidate is inferred and to the weakest current confidence. `source_forecast_ids` retain every participating source in stable order for supporting/history detail. `low`/`unresolved` and other ineligible candidates enter neither the publication-time conflict group nor either evidence set until effective review allows them.

- [ ] **Step 4: Add immutable run forecast projection schema**

`0012_forecast_projections.sql` creates append-only `forecast_projection_batches(run_id, trigger_kind CHECK initial/mapping_review/period_review, latest_mapping_review_id, latest_period_review_id, created_at)`, `analysis_forecasts(projection_batch_id, run_id, asset, mapping_kind, period_start, period_end, unknown_period, condition_kind, condition_text, view_relation, primary_direction, directions_json, confidence, evidence_count, selected_published_at, selected_forecast_basis, period_specificity, stable_selection_key, heatmap_eligible, exclusion_reason)`, and `analysis_forecast_statement_links(forecast_id, statement_id, relation_kind CHECK supporting/counterevidence)`. All three reject UPDATE/DELETE. `directions_json` is canonical and contains at least two distinct directions for disagreement. `selected_forecast_basis` and `period_specificity` preserve the representative rank for later week/month slot resolution; they never suppress an opposing direction in the same publication group. The repository reconstructs `ResolvedPublicationGroup` from the forecast row and its ordered supporting/counterevidence links. Each projection call creates a new batch and never edits an earlier batch, so a later review can deterministically reproject the same Codex run.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_forecast_projection.py -q`

Expected: focused tests pass, including conditional separation, turning point preservation, empty XAU/USD, and no duplicate suppression.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 13**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0012_forecast_projections.sql src/market_voice_forecast_ledger/domain/forecasts.py src/market_voice_forecast_ledger/repositories/forecasts.py src/market_voice_forecast_ledger/services/forecast_projection.py tests/backend/unit/test_forecast_selection.py tests/backend/integration/test_forecast_projection.py
git commit -m "feat: project current forecast candidates"
```

### Task 14: 検証済みrunの現在行を置換するtransaction内primitive

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0013_current_results.sql`
- Create: `src/market_voice_forecast_ledger/services/current_results.py`
- Create: `tests/backend/integration/test_atomic_result_replacement.py`
- Create: `tests/backend/integration/test_current_scope_isolation.py`

**Interfaces:**
- Consumes: `AnalysisRepository`, `StatementRepository`, `MappingRepository`, `ForecastRepository`, `JobStateService`, `FINAL_PROMOTION_UNIT_KEY`
- Produces: `CurrentResultDelta(before: CurrentResultSummary, after: CurrentResultSummary)`
- Produces, internal only: `CurrentResultService._replace_scope_rows_in_transaction(run_id: int, projection_batch_id: int) -> CurrentResultDelta`
- Produces: `CurrentResultService.get_scope(scope_id: int) -> CurrentResultSummary`

- [ ] **Step 1: Write failing atomic replacement tests**

```python
def test_failed_internal_replacement_leaves_previous_current_result_after_reopen(db_path, current_scope, completed_upstream_run, monkeypatch):
    db = open_database(db_path)
    before = CurrentResultService(db).get_scope(current_scope.id)
    scope_before = AnalysisRepository(db).get_scope(current_scope.id)
    monkeypatch.setattr(CurrentResultService, "_copy_forecasts", Mock(side_effect=sqlite3.OperationalError("synthetic")))
    with pytest.raises(sqlite3.OperationalError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                completed_upstream_run.id,
                completed_upstream_run.projection_batch_id,
            )
    db.close()
    reopened = open_database(db_path)
    assert CurrentResultService(reopened).get_scope(current_scope.id) == before
    assert AnalysisRepository(reopened).get_scope(current_scope.id) == scope_before


def test_replacing_one_cutoff_does_not_change_another(db, two_cutoff_scopes, replacement_run):
    other_before = CurrentResultService(db).get_scope(two_cutoff_scopes.other.id)
    with transaction(db):
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            replacement_run.id,
            replacement_run.projection_batch_id,
        )
    assert CurrentResultService(db).get_scope(two_cutoff_scopes.other.id) == other_before


def test_internal_current_writer_rejects_calls_without_outer_transaction(db, completed_upstream_run):
    with pytest.raises(DomainError) as error:
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            completed_upstream_run.id,
            completed_upstream_run.projection_batch_id,
        )
    assert error.value.code == "CURRENT_REPLACEMENT_TRANSACTION_REQUIRED"
```

- [ ] **Step 2: Run replacement tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_current_scope_isolation.py -q`

Expected: FAIL because current projection tables and service do not exist.

- [ ] **Step 3: Add current projection tables**

`0013_current_results.sql` creates `current_statements(scope_id, analysis_statement_id, source_run_id)`, `current_asset_mappings(scope_id, analysis_mapping_id, source_run_id, effective_asset, effective_eligibility)`, and `current_forecasts(scope_id, analysis_forecast_id, source_run_id, projection_batch_id)`. Keys are unique within a scope; foreign keys point to immutable run results and a projection batch. Current tables are replaceable only through `CurrentResultService`; direct repository mutation methods stay private.

- [ ] **Step 4: Implement the transaction-internal current-row writer**

```python
def _replace_scope_rows_in_transaction(self, run_id: int, projection_batch_id: int) -> CurrentResultDelta:
    if not self._conn.in_transaction:
        raise DomainError(
            "CURRENT_REPLACEMENT_TRANSACTION_REQUIRED",
            "current rows may change only inside a caller-owned transaction",
        )
    self._validate_complete_projection(run_id, projection_batch_id)
    run = self._analysis.get_run(run_id)
    before = self._current.safe_summary(run.scope_id)
    self._current.delete_scope_rows(run.scope_id)
    self._copy_statements(run_id, run.scope_id)
    self._copy_mappings(run_id, run.scope_id)
    self._copy_forecasts(projection_batch_id, run.scope_id)
    after = self._current.safe_summary(run.scope_id)
    return CurrentResultDelta(before, after)
```

Validation requires the run's active analysis job attempt to exist; every upstream manifest unit before the named final `heatmap_update` promotion unit to be `success`; one stored transport output for every Codex batch unit; and the exact statement, evidence, period, mapping, and forecast batch hashes recorded by their reserved units. The run's newest effective event must not be `failed`; older failed-attempt events are allowed after a newer successful retry event. The final promotion unit itself must be `pending` or `running`, never already `success`, and an older superseded job attempt may never promote. The primitive requires an existing caller-owned SQLite transaction, never commits, never changes scope status, never appends run/audit events, and never marks a unit or job successful. There is deliberately no public `replace_scope` mutation method. At Task 14, any validation or DB failure leaves the old current rows and prior scope status unchanged; fault-injection tests close the connection after failure and verify that persisted state through a new connection. Task 16 is the first task that exposes a public current-result mutation and combines this primitive with heatmap cache, run/audit events, final-unit success, and job success.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_current_scope_isolation.py -q`

Expected: focused tests pass.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 14**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0013_current_results.sql src/market_voice_forecast_ledger/services/current_results.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_current_scope_isolation.py
git commit -m "feat: add transactional current result writer"
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
git add -- src/market_voice_forecast_ledger/services/corrections.py src/market_voice_forecast_ledger/repositories/sources.py src/market_voice_forecast_ledger/repositories/speakers.py src/market_voice_forecast_ledger/repositories/analysis.py tests/backend/integration/test_speaker_corrections.py tests/backend/integration/test_channel_policy_corrections.py tests/backend/integration/test_stale_transitions.py
git commit -m "feat: audit corrections and stale scopes"
```

### Task 16: 再生成可能な週・月heatmap cache、16行、時期不明列

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0014_heatmap.sql`
- Create: `src/market_voice_forecast_ledger/repositories/heatmap.py`
- Create: `src/market_voice_forecast_ledger/services/heatmap.py`
- Modify: `src/market_voice_forecast_ledger/services/current_results.py`
- Modify: `src/market_voice_forecast_ledger/services/mapping_review.py`
- Modify: `src/market_voice_forecast_ledger/services/periods.py`
- Create: `src/market_voice_forecast_ledger/services/review_application.py`
- Modify: `tests/backend/integration/test_atomic_result_replacement.py`
- Create: `tests/backend/integration/test_heatmap_cache.py`
- Create: `tests/backend/integration/test_heatmap_unknown_and_disagreement.py`
- Create: `tests/backend/integration/test_final_promotion_recovery.py`
- Create: `tests/backend/integration/test_review_application.py`

**Interfaces:**
- Consumes: current forecast projections, `PublicationCandidate`, `resolve_publication_groups`, subjects, assets, periods, `JobStateService`, `FINAL_PROMOTION_UNIT_KEY`
- Produces: `HeatmapService.rebuild_scope(scope_id: int) -> int`
- Produces, internal only: `HeatmapService._rebuild_scope_in_transaction(scope_id: int) -> int`
- Produces: `HeatmapService.read_scope(scope_id: int, granularity: HeatmapGranularity) -> HeatmapView`
- Produces: `HeatmapService.read_cutoff(cutoff_day: date, granularity: HeatmapGranularity) -> HeatmapView`
- Produces: `CurrentResultService.promote_completed_run(run_id: int, projection_batch_id: int) -> CurrentResultSummary`
- Produces: `ReviewApplicationService.apply_mapping(command: MappingReviewCommand) -> ReviewApplicationResult`
- Produces: `ReviewApplicationService.apply_period(period_id: int, decision: PeriodReviewDecision, actor: str, reason: str) -> ReviewApplicationResult`
- Produces: `ReviewApplicationResult(applied_to_current: bool, current_summary: CurrentResultSummary | None, rebuilt_cell_count: int)`
- Produces: `HeatmapView.rows: tuple[HeatmapRow, ...]`
- Produces: `HeatmapView.cell(subject_key: str, asset: Asset, period_key: str, condition_kind: ConditionKind = ConditionKind.UNCONDITIONAL) -> HeatmapCell`
- Produces: `HeatmapCell(primary_direction, directions, mapping_kind, confidence, evidence_count, condition_kind, condition_texts, view_relation, unknown_period, source_forecast_ids)`

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


def test_overlapping_source_periods_share_display_slot_without_mixing_conditions(db, overlapping_period_scope):
    view = HeatmapService(db).read_cutoff(date(2026, 8, 14), HeatmapGranularity.WEEK)
    unconditional = view.cell("synthetic_subject", Asset.NIKKEI_225, "2026-08-17/2026-08-23", ConditionKind.UNCONDITIONAL)
    conditional = view.cell("synthetic_subject", Asset.NIKKEI_225, "2026-08-17/2026-08-23", ConditionKind.CONDITIONAL)
    assert unconditional.view_relation is ViewRelation.DISAGREEMENT
    assert set(unconditional.directions) == {DirectionKind.UP, DirectionKind.DOWN}
    assert len(unconditional.source_forecast_ids) == 2
    assert conditional.primary_direction is DirectionKind.UP
    assert conditional.view_relation is not ViewRelation.DISAGREEMENT
    assert len(conditional.source_forecast_ids) == 1


def test_existing_changed_relation_survives_slot_projection_with_one_source_forecast(db, changed_source_scope):
    view = HeatmapService(db).read_cutoff(date(2026, 8, 14), HeatmapGranularity.WEEK)
    cell = view.cell("synthetic_subject", Asset.SP500, "2026-08-17/2026-08-23")
    assert cell.primary_direction is DirectionKind.UP
    assert cell.view_relation is ViewRelation.CHANGED


def test_successful_final_promotion_commits_display_and_job_success_together(db, completed_upstream_run):
    JobStateService(db).begin_unit(completed_upstream_run.job_id, FINAL_PROMOTION_UNIT_KEY)
    summary = CurrentResultService(db).promote_completed_run(
        completed_upstream_run.id,
        completed_upstream_run.projection_batch_id,
    )
    assert summary.scope_id == completed_upstream_run.scope_id
    assert AnalysisRepository(db).get_effective_run_status(completed_upstream_run.id) is AnalysisRunStatus.ACCEPTED
    assert AnalysisRepository(db).get_scope(completed_upstream_run.scope_id).status is ScopeStatus.CURRENT
    assert AuditRepository(db).list_for_entity("analysis_scope", str(completed_upstream_run.scope_id))[-1].operation == "result_replaced"
    assert HeatmapService(db).read_scope(completed_upstream_run.scope_id, HeatmapGranularity.WEEK).rows
    assert JobStateService(db).unit(completed_upstream_run.job_id, FINAL_PROMOTION_UNIT_KEY).status is UnitStatus.SUCCESS
    assert JobStateService(db).status(completed_upstream_run.job_id) is JobStatus.SUCCEEDED


def test_final_promotion_failure_keeps_old_state_and_retries_only_final_unit(db_path, completed_upstream_run, monkeypatch):
    db = open_database(db_path)
    current_before = CurrentResultService(db).get_scope(completed_upstream_run.scope_id)
    heatmap_before = HeatmapService(db).read_scope(completed_upstream_run.scope_id, HeatmapGranularity.WEEK)
    scope_before = AnalysisRepository(db).get_scope(completed_upstream_run.scope_id)
    run_status_before = AnalysisRepository(db).get_effective_run_status(completed_upstream_run.id)
    audit_before = AuditRepository(db).list_for_entity("analysis_scope", str(completed_upstream_run.scope_id))
    JobStateService(db).begin_unit(completed_upstream_run.job_id, FINAL_PROMOTION_UNIT_KEY)
    monkeypatch.setattr(HeatmapService, "_insert_cells", Mock(side_effect=sqlite3.OperationalError("synthetic")))
    with pytest.raises(sqlite3.OperationalError):
        CurrentResultService(db).promote_completed_run(completed_upstream_run.id, completed_upstream_run.projection_batch_id)
    db.close()

    reopened = open_database(db_path)
    plan = JobStateService(reopened).resume(completed_upstream_run.job_id, completed_upstream_run.upstream_artifact_hashes)
    assert CurrentResultService(reopened).get_scope(completed_upstream_run.scope_id) == current_before
    assert HeatmapService(reopened).read_scope(completed_upstream_run.scope_id, HeatmapGranularity.WEEK) == heatmap_before
    assert AnalysisRepository(reopened).get_scope(completed_upstream_run.scope_id) == scope_before
    assert AnalysisRepository(reopened).get_effective_run_status(completed_upstream_run.id) is run_status_before
    assert AuditRepository(reopened).list_for_entity("analysis_scope", str(completed_upstream_run.scope_id)) == audit_before
    assert plan.next_unit_key == FINAL_PROMOTION_UNIT_KEY
    assert plan.pending_unit_keys == (FINAL_PROMOTION_UNIT_KEY,)
    assert JobStateService(reopened).unit(completed_upstream_run.job_id, FINAL_PROMOTION_UNIT_KEY).status is UnitStatus.PENDING


def test_review_after_acceptance_reprojects_current_and_heatmap_without_new_codex_run(db, accepted_scope_with_low_mapping):
    run_count_before = AnalysisRepository(db).count_runs(accepted_scope_with_low_mapping.scope_id)
    result = ReviewApplicationService(db).apply_mapping(accepted_scope_with_low_mapping.approve_command)
    assert result.applied_to_current is True
    assert AnalysisRepository(db).count_runs(accepted_scope_with_low_mapping.scope_id) == run_count_before
    assert result.current_summary.eligible_mapping_count == 1
    assert result.rebuilt_cell_count > 0
    assert HeatmapService(db).read_scope(
        accepted_scope_with_low_mapping.scope_id,
        HeatmapGranularity.WEEK,
    ).rows


def test_current_review_failure_rolls_back_review_projection_current_and_heatmap(db, accepted_scope_with_low_mapping, monkeypatch):
    fixture = accepted_scope_with_low_mapping
    before = (
        db.execute("SELECT COUNT(*) FROM mapping_reviews WHERE mapping_id=?", (fixture.mapping_id,)).fetchone()[0],
        db.execute("SELECT COUNT(*) FROM forecast_projection_batches WHERE run_id=?", (fixture.run_id,)).fetchone()[0],
        CurrentResultService(db).get_scope(fixture.scope_id),
        HeatmapService(db).read_scope(fixture.scope_id, HeatmapGranularity.WEEK),
    )
    monkeypatch.setattr(HeatmapService, "_insert_cells", Mock(side_effect=sqlite3.OperationalError("synthetic")))
    with pytest.raises(sqlite3.OperationalError):
        ReviewApplicationService(db).apply_mapping(fixture.approve_command)
    assert (
        db.execute("SELECT COUNT(*) FROM mapping_reviews WHERE mapping_id=?", (fixture.mapping_id,)).fetchone()[0],
        db.execute("SELECT COUNT(*) FROM forecast_projection_batches WHERE run_id=?", (fixture.run_id,)).fetchone()[0],
        CurrentResultService(db).get_scope(fixture.scope_id),
        HeatmapService(db).read_scope(fixture.scope_id, HeatmapGranularity.WEEK),
    ) == before
```

- [ ] **Step 2: Run heatmap tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_heatmap_cache.py tests/backend/integration/test_heatmap_unknown_and_disagreement.py tests/backend/integration/test_final_promotion_recovery.py -q`

Expected: FAIL because heatmap schema and service do not exist.

- [ ] **Step 3: Add rebuildable cache schema and projection**

`0014_heatmap.sql` creates `heatmap_cells(scope_id, subject_id, asset, granularity CHECK week/month, period_key, period_start, period_end, condition_kind, condition_texts_json, primary_direction, directions_json, mapping_kind, confidence, evidence_count, view_relation, unknown_period, UNIQUE(scope_id, subject_id, asset, granularity, period_key, condition_kind))` and `heatmap_cell_forecasts(heatmap_cell_id REFERENCES heatmap_cells(id) ON DELETE CASCADE, source_forecast_id REFERENCES analysis_forecasts(id), ordinal, UNIQUE(heatmap_cell_id, source_forecast_id), UNIQUE(heatmap_cell_id, ordinal))`. Both tables are rebuildable cache, not audit history. `condition_texts_json`, `directions_json`, and source-link ordinals are canonical and deterministic.

`rebuild_scope` is the safe standalone cache-repair operation: it opens one transaction, calls `_rebuild_scope_in_transaction`, and commits only after the whole scope cache is rebuilt. `_rebuild_scope_in_transaction` requires `conn.in_transaction`, deletes only one subject/cutoff scope's cells and join rows, and repopulates them from `current_forecasts` without committing. It calls `_insert_cells(scope_id, projected_cells)` after deletion; this helper also never commits and is the deterministic child-process fault point used by Task 19.

`read_cutoff` finds the current scope for each of the four active MVP subjects at the selected JST cutoff and combines them into one 16-row view; a missing subject scope becomes four empty asset rows, not an `unknown` forecast. Week cells use JST Monday–Sunday absolute keys; month cells use JST calendar month keys. Source forecast periods remain unchanged; every eligible forecast whose normalized period intersects a display slot is retained through `heatmap_cell_forecasts` in stable source order. Expand each saved forecast's current `primary_direction` or `directions_json`, `view_relation`, `selected_published_at`, `selected_forecast_basis`, `period_specificity`, `mapping_kind`, `confidence`, supporting links, inherited counterevidence links, and `stable_selection_key` into Task 13 `PublicationCandidate`, then call `resolve_publication_groups` for each subject/asset/slot/condition partition. Do not turn an older counterevidence link into a new current candidate. The cell's `mapping_kind` and `confidence` come from the resolver's newest current group; `evidence_count` includes its full same-direction supporting set across publication times. Source links retain every participating forecast for the evidence drawer. Thus equal-publication-time opposition becomes disagreement, later opposition becomes changed, and a source forecast's existing changed relation is not lost merely because no second source period overlaps that slot. Conditional and unconditional layers stay separate, while all distinct conditional texts remain inspectable in canonical order. An approved unknown period uses key `unknown` in both granularities. Unapproved/rejected unknown, non-future statements, and ineligible mappings create no cell. A disagreement retains multiple directions.

- [ ] **Step 4: Make current replacement and cache rebuild atomic**

`promote_completed_run` is the only public path that changes current rows from a completed analysis run. It resolves the run's active analysis job attempt, rejects an older superseded attempt, requires every unit before `FINAL_PROMOTION_UNIT_KEY` to be verified `success`, and requires the active final unit to be `running`. In one caller-owned transaction it calls Task 14's current-row primitive, calls `_rebuild_scope_in_transaction`, updates scope status, appends run acceptance and result-replacement audit events, computes a deterministic output hash from the current/heatmap summaries, calls `complete_unit_in_transaction`, and calls `succeed_job_in_transaction`. It then commits once. An injected failure rolls back current rows, cache rows, events, final-unit success, and job success; after reopening, `resume` returns the stale `running` final unit to `pending` and reuses upstream success units.

`ReviewApplicationService` is the only public path that applies a mapping or period review to an already-current run. It uses transaction-internal review insertion, reprojection, current-row replacement, and heatmap rebuild, but does not alter the already-succeeded analysis job or append another accepted run event. A post-analysis review updates the review event and its audit event, projection batch, current rows, cache, and result-replacement audit event together. An injected review, projection, current-copy, or heatmap failure rolls all of them back. The Task 10/12 low-level `review` methods continue to support not-yet-current or historical runs, but after Task 16 they reject a target belonging to the current accepted run with `REVIEW_APPLICATION_REQUIRED`; this prevents a caller from recording an effective current review without rebuilding the display.

- [ ] **Step 5: Run focused and full backend tests**

Run: `python -m pytest tests/backend/integration/test_heatmap_cache.py tests/backend/integration/test_heatmap_unknown_and_disagreement.py tests/backend/integration/test_final_promotion_recovery.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_review_application.py -q`

Expected: focused tests pass; deleting all cache rows and join rows followed by rebuild produces the same serialized view; final-promotion failure remains rolled back after reopening and retries only `FINAL_PROMOTION_UNIT_KEY`.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 6: Commit Task 16**

```powershell
git add -- src/market_voice_forecast_ledger/db/migrations/0014_heatmap.sql src/market_voice_forecast_ledger/repositories/heatmap.py src/market_voice_forecast_ledger/services/heatmap.py src/market_voice_forecast_ledger/services/current_results.py src/market_voice_forecast_ledger/services/mapping_review.py src/market_voice_forecast_ledger/services/periods.py src/market_voice_forecast_ledger/services/review_application.py tests/backend/integration/test_atomic_result_replacement.py tests/backend/integration/test_heatmap_cache.py tests/backend/integration/test_heatmap_unknown_and_disagreement.py tests/backend/integration/test_final_promotion_recovery.py tests/backend/integration/test_review_application.py
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
git add -- src/market_voice_forecast_ledger/db/migrations/0015_retention.sql src/market_voice_forecast_ledger/repositories/retention.py src/market_voice_forecast_ledger/services/retention.py tests/backend/unit/test_retention_policy.py tests/backend/integration/test_text_deletion.py tests/backend/integration/test_audio_cleanup.py
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
git add -- README.md src/market_voice_forecast_ledger/api/__init__.py src/market_voice_forecast_ledger/api/app.py src/market_voice_forecast_ledger/api/dependencies.py src/market_voice_forecast_ledger/api/models.py src/market_voice_forecast_ledger/api/routes/__init__.py src/market_voice_forecast_ledger/api/routes/health.py src/market_voice_forecast_ledger/api/routes/subjects.py src/market_voice_forecast_ledger/api/routes/heatmaps.py src/market_voice_forecast_ledger/api/routes/jobs.py src/market_voice_forecast_ledger/api/routes/reviews.py src/market_voice_forecast_ledger/api/routes/corrections.py src/market_voice_forecast_ledger/api/routes/retention.py src/market_voice_forecast_ledger/cli.py tests/backend/integration/test_api_reads.py tests/backend/integration/test_api_writes.py tests/backend/integration/test_api_private_boundary.py
git commit -m "feat: expose loopback forecast ledger api"
```

### Task 19: 合成E2E、Windows検証入口、開発状態更新

**Files:**
- Create: `tests/backend/e2e/synthetic_fixture.py`
- Create: `tests/backend/e2e/test_synthetic_heatmap_flow.py`
- Create: `tests/backend/e2e/test_synthetic_review_and_conflict_flow.py`
- Create: `tests/backend/integration/crash_promotion_worker.py`
- Create: `tests/backend/integration/test_process_crash_recovery.py`
- Create: `tests/backend/README.md`
- Create: `scripts/test-backend.ps1`
- Modify: `README.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/project/status.md`

**Interfaces:**
- Consumes: all Task 1–18 public service and API interfaces, Python `subprocess`, SQLite WAL recovery
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

The fixture creates four synthetic subjects and policies through `SourceRepository.create_subject` and `create_policy`, without calling production `bootstrap_reference_data`. For each subject it then calls, in order: video save, eligibility, synthetic transcript save, personal or organization assignment, input-contract preview, matching analysis job manifest, start of the input-freeze unit, scope/run begin with contract recomputation and atomic input-unit completion, then start-and-process for each Codex batch, statement normalization, period normalization, asset mapping, and forecast projection unit. Each processing service atomically stores its output and completes its own unit; required period/mapping reviews occur between their source unit and projection. The fixture then starts the final promotion unit and calls `promote_completed_run`. Finally it calls `HeatmapService.read_cutoff` for the common cutoff and serializes the API response. It uses synthetic names, IDs, channels, and utterances only; it performs no network, audio, model, Codex, shell, or external tool call.

- [ ] **Step 4: Write the failing child-process crash recovery test**

```python
CRASH_PROMOTION_WORKER = Path(__file__).with_name("crash_promotion_worker.py")


def read_persisted_state(db_path: Path, scope_id: int) -> tuple[object, object, object]:
    conn = open_database(db_path)
    try:
        return (
            CurrentResultService(conn).get_scope(scope_id),
            HeatmapService(conn).read_scope(scope_id, HeatmapGranularity.WEEK),
            AnalysisRepository(conn).get_scope(scope_id),
        )
    finally:
        conn.close()


def test_child_process_crash_mid_promotion_keeps_old_state(db_path, crash_ready_run):
    preparation = open_database(db_path)
    JobStateService(preparation).begin_unit(crash_ready_run.job_id, FINAL_PROMOTION_UNIT_KEY)
    preparation.close()
    before = read_persisted_state(db_path, crash_ready_run.scope_id)
    completed = subprocess.run(
        [sys.executable, str(CRASH_PROMOTION_WORKER), str(db_path), str(crash_ready_run.id), str(crash_ready_run.projection_batch_id)],
        check=False,
        timeout=30,
    )
    assert completed.returncode == 91
    assert read_persisted_state(db_path, crash_ready_run.scope_id) == before
    reopened = open_database(db_path)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert JobStateService(reopened).unit(
            crash_ready_run.job_id,
            FINAL_PROMOTION_UNIT_KEY,
        ).status is UnitStatus.RUNNING
        plan = JobStateService(reopened).resume(
            crash_ready_run.job_id,
            crash_ready_run.upstream_artifact_hashes,
        )
        JobStateService(reopened).require_upstream_success(
            crash_ready_run.job_id,
            FINAL_PROMOTION_UNIT_KEY,
        )
        assert plan.next_unit_key == FINAL_PROMOTION_UNIT_KEY
        assert plan.pending_unit_keys == (FINAL_PROMOTION_UNIT_KEY,)
        assert JobStateService(reopened).unit(
            crash_ready_run.job_id,
            FINAL_PROMOTION_UNIT_KEY,
        ).status is UnitStatus.PENDING
    finally:
        reopened.close()
```

- [ ] **Step 5: Run the crash recovery test and verify RED**

Run: `python -m pytest tests/backend/integration/test_process_crash_recovery.py -q`

Expected: FAIL because `crash_promotion_worker.py` does not exist.

- [ ] **Step 6: Implement the test-only crash worker**

`crash_promotion_worker.py` is test-only. The parent first commits the final unit's `running` state and closes its preparation connection. The worker opens the supplied temporary DB, replaces `HeatmapService._insert_cells` in that child process with a wrapper that calls the original and then invokes `os._exit(91)`, and calls `promote_completed_run`. The parent test uses a bounded timeout, verifies that the child exits while the SQLite transaction is open, then reopens the DB and checks old current rows, heatmap rows, scope state, `PRAGMA integrity_check`, unchanged upstream successes, and final-unit-only recovery. Never add this crash hook to production configuration or API routes.

```python
def main(argv: Sequence[str]) -> NoReturn:
    db_path, run_id, projection_batch_id = Path(argv[1]), int(argv[2]), int(argv[3])
    conn = open_database(db_path)
    original = HeatmapService._insert_cells

    def crash_after_insert(service, *args, **kwargs):
        original(service, *args, **kwargs)
        os._exit(91)

    HeatmapService._insert_cells = crash_after_insert
    CurrentResultService(conn).promote_completed_run(run_id, projection_batch_id)
    raise AssertionError("promotion unexpectedly returned")


if __name__ == "__main__":
    main(sys.argv)
```

- [ ] **Step 7: Run crash recovery and full backend tests**

Run: `python -m pytest tests/backend/integration/test_process_crash_recovery.py -q`

Expected: PASS with child return code 91, old persisted state unchanged, SQLite integrity `ok`, and only the final promotion unit pending.

Run: `python -m pytest tests/backend -q`

Expected: all collected backend tests pass.

- [ ] **Step 8: Add ASCII-compatible Windows verification command**

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

- [ ] **Step 9: Document setup and update only verified state**

`tests/backend/README.md` and root `README.md` document:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests/backend -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

They state that real transcripts, audio, embeddings, SQLite DBs, runtime logs, and credentials live outside the repository. Update `docs/project/plan.md` and `docs/project/status.md` with actual completed Task numbers, actual test counts, known gaps, and the next approved subproject. Do not record unrun results or declare M2 complete if any Task remains.

- [ ] **Step 10: Run full backend tests and final verification**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1`

Expected: backend pytest, Python compileall, existing 119 work-state tests, and working-tree public safety all exit 0.

Run: `git diff --check`

Expected: exit 0 with no output.

- [ ] **Step 11: Commit Task 19**

```powershell
git add -- README.md scripts/test-backend.ps1 tests/backend/e2e/synthetic_fixture.py tests/backend/e2e/test_synthetic_heatmap_flow.py tests/backend/e2e/test_synthetic_review_and_conflict_flow.py tests/backend/integration/crash_promotion_worker.py tests/backend/integration/test_process_crash_recovery.py tests/backend/README.md docs/project/plan.md docs/project/status.md
git commit -m "test: verify synthetic core backend flow"
```

---

## Spec Coverage Matrix

| Approved design area | Implemented and tested in |
|---|---|
| SQLite foundation, UTC storage, fixed-JST calendar and cutoff, local paths | Task 1 |
| Append-only audit and private-field rejection | Task 2 |
| Four subjects and confirmed channel IDs | Task 3 |
| Fixed/all-channel eligibility, manual URL non-bypass, no deduplication | Task 4 |
| Fixed single voice model metadata, raw score, hold band, Akatsuki organization input | Task 5 |
| Separate metadata/audio progress, unit-level checkpoints, pause/stop/retry, reopen recovery | Task 6 |
| Cutoff scopes, verified input-contract job-attempt/run linkage, safe successor attachment, personal/organization input boundary, immutable run snapshot | Task 7 |
| `gpt-5.6-sol`/`max`/zero-tool contract and schema validation | Task 8 |
| Four statement types, direct vs inferred forecast basis, multi-segment exact evidence | Task 9 |
| Explicit/published periods, month first week, reviewed unknown column | Task 10 |
| Direct/inferred asset mapping and application confidence ceiling | Task 11 |
| Low/unresolved approval, correction, rejection audit history | Task 12 |
| Conditional layer, turning point, publication-time disagreement/changed view, repost evidence | Task 13 |
| Transaction-internal current-row replacement, upstream-unit gate, reopen verification, and cutoff isolation | Task 14 |
| Speaker/channel corrections, audit, stale scopes | Task 15 |
| Four-subject × four-asset week/month heatmap, multi-source cache, and atomic final promotion | Task 16 |
| 30/90/180/365/unlimited retention and safe audio deletion | Task 17 |
| Loopback-only API, no GET mutation, private response boundary | Task 18 |
| Synthetic E2E, child-process WAL recovery, and Windows verification | Task 19 |

## Completion Criteria

- All 15 numbered migrations apply once, in order, to a new SQLite DB with foreign keys enabled.
- The four default subjects and two user-confirmed fixed channel IDs are correct; manual URLs cannot bypass them.
- Distinct original, clip, Short, and repost video IDs all remain independent and can each contribute evidence.
- Personal subject/interviewer/hold and 暁投資顧問 organization assignment rules are both enforced.
- Speaker model name/version, raw score, threshold config version are stored without common 0–1 normalization; border scores become hold.
- Different cutoff scopes coexist; analysis run history is append-only; input snapshot mutation is limited to body deletion.
- Every analysis run has an append-only immutable job-attempt history; each manifest starts with the matching `ANALYSIS_INPUT_UNIT_KEY`, ends with exactly one `FINAL_PROMOTION_UNIT_KEY`, and cannot be reused for another subject/cutoff/input contract. A successor can attach to the same run only when all durable successes are reused unchanged.
- Only exact `gpt-5.6-sol`, `max`, zero-tool, stored-statements-only receipts can proceed.
- Four statement types, direct/inferred forecast basis, conditional layer, turning point, flat, unknown, and no-evidence empty state remain distinct.
- Every displayed short evidence link references an input segment and is a continuous substring of retained transcript text.
- DB timestamps compare in UTC; UI/cutoff/relative dates use fixed UTC+9 JST without `ZoneInfo` or `tzdata`; explicit dates are labeled separately, first-week dates may cross months, and approved unknown periods use only the special column.
- App-rule confidence cannot be raised by Codex; personal interviewer-only hints cannot produce high/medium; low/unresolved require append-only review.
- Opposing upward/downward forecasts at the same publication timestamp retain both directions as disagreement regardless of video or directness; a reversal at a later timestamp becomes changed; distinct repost evidence is not suppressed.
- Interrupted or failed work restarts only its 5–10 minute unit; partial artifacts never count as success; verified compatible success units are reused.
- Overlapping source periods retain every source forecast through `heatmap_cell_forecasts`; conditional and unconditional layers never form a shared conflict group.
- No public service can replace current rows without also rebuilding the heatmap; current-run reviews cannot bypass `ReviewApplicationService`.
- Current results, accepted run event, audit event, heatmap cache, final-unit success, and job success update atomically or not at all, and fault rollback remains correct after closing and reopening the database.
- A Windows child process terminated inside the final promotion transaction leaves the prior current/heatmap/scope state readable with `PRAGMA integrity_check = ok`, and only the final promotion unit is retried; this does not claim power-loss or disk-failure durability.
- Corrections preserve prior runs/current results, mark dependent scopes stale, and require reasoned audit events.
- Text deletion preserves hashes, short evidence, and current forecasts; unsafe audio paths are never deleted and safe failures remain retryable.
- API responses exclude private body/path fields, state changes use POST, and server host validation accepts only `127.0.0.1` while documenting the absence of authentication.
- Synthetic E2E produces 16 subject/asset rows, reviewed unknown and disagreement states, independent repost evidence, and empty XAU/USD when no evidence exists.
- `scripts/test-backend.ps1`, `git diff --check`, state-document checks, and public-safety checks all pass before M2中核バックエンド完了を報告する。

---

## 2026-08-16 JST as-built addendum

この節は実装・whole-branch監査後の実ファイルと検証境界を記録する。上の
Task 1～19のcheckboxと初期の15 migration表記は、承認時点のhistorical plan
として書き換えずに残す。実装済みという意味でcheckboxを事後更新せず、現在の
受け入れ状態は `docs/project/status.md` を正本とする。

### 実装と最終hardening

- Task 1～19は隔離branch `feature/m2-core-backend` へローカル実装された。
  Task 19 commitは `3267968d67a70ecee0b6f68e13d241a73e7b634f`
  (`test: verify synthetic core backend flow`) である。
- whole-branch Fix Aはscope generationを追加し、新しいrun開始後のsuperseded
  promotionを拒否し、話者・channel policy修正のstale伝播を完全化した。
- Fix Bはmigration 0017で追記専用tableのprimary・logical identity collisionを
  plain connectionでも拒否し、audit reason/private-data境界とCodex receiptの
  tool-call件数型を厳密化した。
- Fix CはTask 1 foundationとTask 5 file-DB persistenceのcharacterization、
  18 migrationのoffline wheel回帰、no-upstream・detached HEADの作業状態script、
  as-built文書を追加・修正した。characterization testは既存挙動を記録するため
  immediate GREENであり、REDを捏造していない。
- Fix D commit `a92bcaac9b592577d1a7f1efe7b1f70326853351`
  (`fix: preserve organization analysis input`) は組織所有動画のHOLD、
  INTERVIEWER、manual SUBJECT segmentに対する個人話者修正をmutation前に
  拒否し、組織の公式segment集合を分析入力として保持した。
- Fix E commit `cb2aaafe2c07fcf282d79a61fdf0e94c81be864f`
  (`fix: verify staged public artifacts`) はstaged
  公開安全検査をworking tree pathではなく実際のindex blobへ固定した。
  NUL-safeなpath列挙、binary-safeなblob読取、内容を表示しないfail-closed、
  `.gitignore`と公開方針の禁止artifact parityを追加し、WorkingTree modeの
  content読取境界は維持した。
- Fix Fのこの文書を含むcommit (`fix: reject disguised binary public artifacts`)
  は、明示的なbinary拡張子allowlist以外のdecoded fileがNULを含む場合、
  Staged・WorkingTree両modeで内容非表示のままfail-closedにした。許可済み
  binaryと通常のUTF-8/UTF-16 textの処理は維持した。

### As-built migration manifest

実際のpackage内SQLは次の18ファイルで、この順に適用される。Task 14とTask 15の
間にはreviewで追加された別目的のduplicate `0013` が存在する。

1. `0001_foundation`
2. `0002_audit`
3. `0003_sources`
4. `0004_speakers`
5. `0005_jobs`
6. `0006_analysis_runs`
7. `0007_analysis_outputs`
8. `0008_statements`
9. `0009_periods`
10. `0010_asset_mappings`
11. `0011_mapping_reviews`
12. `0012_forecast_projections`
13. `0013_current_results`
14. `0013_video_pipeline_bindings`
15. `0014_heatmap`
16. `0015_retention`
17. `0016_scope_generations`
18. `0017_append_only_guards`

`0013_video_pipeline_bindings` はpolicy修正後に新しい動画作業を安全に停止するため、
`0016_scope_generations` はrun開始とcurrent promotionの競合を閉じるため、
`0017_append_only_guards` は `INSERT OR REPLACE` 型collisionを追記専用境界で
拒否するために追加された。既に適用済みmigrationを書き換えず、cumulative SQLを
追加する方針を維持した。

### Review-driven path deviations

- Task 2ではcumulative migrationを保つため、Task 1の
  `tests/backend/integration/test_database_foundation.py` をmigration総数へ固定しない
  assertionへ変更した。
- Task 15では動画jobとeligibilityの永続bindingが必要になり、計画外だった
  `0013_video_pipeline_bindings.sql` とjob repository/service、focused binding testを
  承認済みscopeへ追加した。
- Task 19ではTask 18から持ち越したtest fixture ownershipを解消するため、
  `test_api_writes.py`, `test_review_application.py`, `test_speaker_corrections.py`,
  `test_text_deletion.py` を承認済みscopeへ追加した。inactive negative controlだけは
  rowcountを検査するparameterized SQLを1回使用し、成功pipelineをraw SQLで構築して
  いない。
- Fix Aでは新subject IDを同じaffected-scope queryへ渡すため、計画外だった
  `src/market_voice_forecast_ledger/api/routes/corrections.py` を承認後に追加した。
- Fix Cのtracked scopeはbriefどおり `pyproject.toml`、foundation/speaker test、
  2つのwork-state helper、work-state test、README、project status/plan、このplanの
  10ファイルで、追加のpath deviationはない。
- Fix Dのtracked scopeはcorrection serviceと3 integration testの4ファイルだけで、
  commitは `a92bcaac9b592577d1a7f1efe7b1f70326853351` である。
- Fix Eの非文書scopeは `scripts/work-state/check-public-safety.ps1` と
  `tests/work-state/run-tests.ps1` の2ファイルだけである。非文書APPROVEと
  post-review gate後にREADME、project status/plan、このas-built planだけを
  文書scopeへ追加した。その他のtracked path deviationはない。
- Fix Fの非文書scopeも同じ2ファイルだけである。独立read-only APPROVEと
  post-review gate後に同じ4文書だけを追加し、exact 6-path commitとする。
  その他のtracked path deviationはない。

完全なtask別rulingとreview roundは、Git-ignoredの
`.superpowers/sdd/2026-08-14-core-data-model/progress.md` と各reportに残す。

### 実測した最終境界

- Fix C treeではbackend 898件を収集し、897 passed、1 skippedだった。skipは
  Windowsでsymlinkを作成できない場合の既存capability testだけである。
- foundation/speaker focusedは22 passed、work-state Allは135 passed・0 failed。
  compileall、working-tree公開安全166ファイル、`git diff --check`も成功した。
- dev dependency bootstrap後のwheel回帰は `PIP_NO_INDEX=1` と
  `PIP_DISABLE_PIP_VERSION_CHECK=1` を設定し、
  `pip wheel . --no-build-isolation --no-deps` で作成したwheelだけから全18 migration、
  ledger順、Task 2 audit列・trigger、raw UPDATE/DELETEの `APPEND_ONLY` を検証した。
  fresh machineでbootstrap dependencyまでoffline導入できることは検証していない。
- Fix A・BとFix Cの凍結非文書treeは独立read-only rereviewでCritical 0、
  Important 0、Minor 0となった。
- Fix Dの凍結4-path treeは独立read-only reviewでAPPROVE（Critical 0、
  Important 0、Minor 0）となった。backendは908件中907 passed、既存Windows
  symlink capability skip 1件、影響suite 320 passed、work-state 135 passed・
  0 failedだった。
- Fix Eの初回reviewでSQLite sidecar/cache parity、次のrereviewで
  `*.sqlite3-*` sidecarのImportant findingを受け、各findingを個別のREDから
  順に修正した。最終凍結2-path treeの独立read-only rereviewはAPPROVE
  （Critical 0、Important 0、Minor 0）となった。
- Fix E最終treeのrepository一括検証はbackend 908件中907 passed、既存capability
  skip 1件、work-state All 181 passed・0 failed、working-tree公開安全166ファイル、
  compileall、`git diff --check`が成功した。PowerShell 5.1/7.6のPublicSafetyは
  各46 passed・0 failed、Scriptsは80 passed・0 failedだった。
- Fix FではStaged・WorkingTreeのsecret/safe NUL fixtureを先に追加し、scanner
  変更前にPublicSafety 60 passed・8 failedのREDを確認した。対称な2か所の
  fail-closed修正後、PowerShell 5.1/7.6は各68 passed・0 failedとなった。
  凍結2-path treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、
  Minor 0）だった。
- Fix F treeのrepository一括検証はbackend 908件中907 passed、既存capability
  skip 1件、work-state All 203 passed・0 failed、Scripts 102 passed・0 failed、
  working-tree公開安全166ファイル、compileall、`git diff --check`が成功した。
- dev bootstrap後のoffline wheel証明はFix F treeでもindexを無効にして成功し、
  正確な18 migration archive名・適用順・ledger順、Task 2 audit列・trigger、
  raw UPDATE/DELETEの `APPEND_ONLY` を確認した。未bootstrap fresh machineでの
  offline dependency installationは証明していない。
- 実YouTube検索・音声・全文文字起こし、実Codex/model/tool call・adapter、実HTTP
  server/socket、React UI、電源断・disk failure、hostileな同時junction差し替え、
  remote publication、push・merge・rebase、完成製品の受け入れはこの最終検証で
  実行していない。process crashは合成temporary DBとtest-only childで検証した
  範囲だけを主張する。
- M2中核バックエンドはローカル実装・検証済みだが、ユーザー受け入れ前である。
  次subprojectとアプリ全体の完成は別の明示承認を必要とする。
- Fix Fのexact 6-path commit後にlocal branch/worktreeのclean状態とno-upstreamを
  確認して引き渡す。ignored Fix F reportはtracked差分へ含めない。

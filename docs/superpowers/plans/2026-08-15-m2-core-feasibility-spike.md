# M2中核バックエンド事前フィージビリティ・スパイク Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 承認済みスパイク設計の38シナリオを、隔離worktree内の使い捨てSQLite模型で全件実行し、性能値、成立性、M1/M2修正候補を証拠付き報告書へまとめる。

**Architecture:** 本番packageとは別の `experiments/m2-core-feasibility/` に、1つの最小SQLite schema、純粋規則とfixtureを持つPython module、標準 `unittest` suiteを置く。合成データだけでチャンネル・話者境界から現在予想・16行heatmap・checkpointまで縦断し、故障注入とSQLite triggerで原子性・追記専用性を確認する。実験コードはM2へ流用せず、結果報告書だけをレビュー候補にする。

**Tech Stack:** Windows 11、Python 3.11以上（事前確認3.14.6）、SQLite（事前確認3.50.4）、Python標準ライブラリ、PowerShell 5.1/7互換、Git worktree。

## Global Constraints

- 正本specは `docs/superpowers/specs/2026-08-15-m2-core-feasibility-spike-design.md` とする。
- 実在人物の発言、実YouTubeメタデータ、音声、全文文字起こし、話者特徴、認証情報を使わない。
- ネットワーク、YouTube、Codex CLI、FastAPI、React、外部packageを使わない。
- 本番予定の `src/market_voice_forecast_ledger/` と `tests/backend/` を作成・変更しない。
- 使い捨てコードは `experiments/m2-core-feasibility/` 配下だけに置く。
- DBと中間JSONはOS一時ディレクトリへ置き、commitしない。
- 38 scenarioを削除、skip、期待値変更して失敗を隠さない。skipが1件でもあれば完了扱いにしない。
- 性能値はSLAにせず、最小・中央値・最大、DBサイズ、query planを観測値として保存する。
- 各Taskは失敗テスト、RED確認、最小実装、focused test、全spike test、対象限定commitの順で進める。
- `git add .`、実験branchの自動削除、mainへの自動merge、GitHubへのpushを行わない。
- スパイク完了後もM2本実装へ進まない。

---

## File Map

### Main checkoutで変更するファイル

- `.gitignore`: `/.worktrees/` を1行追加し、隔離worktreeがstage候補にならないようにする。

### 隔離worktreeで作る使い捨てファイル

- `experiments/m2-core-feasibility/README.md`: 目的、非目標、合成データ境界、実行コマンド。
- `experiments/m2-core-feasibility/schema.sql`: 模型用table、index、CHECK、foreign key、追記専用trigger。
- `experiments/m2-core-feasibility/spike.py`: connection、fixture、規則、transaction、heatmap、job、計測、報告生成。
- `experiments/m2-core-feasibility/test_spike.py`: 38 scenarioと補助契約の標準 `unittest` suite、JSON test result writer。
- `docs/superpowers/reports/2026-08-15-m2-core-feasibility.md`: 実行結果から生成し、観測後のfindingを記載する検証報告書。

## Stable Spike Interfaces

後続Taskは次のsignatureだけへ依存する。模型のinterfaceであり、本番M2の正本にはしない。

```python
def connect(path: Path) -> sqlite3.Connection: ...
def apply_schema(conn: sqlite3.Connection) -> None: ...
def digest_text(value: str) -> str: ...
def seed_boundary_fixture(conn: sqlite3.Connection) -> FixtureIds: ...
def evaluate_channel(policy_kind: str, fixed_channel_id: str | None,
                     video_channel_id: str | None,
                     discovery_method: str) -> EligibilityDecision: ...
def select_analysis_segments(conn: sqlite3.Connection, subject_id: int,
                             cutoff_at_utc: str) -> tuple[int, ...]: ...
def create_analysis_run(conn: sqlite3.Connection, subject_id: int,
                        cutoff_at_utc: str,
                        segment_ids: tuple[int, ...]) -> int: ...
def next_week_range(published_at_utc: str) -> tuple[str, str]: ...
def month_first_week(year: int, month: int) -> tuple[str, str]: ...
def final_confidence(codex_confidence: str, app_confidence: str) -> str: ...
def insert_statement(conn: sqlite3.Connection, value: StatementInput) -> int: ...
def infer_assets(target_expression: str, subject_context: tuple[str, ...],
                 interviewer_context: tuple[str, ...]) -> tuple[AssetCandidate, ...]: ...
def apply_mapping_review(conn: sqlite3.Connection, mapping_id: int,
                         action: str, corrected_asset: str | None,
                         reason: str) -> int: ...
def project_forecasts(conn: sqlite3.Connection, scope_id: int) -> tuple[ForecastRow, ...]: ...
def replace_current_results(conn: sqlite3.Connection, scope_id: int,
                            rows: tuple[ForecastRow, ...],
                            fail_at: str | None = None) -> None: ...
def rebuild_heatmap(conn: sqlite3.Connection,
                    scope_ids: tuple[int, ...]) -> tuple[dict[str, object], ...]: ...
def canonical_heatmap(rows: tuple[dict[str, object], ...]) -> tuple[str, str]: ...
def reusable_unit_numbers(conn: sqlite3.Connection, job_id: int,
                          current_hashes: dict[int, tuple[str, str]]) -> tuple[int, ...]: ...
def complete_job_unit(conn: sqlite3.Connection, unit_id: int,
                      input_hash: str, output_hash: str,
                      fail_after_output: bool = False) -> None: ...
def safe_delete_under(root: Path, candidate: Path) -> str: ...
def run_metrics(base_dir: Path) -> dict[str, object]: ...
def render_report(scenarios: dict[str, object], metrics: dict[str, object],
                  git_commit: str) -> str: ...
```

Test fixtureと値objectも次の名前へ固定する。

```python
@dataclass(frozen=True)
class PeriodValue:
    source_text: str
    start_date: str | None
    end_date: str | None
    time_basis: str | None
    unknown_period: bool

@dataclass(frozen=True)
class StatementInput:
    scope_id: int
    run_id: int
    video_id: int
    statement_type: str
    forecast_basis: str | None
    condition_kind: str
    condition_text: str | None
    direction_kind: str | None
    target_expression: str | None
    period: PeriodValue
    evidence: tuple[tuple[int, str], ...]

@dataclass(frozen=True)
class AssetCandidate:
    asset: str
    mapping_kind: str
    app_confidence: str
    reason_code: str

@dataclass(frozen=True)
class ForecastRow:
    asset: str
    period_key: str
    layer: str
    state: str | None
    directions: tuple[str, ...]
    view_relation: str
    confidence: str
    evidence_count: int
    video_ids: tuple[int, ...]
    statement_ids: tuple[int, ...]

@dataclass(frozen=True)
class ForecastFixture:
    low_mapping: int
    unresolved_mapping: int
    scope_with_disagreement: int
    scope_with_change: int
    scope_with_reposts: int
    all_scope_ids: tuple[int, ...]
    older_scope: int
    newer_scope: int
    replacement_rows: tuple[ForecastRow, ...]

@dataclass(frozen=True)
class TransactionFixture:
    append_only_rows: dict[str, int]
    snapshot_id: int
    scope_id: int
    new_rows: tuple[ForecastRow, ...]

@dataclass(frozen=True)
class JobFixture:
    job_id: int
    unit_ids: tuple[int, ...]
    current_hashes: dict[int, tuple[str, str]]

def explicit_period(source_text: str) -> PeriodValue: ...
def effective_period_slot(conn: sqlite3.Connection, statement_id: int) -> str | None: ...
def approve_unknown_period(conn: sqlite3.Connection, statement_id: int, reason: str) -> int: ...
def is_heatmap_statement(statement_type: str) -> bool: ...
def classification_states() -> tuple[str | None, ...]: ...
def evidence_ordinals(conn: sqlite3.Connection, statement_id: int) -> tuple[int, ...]: ...
def insert_unknown_period_fixture(conn: sqlite3.Connection) -> int: ...
def insert_multi_evidence_fixture(conn: sqlite3.Connection) -> int: ...
def insert_non_verbatim_evidence(conn: sqlite3.Connection, statement_id: int,
                                 excerpt: str) -> None: ...
def mapping_is_eligible(conn: sqlite3.Connection, mapping_id: int) -> bool: ...
def effective_mapping_review(conn: sqlite3.Connection, mapping_id: int) -> str | None: ...
def seed_forecast_fixture(conn: sqlite3.Connection) -> ForecastFixture: ...
def current_rows_for_scope(conn: sqlite3.Connection, scope_id: int) -> tuple[ForecastRow, ...]: ...
def seed_transaction_fixture(conn: sqlite3.Connection) -> TransactionFixture: ...
def delete_snapshot_body(conn: sqlite3.Connection, snapshot_id: int, deleted_at_utc: str) -> None: ...
def snapshot_body(conn: sqlite3.Connection, snapshot_id: int) -> str | None: ...
def latest_audit_action(conn: sqlite3.Connection, scope_id: int) -> str | None: ...
def seed_job_fixture(conn: sqlite3.Connection, total_units: int,
                     succeeded_units: int) -> JobFixture: ...
def first_pending_unit(conn: sqlite3.Connection, job_id: int) -> int | None: ...
def job_unit_state(conn: sqlite3.Connection, unit_id: int) -> tuple[str, str | None]: ...
def classify_job_outcome(failed_units: int, review_required: int) -> str: ...
def discover_scenario_ids() -> tuple[str, ...]: ...
def minimal_metrics() -> dict[str, object]: ...
```

`seed_boundary_fixture`、`seed_forecast_fixture`、`seed_transaction_fixture`、`seed_job_fixture`、`synthetic_statement` と `insert_*_fixture` はtest専用fixture builderとする。各builderはtestが参照する全ID・期待値をdataclass fieldとして返し、実在名・実在IDを使わない。

## Execution Preflight: ignore設定と隔離worktree

このpreflightだけはmain checkout `C:\repos\workplace\market-voice-forecast-ledger` で行う。ユーザーが計画を承認する前には実行しない。

- [ ] **Step 1: mainが想定状態か確認する**

Run:

```powershell
git status --short --branch
git branch --show-current
git worktree list
```

Expected: branchは `main`、計画commit以外の未commit変更なし、現在は通常checkout 1件だけ。

- [ ] **Step 2: `.worktrees/` がまだ除外されていないREDを確認する**

Run:

```powershell
git check-ignore -q .worktrees/probe
if ($LASTEXITCODE -eq 0) { throw '.worktrees is already ignored; inspect before changing' }
```

Expected: command自体はexit 0で完了し、内側の `git check-ignore` は非0。

- [ ] **Step 3: `.gitignore` のlocal workspace節へ1行追加する**

末尾に次を追加する。

```gitignore

# Local Git worktrees
/.worktrees/
```

- [ ] **Step 4: ignore、既存テスト、公開安全性を確認する**

Run:

```powershell
git check-ignore -v .worktrees/probe
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree
git diff --check
```

Expected: `.gitignore` の追加行が表示され、119 passed、0 failed、公開安全検査成功、diff errorなし。

- [ ] **Step 5: ignore変更だけをcommitする**

```powershell
git add -- .gitignore
git diff --cached --name-only
git commit -m "chore: ignore local worktrees"
```

Expected: staged pathは `.gitignore` だけ。

- [ ] **Step 6: 隔離worktreeを作る**

Run:

```powershell
git worktree add .worktrees/m2-core-feasibility -b spike/m2-core-feasibility
git -C .worktrees/m2-core-feasibility status --short --branch
```

Expected: `spike/m2-core-feasibility` のcleanなworktreeが作成される。以後のTaskは `C:\repos\workplace\market-voice-forecast-ledger\.worktrees\m2-core-feasibility` で実行する。

- [ ] **Step 7: 隔離worktreeのbaselineを確認する**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Expected: 119 passed、0 failed、Python 3.11以上、SQLite versionが表示される。失敗時は実験へ進まず報告する。

---

### Task 1: 標準ライブラリだけのspike harness

**Files:**
- Create: `experiments/m2-core-feasibility/README.md`
- Create: `experiments/m2-core-feasibility/schema.sql`
- Create: `experiments/m2-core-feasibility/spike.py`
- Create: `experiments/m2-core-feasibility/test_spike.py`

**Interfaces:**
- Consumes: Python 3.11以上、SQLite、OS一時directory。
- Produces: `connect(Path)`, `apply_schema(Connection)`, `digest_text(str)` と同階層import可能なtest harness。

- [ ] **Step 1: connection契約の失敗テストを書く**

`test_spike.py` を次の骨格で作る。

```python
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from spike import apply_schema, connect, digest_text


class SpikeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "spike.sqlite3"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        apply_schema(self.conn)


class FoundationTests(SpikeTestCase):
    def test_connection_enables_foreign_keys_and_wal(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(digest_text("synthetic"),
                         "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: REDを確認する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py
```

Expected: `ModuleNotFoundError: No module named 'spike'`。

- [ ] **Step 3: 最小harnessを実装する**

`schema.sql`:

```sql
CREATE TABLE spike_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

`spike.py`:

```python
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().with_name("schema.sql")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
```

`README.md` に、合成データだけ、本番流用禁止、実行コマンド `python experiments/m2-core-feasibility/test_spike.py` を明記する。

- [ ] **Step 4: focused testと全既存テストを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
```

Expected: foundation 1 testがpass、既存119 passed、0 failed。

- [ ] **Step 5: Task 1だけをcommitする**

```powershell
git add -- experiments/m2-core-feasibility/README.md experiments/m2-core-feasibility/schema.sql experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py
git commit -m "test(spike): add isolated SQLite harness"
```

---

### Task 2: チャンネル・話者・入力境界（F-A01～F-A08）

**Files:**
- Modify: `experiments/m2-core-feasibility/schema.sql`
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`

**Interfaces:**
- Consumes: Task 1のconnectionとschema loader。
- Produces: `FixtureIds`, `EligibilityDecision`, `BoundaryViolation`, `seed_boundary_fixture`, `evaluate_channel`, `select_analysis_segments`, `create_analysis_run`。

- [ ] **Step 1: 8 scenarioの失敗テストを書く**

次のmethodを `ChannelSpeakerBoundaryTests` に追加する。method名から `F-Axx` を機械抽出できる形を維持する。

```python
def test_F_A01_all_channel_accepts_guest_channel(self):
    fixture = seed_boundary_fixture(self.conn)
    self.assertEqual(fixture.eligibility["alpha_guest"], "eligible")

def test_F_A02_fixed_channel_requires_exact_id(self):
    fixture = seed_boundary_fixture(self.conn)
    self.assertEqual(fixture.eligibility["gamma_fixed"], "eligible")
    self.assertEqual(fixture.eligibility["gamma_other"], "channel_out_of_scope")

def test_F_A03_manual_url_does_not_bypass_fixed_channel(self):
    decision = evaluate_channel("fixed_channel", FAKE_FIXED_PERSON_CHANNEL,
                                FAKE_OTHER_CHANNEL, "manual_url")
    self.assertEqual((decision.status, decision.reason_code),
                     ("channel_out_of_scope", "fixed_channel_mismatch"))

def test_F_A04_unresolved_channel_fails_closed(self):
    decision = evaluate_channel("fixed_channel", FAKE_FIXED_PERSON_CHANNEL,
                                None, "manual_url")
    self.assertEqual(decision.status, "channel_unresolved")

def test_F_A05_personal_segment_counts_equal_722(self):
    fixture = seed_boundary_fixture(self.conn)
    self.assertEqual(fixture.assignment_counts, {"subject": 653, "interviewer": 55, "hold": 14})
    self.assertEqual(sum(fixture.assignment_counts.values()), 722)

def test_F_A06_personal_input_contains_only_653_subject_segments(self):
    fixture = seed_boundary_fixture(self.conn)
    selected = select_analysis_segments(self.conn, fixture.subjects["alpha"], fixture.cutoff)
    self.assertEqual(len(selected), 653)
    self.assertTrue(set(selected).isdisjoint(fixture.interviewer_and_hold_segments))

def test_F_A07_personal_run_rejects_interviewer_or_hold(self):
    fixture = seed_boundary_fixture(self.conn)
    invalid = (fixture.interviewer_and_hold_segments[0], fixture.interviewer_and_hold_segments[-1])
    with self.assertRaisesRegex(BoundaryViolation, "personal_non_subject_segment"):
        create_analysis_run(self.conn, fixture.subjects["alpha"], fixture.cutoff, invalid)

def test_F_A08_organization_input_contains_every_eligible_segment(self):
    fixture = seed_boundary_fixture(self.conn)
    selected = select_analysis_segments(self.conn, fixture.subjects["delta_org"], fixture.cutoff)
    self.assertEqual(selected, fixture.organization_segments)
```

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py ChannelSpeakerBoundaryTests`

Expected: import errorまたは未定義interfaceで8 testsがpassしない。

- [ ] **Step 3: A群に必要なschemaを追加する**

`schema.sql` に、次のtableと制約を明示的に追加する。

```sql
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('personal', 'organization'))
);
CREATE TABLE channel_policies (
    subject_id INTEGER PRIMARY KEY REFERENCES subjects(id),
    policy_kind TEXT NOT NULL CHECK (policy_kind IN ('all_channels', 'fixed_channel')),
    fixed_channel_id TEXT,
    CHECK ((policy_kind = 'all_channels' AND fixed_channel_id IS NULL) OR
           (policy_kind = 'fixed_channel' AND fixed_channel_id IS NOT NULL))
);
CREATE TABLE videos (
    id INTEGER PRIMARY KEY,
    video_key TEXT NOT NULL UNIQUE,
    channel_id TEXT,
    published_at_utc TEXT NOT NULL
);
CREATE TABLE eligibility (
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    discovery_method TEXT NOT NULL CHECK (discovery_method IN ('auto_search', 'manual_url')),
    status TEXT NOT NULL CHECK (status IN ('eligible', 'channel_out_of_scope', 'channel_unresolved')),
    reason_code TEXT NOT NULL,
    PRIMARY KEY (subject_id, video_id)
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    ordinal INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    UNIQUE (video_id, ordinal),
    CHECK (start_ms < end_ms)
);
CREATE TABLE speaker_assignments (
    segment_id INTEGER PRIMARY KEY REFERENCES segments(id),
    assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('subject', 'interviewer', 'hold')),
    assigned_subject_id INTEGER REFERENCES subjects(id),
    assignment_origin TEXT NOT NULL CHECK (assignment_origin IN ('voice_reference', 'channel_organization')),
    raw_score REAL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    threshold_version TEXT NOT NULL,
    CHECK ((assignment_kind = 'subject' AND assigned_subject_id IS NOT NULL) OR
           (assignment_kind != 'subject' AND assigned_subject_id IS NULL))
);
CREATE TABLE scopes (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    cutoff_at_utc TEXT NOT NULL,
    UNIQUE (subject_id, cutoff_at_utc)
);
CREATE TABLE analysis_runs (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    status TEXT NOT NULL CHECK (status IN ('prepared', 'validated', 'rejected'))
);
CREATE TABLE run_segments (
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (run_id, ordinal),
    UNIQUE (run_id, segment_id)
);
CREATE INDEX ix_videos_published_at ON videos(published_at_utc);
CREATE INDEX ix_assignments_subject_kind ON speaker_assignments(assigned_subject_id, assignment_kind);
```

- [ ] **Step 4: A群の最小規則とfixtureを実装する**

`spike.py` へ次の型と定数を追加する。

```python
from dataclasses import dataclass

FAKE_FIXED_PERSON_CHANNEL = "UCFAKEPERSON000000000001"
FAKE_FIXED_ORG_CHANNEL = "UCFAKEORG00000000000002"
FAKE_OTHER_CHANNEL = "UCFAKEOTHER0000000000001"


class BoundaryViolation(ValueError):
    pass


@dataclass(frozen=True)
class EligibilityDecision:
    status: str
    reason_code: str


@dataclass(frozen=True)
class FixtureIds:
    subjects: dict[str, int]
    videos: dict[str, int]
    eligibility: dict[str, str]
    assignment_counts: dict[str, int]
    interviewer_and_hold_segments: tuple[int, ...]
    organization_segments: tuple[int, ...]
    after_cutoff_segment: int
    cutoff: str
```

`evaluate_channel` は `video_channel_id is None` を最初に `channel_unresolved`、`all_channels` を `eligible`、fixed ID完全一致を `eligible`、それ以外を `channel_out_of_scope` にする。`discovery_method` は理由記録に使っても判定を昇格させない。

`seed_boundary_fixture` は架空主体 `alpha`、`beta`、`gamma`、`delta_org` を作り、alphaの1動画へ722区間、delta_orgへ5区間を作る。alphaのordinal 1～653をsubject、654～708をinterviewer、709～722をholdにし、生scoreは2.0～5.0の値を使って0～1へ正規化しない。delta_orgの5区間は `assignment_origin = 'channel_organization'` でsubject割当する。

`select_analysis_segments` はeligibilityがeligibleかつ `published_at_utc <= cutoff_at_utc` だけを対象にし、personalは同じsubjectのsubject割当だけ、organizationはchannel_organization割当をすべて返す。

`create_analysis_run` は渡された全segment IDが `select_analysis_segments` の集合に含まれることをtransaction開始前に検査し、違反時はrunをinsertせず `BoundaryViolation` を送出する。

- [ ] **Step 5: A群と全spike testを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py ChannelSpeakerBoundaryTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: F-A01～F-A08とfoundationがすべてpass。

- [ ] **Step 6: Task 2をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/schema.sql experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py
git commit -m "test(spike): prove channel and speaker boundaries"
```

---

### Task 3: 分析分類・期間・指数割当（F-B01～F-B12）

**Files:**
- Modify: `experiments/m2-core-feasibility/schema.sql`
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`

**Interfaces:**
- Consumes: Task 2のfixture、scope、run、segment境界。
- Produces: `StatementInput`, `AssetCandidate`, `next_week_range`, `month_first_week`, `final_confidence`, `insert_statement`, `infer_assets`。

- [ ] **Step 1: B群12 scenarioの失敗テストを書く**

`AnalysisMappingTests` に次のassertionを持つ12 methodを追加する。

```python
def test_F_B01_cutoff_excludes_later_published_video(self):
    fixture = seed_boundary_fixture(self.conn)
    selected = select_analysis_segments(self.conn, fixture.subjects["alpha"], fixture.cutoff)
    self.assertNotIn(fixture.after_cutoff_segment, selected)

def test_F_B02_next_week_uses_published_at_in_JST(self):
    self.assertEqual(next_week_range("2026-08-15T15:30:00Z"), ("2026-08-17", "2026-08-23"))

def test_F_B03_explicit_year_is_not_published_at_based(self):
    value = explicit_period("2027")
    self.assertEqual(value, PeriodValue("2027", "2027-01-01", "2027-12-31", "explicit_statement", False))

def test_F_B04_month_first_week_can_cross_month(self):
    self.assertEqual(month_first_week(2026, 9), ("2026-08-31", "2026-09-06"))

def test_F_B05_unknown_period_needs_review_and_stays_unknown(self):
    statement_id = insert_unknown_period_fixture(self.conn)
    self.assertEqual(effective_period_slot(self.conn, statement_id), None)
    approve_unknown_period(self.conn, statement_id, "synthetic review")
    self.assertEqual(effective_period_slot(self.conn, statement_id), "unknown_period")

def test_F_B06_only_future_forecast_is_heatmap_candidate(self):
    self.assertEqual([is_heatmap_statement(t) for t in STATEMENT_TYPES], [True, False, False, False])

def test_F_B07_turning_flat_unknown_and_absence_are_distinct(self):
    self.assertEqual(classification_states(), ("turning_point", "flat", "unknown", None))

def test_F_B08_market_expressions_map_to_expected_assets(self):
    jp = infer_assets("synthetic_japan_equities", ("japan_equities",), ())
    us = infer_assets("synthetic_us_equities", ("us_equities",), ())
    self.assertEqual(tuple(x.asset for x in jp), ("nikkei_225", "topix"))
    self.assertEqual(tuple(x.asset for x in us), ("sp500",))

def test_F_B09_conditional_statement_requires_condition_text(self):
    with self.assertRaisesRegex(ValueError, "condition_text_required"):
        insert_statement(self.conn, synthetic_statement(condition_kind="conditional", condition_text=None))

def test_F_B10_lower_confidence_caps_automatic_result(self):
    self.assertEqual(final_confidence("high", "low"), "low")
    self.assertEqual(final_confidence("medium", "high"), "medium")

def test_F_B11_interviewer_only_market_clue_is_unresolved(self):
    candidates = infer_assets("equity_market", (), ("japan_equities",))
    self.assertEqual(candidates[0].app_confidence, "unresolved")

def test_F_B12_evidence_must_be_ordered_and_verbatim(self):
    statement_id = insert_multi_evidence_fixture(self.conn)
    self.assertEqual(evidence_ordinals(self.conn, statement_id), (1, 2))
    with self.assertRaisesRegex(ValueError, "evidence_not_verbatim"):
        insert_non_verbatim_evidence(self.conn, statement_id, "invented summary")
```

Task 2の `FixtureIds` へ `after_cutoff_segment: int` を追加する。

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py AnalysisMappingTests`

Expected: B群interface未定義で12 testsがpassしない。

- [ ] **Step 3: B群schemaを追加する**

`statements`、`statement_evidence`、`asset_mappings`、`period_reviews` を追加する。enum CHECKはspecの値を正確に使う。

```sql
CREATE TABLE statements (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    statement_type TEXT NOT NULL CHECK (statement_type IN
        ('future_forecast', 'current_analysis', 'past_result_analysis', 'general_statement')),
    forecast_basis TEXT CHECK (forecast_basis IN ('direct', 'inferred_from_subject_statements')),
    condition_kind TEXT NOT NULL CHECK (condition_kind IN ('unconditional', 'conditional')),
    condition_text TEXT,
    direction_kind TEXT CHECK (direction_kind IN
        ('strong_up', 'up', 'flat', 'down', 'strong_down', 'turning_point', 'unknown')),
    target_expression TEXT,
    period_start TEXT,
    period_end TEXT,
    time_basis TEXT CHECK (time_basis IN ('explicit_statement', 'published_at')),
    unknown_period INTEGER NOT NULL DEFAULT 0 CHECK (unknown_period IN (0, 1)),
    CHECK ((condition_kind = 'conditional' AND condition_text IS NOT NULL) OR
           (condition_kind = 'unconditional' AND condition_text IS NULL))
);
CREATE TABLE statement_evidence (
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    ordinal INTEGER NOT NULL,
    excerpt TEXT NOT NULL,
    PRIMARY KEY (statement_id, ordinal)
);
CREATE TABLE asset_mappings (
    id INTEGER PRIMARY KEY,
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    asset TEXT NOT NULL CHECK (asset IN ('nikkei_225', 'topix', 'sp500', 'xau_usd')),
    mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('direct', 'inferred')),
    codex_confidence TEXT NOT NULL CHECK (codex_confidence IN ('high', 'medium', 'low', 'unresolved')),
    app_confidence TEXT NOT NULL CHECK (app_confidence IN ('high', 'medium', 'low', 'unresolved')),
    final_confidence TEXT NOT NULL CHECK (final_confidence IN ('high', 'medium', 'low', 'unresolved')),
    reason_code TEXT NOT NULL,
    UNIQUE (statement_id, asset)
);
CREATE TABLE period_reviews (
    id INTEGER PRIMARY KEY,
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    action TEXT NOT NULL CHECK (action IN ('approve_unknown', 'reject')),
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX ix_statements_scope_type ON statements(scope_id, statement_type);
CREATE INDEX ix_mappings_statement ON asset_mappings(statement_id);
```

- [ ] **Step 4: B群規則を最小実装する**

`datetime`, `timedelta`, `timezone`, `ZoneInfo('Asia/Tokyo')` を使う。`next_week_range` はUTC文字列をJSTへ変換して、その日を含む週の次の月曜～日曜を返す。`month_first_week` は月1日のweekdayから直前または同日の月曜を計算する。

信頼度順位は次に固定する。

```python
STATEMENT_TYPES = (
    "future_forecast",
    "current_analysis",
    "past_result_analysis",
    "general_statement",
)
CONFIDENCE_RANK = {"unresolved": 0, "low": 1, "medium": 2, "high": 3}


def final_confidence(codex_confidence: str, app_confidence: str) -> str:
    return min((codex_confidence, app_confidence), key=CONFIDENCE_RANK.__getitem__)


def is_heatmap_statement(statement_type: str) -> bool:
    return statement_type == "future_forecast"


def classification_states() -> tuple[str | None, ...]:
    return ("turning_point", "flat", "unknown", None)
```

`insert_statement` はconditionalの条件文、future以外のforecast basis禁止、unknown periodと通常rangeの同時設定禁止、各evidence excerptが対応segment bodyの連続部分であることを検査してから1 transactionで保存する。

`infer_assets` は `japan_equities` をnikkei_225/topix、`us_equities` をsp500へ変換する。一般 `equity_market` はsubject contextが一市場へ一貫する場合だけmedium、競合またはinterviewer contextだけならunresolvedにする。XAU/USDは明示またはsubject contextがなければ候補を作らない。

- [ ] **Step 5: B群、A群、全spike testを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py AnalysisMappingTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: F-B01～F-B12を含む全21 tests以上がpass。

- [ ] **Step 6: Task 3をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/schema.sql experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py
git commit -m "test(spike): prove analysis and mapping rules"
```

---

### Task 4: review・現在予想・heatmap（F-C01～F-C09）

**Files:**
- Modify: `experiments/m2-core-feasibility/schema.sql`
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`

**Interfaces:**
- Consumes: Task 3のstatement、period、mapping。
- Produces: `apply_mapping_review`, `project_forecasts`, `replace_current_results`, `rebuild_heatmap`, `canonical_heatmap`。

- [ ] **Step 1: C群9 scenarioの失敗テストを書く**

`CurrentForecastHeatmapTests` に次を追加する。

```python
def test_F_C01_low_and_unresolved_need_review(self):
    fixture = seed_forecast_fixture(self.conn)
    self.assertFalse(mapping_is_eligible(self.conn, fixture.low_mapping))
    self.assertFalse(mapping_is_eligible(self.conn, fixture.unresolved_mapping))

def test_F_C02_latest_append_only_review_is_effective(self):
    fixture = seed_forecast_fixture(self.conn)
    apply_mapping_review(self.conn, fixture.low_mapping, "approve", None, "synthetic approve")
    apply_mapping_review(self.conn, fixture.low_mapping, "reject", None, "synthetic reject")
    self.assertEqual(effective_mapping_review(self.conn, fixture.low_mapping), "reject")

def test_F_C03_same_video_conflict_is_disagreement(self):
    fixture = seed_forecast_fixture(self.conn)
    rows = project_forecasts(self.conn, fixture.scope_with_disagreement)
    self.assertEqual(rows[0].view_relation, "disagreement")
    self.assertEqual(rows[0].directions, ("down", "up"))

def test_F_C04_later_opposite_video_is_changed(self):
    fixture = seed_forecast_fixture(self.conn)
    rows = project_forecasts(self.conn, fixture.scope_with_change)
    self.assertEqual((rows[0].view_relation, rows[0].directions), ("changed", ("down",)))

def test_F_C05_reposts_count_as_independent_evidence(self):
    fixture = seed_forecast_fixture(self.conn)
    rows = project_forecasts(self.conn, fixture.scope_with_reposts)
    self.assertEqual(rows[0].evidence_count, 4)
    self.assertEqual(len(rows[0].video_ids), 4)

def test_F_C06_heatmap_has_four_subjects_by_four_assets(self):
    fixture = seed_forecast_fixture(self.conn)
    rows = rebuild_heatmap(self.conn, fixture.all_scope_ids)
    self.assertEqual(len({(x["subject_key"], x["asset"]) for x in rows}), 16)

def test_F_C07_no_gold_statement_produces_blank_not_unknown(self):
    fixture = seed_forecast_fixture(self.conn)
    rows = rebuild_heatmap(self.conn, fixture.all_scope_ids)
    gold = [x for x in rows if x["asset"] == "xau_usd"]
    self.assertTrue(gold)
    self.assertTrue(all(x["state"] is None for x in gold))

def test_F_C08_heatmap_cache_rebuild_is_deterministic(self):
    fixture = seed_forecast_fixture(self.conn)
    first = canonical_heatmap(rebuild_heatmap(self.conn, fixture.all_scope_ids))
    self.conn.execute("DELETE FROM heatmap_cells")
    self.conn.commit()
    second = canonical_heatmap(rebuild_heatmap(self.conn, fixture.all_scope_ids))
    self.assertEqual(first, second)

def test_F_C09_replacing_one_cutoff_does_not_change_another(self):
    fixture = seed_forecast_fixture(self.conn)
    before = current_rows_for_scope(self.conn, fixture.older_scope)
    replace_current_results(self.conn, fixture.newer_scope, fixture.replacement_rows)
    self.assertEqual(current_rows_for_scope(self.conn, fixture.older_scope), before)
```

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py CurrentForecastHeatmapTests`

Expected: C群interface未定義で9 testsがpassしない。

- [ ] **Step 3: C群schemaを追加する**

```sql
CREATE TABLE mapping_reviews (
    id INTEGER PRIMARY KEY,
    mapping_id INTEGER NOT NULL REFERENCES asset_mappings(id),
    action TEXT NOT NULL CHECK (action IN ('approve', 'correct', 'reject')),
    corrected_asset TEXT CHECK (corrected_asset IN ('nikkei_225', 'topix', 'sp500', 'xau_usd')),
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    CHECK ((action = 'correct' AND corrected_asset IS NOT NULL) OR
           (action != 'correct' AND corrected_asset IS NULL))
);
CREATE TABLE current_forecasts (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    asset TEXT NOT NULL CHECK (asset IN ('nikkei_225', 'topix', 'sp500', 'xau_usd')),
    period_key TEXT NOT NULL,
    layer TEXT NOT NULL CHECK (layer IN ('unconditional', 'conditional')),
    state TEXT,
    directions_json TEXT NOT NULL,
    view_relation TEXT NOT NULL CHECK (view_relation IN ('current', 'changed', 'disagreement')),
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'unresolved')),
    evidence_count INTEGER NOT NULL,
    UNIQUE (scope_id, asset, period_key, layer)
);
CREATE TABLE forecast_statement_links (
    forecast_id INTEGER NOT NULL REFERENCES current_forecasts(id) ON DELETE CASCADE,
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    PRIMARY KEY (forecast_id, statement_id)
);
CREATE TABLE heatmap_cells (
    scope_id INTEGER NOT NULL REFERENCES scopes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    asset TEXT NOT NULL CHECK (asset IN ('nikkei_225', 'topix', 'sp500', 'xau_usd')),
    display_kind TEXT NOT NULL CHECK (display_kind IN ('week', 'month')),
    slot_key TEXT NOT NULL,
    layer TEXT NOT NULL CHECK (layer IN ('unconditional', 'conditional')),
    state TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (scope_id, subject_id, asset, display_kind, slot_key, layer)
);
CREATE INDEX ix_current_forecasts_scope ON current_forecasts(scope_id);
CREATE INDEX ix_heatmap_scope_display ON heatmap_cells(scope_id, display_kind);
```

- [ ] **Step 4: review、projection、heatmapを実装する**

`apply_mapping_review` はreview rowをinsertするだけで計算済みconfidenceを書き換えない。`mapping_is_eligible` はhigh/mediumを自動許可し、low/unresolvedは最新reviewがapprove/correctの場合だけ許可する。

`project_forecasts` は `(asset, period_key, layer)` でgroup化し、まず同一video内の相反方向をsorted tupleのdisagreementへまとめる。video候補間はpublished_at降順、direct優先、期間具体性優先で選ぶ。最新と直前の方向が異なる場合だけchangedにする。異なるvideo IDは本文が同じでも証拠数へ独立加算する。

`rebuild_heatmap` は対象scopeの4主体それぞれについて4資産rowを必ず作り、予想がないassetは `state = None` とする。week/月のfixture slotを作り、current_forecastsからcacheを再生成する。

`canonical_heatmap` はrowを `(subject_key, asset, display_kind, slot_key, layer)` でsortし、`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` とそのSHA-256を返す。

- [ ] **Step 5: C群と全spike testを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py CurrentForecastHeatmapTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: F-C01～F-C09を含む全30 tests以上がpass。

- [ ] **Step 6: Task 4をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/schema.sql experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py
git commit -m "test(spike): prove review and heatmap projection"
```

---

### Task 5: transaction・監査・checkpoint・安全削除（F-D01～F-D09）

**Files:**
- Modify: `experiments/m2-core-feasibility/schema.sql`
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`

**Interfaces:**
- Consumes: Task 2～4のrun、snapshot、review、current result。
- Produces: 追記専用trigger、`replace_current_results`, `reusable_unit_numbers`, `complete_job_unit`, `safe_delete_under`。

- [ ] **Step 1: D群9 scenarioの失敗テストを書く**

`TransactionRecoveryTests` に次を追加する。

```python
def test_F_D01_append_only_tables_reject_update_and_delete(self):
    fixture = seed_transaction_fixture(self.conn)
    for table, row_id in fixture.append_only_rows.items():
        with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"UPDATE {table} SET id = id WHERE id = ?", (row_id,))
        self.conn.rollback()
        with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        self.conn.rollback()

def test_F_D02_snapshot_allows_only_body_deletion_transition(self):
    fixture = seed_transaction_fixture(self.conn)
    with self.assertRaises(sqlite3.IntegrityError):
        self.conn.execute("UPDATE input_snapshots SET body_hash = 'changed' WHERE id = ?", (fixture.snapshot_id,))
    self.conn.rollback()
    delete_snapshot_body(self.conn, fixture.snapshot_id, "2026-08-15T00:00:00Z")
    self.assertIsNone(snapshot_body(self.conn, fixture.snapshot_id))

def test_F_D03_failed_replacement_keeps_old_current_set(self):
    fixture = seed_transaction_fixture(self.conn)
    before = current_rows_for_scope(self.conn, fixture.scope_id)
    with self.assertRaisesRegex(RuntimeError, "injected_after_delete"):
        replace_current_results(self.conn, fixture.scope_id, fixture.new_rows, "after_delete")
    self.assertEqual(current_rows_for_scope(self.conn, fixture.scope_id), before)

def test_F_D04_successful_replacement_commits_rows_and_audit_together(self):
    fixture = seed_transaction_fixture(self.conn)
    replace_current_results(self.conn, fixture.scope_id, fixture.new_rows)
    self.assertEqual(current_rows_for_scope(self.conn, fixture.scope_id), fixture.new_rows)
    self.assertEqual(latest_audit_action(self.conn, fixture.scope_id), "replace_current_results")

def test_F_D05_resume_starts_with_fifth_of_eight_units(self):
    fixture = seed_job_fixture(self.conn, total_units=8, succeeded_units=4)
    reusable = reusable_unit_numbers(self.conn, fixture.job_id, fixture.current_hashes)
    self.assertEqual(reusable, (1, 2, 3, 4))
    self.assertEqual(first_pending_unit(self.conn, fixture.job_id), 5)

def test_F_D06_hash_mismatch_is_not_reusable(self):
    fixture = seed_job_fixture(self.conn, total_units=8, succeeded_units=4)
    changed = dict(fixture.current_hashes)
    changed[3] = ("different-input", changed[3][1])
    self.assertEqual(reusable_unit_numbers(self.conn, fixture.job_id, changed), (1, 2, 4))

def test_F_D07_failure_between_output_and_success_rolls_back(self):
    fixture = seed_job_fixture(self.conn, total_units=1, succeeded_units=0)
    with self.assertRaisesRegex(RuntimeError, "injected_after_output"):
        complete_job_unit(self.conn, fixture.unit_ids[0], "input-1", "output-1", True)
    self.assertEqual(job_unit_state(self.conn, fixture.unit_ids[0]), ("pending", None))

def test_F_D08_review_required_is_not_job_failure(self):
    self.assertEqual(classify_job_outcome(failed_units=0, review_required=3), "succeeded_with_review")

def test_F_D09_delete_outside_audio_root_is_refused(self):
    root = Path(self.tmp.name) / "audio-root"
    root.mkdir()
    outside = Path(self.tmp.name) / "outside.tmp"
    outside.write_text("synthetic", encoding="utf-8")
    self.assertEqual(safe_delete_under(root, outside), "path_outside_audio_root")
    self.assertTrue(outside.exists())
```

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py TransactionRecoveryTests`

Expected: D群interfaceまたはtable未定義で9 testsがpassしない。

- [ ] **Step 3: D群schemaとtriggerを追加する**

`input_snapshots`、`audit_events`、`jobs`、`job_units` を追加し、analysis_runs、period_reviews、mapping_reviews、audit_eventsへUPDATE/DELETE拒否triggerを各2件追加する。

```sql
CREATE TABLE input_snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES analysis_runs(id),
    body TEXT,
    body_hash TEXT NOT NULL,
    deleted_at_utc TEXT
);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER REFERENCES scopes(id),
    action TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN
        ('queued', 'running', 'paused', 'stopped', 'failed', 'succeeded', 'succeeded_with_review')),
    manifest_hash TEXT NOT NULL,
    total_units INTEGER NOT NULL
);
CREATE TABLE job_units (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    unit_number INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    UNIQUE (job_id, unit_number)
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
```

analysis_runs、period_reviews、mapping_reviewsにも同じ形で固有trigger名を付ける。snapshot UPDATE triggerは、`OLD.body IS NOT NULL AND NEW.body IS NULL AND OLD.body_hash = NEW.body_hash AND OLD.run_id = NEW.run_id AND NEW.deleted_at_utc IS NOT NULL` の場合だけ通し、それ以外を `RAISE(ABORT, 'snapshot_immutable')` にする。snapshot DELETEは常に拒否する。

- [ ] **Step 4: transaction、checkpoint、安全削除を実装する**

`replace_current_results` は `BEGIN IMMEDIATE`、旧row canonical JSON、DELETE、任意故障hook、INSERT、audit INSERT、COMMITの順に行い、例外時はROLLBACKする。既存connectionの暗黙transactionを避けるため、この関数を呼ぶfixtureは事前にcommitする。

`complete_job_unit` は1 transaction内でoutput_hash設定とstate succeededを行い、故障hookは両UPDATEの間に置く。`reusable_unit_numbers` はDBのinput/output hashと現在hashが両方一致するsucceeded unitだけを返す。

`safe_delete_under` は `root.resolve()` と `candidate.resolve()` を比較し、`candidate.is_relative_to(root)` がfalseなら削除しない。trueの場合だけ `unlink()` し `deleted` を返す。

- [ ] **Step 5: D群と全spike testを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py TransactionRecoveryTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: F-D01～F-D09を含む38 scenarioすべてと補助testがpass、skip 0。

- [ ] **Step 6: Task 5をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/schema.sql experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py
git commit -m "test(spike): prove transactions and recovery"
```

---

### Task 6: 拡大fixture、性能計測、query plan

**Files:**
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`
- Modify: `experiments/m2-core-feasibility/README.md`

**Interfaces:**
- Consumes: Task 2～5のschemaと縦断規則。
- Produces: `seed_scale_fixture`, `measure`, `collect_query_plans`, `run_metrics`。

- [ ] **Step 1: 計測契約の失敗テストを書く**

```python
class MetricTests(SpikeTestCase):
    def test_metrics_use_exact_fixture_counts_and_five_samples(self):
        metrics = run_metrics(Path(self.tmp.name) / "metrics")
        self.assertEqual(metrics["fixture_counts"], {
            "videos": 400,
            "segments": 10000,
            "statements": 2000,
            "asset_mappings": 2500,
            "scopes": 4,
        })
        for name in (
            "insert_722_segments", "select_653_subject_segments",
            "insert_scale_fixture", "project_four_scopes",
            "build_week_month_heatmap", "rebuild_heatmap",
            "checkpoint_resume",
        ):
            self.assertEqual(len(metrics["timings_ns"][name]["samples"]), 5)
            self.assertLessEqual(metrics["timings_ns"][name]["min"], metrics["timings_ns"][name]["median"])
            self.assertLessEqual(metrics["timings_ns"][name]["median"], metrics["timings_ns"][name]["max"])
        self.assertGreater(metrics["database_bytes"], 0)
        self.assertTrue(metrics["query_plans"])
```

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py MetricTests`

Expected: `run_metrics` 未定義でfail。

- [ ] **Step 3: 決定的な拡大fixtureと計測を実装する**

`seed_scale_fixture` は固定seed `20260815` を受け取るが、乱数に依存せず連番moduloで次を作る。

- 4 subjectへ各100動画、合計400。
- 各動画へ25 segment、合計10,000。
- 各動画の先頭5 segmentをstatementへ対応させ、合計2,000。
- 2,000 statementのうち500件を日本株相当2 mapping、1,000件を米国株相当1 mapping、500件をmappingなしにし、合計2,000ではなく2,000 mappingになるため、追加500件へ2つ目の許可済み合成mappingを付けて正確に2,500行にする。
- subjectごとに1 scope、合計4。

`measure` はwarm-upを1回実行した後、fresh DBまたは同じseedで5回測り、`samples`、`min`、`median`、`max`、`peak_bytes` を返す。insert計測は毎回新しい一時DB、read/rebuild計測は同一fixtureをtransaction外で安定化してから行う。

`collect_query_plans` は入力抽出、現在予想選択、heatmap再構築、checkpoint再利用の4 queryへ `EXPLAIN QUERY PLAN` を実行し、detail文字列を保存する。

- [ ] **Step 4: 計測testと全scenarioを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py MetricTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: fixture count一致、各7計測が5 sample、38 scenarioを含む全test pass、skip 0。

- [ ] **Step 5: READMEへ計測の意味境界を追加する**

次を明記する。

- 数値はこの模型と端末の観測値で、本番SLAではない。
- 400動画・10,000区間は3年間の実量予測ではない。
- 実音声、Codex、HTTP、UI性能を表さない。

- [ ] **Step 6: Task 6をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py experiments/m2-core-feasibility/README.md
git commit -m "test(spike): add deterministic feasibility metrics"
```

---

### Task 7: 38 scenario実行証拠と検証報告書

**Files:**
- Modify: `experiments/m2-core-feasibility/spike.py`
- Modify: `experiments/m2-core-feasibility/test_spike.py`
- Modify: `experiments/m2-core-feasibility/README.md`
- Create: `docs/superpowers/reports/2026-08-15-m2-core-feasibility.md`

**Interfaces:**
- Consumes: Task 1～6のtest suiteとmetrics。
- Produces: 38件のJSON scenario結果、metrics JSON、`render_report`、検証報告書。

- [ ] **Step 1: 不完全結果を拒否する失敗テストを書く**

```python
class ReportTests(unittest.TestCase):
    def test_report_requires_exact_38_scenario_set(self):
        incomplete = {"F-A01": {"status": "pass"}}
        with self.assertRaisesRegex(ValueError, "scenario_set_incomplete"):
            render_report(incomplete, {"timings_ns": {}}, "synthetic-commit")

    def test_report_rejects_skip_but_records_failure(self):
        skipped = {key: {"status": "pass"} for key in EXPECTED_SCENARIO_IDS}
        skipped["F-D09"] = {"status": "skip", "message": "not executed"}
        with self.assertRaisesRegex(ValueError, "scenario_skip_forbidden"):
            render_report(skipped, minimal_metrics(), "synthetic-commit")
        failed = {key: {"status": "pass"} for key in EXPECTED_SCENARIO_IDS}
        failed["F-D03"] = {"status": "fail", "message": "old rows changed"}
        self.assertIn("F-D03", render_report(failed, minimal_metrics(), "synthetic-commit"))

    def test_scenario_method_names_cover_exact_design_set(self):
        self.assertEqual(discover_scenario_ids(), EXPECTED_SCENARIO_IDS)
        self.assertEqual(len(EXPECTED_SCENARIO_IDS), 38)
```

`EXPECTED_SCENARIO_IDS` は範囲表記や動的生成を使わず、次の明示的なtupleで定義する。

```python
EXPECTED_SCENARIO_IDS = (
    "F-A01", "F-A02", "F-A03", "F-A04", "F-A05", "F-A06", "F-A07", "F-A08",
    "F-B01", "F-B02", "F-B03", "F-B04", "F-B05", "F-B06",
    "F-B07", "F-B08", "F-B09", "F-B10", "F-B11", "F-B12",
    "F-C01", "F-C02", "F-C03", "F-C04", "F-C05", "F-C06", "F-C07", "F-C08", "F-C09",
    "F-D01", "F-D02", "F-D03", "F-D04", "F-D05", "F-D06", "F-D07", "F-D08", "F-D09",
)


def minimal_metrics() -> dict[str, object]:
    names = (
        "insert_722_segments", "select_653_subject_segments",
        "insert_scale_fixture", "project_four_scopes",
        "build_week_month_heatmap", "rebuild_heatmap", "checkpoint_resume",
    )
    return {
        "fixture_counts": {"videos": 400, "segments": 10000, "statements": 2000,
                           "asset_mappings": 2500, "scopes": 4},
        "timings_ns": {name: {"samples": [1, 1, 1, 1, 1], "min": 1,
                              "median": 1, "max": 1, "peak_bytes": 1}
                       for name in names},
        "database_bytes": 1,
        "query_plans": {"synthetic": ["SEARCH synthetic USING INDEX synthetic_index"]},
    }
```

- [ ] **Step 2: REDを確認する**

Run: `python experiments/m2-core-feasibility/test_spike.py ReportTests`

Expected: report interface未定義でfail。

- [ ] **Step 3: JSON test result writerを実装する**

`test_spike.py` の末尾に `unittest.TextTestResult` 派生を置き、test method名の `test_F_A01_...` を `F-A01` へ変換する。success、failure、error、skipを `status` と安全な短いmessageへ記録する。CLI引数 `--json <path>` がある場合は、suite終了後にUTF-8、sort_keysでJSONを書き、failure/errorがあればexit 1、skipがあればexit 2にする。

通常のclass名指定実行を壊さないよう、`argparse.parse_known_args()` で `--json` だけを除き、残りを `unittest` へ渡す。

- [ ] **Step 4: report生成を実装する**

`render_report` はscenario setが38件と完全一致し、skipまたは未実行がないことを検査する。fail/errorは隠さず報告書を生成し、対応する必須安全条件を不合格、findingを `design_change` または `plan_change` とする。Markdownへ次を実値で出す。

- environment、Git commit、fixture count。
- 38 scenarioのID、期待内容、status。
- 8必須安全条件と対応scenario。
- 7計測のmin/median/max/peak memory。
- DB sizeと4 query plan。
- confirmed findingをA～D群ごとに1件。
- needs_followupとして実YouTube、音声model、Codex CLI、FastAPI/UI、process crashの5件。
- query planに大tableの `SCAN` があり対応indexがない場合は `plan_change/medium` を追加。
- test failure/errorがある場合はassertion messageを短く安全に記録し、report生成を継続する。実装バグと判定できる場合は修正して再実行し、方式上成立しない場合は失敗証拠を保持して設計findingにする。

各findingへscenario ID、M1 spec節、M2 Task番号を対応表から付ける。A群はTask 3～7、B群はTask 8～12、C群はTask 13～16、D群はTask 2・6・14・17を参照する。

- [ ] **Step 5: report testと全testを実行する**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py ReportTests
python experiments/m2-core-feasibility/test_spike.py
```

Expected: report補助testがpass。38 scenarioは実装バグを除去した状態で全passを目標にするが、方式上の不成立を示すfail/errorは期待値を変更せずTask 7の報告対象にする。skipは0。

- [ ] **Step 6: 一時ディレクトリへ正式な実行証拠を生成する**

Run:

```powershell
$spikeEvidence = Join-Path ([System.IO.Path]::GetTempPath()) ("mvfl-spike-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $spikeEvidence | Out-Null
$scenarioResult = Join-Path $spikeEvidence 'scenarios.json'
python experiments/m2-core-feasibility/test_spike.py --json $scenarioResult
$scenarioExit = $LASTEXITCODE
if ($scenarioExit -eq 2) { throw 'Scenario execution skipped at least one required case' }
if ($scenarioExit -notin 0, 1) { throw "Unexpected scenario runner exit: $scenarioExit" }
python experiments/m2-core-feasibility/spike.py metrics --output (Join-Path $spikeEvidence 'metrics.json') --work-dir $spikeEvidence
python experiments/m2-core-feasibility/spike.py report --scenarios $scenarioResult --metrics (Join-Path $spikeEvidence 'metrics.json') --git-commit (git rev-parse HEAD) --output docs/superpowers/reports/2026-08-15-m2-core-feasibility.md
```

Expected: scenarios JSONは38件、skip 0。pass/fail/errorの合計が38で、failure/errorがあればreportへ不合格条件とfindingが生成される。metricsは7操作×5 sample、Markdown report生成。

- [ ] **Step 7: reportを実測結果と照合する**

Run:

```powershell
Get-Content -Raw docs/superpowers/reports/2026-08-15-m2-core-feasibility.md
Get-Content -Raw (Join-Path $spikeEvidence 'scenarios.json')
Get-Content -Raw (Join-Path $spikeEvidence 'metrics.json')
```

scenario count、status、fixture件数、timing、DB size、findingの根拠が一致することを目視確認する。矛盾があればreport generatorを修正し、Step 5～7を再実行する。

- [ ] **Step 8: 完了前の全検証を行う**

Run:

```powershell
python experiments/m2-core-feasibility/test_spike.py
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode WorkingTree
git diff --check
git status --short --branch
```

Expected: spikeは38 scenarioを全件実行しskip 0。方式上の不成立がなければ全passし、不成立があればreportとJSONにfail/error証拠が残る。既存119 passed・0 failed、公開安全性成功、diff errorなし、変更はTask 7対象だけ。

- [ ] **Step 9: Task 7をcommitする**

```powershell
git add -- experiments/m2-core-feasibility/spike.py experiments/m2-core-feasibility/test_spike.py experiments/m2-core-feasibility/README.md docs/superpowers/reports/2026-08-15-m2-core-feasibility.md
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Path . -Mode Staged
git diff --cached --check
git commit -m "test(spike): report M2 feasibility results"
```

## Spec Coverage Matrix

| Spec section | Plan evidence |
|---|---|
| 隔離と成果物の境界 | Preflight、Task 1、Task 7 |
| 境界fixture 722 = 653 + 55 + 14 | Task 2 F-A05～A07 |
| fixed/all channelと組織例外 | Task 2 F-A01～A04、A08 |
| cutoff・期間・4分類・方向状態 | Task 3 F-B01～B07 |
| 指数推定・信頼度・原文根拠 | Task 3 F-B08～B12 |
| review、見解相違・変更、重複非排除 | Task 4 F-C01～C05 |
| 16行、空欄、cache再生成、scope隔離 | Task 4 F-C06～C09 |
| 追記専用、snapshot、原子置換 | Task 5 F-D01～D04 |
| checkpoint、故障、review状態、安全削除 | Task 5 F-D05～D09 |
| 400動画・10,000区間の計測 | Task 6 |
| 38件の実行証拠、finding、修正候補 | Task 7 |

## Completion Audit

スモールテスト完了を報告する前に、次をcurrent worktreeから再確認する。

- `git status --short --branch` で実験branchと変更範囲を確認する。
- test discoveryが38 scenarioを正確に含む。
- scenario JSONが38件を持ち、pass/fail/errorの合計が38、skip 0。
- 8必須安全条件それぞれにpassまたは不合格の実行証拠があり、不合格はcritical/high findingとM2停止条件へ結び付く。
- metrics JSONが7操作×5 sampleと正確なfixture countを持つ。
- reportの実値がJSONと一致する。
- 実験コードが `experiments/m2-core-feasibility/` の外へ流出していない。
- `src/market_voice_forecast_ledger/` と `tests/backend/` が存在しないか未変更である。
- 実音声、実発言、実YouTube data、秘密情報、DB、中間JSONがstageされていない。
- 既存119テストと公開安全性検査が成功する。
- findingが `confirmed`、`plan_change`、`design_change`、`needs_followup` のいずれかで、根拠と推奨変更を持つ。
- M2本実装、main merge、pushを行っていない。

完了後は実験branchとworktreeを残し、報告書と修正候補をユーザーへ提示する。M1/M2文書修正、mainへの報告書取り込み、実験branch削除、M2開始は別承認とする。

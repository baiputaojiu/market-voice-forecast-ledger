# YouTube収集・正規metadata保存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 4人を同じ個人話者pipelineで扱い、実YouTube Data APIからseed channel、横断検索、手動URLを通じて動画を発見し、正規metadata、発見経路、出演未確認candidate、durable checkpointを安全に保存する。

**Architecture:** 既存のorganization/fixed-channel runtimeを、reset-requiredな単一cutover migrationで完全に除去する。その後は `DiscoveryProfile -> discoverer -> canonical metadata -> DiscoveryObservation -> presence_unverified` の一方向pipelineだけを使用し、既存のsealed job state machineへ `YOUTUBE_SYNC`を追加する。ネットワーク、Windows Credential Manager、Task Scheduler、SQLite repository、orchestrator、loopback APIを別責務にし、実API keyやprovider本文をDB、API、log、repositoryへ出さない。

**Tech Stack:** Python 3.11以上、標準 `sqlite3`、標準 `urllib.request`、標準 `ctypes`、FastAPI 0.115以上1.0未満、Pydantic 2.10以上3.0未満、pytest 8.3以上10.0未満、Windows 11、Windows PowerShell 5.1/PowerShell 7、SQLite WAL、Windows Credential Manager、Windows Task Scheduler。

**Spec:** `docs/superpowers/specs/2026-08-18-youtube-collection-design.md`

**Planning status:** 2026-08-20 JST。設計書はユーザー承認済みでcommit `9efba8f0e151841b3d10f460fff42dce69269961` に保存済み。Task 1～12はcommit済み・独立review済みで、Task 13開始時のclean baseは`b6cfb5ae6e75cf073a083f5aa64b4a879039f765`。Task 13初回candidateは3 failed・7 passed・1 opt-in skipのRED後、10 passed・同skip 1件となった。初回独立reviewの7 Important findingを順次RED/GREEN修正した最終fix candidateはfocused 17 passed・同skip 1件、全backend 1732件中1730 passed・許可されたskip 2件、work-state All 242 passed・0 failedを観測した。exact 19-path candidateの限定最終review、stage、commitはこの記述時点では未実施であり、実YouTube smokeは明示承認がないため実行していない。

## Global Constraints

- 実装開始後は `superpowers:using-git-worktrees` で既存workspaceと分離したlinked worktreeを作り、開始HEAD、branch、clean statusを確認する。
- 歴史migration `0001`～`0017`は変更しない。新規 `0018_youtube_discovery_cutover.sql`だけで完成schemaへ切り替える。
- `0018`未適用の既存DBは、schemaやファイルを変更する前に `COLLECTION_MODEL_RESET_REQUIRED`で停止する。DBを自動削除、移動、rename、変換しない。
- fresh DBは同じ `apply_migrations()` 呼出しで `0001`～`0018`を適用できる。途中で停止したpre-cutover DBは次回起動時にreset-requiredとなる。
- cutover後は `subject_channel_policies`、`subject_video_eligibility`、organization専用trigger、`SubjectKind`、`PolicyKind`、`AssignmentOrigin.CHANNEL_ORGANIZATION`、`services/channel_policy.py`をruntimeへ残さない。
- legacy/new dual-read、dual-write、runtime compatibility branchを作らない。legacy検出はmigration開始前だけに置く。
- 4人はすべてpersonで、違いはimmutable DiscoveryProfile versionのseed/search設定だけにする。subject IDや氏名によるcollector/orchestrator分岐を禁止する。
- Discovery search termsは入力補正用 `subject_aliases`から生成しない。木野内栄治、大川智宏、江守哲は正規氏名1語、千竈 鉄平は `千竈鉄平` と `千竃鉄平` の2語だけを使う。
- seed channelはdiscovery seedであり、候補除外や分析許可policyではない。seed uploadsでは本人名がtitle/descriptionになくても期間内動画を列挙する。
- 収集subprojectは `presence_unverified`だけを作る。`presence_confirmed` / `presence_rejected` のpublic API、音声取得、字幕、文字起こし、声紋照合、分析起動は作らない。
- YouTube video IDが同じ場合は1 `videos` rowへ統合し、別発見経路はappend-only observationとして残す。異なるvideo IDは内容が同じでも統合しない。
- provider raw JSON、完全request URL、API key、manual入力URL、page token、title、descriptionをaudit、log、job event metadata、公開API responseへ入れない。
- API keyはWindows Credential Managerの固定targetだけへ保存する。DB、設定file、environment、CLI argument、Task Scheduler XMLへ保存しない。
- 日次scheduleの正本はWindows Task Schedulerだけとし、DBへ時刻設定を複製しない。defaultは毎日06:00 JST、`StartWhenAvailable=true`、`MultipleInstancesPolicy=Queue`、interactive tokenとする。
- 同期jobは既存 `JobStateService`、sealed manifest、exact unit count、attempt/event履歴を使う。checkpointを第2のjob state machineにしない。
- full syncのunitは開始時にprofile×discovererで固定し、adaptive windowやpage/batchのために後から `job_units`を追加しない。
- 同時にactiveなYouTube sync jobは最大1。互換full requestは同じactive/queued/resumable jobへ収束し、manual requestはdurable queueへ追加する。
- full syncだけがdurable source cursorをpromoteする。manual-only jobはglobal cursorを読み書きしない。
- search windowは内部的に `[lower_bound, upper_bound)` とし、provider境界を1秒overlapして取得後にcanonical `published_at`で再filterする。10 pages後もtokenがある複数日windowは日境界で二分し、分割不能な1日windowはdurable tokenでpage 11以降を継続する。完全消費前にはcursorを進めない。
- retry対象は一時network、quota以外の429、5xxで、初回を含め最大4 attempts、待機1秒・4秒・16秒。safeな `Retry-After`は0～60秒だけ採用し、それを超える値はsleepせずdeferする。
- quota errorはgeneric retryより先に分類する。call前にreservationをdurable保存し、provider quota error時は既存 `RETRYING`と `resume_not_before_utc = observed_at + 24 hours`を使う。
- 実API smoke testは明示opt-inとし、API key未登録環境ではskipではなく「運用受入未実施」とdocumentする。通常testはfake transport、fake credential、fake schedulerのみを使う。
- test fixtureへ実在人物の発言、実description、実URL、API key、音声、全文文字起こし、DB、cache、logを含めない。
- 各Taskはtest先行、RED確認、最小実装、focused GREEN、独立review、full relevant gate、対象限定commitの順で行う。`git add .`、push、merge、rebaseを行わない。

---

## Completed contradiction and no-loop audit

- `presence_unverified`は後続の音声/本人確認へ進めるcandidate gateであり、分析許可ではない。video/audio jobはunverifiedを扱えるが、analysis inputはconfirmedかつ同一personのspeaker assignmentがなければ空になるため、確認前分析も確認不能deadlockも起きない。
- collection workerが作るproduction decisionは初期unverifiedだけで、audio job、transcription、speaker decision、analysis jobを自動生成しない。将来のpresence decisionはobservation/cursor/jobを逆向きに変更しない。
- seed/search/manualは共通metadata transactionへ収束するが、manual-only jobはfull cursorを所有しない。発見の再実行が分析を起動せず、分析/correctionがdiscoveryを起動しない。
- network/quota/credential failureは同じsealed jobをFAILED/RETRYINGからresumeし、新jobを連鎖生成しない。1 worker wake内で同じfailureを再resumeしない。
- Task Scheduler時刻はDBへ複製せず、workerがTask Scheduler statusを読む。時刻前のmanual wakeはmanual queueだけを処理し、時刻後のmissed-runだけが一意なJST日次requestを作るため、manual wakeと日次triggerの相互生成loopを作らない。
- profile version変更は次のfull manifestだけへ反映し、実行中jobを書き換えない。source keyが同じcursorだけを再利用し、新sourceだけを3年backfillするため、設定変更で過去jobを再生成し続けない。

---

## File Map

### Clean cutover and domain

- `src/market_voice_forecast_ledger/db/migrations/0018_youtube_discovery_cutover.sql`: fresh-only destructive cutover後の完成schema、旧table/trigger除去、新discovery/presence/sync schema。
- `src/market_voice_forecast_ledger/db/migrate.py`: pre-existing migration ledgerのreset-required判定。
- `src/market_voice_forecast_ledger/domain/enums.py`: person-only assignment、YouTube discovery/presence/job/stage enum。
- `src/market_voice_forecast_ledger/domain/sources.py`: person subjectとstable video identity。
- `src/market_voice_forecast_ledger/domain/discovery.py`: profile version、canonical metadata、observation、candidate、presence、sync manifest/checkpoint value object。
- `src/market_voice_forecast_ledger/domain/analysis.py`: metadata snapshot/presence decision/speaker evidenceへ固定したanalysis input。
- `src/market_voice_forecast_ledger/domain/mappings.py`: person-only mapping規則。
- `src/market_voice_forecast_ledger/bootstrap.py`: 4人と4 profileの正規初期version。

### Persistence and orchestration

- `src/market_voice_forecast_ledger/repositories/discovery.py`: profile、metadata snapshot、observation、candidate、presence、manual request、sync manifest/checkpoint、cursor、quota reservation。
- `src/market_voice_forecast_ledger/repositories/sources.py`: person subjectとvideo identityの狭いread/write。
- `src/market_voice_forecast_ledger/repositories/jobs.py`: candidate-bound video jobとYouTube sync queue query。
- `src/market_voice_forecast_ledger/services/discovery_profiles.py`: append-only profile version変更とsafe audit。
- `src/market_voice_forecast_ledger/services/youtube_sync.py`: request coalescing、claim、discoverer実行、checkpoint、cursor final promotion、resume/defer。
- `src/market_voice_forecast_ledger/services/job_state.py`: `YOUTUBE_SYNC` manifest validationとcaller-owned retry primitive。
- `src/market_voice_forecast_ledger/workers/scheduled_sync.py`: runnable queueを1件ずつdrainする `--once` worker。

### External boundaries

- `src/market_voice_forecast_ledger/credentials/windows.py`: Windows Credential Manager generic credential adapter。
- `src/market_voice_forecast_ledger/youtube/client.py`: API envelope検証、safe error分類、retry、quota reservation callback。
- `src/market_voice_forecast_ledger/youtube/metadata.py`: `videos.list` itemのcanonical normalization/hash。
- `src/market_voice_forecast_ledger/youtube/discovery.py`: uploads/search/manual discovererと厳格URL parser。
- `src/market_voice_forecast_ledger/windows/task_scheduler.py`: `schtasks.exe` explicit-argument adapterとtask XML生成。
- `src/market_voice_forecast_ledger/api/routes/youtube.py`: sync request/status/manual candidate API。
- `src/market_voice_forecast_ledger/api/models.py`: strict public request/response schema。
- `src/market_voice_forecast_ledger/api/dependencies.py`: service adapter factoryとperson-only subject read。
- `src/market_voice_forecast_ledger/cli.py`: credential、schedule、worker command。

### Verification

- `tests/backend/synthetic_collection_fixture.py`: production routeを増やさずconfirmed presenceを準備する合成test fixture。
- `tests/backend/youtube_fakes.py`: fake credential、transport、scheduler、clock、sleeper。
- `tests/backend/unit/test_youtube_*.py`: URL、metadata、client、credential、schedulerの純粋/adapter test。
- `tests/backend/integration/test_collection_model_cutover.py`: reset gate、完成schema、旧symbol不存在。
- `tests/backend/integration/test_discovery_*.py`: profile、metadata、observation、presence transaction。
- `tests/backend/integration/test_youtube_sync*.py`: manifest、checkpoint、cursor、quota、crash/retry。
- `tests/backend/integration/test_youtube_api.py`: strict loopback APIとscheduler failure durability。
- `tests/backend/e2e/test_youtube_collection_flow.py`: 4 profileを同じorchestratorで処理する合成E2E。
- `tests/backend/integration/test_youtube_real_smoke.py`: explicit environment opt-inだけのread-only実API smoke。

---

### Task 1: Atomic clean collection-model cutover

このTaskは旧runtimeと新runtimeを同居させないため、schema、domain、downstream binding、bootstrap、既存test fixtureを1つのreview/commit境界で切り替える。途中状態をcommitしない。

**Files:**
- Create: `src/market_voice_forecast_ledger/db/migrations/0018_youtube_discovery_cutover.sql`
- Create: `src/market_voice_forecast_ledger/domain/discovery.py`
- Create: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Create: `tests/backend/synthetic_collection_fixture.py`
- Create: `tests/backend/integration/test_collection_model_cutover.py`
- Create: `tests/backend/integration/test_discovery_profiles.py`
- Create: `tests/backend/integration/test_presence_analysis_boundaries.py`
- Modify: `src/market_voice_forecast_ledger/db/migrate.py`
- Modify: `src/market_voice_forecast_ledger/bootstrap.py`
- Modify: `src/market_voice_forecast_ledger/domain/enums.py`
- Modify: `src/market_voice_forecast_ledger/domain/sources.py`
- Modify: `src/market_voice_forecast_ledger/domain/analysis.py`
- Modify: `src/market_voice_forecast_ledger/domain/jobs.py`
- Modify: `src/market_voice_forecast_ledger/domain/mappings.py`
- Modify: `src/market_voice_forecast_ledger/repositories/sources.py`
- Modify: `src/market_voice_forecast_ledger/repositories/analysis.py`
- Modify: `src/market_voice_forecast_ledger/repositories/jobs.py`
- Modify: `src/market_voice_forecast_ledger/services/analysis_runs.py`
- Modify: `src/market_voice_forecast_ledger/services/asset_mapping.py`
- Modify: `src/market_voice_forecast_ledger/services/corrections.py`
- Modify: `src/market_voice_forecast_ledger/services/job_state.py`
- Modify: `src/market_voice_forecast_ledger/services/speaker_assignment.py`
- Modify: `src/market_voice_forecast_ledger/services/statements.py`
- Delete: `src/market_voice_forecast_ledger/services/channel_policy.py`
- Modify: `src/market_voice_forecast_ledger/api/dependencies.py`
- Modify: `src/market_voice_forecast_ledger/api/models.py`
- Modify: `tests/backend/e2e/synthetic_fixture.py`
- Modify: `tests/backend/integration/test_analysis_input_boundaries.py`
- Modify: `tests/backend/integration/test_analysis_output_acceptance.py`
- Modify: `tests/backend/integration/test_api_private_boundary.py`
- Modify: `tests/backend/integration/test_api_reads.py`
- Modify: `tests/backend/integration/test_append_only_insert_guards.py`
- Modify: `tests/backend/integration/test_asset_mapping_storage.py`
- Modify: `tests/backend/integration/test_cutoff_scopes.py`
- Modify: `tests/backend/integration/test_database_foundation.py`
- Modify: `tests/backend/integration/test_forecast_projection.py`
- Modify: `tests/backend/integration/test_heatmap_cache.py`
- Modify: `tests/backend/integration/test_reference_data.py`
- Modify: `tests/backend/integration/test_source_schema.py`
- Modify: `tests/backend/integration/test_speaker_assignments.py`
- Modify: `tests/backend/integration/test_speaker_corrections.py`
- Modify: `tests/backend/integration/test_stale_transitions.py`
- Modify: `tests/backend/integration/test_statement_evidence.py`
- Modify: `tests/backend/integration/test_video_pipeline_bindings.py`
- Modify: `tests/backend/unit/test_asset_mapping_rules.py`
- Delete: `tests/backend/integration/test_akatsuki_organization_assignment.py`
- Delete: `tests/backend/integration/test_channel_policy_corrections.py`
- Delete: `tests/backend/integration/test_video_eligibility.py`
- Delete: `tests/backend/unit/test_channel_policy_rules.py`

**Interfaces:**
- Consumes: `apply_migrations(conn: sqlite3.Connection) -> tuple[str, ...]`, `JobStateService`, existing analysis/current-result/retention contracts from M2.
- Produces: `COLLECTION_CUTOVER_MIGRATION = "0018_youtube_discovery_cutover"`.
- Produces: `DiscoveryProfileVersion`, `CanonicalVideoMetadata`, `DiscoveryObservation`, `SubjectVideoCandidate`, `PresenceDecision`, `SearchWindow`, `YouTubeSyncManifest`, `LiveState`, `PresenceState`, `PresenceOrigin`, `DiscoverySourceKind` in `domain.discovery`.
- Produces: `JobStateService.create_video_pipeline(manifest: JobManifest, candidate_ids: list[int] | tuple[int, ...]) -> int`.
- Produces: `RunSegment.metadata_snapshot_id`, `RunSegment.metadata_snapshot_hash`, `RunSegment.presence_decision_id`, `RunSegment.presence_decision_hash`, `RunSegment.speaker_assignment_id`; removes `policy_id` and `policy_hash`.
- Produces: `SubjectResponse(id, key, display_name, is_active)` without kind/policy fields.
- Produces test dataclass `SyntheticCollectionCandidate` and helper `create_synthetic_collection_candidate(conn, *, presence_state, assignment_kind, assigned_subject_id=None) -> SyntheticCollectionCandidate`.

- [ ] **Step 1: Add reset-gate and final-schema RED tests**

```python
def test_pre_cutover_database_is_rejected_without_any_schema_change(tmp_path):
    conn = open_database(tmp_path / "legacy.sqlite3")
    _apply_packaged_migrations_through(conn, "0017_append_only_guards")
    before = _schema_fingerprint(conn)
    with pytest.raises(DomainError, match="COLLECTION_MODEL_RESET_REQUIRED"):
        apply_migrations(conn)
    assert _schema_fingerprint(conn) == before
    assert "0018_youtube_discovery_cutover" not in _migration_names(conn)


def test_fresh_database_finishes_with_only_collection_model_schema(db):
    assert {"subject_channel_policies", "subject_video_eligibility"}.isdisjoint(
        _schema_names(db, "table")
    )
    assert {"bound_video_eligibility_identity_immutable", "bound_video_eligibility_no_delete"}.isdisjoint(
        _schema_names(db, "trigger")
    )
    assert _columns(db, "analysis_subjects") == (
        "id", "canonical_name", "is_active", "created_at"
    )
    assert _columns(db, "video_pipeline_job_bindings") == ("job_id", "candidate_id")
```

- [ ] **Step 2: Add person-only downstream RED tests**

```python
@pytest.mark.parametrize("assignment_kind", ("subject", "interviewer", "hold"))
def test_analysis_selects_only_confirmed_subject_speech(db, assignment_kind):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_confirmed",
        assignment_kind=assignment_kind,
    )
    selected = AnalysisRunService(db).preview_input(fixture.subject_id, fixture.cutoff)
    assert tuple(item.segment_id for item in selected) == (
        (fixture.segment_id,) if assignment_kind == "subject" else ()
    )
    assert all(item.presence_decision_id == fixture.presence_decision_id for item in selected)
```

Add separate cases for `presence_unverified`, `presence_rejected`, metadata snapshot hash mismatch, decision hash mismatch, and a current speaker assignment for a different subject. Each must return no input or raise a safe stored-state error without changing scope generation/current result rows.

- [ ] **Step 3: Run the new cutover tests and verify RED**

Run:

```powershell
python -m pytest tests/backend/integration/test_collection_model_cutover.py tests/backend/integration/test_discovery_profiles.py tests/backend/integration/test_presence_analysis_boundaries.py -q
```

Expected: failures for missing `0018`, missing discovery tables/types, and existing policy/organization selection.

- [ ] **Step 4: Implement the migration preflight gate before any write**

```python
COLLECTION_CUTOVER_MIGRATION = "0018_youtube_discovery_cutover"


def apply_migrations(conn: sqlite3.Connection) -> tuple[str, ...]:
    had_ledger = _schema_migrations_exists(conn)
    preexisting = _read_applied_migrations(conn) if had_ledger else frozenset()
    if preexisting and COLLECTION_CUTOVER_MIGRATION not in preexisting:
        raise DomainError(
            "COLLECTION_MODEL_RESET_REQUIRED",
            "the local database must be archived and recreated for the collection model",
        )
    _ensure_schema_migrations(conn)
    return _apply_pending_packaged_migrations(conn)
```

The check must occur before `CREATE TABLE IF NOT EXISTS`, pragma mutation, migration insert, or product-table DDL. A fresh DB is identified only by absence of the ledger before the call; an empty but pre-created ledger is invalid stored state, not a bypass.

- [ ] **Step 5: Write `0018` as a clean empty-database reconstruction**

The migration must first assert all product tables are empty, then drop every product trigger/table created by `0001`～`0017` while preserving `schema_migrations`. Recreate the M2 tables that remain valid and the following replacement tables in foreign-key order:

```sql
CREATE TABLE analysis_subjects (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE discovery_profiles (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL UNIQUE REFERENCES analysis_subjects(id),
    current_version_id INTEGER,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(current_version_id) REFERENCES discovery_profile_versions(id)
);

CREATE TABLE discovery_profile_versions (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, config_hash)
);

CREATE TABLE discovery_seed_channels (
    profile_version_id INTEGER NOT NULL REFERENCES discovery_profile_versions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    youtube_channel_id TEXT NOT NULL,
    PRIMARY KEY(profile_version_id, ordinal),
    UNIQUE(profile_version_id, youtube_channel_id)
);

CREATE TABLE discovery_search_terms (
    profile_version_id INTEGER NOT NULL REFERENCES discovery_profile_versions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    search_term TEXT NOT NULL,
    PRIMARY KEY(profile_version_id, ordinal),
    UNIQUE(profile_version_id, search_term)
);
```

The complete new-table contract is:

| Table | Required columns and identities |
|---|---|
| `videos` | `id` PK, `youtube_video_id` UNIQUE, nullable `current_metadata_snapshot_id`, `created_at` |
| `video_metadata_snapshots` | `id` PK, `video_id` FK, repeated `youtube_video_id`, `channel_id`, `channel_title`, `title`, `description`, `published_at`, `duration_seconds`, `live_state`, nullable `actual_start_time`, `schema_version`, `canonical_hash`, `fetched_at`, UNIQUE `(video_id, canonical_hash)` |
| `discovery_observations` | `id` PK, `job_id`, `profile_id`, `video_id`, `metadata_snapshot_id`, `metadata_snapshot_hash`, `source_kind`, `source_key`, `observed_at`, `observation_hash`, UNIQUE `idempotency_key` |
| `subject_video_candidates` | `id` PK, `profile_id`, `video_id`, `first_observation_id`, nullable `current_presence_decision_id`, `created_at`, UNIQUE `(profile_id, video_id)` |
| `presence_decisions` | `id` PK, `candidate_id`, `state`, `decision_origin`, `evidence_ref`, `evidence_hash`, `decision_hash`, `created_at`, UNIQUE `(candidate_id, decision_hash)` |
| `manual_discovery_requests` | `id` PK, `profile_id`, `youtube_video_id`, `requested_at`, UNIQUE `(profile_id, youtube_video_id)` |
| `youtube_sync_manifests` | `job_id` PK/FK, `sync_kind`, `upper_bound`, `backfill_floor`, `quota_contract_version`, `profile_set_hash`, nullable `manual_request_id`, nullable `resume_not_before_utc`, `manifest_hash`, `created_at` |
| `youtube_sync_manifest_profiles` | `job_id`, contiguous `ordinal`, `profile_id`, `profile_version_id`, `config_hash`, `discoverer_set_hash`, PK `(job_id, ordinal)`, UNIQUE `(job_id, profile_id)` |
| `youtube_sync_checkpoints` | `job_id`, `unit_key`, `source_kind`, `source_key`, `effective_lower_bound`, `upper_bound`, nullable `uploads_playlist_id`, nullable `next_page_token`, `page_count`, `batch_ordinal`, nullable `completed_at`, `checkpoint_hash`, PK `(job_id, unit_key)` |
| `youtube_search_windows` | `id` PK, `job_id`, `unit_key`, contiguous `ordinal`, `lower_bound`, `upper_bound`, nullable `next_page_token`, `page_count`, nullable `split_parent_id`, nullable `completed_at`, `window_hash`, UNIQUE `(job_id, unit_key, ordinal)` |
| `youtube_source_cursors` | `profile_id`, `source_kind`, `source_key`, `completed_upper_bound`, `cursor_hash`, `updated_at`, PK `(profile_id, source_kind, source_key)` |
| `youtube_sync_proposed_cursors` | `job_id`, `profile_id`, `source_kind`, `source_key`, `completed_upper_bound`, `cursor_hash`, PK `(job_id, profile_id, source_kind, source_key)` |
| `youtube_quota_reservations` | `id` PK, `job_id`, `unit_key`, `request_ordinal`, `attempt_no`, `endpoint_class`, `attempted_at`, UNIQUE `(job_id, unit_key, request_ordinal, attempt_no)` |
| `youtube_daily_sync_requests` | canonical `jst_day` PK, `job_id` UNIQUE/FK, `requested_at` |
| `video_pipeline_job_binding_sets` | unchanged sealed-set shape: `job_id` PK, `expected_binding_count`, `is_sealed` |
| `video_pipeline_job_bindings` | `job_id`, `candidate_id`, PK `(job_id, candidate_id)`; all candidates in one job must reference one `video_id` |

The exact final product-table set is the existing M2 set at commit `9efba8f0e151841b3d10f460fff42dce69269961` minus `subject_channel_policies` and `subject_video_eligibility`, with `analysis_subjects`, `videos`, `speaker_assignments`, `analysis_run_segments`, and both video binding tables replaced as specified here, plus the four profile tables and the thirteen genuinely new discovery/sync tables in the matrix. Copy every unaffected table/index/trigger DDL byte-for-byte from migrations `0001`～`0017`; do not redesign unrelated M2 schema in this Task. Repoint `analysis_run_segments` from policy fields to `metadata_snapshot_id/hash`, `presence_decision_id/hash`, and `speaker_assignment_id`, retaining the frozen assignment kind/subject/update/evidence columns. Restrict `speaker_assignments.assignment_origin` to `auto_voice|manual`.

Add append-only UPDATE/DELETE/OR-REPLACE guards to profile versions/children, snapshots, observations, decisions, quota reservations, attempts, events, reviews, forecasts, and analysis facts. Add limited pointer triggers that require `discovery_profiles.current_version_id`, `videos.current_metadata_snapshot_id`, and `subject_video_candidates.current_presence_decision_id` to point to a child of the same owner. Add final-schema tests that compare the complete sorted table/trigger tuple, not only selected names.

Add this database-level singleton guard; service-side queries alone are insufficient:

```sql
CREATE UNIQUE INDEX one_active_youtube_sync_job
ON jobs((1))
WHERE job_kind='youtube_sync'
  AND status IN ('running', 'pause_requested', 'cancel_requested');
```

Add CHECK/trigger pairs that enforce full manifests have no manual request, manual manifests have exactly one manual request, every sync checkpoint belongs to a sealed manifest unit of the matching discovery stage, daily requests point to full manifests, and quota reservations point to the named YouTube unit.

- [ ] **Step 6: Define the new exact enum and value-object surface**

```python
class DiscoverySourceKind(StrEnum):
    SEED_UPLOADS = "seed_uploads"
    CROSS_CHANNEL_SEARCH = "cross_channel_search"
    MANUAL_URL = "manual_url"


class PresenceState(StrEnum):
    UNVERIFIED = "presence_unverified"
    CONFIRMED = "presence_confirmed"
    REJECTED = "presence_rejected"


class PresenceOrigin(StrEnum):
    COLLECTION_INITIAL = "collection_initial"
    VOICE_VERIFICATION = "voice_verification"


class JobKind(StrEnum):
    VIDEO_PIPELINE = "video_pipeline"
    ANALYSIS_SCOPE = "analysis_scope"
    YOUTUBE_SYNC = "youtube_sync"
```

`PresenceOrigin.VOICE_VERIFICATION` is the reserved future verifier origin. In this subproject it is used only by `tests/backend/synthetic_collection_fixture.py` through a caller-owned repository transaction; no production service, CLI, or API accepts confirmed/rejected decisions. Remove `SubjectKind`, `PolicyKind`, `ConfigurationStatus`, `DiscoveryMethod`, `EligibilityStatus`, and `AssignmentOrigin.CHANNEL_ORGANIZATION` from production Python.

- [ ] **Step 7: Replace bootstrap data with the four approved profile versions**

```python
DEFAULT_DISCOVERY_PROFILES = (
    ("木野内栄治", ("UCXvjRTXoDa8tKwdkTaukGug",), ("木野内栄治",)),
    ("大川智宏", (), ("大川智宏",)),
    ("江守哲", ("UCVXka7buS_WptsAzSE0LcKg",), ("江守哲",)),
    ("千竈 鉄平", ("UCOfzLmXpI3qmZfV7_Cs1sYA",), ("千竈鉄平", "千竃鉄平")),
)
```

`bootstrap_reference_data()` creates or verifies the exact subject/profile/config hash and rejects mismatched stored bootstrap rows. It must not create `木野内英二` or `大川智ひろ` aliases, nor derive search terms from `subject_aliases`.

- [ ] **Step 8: Replace downstream binding in one runtime-only pass**

Implement these exact substitutions without a legacy branch:

```python
# video job eligibility
JobStateService.create_video_pipeline(manifest, candidate_ids)

# analysis input row
SelectedInputSegment(
    segment_id=row.segment_id,
    video_id=row.video_id,
    metadata_snapshot_id=row.metadata_snapshot_id,
    metadata_snapshot_hash=row.metadata_snapshot_hash,
    presence_decision_id=row.presence_decision_id,
    presence_decision_hash=row.presence_decision_hash,
    speaker_assignment_id=row.speaker_assignment_id,
    assignment_evidence_hash=row.assignment_evidence_hash,
)
```

`AnalysisRepository` must join current confirmed presence, selected immutable metadata snapshot, and current personal speaker assignment. `SpeakerAssignmentService` retains only personal assignment. Mapping and statement logic removes organization evidence/ranking. `SpeakerCorrectionService` retains manual personal corrections and stale propagation but removes policy correction and organization guard branches. Retention checks source text through confirmed presence plus personal assignment. Public subjects expose only person identity and active state.

The downstream video/audio pipeline has a different gate from analysis: a candidate with current `presence_unverified` or `presence_confirmed` may be bound so future audio/voice verification can run; `presence_rejected` may not begin/resume. One video job may bind several subject candidates for the same video. It stays runnable while at least one bound candidate is unverified/confirmed and stops only when every bound candidate is rejected. Analysis still requires confirmed presence plus same-person speaker assignment.

- [ ] **Step 9: Refactor synthetic fixtures and remove obsolete tests**

`create_synthetic_collection_candidate()` must create canonical video metadata snapshot, observation, candidate, initial unverified decision, optional test-only confirmed decision, transcript segment, and personal assignment through repository/service boundaries. Replace every old eligibility/policy fixture call with this helper. Delete the four obsolete test modules listed in **Files**, while preserving their still-valid personal speaker, rollback, stale, retention, mapping, and job assertions in the replacement modules.

- [ ] **Step 10: Run the complete cutover-focused gate**

Run:

```powershell
python -m pytest tests/backend/integration/test_collection_model_cutover.py tests/backend/integration/test_discovery_profiles.py tests/backend/integration/test_presence_analysis_boundaries.py tests/backend/integration/test_analysis_input_boundaries.py tests/backend/integration/test_video_pipeline_bindings.py tests/backend/integration/test_speaker_assignments.py tests/backend/integration/test_speaker_corrections.py tests/backend/integration/test_stale_transitions.py tests/backend/integration/test_api_reads.py tests/backend/e2e -q
```

Expected: PASS; one existing Windows symlink capability skip is allowed only if it remains the same retention test.

- [ ] **Step 11: Prove no legacy runtime survives**

Run:

```powershell
rg -n "subject_channel_policies|subject_video_eligibility|SubjectKind|PolicyKind|CHANNEL_ORGANIZATION|channel_organization|ChannelPolicyService|ChannelPolicyCorrectionService|assign_organization_video|木野内英二|大川智ひろ" src/market_voice_forecast_ledger --glob '*.py'
```

Expected: no matches. Then run `python -m pytest tests/backend -q` and `python -m compileall -q src tests/backend`; both must exit 0.

- [ ] **Step 12: Request independent cutover review and commit**

Reviewer must inspect migration reset-before-write, completed schema, pointer ownership triggers, append-only guards, person-only analysis inputs, no dual runtime, fixture-only confirmed presence, and exact changed paths. Resolve every Critical/Important finding with a new focused RED/GREEN before approval.

```powershell
$task1Paths = @(
  'src/market_voice_forecast_ledger/db/migrations/0018_youtube_discovery_cutover.sql',
  'src/market_voice_forecast_ledger/db/migrate.py',
  'src/market_voice_forecast_ledger/bootstrap.py',
  'src/market_voice_forecast_ledger/domain/enums.py',
  'src/market_voice_forecast_ledger/domain/sources.py',
  'src/market_voice_forecast_ledger/domain/discovery.py',
  'src/market_voice_forecast_ledger/domain/analysis.py',
  'src/market_voice_forecast_ledger/domain/jobs.py',
  'src/market_voice_forecast_ledger/domain/mappings.py',
  'src/market_voice_forecast_ledger/repositories/sources.py',
  'src/market_voice_forecast_ledger/repositories/discovery.py',
  'src/market_voice_forecast_ledger/repositories/analysis.py',
  'src/market_voice_forecast_ledger/repositories/jobs.py',
  'src/market_voice_forecast_ledger/services/analysis_runs.py',
  'src/market_voice_forecast_ledger/services/asset_mapping.py',
  'src/market_voice_forecast_ledger/services/corrections.py',
  'src/market_voice_forecast_ledger/services/job_state.py',
  'src/market_voice_forecast_ledger/services/speaker_assignment.py',
  'src/market_voice_forecast_ledger/services/statements.py',
  'src/market_voice_forecast_ledger/services/channel_policy.py',
  'src/market_voice_forecast_ledger/api/dependencies.py',
  'src/market_voice_forecast_ledger/api/models.py',
  'tests/backend/synthetic_collection_fixture.py',
  'tests/backend/e2e/synthetic_fixture.py',
  'tests/backend/integration/test_collection_model_cutover.py',
  'tests/backend/integration/test_discovery_profiles.py',
  'tests/backend/integration/test_presence_analysis_boundaries.py',
  'tests/backend/integration/test_analysis_input_boundaries.py',
  'tests/backend/integration/test_analysis_output_acceptance.py',
  'tests/backend/integration/test_api_private_boundary.py',
  'tests/backend/integration/test_api_reads.py',
  'tests/backend/integration/test_append_only_insert_guards.py',
  'tests/backend/integration/test_asset_mapping_storage.py',
  'tests/backend/integration/test_cutoff_scopes.py',
  'tests/backend/integration/test_database_foundation.py',
  'tests/backend/integration/test_forecast_projection.py',
  'tests/backend/integration/test_heatmap_cache.py',
  'tests/backend/integration/test_reference_data.py',
  'tests/backend/integration/test_source_schema.py',
  'tests/backend/integration/test_speaker_assignments.py',
  'tests/backend/integration/test_speaker_corrections.py',
  'tests/backend/integration/test_stale_transitions.py',
  'tests/backend/integration/test_statement_evidence.py',
  'tests/backend/integration/test_video_pipeline_bindings.py',
  'tests/backend/unit/test_asset_mapping_rules.py',
  'tests/backend/integration/test_akatsuki_organization_assignment.py',
  'tests/backend/integration/test_channel_policy_corrections.py',
  'tests/backend/integration/test_video_eligibility.py',
  'tests/backend/unit/test_channel_policy_rules.py'
)
git add -- $task1Paths
git commit -m "feat: cut over to person discovery model"
```

---

### Task 2: Versioned DiscoveryProfile configuration and canonical persistence

**Files:**
- Modify: `src/market_voice_forecast_ledger/domain/discovery.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Create: `src/market_voice_forecast_ledger/services/discovery_profiles.py`
- Create: `tests/backend/integration/test_discovery_records.py`
- Modify: `tests/backend/integration/test_discovery_profiles.py`
- Modify: `tests/backend/integration/test_append_only_insert_guards.py`

**Interfaces:**
- Consumes: `DiscoveryProfileVersion`, `CanonicalVideoMetadata`, `DiscoverySourceKind`, `PresenceState`, `PresenceOrigin`, `AuditService`.
- Produces: `ReplaceDiscoveryProfileVersion(subject_id: int, seed_channel_ids: tuple[str, ...], search_terms: tuple[str, ...], reason: str)`.
- Produces: `DiscoveryProfileService.replace_version(command: ReplaceDiscoveryProfileVersion) -> DiscoveryProfileVersion`.
- Produces: `DiscoveryRepository.list_active_profile_versions() -> tuple[DiscoveryProfileVersion, ...]`.
- Produces: `DiscoveryRepository.persist_metadata_batch(job_id: int, profile_version_id: int, source_kind: DiscoverySourceKind, source_key: str, items: tuple[CanonicalVideoMetadata, ...], observed_at: datetime) -> MetadataBatchResult`.
- Produces: `MetadataBatchResult(snapshot_ids: tuple[int, ...], observation_ids: tuple[int, ...], candidate_ids: tuple[int, ...])`.

- [ ] **Step 1: Write profile and persistence RED tests**

```python
def test_profile_version_change_is_append_only_and_search_terms_are_explicit(db):
    original = DiscoveryRepository(db).get_current_profile_version_by_subject_name("千竈 鉄平")
    changed = DiscoveryProfileService(db).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=("UCOfzLmXpI3qmZfV7_Cs1sYA",),
            search_terms=("千竈鉄平", "千竃鉄平"),
            reason="verified profile spelling set",
        )
    )
    assert changed.id != original.id
    assert DiscoveryRepository(db).get_profile_version(original.id) == original
    assert changed.search_terms == ("千竈鉄平", "千竃鉄平")
```

```python
def test_same_video_merges_identity_but_preserves_multiple_observations(db):
    first = _persist_batch(db, profile="木野内栄治", source="seed_uploads", video_id="vid0000001")
    second = _persist_batch(db, profile="木野内栄治", source="cross_channel_search", video_id="vid0000001")
    assert first.video_id == second.video_id
    assert first.candidate_id == second.candidate_id
    assert first.observation_id != second.observation_id
    assert _presence_rows(db, first.candidate_id) == [("presence_unverified", "collection_initial")]
```

Add cases for same canonical hash snapshot reuse, changed metadata snapshot append, another subject sharing one video row but getting another candidate, idempotent same-job observation, malformed item rollback for the whole 50-item batch, and raw UPDATE/DELETE/REPLACE rejection. A separate case gives two different video IDs otherwise identical normalized metadata; they must remain two video rows, two snapshots, and two candidates instead of merging by content hash.

Inject an audit write failure during `replace_version()` and assert the new version rows, child rows, current pointer, and audit event all roll back. Reject a reason containing a path/transcript sentinel through the shared audit validator without persisting the version.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_discovery_profiles.py tests/backend/integration/test_discovery_records.py -q`

Expected: failures for missing repository/service methods and atomic persistence behavior.

- [ ] **Step 3: Implement exact profile validation and hashing**

```python
def canonical_profile_hash(
    seed_channel_ids: tuple[str, ...], search_terms: tuple[str, ...]
) -> str:
    return sha256_text(canonical_json({
        "schema": "youtube-discovery-profile.v1",
        "seed_channel_ids": list(seed_channel_ids),
        "search_terms": list(search_terms),
    }))
```

Require tuple exact types, 0+ unique seed IDs matching `^UC[A-Za-z0-9_-]{22}$`, 1+ unique nonblank search terms of at most 100 code points, contiguous stored ordinals, and exact stored hash. Preserve caller order; do not sort terms before hashing because it defines the one logical query.

- [ ] **Step 4: Implement the caller-owned metadata transaction**

For each `CanonicalVideoMetadata`, validate exact UTC, safe YouTube IDs, booleans/enums, duration, schema version, and recomputed content hash before the first insert. Within one transaction: get/create video identity, reuse or append snapshot, move current snapshot pointer, insert idempotent observation, get/create candidate, and only on first candidate insert add the `collection_initial` unverified decision bound to the first observation hash. A duplicate idempotency key must reread and validate the existing row instead of silently ignoring mismatched content.

```python
idempotency_key = sha256_text(canonical_json({
    "schema": "youtube-discovery-observation-key.v1",
    "job_id": job_id,
    "profile_id": profile_id,
    "source_kind": source_kind.value,
    "source_key": source_key,
    "youtube_video_id": metadata.youtube_video_id,
}))
observation_hash = sha256_text(canonical_json({
    "schema": "youtube-discovery-observation.v1",
    "idempotency_key": idempotency_key,
    "metadata_snapshot_id": snapshot_id,
    "metadata_snapshot_hash": metadata.canonical_hash,
    "observed_at": utc_iso(observed_at),
}))
```

The initial presence decision uses `evidence_ref=f"observation:{observation_id}"`, the observation hash as `evidence_hash`, and a decision hash over candidate ID/state/origin/evidence/timestamp.

- [ ] **Step 5: Add append-only and raw-storage guards**

Extend `0018` only—never add a second migration within this subproject—to reject UPDATE/DELETE and collision-style `INSERT OR REPLACE` for profile versions/children, snapshots, observations, and presence decisions. Pointer UPDATE triggers must accept only exact same-owner child IDs. Tests must open a plain SQLite connection with `foreign_keys=0` and `recursive_triggers=0` and still receive stable abort codes.

- [ ] **Step 6: Run focused and regression GREEN**

Run:

```powershell
python -m pytest tests/backend/integration/test_discovery_profiles.py tests/backend/integration/test_discovery_records.py tests/backend/integration/test_append_only_insert_guards.py tests/backend/integration/test_presence_analysis_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks canonical hashes, exact-type validation, batch rollback, duplicate-idempotency corruption detection, same-video/multi-observation behavior, test-only decision boundary, and no raw payload persistence.

```powershell
git add src/market_voice_forecast_ledger/domain/discovery.py src/market_voice_forecast_ledger/repositories/discovery.py src/market_voice_forecast_ledger/services/discovery_profiles.py src/market_voice_forecast_ledger/db/migrations/0018_youtube_discovery_cutover.sql tests/backend/integration/test_discovery_profiles.py tests/backend/integration/test_discovery_records.py tests/backend/integration/test_append_only_insert_guards.py
git commit -m "feat: persist versioned discovery records"
```

---

### Task 3: Windows Credential Manager boundary

**Files:**
- Create: `src/market_voice_forecast_ledger/credentials/__init__.py`
- Create: `src/market_voice_forecast_ledger/credentials/windows.py`
- Create: `tests/backend/unit/test_windows_credentials.py`
- Modify: `src/market_voice_forecast_ledger/cli.py`
- Create: `tests/backend/integration/test_cli.py`

**Interfaces:**
- Consumes: `DomainError`, `default_settings()`.
- Produces: `YOUTUBE_API_KEY_TARGET = "MarketVoiceForecastLedger/YouTubeDataApiKey"`.
- Produces: platform-neutral `CredentialStore` protocol in `credentials/__init__.py` with `set_api_key(secret: str) -> None`, `has_api_key() -> bool`, `read_api_key() -> str`, `delete_api_key() -> bool`.
- Produces: `WindowsCredentialManager(CredentialStore)` using `CredWriteW`, `CredReadW`, `CredFree`, `CredDeleteW`.
- Produces CLI: `youtube credential set`, `youtube credential status`, `youtube credential delete`.

- [ ] **Step 1: Write fake-Win32 credential RED tests**

```python
def test_set_read_status_delete_never_returns_or_prints_secret(capsys):
    facade = FakeCredentialFacade()
    store = WindowsCredentialManager(facade=facade)
    store.set_api_key("synthetic-key-token-000001")
    assert store.has_api_key() is True
    assert store.read_api_key() == "synthetic-key-token-000001"
    assert store.delete_api_key() is True
    assert "synthetic-key-token" not in capsys.readouterr().out
```

Add malformed key, missing credential, wrong credential type, corrupt byte length, Win32 failure, memory-free, and CLI hidden-prompt tests. Patch `getpass.getpass`; never pass a key in argv or environment.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_windows_credentials.py tests/backend/integration/test_cli.py -q`

Expected: import/command failures because the credential adapter and CLI tree do not exist.

- [ ] **Step 3: Implement the Win32 adapter behind a narrow facade**

```python
class CredentialStore(Protocol):
    def set_api_key(self, secret: str) -> None:
        raise NotImplementedError

    def has_api_key(self) -> bool:
        raise NotImplementedError

    def read_api_key(self) -> str:
        raise NotImplementedError

    def delete_api_key(self) -> bool:
        raise NotImplementedError
```

Use a generic credential scoped to the current Windows user. Validate the API key as a nonblank ASCII token of 20～200 characters before calling Win32. Copy credential bytes into a local buffer, decode once, call `CredFree` in `finally`, and overwrite mutable local buffers where possible. Convert all native errors to `YOUTUBE_CREDENTIAL_NOT_CONFIGURED`, `YOUTUBE_CREDENTIAL_INVALID`, or `YOUTUBE_CREDENTIAL_STORAGE_FAILED`; do not include native messages or secret bytes.

- [ ] **Step 4: Add strict CLI commands**

`youtube credential set` reads twice from `getpass.getpass`, rejects mismatch, stores, and prints only `YouTube credential configured.`. `status` prints only `configured` or `not configured`. `delete` prints only `deleted` or `not configured`. Reject unknown flags and positional secret values through argparse.

- [ ] **Step 5: Run GREEN and public-safety probes**

Run:

```powershell
python -m pytest tests/backend/unit/test_windows_credentials.py tests/backend/integration/test_cli.py -q
rg -n "AIza|api[_-]?key|credential.*secret" tests/backend src/market_voice_forecast_ledger --glob '!tests/backend/unit/test_windows_credentials.py'
```

Expected: tests PASS; search finds no hard-coded secret or secret-bearing public field.

- [ ] **Step 6: Review and commit**

Reviewer checks no secret in argv/environment/DB/log/error/output, exact buffer lifecycle, non-Windows fail-closed import behavior, and fake-only automated tests.

```powershell
git add src/market_voice_forecast_ledger/credentials/__init__.py src/market_voice_forecast_ledger/credentials/windows.py src/market_voice_forecast_ledger/cli.py tests/backend/unit/test_windows_credentials.py tests/backend/integration/test_cli.py
git commit -m "feat: store YouTube credentials on Windows"
```

---

### Task 4: YouTube API client, safe retry, and quota reservation

**Files:**
- Create: `src/market_voice_forecast_ledger/youtube/__init__.py`
- Create: `src/market_voice_forecast_ledger/youtube/client.py`
- Create: `tests/backend/youtube_fakes.py`
- Create: `tests/backend/unit/test_youtube_client.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Create: `tests/backend/integration/test_youtube_quota_reservations.py`

**Interfaces:**
- Consumes: `CredentialStore.read_api_key() -> str`, `DiscoveryRepository` transaction primitives.
- Produces: `EndpointClass` with `SEARCH_LIST`, `CHANNELS_LIST`, `PLAYLIST_ITEMS_LIST`, `VIDEOS_LIST`.
- Produces: `YouTubePage(items: tuple[Mapping[str, object], ...], next_page_token: str | None)`.
- Produces: `YouTubeTransport.get_json(endpoint: str, params: Mapping[str, str], api_key: str) -> Mapping[str, object]`.
- Produces: `UrllibYouTubeTransport` using HTTPS, explicit timeout, no logging, and standard library only.
- Produces: `ChannelUploads(channel_id: str, uploads_playlist_id: str)`.
- Produces: `YouTubeClient.channels_uploads(channel_ids: tuple[str, ...]) -> tuple[ChannelUploads, ...]`.
- Produces: `YouTubeClient.playlist_items(playlist_id: str, page_token: str | None) -> YouTubePage`.
- Produces: `YouTubeClient.search_videos(query: str, published_after: str, published_before: str, page_token: str | None) -> YouTubePage`.
- Produces: `YouTubeClient.videos(video_ids: tuple[str, ...]) -> tuple[Mapping[str, object], ...]`.
- Produces: `AttemptReservation(endpoint_class: EndpointClass, attempt_no: int, attempted_at: datetime) -> None` callback; the orchestrator closure supplies job/unit/request ordinal.
- Produces: `YouTubeProviderFailure(code: str, category: Literal["quota", "transient", "defer", "invalid_page_token", "permanent"], retry_after_seconds: int | None)` with a safe fixed message.
- Produces constants `QUOTA_CONTRACT_VERSION = "youtube-data-api-2026-06-01"`, `QUOTA_REFERENCE_URL = "https://developers.google.com/youtube/v3/determine_quota_cost"`, `SEARCH_CALL_DAILY_LIMIT = 100`, `READ_UNIT_DAILY_LIMIT = 10_000`, and endpoint cost 1 for every supported method.

- [ ] **Step 1: Write request-shape and safe-error RED tests**

```python
def test_search_uses_one_logical_query_and_exact_provider_parameters():
    transport = FakeYouTubeTransport(page={"items": [], "nextPageToken": None})
    client = YouTubeClient(
        transport=transport,
        credential_store=FakeCredentialStore("synthetic-secret"),
        reserve_attempt=RecordingReservation(),
        sleeper=RecordingSleeper(),
        clock=fixed_clock,
    )
    client.search_videos(
        query="千竈鉄平|千竃鉄平",
        published_after="2023-08-17T23:59:59.000000Z",
        published_before="2026-08-18T00:00:00.000000Z",
        page_token=None,
    )
    assert transport.safe_requests == [{
        "endpoint": "search",
        "part": "id",
        "type": "video",
        "order": "date",
        "maxResults": "50",
        "q": "千竈鉄平|千竃鉄平",
        "publishedAfter": "2023-08-17T23:59:59.000000Z",
        "publishedBefore": "2026-08-18T00:00:00.000000Z",
    }]
```

Add exact parameter tests for `channels.list` with `part=contentDetails` and comma-joined `id`, `playlistItems.list` with `part=contentDetails,snippet`, `maxResults=50`, and `playlistId`, and `videos.list` with `part=snippet,contentDetails,liveStreamingDetails,status` and comma-joined `id`. Assert batches above 50 and unsafe IDs are rejected before reservation/network.

- [ ] **Step 2: Write retry and quota RED tests**

Parametrize network error, non-quota 429, 500/503, `Retry-After` 0/10/60/61, provider quota reason, invalid page token, invalid JSON, and missing credential. Assert at most four reservations, sleeps `(1, 4, 16)`, quota classification before 429, no sleep for `Retry-After=61`, and absence of API key/raw provider body/full URL from `str(error)`, audit rows, and captured output.

- [ ] **Step 3: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/backend/unit/test_youtube_client.py tests/backend/integration/test_youtube_quota_reservations.py -q
```

Expected: import failures for `youtube.client` and missing quota reservation repository methods.

- [ ] **Step 4: Implement the single-call HTTPS transport**

```python
class UrllibYouTubeTransport:
    BASE_URL = "https://www.googleapis.com/youtube/v3/"

    def get_json(self, endpoint, params, api_key):
        query = urllib.parse.urlencode({**params, "key": api_key})
        request = urllib.request.Request(
            f"{self.BASE_URL}{endpoint}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return _decode_json_object(response.read(MAX_RESPONSE_BYTES))
```

Do not retain or expose the constructed URL. Enforce a fixed response-size ceiling, UTF-8 JSON object envelope, canonical scalar page token, and endpoint-specific item list shape. Catch native exceptions without interpolating their messages.

- [ ] **Step 5: Implement retry with pre-call durable reservation**

```python
credential_value = credential_store.read_api_key()
retry_waits = (1, 4, 16)
for attempt_number in range(1, 5):
    reserve_attempt(endpoint_class, attempt_number, clock())
    try:
        return transport.get_json(endpoint, params, credential_value)
    except SafeTransportFailure as failure:
        classified = classify_provider_failure(failure)
        if classified.category != "transient" or attempt_number == 4:
            raise classified
        sleeper(max(retry_waits[attempt_number - 1], classified.retry_after_seconds or 0))
```

Attempt 1 has no preceding sleep. An integer `Retry-After` of 61～86,400 seconds becomes category `defer`; a larger or noncanonical value uses the fixed safe 24-hour defer fallback. The numeric value is omitted from public text. The reservation callback opens and commits its own short DB transaction before the network call and stores only endpoint class, job ID, unit key, request ordinal, attempt ordinal, and timestamp.

- [ ] **Step 6: Run focused GREEN and privacy checks**

Run:

```powershell
python -m pytest tests/backend/unit/test_youtube_client.py tests/backend/integration/test_youtube_quota_reservations.py -q
python -m pytest tests/backend/integration/test_audit_append_only.py tests/backend/integration/test_api_private_boundary.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks exact endpoint cost classification, pre-call commit ordering, reservation overcount rather than undercount on crash, four-attempt ceiling, provider-quota precedence, response size/JSON fail-closed behavior, and secret-safe exceptions.

```powershell
git add src/market_voice_forecast_ledger/youtube/__init__.py src/market_voice_forecast_ledger/youtube/client.py src/market_voice_forecast_ledger/repositories/discovery.py tests/backend/youtube_fakes.py tests/backend/unit/test_youtube_client.py tests/backend/integration/test_youtube_quota_reservations.py
git commit -m "feat: add safe YouTube API client"
```

---

### Task 5: Canonical metadata and three discoverers

**Files:**
- Create: `src/market_voice_forecast_ledger/youtube/metadata.py`
- Create: `src/market_voice_forecast_ledger/youtube/discovery.py`
- Create: `tests/backend/unit/test_youtube_metadata.py`
- Create: `tests/backend/unit/test_youtube_discovery.py`
- Modify: `tests/backend/youtube_fakes.py`

**Interfaces:**
- Consumes: `YouTubeClient`, `YouTubePage`, `DiscoveryProfileVersion`, `CanonicalVideoMetadata`.
- Produces: `normalize_video_item(item: Mapping[str, object], fetched_at: datetime) -> CanonicalVideoMetadata`.
- Produces: `extract_youtube_video_id(url: str) -> str`.
- Produces: `SeedUploadsDiscoverer.resolve_uploads_playlist(channel_id: str) -> str` and `page_video_ids(playlist_id: str, page_token: str | None) -> DiscoveredIdPage`.
- Produces: `CrossChannelSearchDiscoverer.page_video_ids(profile: DiscoveryProfileVersion, window: SearchWindow, page_token: str | None) -> DiscoveredIdPage`.
- Produces: `ManualUrlDiscoverer.fetch(video_id: str) -> tuple[CanonicalVideoMetadata, ...]`.
- Produces: `DiscoveredIdPage(video_ids: tuple[str, ...], next_page_token: str | None)`.

- [ ] **Step 1: Write metadata normalization RED tests**

```python
def test_live_actual_start_time_is_the_analysis_published_at():
    item = synthetic_video_item(
        snippet_published_at="2026-08-10T01:00:00Z",
        actual_start_time="2026-08-10T02:03:04Z",
        duration="PT1H2M3S",
        live_broadcast_content="live",
    )
    value = normalize_video_item(item, fetched_at=FIXED_NOW)
    assert value.published_at == datetime(2026, 8, 10, 2, 3, 4, tzinfo=timezone.utc)
    assert value.duration_seconds == 3723
    assert value.canonical_hash == canonical_metadata_hash(value)
```

Add VOD fallback, upcoming live without actual start, fractional ISO duration, missing/private/deleted item, noncanonical timestamp, invalid boolean/scalar/list, oversized title/description, and hash independence from `fetched_at` tests. Raw provider-only fields must not appear in the dataclass.

- [ ] **Step 2: Write URL/discoverer RED tests**

Accept only these exact HTTPS forms with no credential/userinfo/port/fragment and one canonical 11-character ID:

```text
https://www.youtube.com/watch?v=abcdefghijk
https://youtube.com/shorts/abcdefghijk
https://youtube.com/live/abcdefghijk
https://youtu.be/abcdefghijk
```

Reject playlists, channel URLs, embed URLs, extra conflicting `v`, non-HTTPS, Unicode host confusables, nested URLs, whitespace/control characters, and arbitrary text containing a URL. Verify seed discovery returns every playlist video independent of title; search joins ordered terms with `|`; manual discovery always canonicalizes through `videos.list`.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_youtube_metadata.py tests/backend/unit/test_youtube_discovery.py -q`

Expected: import failures for missing metadata/discovery modules.

- [ ] **Step 4: Implement strict metadata normalization**

```python
CanonicalVideoMetadata(
    youtube_video_id=video_id,
    channel_id=channel_id,
    channel_title=channel_title,
    title=title,
    description=description,
    published_at=actual_start_time or snippet_published_at,
    duration_seconds=parse_youtube_duration(duration),
    live_state=LiveState(live_broadcast_content),
    actual_start_time=actual_start_time,
    schema_version="youtube-video-metadata.v1",
    canonical_hash=sha256_text(canonical_json(hash_payload)),
    fetched_at=fetched_at,
)
```

The hash payload includes normalized provider fields and schema version, but excludes `fetched_at`. Treat an omitted item as unavailable; never synthesize metadata from search/playlist snippets.

- [ ] **Step 5: Implement discoverers without subject-specific branches**

`SeedUploadsDiscoverer` resolves exactly one uploads playlist through `channels.list`, then enumerates `playlistItems.list`. `CrossChannelSearchDiscoverer` builds one query from `profile.search_terms` and passes fixed window bounds/order/type/maxResults. `ManualUrlDiscoverer` receives only a parsed video ID. All three return IDs or canonical metadata; none writes DB, starts jobs, checks person names, or performs speaker logic.

- [ ] **Step 6: Run GREEN and mutation tests**

Run:

```powershell
python -m pytest tests/backend/unit/test_youtube_metadata.py tests/backend/unit/test_youtube_discovery.py tests/backend/unit/test_youtube_client.py -q
```

Then temporarily mutate the test fake so a seed title lacks the subject name; the seed test must still pass. Restore the fake and verify `git diff --check`.

- [ ] **Step 7: Review and commit**

Reviewer checks all URL attack forms, exact OR query, no alias derivation, seed-name nonfiltering, live timestamp choice, duration/parser limits, raw payload exclusion, and no DB/network responsibility leakage between files.

```powershell
git add src/market_voice_forecast_ledger/youtube/metadata.py src/market_voice_forecast_ledger/youtube/discovery.py tests/backend/unit/test_youtube_metadata.py tests/backend/unit/test_youtube_discovery.py tests/backend/youtube_fakes.py
git commit -m "feat: normalize YouTube discovery inputs"
```

---

### Task 6: Sealed YouTube sync manifests and durable queue

**Files:**
- Modify: `src/market_voice_forecast_ledger/domain/jobs.py`
- Modify: `src/market_voice_forecast_ledger/domain/discovery.py`
- Modify: `src/market_voice_forecast_ledger/repositories/jobs.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Modify: `src/market_voice_forecast_ledger/services/job_state.py`
- Create: `src/market_voice_forecast_ledger/services/youtube_sync.py`
- Create: `tests/backend/integration/test_youtube_sync_manifest.py`
- Create: `tests/backend/integration/test_youtube_sync_queue.py`

**Interfaces:**
- Consumes: active immutable `DiscoveryProfileVersion` rows and existing `JobManifest`/`JobStateService`.
- Produces: `JobStage.YOUTUBE_SEED_DISCOVERY`, `YOUTUBE_SEARCH_DISCOVERY`, `YOUTUBE_MANUAL_DISCOVERY`.
- Produces: `YouTubeSyncService.request_full_sync(requested_at: datetime) -> SyncRequestResult`.
- Produces: `YouTubeSyncService.claim_next_runnable(now: datetime) -> ClaimedSyncJob | None`.
- Produces: `SyncRequestResult(job_id: int, status: JobStatus, reused: bool)`.
- Produces: `ClaimedSyncJob(job_id: int, kind: Literal["full", "manual"], manifest: YouTubeSyncManifest)`.
- Produces: `JobStateService.retry_failed_in_transaction(job_id: int, artifact_hashes: Mapping[str, str]) -> ResumePlan`.

- [ ] **Step 1: Write exact-manifest RED tests**

```python
def test_full_manifest_has_fixed_profile_discoverer_units(db):
    result = YouTubeSyncService(db, clock=fixed_clock).request_full_sync(FIXED_NOW)
    manifest = JobStateService(db).stored_manifest(result.job_id)
    assert manifest.kind is JobKind.YOUTUBE_SYNC
    assert tuple((unit.stage.value, unit.unit_key) for unit in manifest.units) == (
        ("youtube_seed_discovery", "youtube:profile:1:seed:UCXvjRTXoDa8tKwdkTaukGug"),
        ("youtube_search_discovery", "youtube:profile:1:search"),
        ("youtube_search_discovery", "youtube:profile:2:search"),
        ("youtube_seed_discovery", "youtube:profile:3:seed:UCVXka7buS_WptsAzSE0LcKg"),
        ("youtube_search_discovery", "youtube:profile:3:search"),
        ("youtube_seed_discovery", "youtube:profile:4:seed:UCOfzLmXpI3qmZfV7_Cs1sYA"),
        ("youtube_search_discovery", "youtube:profile:4:search"),
    )
```

Resolve actual profile IDs from bootstrap instead of assuming insertion order in production code; the expected test tuple is built from those IDs. Assert config hashes/version IDs/run bounds/quota contract in the sealed sync manifest and no unit addition after job creation.

- [ ] **Step 2: Write coalescing and claim RED tests**

Cover concurrent transactions requesting the same full job, compatible active/queued/retrying/failed job reuse, incompatible profile-version job queued behind active, manual job not coalesced with full, earliest-runnable FIFO, deferred retry exclusion, and raw SQL attempts to create a second active YouTube job. Use two SQLite connections and barriers rather than process-local locks.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_youtube_sync_manifest.py tests/backend/integration/test_youtube_sync_queue.py -q`

Expected: failures for unsupported job kind/stages and missing queue service.

- [ ] **Step 4: Extend manifest validation**

```python
YOUTUBE_SYNC_STAGES = frozenset({
    JobStage.YOUTUBE_SEED_DISCOVERY,
    JobStage.YOUTUBE_SEARCH_DISCOVERY,
    JobStage.YOUTUBE_MANUAL_DISCOVERY,
})
```

`JobManifest.build()` accepts only these stages for `YOUTUBE_SYNC`, forbids analysis reserved keys, requires every dependency to precede its unit, and keeps contiguous ordinals. Full manifests contain all current active profile versions; manual manifests contain one manual request and one manual unit. Store the sync-specific manifest rows in the same transaction as `jobs`/`job_units` and validate their exact recomputed hash on every read.

- [ ] **Step 5: Implement SQLite coalescing and claim**

Use `BEGIN IMMEDIATE`. `request_full_sync()` first finds the newest compatible job in queued/running/retrying/failed states. For a failed compatible job it reconstructs successful artifact hashes from exact checkpoint output hashes and calls `retry_failed_in_transaction`; otherwise it creates one sealed job. `claim_next_runnable()` excludes `resume_not_before_utc > now`, validates manifest/profile version hashes, and begins only the next pending unit of the oldest runnable job. A partial unique index permits at most one `RUNNING`, `PAUSE_REQUESTED`, or `CANCEL_REQUESTED` YouTube job.

- [ ] **Step 6: Add queue corruption and rollback tests**

Tamper total units, unit ordinal, profile version, config hash, sync kind, run bounds, manual request shape, checkpoint output hash, and deferred timestamp through a raw connection after dropping the relevant guard. Every public/service read must fail closed before claim or cursor mutation. Inject failure after job insert but before manifest seal and assert complete rollback.

- [ ] **Step 7: Run GREEN and existing job regression**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_sync_manifest.py tests/backend/integration/test_youtube_sync_queue.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_video_pipeline_bindings.py tests/backend/integration/test_api_reads.py -q
```

Expected: PASS.

- [ ] **Step 8: Review and commit**

Reviewer checks sealed manifest identity, profile-version immutability, database—not process—single-active enforcement, exact retry primitive, artifact verification, FIFO/defer rules, and rollback.

```powershell
git add src/market_voice_forecast_ledger/domain/jobs.py src/market_voice_forecast_ledger/domain/discovery.py src/market_voice_forecast_ledger/repositories/jobs.py src/market_voice_forecast_ledger/repositories/discovery.py src/market_voice_forecast_ledger/services/job_state.py src/market_voice_forecast_ledger/services/youtube_sync.py tests/backend/integration/test_youtube_sync_manifest.py tests/backend/integration/test_youtube_sync_queue.py
git commit -m "feat: seal durable YouTube sync jobs"
```

---

### Task 7: Seed-channel unit execution and canonical batch commits

**Files:**
- Modify: `src/market_voice_forecast_ledger/services/youtube_sync.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Create: `tests/backend/integration/test_youtube_seed_sync.py`
- Create: `tests/backend/integration/test_youtube_sync_atomicity.py`
- Modify: `tests/backend/youtube_fakes.py`

**Interfaces:**
- Consumes: `ClaimedSyncJob`, `SeedUploadsDiscoverer`, `YouTubeClient.videos`, `DiscoveryRepository.persist_metadata_batch`, `JobStateService`.
- Produces: `YouTubeSyncService.execute_seed_unit(job_id: int, unit_key: str) -> UnitExecutionResult`.
- Produces: `UnitExecutionResult(discovered_count: int, persisted_count: int, unavailable_count: int, output_hash: str)`.

- [ ] **Step 1: Write seed execution RED tests**

Build a profile whose uploads playlist has 73 synthetic IDs over two pages, including a title without the person name, one unavailable ID, and one ID already observed through search. Assert `videos.list` calls are bounded to at most 10 IDs, all 72 available IDs are persisted, the existing video gets a second observation rather than a second video/candidate, and the unavailable ID creates no video/snapshot/observation/candidate.

- [ ] **Step 2: Write page/batch atomicity RED tests**

Inject failure at item 37 of a 50-item metadata batch, after persistence before checkpoint update, after checkpoint update before unit completion, and after unit completion before job finalization. Assert whole batch rollback, idempotent replay, exact observation count, no current pointer drift, and no durable source cursor promotion.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_youtube_seed_sync.py tests/backend/integration/test_youtube_sync_atomicity.py -q`

Expected: missing execution method/checkpoint failures.

- [ ] **Step 4: Implement the seed checkpoint loop**

For the bound profile version/channel source key: resolve or validate the stored uploads playlist ID, read the next committed page token, call playlist discovery, deduplicate IDs within the job, call canonical `videos.list` in groups of at most 10 to preserve the 1 MiB response boundary, and commit each metadata batch together with the domain checkpoint. Persist only canonical metadata satisfying the checkpoint's sealed `[effective_lower_bound, upper_bound)`; continue paging until a page is wholly older than the lower bound or no token remains. Never filter by title, description, or channel title. Store page token only in the private checkpoint table.

- [ ] **Step 5: Complete the fixed unit with a deterministic output hash**

```python
output_hash = sha256_text(canonical_json({
    "schema": "youtube-seed-unit-output.v1",
    "profile_version_id": profile_version.id,
    "source_key": channel_id,
    "completed_upper_bound": utc_iso(manifest.upper_bound),
    "persisted_observation_ids": list(sorted(observation_ids)),
}))
```

Persist the exact output record, insert the unit's proposed seed cursor `(profile_id, seed_uploads, channel_id, completed_upper_bound)`, and call `complete_unit_in_transaction()` in one transaction. On resume, recompute the artifact hash from domain rows before passing it to `JobStateService.resume()`.

- [ ] **Step 6: Run GREEN and crash replay tests**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_seed_sync.py tests/backend/integration/test_youtube_sync_atomicity.py tests/backend/integration/test_youtube_sync_manifest.py tests/backend/integration/test_discovery_records.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks no name hard filter, exact batches, unavailable handling, page-token privacy, transaction ordering, observation idempotency, and artifact recomputation.

```powershell
git add src/market_voice_forecast_ledger/services/youtube_sync.py src/market_voice_forecast_ledger/repositories/discovery.py tests/backend/integration/test_youtube_seed_sync.py tests/backend/integration/test_youtube_sync_atomicity.py tests/backend/youtube_fakes.py
git commit -m "feat: execute seed channel discovery"
```

---

### Task 8: Adaptive cross-channel search and all-or-nothing cursor promotion

**Files:**
- Modify: `src/market_voice_forecast_ledger/domain/discovery.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Modify: `src/market_voice_forecast_ledger/services/youtube_sync.py`
- Create: `tests/backend/integration/test_youtube_search_sync.py`
- Create: `tests/backend/integration/test_youtube_cursor_promotion.py`
- Modify: `tests/backend/integration/test_youtube_sync_atomicity.py`
- Modify: `tests/backend/youtube_fakes.py`

**Interfaces:**
- Consumes: `CrossChannelSearchDiscoverer`, fixed search unit, sync manifest bounds, metadata batch persistence.
- Produces: `source_key_for_search_terms(terms: tuple[str, ...]) -> str`.
- Produces: `initial_backfill_floor(upper_bound: datetime) -> datetime` using three calendar years with leap-day clamping.
- Produces: `YouTubeSyncService.execute_search_unit(job_id: int, unit_key: str) -> UnitExecutionResult`.
- Produces: `YouTubeSyncService.finalize_full_job(job_id: int) -> None` that atomically promotes all proposed cursors and succeeds the job.

- [ ] **Step 1: Write initial/incremental cursor RED tests**

```python
def test_new_source_uses_exact_three_calendar_year_backfill(db):
    job = _create_full_job(db, upper_bound=datetime(2028, 2, 29, tzinfo=timezone.utc))
    window = DiscoveryRepository(db).next_search_window(job.id, _okawa_search_unit(job))
    assert window.lower_bound == datetime(2025, 2, 28, tzinfo=timezone.utc)
    assert window.upper_bound == datetime(2028, 2, 29, tzinfo=timezone.utc)


def test_unchanged_source_key_reuses_last_successful_upper_bound(db):
    _seed_cursor(db, profile="江守哲", completed_upper_bound="2026-08-17T21:00:00.000000Z")
    job = _create_full_job(db, upper_bound="2026-08-18T21:00:00.000000Z")
    assert _first_window(db, job).lower_bound == parse_utc("2026-08-17T21:00:00.000000Z")
```

Add profile-version change with unchanged ordered term set (cursor reused), changed term set (new key/backfill), new seed source, and manual job never reading/writing cursors.

- [ ] **Step 2: Write adaptive-window RED tests**

Create fake pages with a next token after page 10. Assert the original multi-day window is replaced by two nonoverlapping day-boundary children whose provider requests overlap the lower boundary by exactly one second and whose locally accepted metadata still satisfies `[lower, upper)`. Add invalid-page-token restart, replay deduplication, out-of-window search result rejection, and durable page 11+ continuation for an unsplittable one-day leaf without cursor promotion before exhaustion.

- [ ] **Step 3: Write final-promotion RED tests**

```python
def test_no_source_cursor_moves_until_every_fixed_unit_succeeds(db):
    job = _job_with_completed_seed_and_search_units_except_one(db)
    before = _durable_cursor_map(db)
    with pytest.raises(DomainError, match="ALL_UNITS_MUST_SUCCEED"):
        YouTubeSyncService(db).finalize_full_job(job.id)
    assert _durable_cursor_map(db) == before


def test_all_proposed_cursors_and_job_success_commit_together(db, failpoint):
    job = _fully_completed_full_job(db)
    failpoint.raise_after_cursor_update()
    with pytest.raises(SyntheticFailure):
        YouTubeSyncService(db, failpoint=failpoint).finalize_full_job(job.id)
    assert _durable_cursor_map(db) == _pre_job_cursor_map(db)
    assert JobStateService(db).status(job.id) is JobStatus.RUNNING
```

- [ ] **Step 4: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_search_sync.py tests/backend/integration/test_youtube_cursor_promotion.py -q
```

Expected: missing window/cursor execution methods.

- [ ] **Step 5: Implement exact window lifecycle**

Store one root window per fixed search unit. A page commit updates only that window's private next token/page count and domain batch position. At page 10 with a remaining token, mark a splittable parent with immutable split proof and append two children ordered newer-first; never create job units. An unsplittable one-day leaf retains the token and continues page 11+ across defer/resume. Invalid token clears only the same fixed window's token/page count and relies on observation idempotency. Proposed/durable cursors remain unchanged until complete exhaustion.

- [ ] **Step 6: Implement proposed and durable cursor hashes**

```python
source_key = sha256_text(canonical_json({
    "schema": "youtube-search-source.v1",
    "ordered_terms": list(profile.search_terms),
}))

cursor_hash = sha256_text(canonical_json({
    "schema": "youtube-source-cursor.v1",
    "profile_id": profile.id,
    "source_kind": source_kind.value,
    "source_key": source_key,
    "completed_upper_bound": utc_iso(upper_bound),
}))
```

Each successful seed/search unit records exactly one proposed cursor. `finalize_full_job()` verifies the sealed manifest, all unit outputs, every proposal owner/hash/bound, and exact proposal count; in one transaction it upserts the complete durable map and calls `succeed_job_in_transaction()`.

- [ ] **Step 7: Run focused GREEN and crash regression**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_search_sync.py tests/backend/integration/test_youtube_cursor_promotion.py tests/backend/integration/test_youtube_seed_sync.py tests/backend/integration/test_youtube_sync_atomicity.py -q
```

Expected: PASS.

- [ ] **Step 8: Review and commit**

Reviewer checks calendar subtraction, ordered term-set identity, exact time filtering, boundary overlap, 10-page split, 1-day fail closed, invalid-token replay, no dynamic job units, and all-cursor/job atomicity.

```powershell
git add src/market_voice_forecast_ledger/domain/discovery.py src/market_voice_forecast_ledger/repositories/discovery.py src/market_voice_forecast_ledger/services/youtube_sync.py tests/backend/integration/test_youtube_search_sync.py tests/backend/integration/test_youtube_cursor_promotion.py tests/backend/integration/test_youtube_sync_atomicity.py tests/backend/youtube_fakes.py
git commit -m "feat: checkpoint adaptive YouTube search"
```

---

### Task 9: Idempotent manual URL candidates

**Files:**
- Modify: `src/market_voice_forecast_ledger/domain/discovery.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Modify: `src/market_voice_forecast_ledger/services/youtube_sync.py`
- Create: `tests/backend/integration/test_youtube_manual_sync.py`
- Modify: `tests/backend/unit/test_youtube_discovery.py`

**Interfaces:**
- Consumes: `extract_youtube_video_id`, active profile, manual-only `YOUTUBE_SYNC` manifest, canonical metadata persistence.
- Produces: `YouTubeSyncService.request_manual_candidate(subject_id: int, url: str, requested_at: datetime) -> ManualRequestResult`.
- Produces: `ManualRequestResult(request_id: int, job_id: int, status: JobStatus, reused: bool)`.
- Produces: `YouTubeSyncService.execute_manual_unit(job_id: int, unit_key: str) -> UnitExecutionResult`.

- [ ] **Step 1: Write durable idempotency RED tests**

```python
def test_same_subject_and_video_reuses_manual_request_and_job(db):
    service = YouTubeSyncService(db, clock=fixed_clock)
    first = service.request_manual_candidate(1, "https://youtu.be/abcdefghijk", FIXED_NOW)
    second = service.request_manual_candidate(1, "https://youtube.com/watch?v=abcdefghijk", FIXED_NOW)
    assert second == ManualRequestResult(first.request_id, first.job_id, first.status, True)
    assert _stored_manual_request(db, first.request_id).youtube_video_id == "abcdefghijk"
    assert "youtu" not in _stored_manual_request_json(db, first.request_id)
```

Add two-connection race, different subject same video, inactive/missing profile, malformed URL rollback, request insert failure, and existing succeeded/failed request re-registration cases.

- [ ] **Step 2: Write manual execution RED tests**

Assert a 5-year-old video is accepted, canonicalized only through `videos.list`, and persisted with source `manual_url` and source key equal to the safe manual request ID token. An unavailable/private/deleted video produces a successful unit with unavailable count 1, no candidate, and no provider/raw URL storage. Neither path reads or changes full-discovery cursors.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python -m pytest tests/backend/integration/test_youtube_manual_sync.py tests/backend/unit/test_youtube_discovery.py -q`

Expected: missing manual service/repository methods.

- [ ] **Step 4: Implement atomic request/job creation**

Within `BEGIN IMMEDIATE`: validate active subject/profile, parse to a canonical video ID before any write, find the unique `(profile_id, youtube_video_id)` request, and return its exact linked job if present. If that job is FAILED, verify completed artifacts and move the same job through `retry_failed_in_transaction`; if it is STOPPED or SUCCEEDED, return that terminal state unchanged. Otherwise insert one manual request, build one-unit sealed manual manifest bound to request/profile version/hash/video ID hash, create the job, and link it through `youtube_sync_manifests.manual_request_id`. Any partial failure rolls back both rows.

- [ ] **Step 5: Execute manual units through the common metadata transaction**

Fetch one ID through `videos.list`, normalize with `metadata.py`, then call the same `persist_metadata_batch()` used by seed/search. The manual source key is `manual-request:<positive-id>` and never contains the URL/video title. Complete the unit with a deterministic result hash; manual finalization succeeds the job without creating/promoting proposed cursors.

- [ ] **Step 6: Run GREEN and regression**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_manual_sync.py tests/backend/integration/test_discovery_records.py tests/backend/integration/test_youtube_cursor_promotion.py tests/backend/unit/test_youtube_discovery.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks strict URL equivalence, raw URL nonpersistence, transaction/race idempotency, any-age behavior, unavailable behavior, same common pipeline, and zero cursor side effects.

```powershell
git add src/market_voice_forecast_ledger/domain/discovery.py src/market_voice_forecast_ledger/repositories/discovery.py src/market_voice_forecast_ledger/services/youtube_sync.py tests/backend/integration/test_youtube_manual_sync.py tests/backend/unit/test_youtube_discovery.py
git commit -m "feat: queue manual YouTube candidates"
```

---

### Task 10: Crash recovery, quota defer, and one-shot queue worker

**Files:**
- Create: `src/market_voice_forecast_ledger/workers/__init__.py`
- Create: `src/market_voice_forecast_ledger/workers/scheduled_sync.py`
- Create: `src/market_voice_forecast_ledger/windows/__init__.py`
- Create: `src/market_voice_forecast_ledger/windows/task_scheduler.py`
- Modify: `src/market_voice_forecast_ledger/services/youtube_sync.py`
- Modify: `src/market_voice_forecast_ledger/repositories/discovery.py`
- Modify: `src/market_voice_forecast_ledger/services/job_state.py`
- Create: `tests/backend/integration/crash_youtube_sync_worker.py`
- Create: `tests/backend/integration/test_youtube_sync_recovery.py`
- Create: `tests/backend/integration/test_youtube_sync_worker.py`
- Create: `tests/backend/unit/test_task_scheduler.py`
- Modify: `tests/backend/youtube_fakes.py`

**Interfaces:**
- Consumes: `YouTubeSyncService.claim_next_runnable`, unit executors, `YouTubeProviderFailure`, `JobStateService.recover_interrupted`.
- Produces: `run_once(settings: Settings, dependencies: WorkerDependencies | None = None) -> WorkerSummary`.
- Produces: `WorkerSummary(claimed_jobs: int, completed_jobs: int, deferred_jobs: int, failed_jobs: int)`.
- Produces: `WorkerDependencies(credential_store: CredentialStore, transport: YouTubeTransport, schedule_reader: TaskScheduleReader, clock: Callable[[], datetime], sleeper: Callable[[float], None])` with `production(settings: Settings) -> WorkerDependencies`.
- Produces: `TaskWakeAdapter.request_start() -> None`, `TaskScheduleReader.status() -> ScheduledTaskStatus`, `ScheduledTaskStatus(installed: bool, local_time: str | None, start_when_available: bool, multiple_instances: str | None)` with `unavailable()`, `is_due(now: datetime) -> bool`, and `jst_day(now: datetime) -> date`; `TaskSchedulerAdapter` implements both protocols.
- Produces: `YouTubeSyncService.defer_current_unit(job_id: int, unit_key: str, error_code: str, resume_not_before_utc: datetime) -> None`.
- Produces: `YouTubeSyncService.recover_interrupted_job(job_id: int) -> ResumePlan`.

- [ ] **Step 1: Write retry/defer RED tests**

Parametrize credential missing/corrupt, network failure after four attempts, quota provider reason on first call, non-quota 429 recovery, 5xx recovery, and `Retry-After=61`. Assert safe error codes, exact attempt rows, persisted quota reservations, `RETRYING` only for deferred jobs, quota resume at exact `observed_at + 24h`, retry-after resume at exact `observed_at + 61s`, no claim before due, and same job ID after credential repair or due time.

- [ ] **Step 2: Write process-crash RED test**

The child helper opens the real temp DB, claims a fixed unit, commits RUNNING + checkpoint, then calls `os._exit(91)` before its network result. The parent verifies durable RUNNING state, invokes explicit recovery, sees an interrupted attempt and pending unit with prior successful unit artifacts preserved, reruns, and proves exactly one final observation/cursor promotion.

- [ ] **Step 3: Write drain/FIFO RED tests**

Queue full, manual, and another manual job while one is active. Assert `run_once()` processes one job at a time in created-ID order, continues through jobs already queued at process start or while it runs, stops when the remaining head is deferred, and never starts two active jobs across two worker processes. Repeated wakeups reuse one compatible daily full job; they do not generate a failure chain.

Add a fake schedule status matrix: not installed, before configured local time, exact configured time, after time with no daily request, after time with an existing daily request, and status-read failure with a queued manual job. Only due cases may call the daily full coalescer; the failure case must still drain the manual job. Add minimal `/Query /TN <fixed-name> /XML` and `/Run /TN <fixed-name>` adapter tests with argument arrays, `shell=False`, hidden window, fixed timeout, strict XML parsing, and native output suppression.

- [ ] **Step 4: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_sync_recovery.py tests/backend/integration/test_youtube_sync_worker.py -q
```

Expected: missing worker/defer/recovery methods.

- [ ] **Step 5: Implement same-job defer and recovery**

`defer_current_unit()` runs in one transaction: fail the running unit with a safe code, move the failed job through the existing retry primitive, store canonical `resume_not_before_utc`, and preserve verified success artifacts. If the transaction crashes, the next request/worker may see FAILED and invoke the same retry primitive. `recover_interrupted_job()` reconstructs artifact hashes from completed domain output records, calls existing interrupted recovery, and validates that every retained success still matches its checkpoint artifact.

- [ ] **Step 6: Implement the one-shot sequential worker**

```python
def run_once(settings, dependencies=None):
    deps = dependencies or WorkerDependencies.production(settings)
    conn = open_database(settings.database_path)
    try:
        apply_migrations(conn)
        bootstrap_reference_data(conn)
        service = YouTubeSyncService.from_dependencies(conn, deps)
        try:
            schedule = deps.schedule_reader.status()
        except DomainError as error:
            if error.code != "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE":
                raise
            schedule = ScheduledTaskStatus.unavailable()
        if schedule.is_due(deps.clock()) and not service.has_daily_request(schedule.jst_day(deps.clock())):
            service.ensure_daily_full_request(schedule.jst_day(deps.clock()))
        service.resume_failed_jobs_for_wake()
        while claimed := service.claim_next_runnable(deps.clock()):
            service.execute_claimed_job(claimed)
        return service.worker_summary()
    finally:
        conn.close()
```

`TaskScheduleReader` reads the configured local time from Task Scheduler, so schedule time is not duplicated in DB. Before that time an on-demand manual wake drains its queued job without creating the daily full job; at or after the time it performs missed-run catch-up. A status read failure suppresses only automatic daily creation and still drains already-durable API/manual jobs. `ensure_daily_full_request()` uses a unique JST calendar-day key and the same full-job coalescer. `resume_failed_jobs_for_wake()` runs once at process start, validates stored artifacts, and resets each eligible failed YouTube job at most once for this wake; a failure during the drain is not retried again in the same process. Thus repeated starts create at most one daily full request, failed work reuses its job without a failure chain, and manual-only jobs remain separate and never mutate full cursors. Worker exceptions are reduced to safe codes; raw exception/provider data is not logged.

- [ ] **Step 7: Run GREEN and full job-state regression**

Run:

```powershell
python -m pytest tests/backend/integration/test_youtube_sync_recovery.py tests/backend/integration/test_youtube_sync_worker.py tests/backend/integration/test_youtube_sync_atomicity.py tests/backend/integration/test_job_checkpoints.py tests/backend/integration/test_process_crash_recovery.py tests/backend/unit/test_task_scheduler.py -q
```

Expected: PASS, including child exit 91 test.

- [ ] **Step 8: Review and commit**

Reviewer checks transaction boundaries, same-job identity, defer timestamp, crash artifacts, no busy retry, FIFO/drain semantics, database singleton, schedule-time source, before-time manual wake behavior, missed-run catch-up, daily idempotency, and absence of a second state machine.

```powershell
git add src/market_voice_forecast_ledger/workers/__init__.py src/market_voice_forecast_ledger/workers/scheduled_sync.py src/market_voice_forecast_ledger/windows/__init__.py src/market_voice_forecast_ledger/windows/task_scheduler.py src/market_voice_forecast_ledger/services/youtube_sync.py src/market_voice_forecast_ledger/repositories/discovery.py src/market_voice_forecast_ledger/services/job_state.py tests/backend/integration/crash_youtube_sync_worker.py tests/backend/integration/test_youtube_sync_recovery.py tests/backend/integration/test_youtube_sync_worker.py tests/backend/unit/test_task_scheduler.py tests/backend/youtube_fakes.py
git commit -m "feat: recover durable YouTube sync work"
```

---

### Task 11: Strict loopback YouTube sync API

**Files:**
- Create: `src/market_voice_forecast_ledger/api/routes/youtube.py`
- Modify: `src/market_voice_forecast_ledger/api/routes/__init__.py`
- Modify: `src/market_voice_forecast_ledger/api/app.py`
- Modify: `src/market_voice_forecast_ledger/api/dependencies.py`
- Modify: `src/market_voice_forecast_ledger/api/models.py`
- Create: `tests/backend/integration/test_youtube_api.py`
- Modify: `tests/backend/integration/test_api_private_boundary.py`
- Modify: `tests/backend/integration/test_api_writes.py`

**Interfaces:**
- Consumes: `YouTubeSyncService.request_full_sync`, `request_manual_candidate`, `PublicReadAdapter.read_job`, `TaskWakeAdapter.request_start() -> None`.
- Produces: `POST /api/youtube-syncs`, `GET /api/youtube-syncs/{job_id}`, `POST /api/youtube-manual-candidates`.
- Produces: `get_task_wake_adapter` dependency and `task_wake_dependency(adapter)` test override.
- Produces strict `YouTubeSyncRequestResponse`, `YouTubeSyncStatusResponse`, `YouTubeManualCandidateRequest`, `YouTubeManualCandidateResponse`.

- [ ] **Step 1: Write API RED tests**

```python
def test_post_sync_persists_then_requests_scheduler_start(client, fake_wake):
    response = client.post("/api/youtube-syncs", json={})
    assert response.status_code == 202
    assert response.json() == {
        "job_id": 1,
        "status": "queued",
        "reused": False,
    }
    assert fake_wake.request_count == 1
```

Add same-job retry response, strict empty query/body shapes, positive canonical path ID, manual `{subject_id,url}` strict types, invalid URL 422, missing subject 404, inactive profile 422, same request reuse, and scheduler-start failure 503 while job/request remains durably queued.

- [ ] **Step 2: Write private response RED tests**

Populate query term, page token, description, title, source key, retry details, API key sentinel, local path, and provider body sentinel in private test doubles/storage. Assert none occurs in any JSON or raw response body. Status exposes only job ID/status, fixed-unit stage/counts, `resume_not_before_utc`, discovered/persisted/unavailable totals, and safe unit error codes.

- [ ] **Step 3: Run tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_task_scheduler.py tests/backend/integration/test_youtube_api.py tests/backend/integration/test_api_private_boundary.py -q`

Expected: 404/missing model/dependency failures.

- [ ] **Step 4: Implement strict request/response models**

```python
class YouTubeManualCandidateRequest(StrictApiModel):
    subject_id: int = Field(gt=0)
    url: str = Field(min_length=1, max_length=2048)


class YouTubeSyncRequestResponse(StrictApiModel):
    job_id: int = Field(gt=0)
    status: Literal["queued", "running", "retrying", "failed", "succeeded"]
    reused: bool
```

Add the new safe request field names to `_SAFE_LOCATIONS`. Map `YOUTUBE_SYNC_UNAVAILABLE` to 503, invalid URL/profile to 422, missing entities to 404, stored corruption to 500, and never return `DomainError.message`.

- [ ] **Step 5: Implement persist-before-wake route ordering**

Each POST commits the service transaction first, then calls `TaskWakeAdapter.request_start()`. If wake fails, raise `YOUTUBE_SYNC_UNAVAILABLE` without deleting or changing the queued job. GET validates the sealed YouTube manifest/checkpoints before constructing the public summary.

- [ ] **Step 6: Run focused GREEN and API regression**

Run:

```powershell
python -m pytest tests/backend/unit/test_task_scheduler.py tests/backend/integration/test_youtube_api.py tests/backend/integration/test_api_private_boundary.py tests/backend/integration/test_api_writes.py tests/backend/integration/test_api_reads.py -q
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks strict types/unknown fields, transaction-before-external-call order, 503 durability, status provenance validation, safe error classification, private-field absence, and no credential access in route code.

```powershell
git add src/market_voice_forecast_ledger/api/routes/youtube.py src/market_voice_forecast_ledger/api/routes/__init__.py src/market_voice_forecast_ledger/api/app.py src/market_voice_forecast_ledger/api/dependencies.py src/market_voice_forecast_ledger/api/models.py tests/backend/integration/test_youtube_api.py tests/backend/integration/test_api_private_boundary.py tests/backend/integration/test_api_writes.py
git commit -m "feat: expose YouTube sync requests"
```

---

### Task 12: Windows Task Scheduler adapter and operational CLI

**Files:**
- Modify: `src/market_voice_forecast_ledger/windows/task_scheduler.py`
- Modify: `tests/backend/unit/test_task_scheduler.py`
- Modify: `src/market_voice_forecast_ledger/cli.py`
- Modify: `tests/backend/integration/test_cli.py`

**Interfaces:**
- Consumes: `TaskSchedulerAdapter.request_start`, `workers.scheduled_sync.run_once`, `default_settings`, explicit `subprocess.run` callable.
- Produces: extends `TaskSchedulerAdapter` with `install(time: time) -> None`, `update(time: time) -> None`, and `remove() -> bool`; retains the Task 10 `status()` and `request_start()` contracts.
- Produces CLI: `youtube schedule install [--time HH:mm]` with default `06:00`, `update --time HH:mm`, `status`, `remove`; `youtube-sync worker --once`.

- [ ] **Step 1: Write XML and subprocess RED tests**

Assert generated UTF-16 Task Scheduler XML contains one calendar trigger at `YYYY-MM-DDTHH:mm:00+09:00`, `StartWhenAvailable>true`, `MultipleInstancesPolicy>Queue`, `LogonType>InteractiveToken`, no password, no API key/environment, and an action with `sys.executable`, `-m market_voice_forecast_ledger.cli youtube-sync worker --once`. Assert every `schtasks.exe` call uses an argument list, `shell=False`, hidden window, captured output, and fixed timeout.

- [ ] **Step 2: Write CLI and failure RED tests**

Cover omitted install time yielding `06:00`, canonical `00:00`/`06:00`/`23:59`, and reject `6:00`, seconds, offset, shell metacharacters, missing update time, and extra args. Test install/update temp XML cleanup on success/failure, status parse fail-closed, absent remove idempotency, `/Run` safe errors, and worker `--once` dependency seam without real network/Task Scheduler.

- [ ] **Step 3: Run tests and verify RED**

Run: `python -m pytest tests/backend/unit/test_task_scheduler.py tests/backend/integration/test_cli.py tests/backend/integration/test_youtube_api.py -q`

Expected: missing adapter/command failures.

- [ ] **Step 4: Implement exact task XML and adapter**

```xml
<Settings>
  <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
  <StartWhenAvailable>true</StartWhenAvailable>
  <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
</Settings>
```

Use a fixed task name `Market Voice Forecast Ledger - YouTube Sync`. Resolve the current user's SID with explicit `whoami.exe /user /fo csv /nh` arguments and strict CSV/SID validation, then write that SID as `Principal/UserId` with `InteractiveToken` and `LeastPrivilege`. Build XML with `xml.etree.ElementTree`, not string interpolation. Write it to `tempfile.TemporaryDirectory()` under local temp, invoke `schtasks.exe /Create /TN <name> /XML <path> /F`, and let the context delete it. `/Query /XML`, `/Run`, and `/Delete /F` use explicit arguments. Translate nonzero exits to safe scheduler codes without native stdout/stderr.

- [ ] **Step 5: Wire CLI and API dependency**

`youtube-sync worker --once` is the only task action and calls `run_once(default_settings())`. Schedule install/update/status/remove instantiate the real adapter. Preserve Task 11's request-scoped wake dependency and verify app creation itself still executes no `schtasks` command.

- [ ] **Step 6: Run dual-PowerShell and focused GREEN**

Run:

```powershell
python -m pytest tests/backend/unit/test_task_scheduler.py tests/backend/integration/test_cli.py tests/backend/integration/test_youtube_api.py -q
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/test-backend.ps1))"
pwsh -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/test-backend.ps1))"
```

Expected: PASS.

- [ ] **Step 7: Review and commit**

Reviewer checks interactive-token/no-password semantics, +09:00 boundary, catch-up/Queue, argument-array execution, temp cleanup, stdout/stderr privacy, exact CLI parser, and production/test dependency separation.

```powershell
git add src/market_voice_forecast_ledger/windows/task_scheduler.py src/market_voice_forecast_ledger/cli.py tests/backend/unit/test_task_scheduler.py tests/backend/integration/test_cli.py
git commit -m "feat: schedule daily YouTube sync"
```

---

### Task 13: Architecture, E2E, opt-in smoke, and operational handoff

**Files:**
- Create: `tests/backend/e2e/test_youtube_collection_flow.py`
- Create: `tests/backend/integration/test_youtube_architecture.py`
- Create: `tests/backend/integration/test_youtube_real_smoke.py`
- Modify: `tests/backend/e2e/synthetic_fixture.py`
- Modify: `README.md`
- Modify: `tests/backend/README.md`
- Modify: `docs/project/status.md`
- Modify: `docs/project/requirements.md`
- Modify: `docs/project/decisions.md`
- Modify: `docs/project/plan.md`
- Modify: `docs/superpowers/specs/2026-08-18-youtube-collection-design.md`
- Modify: `docs/superpowers/plans/2026-08-18-youtube-collection.md`

**Interfaces:**
- Consumes: every public interface produced by Tasks 1～12.
- Produces: one deterministic 4-profile fake-provider flow, architecture guard, read-only real smoke command, and exact Windows operation instructions.

- [ ] **Step 1: Write the E2E RED test**

The fake provider must return:

```python
SYNTHETIC_DISCOVERY = {
    "木野内栄治": {"seed": ("vidseed0001",), "search": ("vidshared01",)},
    "大川智宏": {"seed": (), "search": ("vidsearch02",)},
    "江守哲": {"seed": ("vidseed0003",), "search": ("videxternal3",)},
    "千竈 鉄平": {"seed": ("vidseed0004",), "search": ("vidshared01",)},
}
```

Use valid 11-character synthetic IDs in the actual fixture. Execute one full job through the public orchestrator/worker, then assert 4 person subjects, 4 profile versions, exact approved seed/search values, one shared video row, separate per-subject candidates, multiple observations, every candidate current state unverified, exact cursor map, succeeded sealed job, and no transcript/speaker/analysis row created by collection.

- [ ] **Step 2: Write architecture RED tests**

Parse/import production modules and assert:

```python
assert not legacy_schema_names & current_schema_names
assert not _runtime_matches(LEGACY_SYMBOL_PATTERN)
assert _orchestrator_subject_conditionals() == ()
assert _network_imports_outside("youtube/client.py") == ()
assert _native_credential_imports_outside(("credentials/windows.py", "cli.py", "workers/scheduled_sync.py")) == ()
assert _scheduler_imports_outside(("windows/task_scheduler.py", "cli.py", "api/dependencies.py")) == ()
```

The source-symbol scan inspects current Python runtime/tests, while a migrated in-memory DB supplies the authoritative final-schema names; cutover SQL is allowed to name legacy objects only in its DROP statements. The test must fail on subject IDs/canonical names inside orchestrator conditionals, legacy organization/fixed policy symbols in Python, legacy objects surviving final SQLite schema, network calls in repository/service, DB imports in discoverers, or multiple per-subject collector classes.

- [ ] **Step 3: Write the opt-in smoke test**

The test is collected always but runs only when `MVFL_RUN_YOUTUBE_SMOKE=1`. It reads the key through `WindowsCredentialManager`, calls `channels.list` for one approved seed and `videos.list` for one explicit synthetic/user-provided video ID environment value, asserts only envelope/schema shape, performs no DB writes, and never prints provider values. Without opt-in it marks a single named skip reason `real YouTube operational acceptance not requested`.

- [ ] **Step 4: Run new tests and verify RED**

Run:

```powershell
python -m pytest tests/backend/e2e/test_youtube_collection_flow.py tests/backend/integration/test_youtube_architecture.py tests/backend/integration/test_youtube_real_smoke.py -q
```

Expected before fixture/docs completion: E2E/architecture assertion failures; real smoke has one explicit opt-in skip.

- [ ] **Step 5: Complete the synthetic flow and architecture boundary**

Use only fake credential/transport/scheduler/clock/sleeper, real migration/repository/service/job/worker code, and synthetic metadata. Do not import private fixture factories from unrelated test modules. Keep search/display strings synthetic except the approved bootstrap names/configuration required to verify reference data.

- [ ] **Step 6: Update operational documentation with exact commands**

Document these commands and boundaries:

```powershell
python -m market_voice_forecast_ledger.cli youtube credential set
python -m market_voice_forecast_ledger.cli youtube credential status
python -m market_voice_forecast_ledger.cli youtube schedule install --time 06:00
python -m market_voice_forecast_ledger.cli youtube schedule status
python -m market_voice_forecast_ledger.cli youtube-sync worker --once
```

Document `COLLECTION_MODEL_RESET_REQUIRED` manual archive/delete/recreate procedure without an automatic destructive command. Mark audio/transcript/voice verification, live server acceptance, UI, and real YouTube smoke as excluded or pending unless separately observed. Amend the spec/plan status only with actual commit/test/review evidence; do not rewrite the approved design decisions.

- [ ] **Step 7: Run complete verification**

Run in this order:

```powershell
python -m pytest tests/backend -q
python -m compileall -q src tests/backend
powershell -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite All
powershell -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Mode WorkingTree -Path .
git diff --check
powershell -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

Expected: every command exits 0; only the established Windows symlink capability skip and the explicit real-YouTube-smoke opt-in skip are permitted and must be named separately in the report.

- [ ] **Step 8: Run the optional real smoke only with explicit user approval**

If and only if the user explicitly authorizes a real API call and has configured Credential Manager:

```powershell
$env:MVFL_RUN_YOUTUBE_SMOKE='1'
$env:MVFL_YOUTUBE_SMOKE_VIDEO_ID='abcdefghijk'
python -m pytest tests/backend/integration/test_youtube_real_smoke.py -q
Remove-Item Env:MVFL_RUN_YOUTUBE_SMOKE
Remove-Item Env:MVFL_YOUTUBE_SMOKE_VIDEO_ID
```

Replace the example ID only in the process environment, never in repository files. Record call count and pass/fail, not returned metadata. If not authorized/configured, leave operational acceptance explicitly pending.

- [ ] **Step 9: Request final independent review**

Reviewer reads the approved spec, all task commits, final schema, orchestrator, adapters, API, tests, and docs. Required verdict areas: clean cutover, four-person config-only model, no legacy symbols, no network/DB responsibility leak, no secret/private response, append-only provenance, exact same-video behavior, retry/quota/cursor crash safety, scheduler semantics, and no audio/analysis loop. Resolve all Critical/Important findings with sequential RED/GREEN and rerun the entire final gate.

- [ ] **Step 10: Stage exact files, run staged safety, and commit**

```powershell
git diff --name-only
git diff --check
git add README.md tests/backend/README.md docs/project/status.md docs/project/requirements.md docs/project/decisions.md docs/project/plan.md docs/superpowers/specs/2026-08-18-youtube-collection-design.md docs/superpowers/plans/2026-08-18-youtube-collection.md tests/backend/e2e/test_youtube_collection_flow.py tests/backend/integration/test_youtube_architecture.py tests/backend/integration/test_youtube_real_smoke.py tests/backend/e2e/synthetic_fixture.py
powershell -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Mode Staged -Path .
git diff --cached --check
git commit -m "test: verify YouTube collection flow"
```

After commit, verify exact HEAD/message/path set, `git status --short` empty, no upstream/push occurred, and create an ignored task report under `.superpowers/sdd/2026-08-18-youtube-collection/` with observed—not anticipated—counts.

---

## Plan Self-Review Checklist

- [x] Every approved spec section maps to at least one Task: cutover (1), profile/persistence (2), credential (3), client/quota (4), discovery/metadata (5), jobs (6), seed (7), search/cursor (8), manual (9), recovery/worker (10), API (11), scheduler/CLI (12), E2E/docs/smoke (13).
- [x] The writing-plans placeholder scan returned no forbidden placeholder phrase.
- [x] Signatures are consistent for `DiscoveryProfileVersion`, `CanonicalVideoMetadata`, `persist_metadata_batch`, `request_full_sync`, `request_manual_candidate`, `claim_next_runnable`, `run_once`, and scheduler/credential protocols.
- [x] The file-role order audit returned `FILE_ROLE_ORDER_OK`: every created file has one first producer and later edits are marked Modify.
- [x] Every Task contains an executable RED command, concrete implementation contract, GREEN command, review gate, and exact-path commit.
- [x] Historic migrations remain untouched; architecture checks inspect current Python and migrated schema while allowing legacy names only in cutover DROP statements.
- [x] No task adds confirmed/rejected public writers, collection-triggered audio/transcription/analysis, UI, real model execution, or automatic DB deletion.

## Execution Handoff

The plan is planning-only. After the user explicitly approves implementation, choose one execution mode:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`; dispatch a fresh implementer for each Task and a separate reviewer before the next Task.
2. **Inline Execution:** use `superpowers:executing-plans`; execute Tasks in dependency order with review checkpoints after each commit.

In either mode, Task 1 begins only after an isolated worktree and exact baseline verification are complete.

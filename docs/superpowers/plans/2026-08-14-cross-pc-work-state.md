# 複数PC間の作業状態保存・再開 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 公開GitHubを介して、別PCのCodexが判断経緯と実装状態を再構築できる保存・再開基盤をローカル実装し、安全に検証する。

**Architecture:** 4つの更新型Markdown状態ファイルを情報の正本にし、`AGENTS.md` は短いルーティングだけを担う。保存と再開を別々のリポジトリスコープスキルにし、機械判定できる状態文書検査、公開安全検査、Git状態取得、remote SHA確認はPowerShellスクリプトへ分離する。

**Tech Stack:** Markdown、Codex repository skills、PowerShell 7/Windows PowerShell互換構文、Git、Codex CLI `gpt-5.6-sol`。

## Global Constraints

- 対象プロジェクトでは、最終承認前にGit初期化、commit、GitHubリポジトリ作成、remote接続、pushを行わない。
- テスト用Git操作はシステム一時ディレクトリに作成した使い捨てfixtureだけを対象にする。
- 通常の保存ごとに新しい状態ファイルを作らず、4つの既存状態ファイルを更新する。
- 無関係なユーザー変更を変更、削除、stage、commitしない。
- 保存完了はpush成功とremote SHA一致の両方が確認できた場合だけとする。
- 公開物へ認証情報、実データ、大量の文字起こし、音声、動画、話者特徴量を含めない。
- 作成するスキルは一つずつbaseline、実装、適用後テスト、構造検証を完了してから次へ進む。

---

### Task 1: 状態管理文書とリポジトリ共通規則

**Files:**
- Create: `AGENTS.md`
- Create: `README.md`
- Create: `.gitignore`
- Create: `.gitattributes`
- Create: `.editorconfig`
- Create: `docs/project/requirements.md`
- Create: `docs/project/decisions.md`
- Create: `docs/project/plan.md`
- Create: `docs/project/status.md`
- Create: `docs/project/public-data-policy.md`

**Interfaces:**
- Consumes: 承認済み設計とこれまでに確定した相場見通し発言台帳の要件。
- Produces: 保存・再開スキルが更新・読込する固定パスと必須見出し。

- [x] `tests/work-state/run-tests.ps1` に、必須ファイル、必須見出し、`AGENTS.md` の行数上限、相互リンクを検査する失敗テストを追加する。
- [x] テストを実行し、対象文書が存在しないため失敗することを確認する。
- [x] 5つの共通ファイルと5つの状態・方針文書を作成する。
- [x] テストを再実行し、文書構造検査が成功することを確認する。
- [x] commitはGlobal Constraintsにより保留し、変更一覧を記録する。

### Task 2: 決定的な状態・公開安全検査スクリプト

**Files:**
- Create: `scripts/work-state/inspect-git-state.ps1`
- Create: `scripts/work-state/check-public-safety.ps1`
- Create: `scripts/work-state/check-state-docs.ps1`
- Create: `scripts/work-state/verify-remote-head.ps1`
- Modify: `tests/work-state/run-tests.ps1`

**Interfaces:**
- Produces: 全スクリプトは成功時exit 0、違反・不整合時は非0を返す。`inspect-git-state.ps1 -Json` はroot、branch、head、upstream、dirty、ahead、behind、remotesをJSONで返す。`verify-remote-head.ps1` はremote branch SHAとHEADの一致だけを成功とする。

- [x] 安全なfixture、秘密文字列fixture、禁止拡張子fixture、大容量fixture、Gitでないfixture、remote一致・不一致fixtureの失敗テストを追加する。
- [x] テストを実行し、スクリプト未作成のため失敗することを確認する。
- [x] 4スクリプトを最小実装する。
- [x] テストを再実行して成功を確認する。
- [x] 境界条件として未追跡ファイル、upstream不在、bare remote不在を追加検証する。
- [x] commitはGlobal Constraintsにより保留し、変更一覧を記録する。

### Task 3: 保存スキル

**Files:**
- Create: `.agents/skills/save-work-state/SKILL.md`
- Create: `.agents/skills/save-work-state/agents/openai.yaml`
- Create: `tests/work-state/scenarios/save-work-state.md`
- Modify: `tests/work-state/run-tests.ps1`

**Interfaces:**
- Consumes: 4状態ファイルとTask 2のスクリプト。
- Produces: 「別PCへ引き継げるようにして」「現在の進捗を記録して」「GitHubへ反映して」などで起動する保存契約。

- [x] dirty、無関係変更、push失敗、急ぎという圧力を組み合わせた保存baselineシナリオを作る。
- [x] スキルなしの別プロセスCodex CLIへシナリオを与え、安全契約から外れる挙動を記録する。
- [x] baselineの欠落だけを補う最小の `SKILL.md` と `openai.yaml` を作る。
- [x] skill-creatorの `quick_validate.py` 相当のローカル構造テストを実行する（PyYAML不在のため公式validator自体は未実行）。
- [x] 同じシナリオをスキル付きCodex CLIへ与え、対象限定stage、push後検証、失敗時非完了報告に従うことを確認する。
- [x] 新しい抜け道がないことを5反復で確認する。
- [x] commitと実remoteへのpushはGlobal Constraintsにより保留する。

### Task 4: 再開スキル

**Files:**
- Create: `.agents/skills/resume-work-state/SKILL.md`
- Create: `.agents/skills/resume-work-state/agents/openai.yaml`
- Create: `tests/work-state/scenarios/resume-work-state.md`
- Modify: `tests/work-state/run-tests.ps1`

**Interfaces:**
- Consumes: `AGENTS.md`、4状態ファイル、Task 2のGit状態検査。
- Produces: 「前回の続き」「別PCの作業を引き継いで」「最新の進捗を読み込んで」などで起動する再開契約。

- [x] dirty、remote進行、状態説明と実ファイルの不一致、すぐ実装開始という圧力を組み合わせたbaselineシナリオを作る。
- [x] スキルなしの別プロセスCodex CLIで安全でない更新または照合不足を記録する。
- [x] baselineの欠落だけを補う最小の `SKILL.md` と `openai.yaml` を作る。
- [x] skill-creatorの `quick_validate.py` 相当のローカル構造テストを実行する（PyYAML不在のため公式validator自体は未実行）。
- [x] 同じシナリオをスキル付きCodex CLIへ与え、dirty時停止、fast-forward-only、実体優先、作業前サマリーに従うことを確認する。
- [x] 新しい抜け道がないことを5反復で確認する。
- [x] commitと実remoteからのpullはGlobal Constraintsにより保留する。

### Task 5: 一時Git remoteによる統合試験

**Files:**
- Modify: `tests/work-state/run-tests.ps1`
- Create: `tests/work-state/README.md`

**Interfaces:**
- Consumes: Task 1～4の全成果物。
- Produces: 対象プロジェクトをGit化せずに行う保存・再開の検証結果。

- [x] システム一時領域へsource、bare remote、second cloneを作る統合テストを追加する。
- [x] remote SHA一致、remote SHA不一致、dirty clone、履歴分岐、安全検査失敗を再現する。
- [x] 全テストを実行し、119 passed、0 failedを確認する。
- [x] `.gitignore` と公開方針に禁止データ種別の漏れがないか照合し、`secrets/` と `credentials/` の強制stageも拒否するよう補強した。
- [x] 実装ファイル一覧、テスト結果、既知の制約、初回公開予定一覧を `docs/project/status.md` へ反映する。
- [ ] 実Git初期化、commit、GitHub作成、pushを行わずユーザーへ最終承認を求める。

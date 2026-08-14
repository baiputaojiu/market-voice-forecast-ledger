# 作業状態

最終更新: 2026-08-14 JST

この文書の状態は、このファイルを含むcommitに対応する。SHAは本文へ埋め込まず、Gitから取得する。

## 現在のフェーズ（Current Phase）

M0「複数PC間の作業状態保存・再開基盤」は完了。公開GitHubと別cloneから要件、決定、計画、進捗、2つのリポジトリスキル、検証手順を再構築できる。アプリ本体は設計段階で、実装未着手。次はM1「アプリ設計の完成」。

## Git状態（Git State）

- 公開リポジトリ: `https://github.com/baiputaojiu/market-voice-forecast-ledger`
- branch: `main`
- upstream: `origin/main`
- visibility: `PUBLIC`
- commit SHAとahead/behindは本文へ固定せず、`scripts/work-state/inspect-git-state.ps1 -Json`で取得する。
- 保存完了は`scripts/work-state/verify-remote-head.ps1`によるlive remote SHA一致を条件とする。

## 完了済み（Completed）

- プロジェクトの目的、対象者、対象資産、期間、予想分類、情報境界を確定した。
- ローカルWebアプリ構成と、4資産比較ヒートマップ、分析入力、実作業単位による進捗画面の方針を承認した。
- 作業状態保存・再開方式の調査、方式比較、設計承認、詳細実装計画を完了した。
- `AGENTS.md`、4つの更新型状態文書、公開データ方針、README、除外・改行・文字コード設定を実装した。
- Git状態、状態文書、公開安全、live remote SHAを検査する4つのPowerShellスクリプトを実装した。
- `$save-work-state` と `$resume-work-state` をリポジトリスコープのCodexスキルとして実装した。
- 両スキルをbaselineと適用後で各5反復評価し、安定した結果を固定の評価文書へ記録した。
- 一時source、bare remote、second cloneを使う統合試験を実装した。
- 承認済み26ファイルだけを初回commitし、公開GitHubの`main`へpushした。
- 初回push後にlocal HEADとlive `origin/main`のSHA一致を確認した。
- GitHubから別cloneし、26ファイルのみ、`.stitch`なし、clean、ahead/behind 0、全119テスト成功を確認した。

## 作業中（In Progress）

- 現在進行中のアプリ実装作業はない。
- 次の作業としてM1のデータモデルと変更不能スナップショット形式の設計を開始できる状態。

## 未着手（Not Started）

- データモデルと変更不能スナップショット形式の確定。
- YouTube収集、重複検出、音声処理、話者確認の詳細設計。
- Codex分析prompt、JSON Schema、バッチmanifest、集約規則の確定。
- UI例外処理、再試行、監査ログ、テスト戦略の詳細化。
- M2以降のアプリ実装。

## 検証結果（Verification Results）

- 文書構造: 最初に必須文書欠落によるREDを確認し、追加後はGREEN。検証説明追加時も4件のREDを確認してから修正した。
- 補助スクリプト: 未作成によるREDを確認後、Git状態・公開安全・状態文書・remote SHA検査18件がGREEN。
- 公開安全の境界: `credentials/`強制stageの抜けをREDで再現し、禁止ディレクトリ追加後にGREEN。
- 統合試験: second cloneのbehind、fast-forward、dirty、禁止DB、禁止資格情報、秘密文字列、履歴分岐、remote不一致・到達不能を含む12件がGREEN。
- 保存スキル: baseline 0/5、適用後5/5が完全契約を満たした。
- 再開スキル: baseline 0/5、適用後5/5が完全契約を満たした。
- 全決定的スイート: 2026-08-14に119 passed、0 failed。
- 公開直前のworking tree検査とstaged検査は26ファイルで合格。`.stitch`、実データ、秘密情報はcommitされていない。
- 公開GitHubは`PUBLIC`、既定branchは`main`、初回push後のlive remote SHA照合は成功。
- GitHubからの別cloneでも状態文書検査、公開安全検査、119件の全テストが成功。

## 既知の問題（Known Issues）

- `.stitch/DESIGN.md` と `.stitch/metadata.json` の日本語が文字化けしている。
- `.stitch` のHTMLには実在人物名と架空の予想・証拠文が組み合わされ、外部CDN、Google Fonts、外部プロフィール画像も参照されている。
- `analysis-run.html` など一部画面に英語の「Analyst Ledger」が残る。
- `.stitch`生生成物は公開対象外。公開用の画面資料はM1で別途無害化する必要がある。
- skill-creator公式`quick_validate.py`はローカルPythonにPyYAMLがないため未実行。frontmatter、metadata、必須契約はローカル構造テスト34件とCodex反復評価で検証した。
- CPUのみの音声処理エンジンは未検証。

## 未解決事項（Open Questions）

- Windowsネイティブ音声処理とWSL2アダプターの最終選択。M1の技術検証で決める。
- 公開用の画面資料で実在する分析主体名を残すか、合成名だけにするか。誤認防止の観点から合成名を推奨する。

## 次の作業（Next Actions）

1. M1のデータモデルと変更不能スナップショット形式を設計する。
2. 収集、文字起こし、話者分離・割当、予想分析の境界と入出力を詳細化する。
3. Codex分析のprompt、JSON Schema、バッチmanifest、集約規則を確定する。
4. UI例外処理、再試行、監査ログ、テスト戦略を確定する。

## 重要ファイル（Important Files）

- `docs/project/requirements.md`: 現在有効な要件の正本。
- `docs/project/decisions.md`: 重要な決定、理由、却下案。
- `docs/project/plan.md`: 現在のマイルストーンと作業順序。
- `docs/project/public-data-policy.md`: 公開・非公開情報の境界。
- `docs/superpowers/specs/2026-08-14-cross-pc-work-state-design.md`: 保存・再開基盤の承認済み設計。
- `.agents/skills/save-work-state/SKILL.md`: GitHubへ保存する処理契約。
- `.agents/skills/resume-work-state/SKILL.md`: 別PCで再開する処理契約。
- `tests/work-state/run-tests.ps1`: 決定的な全検査の入口。
- `tests/work-state/skill-evaluation.md`: スキル反復評価の固定記録。
- `.stitch/`: 公開しないローカル視覚設計原本。

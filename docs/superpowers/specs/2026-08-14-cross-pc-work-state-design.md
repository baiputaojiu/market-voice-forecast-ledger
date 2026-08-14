# 複数PC間の作業状態保存・再開 設計

## 目的

公開GitHubリポジトリとリポジトリスコープのCodexスキルを使い、同時編集を行わない複数PC間で開発を安全に引き継ぐ。別PCのCodexが、ソースコードだけでなく、要件、判断理由、現在の計画、検証結果、未解決事項、次の作業を理解して再開できる状態を作る。

## 現状

- 対象ディレクトリは `market-voice-forecast-ledger`。
- Gitリポジトリではなく、remote、branch、commitは存在しない。
- アプリの実装とテストは未着手。
- `.stitch` 以下にデザインシステム、HTML、PNG、メタデータがある。
- ヒートマップ、分析入力、進捗画面のUI方針と、ローカルWebアプリの全体構成は承認済み。
- `.stitch/DESIGN.md` と `.stitch/metadata.json` の日本語には文字化けがある。
- StitchのHTMLには実在人物名とデザイン確認用の架空発言が組み合わされ、外部CDNと外部画像も参照されている。

## 採用方式

4つの更新型状態ファイル、2つのリポジトリスコープスキル、決定的な安全確認スクリプトを組み合わせる。通常の保存ごとに新規状態ファイルを作らず、過去状態はGit履歴から取得する。

単一の巨大な状態ファイルは、要件と進捗と履歴が混ざって読みづらくなるため採用しない。GitHub Issueを正本にする方式は、認証と外部状態へ依存し、公開範囲の制御も複雑になるため採用しない。

## リポジトリ構成

```text
market-voice-forecast-ledger/
├─ AGENTS.md
├─ README.md
├─ .gitignore
├─ .gitattributes
├─ .editorconfig
├─ .agents/skills/
│  ├─ save-work-state/
│  │  ├─ SKILL.md
│  │  └─ agents/openai.yaml
│  └─ resume-work-state/
│     ├─ SKILL.md
│     └─ agents/openai.yaml
├─ docs/
│  ├─ project/
│  │  ├─ requirements.md
│  │  ├─ decisions.md
│  │  ├─ plan.md
│  │  ├─ status.md
│  │  └─ public-data-policy.md
│  ├─ design/assets/
│  └─ superpowers/{specs,plans}/
├─ scripts/work-state/
│  ├─ inspect-git-state.ps1
│  ├─ check-public-safety.ps1
│  ├─ check-state-docs.ps1
│  └─ verify-remote-head.ps1
└─ tests/work-state/
```

`frontend`、`backend`、`examples/synthetic` はアプリ実装時に必要になった段階で作成する。空ディレクトリは先行して作らない。

## 状態ファイルの責務

### `requirements.md`

現在有効な要件の正本とする。目的、スコープ、機能要件、非機能要件、分析制約、MVP外の将来機能を記録する。進捗や時系列の作業履歴は記録しない。

### `decisions.md`

重要な採用・却下・置換済み判断を、安定した決定ID、状態、内容、理由、比較案、影響範囲とともに記録する。会話全文は複製しない。

### `plan.md`

現在有効なマイルストーン、完了条件、依存関係、完了・作業中・未着手のタスクを記録する。過去の詳細な日誌はGit履歴に委ねる。

### `status.md`

再開時の最初の入口とする。最終更新日時、現在フェーズ、Git状態、完了事項、作業中、未着手、検証結果、既知の問題、未解決事項、次の3～5作業、重要ファイルを記録する。

コミットSHAは自己参照を避けるためファイル本文へ固定しない。「この状態は、このファイルを含むコミット」と定義し、実際のSHAはGitから取得して保存結果に表示する。

## `AGENTS.md`

常時必要な短い規則だけを書く。

- プロジェクトの一文説明
- 最初に読む状態ファイルの順序
- 状態説明よりソース、テスト、Git状態を優先する規則
- 予想分析にWeb検索、現在相場、Codexの一般知識を混ぜない規則
- 秘密情報と実データをcommitしない規則
- 保存要求を `$save-work-state`、再開要求を `$resume-work-state` へ誘導する案内
- 標準検証コマンド

進捗、長い判断履歴、詳細なGit手順は書かない。

## 保存スキル

`.agents/skills/save-work-state` に配置する。別PCへ引き継げる状態にする意図、現在の進捗を記録する意図、GitHubへ反映する意図で暗黙に起動できるdescriptionを持たせる。

保存処理は次の契約に従う。

1. Gitルート、ブランチ、upstream、作業ツリー、未追跡ファイル、remoteとの差を確認する。
2. 会話だけでなく、実ファイル、設計、テスト結果、Git差分を確認する。
3. 4つの状態ファイルを必要な範囲だけ更新する。
4. 状態文書の構造と公開安全性をスクリプトで検査する。
5. 今回の作業に関係するファイルだけを明示的にstageする。`git add .` は使用しない。
6. staged差分を確認して、焦点の合ったcommitを作る。
7. upstreamへpushする。
8. ローカルHEADとremote branchのSHA一致を確認する。
9. 一致した場合だけ保存完了と報告する。

remote未設定、upstream未設定、履歴分岐、秘密情報検出、安全検査失敗、push失敗、remote SHA不一致では保存完了と報告しない。force push、無関係な変更のstage、履歴書き換えは行わない。

## 再開スキル

`.agents/skills/resume-work-state` に配置する。前回の続き、別PCからの引き継ぎ、GitHubの最新進捗の読み込みを求められた場合に暗黙に起動できるdescriptionを持たせる。

再開処理は次の契約に従う。

1. Gitルート、branch、upstream、作業ツリーを確認する。
2. 作業ツリーがクリーンでupstreamが設定済みの場合だけfetchし、fast-forward-onlyで更新する。
3. dirty、履歴分岐、upstream不在ではstash、reset、rebaseを行わず停止して報告する。
4. `AGENTS.md`、`status.md`、`requirements.md`、`decisions.md`、`plan.md` を読む。
5. ソース、設計、テスト結果、Git履歴と照合する。
6. 不一致では実ファイル、テスト結果、Git状態を優先する。
7. 作業開始前に目的、完了事項、進捗、未解決事項、次の作業、阻害要因を簡潔に示す。

## Git運用

同時作業を行わないMVP期間は `main` を唯一の作業ブランチとする。

- 再開時は `fetch` と `pull --ff-only` を使う。
- PC切り替え時は状態更新、安全検査、commit、push、remote SHA確認まで行う。
- push済み履歴を書き換えず、force pushしない。
- push失敗時はローカルで完了した段階と未完了内容を明示する。
- 複数人の同時開発へ移行する時点でfeature branchとpull requestへ変更する。

対象プロジェクトへのGit初期化、最初のcommit、公開GitHubリポジトリ作成、最初のpushは、ローカル実装と検証結果をユーザーへ提示し、最終承認を得た後にだけ実行する。

## 公開範囲

公開するものはソースコード、テスト、合成サンプル、公開可能な設定例、設計資料、確定要件、決定事項、計画、進捗要約、スキル、安全確認スクリプトとする。

公開しないものは認証情報、個人情報、PC固有秘密、YouTube動画・音声、大量の全文文字起こし、本番データベース、分析入力スナップショット、話者埋め込み、音声特徴量、モデル、キャッシュ、一時ファイル、ログ、権利確認できない情報とする。

アプリの実データ既定保存先はリポジトリ外の `%LOCALAPPDATA%\MarketVoiceForecastLedger\` とする。Stitchの生生成物は公開対象から除外し、承認済み設計は文字化けと実在人物への架空発言割当を除去して `docs/design` に保存する。

## 検証

対象プロジェクトをGit化する前に次を確認する。

1. 状態ファイルの必須見出しと内部リンク。
2. 禁止拡張子、禁止ディレクトリ、秘密文字列、大容量ファイルの検出。
3. 各スキルのfrontmatter、名前、description、UIメタデータ。
4. スキルなしのCodex CLIテストで起きる失敗を記録し、スキル適用後に安全契約へ従うこと。
5. 一時Gitリポジトリとローカルbare remoteを使う保存、push、remote SHA一致の模擬試験。
6. 別の一時cloneを使う再開試験。
7. dirty、分岐、秘密情報、push失敗時の安全停止。

ローカル検証後、初回公開予定のファイル一覧と結果を提示する。ユーザーの最終承認後にだけGitHubへ公開し、公開後は別ディレクトリへcloneして実remoteによる再開確認を行う。

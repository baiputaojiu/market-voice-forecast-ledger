# Repository guidance

このリポジトリは、YouTube上の対象者の発言だけを根拠に、相場見通しを記録・比較するローカルWebアプリを開発する。

## 作業開始時

1. `docs/project/status.md` を読む。
2. `docs/project/requirements.md` と `docs/project/decisions.md` を読む。
3. `docs/project/plan.md` と、状態文書が指す設計・テストを確認する。
4. 状態文書と実ファイルが食い違う場合は、ソースコード、テスト結果、Git状態を優先し、差異を報告する。

## 常時適用する規則

- 予想分析をWeb検索、現在の相場、Codex自身の一般知識で補強しない。指定日時点までに保存された対象者の発言だけを使う。
- 話者分離・話者割当と、将来予想の分析を別機能として保つ。
- APIキー、認証情報、YouTube動画・音声、全文文字起こし、本番DB、話者埋め込み、モデル、キャッシュ、ログをcommitしない。
- 無関係な既存変更を変更、削除、stage、commitしない。`git add .` とforce pushを使わない。
- 作業状態ファイルを通常の保存ごとに増やさない。現在状態は既存ファイルを更新し、履歴はGitに残す。

## 保存と再開

- 「進捗を記録」「別PCへ引き継ぐ」「GitHubへ反映」などの依頼では `$save-work-state` を使う。
- 「前回の続き」「GitHubの最新状態から再開」「別PCの作業を引き継ぐ」などの依頼では `$resume-work-state` を使う。
- 保存完了は、push成功後にremote branchとローカルHEADの一致を確認できた場合だけ報告する。

## 検証

作業状態基盤は `powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1` で検証する。

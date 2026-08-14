# 相場見通し発言台帳

YouTube上の対象者の発言を収集し、発言だけを根拠に日経平均、TOPIX、S&P 500、XAU/USDの将来見通しを整理・比較するWindows向けローカルWebアプリです。現在は設計と開発基盤の整備段階で、投資判断に利用できる完成版ではありません。

## 現在の状態

- [最新の引き継ぎ状態](docs/project/status.md)
- [確定要件](docs/project/requirements.md)
- [重要な決定と理由](docs/project/decisions.md)
- [現在の計画](docs/project/plan.md)
- [公開データ方針](docs/project/public-data-policy.md)

## 別PCで再開する

1. 公開GitHubリポジトリをcloneする。
2. cloneしたリポジトリのルートをCodexで開く。
3. 「GitHubに保存した最新状態から再開して」と依頼する。

Codexはリポジトリ内の `$resume-work-state` スキルを使い、Git、状態文書、実ファイル、テスト結果を照合してから次の作業を提示します。

## 作業状態を保存する

「別PCへ引き継げるようにして」または「今日の作業内容をGitHubへ反映して」と依頼します。`$save-work-state` は状態文書と公開安全性を検査し、対象を限定してcommit・pushした後、remoteへの反映まで確認します。

## 注意

- 予想分析は、保存された対象者の発言だけを根拠にします。Web検索、現在相場、Codexの一般知識では補強しません。
- 実際に収集した動画、音声、全文文字起こし、本番データベースはGitHubへ保存しません。
- 分析結果は投資助言ではありません。

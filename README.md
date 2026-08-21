# 相場見通し発言台帳

YouTube上の対象者の発言を収集し、発言だけを根拠に日経平均、TOPIX、S&P 500、XAU/USDの将来見通しを整理・比較するWindows向けローカルWebアプリです。M2中核バックエンドはユーザー受け入れ済みです。YouTube収集subprojectはTask 1～13の実装・独立review・完全合成E2E・architecture guard・明示opt-inの実YouTube read-only smokeを完了し、`feat: add durable YouTube collection pipeline (#1)`として`main`へ統合済みです。複数日の収集運用、音声・本人声確認・予想分析・UIを含む完成版は未完了で、投資判断には利用できません。

## 現在の状態

- [最新の引き継ぎ状態](docs/project/status.md)
- [確定要件](docs/project/requirements.md)
- [重要な決定と理由](docs/project/decisions.md)
- [現在の計画](docs/project/plan.md)
- [公開データ方針](docs/project/public-data-policy.md)

YouTube収集のsquash統合commitは`157f739`です。公開済みruntime codeは
Task Scheduler一覧互換修正`95ff083`に続き、Windowsが登録XMLを再出力するときの宣言・既定値・
設定正規化へ対応する`5db7dbf`まで`origin/main`へ反映済みです。source commit `9adef31`の
feature branchとworktreeは統合後にcleanupしました。統合treeでは関連216 testsが成功し、
全backendは1747件中1745 passed、既存Windows symlink capability skip 1件、
明示opt-in real smoke skip 1件でした。通常testはfake credential、transport、
scheduler、clock、sleeperと一時SQLiteだけを使用し、実YouTubeへ接続しません。

## WindowsでYouTube収集を運用する

API keyはコマンド引数、環境変数、設定ファイル、DBへ渡さず、非表示promptから
Windows Credential Managerへ登録します。状態確認は設定済みかどうかだけを表示します。

```powershell
python -m market_voice_forecast_ledger.cli youtube credential set
python -m market_voice_forecast_ledger.cli youtube credential status
```

現在のWindowsユーザーに、毎日06:00 JST、ログオン中のみ、取りこぼしを次の
利用可能時に実行し、複数instanceをqueueするtaskを登録して状態を確認します。

```powershell
python -m market_voice_forecast_ledger.cli youtube schedule install --time 06:00
python -m market_voice_forecast_ledger.cli youtube schedule status
```

2026-08-22 JST時点の開発端末ではCredentialが`configured`、Task Schedulerが
`installed 06:00`であることを、秘密値を読み出さず確認しています。scheduler XML
正規化修正`5db7dbf`はremote反映済みです。実YouTube read-only smokeも、公開video IDを
process環境だけに置いて成功し、終了後に環境変数を削除しています。

登録済みqueueを同じone-shot workerで処理します。`--once`はprocessを1回起動し、
runnableなdurable jobをquota/defer境界まで順番にdrainする意味です。
`search.list`はUTC日ごとに100 attempts、`channels.list`・`playlistItems.list`・
`videos.list`は合計10,000 attemptsまでをcall前に原子的に予約します。上限到達時は
providerへ接続せず24時間deferし、同じcheckpointから再開します。1日windowが
10 pageを超える場合もpage tokenを保持してpage 11以降を継続し、cursorはwindowを
最後まで消費した後だけ進めます。

```powershell
python -m market_voice_forecast_ledger.cli youtube-sync worker --once
```

### `COLLECTION_MODEL_RESET_REQUIRED` の手動復旧

このsafe codeは、旧collection modelのDBを推測変換せず、元DBを変更しないで停止した
ことを示します。アプリとworkerを終了し、エクスプローラーで
`%LOCALAPPDATA%\MarketVoiceForecastLedger\`の`ledger.sqlite3`と、存在する同名の
`-wal`・`-shm`を任意の退避先へコピーし、退避物を確認してください。その後に限り、
元の3ファイルを手動で削除し、通常のアプリ起動または上記worker起動で空DBを再作成します。
自動削除・自動rename・自動変換するコマンドは提供しません。退避DBは新DBへ自動取込されません。

## 別PCで再開する

1. 公開GitHubリポジトリをcloneする。
2. cloneしたリポジトリのルートをCodexで開く。
3. 「GitHubに保存した最新状態から再開して」と依頼する。

Codexはリポジトリ内の `$resume-work-state` スキルを使い、Git、状態文書、実ファイル、テスト結果を照合してから次の作業を提示します。

## 作業状態を保存する

「別PCへ引き継げるようにして」または「今日の作業内容をGitHubへ反映して」と依頼します。`$save-work-state` は状態文書と公開安全性を検査し、対象を限定してcommit・pushした後、remoteへの反映まで確認します。

## Windowsでバックエンドを検証する

Python 3.11以上を用意し、リポジトリのルートで次を実行します。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests/backend -q
```

`.[dev]` のbootstrap後に行うwheel回帰は、package indexを無効にし、
`--no-build-isolation --no-deps`で埋め込みmigrationを検証します。これは
未bootstrapのfresh machineで依存packageまでoffline導入できるという主張ではありません。

バックエンド全件、compileall、既存の作業状態検査、公開安全性検査、
`git diff --check`を一度に実行する入口は次です。このscriptはrepositoryの
`.venv\Scripts\python.exe`があれば必ずそれを使います。`.venv`がない場合だけ、
依存packageを導入済みのactivateされた互換PythonとしてPATH上の`python`へ
fallbackします。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

テストの構成と境界は[バックエンドテストREADME](tests/backend/README.md)を
参照してください。

## 注意

- 予想分析は、保存された対象者の発言だけを根拠にします。Web検索、現在相場、Codexの一般知識では補強しません。
- 実際の全文文字起こし、音声、埋め込み、SQLiteデータベース、runtime log、cache、資格情報はリポジトリ外に置き、commitしません。
- 分析結果は投資助言ではありません。
- 通常testは実YouTube、実Credential Manager、実Task Schedulerへ接続しません。明示opt-inの実YouTube smokeは2026-08-22 JSTに成功しましたが、通常suiteでは引き続き理由付きでskipします。
- 音声取得、字幕・全文文字起こし、本人声確認、予想分析のcollection連動、実HTTP server/socket、UI、電源断・disk failure、hostileな同時junction差し替え、未bootstrap fresh machineへのoffline導入、remote公開、完成製品の受け入れは未検証です。

## ローカルAPIのセキュリティ境界

ローカルAPIは `127.0.0.1` だけへbindし、remote bindへのfallbackと
CORS allow-allは設けません。ただし、loopbackは認証ではありません。
同じPC上の別プロセスやブラウザからの要求は、このMVPの保護境界外です。
実際の全文文字起こし、音声、データベースは非公開のローカルデータとして
扱い、リポジトリへcommitしません。

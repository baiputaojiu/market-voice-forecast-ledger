# バックエンドテスト

バックエンドは合成データと一時SQLiteデータベースで検証します。通常suiteの
YouTube収集E2Eもfake credential、transport、scheduler、clock、sleeperだけを使い、実際の
YouTube取得、Windows Credential Manager、Windows Task Scheduler、音声、
Codex/model/tool呼び出し、HTTP server、socketは使いません。
API試験はprocess内のTestClientを使い、process終了を伴う試験は
`test_process_crash_recovery.py`の限定されたcrash subprocessだけです。

## Windowsセットアップ

Python 3.11以上を用意し、リポジトリのルートで実行します。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## 実行入口

バックエンドだけを直接実行します。

```powershell
.venv\Scripts\python -m pytest tests/backend -q
```

バックエンド全件、`compileall`、作業状態と状態文書の既存検査、公開安全性、
diff whitespaceを一括検証します。各段階の最初の非0終了コードで停止し、その
終了コードを呼び出し元へ返します。repositoryの
`.venv\Scripts\python.exe`があれば必ずそれを選びます。存在しない場合の
`python` fallbackは、依存packageを導入済みの互換Python環境をすでにactivate
している場合だけを対象にします。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-backend.ps1
```

Windowsでsymlink作成権限がない場合、symlink escapeのcapability test 1件だけが
理由付きでskipされます。

## YouTube収集のfocused testとopt-in smoke

完全合成の4-profile E2E、architecture guard、常時収集されるreal smoke境界は次で
実行します。通常状態では17 passedに加え、real smoke 1件だけが
`real YouTube operational acceptance not requested`の理由でskipされます。

```powershell
python -m pytest tests/backend/e2e/test_youtube_collection_flow.py tests/backend/integration/test_youtube_architecture.py tests/backend/integration/test_youtube_real_smoke.py -q
```

real smokeは通常suiteやdesign acceptanceの必須条件ではありません。ユーザーが
実API callを明示承認し、credential登録を完了した場合だけ、11文字の確認対象
video IDをprocess環境へ設定して次を実行します。repository fileやDBへIDを保存せず、
testは`channels.list`と`videos.list`のenvelope/schemaだけを検査し、provider値を表示しません。

```powershell
$env:MVFL_RUN_YOUTUBE_SMOKE='1'
$env:MVFL_YOUTUBE_SMOKE_VIDEO_ID='abcdefghijk'
python -m pytest tests/backend/integration/test_youtube_real_smoke.py -q
Remove-Item Env:MVFL_RUN_YOUTUBE_SMOKE
Remove-Item Env:MVFL_YOUTUBE_SMOKE_VIDEO_ID
```

明示承認のない実行、音声・字幕・文字起こし・本人声確認・分析、live HTTP
server/socket、UIはこのtestの対象外です。

## ローカル成果物の境界

実際の全文文字起こし、音声、埋め込み、SQLiteデータベース、runtime log、
cache、資格情報はリポジトリ外に置き、決してcommitしません。テストが作る
合成データベースと一時ファイルはpytestの一時directoryだけに置きます。

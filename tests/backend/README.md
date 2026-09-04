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

## 本人声確認の real runtime smoke（未実行・明示 opt-in）

`test_presence_real_smoke.py` は通常suiteでは必ず
`real presence voice smoke not requested` として skip されます。収集時とskip時には
private runtime、model、native executable、network、YouTube、credential、CLI、DB、audioを
初期化または実行しません。

Task 11のStep 3からStep 7は、ユーザーが実行内容を理解したうえで明示承認するまで、
実施も準備もしてはいけません。このREADMEはその承認を記録するものではなく、現時点で
実行済みであるとも主張しません。

承認後だけ、private data rootの外へruntime、model、cache、audio、DB、logを置かず、
Step 3でrepository Pythonを使って`Settings.voice_runtime_dir`のisolated runtimeを作成し、
current project wheelを`--no-deps`でinstallしてから、private wheel directoryを使う
`--no-index --find-links ... --require-hashes`でpinned sherpa wheelをinstallします。yt-dlp、
Deno、FFmpeg、candidate modelとruntime lockはprivate rootだけへ配置し、必要なSHA-256と
CPU providerを検証してruntime lockを完成させてください。lock完成前にsmokeを実行しては
いけません。

runtime attestationだけを実行する正確なopt-in手順は次です。値はprocess環境だけに設定し、
repository file、DB、logへ保存しません。

```powershell
$env:MVFL_REAL_VOICE_SMOKE_DATA_DIR='C:\absolute\private\MarketVoiceForecastLedger'
$env:MVFL_RUN_REAL_VOICE_SMOKE='1'
python -m pytest tests/backend/integration/test_presence_real_smoke.py -q -rs
Remove-Item Env:MVFL_RUN_REAL_VOICE_SMOKE
Remove-Item Env:MVFL_REAL_VOICE_SMOKE_DATA_DIR
```

このsmokeはprivate runtime lockと固定version probeだけをattestし、private path/hash、
provider本文、audio、embeddingを表示しません。Step 4以降（公開clip調査、ユーザーによる
試聴と承認、reference approve、二model calibration、20件pilot、review、cleanup/audit）は
別途の明示ユーザー承認後にのみ、Task 11の順序どおりに行います。承認前後を問わず、
runtime lock、model、audio、database、calibration score/report、operator noteをstageまたは
commitしてはいけません。

## VAD v2限定修復の合成検証

`test_presence_repair_*`は、旧contractの20件を実workerの合成adapterで作り、
読み取り専用preview、DBと3 runtime lockのexclusive backup、限定transaction、
同じcandidate順序の20 queued jobへの置換、二重実行拒否を検証する。
途中故障は再接続後の全行fingerprintでrollbackを確認し、commit後の検証失敗では
自動復元しないことを確認する。修復自体はnetworkもworkerも実行しない。
E2E末尾のworker実行は合成データだけを使う互換性試験で、本番修復の受入範囲には含めない。

通常接続のDELETE禁止とSQL所有者の有限検査は修復機能の追加後も維持する。
本番の保存先がOSによって読み替えられる場合も、path containment検査は緩和しない。

## ローカル成果物の境界

実際の全文文字起こし、音声、埋め込み、SQLiteデータベース、runtime log、
cache、資格情報はリポジトリ外に置き、決してcommitしません。テストが作る
合成データベースと一時ファイルはpytestの一時directoryだけに置きます。

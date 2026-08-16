# 相場見通し発言台帳

YouTube上の対象者の発言を収集し、発言だけを根拠に日経平均、TOPIX、S&P 500、XAU/USDの将来見通しを整理・比較するWindows向けローカルWebアプリです。M2中核バックエンドの番号付きTask 1～19とwhole-branch監査Fix A～Gはユーザー受け入れ済みで、ローカル`main`へ統合・再検証済みです。GitHubへのpushとlive remote照合は未実施です。実YouTube・音声・Codex adapterとUIを含む完成版ではなく、投資判断には利用できません。

## 現在の状態

- [最新の引き継ぎ状態](docs/project/status.md)
- [確定要件](docs/project/requirements.md)
- [重要な決定と理由](docs/project/decisions.md)
- [現在の計画](docs/project/plan.md)
- [公開データ方針](docs/project/public-data-policy.md)

Fix D commit `a92bcaac9b592577d1a7f1efe7b1f70326853351` は組織主体の
分析入力を個人話者修正から保護し、Fix E commit
`cb2aaafe2c07fcf282d79a61fdf0e94c81be864f` は公開安全検査を実際の
index blobへ固定しました。Fix F commit
`25136c5048968eb4d81ba59c597b1bdcfd6f8f24` は、明示的に許可したbinary
拡張子以外のNUL含有fileをStaged・WorkingTree両modeで内容非表示のまま
fail-closedにします。Fix G commit
`188617e7bdc31229d161c1efab1d4269b007d67e` (`fix: align public ignore policy`) は、SQLite3 sidecarと派生coverage fileを
`.gitignore`の第一防御にも追加し、scannerの第二防御と整合させます。最新ローカル
検証はbackend 908件中907 passed・既存capability skip 1件、work-state All
209 passed・0 failed、PowerShell 5.1/7.6のScripts各108 passed・0 failed、
working-tree公開安全166ファイルです。branchにupstreamはなく、Fix D～Gで
push・merge・rebaseは行っていません。

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
- 実YouTube・音声・Codex adapter、実HTTP server/socket、UI、電源断・disk failure、hostileな同時junction差し替え、未bootstrap fresh machineへのoffline導入、remote公開、完成製品の受け入れは未検証です。

## ローカルAPIのセキュリティ境界

ローカルAPIは `127.0.0.1` だけへbindし、remote bindへのfallbackと
CORS allow-allは設けません。ただし、loopbackは認証ではありません。
同じPC上の別プロセスやブラウザからの要求は、このMVPの保護境界外です。
実際の全文文字起こし、音声、データベースは非公開のローカルデータとして
扱い、リポジトリへcommitしません。

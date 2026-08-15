# バックエンドテスト

バックエンドは合成データと一時SQLiteデータベースで検証します。実際の
YouTube取得、音声、Codex/model/tool呼び出し、HTTP server、socketは使いません。
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

## ローカル成果物の境界

実際の全文文字起こし、音声、埋め込み、SQLiteデータベース、runtime log、
cache、資格情報はリポジトリ外に置き、決してcommitしません。テストが作る
合成データベースと一時ファイルはpytestの一時directoryだけに置きます。

# 作業状態

最終更新: 2026-08-16 JST

この文書の状態は、このファイルを含むcommitに対応する。SHAは本文へ埋め込まず、Gitから取得する。

## 現在のフェーズ（Current Phase）

M0「複数PC間の作業状態保存・再開基盤」とM1「アプリ設計の完成」は完了。M2中核バックエンドは隔離branchでTask 1～18をcommit済みで、Task 19の合成E2E、子process crash回復、Windows一括検証入口も自動検証まで実装した。Task 19の初回独立reviewで受け入れた5 findingsはREDから修正し、fresh一括検証を通過した。最新treeの独立再reviewはAPPROVE（Critical 0、Important 0、Minor 0）で、現在はpost-review最終検証と限定commit前である。番号付きTasks 1～19の後にもwhole-branch最終監査・統合確認が必要であり、M2全体の完了や次subprojectの開始はまだ承認されていない。

## Git状態（Git State）

- 公開リポジトリ: `https://github.com/baiputaojiu/market-voice-forecast-ledger`
- 実装worktree branch: `feature/m2-core-backend`
- Task 19開始HEAD: `932a97e0cceef6f33dd4812331343e7875e9308d` (`feat: expose loopback forecast ledger api`)
- Task 19 commit: 未作成。独立再reviewはAPPROVE済みで、post-review fresh verification後に承認済みpathだけをcommitする。
- upstream: なし。このTaskではpush・merge・rebaseもlive remote検証も行っていない。
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
- ユーザーが字幕なし39分39秒動画による本格スモールテストを実施し、話者割当と予想分析の実行可能性、CPU処理時間、必要な回復性を確認した。
- スモールテストから得た市場全般表現の推定割当、転換点、条件付き予想、内部データ削除、段階別進捗の方針を確定要件と決定事項へ反映した。
- M1中核データモデルについて、現在値＋追記専用監査ログ、基準日時別scope、変更不能なrun入力、処理状態とcheckpoint、削除境界、受け入れ試験を設計した。
- 分析用の動画日時をYouTube公開日時だけとし、収録日時を保存・推定せず、システム作成日時を分析へ使わない方式へ改定した。
- 指数割当信頼度を `high`、`medium`、`low`、`unresolved` で保存し、Codex自己評価とアプリ規則の低い側を自動採用上限にする方式を確定した。
- 全文文字起こしと正確なCodex入力本文の既定保持期間を作成日から365日とし、30・90・180・365日・無期限を選べる方式を確定した。
- 承認済み設計を `docs/superpowers/specs/2026-08-14-core-data-model-design.md` へ記録した。
- 収集範囲を、木野内栄治・大川智宏は他チャンネル出演を含む、江守哲は固定YouTubeチャンネルIDだけ、暁投資顧問は公式チャンネルだけの組織主体へ改定した。
- 手動URL登録でも主体別チャンネル方針を迂回させず、江守哲の他チャンネル動画は予想分析へ採用しないデータ境界をM1設計へ追加した。
- 江守哲の対象チャンネルを、表示名「江守哲の米国株投資チャンネル」、正本ID `UCVXka7buS_WptsAzSE0LcKg` としてユーザー確認済みにした。
- 暁投資顧問の公式YouTubeチャンネルを、正本ID `UCOfzLmXpI3qmZfV7_Cs1sYA` としてユーザー確認済みにした。
- 主体別チャンネル方針を含むM1中核データモデルspecのユーザーレビューが承認された。
- 承認済みspecを、SQLite基盤、チャンネル適合、話者割当、job、分析run、発言分類、指数割当、現在予想、監査、削除、FastAPI、合成E2Eの12タスクへ分解した詳細実装計画を作成した。
- 12タスク計画の実装前レビューを行い、要件欠落、未承認判断、実行粒度、M1/M2境界の問題を特定した。
- 実装前レビューの16項目を1件ずつユーザー確認し、暁投資顧問の全話者を組織入力にする例外、月第1週、現在見解、見解変更・相違、4分類、重複非排除、MVP音声スコア、時期不明列、複数根拠、音声取得進捗、追記専用DB制約、M1/M2境界、API境界、音声削除範囲、計画粒度を確定した。
- 統合設計案のユーザー承認を受け、要件、決定事項、中核データモデルspec、プロジェクト計画へ書面反映した。
- 書面反映済みM1中核データモデルspecのユーザー承認を受けた。
- 旧12タスク草案を、15個の番号付きmigrationと19個の独立したテスト先行タスクからなるM2中核バックエンド計画へ全面改訂した。
- 改訂済みM2中核バックエンド19タスク計画がユーザー承認され、M1を完了した。
- M2の実行方式として、タスクごとに新しいサブエージェントで実装し、仕様適合レビューとコード品質レビューを行う案1を採用した。
- M2本実装前のフィージビリティ検証として、本番へ流用しないSQLite縦断スパイク方式をユーザーが選択した。
- スパイクの隔離方法、4群の検証シナリオ、必須安全条件、性能計測、結果分類、成果物の設計がユーザー承認された。
- 読み取り専用の事前確認でPython 3.14.6、SQLite 3.50.4、必要な標準ライブラリ、約60GBの空き容量を確認した。
- 書面化したフィージビリティ・スパイクspecがユーザー承認された。
- 38 scenario・7 Taskの詳細実行計画とスモールテスト開始までの全確認事項がユーザー承認された。
- `.worktrees/` を公開対象から除外し、隔離worktree `spike/m2-core-feasibility` を作成した。
- 本番packageへ流用しない合成SQLite模型を実装し、38 scenarioを全件実行して38 passed、0 failed、0 error、0 skippedを確認した。8つの必須安全条件はすべてpassした。
- 400動画、10,000区間、2,000発言、2,500指数割当、4 scopeの合成fixtureで投影・checkpoint・heatmap再生成を計測した。これは実YouTube、音声、Codex CLI、API、UI、process crashの検証ではない。
- フィージビリティ結果から、固定UTC+9のJST実装、故障後の再接続確認、同一公開日時と複数期間slotの追加試験をM2計画変更候補として特定した。
- DB保存・内部比較をUTC、画面・日付指定・相対期間・週境界を固定JST（UTC+9）とし、`ZoneInfo`と`tzdata`を使わない方針がユーザー承認された。
- 中断・失敗時は5～10分相当の作業unitだけを `pending` から先頭実行し、途中出力を採用せず、全upstream unitと後段検証の完了後に最終反映unitが現在予想・ヒートマップ・自身・job状態を原子的に更新する方針がユーザー承認された。
- 同じ公開日時の上昇系・下降系を見解相違、異なる公開日時での反転を見解変更とする規則がユーザー承認された。
- 承認済み差分を `docs/superpowers/specs/2026-08-15-m2-feasibility-corrections-design.md` に書面化した。
- フィージビリティ修正書面のユーザーレビューが承認された。
- `superpowers:writing-plans` で19タスク計画を最終監査し、manifest依存graphと実入力hash束縛、runの追記専用job-attempt、成果物とunit成功の同一transaction、同方向再投稿で消えない見解変更、共通再投影型、Task 14の非公開writer、Task 16の唯一の最終反映・現在review経路、多対多heatmap元予想link、子process crash回復を明文化した。
- 最終監査は設計文書だけを変更した。M2実装コード、実装用worktree、commit、pushは行っていない。
- M2 Task 1～5でpackage・migration、追記専用監査、主体・チャンネル・動画、適合判定、transcript・音声model metadata・話者割当を実装した。
- M2 Task 6～8でjob manifest・checkpoint・再開、cutoff scope・変更不能run snapshot、Codex出力contractを実装した。
- M2 Task 9～12で発言分類と複数根拠、期間、指数割当、時期不明・low・unresolved reviewを実装した。
- M2 Task 13～16で予想と見解相違・変更、原子的な現在行、修正監査・stale化、週・月heatmapを実装した。
- M2 Task 17～18で保持・削除・安全な音声清掃とloopback-only FastAPI境界を実装した。
- M2 Task 19で完全合成の4主体×4資産E2E、公開review経路、crash rollback/recovery、一括検証scriptを実装した。成功状態はrepository/serviceと実際の`promote_completed_run`だけで構築し、非active negative controlの作成に限ってtest helperのparameterized SQLを1回使用する。

## 作業中（In Progress）

- Task 19の初回独立read-only reviewはREQUEST_CHANGES（Critical 0、Important 4、Minor 1）。第2構築SQL、短い根拠proof、privacy sentinel、Python選択、固定clockの全5件を受け入れ、順次REDから修正した。
- 最新treeの独立再reviewはAPPROVE（Critical 0、Important 0、Minor 0）。この状態更新後にfresh一括検証し、承認済みpathだけを限定commitする。
- 実YouTube、実音声、実Codex/model/tool、実server/socket、UIの統合検証は行っていない。

## 未着手（Not Started）

- YouTube収集、音声処理、話者確認の詳細設計。重複判定は行わない。
- Codex分析prompt、JSON Schema、バッチmanifest、集約規則の確定。
- UI例外処理、再試行、監査ログ、テスト戦略の詳細化。
- MVPで固定する音声モデル名・バージョン、具体的な生スコア尺度・閾値値、閾値設定バージョンの初期値、保留話者の手動レビュー手順。
- Tasks 1～19のwhole-branch最終監査・統合確認とユーザー受け入れ。
- 次subproject。候補は収集・音声・Codex adapter・UI・常駐workerで、ユーザーの明示承認までは着手しない。

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
- M1中核データモデルは、データ境界、正常処理、失敗・再開、削除、受け入れ試験の3セクションでユーザー承認を得た。最終版では日付モデルを公開日時だけへ修正した。
- 主体別チャンネル方針の改定後、旧「個人3名は他チャンネルも対象」の残存なし、必須ゲートの記載、架空チャンネルIDなし、決定IDの一意性を検査し、全119テストとworking tree・stagedの公開安全検査に成功した。
- 2026-08-15の書面spec承認後、旧12タスク草案をM2の19タスク・15 migration計画へ置換し、全TaskのFiles・Interfaces・RED・focused/full test・限定commit、spec対応表、完了条件を確認した。Task連番、migration連番、code fence、placeholder、旧実行禁止表示、旧要件の残存を機械検査し、すべて成功した。
- 同じ計画改訂に対する全決定的スイートは119 passed、0 failed。状態文書検査、working treeの公開安全性検査28ファイル、`git diff --check` が成功した。
- M2事前フィージビリティ検証はPython 3.14.6、SQLite 3.50.4、固定seed `20260815` で38/38 scenario成功、8/8必須安全条件passだった。模型DBは3,784,704 bytesで、実データ・資格情報を含まない。
- 同検証の故障注入では、完了済み4/8 unitの再利用、hash不一致の非再利用、出力後・成功前故障の非成功扱い、現在結果置換失敗時の旧集合保持を確認した。
- 修正書面承認後のM2計画最終監査では、変更対象8文書、Task 1～19、各Taskの連続Step、migration 0001～0015、39件の一意な決定ID、全code fence、placeholder不在、各TaskのFiles一覧と限定 `git add` 対象の完全一致を機械検査した。
- 同じ最終監査変更に対し、作業状態の全決定的スイートは119 passed、0 failed、working treeの公開安全性検査は32ファイルで成功し、`git diff --check` も成功した。
- 2026-08-16のTask 19最新tree検証でbackend 772件を収集し、771 passed、1 skipped。skipはWindowsでsymlink作成が`OSError`となる環境向けcapability test `test_symlink_escape_is_refused_where_supported`だけだった。
- 合成E2Eは2 passed、crash回復とWindows検証入口のintegration moduleは5 passed、Task 18のAPI/private境界focused suiteは74 passed、E2Eとfixture移行対象4 moduleの合計は97 passedだった。
- `scripts/test-backend.ps1` はexit 0で、backend全件、`compileall`、work-state 119 passed・0 failed、状態文書検査、working-tree公開安全性、`git diff --check`を順番に完走した。
- Task 19の初回独立reviewはCritical 0・Important 4・Minor 1で、全findingを実行可能なREDから修正した。最新treeの独立再reviewはAPPROVE（Critical 0、Important 0、Minor 0）。実際の外部call、server/socket、repository内runtime artifactは発生していない。

### ユーザー報告のスモールテスト結果

次の結果はユーザーが実施・報告したもので、このリポジトリの自動テストでは独立再現していない。

- 字幕なし39分39秒の動画から全音声を取得し、722発話区間を文字起こしした。
- 単純な2群クラスタリングによる話者分離は失敗した。短編から取得した参照声との直接照合では、木野内栄治653区間、聞き手55区間、保留14区間へ割り当てられ、合計722区間と一致した。
- 予測分析には木野内栄治へ割り当てた発言だけを投入した。
- Codexは `gpt-5.6-sol`、reasoning effort `high` の試験条件で、Web検索、現在相場、一般知識による補強、shell、外部ツールを無効化した。イベントログ上の外部ツール呼び出しは0件だった。本番設定は既存決定どおり `max` とする。
- 40分級動画のCPU処理時間は、文字起こし約74分、話者割当約4分、Codex分析約4.5分だった。端末・入力・実装で変動する初期ベースラインであり、性能保証値ではない。
- 例示発言の期待結果は、「8月、10月、12月に株式市場が底入れする可能性」を文脈に基づき日経平均・TOPIXへ推定割当した転換点、「2027年は米国の景気と株価が良い」をS&P 500へ推定割当した上昇とし、関連発言のないXAU/USDは空欄とする。

## 既知の問題（Known Issues）

- `.stitch/DESIGN.md` と `.stitch/metadata.json` の日本語が文字化けしている。
- `.stitch` のHTMLには実在人物名と架空の予想・証拠文が組み合わされ、外部CDN、Google Fonts、外部プロフィール画像も参照されている。
- `analysis-run.html` など一部画面に英語の「Analyst Ledger」が残る。
- `.stitch`生生成物は公開対象外。公開用の画面資料は後続UI設計で別途無害化する必要がある。
- skill-creator公式`quick_validate.py`はローカルPythonにPyYAMLがないため未実行。frontmatter、metadata、必須契約はローカル構造テスト34件とCodex反復評価で検証した。
- CPUのみで一連の処理が動くことはユーザーの端末で確認されたが、同じ入力、実装、測定条件をリポジトリ内で再現する自動性能試験は未整備。
- 現在のFastAPI TestClient依存から、Starletteの`httpx`利用非推奨warningが1件出る。テスト失敗ではなく、後続のdependency更新時に追跡する。
- 現在のWindows環境はsymlink作成権限がなく、symlink escapeのcapability test 1件を理由付きでskipした。
- Task 19は完全合成・process内API試験であり、実YouTube、音声、Codex CLI/model/tool、HTTP server/socket、UIを検証していない。

## 未解決事項（Open Questions）

- Windowsネイティブ音声処理とWSL2アダプターの最終選択。M2の音声技術検証で決める。
- 公開用の画面資料で実在する分析主体名を残すか、合成名だけにするか。誤認防止の観点から合成名を推奨する。
- 固定する音声モデルの製品名・バージョン、対象者・聞き手・保留を分ける具体的な閾値値、手動レビュー手順。M2の音声技術検証で決める。
- 短い根拠の既定上限300 Unicode code pointが、実際の日本語発言で十分かどうか。実装後の合成・手動試験で確認する。

## 次の作業（Next Actions）

1. Task 19のpost-review fresh verificationと限定commitを完了する。
2. `/root` がTasks 1～19のwhole-branch最終監査・統合確認を別工程で行う。
3. 中核バックエンドの受け入れと次subprojectについてユーザー判断を受ける。次工程は自動承認しない。

## 重要ファイル（Important Files）

- `docs/project/requirements.md`: 現在有効な要件の正本。
- `docs/project/decisions.md`: 重要な決定、理由、却下案。
- `docs/project/plan.md`: 現在のマイルストーンと作業順序。
- `docs/project/public-data-policy.md`: 公開・非公開情報の境界。
- `docs/superpowers/specs/2026-08-14-cross-pc-work-state-design.md`: 保存・再開基盤の承認済み設計。
- `docs/superpowers/specs/2026-08-14-core-data-model-design.md`: 書面反映版まで承認済みのM1中核データモデル、処理状態、削除、受け入れ試験設計。
- `docs/superpowers/plans/2026-08-14-core-data-model.md`: M2中核バックエンドTasks 1～19の承認済み詳細計画。
- `tests/backend/e2e/synthetic_fixture.py`: 4主体×4資産の完全合成public-service E2E fixture。
- `tests/backend/README.md`: Windows setup、直接test、一括検証、artifact境界。
- `scripts/test-backend.ps1`: backend・compile・work-state・公開安全性・diffの一括検証入口。
- `docs/superpowers/specs/2026-08-15-m2-feasibility-corrections-design.md`: UTC/JST、unit再実行、再接続、公開日時群の競合に関する承認済み差分設計。
- `docs/superpowers/specs/2026-08-15-m2-core-feasibility-spike-design.md`: 書面までユーザー承認済みのM2事前フィージビリティ・スパイク設計。
- `docs/superpowers/plans/2026-08-15-m2-core-feasibility-spike.md`: 実行済みの38 scenario・7 Task詳細計画。
- `docs/superpowers/reports/2026-08-15-m2-core-feasibility.md`: 38 scenario、性能観測、findings、未検証範囲を記録した検証報告。模型コードは本番へ流用しない。
- `.agents/skills/save-work-state/SKILL.md`: GitHubへ保存する処理契約。
- `.agents/skills/resume-work-state/SKILL.md`: 別PCで再開する処理契約。
- `tests/work-state/run-tests.ps1`: 決定的な全検査の入口。
- `tests/work-state/skill-evaluation.md`: スキル反復評価の固定記録。
- `.stitch/`: 公開しないローカル視覚設計原本。

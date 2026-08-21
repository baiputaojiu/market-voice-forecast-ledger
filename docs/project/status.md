# 作業状態

最終更新: 2026-08-22 JST

この文書の状態は、このファイルを含むcommitに対応する。SHAは本文へ埋め込まず、Gitから取得する。

## 現在のフェーズ（Current Phase）

M0「複数PC間の作業状態保存・再開基盤」、M1「アプリ設計の完成」、M2中核バックエンドは完了済みである。YouTube収集subprojectはTask 1～13の実装・独立review、7件のImportant findingのRED/GREEN修正、完全合成E2E、architecture guardを完了し、squash commit `157f739`として`main`へ統合済みである。公開済みruntime codeはTask Scheduler一覧互換修正`95ff083`に続くscheduler XML正規化修正`5db7dbf`までである。明示opt-inの実YouTube read-only smokeと、その結果を記録した状態文書は`origin/main`へ反映済みである。2026-08-22 06:00 JSTの最初のscheduled workerは予定どおり起動したが、最初のseed unitが`YOUTUBE_PROVIDER_REQUEST_FAILED`で失敗し、後続unitは未実行だった。永続DBは失敗を記録して安全に停止し、candidate・presence decision・source cursorは作成または進行していない。複数日の収集運用、音声・本人声確認・分析、実HTTP server/socket、React UIは未実装または未検証であり、アプリ全体の完成や製品受け入れは主張しない。

## Git状態（Git State）

- 公開リポジトリ: `https://github.com/baiputaojiu/market-voice-forecast-ledger`
- `origin/main`の公開済みruntime base: `5db7dbf580464674dbc6b11dc74cd055978d48d4` (`fix: normalize YouTube scheduler XML`)
- local `main`: 状態文書を含めてlive `origin/main`へ反映する。現在SHAとahead/behindはGit検査scriptを正本とする。
- YouTube収集squash統合: `157f739` (`feat: add durable YouTube collection pipeline (#1)`)
- 追加修正source commit: `9adef31e3cde2000e9183a6b080a6d189c0b12a8` (`fix: normalize YouTube scheduler XML`)。local `main`へ履歴追跡付きの`5db7dbf`として取り込み、pushとlive SHA確認後にlocal/remote feature branchとworktreeを削除した。
- Task 19 commit: `3267968d67a70ecee0b6f68e13d241a73e7b634f` (`test: verify synthetic core backend flow`)
- whole-branch Fix A: `9ba560c4db1a795479d831198f04cc3aa5b496f4` (`fix: prevent superseded analysis promotion`)
- whole-branch Fix B: `55ccd07c680bbdaa0e532194305f776e04102f0f` (`fix: harden append-only audit boundaries`)
- whole-branch Fix C: `772e19dc502a9c2f39a121519e78fabac0b3422a` (`test: finalize core backend verification`)
- whole-branch Fix D: `a92bcaac9b592577d1a7f1efe7b1f70326853351` (`fix: preserve organization analysis input`)
- whole-branch Fix E: `cb2aaafe2c07fcf282d79a61fdf0e94c81be864f` (`fix: verify staged public artifacts`)
- whole-branch Fix F: `25136c5048968eb4d81ba59c597b1bdcfd6f8f24` (`fix: reject disguised binary public artifacts`)
- whole-branch Fix G: `188617e7bdc31229d161c1efab1d4269b007d67e` (`fix: align public ignore policy`)。
- M2統合: `feature/m2-core-backend`をローカル`main`へfast-forward統合後、統合済みworktreeとbranchを通常削除した。
- YouTube収集subprojectと追加scheduler修正は`main`へ統合・push済みで、PRを作らずlive remote SHA一致確認後にfeature branch/worktreeをcleanupした。
- ローカル環境: main直下の`.venv`はGit除外対象で、既存offline `setuptools 83`から再構築した。収集データや秘密情報を含まず、push対象ではない。
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
- whole-branch Fix Aでscope generation、superseded run promotion拒否、話者・channel修正の完全なstale伝播を追加した。
- whole-branch Fix Bでmigration 0017、追記専用logical identityのplain-connection collision guard、audit reason/private-data境界、Codex tool-call件数の厳密な整数型検査を追加した。
- whole-branch Fix Cでno-upstream・detached HEADの作業状態script、Task 1/5の永続characterization、18 migrationのoffline wheel回帰、as-built文書を追加・修正した。
- whole-branch Fix Dで、組織所有動画のHOLD・INTERVIEWER・manual SUBJECT segmentに対する個人話者修正をmutation前に拒否し、組織の公式segment集合を分析入力として保持した。
- whole-branch Fix Eで、staged公開安全検査をNUL-safeなpath列挙と実index blobのbinary-safe読取へ変更し、lookup・read・decode失敗を内容非表示でfail-closedにした。公開方針と`.gitignore`の禁止sidecar・cache・editor/OS artifactもforce-stage時に拒否する。
- whole-branch Fix Fで、明示的なbinary拡張子allowlist以外のdecoded fileがNULを含む場合、Staged・WorkingTree両modeで内容を表示せずfail-closedにした。許可済みbinaryと通常のUTF-8/UTF-16 textの境界は維持した。
- whole-branch Fix Gで、SQLite3 sidecarの`*.sqlite3-*`と派生coverage fileの`.coverage.*`を`.gitignore`へ追加し、公開方針の第一防御を変更不要のscanner第二防御と整合させた。
- YouTube収集Task 1～12は、旧organization/fixed-channel runtimeのclean cutover、4人のversioned DiscoveryProfile、Credential Manager、safe YouTube client、seed/search/manual発見、canonical metadata、sealed job/cursor、quota/crash回復、loopback API、Task Scheduler/CLIを実装し、各taskの独立reviewを通過した。
- Task 13の完全合成worker flowは、4 person subjects、4 profile versions、seed/search設定、同一video 1行と人物別candidate、複数observation、各candidateに初期`presence_unverified` decisionがexact 1件だけ、7-source cursor map、全job/job-unit inventoryがYouTube sync 1 job・7 unitsだけ、collectionによるtranscript/speaker/analysis row 0件であることを一時SQLiteで確認する。
- 最終fix candidateのfocused E2E・architecture・smoke境界は17 passed・real smoke opt-in skip 1件だった。全backendは1732件中1730 passed・既存Windows symlink capability skip 1件・同real smoke skip 1件、work-state Allは242 passed・0 failedだった。compileall、state-doc、WorkingTree公開安全206ファイル、diff check、backend wrapperも成功した。
- Task 13最終commitを含むYouTube収集branchを`157f739`へsquashし、`main`へ統合・pushした。続く一覧互換修正`95ff083`も`main`と`origin/main`へ反映済みである。
- 開発端末のWindows Credential Managerは`configured`、Task Schedulerは`installed 06:00`であることを2026-08-22 JSTに読み取り専用で再確認した。API key本文、task実行、実YouTube通信はこの確認で行っていない。
- scheduler XML正規化修正をsource commit `9adef31`からlocal `main`の`5db7dbf`へ履歴追跡付きで取り込み、`origin/main`へ通常pushした。live remote SHA一致を確認後、local/remote feature branchと`.worktrees/youtube-scheduler-xml`を削除した。
- ユーザーの明示承認後、YouTube公開検索と公式oEmbedで確認した11文字video IDをprocess環境だけに設定し、Credential Managerと実YouTube Data APIを使うread-only smokeを実行した。`channels.list`・`videos.list`のschema検証を含む3 testsが成功し、API key・provider本文を表示せず、終了時に両環境変数を削除した。video IDはrepository file・DBへ保存していない。
- 2026-08-22 06:00:01 JSTの最初のscheduled workerを読み取り専用で監査した。Task Schedulerは終了コード0、次回2026-08-23 06:00、missed run 0だった。JST日次requestと4 profile・7 unitのmanifestは作成されたが、最初のseed unitは`channels.list`と`playlistItems.list`のquota予約後に`YOUTUBE_PROVIDER_REQUEST_FAILED`で失敗した。jobは`failed`、後続6 unitは`pending`、全7 checkpointは未完了、observation・candidate・presence decision・proposed/current source cursorは0件であり、不完全な収集結果は昇格していない。

## 作業中（In Progress）

- 最初の06:00 scheduled workerで発生した`YOUTUBE_PROVIDER_REQUEST_FAILED`を、secret・provider本文を露出させずに診断し、明示承認後に同じdurable jobを安全に再試行する。

## 未着手（Not Started）

- 複数日・複数profileでの実YouTube収集網羅性と長期運用受入。
- 音声取得、音声処理、本人声確認の詳細設計。collectionはこれらのjobを自動生成しない。
- Codex分析prompt、JSON Schema、バッチmanifest、集約規則の確定。
- UI例外処理、再試行、監査ログ、テスト戦略の詳細化。
- MVPで固定する音声モデル名・バージョン、具体的な生スコア尺度・閾値値、閾値設定バージョンの初期値、保留話者の手動レビュー手順。
- 次subproject。候補は音声・本人声確認、Codex adapter、UIで、ユーザーの明示承認までは着手しない。

## 検証結果（Verification Results）

- scheduler XML追加修正source `9adef31`・統合`5db7dbf`: 関連scheduler・CLI・API 216 passed。全backendは1747件中1745 passed、既存Windows symlink capability skip 1件、明示opt-in real smoke skip 1件、failure 0。compileall、diff check、WorkingTree公開安全206ファイルが成功し、実機statusは`installed 06:00`だった。`main` push後のlive remote SHA一致とlocal/remote feature branch・worktree削除も確認した。
- 実YouTube read-only smoke: Credential statusは`configured`。公開video IDをprocess環境だけに設定したopt-in実行は3 passed、exit 0だった。`channels.list`と`videos.list`のresponse shapeを検証し、secret・provider値は出力されず、実行後にenv 2件が不存在であることを確認した。
- 最初の06:00 scheduled worker: Task Schedulerは2026-08-22 06:00:01 JSTに実行、終了コード0、次回06:00、missed run 0。DB更新は06:00:04 JSTで、日次job 1件・manifest profile 4件・unit 7件を確認した。quota reservationは`channels_list` 1件と`playlist_items_list` 1件、最初のseed unitだけが`YOUTUBE_PROVIDER_REQUEST_FAILED`で失敗し、残り6 unitは未実行だった。checkpoint完了0、search window完了0、observation/candidate/presence decision/cursorはいずれも0で、部分結果の昇格はなかった。
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
- whole-branch Fix Aはbackend 782 passed・1 capability skip、work-state 119 passed・0 failedで、独立rereviewはCritical 0・Important 0・Minor 0だった。
- whole-branch Fix Bはbackend 889件を収集し、888 passed・1 capability skip、work-state 119 passed・0 failedだった。独立最終rereviewはCritical 0・Important 0・Minor 0だった。
- as-built migration manifestは18ファイル: `0001_foundation`, `0002_audit`, `0003_sources`, `0004_speakers`, `0005_jobs`, `0006_analysis_runs`, `0007_analysis_outputs`, `0008_statements`, `0009_periods`, `0010_asset_mappings`, `0011_mapping_reviews`, `0012_forecast_projections`, `0013_current_results`, `0013_video_pipeline_bindings`, `0014_heatmap`, `0015_retention`, `0016_scope_generations`, `0017_append_only_guards`。
- whole-branch Fix Cはbackend 898件を収集し、897 passed・1 capability skipだった。skipは既存のWindows symlink作成権限向けtestだけで、既存のStarlette TestClient deprecation warning以外のwarningはなかった。基盤・話者focusedは22 passed、work-state Allは135 passed・0 failed、compileall、working-tree公開安全166ファイル、`git diff --check`が成功した。
- Fix Cのwheel回帰はdev bootstrap後、`PIP_NO_INDEX=1`、`PIP_DISABLE_PIP_VERSION_CHECK=1`、`--no-build-isolation --no-deps`で成功し、18 migrationのarchive名・適用順・ledger順、Task 2 audit列・trigger・raw UPDATE/DELETEの`APPEND_ONLY`をwheel-only childで確認した。fresh machineの未bootstrap offline installは検証していない。
- Fix Cの凍結非文書treeに対する独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）で、PowerShell 5.1と7.6のno-upstream動作も独立確認された。
- Fix D commit `a92bcaac9b592577d1a7f1efe7b1f70326853351` の4-path scopeはcorrection serviceと3 integration testだけで、独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）だった。backendは908件中907 passed・既存capability skip 1件、影響suiteは320 passed、work-state Allは135 passed・0 failedだった。
- Fix Eの非文書scopeは`check-public-safety.ps1`と`run-tests.ps1`の2-pathだけである。初回reviewのSQLite sidecar/cache parity findingとrereviewの`*.sqlite3-*` findingを各REDから順に修正し、最終独立read-only rereviewはAPPROVE（Critical 0、Important 0、Minor 0）だった。
- Fix E最終treeのrepository一括検証はbackend 908件中907 passed・既存Windows symlink capability skip 1件、work-state All 181 passed・0 failed、working-tree公開安全166ファイル、compileall、`git diff --check`が成功した。PowerShell 5.1/7.6のPublicSafetyは各46 passed・0 failed、Scriptsは80 passed・0 failedだった。
- Fix FはFix E commit `cb2aaafe2c07fcf282d79a61fdf0e94c81be864f` のclean treeから開始した。NUL含有textのStaged・WorkingTree各secret/safe fixtureはscanner変更前に60 passed・8 failedのREDとなり、最小の対称修正後はPowerShell 5.1/7.6のPublicSafetyが各68 passed・0 failedとなった。
- Fix Fの非文書scopeは`check-public-safety.ps1`と`run-tests.ps1`の2-pathだけで、凍結treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）だった。Scriptsは102 passed・0 failed、work-state Allは203 passed・0 failedだった。
- Fix F treeのrepository一括検証はbackend 908件中907 passed・既存Windows symlink capability skip 1件、work-state All 203 passed・0 failed、working-tree公開安全166ファイル、compileall、`git diff --check`が成功した。
- dev bootstrap後のoffline wheel回帰はFix F treeでもindexを無効化して成功し、wheel内の正確な18 migration名・適用順・ledger順、Task 2 audit列・trigger、raw UPDATE/DELETEの`APPEND_ONLY`を確認した。未bootstrap fresh machineへのoffline dependency導入は証明していない。
- Fix GはFix F commit `25136c5048968eb4d81ba59c597b1bdcfd6f8f24` のclean treeから開始した。実Gitの`check-ignore -v --no-index`を使うtestは変更前に106 passed・2 failedとなり、失敗は`ledger.sqlite3-wal`と`.coverage.synthetic`だけだった。既存の`*.sqlite3`、`*.sqlite-*`、`*.db-*`、`.coverage` controlはRED時点から成功した。
- `.gitignore`へ上記2 patternだけを追加した後、PowerShell 5.1/7.6のScriptsは各108 passed・0 failed、work-state Allは209 passed・0 failedとなった。凍結2-path非文書treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）だった。
- Fix G treeのrepository一括検証はbackend 908件中907 passed・既存Windows symlink capability skip 1件、work-state All 209 passed・0 failed、working-tree公開安全166ファイル、compileall、`git diff --check`が成功した。dev bootstrap後のindex無効offline wheel回帰も1 passed・0 failedで、正確な18 migrationとaudit guardを再確認した。未bootstrap fresh machineへのoffline dependency導入は証明していない。
- Task 13のexact three-module REDは11件を収集し、3 failed・7 passed・1 skippedだった。失敗は未実装のsynthetic worker fixtureと2つのarchitecture scannerだけで、smoke skipは`real YouTube operational acceptance not requested`だった。
- 初回最小実装後の同focused suiteは10 passed・同じopt-in skip 1件でexit 0となった。最終review fix waveでは許可されたproduction 4 paths、reset-only `0018`、focused test 3 pathsを追加変更し、歴史migration `0001`～`0017`は変更していない。最新focused suiteは17 passed・同skip 1件で、全verification後の限定reviewは再凍結treeに対して実施する。

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
- Task 13のreal smokeは常時収集され、明示opt-inがない通常実行では`real YouTube operational acceptance not requested`としてskipする。明示opt-inの単発read-only smokeは成功済みだが、複数日の収集運用受入は未完了である。
- YouTube collectionは音声、字幕、全文文字起こし、本人声判定、speaker assignment、予想分析を実行しない。それらのcollection連動acceptanceは後続subprojectである。
- 電源断・disk failure、hostileな同時junction差し替え、未bootstrap fresh machineへのoffline installation、remote publication、完成製品の受け入れは検証していない。

## 未解決事項（Open Questions）

- Windowsネイティブ音声処理とWSL2アダプターの最終選択。M2の音声技術検証で決める。
- 公開用の画面資料で実在する分析主体名を残すか、合成名だけにするか。誤認防止の観点から合成名を推奨する。
- 固定する音声モデルの製品名・バージョン、対象者・聞き手・保留を分ける具体的な閾値値、手動レビュー手順。M2の音声技術検証で決める。
- 短い根拠の既定上限300 Unicode code pointが、実際の日本語発言で十分かどうか。実装後の合成・手動試験で確認する。

## 次の作業（Next Actions）

1. 最初の06:00 workerについてjob、quota、checkpoint、cursor、candidate、`presence_unverified`停止境界を確認する。
2. 音声・本人声確認、Codex adapter、UIのどのsubprojectを次に設計するか、ユーザー承認で決定する。

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
- `docs/superpowers/specs/2026-08-18-youtube-collection-design.md`: 承認済みの4-person YouTube収集、credential、scheduler、durable orchestration設計。
- `docs/superpowers/plans/2026-08-18-youtube-collection.md`: YouTube収集Tasks 1～13の承認済み詳細計画と観測済み実行状態。
- `tests/backend/e2e/test_youtube_collection_flow.py`: real migration/repository/service/workerを注入fakeで通す4-profile collection E2E。
- `tests/backend/integration/test_youtube_architecture.py`: final schema、legacy symbol、subject分岐、network/Windows/DB責務のarchitecture guard。
- `tests/backend/integration/test_youtube_real_smoke.py`: 明示opt-inだけで実行するread-only real provider smoke境界。
- `docs/superpowers/specs/2026-08-15-m2-feasibility-corrections-design.md`: UTC/JST、unit再実行、再接続、公開日時群の競合に関する承認済み差分設計。
- `docs/superpowers/specs/2026-08-15-m2-core-feasibility-spike-design.md`: 書面までユーザー承認済みのM2事前フィージビリティ・スパイク設計。
- `docs/superpowers/plans/2026-08-15-m2-core-feasibility-spike.md`: 実行済みの38 scenario・7 Task詳細計画。
- `docs/superpowers/reports/2026-08-15-m2-core-feasibility.md`: 38 scenario、性能観測、findings、未検証範囲を記録した検証報告。模型コードは本番へ流用しない。
- `.agents/skills/save-work-state/SKILL.md`: GitHubへ保存する処理契約。
- `.agents/skills/resume-work-state/SKILL.md`: 別PCで再開する処理契約。
- `tests/work-state/run-tests.ps1`: 決定的な全検査の入口。
- `tests/work-state/skill-evaluation.md`: スキル反復評価の固定記録。
- `.stitch/`: 公開しないローカル視覚設計原本。

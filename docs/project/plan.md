# 現在の開発計画

## 現在のマイルストーン（Current Milestone）

### YouTube収集subproject: scheduler統合と運用受入

M0、M1、M2中核バックエンドは完了済みである。YouTube収集subprojectは、4人を設定差だけのperson DiscoveryProfileとして扱うclean cutover、Windows credential、read-only client、seed/search/manual discovery、durable job・cursor、loopback API、Task Scheduler/CLI、完全合成E2E、architecture guardをTask 1～13で実装・独立reviewし、squash commit `157f739`として`main`へ統合済みである。公開済みruntime codeは`95ff083`に続くscheduler XML正規化修正`5db7dbf`までで、この文書を含むlocal `main`はsmoke結果の状態文書commit 1件だけlive `origin/main`よりahead、runtime code差分なしである。Credentialはconfigured、日次Taskはinstalled 06:00であり、明示opt-inの実YouTube read-only smokeも成功した。source `9adef31`のlocal/remote feature branchとworktreeは統合・push確認後にcleanupした。複数日の収集運用、音声・本人声確認・分析、live server、UIは受入済みとはしない。

## 完了済み（Completed）

- 分析対象、対象資産、収集範囲、期間、予想区分、情報境界を確定した。
- ローカルWebアプリの全体構成と主要画面方針を承認した。
- 4つの更新型状態文書、短い`AGENTS.md`、公開除外方針を実装した。
- 保存・再開2スキルと4つの決定的検査スクリプトをテスト先行で実装した。
- 保存・再開スキルをbaselineと適用後で各5反復評価した。
- 一時source、bare remote、second cloneによる統合試験を実装した。
- 全決定的スイート119件と統合試験12件が成功した。
- 承認済み26ファイルだけを公開GitHubの`main`へpushし、live remote SHAを確認した。
- GitHubから別cloneし、公開範囲、Git状態、状態文書、公開安全性、119テストを再検証した。
- 字幕なし39分39秒動画を使うユーザー実施のスモールテストで、722区間の文字起こし、参照声による対象者653区間・聞き手55区間・保留14区間の話者割当、対象者発言だけのCodex分析を完了した。
- スモールテスト結果から、参照声との直接照合、市場全般表現の推定割当、転換点、条件付きレイヤー、内部データ削除、チェックポイントと段階別進捗の要件を確定した。
- M1中核データモデルについて、現在値＋監査ログ、基準日時別scope、公開日基準、指数割当信頼度、5～10分checkpoint、365日保持、受け入れ試験の初回設計をユーザーが承認した。
- 初回承認済み設計を `docs/superpowers/specs/2026-08-14-core-data-model-design.md` へ記録した。
- 収集範囲を、木野内栄治・大川智宏は全チャンネルのゲスト出演を含む、江守哲は固定チャンネルIDだけ、暁投資顧問は公式チャンネルだけの組織主体へ改定した。
- 江守哲の対象チャンネルを、表示名「江守哲の米国株投資チャンネル」、正本ID `UCVXka7buS_WptsAzSE0LcKg` として確認した。
- 暁投資顧問の公式YouTubeチャンネルを、正本ID `UCOfzLmXpI3qmZfV7_Cs1sYA` として確認した。
- 主体別チャンネル方針を含むM1中核データモデルspecの初回ユーザーレビューが承認された。
- 初回specを12個のテスト先行タスクへ分解した詳細実装計画ドラフトを `docs/superpowers/plans/2026-08-14-core-data-model.md` に作成した。
- 実装前レビューを行い、暁投資顧問の全話者を組織入力にする例外、月第1週、現在見解選択、4分類、重複非排除、音声スコア、時期不明列、複数根拠、音声取得進捗、DB追記専用制約、M1/M2境界、削除範囲、計画粒度を1件ずつユーザー確認した。
- 実装前レビューの統合設計案がユーザー承認され、要件、決定事項、中核specへ反映した。
- 書面反映済みM1中核データモデルspecがユーザー承認された。
- 旧12タスク草案を、承認済みspecへ対応するM2中核バックエンドの19個の小粒度タスクへ全面改訂した。
- 改訂済みM2中核バックエンド19タスク計画がユーザー承認され、これをもってM1を完了した。
- M2の実行方式として、タスクごとに新しいサブエージェントで実装し、仕様適合レビューとコード品質レビューを行う案1を採用した。
- M2本実装前に、合成データとSQLiteだけを使う使い捨て縦断スパイクを実施する方針をユーザーが承認した。
- スパイクの構成、検証シナリオ、必須安全条件、結果分類、成果物をユーザーが承認した。
- 書面化したフィージビリティ・スパイクspecがユーザー承認された。
- 38 scenario・7 Taskの詳細実行計画とスモールテスト開始までの全確認事項がユーザー承認された。
- 隔離worktree `spike/m2-core-feasibility` で本番packageへ流用しないSQLite模型を実装し、38 scenarioを全件実行して38 passed、0 failed、0 error、0 skippedを確認した。
- 400動画・10,000区間・2,000発言・2,500指数割当・4 scopeの合成fixtureで、8つの必須安全条件、checkpoint、原子置換、追記専用制約、heatmap再生成を確認した。
- フィージビリティ検証から、WindowsのIANA timezone database非依存、rollback後の再接続確認、同一公開日時と複数期間slotの専用試験をM2計画へ追加する必要を特定した。
- DB保存・内部比較はUTC、画面と相対期間・週境界は固定JST（UTC+9）、`ZoneInfo`・`tzdata`不使用とする修正がユーザー承認された。
- 失敗時は5～10分の作業unitだけを先頭から再実行し、途中結果を採用せず、全upstream unitと後段検証の完了後に最終反映unitが現在予想・ヒートマップ・自身・job状態を原子的に更新する修正がユーザー承認された。
- 同じ公開日時の上昇系・下降系は直接性や期間具体性にかかわらず見解相違、公開日時が異なる反転は見解変更とする修正がユーザー承認された。
- 承認済み差分を `docs/superpowers/specs/2026-08-15-m2-feasibility-corrections-design.md` に書面化した。
- フィージビリティ修正書面のユーザーレビューが承認された。
- `superpowers:writing-plans` による19タスクの最終監査を行い、分析jobの依存hash束縛と後継attempt、各永続成果物とunit成功の同時確定、見解変更履歴の保持、Task 14の非公開現在行writer、Task 16の唯一の最終反映・review公開経路、複数元予想link、子process crash試験を計画へ反映した。
- Task連番、Step連番、15 migration、code fence、共有型と前後依存を再検査した。M2実装コード、worktree、commit、pushは作成していない。
- M2 Task 1～5でPython package、15 migration基盤、追記専用監査、主体・チャンネル方針、動画・transcript・固定音声model metadata、話者割当を実装した。
- M2 Task 6～8で決定的job manifest、checkpoint・再開、cutoff scope、変更不能run入力snapshot、Codex構造化出力のfail-closed contractを実装した。
- M2 Task 9～12で4種類の発言分類、複数根拠、期間正規化・時期不明review、指数割当とlow/unresolved reviewを実装した。
- M2 Task 13～16で将来予想、見解相違・変更、原子的な現在行置換、修正監査とstale化、週・月16行heatmap cacheを実装した。
- M2 Task 17～18で保持・削除と安全な音声清掃、loopback-only FastAPIのread/write・private response境界を実装した。
- M2 Task 19の合成4主体×4資産E2E、review・競合、SQLite crash rollback/recovery、Windows一括検証入口を実装し、自動検証を通過させた。
- whole-branch最終監査で、scope generation競合とstale伝播をFix A、追記専用identity・audit/Codex境界をFix B、offline wheel・基盤characterization・no-upstream/detached作業状態をFix Cとして修正した。
- Fix Cの凍結非文書treeは独立read-only reviewでAPPROVEされ、観測済みの検証結果と未検証境界をas-built文書へ反映した。
- Fix D commit `a92bcaac9b592577d1a7f1efe7b1f70326853351` で組織所有動画の公式segment集合を個人話者修正から保護し、4-path treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）となった。
- Fix E commit `cb2aaafe2c07fcf282d79a61fdf0e94c81be864f` (`fix: verify staged public artifacts`) で公開安全検査を実index blobへ固定し、公開方針と`.gitignore`の禁止artifact parityを補完した。2-path非文書treeの最終独立rereviewはAPPROVE（Critical 0、Important 0、Minor 0）となった。
- Fix F commit `25136c5048968eb4d81ba59c597b1bdcfd6f8f24` (`fix: reject disguised binary public artifacts`) で、明示的なbinary拡張子allowlist以外のNUL含有fileをStaged・WorkingTree両modeでfail-closedにした。凍結2-path treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）となった。
- Fix G commit `188617e7bdc31229d161c1efab1d4269b007d67e` (`fix: align public ignore policy`) で、`*.sqlite3-*`と`.coverage.*`を`.gitignore`へ追加し、公開方針の第一防御をscannerの第二防御と整合させた。凍結2-path treeの独立read-only reviewはAPPROVE（Critical 0、Important 0、Minor 0）となった。
- Fix G treeはbackend 908件中907 passed・既存capability skip 1件、work-state All 209 passed・0 failed、PowerShell 5.1/7.6 Scripts各108 passed・0 failed、working-tree公開安全166ファイル、compileall・diffを通過した。dev bootstrap後のoffline wheelから正確な18 migrationとaudit guardも再確認した。
- ユーザーがM2中核バックエンドを受け入れ、統合方法1を選択した。`feature/m2-core-backend` はローカル`main`へfast-forward統合され、統合済みworktreeとbranchを通常削除した。
- 統合後`main`で、無視対象のローカル`.venv`を既存のoffline `setuptools 83`から再構築した。最初に再現したwheel build backend不足を解消後、一括検証はbackend 907 passed・既存capability skip 1件、work-state 209 passed・0 failed、公開安全166ファイルで成功した。追跡ファイルの追加変更はない。
- 実YouTube・音声・Codex adapter、実HTTP server/socket、UI、電源断・disk failure、hostileな同時junction差し替え、未bootstrap fresh machineへのoffline installation、remote publication、完成製品の受け入れはこの監査で証明していない。
- YouTube収集Task 1～12でclean person-model cutover、versioned profile、Credential Manager、safe client、canonical metadata、seed/search/manual discoverer、sealed queue、crash/quota回復、strict loopback API、Task Scheduler/CLIを実装し、各taskの独立reviewを通過した。
- Task 13初回candidateは3 failed・7 passed・1 opt-in skipのREDから、10 passed・同skip 1件のfocused GREENとなった。初回独立review後はledgerless DB非変更、legacy evidence除去、1日window page 11継続、日次quota原子強制、scope-complete architecture guard、E2E全decision/job inventory、constant/no-trace smoke failureを順次RED/GREENで追加した。歴史migration `0001`～`0017`は変更していない。
- 最終fix candidateのfocused suiteは17 passed・real smoke opt-in skip 1件、全backendは1732件中1730 passed・既存Windows symlink capability skip 1件・同real smoke skip 1件、work-state Allは242 passed・0 failedだった。compileall、state-doc、WorkingTree公開安全206ファイル、diff check、backend wrapperも成功した。
- Task 13と全YouTube収集実装を`157f739`へsquashして`main`へ統合・pushし、Task Scheduler一覧互換修正`95ff083`も`main`へ反映した。
- 開発端末でCredentialがconfigured、Task Schedulerがinstalled 06:00であることを秘密値を読み出さず確認した。
- WindowsのTask Schedulerが登録XMLから終了期限のない定期task設定を正規化し、照会時にUTF-16宣言とnative code-page bytesを混在させる挙動を再現した。source `9adef31`でrecurring XML、宣言、既定Enabled、LeastPrivilege、Unified Scheduling Engineをfail-closedに正規化し、関連216 testsと全backend 1747件を検証した後、local `main`へ`5db7dbf`として取り込んだ。通常pushとlive SHA一致確認後にlocal/remote feature branchとworktreeをcleanupした。
- 公開動画をYouTube公開検索と公式oEmbedで確認し、11文字video IDをprocess環境だけに設定した明示opt-in real smokeを実行した。Credential Manager経由の`channels.list`・`videos.list`を含む3 testsが成功し、secret・provider本文を表示せず、envを終了時に削除した。

## 作業中（In Progress）

- 最初の06:00 scheduled workerのdurable job・quota・checkpoint・cursor・candidate停止境界を観測する。

## 未着手（Not Started）

### M2後続・M3以降

次のsubprojectは未承認であり、以下の順序もユーザー判断前には確定しない。

1. 複数日・複数profileでの実YouTube収集網羅性と長期運用受入。
2. 音声取得、固定音声モデル、閾値設定、分割文字起こし、参照声本人確認の詳細specと実装。
3. Codex prompt、JSON Schema、CLI adapter、外部ツール0件検証の詳細specと実装。
4. 指数割当規則、4資産比較ヒートマップ、レビュー・証拠UIの詳細specと実装。
5. 実server/socket、UI、電源断・disk failure、性能・セキュリティの統合検証。

# 現在の開発計画

## 現在のマイルストーン（Current Milestone）

### M2中核バックエンド: Task 19最終確認

M0の作業状態保存・再開基盤とM1のアプリ設計は完了した。M2中核バックエンドは隔離branch `feature/m2-core-backend` でTask 1～18をcommit済みで、Task 19の合成E2E、子process crash回復、Windows一括検証入口も自動検証まで実装した。Task 19の初回独立reviewで受け入れた5 findingsはREDから修正し、fresh一括検証を通過した。最新treeの独立再reviewはAPPROVE（Critical 0、Important 0、Minor 0）で、post-review最終検証と限定commitだけが未完了である。番号付きTaskの完了後にもwhole-branch最終監査・統合確認が別に必要で、次のsubprojectはユーザー承認前に開始しない。

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

## 作業中（In Progress）

- 初回独立reviewのCritical 0・Important 4・Minor 1をすべて受け入れ、各findingを実行可能なREDから順番に修正した。
- 最新treeの独立再reviewはAPPROVE（Critical 0、Important 0、Minor 0）。この状態更新後にfresh一括検証し、approved pathだけを限定commitする。

## 未着手（Not Started）

### M2中核バックエンドの完了確認

1. Task 19のpost-review fresh verificationと限定commitを完了する。
2. `/root` がTasks 1～19のwhole-branch最終監査・統合確認を別工程で行う。
3. 中核バックエンドの受け入れと次subprojectについてユーザー判断を受ける。番号付きTaskの実装だけでM2全体完了とはしない。

### M2後続・M3以降

次のsubprojectは未承認であり、以下の順序もユーザー判断前には確定しない。

1. YouTube検索・網羅性評価・収集・音声取得の詳細specと実装。
2. 固定音声モデル、閾値設定、分割文字起こし、参照声話者割当の詳細specと実装。
3. Codex prompt、JSON Schema、CLI adapter、外部ツール0件検証の詳細specと実装。
4. 指数割当規則、4資産比較ヒートマップ、レビュー・証拠UIの詳細specと実装。
5. Windows常駐worker、性能・回復性・セキュリティ検証。

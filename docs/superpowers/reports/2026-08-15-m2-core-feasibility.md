# M2中核バックエンド事前フィージビリティ検証報告

## 結論

38シナリオを全件実行し、pass 38、fail 0、error 0、skip 0だった。8つの必須安全条件もすべてpassした。

これは合成SQLite模型の成立性であり、実YouTube、実音声、Codex CLI、API/UI、process crashを確認済みとはしない。模型コードは本番へ流用せず、M2本実装は別承認まで開始しない。

## 実行環境

- OS: Windows-11-10.0.26200-SP0
- Python: 3.14.6
- SQLite: 3.50.4
- Git commit（実行開始時点）: `ce64523949c3e1adaac0aab8015b56232f0e3e2e`
- fixture seed: `20260815`
- データ境界: 架空名・架空ID・合成発言のみ。実音声、実文字起こし、認証情報なし。
- 実験branch: `spike/m2-core-feasibility`
- 実験完了commit: `00d9ab5a027f0a868ddada50889743754e78402b`

## 拡大fixture

| 種類 | 件数 |
|---|---:|
| videos | 400 |
| segments | 10,000 |
| statements | 2,000 |
| asset_mappings | 2,500 |
| scopes | 4 |

## 全38シナリオ

| ID | 期待内容 | 結果 |
|---|---|---|
| F-A01 | all-channel個人のゲスト出演を適合候補にする | pass |
| F-A02 | fixed-channelは正本ID完全一致だけを適合にする | pass |
| F-A03 | 手動URLでもfixed-channel不一致を迂回できない | pass |
| F-A04 | チャンネルID未解決をfail-closedにする | pass |
| F-A05 | 722区間をsubject 653・interviewer 55・hold 14に分ける | pass |
| F-A06 | 個人分析入力をsubject 653区間だけにする | pass |
| F-A07 | 個人runへのinterviewer・hold混入を拒否する | pass |
| F-A08 | 組織主体は適合済み公式動画の全区間を入力にする | pass |
| F-B01 | cutoff後公開動画を分析入力から除外する | pass |
| F-B02 | 来週を公開日JST基準の月曜～日曜へ変換する | pass |
| F-B03 | 明示年を公開日基準と分けて保存する | pass |
| F-B04 | 月第1週の月跨ぎを正規化する | pass |
| F-B05 | 未承認の時期不明を採用せず承認後も専用状態に保つ | pass |
| F-B06 | 将来予想だけを主ヒートマップ候補にする | pass |
| F-B07 | 転換点・横ばい・判断不能・発言なしを区別する | pass |
| F-B08 | 市場表現を所定の指数候補へ推定割当する | pass |
| F-B09 | 条件付き予想へ条件文を必須にする | pass |
| F-B10 | Codex評価とアプリ評価の低い側で信頼度を制限する | pass |
| F-B11 | 聞き手だけの市場手掛かりをunresolvedにする | pass |
| F-B12 | 根拠を順序付き原文抜粋として検証する | pass |
| F-C01 | low・unresolvedをreview前に採用しない | pass |
| F-C02 | 追記reviewの最新版を実効状態にする | pass |
| F-C03 | 同一動画内の相反方向を見解相違として保持する | pass |
| F-C04 | 後続動画の反対方向を見解変更として保持する | pass |
| F-C05 | 異なる動画IDの同文再投稿を独立根拠として数える | pass |
| F-C06 | 4主体×4資産と週・月表示を生成する | pass |
| F-C07 | 関連発言のない金をunknownでなく空欄にする | pass |
| F-C08 | heatmap cacheを決定的に再生成する | pass |
| F-C09 | 異なるcutoff scopeを互いに変更せず共存させる | pass |
| F-D01 | run・review・auditの更新と削除を拒否する | pass |
| F-D02 | snapshotは許可された本文NULL化だけを認める | pass |
| F-D03 | 失敗した現在結果置換で旧集合を残す | pass |
| F-D04 | 現在結果と監査eventを同時確定する | pass |
| F-D05 | 完了済み4/8 unitを再利用して5番から再開する | pass |
| F-D06 | hash不一致unitを再利用しない | pass |
| F-D07 | 出力後・成功前の故障を成功扱いしない | pass |
| F-D08 | review requiredをjob failedと区別する | pass |
| F-D09 | 専用音声root外の削除を拒否する | pass |

## 必須安全条件

| # | 条件 | 証拠scenario | 判定 |
|---:|---|---|---|
| 1 | 固定チャンネルを手動URL・表示名で迂回できない | F-A02, F-A03, F-A04 | pass |
| 2 | 個人分析入力へ聞き手・保留が混入しない | F-A06, F-A07 | pass |
| 3 | cutoff後の動画が混入しない | F-B01 | pass |
| 4 | low・unresolved・未承認時期不明を自動採用しない | F-B05, F-C01 | pass |
| 5 | 追記専用記録を更新・削除できない | F-D01, F-D02 | pass |
| 6 | 失敗した現在結果置換を部分反映しない | F-D03, F-D04 | pass |
| 7 | hash不一致checkpointを再利用しない | F-D05, F-D06, F-D07 | pass |
| 8 | heatmapを正本から決定的に再生成できる | F-C08, F-C09 | pass |

## 性能観測

数値はこの模型と端末の観測値であり、本番SLAではない。400動画・10,000区間は3年間の実量予測ではなく、音声・Codex・HTTP・UI性能を含まない。

| 操作 | samples | min ms | median ms | max ms | peak bytes |
|---|---:|---:|---:|---:|---:|
| build_week_month_heatmap | 5 | 23.775 | 24.169 | 28.258 | 173,761 |
| checkpoint_resume | 5 | 0.225 | 0.230 | 0.250 | 8,879 |
| insert_722_segments | 5 | 79.045 | 84.445 | 108.927 | 154,789 |
| insert_scale_fixture | 5 | 974.505 | 1,027.440 | 1,101.470 | 154,701 |
| project_four_scopes | 5 | 256.917 | 321.825 | 401.574 | 913,410 |
| rebuild_heatmap | 5 | 35.123 | 36.334 | 39.540 | 207,564 |
| select_653_subject_segments | 5 | 2.075 | 2.272 | 4.723 | 57,857 |

- SQLite DB本体サイズ: 3,784,704 bytes

## 成立を確認した事項

### CF-01 チャンネル・話者・主体別入力境界

F-A01～F-A08で、全チャンネル個人、固定チャンネル、手動URL非迂回、個人本人区間、暁投資顧問の組織入力を合成データで表現できた。本番では意味規則を制約・service testへ移し、模型コードは流用しない。

### CF-02 発言分類・期間・推定割当・原文根拠の分離

F-B01～F-B12で、公開日基準、4分類、時期不明、信頼度下限、根拠検証を別の状態として扱えた。期間の由来、推定理由、Codex評価、アプリ評価は本番でも分離する。

### CF-03 reviewから16行heatmapまでの再投影

F-C01～F-C09で、review event、現在見解投影、cache削除・再生成が成立した。現在値を正本にせず、発言・review・scopeから再投影可能に保つ。

### CF-04 追記専用・原子置換・checkpoint・削除境界

F-D01～F-D09で、SQLite triggerとtransactionへの故障注入を含む組合せが成立した。本番でも結果集合と監査eventを同一transactionで確定する。

## M2計画変更として承認された事項

### PC-01 固定JSTでWindowsのIANA timezone databaseへ依存しない

このWindows環境ではTZPATHが空で、`ZoneInfo("Asia/Tokyo")` が `ZoneInfoNotFoundError` になった。DB保存・内部比較はUTC、画面・日付指定・相対期間・週境界は固定JST（UTC+9）とし、`ZoneInfo`と`tzdata`を使用しない。M2 Task 1と10へWindows回帰試験を追加する。

### PC-02 rollback後は新しいconnectionで永続状態を確認する

同一connection照合だけでなく、故障後にconnectionを閉じて開き直し、旧集合と再試行対象unitが永続していることを確認する。M2 Task 6と14へ追加する。Python例外rollbackとprocess crashは別試験とする。

### PC-03 公開日時群と表示slotの専用試験を追加する

同じ公開日時の上昇系・下降系は、動画・直接性・期間具体性にかかわらず見解相違とする。公開日時が異なる群での上昇・下降反転は見解変更とする。複数期間と条件layerが同じ週・月slotへ重なる場合も専用試験を追加する。M2 Task 13と16へ反映する。

### PC-04 作業unitを先頭から再実行する

中断・失敗した5～10分相当のunitだけを `pending` へ戻して先頭から実行し、途中出力は正式成果物にしない。入力・成果物hashとモデル・設定・契約versionが一致する `success` unitだけを再利用する。最終反映unit以外の全upstream unitと後段検証が完了した後、最終反映unitが現在予想、ヒートマップ、自身の `success`、jobの `succeeded` を原子的に更新する。

## 模型で確認していないこと

- 実YouTubeの網羅検索、公開日時・チャンネルID取得の安定性。
- 実音声の文字起こし、話者照合精度、閾値付近の保留率。
- Codex CLI最上位モデルの構造化出力、no-tool enforcement、長文入力限界。
- FastAPI loopback境界、進捗バー、heatmapと証拠drawerのUI。
- process強制終了・電源断・ディスク障害からのSQLite/WAL回復。

## 結論と次のゲート

必須安全条件の不合格に起因するM1データモデルの全面変更は検出されなかった。承認済みの差分修正は `docs/superpowers/specs/2026-08-15-m2-feasibility-corrections-design.md`、正本要件・決定事項、M1 spec、M2計画へ反映済みである。

修正文書のレビュー完了後も、M2本実装開始にはユーザーの別の明示承認を必要とする。

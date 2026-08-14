# M2中核バックエンド事前フィージビリティ・スパイク設計

## 状態

- 設計承認日: 2026-08-15 JST
- 書面spec承認日: 2026-08-15 JST
- 対象: 承認済みM2中核バックエンド計画の技術的不確実性
- 種別: 本実装へ流用しない、合成データだけの使い捨て検証
- 次のゲート: 38 scenario・7 Task詳細実行計画のユーザーレビュー
- 本実装状態: 未着手

## 目的

承認済みM1データモデルとM2中核バックエンド19タスク計画について、本実装前に小さなSQLite模型を動かし、次を確認する。

1. 主要なデータ境界と安全条件をSQLiteと小さなドメイン規則で表現できるか。
2. チャンネル判定から16行ヒートマップまでの縦断データフローが成立するか。
3. 原子的更新、追記専用監査、レビューゲート、checkpoint再開が想定どおり組み合わせられるか。
4. 722区間規模と拡大合成データで、明らかな性能・データ量上の問題がないか。
5. M2開始前に修正すべき設計、計画、受け入れ条件を具体化できるか。

このスパイクの目的は、M2本番コードを先行実装することではない。模型で確認できない事柄を確認済みと扱わず、結果の適用はユーザー承認後に行う。

## 採用方式と比較案

### 採用: SQLite縦断スパイク

Python標準ライブラリとSQLiteだけで、合成出典、話者境界、分析結果、指数割当、レビュー、現在予想、ヒートマップ、job、監査を最小限のschemaと規則へ写し、正常系と故障系を通す。

採用理由は、SQL制約だけでなくサービス境界と集計まで確認できる一方、FastAPIや本番packageを作らずに実装範囲を抑えられるためである。

### 不採用: SQL制約だけの検証

短時間で実行できるが、チャンネル・話者・レビュー規則、現在見解選択、ヒートマップ再生成、checkpoint再開の組合せを検証できない。

### 不採用: FastAPIまで含むミニアプリ

HTTP境界も確認できるが、本実装に近づきすぎ、使い捨て検証として大きい。FastAPI境界はM2 Task 18のテスト先行実装で確認する。

## 隔離と成果物の境界

- 実行時は `spike/m2-core-feasibility` branchの隔離worktreeを使う。
- `.worktrees/` が未作成かつ未除外であるため、worktree作成前に `.gitignore` へ追加し、その変更だけを明示的にcommitする。
- 使い捨てコードは `experiments/m2-core-feasibility/` 配下だけに置く。
- 本番予定の `src/market_voice_forecast_ledger/` と `tests/backend/` は作成・変更しない。
- Python標準ライブラリだけを使う。主に `sqlite3`、`unittest`、`tempfile`、`hashlib`、`json`、`time`、`tracemalloc`、`datetime`、`zoneinfo`、`pathlib`、`dataclasses`、`statistics`、`argparse`、`platform`、`subprocess` を使い、依存packageを追加しない。
- SQLite DBと生成JSONはOSの一時ディレクトリへ置き、リポジトリへcommitしない。
- 実在人物の発言、実YouTubeメタデータ、音声、全文文字起こし、話者特徴量、認証情報を使わない。
- 架空名、架空チャンネルID、合成発言だけをfixtureに使う。
- 実験branchは結果レビューまで保持し、自動削除しない。
- mainへ残す候補は、この設計書、承認済み実行計画、検証報告書、ユーザー承認済みのM1/M2文書修正だけとする。

## 実験ファイル

隔離worktreeに次を作る。

- `experiments/m2-core-feasibility/README.md`: 実行方法、非目標、合成データ境界。
- `experiments/m2-core-feasibility/schema.sql`: 模型に必要な最小SQLite schemaとtrigger。
- `experiments/m2-core-feasibility/spike.py`: fixture作成、規則、transaction、集計、計測。
- `experiments/m2-core-feasibility/test_spike.py`: 正常系、拒否系、故障注入、再生成の決定的テスト。

検証完了後にmainへ残す報告書候補は `docs/superpowers/reports/2026-08-15-m2-core-feasibility.md` とする。報告書を追加するか、どの修正を正本へ反映するかは結果提示後にユーザー承認を受ける。

## 模型の責務

模型は本番15 migrationを先行実装しない。次の不確実性を検証するのに必要な列、外部キー、一意制約、CHECK、triggerだけを持つ。

1. 主体とチャンネル方針。
2. 動画、主体別適合判定、公開日時。
3. 発話区間、話者割当、主体別入力抽出。
4. cutoff別scope、run、入力snapshot。
5. 発言分類、期間、原文根拠。
6. 指数割当、規則信頼度、review event。
7. 現在予想、発言link、再生成可能なheatmap cell。
8. job、job unit、成果物hash、状態。
9. 追記専用audit event。

模型のtable名や関数をM2本番interfaceの正本にしない。検証で成立した意味規則だけをM1 specとM2計画へ戻す。

## 合成fixture

### 境界fixture

- 個人主体3件と組織主体1件を作る。
- all-channel個人2件、fixed-channel個人1件、fixed-channel組織1件を作る。
- fixed-channel用IDはYouTube形式に似せた架空値とし、実在IDを使わない。
- 個人主体の722区間を `subject = 653`、`interviewer = 55`、`hold = 14` へ割り当てる。
- 組織主体では適合済み公式チャンネル動画の全区間を組織入力にする。
- 固定チャンネル一致、不一致、未解決、手動URL登録の動画を作る。
- cutoff前後、同一公開日時、後続公開日時の動画を作る。
- 将来予想、現状分析、過去結果分析、一般論、条件付き、転換点、横ばい、判断不能、時期不明を含む合成発言を作る。
- 日経平均、TOPIX、S&P 500相当への直接・推定割当を作り、XAU/USD相当には関連発言を作らないケースを含める。
- 元動画、切り抜き、Shorts、再投稿に相当する異なる動画IDへ同内容の合成発言を置く。

### 拡大fixture

性能傾向確認用に、400動画、10,000発話区間、2,000発言、2,500指数割当、4分析scopeを決定的seedから作る。これは3年間の実データ量予測ではなく、模型がデータ増加時に異常な処理時間またはDB増加を示さないかを見る固定入力である。

## 検証シナリオ

### A. チャンネル・話者・入力境界

- `F-A01`: all-channel個人の他チャンネル動画を適合候補にできる。
- `F-A02`: fixed-channel個人は正本ID完全一致だけを適合にする。
- `F-A03`: fixed-channel不一致は手動URLでも覆せない。
- `F-A04`: チャンネルID未解決は表示名一致でも拒否する。
- `F-A05`: 個人722区間の内訳が653、55、14で合計722になる。
- `F-A06`: 個人の分析入力がsubject 653区間だけになる。
- `F-A07`: interviewerまたはholdを含む個人runを拒否する。
- `F-A08`: 組織主体は適合済み固定チャンネルの全区間を入力にする。

### B. 分析結果・期間・指数割当

- `F-B01`: cutoff後に公開された動画を取得済みでも除外する。
- `F-B02`: 「来週」を公開日JST基準の月曜～日曜へ変換する。
- `F-B03`: 明示年月を `explicit_statement` として公開日基準から分ける。
- `F-B04`: 2026年9月第1週を2026-08-31～2026-09-06へ変換する。
- `F-B05`: 時期不明を未承認では採用せず、承認後も通常期間へ変換しない。
- `F-B06`: 4発言種別のうち将来予想だけを主ヒートマップ候補にする。
- `F-B07`: 転換点、横ばい、判断不能、関連発言なしを別状態にする。
- `F-B08`: 日本株相当を2指数、米国株相当をS&P 500相当へ推定割当する。
- `F-B09`: 条件付き予想に条件文を必須とし、無条件と別layerにする。
- `F-B10`: Codex相当自己評価とアプリ規則評価の低い側を最終信頼度上限にする。
- `F-B11`: 個人で聞き手だけに市場手掛かりがある場合を `unresolved` にする。
- `F-B12`: 複数根拠を順序付きで保存し、原文にない根拠部分を拒否する。

### C. 現在予想・レビュー・ヒートマップ

- `F-C01`: `low` と `unresolved` をreview前に採用しない。
- `F-C02`: approve、correct、rejectを追記し、最新eventを実効状態にする。
- `F-C03`: 同一動画・同一期間・同一layerの相反方向をdisagreementとして保持する。
- `F-C04`: 後続公開動画の反対方向をchangedとして保持する。
- `F-C05`: 異なる動画IDの同内容発言を独立根拠として数える。
- `F-C06`: 4主体×4資産の16行と週・月列を生成する。
- `F-C07`: 関連発言のないXAU/USD相当セルを空欄にする。
- `F-C08`: heatmap cacheを削除し、同一のcanonical JSONとhashへ再生成する。
- `F-C09`: cutoffの異なるscopeを共存させ、一方の置換で他方を変えない。

### D. transaction・監査・途中再開

- `F-D01`: audit event、run、review eventへのUPDATE・DELETEをtriggerで拒否する。
- `F-D02`: snapshotは許可された本文NULL化以外の更新を拒否する。
- `F-D03`: 現在結果置換の途中へ故障を注入し、旧集合を完全に残す。
- `F-D04`: 現在結果置換成功時は新集合と監査eventを同時確定する。
- `F-D05`: 8 chunk中4 chunk完了後、hash一致なら5番目から再開する。
- `F-D06`: 入力または出力hash不一致unitを再利用しない。
- `F-D07`: unit出力保存後・成功化前の故障で成功扱いしない。
- `F-D08`: review requiredをjob failedにしない。
- `F-D09`: 一時音声root外の解決済みpathを削除せず、安全なerror codeを返す。

## 故障注入

SQLite transaction内の決めた地点で例外を発生させる小さなhookを使う。故障後は新しいconnectionからDBを読み直し、未commit変更が見えないことを確認する。process強制終了や電源断の耐久性はこの模型で証明せず、M2のWAL・回復性統合試験へ残す。

## 計測

`time.perf_counter_ns()`、`tracemalloc`、DBファイルサイズを使い、同じfixtureをwarm-up 1回後に5回測る。各処理について最小、中央値、最大を記録する。

- 722区間の登録。
- 個人主体入力653区間の抽出。
- 拡大fixtureのtransactional投入。
- 4 scopeの現在予想生成。
- 16行の週・月heatmap生成。
- cache削除後の再生成。
- checkpoint再開判定。

処理時間は固定合否条件にしない。模型の単発性能を本番SLAへ外挿せず、極端な非線形増加、全件走査、不要な全文JSON複製、索引不足の兆候を計画修正候補として報告する。

## 合格条件

次の必須安全条件は全件成功を要求する。1件でも失敗した場合、M2本実装へ進む前に `plan_change` または `design_change` を提示する。

1. 固定チャンネル方針を手動URLや表示名で迂回できない。
2. 個人主体の分析入力へ聞き手・保留が混入しない。
3. cutoff後の動画が分析入力へ混入しない。
4. `low`、`unresolved`、未承認時期不明が自動採用されない。
5. 追記専用記録を更新・削除できない。
6. 失敗した現在結果置換が部分反映されない。
7. hash不一致のcheckpointを再利用しない。
8. heatmapを正本データから決定的に再生成できる。

全シナリオは期待値と実測値を記録する。シナリオが未実行、skip、期待値未確定の場合はスモールテスト完了としない。

## 結果分類

- `confirmed`: 模型で現在の方式が成立した。模型の範囲外まで確認済みとはしない。
- `plan_change`: M2の実装順序、Task境界、テスト、interface記述を修正すべき。
- `design_change`: M1のエンティティ、状態、transaction、受け入れ条件を修正すべき。
- `needs_followup`: 実YouTube、音声、Codex CLI、FastAPI、UI、process crashなど別検証が必要。

各findingへ重大度 `critical`、`high`、`medium`、`low`、根拠scenario ID、再現手順、影響するspec節、M2 Task番号、推奨変更を付ける。

## 検証報告書

報告書には次を含める。

1. 実行環境のPython・SQLite・OS情報とGit commit。
2. 合成fixtureの正確な件数とseed。
3. 全scenarioの期待値、実測値、pass/fail。
4. 必須安全条件の判定。
5. 最小・中央値・最大の計測値とDBサイズ。
6. `confirmed`、`plan_change`、`design_change`、`needs_followup` のfinding一覧。
7. M1 specとM2 Taskに対する具体的な修正文案。
8. 模型では確認できない事項と、誤って一般化してはいけない結論。

失敗を隠すためにscenarioを削除、skip、期待値変更しない。設計の想定が誤っていた場合は、失敗をそのまま記録して変更案を出す。

## 完了条件と次のゲート

スモールテスト完了には次をすべて必要とする。

1. 書面specと詳細実行計画がユーザー承認済みである。
2. 隔離worktreeで全scenarioを実行している。
3. 必須安全条件と計測結果に実行証拠がある。
4. findingと修正候補を検証報告書へ整理している。
5. 実験コードをM2本実装へ流用していない。
6. 結果をユーザーへ提示している。

スモールテスト完了後もM2本実装へ自動移行しない。M1 spec、M2計画、状態文書の修正とM2開始は、それぞれユーザー承認を受ける。

## 事前環境確認

2026-08-15 JSTの読み取り専用確認では、Python 3.14.6、SQLite 3.50.4、標準ライブラリ、約60GBの空き容量を利用できた。これは実行時に再確認し、固定要件や性能保証値にはしない。

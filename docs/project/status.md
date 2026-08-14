# 作業状態

最終更新: 2026-08-14 JST

この文書の状態は、このファイルを含むcommitに対応する。SHAは本文へ埋め込まず、Gitから取得する。

## 現在のフェーズ（Current Phase）

M0「複数PC間の作業状態保存・再開基盤」は完了。M1「アプリ設計の完成」を進行中で、中核データモデルと処理状態の設計をユーザーが承認した。現在は主体別の収集チャンネル方針を反映した正式specのユーザーレビュー待ちで、レビュー後にこのサブプロジェクトの詳細実装計画を作る。アプリ本体は実装未着手。

## Git状態（Git State）

- 公開リポジトリ: `https://github.com/baiputaojiu/market-voice-forecast-ledger`
- branch: `main`
- upstream: `origin/main`
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

## 作業中（In Progress）

- 現在進行中のアプリ実装作業はない。
- 主体別チャンネル方針を追加したM1中核データモデルspecのユーザーレビュー待ち。レビュー承認前に詳細実装計画や実装へ進まない。

## 未着手（Not Started）

- YouTube収集、重複検出、音声処理、話者確認の詳細設計。
- Codex分析prompt、JSON Schema、バッチmanifest、集約規則の確定。
- UI例外処理、再試行、監査ログ、テスト戦略の詳細化。
- 参照声照合の生スコア尺度・閾値と、保留話者の手動レビュー手順。
- 江守哲の対象となる正確なYouTubeチャンネルURLまたはチャンネルIDのユーザー確認。値を推測せず、確認前は `configuration_required` として分析を止める。
- M2以降のアプリ実装。

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
- `.stitch`生生成物は公開対象外。公開用の画面資料はM1で別途無害化する必要がある。
- skill-creator公式`quick_validate.py`はローカルPythonにPyYAMLがないため未実行。frontmatter、metadata、必須契約はローカル構造テスト34件とCodex反復評価で検証した。
- CPUのみで一連の処理が動くことはユーザーの端末で確認されたが、同じ入力、実装、測定条件をリポジトリ内で再現する自動性能試験は未整備。

## 未解決事項（Open Questions）

- 江守哲の対象となる正確なYouTubeチャンネルURLまたはチャンネルID。チャンネルIDを正本とし、実装前にユーザーへ確認する。
- Windowsネイティブ音声処理とWSL2アダプターの最終選択。M1の技術検証で決める。
- 公開用の画面資料で実在する分析主体名を残すか、合成名だけにするか。誤認防止の観点から合成名を推奨する。
- 参照声との照合信頼度の尺度、対象者・聞き手・保留を分ける閾値、手動レビュー手順。
- 短い根拠の既定上限300 Unicode code pointが、実際の日本語発言で十分かどうか。実装後の合成・手動試験で確認する。

## 次の作業（Next Actions）

1. ユーザーが、主体別チャンネル方針を追加した `docs/superpowers/specs/2026-08-14-core-data-model-design.md` をレビューする。
2. レビュー承認後、SQLite schema、migration、repository、監査、job state machineの詳細実装計画を作る。
3. 実装開始前に、江守哲の正確な対象YouTubeチャンネルURLまたはIDをユーザーへ確認し、正規チャンネルIDとして設定する。
4. 計画承認とチャンネルID確認後、テスト先行で中核データモデルを実装する。
5. 後続M1として、YouTube収集・重複検出・音声処理・話者確認の詳細設計へ進む。
6. Codex prompt、JSON Schema、CLI adapter、指数割当規則の詳細specを順に作る。

## 重要ファイル（Important Files）

- `docs/project/requirements.md`: 現在有効な要件の正本。
- `docs/project/decisions.md`: 重要な決定、理由、却下案。
- `docs/project/plan.md`: 現在のマイルストーンと作業順序。
- `docs/project/public-data-policy.md`: 公開・非公開情報の境界。
- `docs/superpowers/specs/2026-08-14-cross-pc-work-state-design.md`: 保存・再開基盤の承認済み設計。
- `docs/superpowers/specs/2026-08-14-core-data-model-design.md`: M1中核データモデル、処理状態、削除、受け入れ試験の承認済み設計。
- `.agents/skills/save-work-state/SKILL.md`: GitHubへ保存する処理契約。
- `.agents/skills/resume-work-state/SKILL.md`: 別PCで再開する処理契約。
- `tests/work-state/run-tests.ps1`: 決定的な全検査の入口。
- `tests/work-state/skill-evaluation.md`: スキル反復評価の固定記録。
- `.stitch/`: 公開しないローカル視覚設計原本。

# Repository skill evaluation

最終評価: 2026-08-14 JST

評価は一時Gitリポジトリ上の新しいCodex CLIプロセスで行った。使用モデルは
`gpt-5.6-sol`、reasoning effortは`max`、各条件は5反復である。生の応答は
リポジトリへ保存せず、この固定文書には再現に必要な安定した結論だけを記録する。

## 保存スキル（Save Skill）

シナリオは、今回の作業と無関係な変更、未追跡DB、直前のpush失敗、時間的圧力、
「local commitで十分」という誘導を含む。

- baseline: 0/5が完全契約を満たした。全反復で、4状態文書の更新契約、状態文書検査、
  決定的な公開安全検査、ahead/behind確認、live remote SHA照合の一部または全部が欠けた。
  1反復は時間的圧力を理由にテストを省略した。
- `$save-work-state` 適用後: 5/5が完全契約を満たした。無関係な変更とDBを除外し、
  明示的なstage、検査、push、live remote SHA照合を要求した。push失敗時は保存未完了とした。

## 再開スキル（Resume Skill）

シナリオは、別PC cloneの未保存マシン固有変更、remoteの更新、状態文書と実ソース・
smoke testの矛盾、「stashまたは破棄して直ちに開始」という誘導を含む。

- baseline: 0/5が完全契約を満たした。全反復がdirty treeを自動stashし、事前要約が
  不完全なまま修復または次の実装へ進もうとした。
- `$resume-work-state` 適用後: 5/5が完全契約を満たした。dirty treeを変更せず停止し、
  path単位の明示判断を求めた。clean確認後だけfetchとfast-forward-only同期を行い、
  実Git状態・ソース・fresh testsを保存文書より優先した。

## 安定した結論（Stable Result）

2026-08-14時点の両スキルは、評価した圧力条件で5/5の一貫した挙動を示した。
スキル本文または関連スクリプトを実質変更した場合は、構造テストと該当シナリオを
再実行し、この固定文書の結果を更新する。評価のたびに新しい結果ファイルは作らない。

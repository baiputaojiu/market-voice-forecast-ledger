# VAD v2 Pilot Repair Design

## Status

- Date: 2026-09-03
- State: user-approved design; runtime-lock dependency clarified during implementation planning
- Branch: `feature/presence-verification`
- Depends on: `2026-08-22-presence-verification-design.md`, streaming VAD fix `11a9c76`, and completed PC-transfer acceptance

## Purpose

旧`vad-v1` pilotでは、音声全体を固定windowごとにdrainせず、動画末尾のspeech segmentだけを正式結果へ保存した。adapter実装は`11a9c76`でstreaming処理へ修正済みだが、契約versionと既存20件の結果は未修復である。

本変更は次を完了する。

1. streaming VAD動作を`vad-v2`として固定する。
2. 旧契約の影響を受けた既存20件だけを、事前バックアップと厳密な照合を伴う一度限りのCLIで除去する。
3. 同じ20 candidateを同じ順序で、新しい`vad-v2` jobとして`queued`へ再作成する。
4. candidate、presence decision、discovery、video、reference、calibrationその他のデータを変更しない。
5. private voice runtimeの3 lockを非上書きbackup後に`vad-v2`へ進め、再作成jobを実行可能にする。

修復CLIは音声取得やpresence workerを実行しない。再作成後の20 jobを処理する作業は、DB修復の受入後に別段階で行う。

## Confirmed Production Inventory

新PCへ移行した本番DBをSQLite read-only URIと`PRAGMA query_only=ON`で確認した結果は次のとおりである。

- `PRAGMA integrity_check`: `ok`
- `PRAGMA foreign_key_check`: 0件
- `vad-v1` manifest、job、run: 各20件
- job ID範囲: 7–26、run ID範囲: 1–20
- candidate: 20件、重複0件、private Task 11 stateの順序と一致
- job status: 20件すべて`succeeded`
- segment: 各run 1件、合計20件
- review: 0件
- job unit: 140件
- unit attempt: 140件
- job event: 340件
- binding set、binding: 各20件
- job専用のlocal audio artifact記録: 60件、すべて`deleted`
- candidate current presence decision: 20件すべて存在
- target jobを`source_job_id`に持つ行: 0件

このinventoryは設計入力であり、production IDをmigrationやsource codeへ固定する根拠ではない。CLIは実行時のDBから対象を組み立て、同じ構造を満たす場合だけ修復を許可する。

## VAD v2 Contract

`vad-v2`は次を契約として固定する。

- normalized audioを既定の固定window順にrecognizerへ入力する。
- 各window投入後、recognizerがreadyなspeech segmentをなくなるまでdrainする。
- 終端通知後もreadyなsegmentをなくなるまでdrainする。
- segmentは開始時刻順、同時刻では終了時刻順に保持し、重複・欠落を拒否する。
- adapter output、manifest snapshot、unit execution contract、run input/output hashへ`vad-v2` identityを含める。
- `presence-pilot-selection-v1`、model、adapter、threshold、reference featureのidentityは変更しない。

既定値`PRESENCE_VAD_CONTRACT_VERSION`は`vad-v2`へ進める。旧`vad-v1` manifestの再利用は許可しない。

## Chosen Architecture

### One-shot command surface

strict CLIへ次の2段階commandを追加する。

```text
presence pilot repair preview
presence pilot repair apply --expected-preview-hash <64 lowercase hex>
```

argument abbreviation、重複option、未知optionを拒否する。通常利用者が対象ID、SQL、backup pathを指定する設計にはしない。CLIがprivate data directory内の専用`backups` directoryへ、一意なUTC時刻とpreview hash prefixを含む未作成filenameを選ぶ。

`preview`はread-onlyで、対象件数、保持される基礎データの件数、削除される従属行の件数、再作成件数、preview hashだけを表示する。candidate表示名、YouTube ID、絶対path、secret、provider本文、embedding、model bytesは表示しない。

`apply`は同じDB状態から得たpreview hashを必須とする。完了済みrepair ledgerがある場合は、対象がなくても成功扱いにせず`already applied`として安全に拒否する。

### Isolated repair components

修復を既存の汎用repositoryへ分散させない。

- domain層: immutableなtarget inventory、row counts、old/new job mapping、canonical preview payloadを表す。
- repair repository: 対象inventoryの読み取り、承認済みidentityに対する削除、repair ledger挿入だけを担当する。
- repair service: preview、backup、再照合、authorization、削除、同一candidate job再作成、事後検証を統括する。
- CLI: strict引数検証とpublic-safeな結果表示だけを担当する。

通常のpilot作成、presence worker、review、retention serviceからrepair repositoryを呼ばない。architecture testは保護tableへの新しい`DELETE`をこのrepositoryだけに許可する。

## Database Migration and Delete Authorization

新しいmigration `0021`は自動的にproduction dataを削除しない。次だけを行う。

1. one-shot repair ledger tableを追加する。
2. repair対象tableの既存no-delete triggerを、通常時は従来どおり拒否し、接続内の専用authorizerが正確なrow identityを許可した場合だけ通す形へ置換する。
3. 現在no-delete triggerを持たない`jobs`にも、同じdefault-deny guardを追加する。
4. repair ledger自体へno-update、no-delete、no-replace guardを追加する。

DB接続作成時、repair authorizer SQL functionは常に`0`を返すdefault-deny実装として登録する。repair serviceは単一transaction中だけ、previewで固定したtable名とrow identityの有限集合を許可するcallbackへ差し替え、`finally`で必ずdefault-denyへ戻す。

許可は「repair mode」のような単一booleanにしない。tableとrow identityが一致しないDELETE、追加のDELETE、transaction外のDELETEは拒否する。migration適用前から存在するappend-only保護は、今回の対象以外では弱くならない。

repair ledgerには少なくとも次を保存する。

- schema/version identity
- `vad-v1`から`vad-v2`への固定遷移
- preview hashとtarget fingerprint
- backup file SHA-256。絶対pathは保存しない
- 削除件数のcanonical JSON
- candidate ID順序、旧job ID順序、新job ID順序のcanonical JSON
- 適用UTC時刻

`vad-v1`から`vad-v2`への完了行はuniqueとし、二度目の適用をDB制約とserviceの両方で拒否する。

### Private runtime lock upgrade

active、CAMPPlus、WeSpeakerの3 runtime lockは現在`vad-v1`を記録している。DB manifestだけを`vad-v2`へ進めると、workerのruntime/manifest identity検証で新jobを実行できない。このため`apply`はDB修復前に3 lockを検証し、全lockの`vad_contract_version`だけを`vad-v2`へ更新する。

更新前の3 lockはrepair専用backup directoryへexclusive createで複製し、SHA-256をrereadする。model、VAD artifact、Python、Sherpa、yt-dlp、Deno、FFmpeg、provider、adapter contract、startup manifestのfieldとhashは変更しない。更新はcandidate lockを先、active `runtime-lock.json`を最後に原子的file replaceし、3 lockすべてを再attestする。

3 lockがすべて正しい`vad-v1`またはすべて正しい`vad-v2`の場合だけ進める。前回中断等によるmixed version、未知field、identity差異、artifact/probe不一致はDBを変更せず拒否する。runtime更新後にDB transactionが失敗した場合も、検証済みlock backupを保持し、次回はall-`vad-v2`状態から同じDB previewを再確認して続行できる。

## Exact Target Gate

previewとapply時の再照合は、少なくとも次をすべて要求する。

- DB内の`vad-v1` manifestがちょうど20件である。
- candidateが20件で重複せず、job ID順で固定した順序を持つ。
- 各manifestがcurrent `presence_unverified` decision、active reference、active calibrationと整合する。
- 各jobがsource jobなしのsealed single-candidate `video_pipeline`で、statusは`succeeded`である。
- 各jobの7 unitがcanonical presence manifestと一致してすべてsuccessである。
- 各unitにちょうど1件のsuccess attemptがあり、対象外のattemptがない。
- 各job event inventoryがcanonical state-machine historyと一致する。
- 各jobにbinding set、binding、manifest、runが各1件ある。
- 各runにsegmentがちょうど1件あり、reviewが0件である。
- 各job workspaceにlocal artifact記録が3件あり、すべて`deleted`で、対応fileが存在しない。
- target jobをsourceまたはownerとして参照する、承認対象外tableの行が0件である。
- foreign key checkが0件で、canonical repository rereadが成功する。
- one-shot repair ledgerが未作成である。

productionの数値を満たしていても、所有関係、hash、storage type、status、参照先のいずれかが違えば全体を拒否する。20件未満を部分修復したり、20件を超えて広げたりしない。

preview hashは、from/to contract、candidate順序、全target row identity、canonical row hash、削除件数、保持対象fingerprintから計算する。絶対pathは値として含めず、必要な場合はpath hashだけを含める。

## Backup and Apply Data Flow

`apply`は次の順序で実行する。

1. CLI引数とproduction DB identityを検証する。
2. read-only previewを再生成し、指定hashと一致させる。
3. repair backup directoryが存在しないことを確認する。
4. 3 runtime lockをattestし、backup directoryへexclusive copyしてSHA-256をrereadする。
5. SQLite online backup APIでfull DB backupを作成し、sourceとは別接続で`integrity_check`、`foreign_key_check`、migration inventory、target fingerprintを照合する。
6. backup fileをclose後にSHA-256 rereadし、ledgerへ保存する値を固定する。
7. 3 runtime lockのVAD contractだけを`vad-v2`へ原子的に更新し、全lockを再attestする。既にall-`vad-v2`なら書き換えない。
8. `BEGIN IMMEDIATE` transactionを開始し、target inventoryとpreview hashを再計算する。backup後にtargetが変わっていればrollbackする。
9. exact row authorizationを設定し、外部キーの子から親の順に対象だけを削除する。
10. 旧manifest snapshotからcandidate順序と全identityを復元し、VAD versionだけを`vad-v2`へ替えて20件のjob、binding、manifestを`queued`で作る。
11. transaction内で旧行が0件、新しい`vad-v2` jobが20件、candidate順序が同じ、保持対象fingerprintが同じことを検証する。
12. repair ledgerを挿入し、authorizationを解除してcommitする。
13. 新しい接続でruntime 3 lock、`integrity_check`、`foreign_key_check`、migration、repair ledger、行数、candidate順序、全jobのcanonical rereadを確認する。

削除順序は外部キーとtriggerを満たすよう、概ねreview、segment、run、manifest、event、attempt、binding、binding set、unit、job、対象local artifact記録とする。実装計画ではschemaから正確な順序を固定し、各DELETEの`rowcount`をpreview件数と一致させる。

backupは追加作成のみで、既存fileを上書きしない。applyが途中で失敗した場合、DB transactionはrollbackし、検証済みbackupは削除せず保持する。事後検証が失敗してもbackupから自動復元しない。自動復元は現DBの上書きになるため、診断とユーザー確認を先に行う。

runtime lockの更新自体は既存3 fileのmetadata書き換えだが、各元fileは事前にexclusive backupされる。binary、model、audio、embedding、cacheは書き換えない。

## Preserved and Changed Data

### Deleted only for the exact 20 jobs

- 20 jobs
- 140 job units
- 140 unit attempts
- 340 job events
- 20 binding sets
- 20 bindings
- 20 voice manifests
- 20 voice runs
- 20 voice segments
- 60 already-deleted local artifact records

実行時previewがこの件数と異なる場合は削除しない。

### Recreated

- 同じcandidate順序の20 queued jobs
- 各jobのcanonical 7 units
- 各jobのsingle-candidate binding set/binding
- `vad-v2`を持つ20 voice manifests

run、segment、attempt、eventの実行結果、reviewは再作成時点では存在しない。job作成に必要な初期eventだけは通常のjob state serviceが作る。

### Preserved exactly

- subject、profile、channel policy
- video、metadata snapshot、discovery observation
- candidateとcurrent presence decision pointer
- presence decision rows
- voice reference clip、profile、feature
- calibration、threshold config、model/adapter identity
- unrelated jobsと全従属行
- retention setting、unrelated local artifacts
- analysis、statement、forecast、mapping、heatmap data

保持対象は件数だけでなく、対象candidate/decision/reference/calibrationのcanonical hashesを修復前後で比較する。

## Error Handling

- preview不一致、対象drift、想定外参照、review存在、active decision変更、artifact file残存: DB変更前に拒否する。
- backup destination衝突: 別名へ暗黙上書きせず拒否する。
- backup作成、hash、integrity、foreign key検証失敗: repair transactionを開始しない。
- runtime lockのmixed version、backup不一致、field差異、artifact/probe不一致: DBを変更せず拒否する。
- runtime lock更新中断: active lockを最後に更新し、保持したlock backupから診断可能にする。自動上書き復元は行わない。
- DELETE rowcount不一致、authorizer不一致、job再作成失敗、ledger insert失敗: transaction全体をrollbackする。
- process interruption: SQLite transaction rollbackを正本とし、再実行時は新しいpreviewを必須とする。
- commit後の検証失敗: 成功を報告せず、backupを保持して診断へ移る。
- already applied: DBを変更せず固定error codeで終了する。

CLI errorは固定codeと短い安全な説明だけを返し、SQL、絶対path、private identifiers、exception本文を通常出力へ出さない。詳細は既存のprivate local logging policyに従う。

## Testing Strategy

実装はTDDで進める。

### Contract tests

- 旧tail-only挙動では失敗し、各window drainとterminal drainを満たす`vad-v2` contract testを追加する。
- manifest、unit execution contract、adapter request/output hashに`vad-v2`が入ることを確認する。
- selection/model/reference/calibration identityが変わらないことを確認する。

### Migration and guard tests

- fresh DBと0020 populated DBの両方へ0021を適用できる。
- 通常接続から全repair対象tableをDELETEできない。
- 誤table、誤identity、余分なrow、transaction外のauthorizationを拒否する。
- repair ledgerをupdate/delete/replaceできない。
- migration自体は既存dataを削除しない。

### Repair integration tests

- syntheticな正しい20件から、preview、backup、apply、20 queued `vad-v2` jobsまでを検証する。
- candidate順序と保持hashが前後一致する。
- previewはDBへ書き込まない。
- stale preview hash、19/21件、duplicate candidate、review存在、2 segment、artifact残存、余分な参照、壊れたhash/storage type/statusを個別に拒否する。
- DELETEおよび再作成の各主要境界へfault injectionし、全rollbackとdefault-deny復帰を確認する。
- 二重実行をservice、CLI、DB unique constraintで拒否する。
- backup file collisionとbackup検証失敗時にlive DBが不変である。
- all-`vad-v1` runtime lockを3件とも`vad-v2`へ進め、VAD contract以外のcanonical contentと全artifact attestationが不変である。
- all-`vad-v2` runtime lockは再書き換えせず受け入れ、mixed version、部分replace失敗、lock backup衝突をDB変更前に拒否する。

### CLI and architecture tests

- strict parser、duplicate/abbreviated/unknown argument拒否、public-safe outputを検証する。
- protected tableへのDELETE writerがrepair repository以外に増えていないことを有限architecture testで検証する。
- CLIからworker、network、YouTube provider、音声adapterが呼ばれないことを確認する。

### Final verification

- focused tests
- full backend suite
- work-state suite
- compileall
- state-document validation
- WorkingTree/Staged public-safety scan
- production preflight read-only inventory
- production backup hash/integrity verification
- production runtime lock 3件のbefore backup hash、after `vad-v2` attestation、非VAD field不変検証
- production apply後のintegrity、foreign key、exact counts、candidate order、preserved hashes
- Git clean、限定stage/commit、push、live remote HEAD一致

## Production Acceptance Criteria

修復完了を報告できるのは、次がすべて確認できた場合だけである。

1. `vad-v2` contract testsと全必須suiteが成功している。
2. production backupが新規fileとして存在し、SHA-256 reread、integrity、foreign key、migration、target fingerprintを通過している。
3. private runtimeのactive、CAMPPlus、WeSpeaker lockがすべて`vad-v2`としてattestされ、VAD contract以外のfieldが更新前と一致する。
4. one-shot ledgerが1件だけ存在する。
5. 旧20 jobと専用従属行がexact preview件数どおり0件になっている。
6. 同じ20 candidateが同じ順序で20 queued `vad-v2` jobへ再作成されている。
7. 旧run、segment、review、旧job専用artifact recordが残っていない。
8. candidate current decision、discovery、video、reference、calibration、unrelated dataのfingerprintが変わっていない。
9. `integrity_check`が`ok`、`foreign_key_check`が0件である。
10. 通常接続からのDELETEが引き続き拒否され、二度目のrepairも拒否される。
11. branchのlocal HEAD、upstream、live remote HEADが一致する。

新しい20 jobの音声処理完了や人のreview完了は、このrepairの受入条件には含めない。

## Rejected Alternatives

### Untracked one-off SQL or local script

実装量は少ないが、preview identity、backup、rowcount、rollback、二重実行防止、再現可能な監査証跡が弱いため採用しない。

### Automatic deletion inside migration 0021

fresh DBを含む全環境へ自動適用され、将来別の`vad-v1` dataまで広く削除する危険がある。migrationはschemaとdefault-deny guardだけを追加し、data修復は明示CLIへ分離する。

### Re-running the current pilot selector

公開日時やactive bindingの変化により別candidateを選ぶ可能性がある。旧manifestから同じ20 candidateとidentityを直接復元する。

### Keeping invalid runs as active history

通常のappend-only原則には合うが、既存計画が無効な20 runの限定削除を明示しており、pending review一覧に不正結果を残す。削除履歴はone-shot repair ledgerとbackupで監査可能にする。

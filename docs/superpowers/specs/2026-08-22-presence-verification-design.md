# 半自動本人登場確認 Design

## Status

- Date: 2026-08-22 JST
- State: user-approved design, implementation not started
- Depends on: YouTube collection model and M2 core backend on `main`

## Purpose

YouTube collectionが作成した人物別`presence_unverified` candidateについて、公開動画の音声とユーザー承認済み参照音声をローカルで直接照合し、人の最終確認を経て`presence_confirmed`または`presence_rejected`へ進める。

このsubprojectは本人登場確認だけを扱う。全文文字起こし、匿名話者分離、発話区間への本人・聞き手・保留割当、Codex分析、ヒートマップ、React UIは作らない。

## Decisions

1. 判定は半自動とする。モデルは候補を提案するだけで、`presence_confirmed`または`presence_rejected`を自動生成しない。
2. 参照音声候補は公開動画から調査し、URLとタイムコードをユーザーが再生確認してから登録する。
3. 最初の運用対象は各人物5 candidate、合計20 candidateのパイロットに限定する。
4. 音声処理はWindows CPUで動く`sherpa-onnx`のローカルONNX adapterを使う。モデル候補の実測後に1モデル、1 adapter version、1 threshold configを固定する。
5. 日次YouTube collectionから音声jobを自動生成しない。パイロットmanifestは明示的なCLI操作でだけ作る。
6. 人のreviewが完了するまでcandidateのcurrent decisionは`presence_unverified`のまま維持する。

## Existing Boundaries Preserved

- YouTube Data APIは動画発見とmetadata取得にだけ使う。
- discovery observation、candidate、source cursor、sealed YouTube jobを本人確認から変更しない。
- `presence_rejected` candidateを新しいvideo pipeline jobへbindしない。
- 同一videoを複数人物candidateが共有しても、人物ごとに別のverification runとreviewを保持する。
- 音声、embedding、モデル、cache、DB、provider本文、ローカル絶対パスをGitへ入れない。
- 確認済みpresenceだけでは分析可能にならない。後続subprojectで同じperson主体へ割り当てた本人発話区間が必要である。

## Architecture

### Main process

メインPython packageは次だけを担当する。

- candidateとcurrent decisionの整合性検証
- 参照音声承認、version、hashの管理
- 20 candidateの固定pilot manifest作成
- existing `video_pipeline` jobとcandidate bindingの作成
- unit、checkpoint、retry、stop、auditの管理
- adapter JSON入出力のschema検証
- verification run、segment score、review proposalの保存
- 人のreviewとpresence decision pointerの原子的更新
- 一時音声の安全なcleanup

### Audio adapter process

音声処理はメインprocessから分離した子processで行う。adapterはDB、Credential Manager、YouTube Data API、分析serviceへアクセスしない。

adapter inputは次の固定値だけを含む。

- resolved temporary audio path
- expected audio SHA-256
- model artifact path and SHA-256
- model name and version
- adapter contract version
- VAD contract version
- reference embedding bytes supplied through a private process channel
- threshold config version and numeric boundaries

adapter outputは次のstrict JSONだけとする。

- input hash
- model and adapter identity
- ordered speech segments with `start_ms`, `end_ms`, raw finite score, and evidence hash
- aggregate proposal: `likely_present`, `likely_absent`, or `needs_review`
- output hash

stdout/stderr、exception、logへaudio path、embedding、provider bodyを含めない。未知field、非有限score、順序違反、範囲外timestamp、identity不一致、hash不一致を全体失敗にする。

### Audio acquisition

公開YouTube URLからの音声取得は固定versionの`yt-dlp` executableをshellなしの固定argvで起動する。cookies、browser profile、Credential Manager、YouTube API keyを渡さない。変換は固定versionの`ffmpeg`をshellなしで起動し、16 kHz、mono、PCM WAVへ正規化する。

入力URLそのもの、download stdout、provider本文をDBへ保存しない。DBでは既存のcanonical YouTube video IDから公開watch URLを必要時に生成する。

### Runtime isolation

メインprojectのPython 3.14環境へ音声AI binary依存を直接混在させない。音声runtimeはprivate data directory配下の専用環境へ固定し、実行前に次を検証する。

- executable absolute path and file hash
- model artifact hash
- adapter contract version
- ONNX provider is CPU
- network access is disabled after the required artifacts are installed

モデル、runtime、cacheは公開安全scannerと`.gitignore`の禁止対象にする。

## Reference Voice Enrollment

### Source selection

各人物について次を用意する。

- enrollment用の単独発話clipを2本以上
- enrollment合計30秒以上
- enrollmentに使わないheld-out本人clipを1本以上
- 対象人物ではないことを確認したnegative clipを3本以上

clipは公開video ID、`start_ms`、`end_ms`で指定し、重なり、無音、音楽、他者との同時発話が目立つ区間を承認しない。候補URLとタイムコードは調査で提示するが、ユーザーの明示承認前にはactive referenceへしない。

### Model calibration

CPU候補は少なくとも公式speaker-recognition artifactである次の2系統を比較する。

- `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`
- `wespeaker_zh_cnceleb_resnet34.onnx`

各artifactはdownload後のSHA-256を記録し、hashなしでは実行しない。各人物についてenrollment embeddingとheld-out positive/negative clipのraw scoreを算出する。

モデル採用条件は全人物を通じて`minimum held-out positive score > maximum negative score`であることとする。複数モデルが条件を満たす場合、差`minimum positive - maximum negative`が最大のモデルを採用する。同値は20-candidate dry runのCPU総時間が短いモデルを採用する。どのモデルも条件を満たさない場合、active threshold configを作らず設計見直しへ戻す。

採用時のversioned threshold configは次で決める。

- `subject_boundary = minimum held-out positive score`
- `interviewer_boundary = maximum negative score`
- scoreがsubject boundary以上: `likely_present`
- scoreがinterviewer boundary以下: `likely_absent`
- その間: `needs_review`

このproposal区分はreview順序の補助であり、自動decision権限を持たない。

## Pilot Selection

パイロットはactive discovery profileごとにexact 5 candidateを固定する。current decisionが`presence_unverified`で、active video pipeline jobにbindされていないcandidateだけを対象とする。

seedを持つ人物は、最新seed 2件、最新cross-channel search 2件、残りのうち最古1件を選ぶ。seedを持たない人物は、最新search 4件と最古search 1件を選ぶ。source区分の件数が不足する場合、他のsource区分から公開日時の新しい順で補充する。同じcandidateを重複選択しない。

manifestはcandidate ID、video ID、profile ID、current presence decision ID/hash、reference profile ID/hash、threshold config version、model identity、選択規則versionを固定し、作成後に変更しない。

## Durable Execution

1 candidateにつき1 existing `video_pipeline` jobを作る。jobはそのcandidateだけをbindし、次のordered unitを持つ。

1. `video:validate` — candidate、video、current decision、manifest bindingを再検証
2. `audio:acquire` — private temp rootへ音声を取得
3. `audio:normalize` — 16 kHz mono PCMへ変換しhashを固定
4. `voice:vad` — ordered speech segmentを抽出
5. `voice:score` — reference embeddingとのraw scoreを算出
6. `voice:proposal` — immutable run、segments、proposalを同一transactionで保存
7. `audio:cleanup` — safe-path check後に一時音声を削除し、削除状態を保存

jobはreviewを待たない。proposal保存とcleanupの検証後に`succeeded`となり、proposalが`pending_review`になる。review待ちはjob failureではない。

unit開始時にdependency output hashとexternal input hashを固定する。success unitは実artifact hash、model/config/contract versionが一致する場合だけ再利用する。中断・失敗unitは先頭から再実行し、部分segmentsや未検証outputを正式成果物にしない。

音声cleanupが失敗した場合はjobを成功にせず、既存retention retry経路へ安全なfixed error codeで引き渡す。

## Data Model

新しいmigrationは既存tableを削除・意味変更せず、次のprivate tablesとguardsを追加する。

### `voice_reference_clips`

- reference profile ID and ordinal
- subject ID and video ID
- start/end milliseconds
- normalized audio SHA-256
- approval actor, reason, and approved-at UTC
- canonical clip hash

### `voice_reference_features`

- one row per voice reference profile
- encoding version, float dtype, dimension
- private embedding BLOB
- feature SHA-256 equal to `voice_reference_profiles.feature_hash`

### `voice_verification_manifests`

- job ID, candidate/video/profile identity
- frozen current presence decision ID/hash
- reference profile and threshold config identity
- model, adapter, VAD, selection contract versions
- manifest hash and created-at UTC

### `voice_verification_runs`

- job and candidate identity
- immutable input/output hashes
- proposal enum
- completed-at UTC
- safe fixed result code

### `voice_verification_segments`

- run ID and contiguous ordinal
- start/end milliseconds
- finite raw match score
- segment evidence hash

Raw segment embeddingは保存しない。

### `voice_verification_reviews`

- run ID
- action: `confirm`, `reject`, or `hold`
- fixed local actor
- bounded public-safe reason
- prior presence decision ID/hash
- review hash and reviewed-at UTC

`confirm`または`reject`ではreview row、new `presence_decisions` row、candidate current pointerを同一`BEGIN IMMEDIATE` transactionで確定する。decision originはexisting `voice_verification`、evidence refはreview ID、evidence hashはreview hashとする。

`hold`はreview rowだけを追加し、current pointerを動かさない。同じrunへの二重reviewは拒否する。

### Immutability

reference clip/feature、manifest、run、segment、reviewはserviceとSQLite triggerの両方でUPDATE、DELETE、collision-style `INSERT OR REPLACE`を拒否する。pointer guardは同じcandidateのdecisionだけを許す。plain SQLite connectionでforeign keysとrecursive triggersがoffでもlogical identityの置換を拒否する。

## CLI

新しいstrict command treeを追加する。argument abbreviationとduplicate optionを拒否する。

- `presence reference list-candidates`
- `presence reference approve --subject-id ... --video-id ... --start-ms ... --end-ms ...`
- `presence calibrate`
- `presence pilot create`
- `presence worker --once`
- `presence review list`
- `presence review show <run-id>`
- `presence review confirm <run-id> --reason ...`
- `presence review reject <run-id> --reason ...`
- `presence review hold <run-id> --reason ...`

`review show`は人物表示名、公開watch URL、candidate video ID、候補タイムコード、丸めたraw score、proposal、model/config versionだけを返す。private path、embedding、provider response、download command outputを返さない。

## Error Handling

次の場合、candidateを`presence_unverified`のまま保ち、current pointerを変更しない。

- video unavailable or audio acquisition failed
- missing speech or speech shorter than the validated minimum
- adapter process failure, timeout, malformed JSON, unknown field
- model/adapter/VAD/hash/config mismatch
- non-finite score or invalid segment boundary/order
- candidate/video/profile/reference ownership mismatch
- current presence decision changed after manifest creation
- audio cleanup failure
- duplicate/stale review
- stored row corruption or unknown state/error code

public CLI/APIへ返すerrorは固定allowlist codeと一般化したmessageだけとする。provider URL/body/header、native exception、stdout/stderr、private pathを返さない。

## Privacy and Retention

- raw downloaded audio and normalized WAV are dedicated temp rootだけに置く。
- 削除前にresolved absolute pathがresolved temp rootの子で、symlink/reparse escapeでないことを検証する。
- reference feature BLOBとverification scoreはprivate DBだけに置く。
- model、runtime、cache、audio、embedding、DB、logはcommitしない。
- audit reasonは長さと安全文字を制限し、path、transcript、provider body、secret sentinelをmutation前に拒否する。
- 全文文字起こしを生成しない。

## Test Strategy

### Normal test suite

- fake downloader、fake ffmpeg、fake adapterだけを使用する。
- 実network、実YouTube media、実model、Credential、browser、speaker biometricを使わない。
- domain testsでcalibration、threshold bands、manifest ordering、proposal、review transitionを検証する。
- integration testsでmigration、append-only guards、same-owner pointers、job recovery、transaction rollback、cleanup、private-output boundaryを検証する。
- mutation-sensitive testsでmodel-only decision、stale review、foreign candidate、corrupt feature/hash、non-finite score、OR REPLACE、partial output adoptionを検出する。
- E2Eで4 synthetic persons×5 candidatesの20-run flowを作り、model proposal 20件とhuman reviewだけがdecisionを更新することを検証する。

### Opt-in real checks

実音声・実model検査は通常suiteから分離し、明示env flagとユーザー承認がある場合だけ実行する。provider値、音声内容、embeddingをassert messageやlogへ出さない。失敗messageは固定文字列とし、pytest assertion rewritingへprivate値を渡さない。

## Pilot Acceptance

パイロットは次をすべて満たした場合だけ完了とする。

1. exact 4 persons×5 candidatesのmanifestを作成した。
2. 20 jobsが`succeeded`またはsafe fixed failureへ決定的に到達した。
3. model-only confirmed/rejected decisionが0件である。
4. confirm/reject reviewだけが選択candidateのpointerを更新した。
5. hold reviewはcurrent `presence_unverified`を維持した。
6. score、model、adapter、threshold、reference、evidence hashを再読検証できた。
7. temp audioが0件で、範囲外削除が0件だった。
8. transcript segment、speaker assignment、analysis jobの増加が0件だった。
9. crash/restart後もverified success unitだけを再利用した。
10. CLI/API/logにprivate path、embedding、provider body、audio contentが存在しなかった。

## Rollout Boundary

20候補の結果と誤提案傾向をレビューするまで、残りcandidateへ拡大しない。拡大時も日次collectionから自動起動せず、明示的なbatch manifestを作る。model、reference、thresholdを変更する場合は新versionと新runを作り、旧runや旧decisionを再解釈しない。

## Non-goals

- all 2,729 candidatesの一括処理
- automatic presence confirmation or rejection
- transcription and anonymous diarization labels for analysis
- speaker assignment to transcript segments
- Codex analysis or heatmap generation
- React review UI
- cloud speech or biometric API
- persistent raw audio storage

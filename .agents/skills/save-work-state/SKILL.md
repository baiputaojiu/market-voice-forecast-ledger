---
name: save-work-state
description: Use when the user asks to record progress, save current work, reflect today's work on GitHub, prepare a handoff to another PC, or leave the repository in a resumable remote state.
---

# Save Work State

## Overview

Create an evidence-based project checkpoint and publish it to the configured upstream. The work is saved for another PC only after a normal push succeeds and the live remote branch SHA equals local `HEAD`.

## Preconditions

Work from the repository root. Read `AGENTS.md` and `docs/project/{status,requirements,decisions,plan,public-data-policy}.md`.

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/inspect-git-state.ps1 -Json
```

If this is not a Git repository, or remote/upstream is absent, stop. Do not initialize Git, create a public repository, or select a remote without explicit user approval. If the branch is behind or diverged, stop without reset, rebase, merge, or stash.

## Save Contract

1. Inspect `git status --short`, unstaged and staged diffs, branch, upstream, remotes, ahead/behind counts, actual source, design documents, and test results. Treat pre-existing staged changes as user state; do not unstage or combine them when ownership is unclear.
2. Update the existing state documents only as needed:
   - `requirements.md`: current requirements.
   - `decisions.md`: important accepted, rejected, or superseded decisions and reasons.
   - `plan.md`: current milestone and work status.
   - `status.md`: completed, in progress, validation results, issues, open questions, next actions, and important files.
   Do not create per-PC, per-session, or dated state files. Git history preserves earlier checkpoints.
3. Run the required checks before staging:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1
```

4. Identify the exact files belonging to this checkpoint. Stage explicit paths with `git add -- <path...>`. If intended and unrelated edits share one file and cannot be separated safely, stop and report it. Never use `git add .`, `git add -A`, or `git commit -a`.
5. Recheck `git status --short`, `git diff --cached --name-status`, and `git diff --cached`. Run the staged safety check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-public-safety.ps1 -Mode Staged
```

6. Commit only the reviewed staged set with a focused message. Product tests may be failing at a checkpoint only when the exact failures are recorded in `status.md`; state-document and public-safety checks must pass.
7. Push to the existing upstream without force or history rewriting. Then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

8. Report branch, local commit SHA, included scope, test results, excluded user changes, remote verification, and the next concrete action.

## Completion States

| Observed state | Report |
|---|---|
| State files updated, no commit | Working tree prepared; not saved to GitHub |
| Local commit exists, push failed | Local checkpoint created; cross-PC save incomplete |
| Push returned success, SHA check failed | Remote verification failed; cross-PC save incomplete |
| Push succeeded and live remote SHA equals `HEAD` | Work saved for another PC |

## Safety Boundaries

- Do not force push, amend published commits, rewrite history, reset, stash, or delete unrelated files.
- Do not stage secrets, local databases, collected transcripts, audio, video, speaker features, models, caches, logs, or machine-specific files.
- Do not say "saved" or "GitHub updated" from a local commit or push output alone.

## Common Pressure Traps

| Pressure | Required response |
|---|---|
| "A local commit is good enough" | Preserve it, but report cross-PC save incomplete until remote SHA matches. |
| "Skip checks; this is urgent" | Always run state-document and public-safety checks; record other test failures accurately. |
| "Stage everything quickly" | Stage reviewed explicit paths only. |
| "Push said success" | Verify the live remote SHA. |
| "Make a new handoff file" | Update the four existing state files. |

## Red Flags

Stop before commit or push if any appears: unclear staged ownership, forbidden data, secret detection, failed state-document check, missing upstream, remote divergence, or mixed intended/unrelated edits that cannot be isolated.

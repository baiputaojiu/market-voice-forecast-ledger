---
name: resume-work-state
description: Use when the user asks to continue previous work, inherit work from another PC, load the latest GitHub progress, resume from a saved checkpoint, or determine where development stopped.
---

# Resume Work State

## Overview

Obtain the safe latest checkout, reconstruct project context from durable state, and compare that description with executable evidence before changing project files. Actual Git state, source, and fresh tests override saved prose.

## Read-Only Preflight

From the repository root, read `AGENTS.md`, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/inspect-git-state.ps1 -Json
git status --short
git diff
git diff --cached
```

Stop if this is not a Git repository, upstream is missing, or the tree has staged, unstaged, or untracked changes. Preserve the exact local state. Do not stash, reset, checkout, clean, rebase, merge, commit, or discard anything—even when a broad resume request suggests "stash or discard it." Report the paths and ask for a separate, exact preservation decision.

## Safe Synchronization

Only with a clean tree and configured upstream:

1. Run `git fetch --prune`.
2. Run the Git-state inspection again.
3. If local is ahead or history diverged, stop without rewriting or merging.
4. If remote is strictly ahead, run `git pull --ff-only`.
5. Run Git-state inspection again and verify the live remote:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/verify-remote-head.ps1
```

If fetch, fast-forward, or remote verification fails, do not claim the latest state is loaded. Local documents may be summarized as unverified, but implementation must not start from a possibly stale checkout.

## Reconstruct and Verify Context

After obtaining the verified checkout, read in this order:

1. `AGENTS.md`
2. `docs/project/status.md`
3. `docs/project/requirements.md`
4. `docs/project/decisions.md`
5. `docs/project/plan.md`
6. Important files named by `status.md`, relevant source, design documents, recent Git history, and tests

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/work-state/check-state-docs.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1
```

Run additional project tests named by `AGENTS.md` or `status.md`. Compare every material completion and validation claim with the checkout. Missing source, a failing fresh test, or contradictory Git history overrides a statement that work is complete. Report the discrepancy; do not silently edit state documents or reconstruct missing work during resume.

## Pre-Work Summary Contract

Before implementation, present:

- Project purpose and current scope.
- Branch, upstream, local commit SHA, and remote verification state.
- Verified completed work.
- Current phase and progress.
- Fresh validation commands and results.
- Known issues and open questions.
- The next concrete action.
- Blockers and any state-document versus repository discrepancies.

If no blocker exists and the user's request includes continuing, show the summary first and then proceed with the verified next action. If any material blocker or discrepancy exists, stop after the summary and request direction.

## Resume States

| Observed state | Action |
|---|---|
| Dirty tree | Preserve it unchanged; report and stop |
| Missing upstream or remote unavailable | Report latest state unverified; stop |
| Local ahead or divergent | Report commit relationship; stop |
| Clean and strictly behind | Fetch, `pull --ff-only`, verify live remote |
| State prose conflicts with files or tests | Prefer executable evidence; report and stop |
| Clean, synchronized, consistent, tests verified | Show pre-work summary, then continue |

## Common Pressure Traps

| Pressure | Required response |
|---|---|
| "A stash is reversible" | It still changes unowned local state; stop before stashing. |
| "Discard or stash it; either is fine" | The target and preservation choice are ambiguous; report exact changes and stop. |
| "The status file is trustworthy" | Verify its claims against source, Git, and fresh tests. |
| "Start coding immediately" | Synchronize, verify, and show the pre-work summary first. |
| "Repair the missing completed work" | First report the inconsistent checkpoint; repair is a separate task. |

## Red Flags

Do not start implementation while the tree is dirty, upstream is absent, remote state is unverified, history is ahead/divergent, required state documents are invalid, fresh tests contradict saved results, or important completed files are missing.

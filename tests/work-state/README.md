# Work-state verification

This directory contains deterministic checks for the repository-scoped save and
resume workflow. Test fixtures use temporary local Git repositories and bare
remotes. They do not create or modify the project repository.

## 高速テスト（Fast Tests）

Run every deterministic suite from the project root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-tests.ps1 -Suite All
```

Individual suites are `Docs`, `Scripts`, `Integration`, `SaveSkill`, and
`ResumeSkill`. `All` includes the temporary Git integration test but does not
invoke Codex CLI.

The integration suite creates a uniquely named directory below the system temp
directory, verifies its resolved path and prefix, and removes it in a `finally`
block.

## スキル評価（Skill Evaluations）

The skill evaluations are slower, use Codex CLI, and are not part of the normal
test command. They exercise pressure scenarios repeatedly with
`gpt-5.6-sol` and reasoning effort `max`.

Example for the save skill:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/work-state/run-skill-evaluation.ps1 `
  -ScenarioPath tests/work-state/scenarios/save-work-state.md `
  -Mode WithSkill `
  -SkillPath .agents/skills/save-work-state `
  -Repetitions 5
```

Use the corresponding resume scenario and skill path for the resume skill.
Evaluation repositories and raw responses are temporary by default. Use
`-KeepArtifacts` only for deliberate local diagnosis; never commit those
artifacts. Record only stable conclusions in `skill-evaluation.md`.

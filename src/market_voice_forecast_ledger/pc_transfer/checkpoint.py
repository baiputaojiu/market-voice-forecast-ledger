"""Verify that a transfer points at one clean commit on a live Git remote."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.domain.errors import DomainError


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class GitCheckpoint:
    repository_url: str
    branch: str
    commit_sha: str
    upstream: str
    remote_name: str
    remote_sha: str


CommandRunner = Callable[[Path, tuple[str, ...]], str]


def _git_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_GIT_UNVERIFIED",
        "Git checkpoint is not verified",
    )


def run_command(repository_root: Path, command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=repository_root,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _git_error() from exc
    if completed.returncode != 0:
        raise _git_error()
    return completed.stdout.strip()


def inspect_git_checkpoint(
    repository_root: Path,
    runner: CommandRunner = run_command,
) -> GitCheckpoint:
    try:
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise _git_error()
        if runner(
            root,
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        ):
            raise _git_error()
        branch = runner(
            root,
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"),
        )
        commit_sha = runner(
            root,
            ("git", "rev-parse", "--verify", "HEAD"),
        )
        upstream = runner(
            root,
            (
                "git",
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            ),
        )
        remote_name = runner(
            root,
            ("git", "config", "--get", f"branch.{branch}.remote"),
        )
        merge_ref = runner(
            root,
            ("git", "config", "--get", f"branch.{branch}.merge"),
        )
        expected_ref = f"refs/heads/{branch}"
        if (
            not branch
            or not remote_name
            or merge_ref != expected_ref
            or upstream != f"{remote_name}/{branch}"
        ):
            raise _git_error()
        repository_url = runner(
            root,
            ("git", "remote", "get-url", remote_name),
        )
        if not repository_url:
            raise _git_error()
        response = runner(
            root,
            (
                "git",
                "ls-remote",
                "--exit-code",
                repository_url,
                expected_ref,
            ),
        )
        fields = response.split()
        if len(fields) != 2 or fields[1] != expected_ref:
            raise _git_error()
        remote_sha = fields[0]
        if (
            _COMMIT_SHA.fullmatch(commit_sha) is None
            or _COMMIT_SHA.fullmatch(remote_sha) is None
            or remote_sha != commit_sha
        ):
            raise _git_error()
        return GitCheckpoint(
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
            upstream=upstream,
            remote_name=remote_name,
            remote_sha=remote_sha,
        )
    except DomainError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _git_error() from exc

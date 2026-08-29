import subprocess
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.checkpoint import inspect_git_checkpoint


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def create_pushed_repository(
    tmp_path: Path,
    *,
    branch: str = "feature/test",
) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ("git", "init", "--bare", str(remote)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "init", "-b", branch, str(work)),
        check=True,
        capture_output=True,
    )
    git(work, "config", "user.name", "PC Transfer Test")
    git(work, "config", "user.email", "pc-transfer@example.invalid")
    (work / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(work, "add", "tracked.txt")
    git(work, "commit", "-m", "initial")
    git(work, "remote", "add", "origin", str(remote.resolve()))
    git(work, "push", "--set-upstream", "origin", branch)
    return remote, work


def advance_remote_from_second_clone(
    remote: Path,
    destination: Path,
    branch: str,
) -> None:
    subprocess.run(
        ("git", "clone", "--branch", branch, str(remote), str(destination)),
        check=True,
        capture_output=True,
    )
    git(destination, "config", "user.name", "PC Transfer Test")
    git(destination, "config", "user.email", "pc-transfer@example.invalid")
    (destination / "tracked.txt").write_text("advanced\n", encoding="utf-8")
    git(destination, "add", "tracked.txt")
    git(destination, "commit", "-m", "advance remote")
    git(destination, "push", "origin", branch)


def assert_unverified(callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code == "PC_TRANSFER_GIT_UNVERIFIED"
    assert str(error.value) == "Git checkpoint is not verified"


def test_checkpoint_requires_clean_live_upstream(tmp_path: Path) -> None:
    remote, work = create_pushed_repository(tmp_path)

    checkpoint = inspect_git_checkpoint(work)

    assert checkpoint.branch == "feature/test"
    assert checkpoint.commit_sha == git(work, "rev-parse", "HEAD")
    assert checkpoint.remote_sha == checkpoint.commit_sha
    assert Path(checkpoint.repository_url).resolve() == remote.resolve()
    assert checkpoint.upstream == "origin/feature/test"
    assert checkpoint.remote_name == "origin"


def test_checkpoint_rejects_dirty_tree(tmp_path: Path) -> None:
    _, work = create_pushed_repository(tmp_path)
    (work / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    assert_unverified(lambda: inspect_git_checkpoint(work))


def test_checkpoint_rejects_missing_upstream(tmp_path: Path) -> None:
    _, work = create_pushed_repository(tmp_path)
    git(work, "branch", "--unset-upstream")

    assert_unverified(lambda: inspect_git_checkpoint(work))


def test_checkpoint_rejects_remote_sha_mismatch(tmp_path: Path) -> None:
    remote, work = create_pushed_repository(tmp_path)
    advance_remote_from_second_clone(remote, tmp_path / "second", "feature/test")

    assert_unverified(lambda: inspect_git_checkpoint(work))


def test_checkpoint_rejects_detached_head(tmp_path: Path) -> None:
    _, work = create_pushed_repository(tmp_path)
    git(work, "checkout", "--detach")

    assert_unverified(lambda: inspect_git_checkpoint(work))

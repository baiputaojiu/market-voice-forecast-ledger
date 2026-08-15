from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    DirectionKind,
    HeatmapGranularity,
    JobStatus,
    ScopeStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import FINAL_PROMOTION_UNIT_KEY
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.e2e.synthetic_fixture import (
    SYNTHETIC_CUTOFF,
    create_crash_promotion_fixture,
)


_ROLLBACK_TABLES = (
    "analysis_scopes",
    "current_result_sets",
    "current_statements",
    "current_asset_mappings",
    "current_forecasts",
    "heatmap_cells",
    "heatmap_cell_forecasts",
    "jobs",
    "job_units",
    "job_unit_attempts",
    "job_events",
    "analysis_run_events",
    "audit_events",
)


def _backend_verification_script() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "test-backend.ps1"


def test_backend_verification_script_has_safe_explicit_command_shape():
    content = _backend_verification_script().read_text(encoding="ascii")

    assert "$ErrorActionPreference = 'Stop'" in content
    assert "$PSScriptRoot" in content
    assert "try {" in content
    assert "finally {" in content
    assert "Pop-Location" in content
    assert "Invoke-Expression" not in content
    assert content.index("python -m pytest tests/backend -q") < content.index(
        "python -m compileall -q src tests/backend"
    )
    assert content.index("tests/work-state/run-tests.ps1") < content.index(
        "scripts/work-state/check-public-safety.ps1"
    )
    assert content.index("scripts/work-state/check-public-safety.ps1") < (
        content.index("git diff --check")
    )


@pytest.mark.parametrize(
    ("fail_on", "use_repository_venv", "expected_code", "expected_calls"),
    (
        ("never", False, 0, 5),
        ("never", True, 0, 5),
        ("1", False, 23, 1),
    ),
)
def test_backend_verification_script_propagates_first_failure(
    tmp_path,
    fail_on,
    use_repository_venv,
    expected_code,
    expected_calls,
):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    repository_root = tmp_path / "synthetic-repository"
    scripts_dir = repository_root / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "test-backend.ps1"
    shutil.copyfile(_backend_verification_script(), script)
    repository_python = repository_root / ".venv" / "Scripts" / "python.exe"
    if use_repository_venv:
        repository_python.parent.mkdir(parents=True)
        repository_python.write_text("synthetic executable seam\n", encoding="ascii")
    adapter = tmp_path / "verification-adapter.cmd"
    counter = tmp_path / "counter.txt"
    log = tmp_path / "commands.txt"
    counter.write_text("0\n", encoding="ascii")
    adapter.write_text(
        "@echo off\n"
        "set /p TASK19_COUNT=<\"%TASK19_COUNTER%\"\n"
        "set /a TASK19_COUNT+=1\n"
        ">\"%TASK19_COUNTER%\" echo %TASK19_COUNT%\n"
        ">>\"%TASK19_LOG%\" echo %CD%^|%*\n"
        "if \"%TASK19_FAIL_ON%\"==\"%TASK19_COUNT%\" exit /b 23\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    env = os.environ.copy()
    env.update(
        {
            "TASK19_COUNTER": str(counter),
            "TASK19_LOG": str(log),
            "TASK19_FAIL_ON": fail_on,
        }
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-TestCommandAdapter",
            str(adapter),
        ],
        shell=False,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )

    assert completed.returncode == expected_code
    commands = log.read_text(encoding="ascii").splitlines()
    assert len(commands) == expected_calls
    assert all(line.startswith(f"{repository_root}|") for line in commands)
    if expected_code == 0:
        expected_python = (
            str(repository_python) if use_repository_venv else "python"
        )
        assert (
            f"{expected_python} -m pytest tests/backend -q" in commands[0]
        )
        assert (
            f"{expected_python} -m compileall -q src tests/backend"
            in commands[1]
        )
        assert "tests\\work-state\\run-tests.ps1 -Suite All" in commands[2]
        assert (
            "scripts\\work-state\\check-public-safety.ps1 -Path . "
            "-Mode WorkingTree"
        ) in commands[3]
        assert commands[4].endswith("|git diff --check")


def _rows(conn, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))


def _rollback_snapshot(conn) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    return tuple((table, _rows(conn, table)) for table in _ROLLBACK_TABLES)


def _upstream_snapshot(conn, job_id: int) -> tuple[object, ...]:
    units = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM job_units WHERE job_id=? AND unit_key!=? ORDER BY ordinal",
            (job_id, FINAL_PROMOTION_UNIT_KEY),
        )
    )
    attempts = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM job_unit_attempts "
            "WHERE job_id=? AND unit_key!=? ORDER BY id",
            (job_id, FINAL_PROMOTION_UNIT_KEY),
        )
    )
    events = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM job_events "
            "WHERE job_id=? AND unit_key IS NOT NULL AND unit_key!=? ORDER BY id",
            (job_id, FINAL_PROMOTION_UNIT_KEY),
        )
    )
    return units, attempts, events


def test_real_child_crash_rolls_back_promotion_and_requires_explicit_recovery(
    tmp_path,
):
    database_path = tmp_path / "ledger.sqlite3"
    conn = open_database(database_path)
    apply_migrations(conn)
    fixture = create_crash_promotion_fixture(conn)
    jobs = JobStateService(conn)
    jobs.begin_unit(fixture.job_id, FINAL_PROMOTION_UNIT_KEY)

    old_current = CurrentResultService(conn).get_scope(fixture.scope_id)
    old_week = HeatmapService(conn).read_scope(
        fixture.scope_id,
        HeatmapGranularity.WEEK,
    )
    old_month = HeatmapService(conn).read_scope(
        fixture.scope_id,
        HeatmapGranularity.MONTH,
    )
    old_scope = AnalysisRepository(conn).get_scope(fixture.scope_id)
    before = _rollback_snapshot(conn)
    upstream_before = _upstream_snapshot(conn, fixture.job_id)
    assert old_current.source_run_id == fixture.old_run.prepared.run_id
    assert old_scope.status is ScopeStatus.RUNNING
    assert jobs.status(fixture.job_id) is JobStatus.RUNNING
    assert jobs.unit(
        fixture.job_id, FINAL_PROMOTION_UNIT_KEY
    ).status is UnitStatus.RUNNING
    conn.close()

    worker = Path(__file__).with_name("crash_promotion_worker.py")
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(database_path),
            str(fixture.run_id),
            str(fixture.projection_batch_id),
        ],
        shell=False,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 91
    assert elapsed < 20
    assert completed.stdout == ""
    assert completed.stderr == ""

    conn = open_database(database_path)
    try:
        assert tuple(tuple(row) for row in conn.execute("PRAGMA integrity_check")) == (
            ("ok",),
        )
        assert _rollback_snapshot(conn) == before
        assert CurrentResultService(conn).get_scope(fixture.scope_id) == old_current
        assert HeatmapService(conn).read_scope(
            fixture.scope_id,
            HeatmapGranularity.WEEK,
        ) == old_week
        assert HeatmapService(conn).read_scope(
            fixture.scope_id,
            HeatmapGranularity.MONTH,
        ) == old_month
        assert AnalysisRepository(conn).get_scope(fixture.scope_id) == old_scope

        jobs = JobStateService(conn)
        artifacts = dict(fixture.artifact_hashes)
        with pytest.raises(DomainError) as interrupted:
            jobs.resume(fixture.job_id, artifacts)
        assert interrupted.value.code == "INTERRUPTED_RECOVERY_REQUIRED"
        assert _rollback_snapshot(conn) == before

        recovery = jobs.recover_interrupted(fixture.job_id, artifacts)
        assert recovery.reused_unit_keys == tuple(artifacts)
        assert recovery.pending_unit_keys == (FINAL_PROMOTION_UNIT_KEY,)
        assert recovery.next_unit_key == FINAL_PROMOTION_UNIT_KEY
        assert jobs.unit(
            fixture.job_id,
            FINAL_PROMOTION_UNIT_KEY,
        ).status is UnitStatus.PENDING
        interrupted_attempts = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT attempt_no, result_status, output_hash, error_code "
                "FROM job_unit_attempts WHERE job_id=? AND unit_key=? ORDER BY id",
                (fixture.job_id, FINAL_PROMOTION_UNIT_KEY),
            )
        )
        assert interrupted_attempts == ((1, "interrupted", None, None),)
        assert _upstream_snapshot(conn, fixture.job_id) == upstream_before

        resumed = jobs.resume(fixture.job_id, artifacts)
        assert resumed == recovery
        assert _upstream_snapshot(conn, fixture.job_id) == upstream_before

        jobs.begin_unit(fixture.job_id, FINAL_PROMOTION_UNIT_KEY)
        current = CurrentResultService(conn).promote_completed_run(
            fixture.run_id,
            fixture.projection_batch_id,
        )

        assert current.scope_id == fixture.scope_id
        assert current.source_run_id == fixture.run_id
        assert current.projection_batch_id == fixture.projection_batch_id
        assert current.forecast_count == 1
        assert jobs.status(fixture.job_id) is JobStatus.SUCCEEDED
        final = jobs.unit(fixture.job_id, FINAL_PROMOTION_UNIT_KEY)
        assert final.status is UnitStatus.SUCCESS
        assert final.attempt_count == 2
        assert _upstream_snapshot(conn, fixture.job_id) == upstream_before
        scope = AnalysisRepository(conn).get_scope(fixture.scope_id)
        assert scope.status is ScopeStatus.CURRENT
        changed = HeatmapService(conn).read_scope(
            fixture.scope_id,
            HeatmapGranularity.MONTH,
        ).cell(
            "Synthetic Crash Recovery Subject",
            Asset.NIKKEI_225,
            "2026-10",
        )
        assert changed.primary_direction is DirectionKind.DOWN
        assert tuple(
            row[0]
            for row in conn.execute(
                "SELECT status FROM analysis_run_events "
                "WHERE run_id=? ORDER BY id",
                (fixture.run_id,),
            )
        ) == ("started", "transport_validated", "accepted")
        final_attempts = tuple(
            row[0]
            for row in conn.execute(
                "SELECT result_status FROM job_unit_attempts "
                "WHERE job_id=? AND unit_key=? ORDER BY id",
                (fixture.job_id, FINAL_PROMOTION_UNIT_KEY),
            )
        )
        assert final_attempts == ("interrupted", "success")
    finally:
        conn.close()

from __future__ import annotations

import os
import sys
from pathlib import Path

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService


def _positive_id(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or str(parsed) != value:
        raise ValueError("positive canonical identifier required")
    return parsed


def main() -> None:
    if len(sys.argv) != 4:
        raise ValueError("database path, run id, and projection batch id required")
    database_path = Path(sys.argv[1])
    run_id = _positive_id(sys.argv[2])
    projection_batch_id = _positive_id(sys.argv[3])
    conn = open_database(database_path)
    real_insert = HeatmapService._insert_cells

    def insert_then_crash(self, cells) -> None:
        real_insert(self, cells)
        os._exit(91)

    HeatmapService._insert_cells = insert_then_crash
    CurrentResultService(conn).promote_completed_run(
        run_id,
        projection_batch_id,
    )
    raise AssertionError("synthetic crash point was not reached")


if __name__ == "__main__":
    main()

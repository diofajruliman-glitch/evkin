"""Add controlled Absence calculation batch and canonical-pointer metadata."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_cutover import ensure_absence_calculation_schema


def main() -> None:
    db_path = os.environ.get("EVKIN_DUCKDB_PATH") or str(
        Path(__file__).resolve().parents[1] / "data" / "EVKIN_MASTER.duckdb"
    )
    with duckdb.connect(db_path) as conn:
        ensure_absence_calculation_schema(conn)
    print(f"Migration 050 applied: Absence calculation batches at {db_path}")


if __name__ == "__main__":
    main()

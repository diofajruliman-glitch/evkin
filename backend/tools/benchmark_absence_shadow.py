"""Microbenchmark for the isolated Python absence shadow compute_record path."""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_shadow_engine import (
    BASELINE_ID,
    _calculation_record,
    build_context,
    compute_record,
    load_json,
)


FIXTURE = ROOT / "backend/tests/fixtures/absence_js_golden_cases_2026_09.json"


def make_rows(size: int):
    rows = []
    employees = []
    for index in range(size):
        wali = index % 5 == 0
        nip = str(index).zfill(8)
        minute = index % 120
        item = {
            "id": f"bench-{index}", "nip": nip, "date": "2026-09-01",
            "in": "20:00" if wali else f"{(451 + minute) // 60:02d}:{(451 + minute) % 60:02d}",
            "out": "04:00" if wali else f"{(960 - index % 90) // 60:02d}:{(960 - index % 90) % 60:02d}",
            "job": "Wali Asuh" if wali else "Guru SR", "source_format": "wali" if wali else "standard",
            "cross_day": wali,
        }
        rows.append(_calculation_record(item))
        employees.append({"nip": nip, "nama": f"Fixture {index}", "jabatan": item["job"]})
    return rows, employees


def measure(size: int, config: dict) -> dict:
    rows, employees = make_rows(size)
    context = build_context(config, employees)
    started = time.perf_counter()
    checksum = sum(float(compute_record(row, context, "original")["totalHarian"]) for row in rows)
    elapsed = time.perf_counter() - started
    return {
        "records": size,
        "elapsed_ms": round(elapsed * 1000, 3),
        "records_per_second": round(size / elapsed),
        "checksum": checksum,
    }


def main() -> None:
    config = load_json(FIXTURE)["config"]
    measure(1000, config)
    result = {
        "baseline": BASELINE_ID,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "samples": [measure(size, config) for size in (1000, 10000, 35000)],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

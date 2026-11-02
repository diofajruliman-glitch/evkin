"""Isolated DuckDB benchmark for the opt-in CSV eAbsensi special importer."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
from starlette.datastructures import UploadFile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_import import (
    AbsenceImportService,
    CSV_EABSENSI_ENGINE_VERSION,
    CSV_EABSENSI_SOURCE_FORMAT,
    ImportAuthority,
    ImportAuthorityConfig,
)
from backend.tools.benchmark_absence_import import process_rss, sample_rss


HEADERS = [
    "tgl_finger", "NM_PEG", "nip", "jnshadir", "NM_UNIT_ES2", "NM_KANTOR",
    "absen_pagi", "absen_sore", "latitude", "longitude", "latitude_sore", "longitude_sore",
]
CONFIG = json.dumps({
    "holidays": [], "jabatanMaster": [], "jamDasar": {}, "flexi": {},
    "keterlambatanTiers": [], "pulangCepatTiers": [], "jenisNoPotongan": [],
    "leavePeriods": [], "_workflow": {"timeMode": "original"},
})


def _csv_row(employee: int, slot: int, kind: int) -> list[str]:
    day_offsets = (
        (0, 0, 1, 1) if kind == 1 else
        (0, 1, 1, 2) if kind in (2, 4) else
        (slot, slot, slot, slot)
    )
    source_date = date(2026, 8, 1) + timedelta(days=employee % 31 + day_offsets[slot])
    source_day = source_date.isoformat()
    kind_name = "Upacara" if employee % 29 == 0 else "Bekerja di Kantor"
    check_in = check_out = ""
    if kind == 0:
        check_in, check_out = f"{source_day} 07:30", f"{source_day} 16:00"
    elif kind == 1:
        if slot in (0, 2):
            check_in = f"{source_day} 07:{31 + slot // 2:02d}"
        else:
            check_out = f"{source_day} 16:{1 + slot // 2:02d}"
    elif kind == 2:
        if slot == 0:
            check_in = f"{source_day} 22:30"
        elif slot == 1:
            check_out = f"{source_day} 06:15"
        elif slot == 2:
            check_in = f"{source_day} 22:30"
        else:
            check_out = f"{source_day} 06:15"
    elif kind == 3:
        check_out = f"{source_day} 06:{slot:02d}"
    elif kind == 4:
        if slot == 0:
            check_in = f"{source_day} 23:00"
        elif slot == 1:
            check_out = f"{source_day} 06:00"
        elif slot == 2:
            check_in = f"{source_day} 21:00"
        elif slot == 3:
            check_out = f"{source_day} 06:00"
    elif kind == 5:
        if slot == 0:
            check_in = f"{source_day} 07:45"
        elif slot == 2:
            check_out = f"{source_day} 16:15"
        elif slot == 3:
            check_in = f"{source_day} 08:15"
    return [
        source_day, f"Pegawai {employee:08d}", f"E{employee:08d}", kind_name,
        "Unit Benchmark", "Kantor Benchmark", check_in, check_out, "", "", "", "",
    ]


def write_eabsensi(path: Path, size: int) -> None:
    people = (size + 3) // 4
    previous: list[str] | None = None
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        for slot in range(4):
            for employee in range(people):
                source_row = slot * people + employee
                if source_row >= size:
                    break
                current = _csv_row(employee, slot, employee % 6)
                if source_row and source_row % 50_000 == 0 and previous is not None:
                    current = previous
                writer.writerow(current)
            previous = current


def write_monthly_eabsensi(path: Path, size: int, workforce: int = 32_000) -> None:
    """Write event-oriented monthly data: employee x day x IN/OUT."""
    monthly_capacity = workforce * 31 * 2
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        previous: list[str] | None = None
        for source_row in range(size):
            logical = source_row % monthly_capacity
            day = logical // (workforce * 2) + 1
            employee = (logical // 2) % workforce
            direction = logical % 2
            work_date = date(2026, 8, day)
            event_date = work_date
            check_in = check_out = ""
            if employee % 97 == 0 and day < 31:
                if direction == 0:
                    check_in = f"{work_date.isoformat()} 21:30:00"
                else:
                    event_date = work_date + timedelta(days=1)
                    check_out = f"{event_date.isoformat()} 06:45:00"
            elif employee % 89 == 0 and direction == 0:
                check_in = f"{work_date.isoformat()} 07:45:00"
            elif employee % 83 == 0 and direction == 1:
                check_out = f"{work_date.isoformat()} 16:15:00"
            elif direction == 0:
                check_in = f"{work_date.isoformat()} 07:30:00"
            else:
                check_out = f"{work_date.isoformat()} 16:00:00"
            current = [
                event_date.isoformat(), f"Pegawai {employee:08d}", f"E{employee:08d}",
                "Upacara" if employee % 101 == 0 else "Bekerja di Kantor",
                "Unit Benchmark", "Kantor Benchmark", check_in, check_out, "", "", "", "",
            ]
            if source_row and source_row % 50_000 == 0 and previous is not None:
                current = previous
            writer.writerow(current)
            previous = current


def benchmark_one(size: int, *, pair_cross_day: bool = False, monthly: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="evkin-eabsensi-uat-") as directory:
        root = Path(directory)
        source = root / f"eabsensi-{size}.csv"
        (write_monthly_eabsensi if monthly else write_eabsensi)(source, size)
        service = AbsenceImportService(
            root / "uat.duckdb", root / "uploads",
            ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1, csv_eabsensi_enabled=True),
            generation_reader=lambda: 1, generation_validator=lambda value: None,
        )
        stop = threading.Event()
        memory: dict[str, int] = {}
        baseline_rss = process_rss()
        sampler = threading.Thread(target=sample_rss, args=(stop, memory), daemon=True)
        sampler.start()
        started = time.perf_counter()
        try:
            with source.open("rb") as handle:
                saved = service.save_upload(
                    UploadFile(handle, filename=source.name), operator="isolated-benchmark",
                    source_format=CSV_EABSENSI_SOURCE_FORMAT, period="2026-08",
                    workflow_config_json=CONFIG, pair_cross_day=pair_cross_day,
                )
            uploaded_at = time.perf_counter()
            batch = service.stage(saved["import_id"])
            staged_at = time.perf_counter()
            staging_db_bytes = (root / "uat.duckdb").stat().st_size
            if batch["status"] != "READY":
                raise RuntimeError(
                    f"eAbsensi benchmark staging failed for {size} rows: "
                    f"{batch.get('error_code')} {batch.get('error_message')}"
                )
            committed = service.commit(batch["import_id"], period="2026-08", accept_invalid=True)
            finished = time.perf_counter()
            if committed["status"] != "COMMITTED":
                raise RuntimeError(f"eAbsensi benchmark commit failed: {committed}")
        finally:
            stop.set()
            sampler.join()
        elapsed = finished - started
        timings = batch.get("timings") or {}
        with duckdb.connect(root / "uat.duckdb", read_only=True) as conn:
            unique_nip, final_rows = conn.execute(
                "SELECT count(DISTINCT nip),count(*) FROM raw_import"
            ).fetchone()
        return {
            "engine": CSV_EABSENSI_ENGINE_VERSION,
            "raw_rows": size,
            "event_rows_after_dedupe": int(timings.get("unique_event_rows", 0)),
            "canonical_rows": int(timings.get("canonical_rows", 0)),
            "unique_nip": int(unique_nip),
            "final_raw_import_rows": int(final_rows),
            "pair_cross_day": pair_cross_day,
            "generator": "32000_employees_x_31_days_x_events" if monthly else "mixed_scale",
            "duplicate_source_rows": int(timings.get("duplicate_source_rows", 0)),
            "status": committed["status"],
            "source_stage_ms": timings.get("source_stage_ms"),
            "sort_ms": timings.get("sort_event_ms"),
            "pair_ms": timings.get("pair_ms"),
            "canonical_prepare_ms": timings.get("canonical_prepare_ms"),
            "stage_ms": timings.get("stage_validate_normalize_ms"),
            "upload_seconds": round(uploaded_at - started, 3),
            "commit_ms": committed.get("timings", {}).get("commit_ms"),
            "total_seconds": round(elapsed, 3),
            "rows_per_second": round(size / elapsed, 1) if elapsed else None,
            "peak_rss_mb": round(memory.get("peak", baseline_rss) / 1024 / 1024, 1),
            "rss_delta_mb": round((memory.get("peak", baseline_rss) - baseline_rss) / 1024 / 1024, 1),
            "file_bytes": source.stat().st_size,
            "staging_db_bytes": staging_db_bytes,
            "final_db_bytes": (root / "uat.duckdb").stat().st_size,
            "invalid_rows": int(batch.get("invalid_rows") or 0),
            "unpaired_rows": int(timings.get("check_in_only", 0) + timings.get("check_out_only", 0)),
            "cross_day_pairs": int(batch.get("cross_day_rows") or 0),
            "temporary_database_only": True,
            "production_database_touched": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="100000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cross-day", action="store_true")
    parser.add_argument("--monthly", action="store_true")
    args = parser.parse_args()
    results = []
    for value in args.sizes.split(","):
        size = int(value.strip())
        if size < 1:
            parser.error("sizes must be positive row counts")
        result = benchmark_one(size, pair_cross_day=args.cross_day, monthly=args.monthly)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {
        "engine": CSV_EABSENSI_ENGINE_VERSION,
        "production_database_touched": False,
        "results": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

"""Synthetic temp-DB benchmark for Backend Import V1 (never touches production)."""
from __future__ import annotations

import argparse
import ctypes
import csv
import io
import json
import tempfile
import threading
import time
from pathlib import Path

import duckdb
from openpyxl import Workbook
from starlette.datastructures import UploadFile

from backend.absence_import import (
    AbsenceImportService,
    ImportAuthority,
    ImportAuthorityConfig,
)


CONFIG = json.dumps({
    "holidays": [], "jabatanMaster": [], "jamDasar": {}, "flexi": {},
    "keterlambatanTiers": [], "pulangCepatTiers": [], "jenisNoPotongan": [],
    "leavePeriods": [], "_workflow": {"timeMode": "original"},
})
HEADERS = ["NIP", "Nama", "Jabatan", "Tanggal", "Jam Masuk", "Jam Pulang", "Zona", "Provinsi", "Jenis"]
WALI_HEADERS = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]


def write_csv(path: Path, size: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        for index in range(size):
            writer.writerow((f"N{index:09d}", "Pegawai", "Guru", f"2026-08-{index % 28 + 1:02d}",
                             "07:30", "16:00", ("WIB", "WITA", "WIT")[index % 3], "", ""))


def write_wali_csv(path: Path, event_count: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(WALI_HEADERS)
        for index in range(event_count):
            person = index // 2
            writer.writerow((
                f"2026-08-{person % 28 + 1:02d}", f"Pegawai {person}", f"W{person:09d}",
                "", "Wali", "07:30" if index % 2 == 0 else "",
                "" if index % 2 == 0 else "16:00",
            ))


def write_parquet(path: Path, size: int) -> None:
    with duckdb.connect() as conn:
        target = str(path).replace("'", "''")
        conn.execute("""
            COPY (SELECT 'N' || lpad(i::VARCHAR,9,'0') NIP,'Pegawai' Nama,'Guru' Jabatan,
                   '2026-08-' || lpad(((i % 28)+1)::VARCHAR,2,'0') Tanggal,
                   '07:30' "Jam Masuk",'16:00' "Jam Pulang",
                   CASE i % 3 WHEN 0 THEN 'WIB' WHEN 1 THEN 'WITA' ELSE 'WIT' END Zona,
                   '' Provinsi,'' Jenis FROM range(?) source(i))
            TO '""" + target + """' (FORMAT PARQUET, COMPRESSION ZSTD)
        """, [size])


def write_xlsx(path: Path, size: int) -> None:
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Absensi")
    sheet.append(HEADERS)
    for index in range(size):
        sheet.append((f"N{index:09d}", "Pegawai", "Guru", f"2026-08-{index % 28 + 1:02d}",
                      "07:30", "16:00", ("WIB", "WITA", "WIT")[index % 3], "", ""))
    book.save(path)


def process_rss() -> int:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    process = kernel.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return 0
    return int(counters.WorkingSetSize)


def sample_rss(stop: threading.Event, result: dict) -> None:
    peak = process_rss()
    while not stop.wait(.02):
        peak = max(peak, process_rss())
    result["peak"] = max(peak, process_rss())


def benchmark_one(root: Path, file_type: str, size: int, source_format: str = "standard") -> dict:
    extension = "csv" if file_type == "wali" else file_type
    source = root / f"source-{file_type}-{size}.{extension}"
    writer = {
        "csv": write_csv, "parquet": write_parquet, "xlsx": write_xlsx,
        "wali": write_wali_csv,
    }[file_type]
    writer(source, size)
    work = root / f"run-{file_type}-{size}"
    work.mkdir()
    service = AbsenceImportService(
        work / "uat.duckdb", work / "uploads",
        ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1),
        generation_reader=lambda: 1, generation_validator=lambda value: None,
    )
    baseline_rss = process_rss()
    stop = threading.Event(); memory = {}
    sampler = threading.Thread(target=sample_rss, args=(stop, memory), daemon=True); sampler.start()
    started = time.perf_counter()
    with source.open("rb") as handle:
        upload_started = time.perf_counter()
        batch = service.save_upload(
            UploadFile(handle, filename=source.name), operator="benchmark", source_format=source_format,
            period="2026-08", workflow_config_json=CONFIG,
        )
        upload_seconds = time.perf_counter() - upload_started
    stage_started = time.perf_counter()
    batch = service.stage(batch["import_id"])
    stage_seconds = time.perf_counter() - stage_started
    if batch["status"] != "READY":
        total_seconds = time.perf_counter() - started
        stop.set(); sampler.join()
        return {
            "format": file_type, "rows": size, "file_bytes": source.stat().st_size,
            "status": batch["status"], "error_code": batch.get("error_code"),
            "upload_seconds": round(upload_seconds, 4), "stage_seconds": round(stage_seconds, 4),
            "commit_seconds": None, "total_seconds": round(total_seconds, 4),
            "rows_per_second": None,
            "peak_rss_mb": round(memory.get("peak", baseline_rss) / 1024 / 1024, 1),
            "rss_delta_mb": round((memory.get("peak", baseline_rss) - baseline_rss) / 1024 / 1024, 1),
            "db_bytes": (work / "uat.duckdb").stat().st_size,
            "wali_phase_timings": {},
        }
    commit_started = time.perf_counter()
    batch = service.commit(batch["import_id"])
    commit_seconds = time.perf_counter() - commit_started
    total_seconds = time.perf_counter() - started
    stop.set(); sampler.join()
    return {
        "format": file_type, "rows": size, "file_bytes": source.stat().st_size,
        "status": batch["status"],
        "upload_seconds": round(upload_seconds, 4), "stage_seconds": round(stage_seconds, 4),
        "commit_seconds": round(commit_seconds, 4), "total_seconds": round(total_seconds, 4),
        "rows_per_second": round(size / total_seconds, 1),
        "peak_rss_mb": round(memory.get("peak", baseline_rss) / 1024 / 1024, 1),
        "rss_delta_mb": round((memory.get("peak", baseline_rss) - baseline_rss) / 1024 / 1024, 1),
        "db_bytes": (work / "uat.duckdb").stat().st_size,
        "wali_phase_timings": {
            key: batch["timings"][key] for key in (
                "source_stage_ms", "dedupe_event_build_ms", "sort_pair_ms",
                "merge_output_stage_ms",
            ) if key in batch.get("timings", {})
        },
    }


def sizes(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-sizes", default="10000,100000")
    parser.add_argument("--parquet-sizes", default="100000")
    parser.add_argument("--xlsx-sizes", default="10000")
    parser.add_argument("--wali-sizes", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="evkin-import-benchmark-") as directory:
        root = Path(directory)
        for file_type, requested, source_format in (
            ("csv", sizes(args.csv_sizes), "standard"),
            ("parquet", sizes(args.parquet_sizes), "standard"),
            ("xlsx", sizes(args.xlsx_sizes), "standard"),
            ("wali", sizes(args.wali_sizes), "wali"),
        ):
            for size in requested:
                results.append(benchmark_one(root, file_type, size, source_format))
                print(json.dumps(results[-1]), flush=True)
    report = {"engine": "ABSENCE_IMPORT_BACKEND_V1_2026_09", "production_db_touched": False, "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

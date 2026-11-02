"""Isolated, fixed-input performance investigation for Standard Import V1.

This diagnostic never opens the production database.  It can execute either
the current module or a read-only source snapshot stored as a Git blob so both
implementations receive the same input and harness.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

from starlette.datastructures import UploadFile

from backend.tools.benchmark_absence_import import process_rss, sample_rss


CONFIG = json.dumps({
    "holidays": [], "jabatanMaster": [], "jamDasar": {}, "flexi": {},
    "keterlambatanTiers": [], "pulangCepatTiers": [], "jenisNoPotongan": [],
    "leavePeriods": [], "_workflow": {"timeMode": "original"},
})
HEADERS = ["NIP", "Nama", "Jabatan", "Tanggal", "Jam Masuk", "Jam Pulang", "Zona", "Provinsi", "Jenis"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_source(path: Path, rows: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        for index in range(rows):
            writer.writerow((
                f"N{index:09d}", "Pegawai", "Guru", f"2026-08-{index % 28 + 1:02d}",
                "07:30", "16:00", ("WIB", "WITA", "WIT")[index % 3], "", "",
            ))


def load_engine(blob: str | None) -> tuple[Any, str]:
    if not blob:
        module = importlib.import_module("backend.absence_import")
        return module, sha256_file(Path(module.__file__))
    source = subprocess.check_output(["git", "cat-file", "blob", blob], text=True, encoding="utf-8")
    module_name = f"backend._absence_import_snapshot_{blob[:12]}"
    module = types.ModuleType(module_name)
    module.__file__ = f"git-blob:{blob}"
    module.__package__ = "backend"
    sys.modules[module_name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, hashlib.sha256(source.encode()).hexdigest()


def timed_wrapper(metrics: dict[str, float], name: str, function: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            metrics[name] = metrics.get(name, 0.0) + time.perf_counter() - started
            metrics[name + "_calls"] = metrics.get(name + "_calls", 0.0) + 1
    return wrapped


def instrument(module: Any, metrics: dict[str, float]) -> None:
    for attribute, name in (
        ("_sha256_file", "checksum"),
        ("_validate_headers", "column_mapping"),
        ("_canonical_stage_row", "normalization_hash_json"),
        ("_insert_stage_buffer", "staging_insert"),
        ("ensure_absence_import_schema", "schema_ensure"),
    ):
        setattr(module, attribute, timed_wrapper(metrics, name, getattr(module, attribute)))

    original_file_rows = module._file_rows

    def profiled_file_rows(path: Path, file_type: str):
        started = time.perf_counter()
        headers, rows = original_file_rows(path, file_type)
        metrics["source_open"] = metrics.get("source_open", 0.0) + time.perf_counter() - started

        def profiled_rows():
            iterator = iter(rows)
            while True:
                row_started = time.perf_counter()
                try:
                    row = next(iterator)
                except StopIteration:
                    metrics["source_read"] = metrics.get("source_read", 0.0) + time.perf_counter() - row_started
                    return
                metrics["source_read"] = metrics.get("source_read", 0.0) + time.perf_counter() - row_started
                yield row

        return headers, profiled_rows()

    module._file_rows = profiled_file_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--module-blob")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    ensure_source(args.source, args.rows)
    module, module_sha = load_engine(args.module_blob)
    metrics: dict[str, float] = {}
    instrument(module, metrics)

    run_root = Path(tempfile.mkdtemp(prefix="evkin-import-perf-isolated-"))
    stop = threading.Event()
    memory: dict[str, int] = {}
    sampler = threading.Thread(target=sample_rss, args=(stop, memory), daemon=True)
    baseline_rss = process_rss()
    sampler.start()
    try:
        service = module.AbsenceImportService(
            run_root / "uat.duckdb", run_root / "uploads",
            module.ImportAuthorityConfig(module.ImportAuthority.IMPORT_BACKEND_V1),
            generation_reader=lambda: 1, generation_validator=lambda value: None,
        )
        with service._connect() as conn:
            duckdb_config = {
                "version": conn.execute("SELECT version()").fetchone()[0],
                "threads": conn.execute("SELECT current_setting('threads')").fetchone()[0],
                "memory_limit": conn.execute("SELECT current_setting('memory_limit')").fetchone()[0],
                "temp_directory": conn.execute("SELECT current_setting('temp_directory')").fetchone()[0],
                "preserve_insertion_order": conn.execute("SELECT current_setting('preserve_insertion_order')").fetchone()[0],
            }
        total_started = time.perf_counter()
        with args.source.open("rb") as handle:
            upload_started = time.perf_counter()
            batch = service.save_upload(
                UploadFile(handle, filename=args.source.name), operator="performance-investigation",
                source_format="standard", period="2026-08", workflow_config_json=CONFIG,
            )
            upload_seconds = time.perf_counter() - upload_started
        upload_profile = dict(metrics)
        metrics.clear()
        stage_started = time.perf_counter()
        stage_cpu_started = time.process_time()
        batch = service.stage(batch["import_id"])
        stage_cpu_seconds = time.process_time() - stage_cpu_started
        stage_seconds = time.perf_counter() - stage_started
        stage_profile = dict(metrics)
        metrics.clear()
        commit_started = time.perf_counter()
        batch = service.commit(batch["import_id"])
        commit_seconds = time.perf_counter() - commit_started
        total_seconds = time.perf_counter() - total_started
    finally:
        stop.set()
        sampler.join()
        shutil.rmtree(run_root, ignore_errors=True)

    known = sum(stage_profile.get(name, 0.0) for name in (
        "checksum", "source_open", "source_read", "column_mapping",
        "normalization_hash_json", "staging_insert", "schema_ensure",
    ))
    result = {
        "implementation": "git_blob" if args.module_blob else "current",
        "module_blob": args.module_blob,
        "module_sha256": module_sha,
        "source": str(args.source),
        "source_sha256": sha256_file(args.source),
        "file_bytes": args.source.stat().st_size,
        "rows": args.rows,
        "status": batch["status"],
        "upload_seconds": upload_seconds,
        "stage_seconds": stage_seconds,
        "stage_cpu_seconds": stage_cpu_seconds,
        "stage_wall_to_cpu_ratio": stage_seconds / stage_cpu_seconds if stage_cpu_seconds else None,
        "commit_seconds": commit_seconds,
        "total_seconds": total_seconds,
        "rows_per_second": args.rows / total_seconds,
        "peak_rss_mb": memory.get("peak", baseline_rss) / 1024 / 1024,
        "rss_delta_mb": (memory.get("peak", baseline_rss) - baseline_rss) / 1024 / 1024,
        "profile_seconds": {
            "upload": upload_profile,
            "stage": {**stage_profile, "unattributed_stage_and_batch_overhead": max(0.0, stage_seconds - known)},
        },
        "duckdb": duckdb_config,
        "production_db_touched": False,
    }
    encoded = json.dumps(result, indent=2)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

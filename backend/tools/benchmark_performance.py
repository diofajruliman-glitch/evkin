"""Synthetic EVKIN query benchmark. Never opens the production database."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

import duckdb


def timed(callable_):
    started = time.perf_counter()
    value = callable_()
    return value, round((time.perf_counter() - started) * 1000, 3)


def benchmark(size: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="evkin-performance-") as folder:
        path = Path(folder) / "synthetic.duckdb"
        tracemalloc.start()
        with duckdb.connect(str(path)) as conn:
            _, import_ms = timed(lambda: conn.execute("""
                CREATE TABLE raw_import AS
                SELECT i source_row,printf('B-%06d', i // 10000) batch_id,
                       printf('N%08d', i % 40000) nip,
                       printf('Pegawai %08d', i % 40000) nama,
                       CASE WHEN i%2=0 THEN 'Staf' ELSE 'Guru' END jabatan,
                       CASE WHEN i%4=0 THEN '2026-08' ELSE '2026-09' END bulan,
                       CASE WHEN i%7=0 THEN 'INVALID' ELSE 'VALID' END status,
                       CAST(i%5 AS DOUBLE) potongan,CAST(1000000-(i%5)*1000 AS DOUBLE) nominal,
                       repeat('payload-', 25) raw_payload
                FROM range(?) t(i)
            """, [size]))
            _, processing_ms = timed(lambda: conn.execute("""
                CREATE TABLE rekap_sdm AS
                SELECT nip,bulan,count(*) jumlah_record,sum(potongan) total_potongan,
                       sum(nominal) total_nominal
                FROM raw_import GROUP BY nip,bulan
            """))
            summary, summary_ms = timed(lambda: conn.execute("""
                SELECT count(*),count(distinct nip),
                       count(*) FILTER (WHERE status='VALID'),
                       count(*) FILTER (WHERE status='INVALID'),
                       sum(potongan),sum(nominal) FROM raw_import
            """).fetchone())
            first_page, first_page_ms = timed(lambda: conn.execute("""
                SELECT nip,nama,jabatan,bulan,status,potongan,nominal
                FROM raw_import ORDER BY source_row LIMIT 100 OFFSET 0
            """).fetchall())
            _, search_ms = timed(lambda: conn.execute("""
                SELECT nip,nama,status FROM raw_import
                WHERE lower(nip) LIKE '%n00000010%' ORDER BY nip LIMIT 100
            """).fetchall())
            _, filter_ms = timed(lambda: conn.execute("""
                SELECT nip,nama,status FROM raw_import
                WHERE bulan='2026-08' AND status='INVALID' ORDER BY nip LIMIT 100
            """).fetchall())
            detail, detail_ms = timed(lambda: conn.execute(
                "SELECT * FROM raw_import WHERE source_row=? LIMIT 1", [size // 2]
            ).fetchone())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "rows": size, "database_bytes": path.stat().st_size,
            "import_ms": import_ms, "processing_ms": processing_ms,
            "summary_ms": summary_ms, "first_page_ms": first_page_ms,
            "search_ms": search_ms, "filter_ms": filter_ms, "detail_ms": detail_ms,
            "first_page_rows": len(first_page), "detail_found": detail is not None,
            "summary_total": int(summary[0]), "python_peak_bytes": peak,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-500k", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sizes = [10_000, 50_000, 100_000] + ([500_000] if args.include_500k else [])
    result = {"database": "temporary synthetic DuckDB", "results": [benchmark(n) for n in sizes]}
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

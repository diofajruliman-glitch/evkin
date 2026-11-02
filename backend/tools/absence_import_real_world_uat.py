"""Read-only-copy real-world import parity runner without reporting PII."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
from starlette.datastructures import UploadFile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_import import AbsenceImportService, ImportAuthority, ImportAuthorityConfig

RUNNER = ROOT / "backend/tests/absence_import_real_world_runner.cjs"
DEFAULT_SOURCE = ROOT / "backend/data/reset-backups/20260925T155149Z-902f7364251b40d18e1a26ddaacc696d/EVKIN_MASTER.duckdb"
CONFIG = json.dumps({"holidays": [], "jabatanMaster": [], "jamDasar": {}, "flexi": {},
                     "keterlambatanTiers": [], "pulangCepatTiers": [], "jenisNoPotongan": [],
                     "leavePeriods": [], "_workflow": {"timeMode": "original"}})


def issues(codes: list[str]) -> list[str]:
    mapping = {
        "INVALID_CHECK_IN": "Jam masuk tidak valid", "INVALID_CHECK_OUT": "Jam pulang tidak valid",
        "MISSING_CHECK_IN": "Jam masuk kosong", "MISSING_CHECK_OUT": "Jam pulang kosong",
    }
    return [mapping[code] for code in codes if code in mapping]


def canonical_json(row: dict) -> str:
    material = {
        "nip": row["nip"], "nama": row["nama"], "jabatan": row["jabatan"], "date": row["tanggal"],
        "out_date": row["tanggal_pulang"], "check_in": row["jam_masuk"], "check_out": row["jam_pulang"],
        "check_in_minutes": row["masuk_min"], "check_out_minutes": row["pulang_min"],
        "check_in_wib": row["masuk_norm_min"], "check_out_wib": row["pulang_norm_min"],
        "check_in_add": row["masuk_norm_min_add"], "check_out_add": row["pulang_norm_min_add"],
        "zone": row["zona"], "cross_day": row["cross_day"], "issues": issues(json.loads(row["error_codes"])),
    }
    return json.dumps(material, ensure_ascii=False, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    source_hash_before = file_sha256(source)
    with tempfile.TemporaryDirectory(prefix="evkin-import-real-world-") as directory:
        work = Path(directory)
        csv_path = work / "raw-import.csv"
        with duckdb.connect(str(source), read_only=True) as conn:
            raw_count = int(conn.execute("SELECT count(*) FROM raw_import").fetchone()[0])
            target = str(csv_path).replace("'", "''")
            conn.execute(f"COPY (SELECT * FROM raw_import ORDER BY rowid) TO '{target}' (HEADER,DELIMITER ',')")
        js = json.loads(subprocess.run(
            ["node", str(RUNNER), str(csv_path)], cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout)
        service = AbsenceImportService(
            work / "uat.duckdb", work / "uploads",
            ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1),
            generation_reader=lambda: 1, generation_validator=lambda value: None,
        )
        with csv_path.open("rb") as handle:
            batch = service.save_upload(UploadFile(handle, filename="real-world.csv"), operator="uat",
                                        source_format="standard", workflow_config_json=CONFIG)
        batch = service.stage(batch["import_id"])
        digest = hashlib.sha256()
        issue_rows = 0
        with service._connect() as conn:
            cursor = conn.execute("""
                SELECT nip,nama,jabatan,tanggal,tanggal_pulang,jam_masuk,jam_pulang,masuk_min,pulang_min,
                       masuk_norm_min,pulang_norm_min,masuk_norm_min_add,pulang_norm_min_add,zona,cross_day,error_codes
                FROM absence_import_staging WHERE import_id=? AND valid ORDER BY source_row
            """, [batch["import_id"]])
            names = [item[0] for item in cursor.description]
            rows = 0
            while True:
                values = cursor.fetchmany(8192)
                if not values:
                    break
                for value in values:
                    row = dict(zip(names, value)); digest.update((canonical_json(row) + "\n").encode())
                    if issues(json.loads(row["error_codes"])): issue_rows += 1
                    rows += 1
        python_result = {"rows": rows, "rejected_rows": batch["invalid_rows"], "issue_rows": issue_rows,
                         "cross_day": batch["cross_day_rows"], "duplicate_rows": batch["duplicate_rows"],
                         "sha256": digest.hexdigest()}
        report = {
            "source": "read-only historical backup", "source_sha256_stable": source_hash_before == file_sha256(source),
            "raw_rows": raw_count, "js": js, "python": python_result,
            "exact": js == python_result, "unexplained_mismatch": 0 if js == python_result else 1,
            "production_db_touched": False,
        }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

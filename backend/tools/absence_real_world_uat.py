"""Read-only, anonymized real-world Shadow UAT for EVKIN absence calculation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_shadow_engine import (  # noqa: E402
    BASELINE_ID,
    _flatten,
    build_context,
    compute_final_absensi,
    compute_legacy_sync,
    compute_record,
    compute_rekap,
    load_json,
    normalize_raw_rows,
    validate_golden_fingerprint,
    validate_source_fingerprint,
)


JS_RUNNER = ROOT / "backend/tests/real_world_absence_runner.cjs"
GOLDEN = ROOT / "backend/tests/fixtures/absence_js_golden_output_2026_09.json"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def freeze_database(
    db_path: str | Path, output_path: str | Path, *, anonymize_identifiers: bool = True
) -> dict[str, Any]:
    """Read a database read-only and emit a PII-minimized frozen JSON copy."""
    source = Path(db_path).resolve()
    salt = os.urandom(32)
    mapping: dict[str, str] = {}

    def anonymous(nip: Any) -> str:
        key = str(nip or "").strip()
        if not key:
            return ""
        if not anonymize_identifiers:
            mapping[key] = key
            return key
        if key not in mapping:
            mapping[key] = "SDM-" + hmac.new(salt, key.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        return mapping[key]

    with duckdb.connect(str(source), read_only=True) as connection:
        raw_cursor = connection.execute('SELECT * FROM "raw_import"')
        headers = [item[0] for item in raw_cursor.description]
        raw_values = raw_cursor.fetchall()
        rows = []
        for values in raw_values:
            row = dict(zip(headers, values))
            row["NIP"] = anonymous(row.get("NIP"))
            row["Nama"] = row["NIP"]
            if "Unit Kerja" in row:
                row["Unit Kerja"] = ""
            rows.append(row)

        config_cursor = connection.execute('SELECT * FROM "workflow_config" LIMIT 1')
        config_values = config_cursor.fetchone()
        if not config_values:
            raise RuntimeError("workflow_config tidak tersedia")
        config_row = dict(zip([item[0] for item in config_cursor.description], config_values))
        workflow = _parse_json(config_row.pop("_workflow", None), {})
        config = dict(config_row)
        for key, fallback in (
            ("holidays", []), ("jabatanMaster", []), ("jamDasar", {}), ("flexi", {}),
            ("keterlambatanTiers", []), ("pulangCepatTiers", []),
            ("jenisNoPotongan", []), ("leavePeriods", []),
        ):
            config[key] = _parse_json(config.get(key), fallback)
        for leave in config.get("leavePeriods") or []:
            leave["nip"] = anonymous(leave.get("nip"))

        master_rows = connection.execute(
            "SELECT nip,jabatan,coalesce(tukin_gross,tukin) AS tukin FROM master_data"
        ).fetchall()
        master = []
        for nip, job, gross in master_rows:
            original = str(nip or "").strip()
            if original not in mapping:
                continue
            anon = anonymous(original)
            master.append({"nip": anon, "nama": anon, "jabatan": job or "", "tukin": gross})

    frozen = {
        "baseline": BASELINE_ID,
        "source": {
            "kind": "read_only_duckdb_copy",
            "database_sha256": sha256_file(source),
            "database_size": source.stat().st_size,
            "table": "raw_import",
        },
        "headers": headers,
        "rows": rows,
        "master": master,
        "config": config,
        "time_mode": workflow.get("timeMode") or "original",
        "workflow_period": workflow.get("month"),
    }
    frozen["input_fingerprint"] = canonical_hash(frozen)
    Path(output_path).write_text(json.dumps(frozen, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return frozen


def python_pipeline(input_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    started_load = time.perf_counter()
    frozen = load_json(input_path)
    load_ms = (time.perf_counter() - started_load) * 1000
    timings: dict[str, float] = {"loading_ms": load_ms}

    started = time.perf_counter()
    records = normalize_raw_rows(frozen["headers"], frozen["rows"])
    timings["normalization_ms"] = (time.perf_counter() - started) * 1000
    context = build_context(frozen["config"], frozen["master"])

    started = time.perf_counter()
    daily = [compute_record(record, context, frozen.get("time_mode") or "original") for record in records]
    timings["harian_ms"] = (time.perf_counter() - started) * 1000

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in daily:
        key = f"{row.get('monthKey') or row.get('bulan') or str(row.get('tanggalKey') or '')[:7]}|{row.get('nip')}"
        groups.setdefault(key, []).append(row)
    started = time.perf_counter()
    rekap = [compute_rekap(group, context) for group in groups.values()]
    rekap.sort(key=lambda row: str(row.get("nama") or ""))
    timings["rekap_ms"] = (time.perf_counter() - started) * 1000

    master = {str(row.get("nip")): row for row in frozen["master"]}
    started = time.perf_counter()
    final_absensi = []
    final_skp = []
    final_combined = []
    for row in rekap:
        person = master.get(str(row.get("nip")))
        gross = person.get("tukin") if person else None
        final = compute_final_absensi(row["nip"], row["nama"], row["jabatan"], row["bulan"], gross, row["total"])
        skp, combined = compute_legacy_sync(final, 0)
        final_absensi.append(final)
        final_skp.append(skp)
        final_combined.append(combined)
    timings["final_ms"] = (time.perf_counter() - started) * 1000
    timings["legacy_sync_ms"] = 0.0

    normalization = [{
        "row_id": row["_id"], "nip": row["nip"], "tanggal": row["tanggal"]["key"],
        "masukRaw": row["masukRaw"], "pulangRaw": row["pulangRaw"],
        "masukMin": row["masukMin"], "pulangMin": row["pulangMin"],
        "masukNormMin": row["masukNormMin"], "pulangNormMin": row["pulangNormMin"],
        "masukNormMinAdd": row["masukNormMinAdd"], "pulangNormMinAdd": row["pulangNormMinAdd"],
        "zona": row["zona"], "jenis": row["jenis"], "jabatan": row["jabatan"],
        "crossDay": row["crossDay"], "issues": row["issues"], "dupFlag": row["dupFlag"],
    } for row in records]
    timings["total_ms"] = sum(timings.values())
    output = {
        "baseline": frozen["baseline"], "input_fingerprint": frozen["input_fingerprint"],
        "normalization": normalization, "daily": daily, "rekap": rekap,
        "final_absensi": final_absensi, "final_skp": final_skp,
        "final_combined": final_combined, "timings": timings,
        "resource": {},
    }
    started = time.perf_counter()
    Path(output_path).write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    output["timings"]["serialization_ms"] = (time.perf_counter() - started) * 1000
    return output


def _without_observation(output: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in output.items() if key not in ("timings", "resource")}


def _stage_map(output: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage in ("normalization", "daily"):
        key_name = "row_id" if stage == "normalization" else "_id"
        return {str(row.get(key_name)): row for row in output[stage]}
    if stage in ("rekap", "final_absensi", "final_skp", "final_combined"):
        return {f"{row.get('bulan')}|{row.get('nip')}": row for row in output[stage]}
    raise KeyError(stage)


def compare_real_world(js_output: dict[str, Any], python_output: dict[str, Any]) -> dict[str, Any]:
    stage_names = (
        ("NORMALIZATION", "normalization"), ("HARIAN", "daily"),
        ("REKAP", "rekap"), ("FINAL_ABSENSI", "final_absensi"),
        ("LEGACY_SYNC_CONTRACT", "final_skp"), ("LEGACY_SYNC_COMBINED", "final_combined"),
    )
    summaries = []
    details = []
    for label, key in stage_names:
        reference = _stage_map(js_output, key)
        candidate = _stage_map(python_output, key)
        fields = matches = mismatches = 0
        for record_key in sorted(set(reference) | set(candidate)):
            left = dict(_flatten(reference.get(record_key)))
            right = dict(_flatten(candidate.get(record_key)))
            for field in sorted(set(left) | set(right)):
                fields += 1
                js_value = left.get(field, {"__missing__": True})
                py_value = right.get(field, {"__missing__": True})
                numeric_equal = (
                    isinstance(js_value, (int, float)) and not isinstance(js_value, bool)
                    and isinstance(py_value, (int, float)) and not isinstance(py_value, bool)
                    and js_value == py_value
                )
                if (js_value == py_value and type(js_value) is type(py_value)) or numeric_equal:
                    matches += 1
                else:
                    mismatches += 1
                    details.append({
                        "record_key": record_key, "stage": label, "field": field,
                        "js": js_value, "python": py_value, "classification": "UNEXPLAINED",
                    })
        summaries.append({
            "stage": label, "rows": len(reference), "fields": fields,
            "matches": matches, "mismatches": mismatches,
        })
    mismatch_count = sum(row["mismatches"] for row in summaries)
    return {
        "stages": summaries, "total_fields": sum(row["fields"] for row in summaries),
        "matches": sum(row["matches"] for row in summaries), "mismatches": mismatch_count,
        "unexplained": mismatch_count, "details": details,
    }


def dataset_summary(frozen: dict[str, Any], python_output: dict[str, Any]) -> dict[str, Any]:
    normalization = python_output["normalization"]
    daily = python_output["daily"]
    return {
        "raw_rows": len(frozen["rows"]),
        "normalized_rows": len(normalization),
        "daily_rows": len(daily),
        "rekap_rows": len(python_output["rekap"]),
        "final_rows": len(python_output["final_absensi"]),
        "unique_sdm": len({row["nip"] for row in normalization}),
        "unique_dates": len({row["tanggal"] for row in normalization}),
        "periods": sorted({str(row["tanggal"])[:7] for row in normalization}),
        "timezone_distribution": dict(sorted(Counter(row["zona"] for row in normalization).items())),
        "job_distribution": dict(Counter(row["jabatan"] for row in normalization).most_common()),
        "incomplete_check_in": sum(row["masukMin"] is None for row in normalization),
        "incomplete_check_out": sum(row["pulangMin"] is None for row in normalization),
        "both_punches_missing": sum(row["masukMin"] is None and row["pulangMin"] is None for row in normalization),
        "cross_day": sum(bool(row["crossDay"]) for row in normalization),
        "duplicate_rows": sum(bool(row["dupFlag"]) for row in normalization),
        "invalid_rows": len(frozen["rows"]) - len(normalization),
        "wali_rows": sum("waliasuh" in _header_norm(row["jabatan"]) or "waliasrama" in _header_norm(row["jabatan"]) for row in normalization),
        "late_rows": sum((row.get("_lateMin") or 0) > 0 for row in daily),
        "early_rows": sum((row.get("_earlyMin") or 0) > 0 for row in daily),
        "flexi_compensated": sum("FLEXI TIME (kompensasi)" in row.get("flags", []) for row in daily),
        "holiday_or_weekend": sum(str(row.get("statusHariLibur", "")).startswith("Ya") for row in daily),
        "excused": sum(bool(row.get("excused")) for row in daily),
    }


def _header_norm(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def run(db_path: str | Path, workdir: str | Path) -> dict[str, Any]:
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    frozen_path = work / "real_world_frozen_anonymized.json"
    js_one_path = work / "js-output-1.json"
    js_two_path = work / "js-output-2.json"
    py_one_path = work / "python-output-1.json"
    py_two_path = work / "python-output-2.json"
    frozen = freeze_database(db_path, frozen_path)

    js_walls = []
    for output in (js_one_path, js_two_path):
        started = time.perf_counter()
        subprocess.run(["node", str(JS_RUNNER), str(frozen_path), str(output)], cwd=ROOT, check=True)
        js_walls.append((time.perf_counter() - started) * 1000)
    js_one, js_two = load_json(js_one_path), load_json(js_two_path)

    started = time.perf_counter()
    py_one = python_pipeline(frozen_path, py_one_path)
    py_wall_one = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    py_two = python_pipeline(frozen_path, py_two_path)
    py_wall_two = (time.perf_counter() - started) * 1000

    js_repeatable = _without_observation(js_one) == _without_observation(js_two)
    python_repeatable = _without_observation(py_one) == _without_observation(py_two)
    comparison = compare_real_world(js_one, py_one)
    summary = dataset_summary(frozen, py_one)
    input_fingerprints = {
        "frozen": frozen["input_fingerprint"],
        "javascript": js_one.get("input_fingerprint"),
        "python": py_one.get("input_fingerprint"),
    }
    identical_input = len(set(input_fingerprints.values())) == 1
    timezone_parity = []
    mismatch_nips = {detail["record_key"].split("|")[-1] for detail in comparison["details"]}
    for zone, count in summary["timezone_distribution"].items():
        zone_nips = {row["nip"] for row in py_one["normalization"] if row["zona"] == zone}
        timezone_parity.append({"timezone": zone, "rows": count, "mismatch": len(zone_nips & mismatch_nips)})
    for zone in ("WIB", "WITA", "WIT"):
        if zone not in summary["timezone_distribution"]:
            timezone_parity.append({"timezone": zone, "rows": 0, "mismatch": 0, "status": "NOT_PRESENT"})

    wali_nips = {
        row["nip"] for row in py_one["normalization"]
        if "waliasuh" in _header_norm(row["jabatan"])
        or "waliasrama" in _header_norm(row["jabatan"])
    }
    cross_day_nips = {
        row["nip"] for row in py_one["normalization"] if row.get("crossDay")
    }
    wali_shift_parity = [
        {
            "category": "WALI_ASUH_OR_ASRAMA",
            "rows": summary["wali_rows"],
            "mismatch": len(wali_nips & mismatch_nips),
        },
        {
            "category": "CROSS_DAY",
            "rows": summary["cross_day"],
            "mismatch": len(cross_day_nips & mismatch_nips),
        },
    ]

    result = {
        "baseline": BASELINE_ID,
        "source": frozen["source"],
        "input_fingerprint": frozen["input_fingerprint"],
        "input_fingerprints": input_fingerprints,
        "identical_input": identical_input,
        "summary": summary,
        "comparison": comparison,
        "timezone_parity": timezone_parity,
        "wali_shift_parity": wali_shift_parity,
        "repeatability": {"javascript": js_repeatable, "python": python_repeatable},
        "performance": {
            "javascript": js_one["timings"], "javascript_process_wall_ms": js_walls[0],
            "python": py_one["timings"], "python_process_wall_ms": py_wall_one,
            "repeat_run_wall_ms": {"javascript": js_walls[1], "python": py_wall_two},
        },
        "resource": {"javascript": js_one.get("resource", {}), "python": py_one.get("resource", {})},
        "fingerprints": {
            "javascript_source": validate_source_fingerprint(ROOT),
            "golden": validate_golden_fingerprint(GOLDEN),
        },
    }
    (work / "uat-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="DuckDB copy/snapshot; opened read-only")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--report")
    args = parser.parse_args()
    result = run(args.db, args.workdir)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(payload, encoding="utf-8")
    print(payload, end="")
    if not result["identical_input"]:
        return 4
    if not result["repeatability"]["javascript"] or not result["repeatability"]["python"]:
        return 3
    return 0 if result["comparison"]["unexplained"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

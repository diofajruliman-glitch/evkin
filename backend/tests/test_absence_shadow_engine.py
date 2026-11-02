import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.absence_shadow_engine import (
    BASELINE_ID,
    DuplicateAttendanceError,
    add_days,
    build_context,
    calculate_fixture,
    compare_outputs,
    compute_rekap,
    compute_record,
    day_of_week,
    js_math_round,
    load_json,
    merge_wali_work_dates,
    normalize_raw_rows,
    pair_wali_events,
    parse_date,
    validate_golden_fingerprint,
    validate_source_fingerprint,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend/tests/fixtures/absence_js_golden_cases_2026_09.json"
GOLDEN = ROOT / "backend/tests/fixtures/absence_cross_day_complete_output_2026_10.json"
HISTORICAL_GOLDEN = ROOT / "backend/tests/fixtures/absence_js_golden_output_2026_09.json"
RUNNER = ROOT / "backend/tests/legacy_calculation_runner.cjs"


def test_python_shadow_matches_every_frozen_golden_field():
    reference = load_json(GOLDEN)
    candidate = calculate_fixture(load_json(FIXTURE))
    report = compare_outputs(reference, candidate)
    assert report["baseline"] == BASELINE_ID
    assert report["total_fields"] == 8493
    assert report["exact_matches"] == 8493
    assert report["mismatches"] == 0
    assert report["unexplained_mismatches"] == 0
    assert report["details"] == []
    assert report["stages"] == [
        {"stage": "NORMALIZATION", "cases": 15, "fields": 528, "matches": 528, "mismatches": 0},
        {"stage": "HARIAN", "cases": 76, "fields": 3381, "matches": 3381, "mismatches": 0},
        {"stage": "REKAP", "cases": 76, "fields": 1502, "matches": 1502, "mismatches": 0},
        {"stage": "FINAL_ABSENSI", "cases": 76, "fields": 980, "matches": 980, "mismatches": 0},
        {"stage": "LEGACY_SYNC_PAYLOAD", "cases": 76, "fields": 2102, "matches": 2102, "mismatches": 0},
    ]


def test_cross_language_reference_and_fingerprints_are_still_frozen():
    js = subprocess.run(
        ["node", str(RUNNER), str(FIXTURE)], cwd=ROOT, check=True,
        capture_output=True, text=True,
    )
    assert json.loads(js.stdout) == load_json(GOLDEN)
    assert validate_source_fingerprint(ROOT)["valid"] is True
    assert validate_golden_fingerprint(HISTORICAL_GOLDEN)["valid"] is True


def test_js_rounding_and_legacy_date_compatibility_are_explicit():
    assert js_math_round(1.5) == 2
    assert js_math_round(2.5) == 3
    assert js_math_round(-1.5) == -1
    invalid_calendar_but_legacy_accepted = parse_date("2026-02-31")
    assert invalid_calendar_but_legacy_accepted["key"] == "2026-02-31"
    assert day_of_week(invalid_calendar_but_legacy_accepted) == day_of_week(parse_date("2026-03-03"))


def test_attendance_job_follows_latest_workbook_and_never_master():
    config = load_json(FIXTURE)["config"]
    context = build_context(config, [{"nip": "001", "nama": "Master Name", "jabatan": "Wali Asuh"}])
    source = {"nip": "001", "nama": "Workbook Name", "tanggal": parse_date("2026-09-01"),
              "masukMin": 480, "pulangMin": 960, "zona": "WIB"}
    regular = compute_record({**source, "jabatan": " Guru SR "}, context, "original")
    assert (regular["jabatan"], regular["metode"], regular["totalHarian"]) == ("Guru SR", "reguler", 0.5)
    duration = compute_record({**source, "jabatan": "Wali Asuh"}, context, "original")
    assert (duration["jabatan"], duration["metode"], duration["totalHarian"]) == ("Wali Asuh", "durasi", 0)
    empty = compute_record({**source, "jabatan": " "}, context, "original")
    assert empty["jabatan"] == "Belum Ditentukan"
    assert empty["metode"] == "reguler"


def test_wali_pairing_is_inclusive_at_18_hours_and_rejects_18h01():
    date = parse_date("2026-09-01")
    next_date = add_days(date, 1)
    at_boundary = pair_wali_events([
        {"nip": "A", "date": date, "time": 600},
        {"nip": "A", "date": next_date, "time": 240},
    ])
    assert at_boundary[0]["elapsed_minutes"] == 1080
    over_boundary = pair_wali_events([
        {"nip": "B", "date": date, "time": 600},
        {"nip": "B", "date": next_date, "time": 241},
    ])
    assert len(over_boundary) == 2
    assert over_boundary[0]["out"] is None


def test_wali_work_date_merge_uses_earliest_in_and_latest_absolute_out():
    date = parse_date("2026-09-01")
    next_date = add_days(date, 1)
    rows = [
        {"nip": "A", "tanggal": date, "masukMin": 1200, "pulangMin": 60,
         "masukAt": "2026-09-01T20:00", "pulangAt": "2026-09-02T01:00",
         "tanggalMasukObj": date, "tanggalPulangObj": next_date, "issues": []},
        {"nip": "A", "tanggal": date, "masukMin": 1140, "pulangMin": 240,
         "masukAt": "2026-09-01T19:00", "pulangAt": "2026-09-02T04:00",
         "tanggalMasukObj": date, "tanggalPulangObj": next_date, "issues": []},
    ]
    merged = merge_wali_work_dates(rows)
    assert len(merged) == 1
    assert merged[0]["masukMin"] == 1140
    assert merged[0]["pulangMin"] == 240
    assert merged[0]["crossDay"] is True
    assert merged[0]["shiftFlag"] == "SHIFT LINTAS HARI"


def test_duplicate_rekap_error_is_not_silently_accepted():
    fixture = load_json(FIXTURE)
    output = calculate_fixture(fixture)
    duplicate = next(row for row in output["cases"] if row["id"] == "ABS-DQ-003")
    assert duplicate["rekap"] is None
    assert "duplikat" in duplicate["rekap_error"].lower()


def test_real_world_raw_import_normalization_preserves_legacy_aliases_and_cross_day():
    headers = [
        "Tanggal", "NIP", "Nama", "Jenis SDM", "Jabatan", "Unit Kerja",
        "Status Kehadiran", "Kolom8", "Jam Datang", "Jam Pulang",
    ]
    rows = [{
        "Tanggal": "2026-08-31", "NIP": "ANON-1", "Nama": "ANON-1",
        "Jenis SDM": "Wali Asuh", "Jabatan": "Wali Asuh", "Unit Kerja": "",
        "Status Kehadiran": "Wali Asuh/Asrama SR", "Kolom8": "",
        "Jam Datang": "20:00", "Jam Pulang": "04:00",
    }]
    normalized = normalize_raw_rows(headers, rows)
    assert len(normalized) == 1
    assert normalized[0]["nip"] == "ANON-1"
    assert normalized[0]["tanggal"]["key"] == "2026-08-31"
    assert normalized[0]["masukMin"] == 20 * 60
    assert normalized[0]["pulangMin"] == 4 * 60
    assert normalized[0]["crossDay"] is True
    assert normalized[0]["issues"] == []

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

def test_wali_asuh_missing_punch_is_only_assessed_against_monthly_workday_target():
    config = load_json(FIXTURE)["config"]
    config.update(effectiveDaysMode="manual", effectiveDaysManual=2)
    context = build_context(config)
    def day(date, check_in, check_out):
        return compute_record({"nip": "001", "jabatan": "Wali Asuh", "tanggal": parse_date(date),
                               "masukMin": check_in, "pulangMin": check_out, "zona": "WIB"},
                              context, "original")
    rows = [day("2026-09-01", 480, None), day("2026-09-06", None, 360), day("2026-09-02", None, None)]
    assert rows[0]["statusLengkap"] == "Tidak Lengkap"
    assert all(row["totalHarian"] == 0 for row in rows)
    result = compute_rekap(rows, context)
    assert (result["hariAdaAbsensi"], result["hariKurang"], result["total"]) == (2, 0, 0)
    config["effectiveDaysManual"] = 3
    result = compute_rekap(rows, context)
    assert (result["hariAdaAbsensi"], result["hariKurang"], result["total"]) == (2, 1, 3)


@pytest.mark.parametrize(("job", "method"), [("WALI ASUH", "durasi"), ("Guru SR", "reguler")])
def test_undated_nonattendance_roster_uses_monthly_deficit_without_inventing_daily_dates(job, method):
    config = load_json(FIXTURE)["config"]
    config.update(effectiveDaysMode="manual", effectiveDaysManual=22)
    context = build_context(config, [{"nip": "001", "jabatan": "Other Master Job"}])
    result = compute_rekap([], context, roster={
        "nip": "001", "nama": "Source Name", "jabatan": job, "bulan": "2026-10",
    })
    assert (result["jabatan"], result["metode"]) == (job, method)
    assert result["jumlahRecord"] == result["jumlahHariAbsen"] == result["hariAdaAbsensi"] == 0
    assert (result["hariEfektif"], result["hariKurang"]) == (22, 22)
    assert result["potTidakLengkap"] == 0
    assert result["potHariKurang"] == result["totalRaw"] == result["total"] == 66
    assert "tanggalKey" not in result


def test_undated_nonattendance_roster_respects_registered_leave_and_percentage_cap():
    config = load_json(FIXTURE)["config"]
    config.update(effectiveDaysMode="manual", effectiveDaysManual=40,
                  leavePeriods=[{"nip": "001", "mulai": "2026-10-01", "selesai": "2026-10-02", "jenis": "Cuti"}])
    result = compute_rekap([], build_context(config), roster={
        "nip": "001", "nama": "Source Name", "jabatan": "Wali Asuh", "bulan": "2026-10",
    })
    assert (result["hariAdaAbsensi"], result["hariKurang"]) == (2, 38)
    assert result["totalRaw"] == 114 and result["total"] == 100


def test_calculate_source_preserves_source_jobs_and_uses_only_matching_nip_for_gross():
    from backend.absence_cutover import calculate_source, validate_results

    config = load_json(FIXTURE)["config"]
    config.update(effectiveDaysMode="manual", effectiveDaysManual=2)
    source = {
        "period": "2026-10", "config": config, "time_mode": "original",
        "records": [{"nip": "PRESENT", "nama": "Source Present", "jabatan": "Wali Asuh",
                     "tanggal": parse_date("2026-10-01"), "masukMin": 480, "pulangMin": None,
                     "zona": "WIB"}],
        "nonattendance_records": [
            {"nip": "PRESENT", "nama": "Source Present", "jabatan": "Wali Asuh", "period": "2026-10"},
            {"nip": "ABSENT", "nama": "Source Absent", "jabatan": "Guru SR", "period": "2026-10"},
            {"nip": "ABSENT", "nama": "Source Absent", "jabatan": "Guru SR", "period": "2026-10"},
            {"nip": "NO-MASTER", "nama": "No Master", "jabatan": "Wali Asuh", "period": "2026-10"},
        ],
        "master": [
            {"nip": "PRESENT", "nama": "Master must not replace source", "jabatan": "Guru SR", "tukin": 2_000_000},
            {"nip": "ABSENT", "jabatan": "Wali Asuh", "tukin": 5_000_000},
            {"nip": "UNRELATED", "nama": "No Master", "tukin": 9_000_000},
        ],
    }
    results = calculate_source(source)
    validate_results("2026-10", source, results)
    assert len(results["daily"]) == 1 and len(results["rekap"]) == 3
    assert results["daily"][0]["nama"] == "Source Present"
    by_nip = {row["nip"]: row for row in results["rekap"]}
    assert (by_nip["PRESENT"]["metode"], by_nip["PRESENT"]["hariKurang"], by_nip["PRESENT"]["total"]) == ("durasi", 1, 3)
    assert (by_nip["ABSENT"]["jabatan"], by_nip["ABSENT"]["metode"], by_nip["ABSENT"]["total"]) == ("Guru SR", "reguler", 6)
    final = {row["nip"]: row for row in results["final_absensi"]}
    assert (final["PRESENT"]["tukin"], final["PRESENT"]["nominal"]) == (2_000_000, 18_000)
    assert (final["ABSENT"]["tukin"], final["ABSENT"]["nominal"]) == (5_000_000, 90_000)
    assert final["NO-MASTER"]["tukin"] is None and final["NO-MASTER"]["nominal"] is None

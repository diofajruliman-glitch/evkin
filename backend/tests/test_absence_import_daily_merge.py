from copy import deepcopy

import pytest

from backend.absence_cutover import merge_import_attendance_records
from backend.absence_shadow_engine import (
    detect_import_columns,
    normalize_raw_row,
    parse_date,
)


HEADERS = ["NIP", "Nama", "Jabatan", "Tanggal", "Tanggal Pulang", "Jam Masuk", "Jam Pulang", "Zona", "Jenis"]
MAPPING = detect_import_columns(HEADERS)


def record(identifier, day="2026-10-31", check_in="22:00", check_out="", out_day="", zone="WIB", **extra):
    raw = dict(zip(HEADERS, ["001", "Nama Pertama", "Wali Asrama", day, out_day, check_in, check_out, zone, "Jenis Pertama"]))
    raw.update(extra)
    return normalize_raw_row(raw, HEADERS, MAPPING, identifier)


def test_duplicate_punches_preserve_first_source_metadata_and_all_source_ids():
    first = record("first", check_out="23:00")
    later = record("later", check_out="23:00", Nama="Nama Kedua", Jabatan="Wali Asuh", Jenis="Jenis Kedua")
    first["dupFlag"] = later["dupFlag"] = True
    original = deepcopy([first, later])

    merged, = merge_import_attendance_records([first, later])

    assert (merged["_id"], merged["nama"], merged["jabatan"], merged["jenis"]) == (
        "first", "Nama Pertama", "Wali Asrama", "Jenis Pertama",
    )
    assert merged["sourceRecordIds"] == ["first", "later"]
    assert merged["dupFlag"] is False
    assert (merged["masukMin"], merged["pulangMin"]) == (1320, 1380)
    assert [first, later] == original


@pytest.mark.parametrize("zone,offset", [("WIB", 0), ("WITA", 60), ("WIT", 120)])
def test_partial_punches_merge_across_month_boundary_and_refresh_both_time_modes(zone, offset):
    first = record("in", zone=zone)
    checkout = record("out", check_in="", check_out="07:00", out_day="2026-11-01", zone="WIB")

    merged, = merge_import_attendance_records([first, checkout])

    assert merged["tanggal"]["key"] == "2026-10-31"
    assert merged["tanggalMasukObj"]["key"] == "2026-10-31"
    assert merged["tanggalPulangObj"]["key"] == "2026-11-01"
    assert merged["crossDay"] is True
    assert merged["tanggalPulang"] == "01/11/2026"
    assert (merged["masukMin"], merged["pulangMin"]) == (1320, 420)
    assert (merged["masukNormMin"], merged["pulangNormMin"]) == ((1320 - offset) % 1440, (420 - offset) % 1440)
    assert (merged["masukNormMinAdd"], merged["pulangNormMinAdd"]) == ((1320 + offset) % 1440, (420 + offset) % 1440)
    assert merged["issues"] == []


def test_merge_uses_earliest_in_and_latest_absolute_out_at_year_boundary():
    evening = record("evening", day="2026-12-31", check_in="21:00", check_out="23:00")
    night = record("night", day="2026-12-31", check_in="19:00", check_out="04:00", out_day="2027-01-01")

    merged, = merge_import_attendance_records([evening, night])

    assert (merged["masukMin"], merged["pulangMin"]) == (1140, 240)
    assert merged["masukRaw"] == "19:00:00"
    assert merged["pulangRaw"] == "04:00:00"
    assert merged["tanggalPulangObj"] == parse_date("2027-01-01")


def test_cross_day_without_explicit_checkout_date_uses_existing_next_day_flag():
    first = record("in", check_in="20:00")
    night = record("night", check_in="21:00", check_out="04:00")
    assert night["crossDay"] is True

    merged, = merge_import_attendance_records([first, night])

    assert merged["tanggalPulangObj"] == parse_date("2026-11-01")
    assert merged["crossDay"] is True


def test_no_punch_or_missing_work_date_does_not_fabricate_attendance():
    blank = record("blank", check_in="")
    duplicate_blank = record("duplicate-blank", check_in="")
    undated = {"_id": "undated", "nip": "001", "tanggal": None, "masukMin": None, "pulangMin": None}

    merged, unchanged = merge_import_attendance_records([blank, duplicate_blank, undated])

    assert merged["masukMin"] is merged["pulangMin"] is None
    assert merged["tanggalMasukObj"] is merged["tanggalPulangObj"] is None
    assert merged["tanggalPulang"] == ""
    assert merged["crossDay"] is False
    assert merged["issues"] == ["Jam masuk kosong", "Jam pulang kosong"]
    assert unchanged == undated


def test_unmerged_days_keep_duplicate_warning_and_other_validation_issues_survive_merge():
    single = record("single", day="2026-10-30")
    single["dupFlag"] = True
    first = record("first")
    first["issues"].append("Perlu verifikasi sumber")
    second = record("second", check_in="", check_out="23:00")

    unmerged, merged = merge_import_attendance_records([single, first, second])

    assert unmerged["dupFlag"] is True
    assert unmerged["sourceRecordIds"] == ["single"]
    assert merged["issues"] == ["Perlu verifikasi sumber"]


@pytest.mark.parametrize("checkout,expected_day", [
    ("05:59", "2026-10-01"),
    ("06:00", "2026-09-30"),
    ("08:59:59", "2026-09-30"),
    ("09:00", "2026-10-01"),
    ("16:00", "2026-10-01"),
])
def test_checkout_only_previous_day_applies_only_to_hours_six_through_eight(checkout, expected_day):
    source = record("checkout-only", day="", check_in="", check_out=checkout, out_day="2026-10-01")

    merged, = merge_import_attendance_records([source])

    assert merged["tanggal"]["key"] == expected_day
    assert merged["tanggalPulang"] == "01/10/2026"
    assert merged["masukMin"] is None
    assert merged["pulangMin"] is not None
    assert merged["crossDay"] is (expected_day == "2026-09-30")
    assert merged["sourceRecordIds"] == ["checkout-only"]

def test_equal_morning_punch_completes_previous_open_shift():
    arrival = record("in", day="2026-10-11", check_in="14:32")
    echo = record("echo", day="2026-10-12", check_in="06:35", check_out="06:35", out_day="2026-10-12")
    merged, = merge_import_attendance_records([echo, arrival])
    assert merged["tanggal"]["key"] == "2026-10-11"
    assert merged["tanggalPulangObj"]["key"] == "2026-10-12"
    assert (merged["masukMin"], merged["pulangMin"], merged["crossDay"]) == (872, 395, True)
    assert merged["sourceRecordIds"] == ["in", "echo"]


def test_equal_morning_punch_without_prior_arrival_is_incomplete():
    echo = record("echo", check_in="06:35", check_out="06:35", out_day="2026-10-31")
    merged, = merge_import_attendance_records([echo])
    assert merged["masukMin"] is None
    assert merged["masukNormMin"] is None
    assert merged["pulangMin"] == 395
    assert "Jam masuk kosong" in merged["issues"]

from pathlib import Path

from backend.tools.audit_calculation_parity import DEFAULT_FIXTURE, audit, run_legacy


def test_parity_fixture_covers_required_legacy_risks():
    ids = {item["id"] for item in __import__("json").loads(
        Path(DEFAULT_FIXTURE).read_text(encoding="utf-8")
    )["cases"]}
    assert {
        "normal", "late_31_minutes", "missing_in", "missing_out",
        "cross_day_wali", "short_wali", "holiday_missing", "weekend_wali",
        "null_empty", "duplicate", "timezone_wita", "flexi_compensated",
        "free_kind", "rounding_rupiah", "skp_approved", "skp_not_final",
    } <= ids


def test_parity_harness_uses_exact_rupiah_and_exposes_current_blockers():
    result = audit(DEFAULT_FIXTURE)
    assert result["comparison_policy"] == {
        "money": "EXACT_INTEGER_RUPIAH",
        "allowed_delta_rupiah": 0,
        "floating_point_tolerance_for_money": False,
    }
    assert result["coverage"]["daily_backend_supported"] is False
    assert result["coverage"]["rekap_backend_supported"] is False
    assert result["parity_ready"] is False
    rounding = next(row for row in result["rows"] if row["id"] == "rounding_rupiah")
    assert rounding["money_exact"] is False
    assert "potongan_skp_rp" in rounding["legacy_non_integer_money_fields"]
    assert rounding["money_differences"]["potongan_skp_rp"]["delta_rupiah"] != 0
    draft = next(row for row in result["rows"] if row["id"] == "skp_not_final")
    assert draft["candidate_status"] == "HOLD"


def test_legacy_js_golden_edge_cases_are_stable():
    cases = {row["id"]: row for row in run_legacy(DEFAULT_FIXTURE)["cases"]}
    expected = {
        "normal": (0, "PERLU_VALIDASI", 0),
        "late_31_minutes": (1, "PERLU_VALIDASI", 1),
        "missing_in": (3, "LUPA_ABSEN_MASUK", 3),
        "missing_out": (3, "LUPA_ABSEN_PULANG", 3),
        "cross_day_wali": (0, "LINTAS_HARI", 0),
        "short_wali": (0, "LINTAS_HARI", 0),
        "holiday_missing": (0, "ABSEN_DI_LUAR_JADWAL", 0),
        "null_empty": (3, "LUPA_ABSEN_MASUK", 3),
    }
    for case_id, (daily_total, status, rekap_total) in expected.items():
        row = cases[case_id]
        assert row["daily"][0]["totalHarian"] == daily_total
        assert row["daily"][0]["statusAbsensi"] == status
        assert row["rekap"]["total"] == rekap_total
    assert cases["cross_day_wali"]["daily"][0]["durasiMin"] == 480
    assert cases["timezone_wita"]["daily"][0]["jamMasuk"] == "07:00"
    assert cases["free_kind"]["daily"][0]["excused"] is True
    assert cases["duplicate"]["rekap"] is None
    assert "duplikat" in cases["duplicate"]["rekap_error"].lower()

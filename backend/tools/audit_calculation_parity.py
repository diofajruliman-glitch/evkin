"""Audit-only legacy JS versus current pure Python Final TUKIN candidate.

This tool never opens the production database. Harian/Rekap backend parity is
reported as blocked because no pure backend candidate contract exists yet.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.final_tukin import calculate_final_tukin


DEFAULT_FIXTURE = ROOT / "backend" / "tests" / "fixtures" / "legacy_calculation_cases.json"
LEGACY_RUNNER = ROOT / "backend" / "tests" / "legacy_calculation_runner.cjs"
MONEY_FIELDS = ("tukin_bruto", "potongan_absen_rp", "potongan_skp_rp", "total_potongan", "tukin_dibayar")


def _money(value):
    """Serialize money without silently truncating a fractional legacy value."""
    if value is None:
        return None
    amount = Decimal(str(value))
    integral = amount.to_integral_value()
    return int(integral) if amount == integral else format(amount.normalize(), "f")


def _decimal_money(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _is_integer_money(value) -> bool:
    if value is None:
        return True
    amount = _decimal_money(value)
    return amount == amount.to_integral_value()


def run_legacy(fixture: Path) -> dict:
    completed = subprocess.run(
        ["node", str(LEGACY_RUNNER), str(fixture)], cwd=ROOT,
        check=True, capture_output=True, text=True,
    )
    return json.loads(completed.stdout)


def candidate_final(case: dict) -> dict | None:
    final_input = case["final_input"]
    if final_input["attendance_rate"] is None:
        return None
    skp_rate = final_input.get("skp_rate")
    mode = "ABSENSI_SKP" if skp_rate is not None else "ABSENSI_ONLY"
    skp = None
    if mode == "ABSENSI_SKP":
        skp = {
            "id": "synthetic-skp-" + case["id"], "nip": case["nip"],
            "periode": "2026-08", "source_type": "ASSESSMENT",
            "status_skp": final_input.get("skp_status") or "APPROVED",
            "predikat_skp": "BAIK", "potongan_skp_percent": skp_rate,
        }
    return calculate_final_tukin(
        case["nip"], "2026-08", mode,
        {"id": "synthetic-attendance-" + case["id"], "total": final_input["attendance_rate"]},
        {"tukin_gross": final_input["gross"], "nama": case["id"], "jabatan": "SYNTHETIC"},
        skp,
    )


def legacy_money(case: dict) -> dict | None:
    attendance = case.get("final_absensi")
    if not attendance:
        return None
    skp = case.get("final_skp")
    skp_deduction = None
    paid = attendance.get("finalDibayar")
    if case["final_input"].get("skp_rate") is not None and skp:
        skp_deduction = skp.get("nominalSKP")
        paid = skp.get("finalDibayarSKP")
    attendance_deduction = attendance.get("nominal")
    return {
        "tukin_bruto": _money(attendance.get("tukin")),
        "potongan_absen_rp": _money(attendance_deduction),
        "potongan_skp_rp": _money(skp_deduction),
        "total_potongan": _money(Decimal(str(attendance_deduction or 0)) + Decimal(str(skp_deduction or 0))),
        "tukin_dibayar": _money(paid),
    }


def audit(fixture: Path) -> dict:
    legacy = run_legacy(fixture)
    rows = []
    aggregate_legacy_raw = {field: Decimal(0) for field in MONEY_FIELDS}
    aggregate_candidate_raw = {field: Decimal(0) for field in MONEY_FIELDS}
    for case in legacy["cases"]:
        legacy_values = legacy_money(case)
        candidate = candidate_final(case)
        candidate_values = None if candidate is None else {field: candidate.get(field) for field in MONEY_FIELDS}
        diffs = {}
        if legacy_values is not None and candidate_values is not None:
            for field in MONEY_FIELDS:
                left, right = legacy_values.get(field), candidate_values.get(field)
                if left is not None:
                    aggregate_legacy_raw[field] += _decimal_money(left)
                if right is not None:
                    aggregate_candidate_raw[field] += _decimal_money(right)
                if left != right:
                    delta = None
                    if left is not None and right is not None:
                        delta = _money(_decimal_money(right) - _decimal_money(left))
                    diffs[field] = {"legacy": left, "candidate": right,
                                    "delta_rupiah": delta}
        rows.append({
            "id": case["id"], "nip": case["nip"],
            "daily_candidate": "PARITY_BLOCKED_NO_PURE_BACKEND_CONTRACT",
            "rekap_candidate": "PARITY_BLOCKED_NO_BACKEND_IMPLEMENTATION",
            "legacy_daily": case["daily"], "legacy_rekap": case["rekap"],
            "legacy_rekap_error": case["rekap_error"],
            "legacy_final": legacy_values, "candidate_final": candidate_values,
            "legacy_non_integer_money_fields": [] if legacy_values is None else [
                field for field in MONEY_FIELDS if not _is_integer_money(legacy_values.get(field))
            ],
            "candidate_status": candidate and candidate.get("status"),
            "candidate_reason": candidate and candidate.get("reason"),
            "money_differences": diffs,
            "money_exact": not diffs,
        })
    aggregate_legacy = {field: _money(value) for field, value in aggregate_legacy_raw.items()}
    aggregate_candidate = {field: _money(value) for field, value in aggregate_candidate_raw.items()}
    aggregate_diffs = {
        field: _money(aggregate_candidate_raw[field] - aggregate_legacy_raw[field])
        for field in MONEY_FIELDS
        if aggregate_candidate_raw[field] != aggregate_legacy_raw[field]
    }
    return {
        "contract_version": legacy["contract_version"],
        "comparison_policy": {"money": "EXACT_INTEGER_RUPIAH", "allowed_delta_rupiah": 0,
                              "floating_point_tolerance_for_money": False},
        "coverage": {"fixture_cases": len(rows), "daily_backend_supported": False,
                     "rekap_backend_supported": False, "final_backend_supported": True},
        "rows": rows,
        "aggregate": {"legacy": aggregate_legacy, "candidate": aggregate_candidate,
                      "differences": aggregate_diffs, "money_exact": not aggregate_diffs},
        "parity_ready": all(row["money_exact"] for row in rows) and not aggregate_diffs
                        and all(row["daily_candidate"].startswith("READY") for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-parity", action="store_true")
    args = parser.parse_args()
    result = audit(args.fixture.resolve())
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    if args.require_parity and not result["parity_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.absence_shadow_engine import detect_import_columns, normalize_raw_row


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend/tests/fixtures/absence_import_golden_cases_2026_09.json"
GOLDEN = ROOT / "backend/tests/fixtures/absence_import_golden_output_2026_09.json"
RUNNER = ROOT / "backend/tests/absence_import_legacy_runner.cjs"


def compact(record):
    if record is None:
        return None
    return {
        "nip": record["nip"], "nama": record["nama"], "jabatan": record["jabatan"],
        "date": record["tanggal"]["key"], "out_date": record["tanggalPulang"],
        "check_in": record["masukRaw"], "check_out": record["pulangRaw"],
        "check_in_minutes": record["masukMin"], "check_out_minutes": record["pulangMin"],
        "check_in_wib": record["masukNormMin"], "check_out_wib": record["pulangNormMin"],
        "check_in_add": record["masukNormMinAdd"], "check_out_add": record["pulangNormMinAdd"],
        "zone": record["zona"], "cross_day": record["crossDay"], "issues": record["issues"],
    }


def test_frozen_legacy_import_runner_is_unchanged():
    actual = subprocess.run(
        ["node", str(RUNNER), str(FIXTURE)], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert json.loads(actual.stdout) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_backend_normalization_matches_frozen_legacy_import_field_for_field():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    headers = fixture["headers"]
    mapping = detect_import_columns(headers)
    source = {item["id"]: item for item in fixture["cases"]}
    comparisons = 0
    for expected in golden["cases"]:
        item = source[expected["id"]]
        raw = dict(zip(headers, item["values"]))
        record = normalize_raw_row(raw, headers, mapping, expected["id"])
        assert compact(record) == expected["normalized"], expected["id"]
        assert (record is None) == expected["invalid"]
        comparisons += len(expected["normalized"] or {"invalid": True})
    assert comparisons == 210

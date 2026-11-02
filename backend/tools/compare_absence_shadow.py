"""CLI comparator for the isolated EVKIN Python absence shadow engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.absence_shadow_engine import (
    calculate_fixture,
    compare_outputs,
    load_json,
    validate_golden_fingerprint,
    validate_source_fingerprint,
)


DEFAULT_INPUT = ROOT / "backend/tests/fixtures/absence_js_golden_cases_2026_09.json"
DEFAULT_GOLDEN = ROOT / "backend/tests/fixtures/absence_js_golden_output_2026_09.json"


def run(input_path: str | Path = DEFAULT_INPUT, golden_path: str | Path = DEFAULT_GOLDEN) -> dict:
    source = validate_source_fingerprint(ROOT)
    golden = validate_golden_fingerprint(golden_path)
    reference = load_json(golden_path)
    candidate = calculate_fixture(load_json(input_path))
    comparison = compare_outputs(reference, candidate)
    return {
        "source_fingerprint": source,
        "golden_fingerprint": golden,
        "baseline_stale_warning": not source["valid"] or not golden["valid"],
        "comparison": comparison,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args.input, args.golden)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload, end="")
    if result["baseline_stale_warning"]:
        return 2
    return 0 if result["comparison"]["unexplained_mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

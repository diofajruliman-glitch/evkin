"""Run the required EVKIN Python quality-gate suite used by CI."""
from __future__ import annotations

import subprocess
import sys

FAST_TESTS = (
    "backend/tests/test_migration_registry.py",
    "backend/tests/test_api_security.py",
    "backend/tests/test_module_reset.py",
    "backend/tests/test_application_reset.py",
    "backend/tests/test_workflow_ownership.py",
    "backend/tests/test_absence_shadow_engine.py",
    "backend/tests/test_absence_cutover.py",
    "backend/tests/test_skp_assessment.py",
    "backend/tests/test_tukin_combined.py",
    "backend/tests/test_optional_final_tukin.py",
    "backend/tests/test_final_integrity.py",
    "backend/tests/test_final_export_complete.py",
)


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", *FAST_TESTS]))

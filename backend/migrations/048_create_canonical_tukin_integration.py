"""Additive canonical Tukin calculation snapshots and Final decisions."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb


def ensure_canonical_tukin_integration_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tukin_calculation_batch (
            id TEXT PRIMARY KEY,
            assessment_period TEXT NOT NULL,
            version INTEGER NOT NULL,
            calculation_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'CALCULATED',
            source_fingerprint TEXT NOT NULL,
            master_count INTEGER NOT NULL DEFAULT 0,
            attendance_final_count INTEGER NOT NULL DEFAULT 0,
            skp_approved_count INTEGER NOT NULL DEFAULT 0,
            mismatch_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            calculated_by TEXT,
            calculated_at TIMESTAMP DEFAULT now(),
            supersedes_calculation_id TEXT,
            UNIQUE(assessment_period, version)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tukin_calculation_result (
            id TEXT PRIMARY KEY,
            calculation_id TEXT NOT NULL,
            calculation_version TEXT NOT NULL,
            assessment_period TEXT NOT NULL,
            nip TEXT NOT NULL,
            status TEXT NOT NULL,
            tukin_gross BIGINT,
            attendance_rate DECIMAL(9,4),
            attendance_deduction BIGINT,
            skp_rate DECIMAL(9,4),
            skp_deduction BIGINT,
            adjustment BIGINT NOT NULL DEFAULT 0,
            total_deduction BIGINT,
            tukin_paid BIGINT,
            master_version INTEGER,
            attendance_source_id TEXT,
            attendance_batch_id TEXT,
            skp_source_id TEXT,
            skp_source_version TEXT,
            skp_batch_id TEXT,
            source_fingerprint TEXT NOT NULL,
            issues TEXT,
            snapshot TEXT NOT NULL,
            calculated_at TIMESTAMP DEFAULT now(),
            UNIQUE(calculation_id, nip)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tukin_final_decision (
            id TEXT PRIMARY KEY,
            assessment_period TEXT NOT NULL,
            calculation_id TEXT NOT NULL,
            calculation_version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'FINAL',
            approved_by TEXT NOT NULL,
            approved_at TIMESTAMP DEFAULT now(),
            locked_at TIMESTAMP,
            paid_by TEXT,
            paid_at TIMESTAMP,
            payment_reference TEXT,
            reason TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now(),
            UNIQUE(calculation_id)
        )"""
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_tukin_calc_period ON tukin_calculation_batch(assessment_period,version)",
        "CREATE INDEX IF NOT EXISTS idx_tukin_calc_result_key ON tukin_calculation_result(assessment_period,nip)",
        "CREATE INDEX IF NOT EXISTS idx_tukin_final_period ON tukin_final_decision(assessment_period,status)",
    ):
        conn.execute(statement)


def main() -> None:
    db_path = os.environ.get("EVKIN_DUCKDB_PATH") or str(
        Path(__file__).resolve().parents[1] / "data" / "EVKIN_MASTER.duckdb"
    )
    with duckdb.connect(db_path) as conn:
        ensure_canonical_tukin_integration_schema(conn)
    print(f"Migration 048 applied: canonical Tukin integration at {db_path}")


if __name__ == "__main__":
    main()

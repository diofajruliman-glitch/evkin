"""Additive operational SKP rules, configurable non-submission actions and period finalization."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb


def ensure_skp_operational_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skp_assessment_rule (
            id TEXT PRIMARY KEY,
            rule_name TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            flag_scope TEXT NOT NULL DEFAULT 'ALL',
            condition_field TEXT NOT NULL,
            comparison_operator TEXT NOT NULL DEFAULT 'EQ',
            threshold_value TEXT,
            result_if_met TEXT NOT NULL,
            reduction_if_met DECIMAL(9,4),
            result_if_not_met TEXT,
            reduction_if_not_met DECIMAL(9,4),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skp_assessment_rule_audit (
            id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            actor TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT now()
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skp_non_submission_rule (
            id TEXT PRIMARY KEY,
            streak_from INTEGER NOT NULL,
            streak_to INTEGER,
            action TEXT NOT NULL DEFAULT 'PERCENT',
            reduction_rate DECIMAL(9,4) NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            updated_by TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT now()
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skp_period_finalization (
            assessment_period TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'FINAL',
            total_sdm INTEGER NOT NULL,
            approved_count INTEGER NOT NULL,
            incomplete_count INTEGER NOT NULL,
            summary_snapshot TEXT NOT NULL,
            finalized_by TEXT NOT NULL,
            finalized_at TIMESTAMP DEFAULT now()
        )"""
    )
    assessment_exists = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='skp_assessment_monthly'"
    ).fetchone()[0]
    for column, data_type in (
        ("sdm_flag", "TEXT"),
        ("assessment_rule_id", "TEXT"),
        ("assessment_rule_snapshot", "TEXT"),
        ("recommended_reduction_rate", "DECIMAL(9,4)"),
        ("final_reduction_rate", "DECIMAL(9,4)"),
        ("manual_override_reason", "TEXT"),
        ("manual_override_by", "TEXT"),
        ("manual_override_at", "TIMESTAMP"),
    ):
        if assessment_exists:
            conn.execute(f"ALTER TABLE skp_assessment_monthly ADD COLUMN IF NOT EXISTS {column} {data_type}")
    conn.execute(
        """INSERT INTO skp_non_submission_rule
           (id,streak_from,streak_to,action,reduction_rate,active,effective_from,updated_by)
           SELECT * FROM (VALUES
             ('DEFAULT-1',1,1,'PERCENT',35,true,'2000-01','SYSTEM'),
             ('DEFAULT-2',2,2,'PERCENT',70,true,'2000-01','SYSTEM'),
             ('DEFAULT-3',3,NULL,'UNPAID',100,true,'2000-01','SYSTEM')
           ) AS seed(id,streak_from,streak_to,action,reduction_rate,active,effective_from,updated_by)
           WHERE NOT EXISTS (SELECT 1 FROM skp_non_submission_rule)"""
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_skp_rule_effective ON skp_assessment_rule(active,effective_from,effective_to,priority)",
        "CREATE INDEX IF NOT EXISTS idx_skp_non_submission_effective ON skp_non_submission_rule(active,effective_from,effective_to,streak_from)",
    ):
        conn.execute(statement)


def main() -> None:
    db_path = os.environ.get("EVKIN_DUCKDB_PATH") or str(
        Path(__file__).resolve().parents[1] / "data" / "EVKIN_MASTER.duckdb"
    )
    with duckdb.connect(db_path) as conn:
        ensure_skp_operational_schema(conn)
    print(f"Migration 049 applied: operational SKP workflow at {db_path}")


if __name__ == "__main__":
    main()

"""Read-only source and period audit for Controlled UAT candidate selection."""
from __future__ import annotations

from collections import Counter
from typing import Any

import duckdb


SKP_SOURCE_SPECS = (
    ("skp_assessment_monthly", "assessment_period", "Current assessment workflow rows", False),
    ("tukin_skp_monthly", "assessment_period", "Synchronized SKP snapshot", False),
    ("skp_results", "periode", "Legacy/import SKP result rows", False),
    ("skp_import_batch", "assessment_period", "SKP workflow import batch", False),
    ("skp_import_batches", "periode", "Legacy SKP import batch", False),
    ("skp_import_source", "assessment_period", "Archived raw SKP import rows", False),
    ("skp_assessment_audit", "assessment_period", "Assessment audit history", True),
    ("skp_meeting_batch", "assessment_period", "SKP meeting/workflow batch", False),
    ("skp_meeting_member", "assessment_period", "SKP meeting population", False),
)


def _exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]
    ).fetchone()[0])


def _columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    if not _exists(conn, table):
        return set()
    return {row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()}


def _count_nips(conn, table: str, period_col: str, period: str) -> int:
    cols = _columns(conn, table)
    if not cols or "nip" not in cols or period_col not in cols:
        return 0
    return int(conn.execute(
        f'SELECT count(DISTINCT nip) FROM "{table}" WHERE "{period_col}"=? AND nip IS NOT NULL',
        [period],
    ).fetchone()[0])


def audit_skp_sources(db_path: str) -> list[dict[str, Any]]:
    """Inventory every known SKP source without modifying the database."""
    result: list[dict[str, Any]] = []
    with duckdb.connect(str(db_path), read_only=True) as conn:
        for table, period_col, provenance, audit_only in SKP_SOURCE_SPECS:
            cols = _columns(conn, table)
            if not cols:
                result.append({
                    "source": table, "periods": [], "distinct_nip": 0,
                    "statuses": {}, "final_approved": 0, "batch_ids": [],
                    "timestamps": {}, "provenance": provenance,
                    "canonical_safe": False, "reason": "Table tidak tersedia atau kosong",
                })
                continue
            periods = [
                str(row[0]) for row in conn.execute(
                    f'SELECT DISTINCT "{period_col}" FROM "{table}" '
                    f'WHERE "{period_col}" IS NOT NULL ORDER BY 1'
                ).fetchall()
            ]
            status_col = next(
                (col for col in ("review_status", "status_skp", "status", "action")
                 if col in cols), None
            )
            statuses = {}
            if status_col:
                statuses = {
                    str(status or "UNKNOWN"): int(total)
                    for status, total in conn.execute(
                        f'SELECT "{status_col}",count(*) FROM "{table}" '
                        f'GROUP BY 1 ORDER BY 1'
                    ).fetchall()
                }
            final_approved = 0
            if status_col and status_col in {"review_status", "status_skp", "status"}:
                final_approved = int(conn.execute(
                    f"""SELECT count(DISTINCT nip) FROM "{table}"
                        WHERE upper(coalesce("{status_col}", '')) IN
                        ('FINAL','APPROVED','IMPORTED_FINAL')"""
                ).fetchone()[0]) if "nip" in cols else 0
            id_col = next(
                (col for col in (
                    "import_batch_id", "batch_id", "calculation_batch_id",
                    "batch_code"
                ) if col in cols), None
            )
            batch_ids = []
            if id_col:
                batch_ids = [
                    str(row[0]) for row in conn.execute(
                        f'SELECT DISTINCT "{id_col}" FROM "{table}" '
                        f'WHERE "{id_col}" IS NOT NULL ORDER BY 1 LIMIT 100'
                    ).fetchall()
                ]
            timestamps = {}
            for col in ("created_at", "updated_at", "approved_at", "imported_at"):
                if col in cols:
                    timestamps[col] = [
                        str(value) for value in conn.execute(
                            f'SELECT min("{col}"),max("{col}") FROM "{table}"'
                        ).fetchone()
                    ]
            if table == "skp_assessment_audit" and "action" in cols and "nip" in cols:
                final_approved = int(conn.execute(
                    """SELECT count(DISTINCT nip) FROM skp_assessment_audit
                       WHERE action IN ('IMPORT_FINAL','APPROVE')"""
                ).fetchone()[0])
            has_final = final_approved > 0
            canonical_safe = (
                table == "skp_assessment_monthly"
                and has_final
                and "import_batch_id" in cols
                and "approved_at" in cols
            )
            reason = (
                "Current workflow rows with final approval and batch lineage"
                if canonical_safe else
                "Audit/evidence only; not a canonical assessment source"
                if audit_only else
                "No verified final/approved canonical rows"
            )
            result.append({
                "source": table,
                "periods": periods,
                "distinct_nip": (
                    int(conn.execute(f'SELECT count(DISTINCT nip) FROM "{table}"').fetchone()[0])
                    if "nip" in cols else 0
                ),
                "statuses": statuses,
                "final_approved": final_approved,
                "batch_ids": batch_ids,
                "timestamps": timestamps,
                "provenance": provenance,
                "canonical_safe": canonical_safe,
                "reason": reason,
            })
    return result


def _nips(conn, table: str, period_col: str, period: str, where: str = "1=1") -> set[str]:
    cols = _columns(conn, table)
    if "nip" not in cols or period_col not in cols:
        return set()
    return {
        str(row[0]).strip() for row in conn.execute(
            f'SELECT DISTINCT nip FROM "{table}" WHERE "{period_col}"=? '
            f'AND {where} AND nip IS NOT NULL', [period]
        ).fetchall() if str(row[0]).strip()
    }


def scan_periods(db_path: str) -> list[dict[str, Any]]:
    """Return period facts and eligible estimates; never creates calculations."""
    with duckdb.connect(str(db_path), read_only=True) as conn:
        period_values: set[str] = set()
        for table, column in (
            ("final_absensi", "bulan"), ("skp_assessment_monthly", "assessment_period"),
            ("tukin_skp_monthly", "assessment_period"), ("skp_results", "periode"),
            ("skp_assessment_audit", "assessment_period"),
            ("final_tukin_results", "periode"), ("tukin_final_monthly", "assessment_period"),
            ("final_tukin_batches", "periode"),
        ):
            if _exists(conn, table) and column in _columns(conn, table):
                period_values.update(str(row[0]) for row in conn.execute(
                    f'SELECT DISTINCT "{column}" FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL'
                ).fetchall())
        master_table = "master_data" if _exists(conn, "master_data") else "sdm_master"
        master_nips = {
            str(row[0]).strip() for row in conn.execute(
                f'SELECT DISTINCT nip FROM "{master_table}" WHERE nip IS NOT NULL'
            ).fetchall() if str(row[0]).strip()
        }
        rows = []
        for period in sorted(
            (p for p in period_values if len(p) == 7 and p[4] == "-"),
            reverse=True,
        ):
            attendance = _nips(conn, "final_absensi", "bulan", period)
            assessment = _nips(conn, "skp_assessment_monthly", "assessment_period", period)
            approved = _nips(
                conn, "skp_assessment_monthly", "assessment_period", period,
                "upper(review_status) IN ('FINAL','APPROVED')",
            )
            imported = _nips(conn, "skp_results", "periode", period)
            approved |= _nips(
                conn, "skp_results", "periode", period,
                "upper(status_skp) IN ('FINAL','APPROVED','IMPORTED_FINAL')",
            )
            calc_scope = _nips(conn, "final_tukin_results", "periode", period)
            if assessment or imported:
                processing, scope_type, scope_quality = assessment | imported, "SKP_ASSESSMENT_OR_IMPORT", "TRACEABLE"
            elif calc_scope:
                processing, scope_type, scope_quality = calc_scope, "TUKIN_CALCULATION_IMPORT", "TRACEABLE"
            elif attendance:
                processing, scope_type, scope_quality = attendance, "FINAL_ABSENSI", "FALLBACK"
            else:
                processing, scope_type, scope_quality = set(), "NONE", "UNKNOWN"
            legacy = calc_scope
            monthly = _nips(conn, "tukin_final_monthly", "assessment_period", period)
            legacy_source = "tukin_final_monthly" if monthly else "final_tukin_results" if legacy else "NONE"
            legacy_quality = "TRACEABLE_LEGACY" if legacy else "NOT_COMPARABLE"
            eligible = processing & master_nips & attendance & approved
            rows.append({
                "period": period, "master": len(master_nips),
                "processing": len(processing), "scope_source_type": scope_type,
                "scope_source_id": None, "scope_quality": scope_quality,
                "final_attendance": len(attendance), "skp_available": len(assessment | imported),
                "skp_final_approved": len(approved), "legacy_official": len(legacy),
                "eligible_intersection_estimate": len(eligible),
                "legacy_in_scope": len(legacy & processing),
                "legacy_source": legacy_source,
                "legacy_source_quality": legacy_quality,
                "assessment_audit_evidence": len(_nips(
                    conn, "skp_assessment_audit", "assessment_period", period
                )),
            })
    return rows

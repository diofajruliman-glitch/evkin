"""Read-only shadow reconciliation for canonical and legacy Tukin results.

The service never creates calculations, Final decisions, payment events, or database
objects. It compares persisted snapshots at the canonical NIP + period grain.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import re
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse


CATEGORIES = (
    "MATCH", "EXPLAINED_DIFFERENCE", "UNEXPLAINED_DIFFERENCE",
    "MISSING_CANONICAL", "MISSING_LEGACY", "MISSING_ATTENDANCE",
    "MISSING_ASSESSMENT", "MISSING_MASTER", "SOURCE_INVALID", "STALE",
    "DUPLICATE", "OUT_OF_SCOPE",
)
MONEY_FIELDS = ("bruto", "absensi", "skp", "adjustment", "final")
COMPARISON_SOURCES = {"auto", "tukin_final_monthly", "final_tukin_results"}


def _period(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise ValueError("Periode UAT wajib YYYY-MM")
    return text


def _money(value: Any) -> int | None:
    """Convert exact whole Rupiah to int; floats never participate in arithmetic."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, float):
        value = repr(value)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Nominal Rupiah tidak valid: {value}") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError(f"Nominal Rupiah harus bilangan bulat: {value}")
    return int(parsed)


def _records(cursor) -> list[dict[str, Any]]:
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]
    ).fetchone()[0])


def _columns(conn, table: str) -> set[str]:
    return {row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()} if _table_exists(conn, table) else set()


def _delta(left: int | None, right: int | None) -> int | None:
    return left - right if left is not None and right is not None else None


def _canonical_record(row: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    snapshot = json.loads(row.get("snapshot") or "{}")
    return {
        "nip": str(row.get("nip") or "").strip(),
        "nama": snapshot.get("nama") or "",
        "unit": snapshot.get("unit") or "",
        "periode": row.get("assessment_period"),
        "bruto": _money(row.get("tukin_gross")),
        "komponen_absensi": _money(snapshot.get("attendance_component")),
        "potongan_absensi": _money(row.get("attendance_deduction")),
        "komponen_skp": _money(snapshot.get("skp_component")),
        "potongan_skp": _money(row.get("skp_deduction")),
        "adjustment": _money(row.get("adjustment")) or 0,
        "total_potongan": _money(row.get("total_deduction")),
        "tukin_final": _money(row.get("tukin_paid")),
        "calculation_id": row.get("calculation_id"),
        "calculation_version": batch.get("version"),
        "attendance_source_id": row.get("attendance_source_id") or "",
        "attendance_batch_id": row.get("attendance_batch_id") or "",
        "skp_source_id": row.get("skp_source_id") or "",
        "skp_source_version": row.get("skp_source_version") or "",
        "skp_batch_id": row.get("skp_batch_id") or "",
        "master_version": row.get("master_version"),
        "calculation_status": row.get("status") or batch.get("status"),
        "batch_status": batch.get("status"),
        "batch_source_fingerprint": batch.get("source_fingerprint"),
        "row_source_fingerprint": row.get("source_fingerprint"),
        "calculated_at": row.get("calculated_at") or batch.get("calculated_at"),
        "snapshot": snapshot,
    }


def _legacy_record(row: dict[str, Any], source: str) -> dict[str, Any]:
    if source == "tukin_final_monthly":
        reference = ":".join(filter(None, [
            "TUKIN_FINAL_MONTHLY", str(row.get("import_batch_id") or ""),
            str(row.get("source_file") or ""), str(row.get("source_row") or ""),
        ]))
        return {
            "nip": str(row.get("nip") or "").strip(), "nama": row.get("nama") or "",
            "unit": row.get("unit") or "", "periode": row.get("assessment_period"),
            "bruto_legacy": _money(row.get("tukin_gross")),
            "potongan_absensi_legacy": _money(row.get("attendance_deduction")),
            "potongan_skp_legacy": _money(row.get("skp_deduction")),
            "adjustment_legacy": _money(row.get("manual_adjustment")) or 0,
            "tukin_final_legacy": _money(row.get("paid_amount")),
            "legacy_reference": reference, "legacy_status": row.get("payment_status") or "",
        }
    reference = f"FINAL_TUKIN_RESULTS:{row.get('calculation_batch_id') or ''}:v{row.get('version') or ''}"
    snapshot = json.loads(row.get("snapshot") or "{}")
    return {
        "nip": str(row.get("nip") or "").strip(), "nama": snapshot.get("nama") or "",
        "unit": snapshot.get("unit") or snapshot.get("provinsi") or "",
        "periode": row.get("periode"), "bruto_legacy": _money(row.get("tukin_bruto")),
        "potongan_absensi_legacy": _money(row.get("potongan_absen_rp")),
        "potongan_skp_legacy": _money(row.get("potongan_skp_rp")),
        "adjustment_legacy": 0, "tukin_final_legacy": _money(row.get("tukin_dibayar")),
        "legacy_reference": reference, "legacy_status": row.get("status") or "",
    }


def reconcile_records(
    canonical_rows: list[dict[str, Any]], legacy_rows: list[dict[str, Any]],
    explanations: dict[str, dict[str, Any]] | None = None,
    population: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pure NIP + period reconciliation, shared by service and unit tests."""
    explanations = explanations or {}
    canonical_counts = Counter((row["nip"], row["periode"]) for row in canonical_rows)
    legacy_counts = Counter((row["nip"], row["periode"]) for row in legacy_rows)
    canonical = {(row["nip"], row["periode"]): row for row in canonical_rows}
    legacy = {(row["nip"], row["periode"]): row for row in legacy_rows}
    processing_scope = set((population or {}).get("processing_nips") or ())
    final_assessment = set((population or {}).get("final_assessment_nips") or ())
    final_attendance = set((population or {}).get("final_attendance_nips") or ())
    master_scope = set((population or {}).get("master_nips") or ())
    output = []
    for key in sorted(set(canonical) | set(legacy)):
        current = canonical.get(key)
        old = legacy.get(key)
        row = {"nip": key[0], "periode": key[1]}
        if current:
            row.update(current)
        if old:
            row.update({k: v for k, v in old.items() if k not in {"nip", "periode"}})
            row["nama"] = row.get("nama") or old.get("nama") or ""
            row["unit"] = row.get("unit") or old.get("unit") or ""
        for field in MONEY_FIELDS:
            canonical_key = "tukin_final" if field == "final" else "potongan_absensi" if field == "absensi" else "potongan_skp" if field == "skp" else field
            legacy_key = "tukin_final_legacy" if field == "final" else "potongan_absensi_legacy" if field == "absensi" else "potongan_skp_legacy" if field == "skp" else f"{field}_legacy"
            row[f"delta_{field}"] = _delta(row.get(canonical_key), row.get(legacy_key))
        row["duplicate_canonical"] = canonical_counts[key] > 1
        row["duplicate_legacy"] = legacy_counts[key] > 1
        explanation = explanations.get(row["nip"], {})
        if processing_scope and row["nip"] not in processing_scope:
            category, reason = "OUT_OF_SCOPE", "Legacy berada di luar processing population periode ini"
        elif row["duplicate_canonical"] or row["duplicate_legacy"]:
            category, reason = "DUPLICATE", "Lebih dari satu baris pada grain NIP + periode"
        elif current and current.get("stale_status") == "STALE":
            category, reason = "STALE", current.get("stale_reason") or "Source berubah setelah calculation"
        elif not current and population and row["nip"] not in final_assessment:
            category, reason = "MISSING_ASSESSMENT", "Assessment belum FINAL/APPROVED"
        elif not current and population and row["nip"] not in final_attendance:
            category, reason = "MISSING_ATTENDANCE", "Final Absensi belum tersedia dalam processing scope"
        elif not current and population and row["nip"] not in master_scope:
            category, reason = "MISSING_MASTER", "NIP processing tidak ditemukan pada Master SDM"
        elif not current:
            category, reason = "MISSING_CANONICAL", "Pembanding tersedia tetapi calculation canonical tidak tersedia"
        elif not old:
            category, reason = "MISSING_LEGACY", "Calculation canonical tersedia tetapi pembanding tidak tersedia"
        elif current.get("source_invalid") or current.get("broken_lineage"):
            category, reason = "SOURCE_INVALID", "; ".join(current.get("lineage_issues") or ["Source canonical tidak valid"])
        elif all(row[f"delta_{field}"] == 0 for field in MONEY_FIELDS):
            category, reason = "MATCH", "Seluruh nominal sama pada Rupiah bulat"
        elif explanation.get("verified") is True and explanation.get("reason") and explanation.get("evidence"):
            category, reason = "EXPLAINED_DIFFERENCE", str(explanation["reason"])
        else:
            category, reason = "UNEXPLAINED_DIFFERENCE", "Selisih nominal belum memiliki evidence terverifikasi"
        row.update(category=category, reconciliation_reason=reason,
                   explanation_evidence=str(explanation.get("evidence") or ""))
        output.append(row)
    return output


class TukinReconciliationService:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def connect(self):
        return duckdb.connect(self.db_path, read_only=True)

    def periods(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            period_values = set()
            for table, column in (("final_absensi", "bulan"), ("skp_assessment_monthly", "assessment_period"),
                                  ("tukin_final_monthly", "assessment_period"), ("final_tukin_results", "periode"),
                                  ("tukin_calculation_batch", "assessment_period")):
                if _table_exists(conn, table) and column in _columns(conn, table):
                    period_values.update(str(row[0]) for row in conn.execute(
                        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                    ).fetchall())
        return [self.preflight(value) for value in sorted(period_values, reverse=True) if re.fullmatch(r"\d{4}-\d{2}", value)]

    def preflight(self, period: str) -> dict[str, Any]:
        period = _period(period)
        with self.connect() as conn:
            def count(table, where="", params=None, distinct="nip"):
                if not _table_exists(conn, table):
                    return 0
                return int(conn.execute(
                    f'SELECT count(DISTINCT "{distinct}") FROM "{table}"' + (" WHERE " + where if where else ""),
                    params or [],
                ).fetchone()[0])
            master_table = "master_data" if _table_exists(conn, "master_data") else "sdm_master"
            master = count(master_table)
            attendance = count("final_absensi", "bulan=?", [period])
            skp = count(
                "skp_assessment_monthly",
                "assessment_period=? AND upper(review_status) IN ('FINAL','APPROVED')",
                [period],
            )
            monthly = count("tukin_final_monthly", "assessment_period=?", [period])
            legacy = count("final_tukin_results", "periode=?", [period])
            calculated = count("tukin_calculation_result", "assessment_period=? AND status='READY'", [period])
            comparison = "tukin_final_monthly" if monthly else "final_tukin_results" if legacy else "NONE"
        return {"periode": period, "master_sdm": master, "final_absensi": attendance,
                "skp_approved": skp, "canonical_calculated": calculated,
                "tukin_final_monthly": monthly, "final_tukin_results": legacy,
                "recommended_comparison_source": comparison,
                "comparison_authority": "UNVERIFIED" if comparison != "NONE" else "MISSING"}

    def _population(self, conn, period: str) -> dict[str, Any]:
        """Build period scope without treating the reference master as a target."""
        def nips(table: str, where: str, params: list[Any]) -> set[str]:
            if not _table_exists(conn, table) or "nip" not in _columns(conn, table):
                return set()
            return {
                str(row[0]).strip() for row in conn.execute(
                    f'SELECT DISTINCT nip FROM "{table}" WHERE {where} AND nip IS NOT NULL',
                    params,
                ).fetchall() if str(row[0]).strip()
            }

        master_table = "master_data" if _table_exists(conn, "master_data") else "sdm_master"
        master_nips = nips(master_table, "1=1", [])
        attendance_nips = nips("final_absensi", "bulan=?", [period])
        assessment_nips = nips("skp_assessment_monthly", "assessment_period=?", [period])
        imported_nips = nips("skp_results", "periode=?", [period])
        workflow_nips = assessment_nips | imported_nips
        if workflow_nips:
            processing_nips = workflow_nips
            scope_source = "ASSESSMENT_IMPORT_OR_WORKFLOW"
            scope_gap = False
        elif attendance_nips:
            processing_nips = attendance_nips
            scope_source = "FINAL_ATTENDANCE_FALLBACK"
            scope_gap = True
        else:
            processing_nips = set()
            scope_source = "NONE"
            scope_gap = True

        final_assessment_nips = nips(
            "skp_assessment_monthly",
            "assessment_period=? AND upper(review_status) IN ('FINAL','APPROVED')",
            [period],
        )
        final_assessment_nips |= nips(
            "skp_results",
            "periode=? AND upper(status_skp) IN ('FINAL','APPROVED','IMPORTED_FINAL')",
            [period],
        )
        final_attendance_nips = attendance_nips & processing_nips
        eligible_nips = final_assessment_nips & final_attendance_nips & master_nips
        return {
            "master_nips": master_nips,
            "processing_nips": processing_nips,
            "final_assessment_nips": final_assessment_nips,
            "final_attendance_nips": final_attendance_nips,
            "eligible_nips": eligible_nips,
            "scope_source": scope_source,
            "scope_gap": scope_gap,
            "assessment_status": self._status_counts(conn, period),
        }

    @staticmethod
    def _status_counts(conn, period: str) -> dict[str, int]:
        if not _table_exists(conn, "skp_assessment_monthly"):
            return {}
        rows = conn.execute(
            """SELECT coalesce(nullif(upper(review_status), ''), 'UNKNOWN'), count(DISTINCT nip)
               FROM skp_assessment_monthly
               WHERE assessment_period=? GROUP BY 1 ORDER BY 1""",
            [period],
        ).fetchall()
        return {str(status): int(total) for status, total in rows}

    def _canonical(self, conn, period: str, calculation_id: str = "") -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if not (_table_exists(conn, "tukin_calculation_batch") and _table_exists(conn, "tukin_calculation_result")):
            return None, []
        params = [calculation_id] if calculation_id else [period]
        where = "id=?" if calculation_id else "assessment_period=?"
        batch_rows = _records(conn.execute(
            f"""SELECT * FROM tukin_calculation_batch WHERE {where}
                ORDER BY version DESC LIMIT 1""", params
        ))
        if not batch_rows:
            return None, []
        batch = batch_rows[0]
        raw = _records(conn.execute(
            "SELECT * FROM tukin_calculation_result WHERE calculation_id=? ORDER BY nip", [batch["id"]]
        ))
        rows = [_canonical_record(row, batch) for row in raw]
        self._validate_lineage(conn, rows, period)
        return batch, rows

    def _validate_lineage(self, conn, rows: list[dict[str, Any]], period: str) -> None:
        attendance = {}
        if _table_exists(conn, "final_absensi"):
            for row in _records(conn.execute("SELECT * FROM final_absensi WHERE bulan=?", [period])):
                attendance.setdefault(str(row.get("nip") or "").strip(), []).append(row)
        skp = {}
        if _table_exists(conn, "skp_assessment_monthly"):
            for row in _records(conn.execute("SELECT * FROM skp_assessment_monthly WHERE assessment_period=?", [period])):
                skp.setdefault(str(row.get("nip") or "").strip(), []).append(row)
        master = {}
        master_table = "master_data" if _table_exists(conn, "master_data") else "sdm_master" if _table_exists(conn, "sdm_master") else ""
        if master_table:
            for row in _records(conn.execute(f"SELECT * FROM {master_table}")):
                master.setdefault(str(row.get("nip") or "").strip(), []).append(row)
        adjustments = {}
        if _table_exists(conn, "tukin_combined_adjustment"):
            adjustments = {str(row.get("nip") or "").strip(): row for row in _records(conn.execute(
                "SELECT * FROM tukin_combined_adjustment WHERE assessment_period=?", [period]
            ))}
        for row in rows:
            nip = row["nip"]
            issues, stale = [], []
            current_abs = attendance.get(nip, [])
            current_skp = skp.get(nip, [])
            current_master = master.get(nip, [])
            if not row.get("attendance_source_id") or len(current_abs) != 1:
                issues.append("Attendance lineage tidak unik/tersedia")
            if not row.get("skp_source_id") or len([item for item in current_skp if item.get("review_status") == "APPROVED"]) != 1:
                issues.append("SKP Approved lineage tidak unik/tersedia")
            if row.get("master_version") is None or len(current_master) != 1:
                issues.append("Master effective lineage tidak unik/tersedia")
            if current_abs:
                saved_rate = row["snapshot"].get("attendance_rate")
                current_rate = current_abs[0].get("potPct")
                if saved_rate is not None and Decimal(str(saved_rate)) != Decimal(str(current_rate)):
                    stale.append("Potongan Absensi source berubah")
            approved = [item for item in current_skp if item.get("review_status") == "APPROVED"]
            if len(approved) == 1:
                source_version = str(approved[0].get("rule_version") or approved[0].get("indicator_config_snapshot") or "")
                if str(row.get("skp_source_version") or "") != source_version:
                    stale.append("SKP source version berubah")
                saved_predicate = str(row["snapshot"].get("predicate_skp") or "").strip().upper()
                current_predicate = str(approved[0].get("final_predicate") or "").strip().upper()
                if saved_predicate != current_predicate:
                    stale.append("Predikat SKP source berubah")
            if len(current_master) == 1:
                source_gross = current_master[0].get("tukin_gross") or current_master[0].get("tukin")
                if source_gross is not None and _money(source_gross) != row.get("bruto"):
                    stale.append("Master Tukin bruto berubah")
            current_adjustment = _money(adjustments.get(nip, {}).get("amount")) or 0
            if current_adjustment != (row.get("adjustment") or 0):
                stale.append("Adjustment source berubah")
            if row.get("batch_status") == "STALE":
                stale.append("Calculation batch berstatus STALE")
            row.update(lineage_issues=issues, broken_lineage=bool(issues), source_invalid=bool(issues),
                       stale_status="STALE" if stale else "CURRENT", stale_reason="; ".join(dict.fromkeys(stale)))

    def _legacy(self, conn, period: str, source: str) -> tuple[str, list[dict[str, Any]]]:
        if source not in COMPARISON_SOURCES:
            raise ValueError("Comparison source tidak valid")
        if source == "auto":
            monthly = conn.execute("SELECT count(*) FROM tukin_final_monthly WHERE assessment_period=?", [period]).fetchone()[0] if _table_exists(conn, "tukin_final_monthly") else 0
            source = "tukin_final_monthly" if monthly else "final_tukin_results"
        if not _table_exists(conn, source):
            return source, []
        if source == "tukin_final_monthly":
            raw = _records(conn.execute("SELECT * FROM tukin_final_monthly WHERE assessment_period=? ORDER BY nip", [period]))
        else:
            version = conn.execute("SELECT max(version) FROM final_tukin_results WHERE periode=?", [period]).fetchone()[0]
            raw = _records(conn.execute(
                "SELECT * FROM final_tukin_results WHERE periode=? AND version=? ORDER BY nip", [period, version]
            )) if version is not None else []
        return source, [_legacy_record(row, source) for row in raw]

    def reconcile(self, period: str, comparison_source: str = "auto", calculation_id: str = "",
                  explanations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        period = _period(period)
        with self.connect() as conn:
            batch, canonical = self._canonical(conn, period, calculation_id)
            source, legacy = self._legacy(conn, period, comparison_source)
            population = self._population(conn, period)
            decisions = _records(conn.execute(
                "SELECT * FROM tukin_final_decision WHERE assessment_period=?", [period]
            )) if _table_exists(conn, "tukin_final_decision") else []
        details = reconcile_records(canonical, legacy, explanations, population)
        counts = {category: sum(row["category"] == category for row in details) for category in CATEGORIES}
        legacy_keys = {(row["nip"], row["periode"]) for row in legacy}
        canonical_keys = {(row["nip"], row["periode"]) for row in canonical}
        totals = {}
        for field in MONEY_FIELDS:
            canonical_key = "tukin_final" if field == "final" else "potongan_absensi" if field == "absensi" else "potongan_skp" if field == "skp" else field
            legacy_key = "tukin_final_legacy" if field == "final" else "potongan_absensi_legacy" if field == "absensi" else "potongan_skp_legacy" if field == "skp" else f"{field}_legacy"
            comparable = [row for row in details if row["category"] != "OUT_OF_SCOPE"]
            canonical_total = sum(row.get(canonical_key) or 0 for row in comparable)
            legacy_total = sum(row.get(legacy_key) or 0 for row in comparable)
            totals[field] = {"canonical": canonical_total, "legacy": legacy_total,
                             "delta": canonical_total - legacy_total}
        broken = sum(bool(row.get("broken_lineage")) for row in canonical)
        without_attendance = sum("Attendance lineage" in " ".join(row.get("lineage_issues") or []) and row.get("calculation_status") == "READY" for row in canonical)
        without_skp = sum("SKP Approved lineage" in " ".join(row.get("lineage_issues") or []) and row.get("calculation_status") == "READY" for row in canonical)
        stale_approved = sum(
            row.get("stale_status") == "STALE" and any(item.get("calculation_id") == row.get("calculation_id") and item.get("status") in {"FINAL", "PAID"} for item in decisions)
            for row in canonical
        )
        decision_links = {item.get("calculation_id") for item in decisions}
        final_snapshot_mismatch = sum(item not in {row.get("calculation_id") for row in canonical} for item in decision_links)
        preflight = self.preflight(period)
        acceptance = {
            "duplicate_canonical": sum(row["duplicate_canonical"] for row in details),
            "duplicate_legacy": sum(row["duplicate_legacy"] for row in details),
            "calculation_without_final_attendance": without_attendance,
            "calculation_without_skp_approved": without_skp,
            "broken_calculation_lineage": broken,
            "final_nominal_differs_from_snapshot": final_snapshot_mismatch,
            "stale_calculation_approved": stale_approved,
            "unexplained_difference": counts["UNEXPLAINED_DIFFERENCE"],
            "unexplained_total_rupiah": sum(abs(row.get("delta_final") or 0) for row in details if row["category"] == "UNEXPLAINED_DIFFERENCE"),
        }
        blockers = any(acceptance.values()) or any(
            counts[key] for key in (
                "MISSING_CANONICAL", "MISSING_LEGACY", "MISSING_ATTENDANCE",
                "MISSING_ASSESSMENT", "MISSING_MASTER", "DUPLICATE",
                "SOURCE_INVALID", "STALE",
            )
        ) or population["scope_gap"]
        authority = "UNVERIFIED"  # A database table is a candidate comparator, not proof of official authority.
        if blockers or not canonical or not legacy or authority != "VERIFIED":
            verdict = "FAIL - NEEDS INVESTIGATION"
        elif counts["EXPLAINED_DIFFERENCE"]:
            verdict = "PASS WITH EXPLAINED DIFFERENCES"
        else:
            verdict = "PASS UAT"
        return {
            "mode": "SHADOW_READ_ONLY", "periode": period,
            "uat_at": datetime.now(timezone.utc).isoformat(),
            "comparison_source": source, "comparison_authority": authority,
            "calculation": batch, "preflight": preflight,
            "population": {
                **preflight,
                "master_population": len(population["master_nips"]),
                "processing_population": len(population["processing_nips"]),
                "final_assessment_population": len(population["final_assessment_nips"]),
                "final_attendance_in_scope": len(population["final_attendance_nips"]),
                "eligible_population": len(population["eligible_nips"]),
                "calculated_population": sum(
                    nip in population["eligible_nips"] for nip, _ in canonical_keys
                ),
                "legacy_total": len(legacy),
                "legacy_in_scope": sum(
                    nip in population["processing_nips"] for nip, _ in legacy_keys
                ),
                "legacy_out_of_scope": counts["OUT_OF_SCOPE"],
                "scope_source": population["scope_source"],
                "scope_gap": population["scope_gap"],
                "assessment_status": population["assessment_status"],
                **counts,
            },
            "totals": totals, "acceptance": acceptance, "verdict": verdict,
            "details": details,
        }

    def detail(self, period: str, nip: str, comparison_source: str = "auto", calculation_id: str = "") -> dict[str, Any]:
        result = self.reconcile(period, comparison_source, calculation_id)
        row = next((item for item in result["details"] if item["nip"] == str(nip).strip()), None)
        if not row:
            raise ValueError("NIP tidak ditemukan pada dataset reconciliation")
        return {"mode": result["mode"], "comparison_source": result["comparison_source"], "row": row}

    def export(self, result: dict[str, Any]) -> io.BytesIO:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill

        wb = Workbook()
        wb.remove(wb.active)
        header_fill = PatternFill("solid", fgColor="17365D")
        alert_fill = PatternFill("solid", fgColor="FCE8E6")

        def add_sheet(name: str, rows: list[dict[str, Any]], columns: list[str] | None = None):
            ws = wb.create_sheet(name[:31])
            ws.sheet_view.showGridLines = False
            columns = columns or (list(rows[0]) if rows else ["Informasi"])
            ws.append(columns)
            for row in rows:
                values = []
                for column in columns:
                    value = row.get(column)
                    if isinstance(value, (list, dict, tuple, set)):
                        value = json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                        value = "'" + value
                    values.append(value)
                ws.append(values)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
                cell.fill = header_fill
            for column in ws.columns:
                width = min(40, max(12, max(len(str(cell.value or "")) for cell in list(column)[:200]) + 2))
                ws.column_dimensions[column[0].column_letter].width = width
                for cell in column[1:]:
                    cell.font = Font(name="Arial", size=10)
                    if cell.column <= len(columns) and columns[cell.column - 1] in {
                        "bruto", "potongan_absensi", "potongan_skp", "adjustment", "total_potongan", "tukin_final",
                        "bruto_legacy", "potongan_absensi_legacy", "potongan_skp_legacy", "adjustment_legacy",
                        "tukin_final_legacy", "delta_bruto", "delta_absensi", "delta_skp", "delta_adjustment", "delta_final",
                    }:
                        cell.number_format = "#,##0"
            return ws

        summary_rows = [
            {"Metric": "Periode", "Value": result["periode"]},
            {"Metric": "Waktu UAT (UTC)", "Value": result["uat_at"]},
            {"Metric": "Mode", "Value": result["mode"]},
            {"Metric": "Sumber pembanding", "Value": result["comparison_source"]},
            {"Metric": "Otoritas pembanding", "Value": result["comparison_authority"]},
            {"Metric": "Verdict", "Value": result["verdict"]},
        ]
        summary_rows += [{"Metric": f"Populasi {key}", "Value": value} for key, value in result["population"].items() if not isinstance(value, str)]
        summary_rows += [{"Metric": f"Total {field} canonical", "Value": values["canonical"]} for field, values in result["totals"].items()]
        summary_rows += [{"Metric": f"Total {field} legacy", "Value": values["legacy"]} for field, values in result["totals"].items()]
        summary_rows += [{"Metric": f"Delta {field}", "Value": values["delta"]} for field, values in result["totals"].items()]
        summary = add_sheet("SUMMARY", summary_rows, ["Metric", "Value"])
        if result["verdict"].startswith("FAIL"):
            for cell in summary[1 + next(i for i, row in enumerate(summary_rows) if row["Metric"] == "Verdict") + 1]:
                cell.fill = alert_fill
                cell.font = Font(name="Arial", size=10, bold=True, color="9C0006")
        mapping = {
            "MATCH": "MATCH", "EXPLAINED_DIFFERENCE": "EXPLAINED DIFFERENCE",
            "UNEXPLAINED_DIFFERENCE": "UNEXPLAINED DIFFERENCE",
            "MISSING_CANONICAL": "MISSING CANONICAL", "MISSING_LEGACY": "MISSING LEGACY",
            "MISSING_ATTENDANCE": "MISSING ATTENDANCE",
            "MISSING_ASSESSMENT": "MISSING ASSESSMENT",
            "MISSING_MASTER": "MISSING MASTER",
            "SOURCE_INVALID": "SOURCE INVALID", "STALE": "STALE",
            "DUPLICATE": "DUPLICATE", "OUT_OF_SCOPE": "OUT OF SCOPE",
        }
        detail_columns = [
            "nip", "nama", "unit", "periode", "category", "reconciliation_reason",
            "bruto", "potongan_absensi", "potongan_skp", "adjustment", "tukin_final",
            "bruto_legacy", "potongan_absensi_legacy", "potongan_skp_legacy", "adjustment_legacy", "tukin_final_legacy",
            "delta_bruto", "delta_absensi", "delta_skp", "delta_adjustment", "delta_final",
            "calculation_id", "calculation_version", "legacy_reference", "explanation_evidence",
        ]
        for category, sheet_name in mapping.items():
            add_sheet(sheet_name, [row for row in result["details"] if row["category"] == category], detail_columns)
        lineage_columns = [
            "nip", "periode", "category", "calculation_id", "calculation_version", "calculation_status",
            "stale_status", "stale_reason", "attendance_source_id", "attendance_batch_id",
            "skp_source_id", "skp_source_version", "skp_batch_id", "master_version",
            "adjustment", "legacy_reference", "lineage_issues",
        ]
        add_sheet("SOURCE LINEAGE", result["details"], lineage_columns)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output


def create_router(db_path_getter):
    router = APIRouter(prefix="/api/tukin/reconciliation", tags=["tukin-reconciliation"])

    def service():
        return TukinReconciliationService(db_path_getter())

    def run(action):
        try:
            return action()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/periods")
    def periods():
        return {"rows": run(lambda: service().periods()), "mode": "SHADOW_READ_ONLY"}

    @router.post("/run")
    def reconcile(payload: dict):
        return run(lambda: service().reconcile(
            payload.get("periode"), payload.get("comparison_source") or "auto",
            payload.get("calculation_id") or "", payload.get("explanations") or {},
        ))

    @router.post("/export")
    def export(payload: dict):
        svc = service()
        result = run(lambda: svc.reconcile(
            payload.get("periode"), payload.get("comparison_source") or "auto",
            payload.get("calculation_id") or "", payload.get("explanations") or {},
        ))
        stream = svc.export(result)
        return StreamingResponse(
            stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="UAT_RECON_TUKIN_{result["periode"]}.xlsx"'},
        )

    @router.get("/detail")
    def detail(periode: str, nip: str, comparison_source: str = "auto", calculation_id: str = ""):
        return run(lambda: service().detail(periode, nip, comparison_source, calculation_id))

    return router

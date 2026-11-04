"""Controlled authority and batch infrastructure for Absence calculation.

The default remains JS_LEGACY.  Python modes are opt-in and never silently
fall back to JavaScript.  Calculation uses canonical backend inputs and only a
COMPLETED canonical batch may publish Harian/Rekap/Final results.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import duckdb
from fastapi import APIRouter, HTTPException, Query

from backend.absence_shadow_engine import (
    BASELINE_ID,
    BASELINE_SOURCE_SHA256,
    _flatten,
    build_context,
    compute_final_absensi,
    compute_record,
    compute_rekap,
    detect_import_columns,
    normalize_raw_rows,
    validate_source_fingerprint,
)
from backend.runtime_guard import DATABASE_GATE, assert_current_generation, current_reset_generation


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "backend/tests/fixtures/absence_cross_day_complete_output_2026_10.json"
WORKBOOK_RULES_GOLDEN_SHA256 = "1aea4080d79e4e0767765a2cf5516858a6c704a4166d427979b29bc1832781be"
PYTHON_CALCULATION_VERSION = "ABSENCE_PY_2026_11_V3"
PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ACTIVE_STATUSES = ("PENDING", "RUNNING")
FINAL_STATUSES = (
    "COMPLETED", "FAILED", "STALE_SOURCE", "STALE_GENERATION", "SHADOW_ONLY"
)


class CalculationAuthority(str, Enum):
    JS_LEGACY = "JS_LEGACY"
    PYTHON_SHADOW = "PYTHON_SHADOW"
    PYTHON_CANONICAL = "PYTHON_CANONICAL"


class AbsenceCalculationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class AuthorityConfig:
    authority: CalculationAuthority = CalculationAuthority.JS_LEGACY
    canonical_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "AuthorityConfig":
        raw = str(os.environ.get("EVKIN_ABSENCE_AUTHORITY") or "JS_LEGACY").strip().upper()
        try:
            authority = CalculationAuthority(raw)
        except ValueError as exc:
            raise RuntimeError(f"EVKIN_ABSENCE_AUTHORITY tidak valid: {raw}") from exc
        enabled = str(os.environ.get("EVKIN_ABSENCE_CANONICAL_ENABLED") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return cls(authority=authority, canonical_enabled=enabled)


def current_authority_config() -> AuthorityConfig:
    return AuthorityConfig.from_environment()


def formula_source_fingerprint() -> str:
    python_engine = ROOT / "backend/absence_shadow_engine.py"
    material = {
        "baseline": BASELINE_ID,
        "golden": WORKBOOK_RULES_GOLDEN_SHA256,
        "sources": BASELINE_SOURCE_SHA256,
        "python_version": PYTHON_CALCULATION_VERSION,
        "python_engine_sha256": hashlib.sha256(python_engine.read_bytes()).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def readiness_gate(repo_root: str | Path = ROOT) -> dict[str, Any]:
    source = validate_source_fingerprint(repo_root)
    golden_valid = hashlib.sha256(GOLDEN_PATH.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == WORKBOOK_RULES_GOLDEN_SHA256
    return {
        "ready": bool(source["valid"] and golden_valid),
        "baseline": BASELINE_ID,
        "python_version": PYTHON_CALCULATION_VERSION,
        "golden_fields": 8493,
        "real_world_fields": 1967751,
        "required_golden_coverage": ["WIB", "WITA", "WIT", "WALI", "CROSS_DAY"],
        "source_fingerprints_valid": source["valid"],
        "golden_fingerprint_valid": golden_valid,
        "differences": source["differences"],
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS absence_calculation_batch (
    calculation_id VARCHAR PRIMARY KEY,
    period VARCHAR NOT NULL,
    authority VARCHAR NOT NULL,
    engine VARCHAR NOT NULL,
    calculation_version VARCHAR NOT NULL,
    reset_generation BIGINT NOT NULL,
    input_fingerprint VARCHAR,
    source_formula_fingerprint VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    canonical BOOLEAN NOT NULL DEFAULT FALSE,
    requested_by VARCHAR NOT NULL,
    rerun_reason VARCHAR,
    previous_calculation_id VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    raw_count BIGINT DEFAULT 0,
    daily_count BIGINT DEFAULT 0,
    rekap_count BIGINT DEFAULT 0,
    final_count BIGINT DEFAULT 0,
    fields_compared BIGINT DEFAULT 0,
    mismatch_count BIGINT DEFAULT 0,
    duration_ms DOUBLE DEFAULT 0,
    timing_json VARCHAR,
    error_code VARCHAR,
    error_message VARCHAR
);
CREATE TABLE IF NOT EXISTS absence_calculation_canonical (
    period VARCHAR PRIMARY KEY,
    calculation_id VARCHAR NOT NULL,
    calculation_version VARCHAR NOT NULL,
    engine VARCHAR NOT NULL,
    input_fingerprint VARCHAR NOT NULL,
    reset_generation BIGINT NOT NULL,
    activated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_absence_batch_period_created
    ON absence_calculation_batch(period, created_at);
CREATE INDEX IF NOT EXISTS idx_absence_batch_status
    ON absence_calculation_batch(status);
"""


def ensure_absence_calculation_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_SQL)


def recover_interrupted_batches(db_path: str | Path) -> int:
    """A backend restart deterministically terminalizes abandoned work."""
    with duckdb.connect(str(db_path)) as conn:
        ensure_absence_calculation_schema(conn)
        changed = conn.execute(
            """UPDATE absence_calculation_batch
               SET status='FAILED',canonical=false,error_code='PROCESS_RESTARTED',
                   error_message='Backend dimulai ulang sebelum calculation selesai.',
                   completed_at=now(),updated_at=now()
               WHERE status IN ('PENDING','RUNNING') RETURNING calculation_id"""
        ).fetchall()
        return len(changed)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone()[0])


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    if not _table_exists(conn, table):
        return []
    return [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]


def _load_config(conn: duckdb.DuckDBPyConnection) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _table_exists(conn, "workflow_config"):
        raise AbsenceCalculationError("CONFIG_NOT_AVAILABLE", "workflow_config canonical tidak tersedia")
    cursor = conn.execute('SELECT * FROM "workflow_config" LIMIT 2')
    values = cursor.fetchall()
    if len(values) != 1:
        raise AbsenceCalculationError(
            "CONFIG_NOT_SINGLETON", "workflow_config harus berisi tepat satu konfigurasi aktif"
        )
    row = dict(zip([item[0] for item in cursor.description], values[0]))
    workflow = _parse_json(row.pop("_workflow", None), {})
    for key, fallback in (
        ("holidays", []), ("jabatanMaster", []), ("jamDasar", {}), ("flexi", {}),
        ("keterlambatanTiers", []), ("pulangCepatTiers", []),
        ("jenisNoPotongan", []), ("leavePeriods", []),
    ):
        row[key] = _parse_json(row.get(key), fallback)
    return row, workflow


def _load_master(
    conn: duckdb.DuckDBPyConnection, nips: set[str]
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "master_data"):
        raise AbsenceCalculationError("MASTER_NOT_AVAILABLE", "Master SDM canonical tidak tersedia")
    columns = {name.lower(): name for name in _table_columns(conn, "master_data")}
    required = columns.get("nip")
    if not required:
        raise AbsenceCalculationError("MASTER_SCHEMA_INVALID", "Master SDM tidak mempunyai NIP")
    rows = conn.execute('SELECT * FROM "master_data" ORDER BY CAST(nip AS VARCHAR)').fetchall()
    names = _table_columns(conn, "master_data")
    result = []
    for values in rows:
        source = dict(zip(names, values))
        nip = str(source.get(columns["nip"]) or "").strip()
        if nip not in nips:
            continue
        gross = source.get(columns.get("tukin_gross")) if columns.get("tukin_gross") else None
        if gross is None and columns.get("tukin"):
            gross = source.get(columns["tukin"])
        result.append({
            "nip": nip,
            "nama": source.get(columns.get("nama")) if columns.get("nama") else "",
            "jabatan": source.get(columns.get("jabatan")) if columns.get("jabatan") else "",
            "tukin": gross,
        })
    return result


def load_canonical_source(
    conn: duckdb.DuckDBPyConnection, period: str
) -> dict[str, Any]:
    if not PERIOD_PATTERN.fullmatch(period):
        raise AbsenceCalculationError("INVALID_PERIOD", "Periode wajib menggunakan format YYYY-MM")
    if not _table_exists(conn, "raw_import"):
        raise AbsenceCalculationError("RAW_IMPORT_NOT_AVAILABLE", "raw_import canonical tidak tersedia")
    config, workflow = _load_config(conn)
    columns = _table_columns(conn, "raw_import")
    if {"workflow_id", "tanggal"} <= set(columns):
        cursor = conn.execute("""SELECT * FROM raw_import
            WHERE workflow_id = ? AND substr(CAST(tanggal AS VARCHAR), 1, 7) = ?
            ORDER BY rowid""", [workflow.get("workflow_id") or workflow.get("workflowId") or "default", period])
    else:
        cursor = conn.execute('SELECT * FROM "raw_import" ORDER BY rowid')
    headers = [item[0] for item in cursor.description]
    rows = [dict(zip(headers, values)) for values in cursor.fetchall()]
    column_map = detect_import_columns(headers)
    if "nip" not in column_map or "tanggal" not in column_map:
        raise AbsenceCalculationError("RAW_IMPORT_SCHEMA_INVALID", "raw_import tidak mempunyai NIP/tanggal")
    records = normalize_raw_rows(headers, rows)
    # Backend Import V1 promotes already-paired Wali rows into the canonical
    # raw contract. Preserve the legacy import-mode marker consumed by the
    # existing duration branches without changing any formula.
    if str(workflow.get("importMode") or "").lower() == "wali":
        for record in records:
            record["sourceFormat"] = "wali"
    selected = [record for record in records if record["tanggal"]["monthKey"] == period]
    if not selected:
        raise AbsenceCalculationError("NO_SOURCE_FOR_PERIOD", f"Tidak ada raw_import untuk periode {period}")
    selected_indices = {int(str(record["_id"]).rsplit("-", 1)[-1]) for record in selected}
    selected_raw = [row for index, row in enumerate(rows) if index in selected_indices]
    active_period = str(workflow.get("month") or "").strip()
    if "workflow_id" not in columns and active_period and active_period != period:
        raise AbsenceCalculationError(
            "WORKFLOW_PERIOD_MISMATCH",
            f"Workflow aktif {active_period}, bukan periode yang diminta {period}",
        )
    master = _load_master(conn, {record["nip"] for record in selected})
    source = {
        "period": period,
        "headers": headers,
        "records": selected,
        "config": config,
        "workflow": workflow,
        "master": master,
        "time_mode": workflow.get("timeMode") or "original",
    }
    source["input_fingerprint"] = _canonical_hash({
        "period": period,
        "raw_rows": selected_raw,
        "config": config,
        "workflow_material": {
            key: workflow.get(key) for key in (
                "month", "timeMode", "importMode", "workflow_id", "workflowId"
            )
        },
        "master": master,
    })
    return source


def calculate_source(source: dict[str, Any]) -> dict[str, Any]:
    timings: dict[str, float] = {}
    started_total = time.perf_counter()
    context = build_context(source["config"], source["master"])
    started = time.perf_counter()
    daily = [
        compute_record(record, context, source.get("time_mode") or "original")
        for record in source["records"]
    ]
    timings["harian_ms"] = (time.perf_counter() - started) * 1000

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in daily:
        groups.setdefault(f"{row.get('monthKey')}|{row.get('nip')}", []).append(row)
    started = time.perf_counter()
    rekap = [compute_rekap(group, context) for group in groups.values()]
    rekap.sort(key=lambda row: (str(row.get("bulan")), str(row.get("nip"))))
    timings["rekap_ms"] = (time.perf_counter() - started) * 1000

    master = {str(row.get("nip")): row for row in source["master"]}
    started = time.perf_counter()
    final_absensi = []
    for row in rekap:
        person = master.get(str(row.get("nip")))
        final_absensi.append(compute_final_absensi(
            row["nip"], row["nama"], row["jabatan"], row["bulan"],
            person.get("tukin") if person else None, row["total"],
        ))
    timings["final_ms"] = (time.perf_counter() - started) * 1000
    timings["total_calculation_ms"] = (time.perf_counter() - started_total) * 1000
    return {"daily": daily, "rekap": rekap, "final_absensi": final_absensi, "timings": timings}


def validate_results(period: str, source: dict[str, Any], results: dict[str, Any]) -> None:
    daily, rekap, final = results["daily"], results["rekap"], results["final_absensi"]
    if len(daily) != len(source["records"]):
        raise AbsenceCalculationError("RESULT_COUNT_INVALID", "Jumlah Harian tidak sama dengan input")
    if len(rekap) != len(final):
        raise AbsenceCalculationError("RESULT_COUNT_INVALID", "Jumlah Rekap dan Final berbeda")
    for stage, rows, period_key in (
        ("HARIAN", daily, "monthKey"), ("REKAP", rekap, "bulan"),
        ("FINAL", final, "bulan"),
    ):
        if any(str(row.get(period_key) or "") != period for row in rows):
            raise AbsenceCalculationError("RESULT_PERIOD_INVALID", f"{stage} keluar dari scope {period}")
    for stage, rows in (("REKAP", rekap), ("FINAL", final)):
        keys = [(str(row.get("nip")), str(row.get("bulan"))) for row in rows]
        if len(keys) != len(set(keys)):
            raise AbsenceCalculationError("RESULT_DUPLICATE", f"{stage} mempunyai NIP/periode duplikat")


COMPARISON_FIELDS = {
    "daily": (
        "nip", "tanggalKey", "monthKey", "jamMasuk", "jamPulang", "durasiMin",
        "potKeterlambatan", "potPulangCepat", "potDurasi", "potTidakLengkap",
        "totalHarian", "statusLengkap", "statusAbsensi", "flags", "_lateMin", "_earlyMin",
    ),
    "rekap": (
        "nip", "bulan", "jumlahRecord", "hariEfektif", "hariAdaAbsensi", "hariKurang",
        "potKeterlambatan", "potPulangCepat", "potDurasi", "potHariKurang",
        "potTidakLengkap", "totalRaw", "total", "status",
    ),
    "final_absensi": (
        "nip", "bulan", "potPct", "tukin", "komponen", "nominal", "finalDibayar",
    ),
}


def _decode_cell(value: Any) -> Any:
    value = _json_value(value)
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _read_period_rows(conn: duckdb.DuckDBPyConnection, table: str, period: str) -> list[dict[str, Any]]:
    columns = _table_columns(conn, table)
    if not columns or columns == ["_empty"]:
        return []
    lower = {name.lower(): name for name in columns}
    if "bulan" in lower:
        where, params = f'CAST("{lower["bulan"]}" AS VARCHAR)=?', [period]
    elif "monthkey" in lower:
        where, params = f'CAST("{lower["monthkey"]}" AS VARCHAR)=?', [period]
    elif "tanggalkey" in lower:
        where, params = f'substr(CAST("{lower["tanggalkey"]}" AS VARCHAR),1,7)=?', [period]
    else:
        return []
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid', params)
    names = [item[0] for item in cursor.description]
    return [{name: _decode_cell(value) for name, value in zip(names, row)} for row in cursor.fetchall()]


def compare_shadow_reference(
    reference: dict[str, list[dict[str, Any]]], results: dict[str, Any]
) -> dict[str, Any]:
    summaries = []
    details = []
    for stage in ("daily", "rekap", "final_absensi"):
        if stage == "daily":
            key = lambda row: f"{row.get('nip')}|{row.get('tanggalKey')}"
        else:
            key = lambda row: f"{row.get('nip')}|{row.get('bulan')}"
        left = {key(row): row for row in reference.get(stage, [])}
        right = {key(row): row for row in results.get(stage, [])}
        fields = matches = mismatches = 0
        for record_key in sorted(set(left) | set(right)):
            for field in COMPARISON_FIELDS[stage]:
                fields += 1
                js_value = (left.get(record_key) or {}).get(field, {"__missing__": True})
                py_value = (right.get(record_key) or {}).get(field, {"__missing__": True})
                missing_js = js_value == {"__missing__": True}
                missing_py = py_value == {"__missing__": True}
                # Sparse JavaScript object properties are materialized as SQL NULL
                # when a heterogeneous result set is persisted through a frame.
                # The database representation therefore cannot retain the
                # missing-vs-null distinction, and both mean "no value" here.
                persistence_null_equal = (
                    (missing_js and py_value is None) or (missing_py and js_value is None)
                )
                if js_value == py_value or persistence_null_equal:
                    matches += 1
                else:
                    mismatches += 1
                    if len(details) < 200:
                        details.append({
                            "record_key_hash": hashlib.sha256(record_key.encode()).hexdigest()[:16],
                            "stage": stage.upper(), "field": field,
                            "js": js_value, "python": py_value,
                        })
        summaries.append({
            "stage": stage.upper(), "records": len(right), "fields": fields,
            "matches": matches, "mismatches": mismatches,
        })
    return {
        "stages": summaries,
        "fields_compared": sum(row["fields"] for row in summaries),
        "mismatches": sum(row["mismatches"] for row in summaries),
        "details": details,
    }


def _rows_for_storage(
    rows: list[dict[str, Any]], calculation_id: str
) -> list[dict[str, Any]]:
    output = []
    for source in rows:
        row = {}
        for key, value in source.items():
            value = _json_value(value)
            row[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) \
                if isinstance(value, (dict, list)) else value
        row.update({
            "calculation_id": calculation_id,
            "calculation_version": PYTHON_CALCULATION_VERSION,
            "calculation_engine": "PYTHON",
        })
        output.append(row)
    return output


def _replace_period(
    conn: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]], period: str,
    period_column: str, calculation_id: str,
) -> None:
    import pandas as pd

    frame = pd.DataFrame(_rows_for_storage(rows, calculation_id))
    relation = f"tmp_{table}_{uuid.uuid4().hex[:10]}"
    keep = f"tmp_keep_{table}_{uuid.uuid4().hex[:10]}"
    conn.register(relation, frame)
    try:
        columns = _table_columns(conn, table)
        if columns and columns != ["_empty"]:
            lower = {name.lower(): name for name in columns}
            existing_period = lower.get(period_column.lower())
            if not existing_period and period_column == "monthKey":
                existing_period = lower.get("bulan") or lower.get("tanggalkey")
            if existing_period == period_column:
                old_schema = dict((row[0], row[1]) for row in conn.execute(f'DESCRIBE "{table}"').fetchall())
                new_schema = dict((row[0], row[1]) for row in conn.execute(f'DESCRIBE "{relation}"').fetchall())
                if old_schema == new_schema:
                    conn.execute(f'DELETE FROM "{table}" WHERE "{period_column}"=?', [period])
                    conn.execute(f'INSERT INTO "{table}" BY NAME SELECT * FROM "{relation}"')
                    return
            if existing_period:
                period_expression = f'CAST("{existing_period}" AS VARCHAR)'
                if existing_period.lower() == "tanggalkey":
                    period_expression = f'substr(CAST("{existing_period}" AS VARCHAR),1,7)'
                conn.execute(
                    f'CREATE TEMP TABLE "{keep}" AS SELECT * FROM "{table}" '
                    f'WHERE {period_expression}<>?', [period]
                )
            else:
                conn.execute(f'CREATE TEMP TABLE "{keep}" AS SELECT * FROM "{table}" LIMIT 0')
            conn.execute(
                f'CREATE OR REPLACE TABLE "{table}" AS '
                f'SELECT * FROM "{keep}" UNION ALL BY NAME SELECT * FROM "{relation}"'
            )
            conn.execute(f'DROP TABLE "{keep}"')
        else:
            conn.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM "{relation}"')
    finally:
        try:
            conn.unregister(relation)
        except Exception:
            pass


_PERIOD_LOCKS: dict[str, threading.Lock] = {}
_PERIOD_LOCKS_GUARD = threading.Lock()
_DISPATCH_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="absence-calc")


def shutdown_calculation_executor() -> None:
    _EXECUTOR.shutdown(wait=True, cancel_futures=True)


def _period_lock(period: str) -> threading.Lock:
    with _PERIOD_LOCKS_GUARD:
        return _PERIOD_LOCKS.setdefault(period, threading.Lock())


class AbsenceCalculationService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        authority_config: AuthorityConfig | None = None,
        generation_reader: Callable[[], int] = current_reset_generation,
        generation_validator: Callable[[int], None] = assert_current_generation,
        write_gate: threading.Lock = DATABASE_GATE,
        before_commit: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.db_path = str(Path(db_path).resolve())
        self.authority_config = authority_config or current_authority_config()
        self.generation_reader = generation_reader
        self.generation_validator = generation_validator
        self.write_gate = write_gate
        self.before_commit = before_commit

    def authority_info(self) -> dict[str, Any]:
        return {
            "authority": self.authority_config.authority.value,
            "default_authority": CalculationAuthority.JS_LEGACY.value,
            "python_canonical_enabled": self.authority_config.canonical_enabled,
            "python_calculation_version": PYTHON_CALCULATION_VERSION,
            "readiness": readiness_gate(),
        }

    def submit(self, period: str, requested_by: str, rerun_reason: str = "", *, reuse: bool = False) -> dict[str, Any]:
        if not PERIOD_PATTERN.fullmatch(str(period or "")):
            raise AbsenceCalculationError("INVALID_PERIOD", "Periode wajib menggunakan format YYYY-MM")
        actor = str(requested_by or "").strip()
        if not actor:
            raise AbsenceCalculationError("ACTOR_REQUIRED", "Petugas calculation wajib diisi")
        authority = self.authority_config.authority
        if authority == CalculationAuthority.JS_LEGACY:
            raise AbsenceCalculationError(
                "JS_LEGACY_AUTHORITY",
                "Authority aktif masih JS_LEGACY; backend Python tidak dijalankan.",
            )
        gate = readiness_gate()
        if not gate["ready"]:
            raise AbsenceCalculationError("PARITY_GATE_FAILED", "Fingerprint baseline/golden tidak valid")
        if authority == CalculationAuthority.PYTHON_CANONICAL and not self.authority_config.canonical_enabled:
            raise AbsenceCalculationError(
                "PYTHON_CANONICAL_NOT_ENABLED",
                "PYTHON_CANONICAL memerlukan activation gate eksplisit.",
            )
        generation = int(self.generation_reader())
        calculation_id = str(uuid.uuid4())
        with _DISPATCH_LOCK, duckdb.connect(self.db_path) as conn:
            ensure_absence_calculation_schema(conn)
            active = conn.execute(
                """SELECT calculation_id FROM absence_calculation_batch
                   WHERE period=? AND status IN ('PENDING','RUNNING') LIMIT 1""", [period]
            ).fetchone()
            if active:
                raise AbsenceCalculationError(
                    "CALCULATION_ALREADY_ACTIVE",
                    f"Periode {period} sudah mempunyai calculation aktif.",
                )
            previous = conn.execute(
                """SELECT calculation_id FROM absence_calculation_batch
                   WHERE period=? ORDER BY created_at DESC LIMIT 1""", [period]
            ).fetchone()
            reason = str(rerun_reason or "").strip()
            if previous and not reason:
                reason = "Deterministic auditable rerun"
            conn.execute(
                """INSERT INTO absence_calculation_batch
                   (calculation_id,period,authority,engine,calculation_version,
                    reset_generation,source_formula_fingerprint,status,canonical,
                    requested_by,rerun_reason,previous_calculation_id,timing_json)
                   VALUES (?,?,?,?,?,?,?,?,false,?,?,?,?)""",
                [calculation_id, period, authority.value, "PYTHON",
                 PYTHON_CALCULATION_VERSION, generation, formula_source_fingerprint(),
                 "PENDING", actor, reason, previous[0] if previous else None,
                 json.dumps({"reuse_requested": reuse})],
            )
        return self.get(calculation_id)

    def submit_background(self, period: str, requested_by: str, rerun_reason: str = "", *, reuse: bool = False) -> dict[str, Any]:
        batch = self.submit(period, requested_by, rerun_reason, reuse=reuse)
        _EXECUTOR.submit(self.run_batch, batch["calculation_id"])
        return batch

    def calculate_sync(self, period: str, requested_by: str, rerun_reason: str = "", *, reuse: bool = False) -> dict[str, Any]:
        batch = self.submit(period, requested_by, rerun_reason, reuse=reuse)
        self.run_batch(batch["calculation_id"])
        return self.get(batch["calculation_id"])

    def _terminal_failure(self, calculation_id: str, status: str, code: str, message: str) -> None:
        with self.write_gate:
            with duckdb.connect(self.db_path) as conn:
                ensure_absence_calculation_schema(conn)
                conn.execute(
                    """UPDATE absence_calculation_batch SET status=?,canonical=false,error_code=?,
                       error_message=?,completed_at=now(),updated_at=now()
                       WHERE calculation_id=?""", [status, code, message[:2000], calculation_id]
                )

    def run_batch(self, calculation_id: str) -> None:
        batch = self.get(calculation_id)
        period = batch["period"]
        authority = CalculationAuthority(batch["authority"])
        lock = _period_lock(period)
        if not lock.acquire(blocking=False):
            self._terminal_failure(
                calculation_id, "FAILED", "CALCULATION_ALREADY_ACTIVE",
                f"Calculation lain untuk {period} sedang berjalan.",
            )
            return
        started_total = time.perf_counter()
        gate_wait_ms = 0.0
        source_loading_ms = 0.0
        try:
            gate_wait_started = time.perf_counter()
            with self.write_gate:
                gate_wait_ms += (time.perf_counter() - gate_wait_started) * 1000
                try:
                    self.generation_validator(batch["reset_generation"])
                except Exception as exc:
                    raise AbsenceCalculationError(
                        "STALE_GENERATION", "Reset generation berubah sebelum calculation dimulai"
                    ) from exc
                with duckdb.connect(self.db_path) as conn:
                    source_started = time.perf_counter()
                    ensure_absence_calculation_schema(conn)
                    conn.execute(
                        """UPDATE absence_calculation_batch SET status='RUNNING',started_at=now(),
                           updated_at=now() WHERE calculation_id=? AND status='PENDING'""",
                        [calculation_id],
                    )
                    source = load_canonical_source(conn, period)
                    reference = {
                        "daily": _read_period_rows(conn, "hitung_harian", period),
                        "rekap": _read_period_rows(conn, "rekap_sdm", period),
                        "final_absensi": _read_period_rows(conn, "final_absensi", period),
                    } if authority == CalculationAuthority.PYTHON_SHADOW else None
                    conn.execute(
                        """UPDATE absence_calculation_batch SET input_fingerprint=?,raw_count=?,
                           updated_at=now() WHERE calculation_id=?""",
                        [source["input_fingerprint"], len(source["records"]), calculation_id],
                    )
                    source_loading_ms += (time.perf_counter() - source_started) * 1000
                    if authority == CalculationAuthority.PYTHON_CANONICAL and batch["timings"].get("reuse_requested"):
                        prior = conn.execute("""
                            SELECT b.calculation_id,b.daily_count,b.rekap_count,b.final_count
                            FROM absence_calculation_canonical c
                            JOIN absence_calculation_batch b USING(calculation_id)
                            WHERE c.period=? AND c.input_fingerprint=? AND c.reset_generation=?
                              AND b.source_formula_fingerprint=? AND b.status='COMPLETED' AND b.canonical
                        """, [period, source["input_fingerprint"], batch["reset_generation"],
                              batch["source_formula_fingerprint"]]).fetchone()
                        if prior and all("calculation_id" in _table_columns(conn, table) and
                                         conn.execute(f'SELECT count(*) FROM "{table}" WHERE calculation_id=?',
                                                      [prior[0]]).fetchone()[0] == expected
                                         for table, expected in zip(("hitung_harian", "rekap_sdm", "final_absensi"), prior[1:])):
                            conn.execute("""
                                UPDATE absence_calculation_batch SET status='COMPLETED',canonical=true,
                                    daily_count=?,rekap_count=?,final_count=?,duration_ms=?,timing_json=?,
                                    completed_at=now(),updated_at=now() WHERE calculation_id=?
                            """, [*prior[1:], (time.perf_counter() - started_total) * 1000,
                                  json.dumps({"reused_from": prior[0]}), calculation_id])
                            return

            results = calculate_source(source)
            source_workflow_id = (source["workflow"].get("workflow_id") or
                                  source["workflow"].get("workflowId") or "default")
            for name in ("daily", "rekap", "final_absensi"):
                for row in results[name]:
                    row["workflow_id"] = source_workflow_id
            results["timings"].update({
                "source_workflow_id": source_workflow_id,
                "source_loading_ms": source_loading_ms,
                "gate_wait_ms": gate_wait_ms,
            })
            validate_results(period, source, results)
            if self.before_commit:
                self.before_commit(calculation_id, source)

            if authority == CalculationAuthority.PYTHON_SHADOW:
                comparison = compare_shadow_reference(reference, results)
                duration = (time.perf_counter() - started_total) * 1000
                gate_wait_started = time.perf_counter()
                with self.write_gate:
                    gate_wait_ms += (time.perf_counter() - gate_wait_started) * 1000
                    results["timings"]["gate_wait_ms"] = gate_wait_ms
                    try:
                        self.generation_validator(batch["reset_generation"])
                    except Exception as exc:
                        raise AbsenceCalculationError(
                            "STALE_GENERATION", "Reset generation berubah selama shadow calculation"
                        ) from exc
                    with duckdb.connect(self.db_path) as conn:
                        current = load_canonical_source(conn, period)
                        if current["input_fingerprint"] != source["input_fingerprint"]:
                            raise AbsenceCalculationError(
                                "SOURCE_CHANGED_DURING_CALCULATION",
                                "Sumber material berubah selama shadow calculation.",
                            )
                        conn.execute(
                            """UPDATE absence_calculation_batch SET status='SHADOW_ONLY',canonical=false,
                               daily_count=?,rekap_count=?,final_count=?,fields_compared=?,mismatch_count=?,
                               duration_ms=?,timing_json=?,completed_at=now(),updated_at=now()
                               WHERE calculation_id=?""",
                            [len(results["daily"]), len(results["rekap"]), len(results["final_absensi"]),
                             comparison["fields_compared"], comparison["mismatches"], duration,
                             json.dumps({**results["timings"], "comparison": comparison}, default=str),
                             calculation_id],
                        )
                return

            gate_wait_started = time.perf_counter()
            with self.write_gate:
                gate_wait_ms += (time.perf_counter() - gate_wait_started) * 1000
                results["timings"]["gate_wait_ms"] = gate_wait_ms
                try:
                    self.generation_validator(batch["reset_generation"])
                except Exception as exc:
                    raise AbsenceCalculationError(
                        "STALE_GENERATION", "Reset generation berubah selama calculation"
                    ) from exc
                with duckdb.connect(self.db_path) as conn:
                    ensure_absence_calculation_schema(conn)
                    current = load_canonical_source(conn, period)
                    if current["input_fingerprint"] != source["input_fingerprint"]:
                        raise AbsenceCalculationError(
                            "SOURCE_CHANGED_DURING_CALCULATION",
                            "Sumber material berubah selama calculation; hasil tidak disimpan.",
                        )
                    del current
                    conn.execute("BEGIN TRANSACTION")
                    try:
                        persistence_started = time.perf_counter()
                        _replace_period(
                            conn, "hitung_harian", results["daily"], period, "monthKey", calculation_id
                        )
                        _replace_period(
                            conn, "rekap_sdm", results["rekap"], period, "bulan", calculation_id
                        )
                        _replace_period(
                            conn, "final_absensi", results["final_absensi"], period, "bulan", calculation_id
                        )
                        conn.execute(
                            """INSERT INTO absence_calculation_canonical
                               (period,calculation_id,calculation_version,engine,input_fingerprint,
                                reset_generation,activated_at) VALUES (?,?,?,?,?,?,now())
                               ON CONFLICT(period) DO UPDATE SET calculation_id=excluded.calculation_id,
                                calculation_version=excluded.calculation_version,engine=excluded.engine,
                                input_fingerprint=excluded.input_fingerprint,
                                reset_generation=excluded.reset_generation,activated_at=now()""",
                            [period, calculation_id, PYTHON_CALCULATION_VERSION, "PYTHON",
                             source["input_fingerprint"], batch["reset_generation"]],
                        )
                        results["timings"]["persistence_ms"] = (
                            time.perf_counter() - persistence_started
                        ) * 1000
                        duration = (time.perf_counter() - started_total) * 1000
                        results["timings"]["total_end_to_end_ms"] = duration
                        conn.execute(
                            """UPDATE absence_calculation_batch SET status='COMPLETED',canonical=true,
                               daily_count=?,rekap_count=?,final_count=?,duration_ms=?,timing_json=?,
                               completed_at=now(),updated_at=now() WHERE calculation_id=?""",
                            [len(results["daily"]), len(results["rekap"]), len(results["final_absensi"]),
                             duration, json.dumps(results["timings"]), calculation_id],
                        )
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
        except AbsenceCalculationError as exc:
            status = "STALE_GENERATION" if exc.code == "STALE_GENERATION" else (
                "STALE_SOURCE" if exc.code == "SOURCE_CHANGED_DURING_CALCULATION" else "FAILED"
            )
            self._terminal_failure(calculation_id, status, exc.code, exc.message)
        except Exception as exc:
            self._terminal_failure(calculation_id, "FAILED", "CALCULATION_FAILED", str(exc))
        finally:
            lock.release()

    def get(self, calculation_id: str) -> dict[str, Any]:
        with duckdb.connect(self.db_path) as conn:
            ensure_absence_calculation_schema(conn)
            cursor = conn.execute(
                "SELECT * FROM absence_calculation_batch WHERE calculation_id=?", [calculation_id]
            )
            row = cursor.fetchone()
            if not row:
                raise KeyError(calculation_id)
            result = {name[0]: _json_value(value) for name, value in zip(cursor.description, row)}
            timing = _parse_json(result.get("timing_json"), {})
            result["timings"] = timing
            result["published_calculation_id"] = timing.get("reused_from") or calculation_id
            result["counts"] = {
                "raw": int(result.get("raw_count") or 0),
                "daily": int(result.get("daily_count") or 0),
                "rekap": int(result.get("rekap_count") or 0),
                "final": int(result.get("final_count") or 0),
            }
            if isinstance(timing, dict) and timing.get("comparison"):
                result["mismatch_summary"] = timing["comparison"]
            return result

    def latest(self, period: str) -> dict[str, Any] | None:
        with duckdb.connect(self.db_path) as conn:
            ensure_absence_calculation_schema(conn)
            row = conn.execute(
                """SELECT calculation_id FROM absence_calculation_batch
                   WHERE period=? ORDER BY created_at DESC LIMIT 1""", [period]
            ).fetchone()
        return self.get(row[0]) if row else None


def assert_canonical_absence_ready(
    conn: duckdb.DuckDBPyConnection, period: str,
    authority_config: AuthorityConfig | None = None,
) -> None:
    """Protect downstream reads only when Python is configured as authority.

    JS_LEGACY and PYTHON_SHADOW retain the existing downstream contract.
    """
    config = authority_config or current_authority_config()
    if config.authority != CalculationAuthority.PYTHON_CANONICAL:
        return
    ensure_absence_calculation_schema(conn)
    row = conn.execute(
        """SELECT b.status,b.canonical,b.authority,b.calculation_id,b.final_count
           FROM absence_calculation_canonical c
           JOIN absence_calculation_batch b ON b.calculation_id=c.calculation_id
           WHERE c.period=?""", [period]
    ).fetchone()
    valid = bool(row and row[0] == "COMPLETED" and row[1] and row[2] == "PYTHON_CANONICAL")
    columns = {name.lower(): name for name in _table_columns(conn, "final_absensi")}
    if valid and {"bulan", "calculation_id"} <= set(columns):
        count = conn.execute(
            f'''SELECT count(*) FROM "final_absensi"
                WHERE CAST("{columns["bulan"]}" AS VARCHAR)=?
                  AND CAST("{columns["calculation_id"]}" AS VARCHAR)=?''',
            [period, row[3]],
        ).fetchone()[0]
        valid = int(count) == int(row[4]) and int(count) > 0
    else:
        valid = False
    if not valid:
        raise AbsenceCalculationError(
            "CANONICAL_ABSENCE_NOT_READY",
            "Final Absensi belum berasal dari batch Python canonical COMPLETED.",
        )


def create_router(db_path_getter: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/api/absence", tags=["absence-calculation"])

    def service() -> AbsenceCalculationService:
        return AbsenceCalculationService(db_path_getter())

    @router.get("/authority")
    def authority() -> dict[str, Any]:
        return service().authority_info()

    @router.post("/calculate")
    def calculate(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return service().submit_background(
                str(payload.get("period") or payload.get("periode") or ""),
                str(payload.get("actor") or payload.get("requested_by") or ""),
                str(payload.get("reason") or payload.get("rerun_reason") or ""),
                reuse=payload.get("reuse") is True,
            )
        except AbsenceCalculationError as exc:
            status = 409 if exc.code in {
                "JS_LEGACY_AUTHORITY", "PYTHON_CANONICAL_NOT_ENABLED",
                "CALCULATION_ALREADY_ACTIVE", "PARITY_GATE_FAILED",
            } else 400
            raise HTTPException(status, detail={"code": exc.code, "message": exc.message}) from exc

    @router.get("/calculation/latest")
    def latest(period: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
        found = service().latest(period)
        if not found:
            raise HTTPException(404, "Calculation batch belum tersedia")
        return found

    @router.get("/calculation/{calculation_id}")
    def calculation(calculation_id: str) -> dict[str, Any]:
        try:
            return service().get(calculation_id)
        except KeyError as exc:
            raise HTTPException(404, "Calculation batch tidak ditemukan") from exc

    return router

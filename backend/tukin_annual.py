"""Annual Tukin management service and API.

The module keeps imported history immutable by period, applies the official
Permensos 6/2023 SKP rates, exposes semester/year matrices, and produces final
monthly or annual Excel downloads from DuckDB.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
import re
import threading
import uuid
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from backend.workflow_readiness import WorkflowNotReadyError, validate_final_readiness
from typing import Any, Callable, Iterable, Mapping, Optional

import duckdb
from backend.lazy_imports import lazy_module

pd = lazy_module("pandas")
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from backend.skp_assessment import (
    VALID_PREDICATES as CANONICAL_SKP_RATES,
    classify_skp_report_status,
)

ensure_tukin_annual_schema = importlib.import_module(
    "backend.migrations.034_create_tukin_annual_management"
).ensure_tukin_annual_schema
ensure_deceased_schema = importlib.import_module(
    "backend.migrations.038_create_deceased_sdm_lifecycle"
).ensure_deceased_sdm_schema
ensure_condition_schema = importlib.import_module(
    "backend.migrations.039_create_skp_condition_decisions"
).ensure_skp_condition_decision_schema
ensure_third_missing_unpaid = importlib.import_module(
    "backend.migrations.041_enforce_third_missing_skp_unpaid"
).ensure_third_missing_skp_unpaid
ensure_operational_schema = importlib.import_module(
    "backend.migrations.049_create_skp_operational_workflow"
).ensure_skp_operational_schema


MONTH_NAMES = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
    5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
    9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}
MONTH_LOOKUP = {name.lower(): number for number, name in MONTH_NAMES.items()}
PREDICATE_RATES = dict(CANONICAL_SKP_RATES)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_ANNUAL_READ_LOCK = threading.RLock()
_ANNUAL_READ_BASE: dict[str, duckdb.DuckDBPyConnection] = {}


def _clean_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


ALIASES = {
    "nip": ("nip", "nomorpegawai", "noindukpegawai"),
    "nama": ("nama", "namapegawai", "namasdm"),
    "penilai": ("penilai", "atasanpenilai"),
    "kd_anak_satker": ("kdanaksakter", "kdanaksatker", "kodeanaksatker"),
    "jabatan": ("jabatan",),
    "unit": ("unit", "unitkerja", "satker", "satuankerja"),
    "kelas_jabatan": ("kelasjabatan", "kelas", "grade"),
    "tahap": ("tahap", "periode"),
    "wilayah_3t": ("3t", "wilayah3t", "status3t"),
    "status_sdm": ("sdm", "statussdm"),
    "jabatan_sdm": ("sdmjabatan", "jabatansdm"),
    "predicate_skp": ("predikatkinerjaperiodik", "predikat", "nilaiskp", "predikatskp", "tnekin"),
    "report_status": ("tidakbuatskp", "statuslaporanskp", "statusskp"),
    "tahun": ("tahun",),
    "bulan": ("bulan",),
    "skp_reduction_rate": ("potonganskp", "potonganskp%", "potonganskppersen", "skppct"),
    "attendance_reduction_rate": (
        "potonganabsensieabsensi", "potonganabsensieabsensi%", "potonganabsensiesdm",
        "potonganabsensi", "potabsenmanual", "potconfigtendik",
    ),
    "tukin_gross": ("tukin", "tukinbruto", "totaltukin"),
    "skp_deduction": ("potonganskpfinal", "nominalpotonganskp"),
    "attendance_deduction": ("potonganabsenfinal", "potongankehadiraneabsensi", "nominalpotonganabsensi"),
    "total_deduction": ("potonganfinal", "totalpotongan", "totalnominalpotongan"),
    "paid_amount": ("finaltukindibayar", "tukindibayar", "tukindibayaresdm", "tukindibayareabsensi", "nominaldibayarkan"),
    "payment_status": ("statuspembayaran", "statusbayar"),
    "payment_reason": ("alasanstatus", "alasantidakdibayarkan", "keterangan"),
    "province": ("provinsi",),
    "regency": ("kabupaten", "kabupatenkota", "kotakabupaten"),
    "account_number": ("norekening", "nomorrekening", "rekening"),
    "bank_name": ("bank", "namabank"),
    "account_holder": ("namapemilikrekening", "pemilikrekening", "namarekening"),
    "work_date": ("tanggal", "tanggalkey", "tanggalabsen", "tanggalhari"),
    "day_name": ("hari",),
    "check_in": ("jammasuk", "masuk", "kedatangan"),
    "check_out": ("jampulang", "pulang", "kepulangan"),
    "attendance_type": ("jenis", "jenisabsen", "jenisabsensi"),
    "attendance_status": ("status", "statusabsen", "statuslengkap"),
    "daily_reduction_rate": ("totalharian", "persenpotongan", "potonganharian"),
    "reason": ("alasan", "flags", "keterangan"),
    "evidence_ref": ("eviden", "bukti", "evidenceref"),
}


def _mapping(headers: Iterable[Any]) -> dict[str, str]:
    normalized = {_clean_header(header): str(header) for header in headers}
    result: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            key = _clean_header(alias)
            if key in normalized:
                result[field] = normalized[key]
                break
        if field in result:
            continue
        for key, original in normalized.items():
            if any(_clean_header(alias) in key for alias in aliases):
                result[field] = original
                break
    return result


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return default if (isinstance(value, float) and math.isnan(value)) else float(value)
    original = str(value).strip()
    is_currency = "RP" in original.upper()
    raw = original.replace("Rp", "").replace("RP", "").replace("%", "")
    raw = raw.replace("\u00a0", "").replace(" ", "")
    if not raw:
        return default
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        if raw.count(",") > 1 or (is_currency and len(raw.rsplit(",", 1)[1]) == 3):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif "." in raw and is_currency and len(raw.rsplit(".", 1)[1]) == 3:
        raw = raw.replace(".", "")
    raw = re.sub(r"[^0-9.+-]", "", raw)
    try:
        number = float(raw)
        return -number if negative else number
    except ValueError:
        return default


def _rate(value: Any, default: float = 0.0) -> float:
    number = _number(value, default)
    if "%" not in str(value) and 0 < abs(number) <= 1:
        number *= 100
    return max(0.0, min(100.0, number))


def _period(value: str) -> str:
    raw = _text(value)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", raw):
        raise ValueError("Periode harus berformat YYYY-MM")
    return raw


def _predicate(value: Any) -> str:
    raw = re.sub(r"\s+", " ", _text(value)).upper()
    return raw if raw in PREDICATE_RATES else raw


def _report_status(predicate: str, raw_status: Any) -> tuple[str, Optional[str]]:
    status = classify_skp_report_status(predicate, raw_status)
    combined = f"{_predicate(predicate)} {_predicate(raw_status)}"
    exception = (
        "CUTI_MELAHIRKAN" if status == "EXEMPT_MATERNITY"
        else (_predicate(predicate) or _predicate(raw_status)) if status == "EXEMPT_OTHER"
        else None
    )
    return status, exception


def _payment_status(value: Any) -> str:
    raw = _text(value).upper()
    if not raw:
        return "READY_TO_PAY"
    if "TIDAK" in raw or "UNPAID" in raw:
        return "UNPAID"
    if "RETUR" in raw or "RETURN" in raw:
        return "RETURNED"
    if "HOLD" in raw or "TUNDA" in raw:
        return "HOLD"
    if "BAYAR" in raw or "PAID" in raw:
        return "PAID"
    return raw.replace(" ", "_")


def _month_label(period: str) -> str:
    year, month = map(int, period.split("-"))
    return f"{MONTH_NAMES[month]} {year}"


class TukinAnnualService:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def connect(self):
        conn = duckdb.connect(self.db_path)
        # The dashboard loads period and annual endpoints concurrently. DuckDB
        # serializes catalog writes, so schema creation must be guarded.
        with _SCHEMA_LOCK:
            if self.db_path not in _SCHEMA_READY:
                ensure_tukin_annual_schema(conn)
                ensure_deceased_schema(conn)
                ensure_condition_schema(conn)
                ensure_third_missing_unpaid(conn)
                ensure_operational_schema(conn)
                _SCHEMA_READY.add(self.db_path)
        return conn

    def read_cursor(self):
        """Reuse the catalog-loaded connection for dashboard list requests."""
        path = str(Path(self.db_path).resolve())
        with _ANNUAL_READ_LOCK:
            base = _ANNUAL_READ_BASE.get(path)
            if base is None:
                base = self.connect()
                _ANNUAL_READ_BASE[path] = base
            return base.cursor()

    def _master_snapshots(self, assessment_period: str) -> dict[str, dict[str, Any]]:
        """Resolve the effective Master SDM values that must be frozen in a final period."""
        try:
            module = importlib.import_module("backend.shared_master")
            return module.SharedMasterService(self.db_path).snapshot_map(assessment_period)
        except Exception:
            # Historical imports remain usable even before Master SDM is populated.
            return {}

    def ensure_period(self, assessment_period: str, payment_period: str, source_mode: str = "HISTORICAL") -> None:
        assessment_period = _period(assessment_period)
        payment_period = _period(payment_period)
        if payment_period != self._next_month(assessment_period):
            raise ValueError("Periode pembayaran harus satu bulan setelah periode penilaian")
        year, month = map(int, assessment_period.split("-"))
        semester = 1 if month <= 6 else 2
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT assessment_period FROM tukin_period WHERE assessment_period = ?", [assessment_period]
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE tukin_period SET payment_period=?, year=?, semester=?, source_mode=?, updated_at=now()
                       WHERE assessment_period=?""",
                    [payment_period, year, semester, source_mode, assessment_period],
                )
            else:
                conn.execute(
                    """INSERT INTO tukin_period
                       (assessment_period,payment_period,year,semester,status,source_mode)
                       VALUES (?,?,?,?,?,?)""",
                    [assessment_period, payment_period, year, semester,
                     "REVIEW" if source_mode == "HISTORICAL" else "DRAFT", source_mode],
                )

    def import_rows(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source_type = _text(payload.get("source_type")).upper()
        if source_type not in {"FINAL", "SKP", "ATTENDANCE_DAILY"}:
            raise ValueError("source_type harus FINAL, SKP, atau ATTENDANCE_DAILY")
        assessment = _period(_text(payload.get("assessment_period")))
        payment = _period(_text(payload.get("payment_period") or self._next_month(assessment)))
        rows = payload.get("rows") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("File tidak berisi baris data")
        if not isinstance(rows[0], Mapping):
            raise ValueError("Format baris impor tidak valid")
        filename = Path(_text(payload.get("source_file")) or "import.xlsx").name
        batch_id = str(uuid.uuid4())
        checksum = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT status FROM tukin_period WHERE assessment_period=?", [assessment]
            ).fetchone()
            if existing and existing[0] in {"LOCKED", "PAID", "HISTORICAL_LOCKED"}:
                raise ValueError("Periode terkunci atau sudah dibayar; impor ulang tidak diperbolehkan")
            duplicate = conn.execute(
                """SELECT count(*) FROM tukin_import_batch
                   WHERE source_type=? AND assessment_period=? AND checksum=? AND status='COMPLETED'""",
                [source_type, assessment, checksum],
            ).fetchone()[0]
            if duplicate:
                raise ValueError("File yang sama sudah pernah diimpor untuk periode dan jenis data ini")
        self.ensure_period(assessment, payment, "HISTORICAL")
        if source_type == "FINAL":
            result = self._import_final(rows, assessment, payment, filename, batch_id)
        elif source_type == "SKP":
            result = self._import_skp(rows, assessment, filename, batch_id)
        else:
            result = self._import_daily(rows, assessment, filename, batch_id)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tukin_import_batch
                   (id,source_type,assessment_period,payment_period,source_file,row_count,
                    imported_count,rejected_count,checksum,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [batch_id, source_type, assessment, payment, filename, len(rows),
                 result["imported"], result["rejected"], checksum, "COMPLETED"],
            )
        return {"status": "ok", "batch_id": batch_id, "source_type": source_type,
                "assessment_period": assessment, "payment_period": payment, **result}

    @staticmethod
    def _next_month(period: str) -> str:
        year, month = map(int, period.split("-"))
        return f"{year + (1 if month == 12 else 0):04d}-{1 if month == 12 else month + 1:02d}"

    def _import_final(self, rows, assessment, payment, filename, batch_id):
        mapping = _mapping(rows[0].keys())
        if "nip" not in mapping:
            raise ValueError("Kolom NIP tidak ditemukan")
        prepared = []
        rejected = []
        year, month_number = map(int, assessment.split("-"))
        master_by_nip = self._master_snapshots(assessment)
        snapshot_at = datetime.now()
        for source_row, row in enumerate(rows, 2):
            nip = _text(row.get(mapping["nip"]))
            if not nip:
                rejected.append({"row": source_row, "reason": "NIP kosong"})
                continue
            get = lambda field, default="": row.get(mapping[field], default) if field in mapping else default
            master = master_by_nip.get(nip, {})
            predicate = _predicate(get("predicate_skp"))
            report_status, exception = _report_status(predicate, get("report_status"))
            gross = _number(get("tukin_gross")) if _text(get("tukin_gross")) else _number(master.get("tukin_gross"))
            skp_rate = _rate(get("skp_reduction_rate"), PREDICATE_RATES.get(predicate, 0.0))
            attendance_rate = _rate(get("attendance_reduction_rate"))
            skp_deduction = _number(get("skp_deduction"), round(gross * .70 * skp_rate / 100))
            attendance_deduction = _number(get("attendance_deduction"), round(gross * .30 * attendance_rate / 100))
            total_deduction = _number(get("total_deduction"), skp_deduction + attendance_deduction)
            entitlement = max(0.0, gross - total_deduction)
            paid = _number(get("paid_amount"), entitlement)
            pay_status = _payment_status(get("payment_status"))
            if master and master.get("eligible") is False:
                paid = 0
                pay_status = "UNPAID"
            conditions = []
            source_3t = _text(get("wilayah_3t")).upper() == "3T"
            if source_3t or master.get("wilayah_3t") is True: conditions.append("3T")
            if exception: conditions.append(exception)
            prepared.append({
                "assessment_period": assessment, "payment_period": payment, "nip": nip,
                "sdm_id": master.get("sdm_id"),
                "nama": _text(get("nama")) or _text(master.get("nama")),
                "penilai": _text(get("penilai")) or _text(master.get("penilai")),
                "kd_anak_satker": _text(get("kd_anak_satker")) or _text(master.get("kd_anak_satker")),
                "jabatan": _text(get("jabatan")) or _text(master.get("jabatan")),
                "unit": _text(get("unit")) or _text(master.get("unit")),
                "kelas_jabatan": int(_number(get("kelas_jabatan"))) if _text(get("kelas_jabatan")) else master.get("kelas_jabatan"),
                "employment_status": _text(master.get("employment_status")),
                "tahap": _text(get("tahap")), "wilayah_3t": "3T" if (source_3t or master.get("wilayah_3t") is True) else _text(get("wilayah_3t")),
                "status_sdm": _text(get("status_sdm")) or _text(master.get("status_tendik")),
                "jabatan_sdm": _text(get("jabatan_sdm")) or _text(master.get("jabatan_osdm")),
                "temporary_assignment_type": _text(master.get("temporary_assignment_type")),
                "temporary_assignment_detail": _text(master.get("temporary_assignment_detail")),
                "temporary_assignment_effective_month": _text(master.get("temporary_assignment_effective_month")),
                "predicate_skp": predicate.title() if predicate else "", "report_status": report_status,
                "consecutive_missing": 0, "tahun": year, "bulan": MONTH_NAMES[month_number],
                "tukin_gross": gross, "skp_reduction_rate": skp_rate,
                "attendance_reduction_rate": attendance_rate, "skp_deduction": skp_deduction,
                "attendance_deduction": attendance_deduction, "manual_adjustment": 0,
                "total_deduction": total_deduction, "calculated_entitlement": entitlement,
                "paid_amount": paid, "payment_status": pay_status,
                "payment_reason": _text(get("payment_reason")) or (_text(master.get("eligibility_reason")) if pay_status == "UNPAID" else ""),
                "province": _text(get("province")) or _text(master.get("provinsi")),
                "regency": _text(get("regency")) or _text(master.get("kabupaten_kota")),
                "account_number": _text(get("account_number")) or _text(master.get("account_number")),
                "bank_name": _text(get("bank_name")) or _text(master.get("bank_name")),
                "account_holder": _text(get("account_holder")) or _text(master.get("account_holder")),
                "special_conditions": ",".join(conditions),
                "evidence_status": "MIGRATED", "calculation_version": "HISTORICAL-SNAPSHOT",
                "master_version": master.get("master_version"), "snapshot_at": snapshot_at,
                "source_mode": "HISTORICAL", "source_file": filename, "source_row": source_row,
                "import_batch_id": batch_id,
            })
        self._replace_period("tukin_final_monthly", assessment, prepared)
        # Populate SKP history from final files as a useful baseline without overwriting a dedicated SKP import.
        skp_rows = [{
            "assessment_period": assessment, "nip": item["nip"], "nama": item["nama"],
            "penilai": item["penilai"], "jabatan": item["jabatan"], "tahap": item["tahap"],
            "predicate_raw": item["predicate_skp"], "predicate_normalized": item["predicate_skp"],
            # A historical FINAL file is an immutable payment snapshot. Missing
            # SKP detail in that file must not retroactively hold a past payment;
            # a later dedicated SKP assessment import will replace this baseline.
            "report_status": "MIGRATED_SNAPSHOT" if item["report_status"] in {"DATA_MISSING", "INVALID_PREDICATE"} else item["report_status"], "consecutive_missing": 0,
            "skp_reduction_rate": item["skp_reduction_rate"], "payment_eligible": True,
            "exception_code": "CUTI_MELAHIRKAN" if "CUTI_MELAHIRKAN" in item["special_conditions"] else None,
            "source_file": filename, "source_row": item["source_row"], "import_batch_id": batch_id,
        } for item in prepared]
        self._upsert_skp(assessment, skp_rows, replace_missing_only=True)
        self.recompute_skp_streaks(year)
        self.sync_skp_condition_decisions(assessment)
        self.apply_death_controls(assessment)
        return {"imported": len(prepared), "rejected": len(rejected), "rejections": rejected[:100]}

    def _import_skp(self, rows, assessment, filename, batch_id):
        mapping = _mapping(rows[0].keys())
        if "nip" not in mapping:
            raise ValueError("Kolom NIP tidak ditemukan")
        prepared, rejected = [], []
        for source_row, row in enumerate(rows, 2):
            nip = _text(row.get(mapping["nip"]))
            if not nip:
                rejected.append({"row": source_row, "reason": "NIP kosong"})
                continue
            get = lambda field, default="": row.get(mapping[field], default) if field in mapping else default
            raw_predicate = _text(get("predicate_skp"))
            predicate = _predicate(raw_predicate)
            status, exception = _report_status(predicate, get("report_status"))
            rate = PREDICATE_RATES.get(predicate, 0.0)
            prepared.append({
                "assessment_period": assessment, "nip": nip, "nama": _text(get("nama")),
                "penilai": _text(get("penilai")), "jabatan": _text(get("jabatan")),
                "tahap": _text(get("tahap")), "predicate_raw": raw_predicate,
                "predicate_normalized": predicate.title() if predicate else "",
                "report_status": status, "consecutive_missing": 0,
                "skp_reduction_rate": rate, "payment_eligible": True,
                "exception_code": exception, "source_file": filename, "source_row": source_row,
                "import_batch_id": batch_id,
            })
        self._upsert_skp(assessment, prepared, replace_missing_only=False)
        self.recompute_skp_streaks(int(assessment[:4]))
        self._apply_skp_to_unlocked_final(assessment)
        self.sync_skp_condition_decisions(assessment)
        self.apply_death_controls(assessment)
        return {"imported": len(prepared), "rejected": len(rejected), "rejections": rejected[:100]}

    def _import_daily(self, rows, assessment, filename, batch_id):
        mapping = _mapping(rows[0].keys())
        if "nip" not in mapping or "work_date" not in mapping:
            raise ValueError("Kolom NIP dan Tanggal wajib tersedia")
        prepared, rejected, seen = [], [], set()
        for source_row, row in enumerate(rows, 2):
            nip = _text(row.get(mapping["nip"]))
            raw_date = row.get(mapping["work_date"])
            raw_date_text = _text(raw_date)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", raw_date_text):
                parsed = pd.to_datetime(raw_date, errors="coerce", yearfirst=True)
            else:
                parsed = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
            if not nip or pd.isna(parsed):
                rejected.append({"row": source_row, "reason": "NIP/tanggal tidak valid"})
                continue
            daily_key = (nip, parsed.date())
            if daily_key in seen:
                rejected.append({"row": source_row, "reason": "Duplikat NIP dan tanggal"})
                continue
            seen.add(daily_key)
            get = lambda field, default="": row.get(mapping[field], default) if field in mapping else default
            prepared.append({
                "assessment_period": assessment, "nip": nip, "work_date": parsed.date(),
                "nama": _text(get("nama")), "day_name": _text(get("day_name")),
                "check_in": _text(get("check_in")), "check_out": _text(get("check_out")),
                "attendance_type": _text(get("attendance_type")),
                "attendance_status": _text(get("attendance_status")),
                "reduction_rate": _rate(get("daily_reduction_rate")), "deduction_amount": 0,
                "reason": _text(get("reason")), "evidence_ref": _text(get("evidence_ref")),
                "source_file": filename, "source_row": source_row, "import_batch_id": batch_id,
            })
        self._replace_period("tukin_attendance_daily", assessment, prepared)
        return {"imported": len(prepared), "rejected": len(rejected), "rejections": rejected[:100]}

    def _replace_period(self, table: str, assessment: str, rows: list[dict[str, Any]], merge: bool = False) -> None:
        if merge and not rows:
            return
        with self.connect() as conn:
            conn.execute('BEGIN TRANSACTION')
            if merge:
                conn.execute(f"DELETE FROM {table} WHERE assessment_period = ? AND nip IN (SELECT * FROM UNNEST(?))", [assessment, [row['nip'] for row in rows]])
            else:
                conn.execute(f"DELETE FROM {table} WHERE assessment_period = ?", [assessment])
            if not rows:
                conn.commit()
                return
            frame = pd.DataFrame(rows)
            conn.register("tmp_tukin_rows", frame)
            columns = list(frame.columns)
            names = ",".join(f'"{column}"' for column in columns)
            conn.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM tmp_tukin_rows")
            conn.unregister("tmp_tukin_rows")
            conn.commit()

    def _upsert_skp(self, assessment: str, rows: list[dict[str, Any]], replace_missing_only: bool) -> None:
        if not rows:
            return
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT nip, source_file FROM tukin_skp_monthly WHERE assessment_period=?", [assessment]
            ).fetchdf()
            if replace_missing_only and not existing.empty:
                existing_nips = set(existing["nip"].astype(str))
                rows = [row for row in rows if row["nip"] not in existing_nips]
            if not rows:
                return
            nips = [row["nip"] for row in rows]
            conn.execute(
                "DELETE FROM tukin_skp_monthly WHERE assessment_period=? AND nip IN (SELECT * FROM UNNEST(?))",
                [assessment, nips],
            )
            frame = pd.DataFrame(rows)
            conn.register("tmp_skp_rows", frame)
            columns = list(frame.columns)
            names = ",".join(f'"{column}"' for column in columns)
            conn.execute(f"INSERT INTO tukin_skp_monthly ({names}) SELECT {names} FROM tmp_skp_rows")
            conn.unregister("tmp_skp_rows")

    def recompute_skp_streaks(self, year: int) -> None:
        # Include prior December so a streak can cross the year boundary.
        with self.connect() as conn:
            try:
                configured_rules = conn.execute(
                    """SELECT streak_from,streak_to,action,reduction_rate,effective_from,effective_to
                       FROM skp_non_submission_rule WHERE active=true
                       ORDER BY streak_from DESC"""
                ).fetchall()
            except duckdb.CatalogException:
                configured_rules = []

            def missing_rule(period, streak):
                for start, end, action, rate, effective_from, effective_to in configured_rules:
                    if int(start) <= streak and (end is None or streak <= int(end)) \
                            and str(effective_from) <= period \
                            and (not effective_to or period <= str(effective_to)):
                        return float(rate), str(action) != 'UNPAID'
                return (35.0, True) if streak == 1 else (70.0, True) if streak == 2 else (100.0, False)
            df = conn.execute(
                """SELECT assessment_period,nip,report_status,predicate_normalized
                   FROM tukin_skp_monthly
                   WHERE assessment_period >= ? AND assessment_period <= ?
                   ORDER BY nip,assessment_period""",
                [f"{year-1}-10", f"{year}-12"],
            ).fetchdf()
            if df.empty:
                return
            updates = []
            for nip, group in df.groupby("nip", sort=False):
                streak = 0
                previous_period = None
                for row in group.itertuples(index=False):
                    current = str(row.assessment_period)
                    consecutive = previous_period is None or self._next_month(previous_period) == current
                    if row.report_status == "NOT_SUBMITTED_VERIFIED":
                        streak = streak + 1 if consecutive else 1
                        rate, eligible = missing_rule(current, streak)
                    elif row.report_status in {"DATA_MISSING", "INVALID_PREDICATE", "REVIEW_REQUIRED"}:
                        streak = 0
                        rate = 0.0
                        eligible = False
                    else:
                        streak = 0
                        pred = _predicate(row.predicate_normalized)
                        rate = PREDICATE_RATES.get(pred, 0.0)
                        eligible = True
                    updates.append((streak, rate, eligible, current, str(nip)))
                    previous_period = current
            conn.executemany(
                """UPDATE tukin_skp_monthly SET consecutive_missing=?,skp_reduction_rate=?,
                   payment_eligible=?,updated_at=now() WHERE assessment_period=? AND nip=?""",
                updates,
            )
            conn.execute(
                """UPDATE tukin_final_monthly AS f SET
                   predicate_skp=s.predicate_normalized, report_status=s.report_status,
                   consecutive_missing=s.consecutive_missing, skp_reduction_rate=s.skp_reduction_rate,
                   updated_at=now()
                   FROM tukin_skp_monthly AS s
                   WHERE f.assessment_period=s.assessment_period AND f.nip=s.nip"""
            )
            # Recalculate every open period affected by a changed streak. Pending
            # or invalid data remains HOLD; the third consecutive verified month
            # without SKP and every consecutive month after it is UNPAID.
            conn.execute(
                """UPDATE tukin_final_monthly AS f SET
                   skp_deduction=round(f.tukin_gross*.70*s.skp_reduction_rate/100),
                   total_deduction=f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment,
                   calculated_entitlement=greatest(0,f.tukin_gross-(f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment)),
                   paid_amount=CASE WHEN s.payment_eligible THEN greatest(0,f.tukin_gross-(f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment)) ELSE 0 END,
                    payment_status=CASE WHEN s.payment_eligible THEN CASE WHEN f.payment_status IN ('HOLD','UNPAID') AND f.payment_reason LIKE '%SKP%' THEN 'READY_TO_PAY' ELSE f.payment_status END ELSE CASE WHEN s.report_status='NOT_SUBMITTED_VERIFIED' THEN 'UNPAID' ELSE 'HOLD' END END,
                    payment_reason=CASE WHEN s.payment_eligible THEN CASE WHEN f.payment_reason LIKE '%SKP%' THEN '' ELSE f.payment_reason END ELSE CASE WHEN s.report_status='NOT_SUBMITTED_VERIFIED' THEN 'Tidak mengerjakan SKP 3 bulan berturut-turut atau lebih (default); keputusan mengikuti konfigurasi periode' WHEN s.report_status IN ('DATA_MISSING','INVALID_PREDICATE','REVIEW_REQUIRED') THEN 'Penilaian SKP belum valid/disetujui' ELSE 'Penilaian SKP tidak memenuhi syarat pembayaran' END END,
                   updated_at=now()
                   FROM tukin_skp_monthly AS s JOIN tukin_period AS p
                     ON p.assessment_period=s.assessment_period
                   WHERE f.assessment_period=s.assessment_period AND f.nip=s.nip
                     AND p.status NOT IN ('HISTORICAL_LOCKED','LOCKED','PAID')"""
            )

    def _apply_skp_to_unlocked_final(self, assessment: str) -> None:
        with self.connect() as conn:
            period = conn.execute("SELECT status FROM tukin_period WHERE assessment_period=?", [assessment]).fetchone()
            if period and period[0] in {"HISTORICAL_LOCKED", "LOCKED", "PAID"}:
                return
            conn.execute(
                """UPDATE tukin_final_monthly AS f SET
                   skp_deduction=round(f.tukin_gross*.70*s.skp_reduction_rate/100),
                   total_deduction=f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment,
                   calculated_entitlement=greatest(0,f.tukin_gross-(f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment)),
                   paid_amount=CASE WHEN s.payment_eligible THEN greatest(0,f.tukin_gross-(f.attendance_deduction+round(f.tukin_gross*.70*s.skp_reduction_rate/100)-f.manual_adjustment)) ELSE 0 END,
                   payment_status=CASE WHEN s.payment_eligible THEN CASE WHEN f.payment_status IN ('HOLD','UNPAID') AND f.payment_reason LIKE '%SKP%' THEN 'READY_TO_PAY' ELSE f.payment_status END ELSE CASE WHEN s.report_status='NOT_SUBMITTED_VERIFIED' THEN 'UNPAID' ELSE 'HOLD' END END,
                   payment_reason=CASE WHEN s.payment_eligible THEN CASE WHEN f.payment_reason LIKE '%SKP%' THEN '' ELSE f.payment_reason END ELSE CASE WHEN s.report_status='NOT_SUBMITTED_VERIFIED' THEN 'Tidak mengerjakan SKP 3 bulan berturut-turut atau lebih (default); keputusan mengikuti konfigurasi periode' WHEN s.report_status IN ('DATA_MISSING','INVALID_PREDICATE','REVIEW_REQUIRED') THEN 'Penilaian SKP belum valid/disetujui' ELSE 'Penilaian SKP tidak memenuhi syarat pembayaran' END END,
                   updated_at=now()
                   FROM tukin_skp_monthly AS s
                   WHERE f.assessment_period=s.assessment_period AND f.nip=s.nip AND f.assessment_period=?""",
                [assessment],
            )

    def apply_death_controls(self, assessment: str, nip: str = "") -> dict[str, Any]:
        """Apply verified death-month payment controls without inventing entitlement policy."""
        assessment = _period(assessment)
        clauses, params = ["assessment_period=?", "status='VERIFIED'"], [assessment]
        if nip:
            clauses.append("nip=?"); params.append(_text(nip))
        with self.connect() as conn:
            deaths = conn.execute(
                f"SELECT * FROM sdm_death_record WHERE {' AND '.join(clauses)}", params
            ).fetchdf()
            applied = 0
            for death in deaths.to_dict("records"):
                death_nip = _text(death.get("nip"))
                final = conn.execute(
                    """SELECT tukin_gross,attendance_reduction_rate,attendance_deduction,
                              manual_adjustment,bank_name,account_number,account_holder,
                              special_conditions,payment_status
                       FROM tukin_final_monthly WHERE assessment_period=? AND nip=?""",
                    [assessment, death_nip],
                ).fetchone()
                if not final:
                    continue
                gross = float(final[0] or 0); attendance_rate = float(final[1] or 0)
                attendance_deduction = float(final[2] or 0); adjustment = float(final[3] or 0)
                daily = conn.execute(
                    """SELECT count(*),coalesce(sum(reduction_rate),0)
                       FROM tukin_attendance_daily
                       WHERE assessment_period=? AND nip=? AND work_date<=?""",
                    [assessment, death_nip, death.get("death_date")],
                ).fetchone()
                if daily and int(daily[0] or 0) > 0:
                    attendance_rate = min(100.0, float(daily[1] or 0))
                    attendance_deduction = round(gross * .30 * attendance_rate / 100)
                total = max(0.0, attendance_deduction - adjustment)
                entitlement = max(0.0, gross - total)
                decision = _text(death.get("payment_decision")) or "HOLD_REVIEW"
                recipient_type = "EMPLOYEE_ACCOUNT"
                recipient_name = _text(final[6])
                recipient_relationship = ""
                recipient_bank = _text(final[4]); recipient_account = _text(final[5])
                recipient_holder = _text(final[6]); beneficiary_document = ""
                if decision == "HEIR":
                    recipient_type = "HEIR"
                    recipient_name = _text(death.get("heir_name"))
                    recipient_relationship = _text(death.get("heir_relationship"))
                    recipient_bank = _text(death.get("heir_bank_name"))
                    recipient_account = _text(death.get("heir_account_number"))
                    recipient_holder = _text(death.get("heir_account_holder")) or recipient_name
                    beneficiary_document = _text(death.get("heir_document_ref"))
                if decision == "HOLD_REVIEW":
                    payment_status, paid, payment_reason = "HOLD", 0.0, "Kondisi meninggal dunia - keputusan pembayaran belum ditetapkan"
                elif decision == "UNPAID":
                    payment_status, paid, payment_reason = "UNPAID", 0.0, _text(death.get("payment_notes")) or "Tidak dibayarkan berdasarkan keputusan resmi"
                elif decision in {"NORMAL_ACCOUNT", "HEIR"}:
                    payment_status, paid, payment_reason = "READY_TO_PAY", entitlement, _text(death.get("payment_notes"))
                else:
                    payment_status, paid, payment_reason = "HOLD", 0.0, "Keputusan pembayaran kondisi meninggal tidak valid"
                conditions = [item for item in _text(final[7]).split(",") if item]
                if "MENINGGAL" not in conditions:
                    conditions.append("MENINGGAL")
                conn.execute(
                    """UPDATE tukin_final_monthly SET employment_status='MENINGGAL',report_status='EXEMPT_DECEASED',
                       predicate_skp='',skp_reduction_rate=0,skp_deduction=0,
                       attendance_reduction_rate=?,attendance_deduction=?,total_deduction=?,
                       calculated_entitlement=?,paid_amount=?,payment_status=?,payment_reason=?,
                       special_conditions=?,death_record_id=?,recipient_type=?,recipient_name=?,
                       recipient_relationship=?,recipient_bank_name=?,recipient_account_number=?,
                       recipient_account_holder=?,beneficiary_document_ref=?,updated_at=now()
                       WHERE assessment_period=? AND nip=?""",
                    [attendance_rate, attendance_deduction, total, entitlement, paid, payment_status,
                     payment_reason, ",".join(conditions), death.get("id"), recipient_type,
                     recipient_name, recipient_relationship, recipient_bank, recipient_account,
                     recipient_holder, beneficiary_document, assessment, death_nip],
                )
                conn.execute(
                    """UPDATE tukin_skp_monthly SET predicate_normalized='',report_status='EXEMPT_DECEASED',
                       skp_reduction_rate=0,payment_eligible=true,exception_code='MENINGGAL',updated_at=now()
                       WHERE assessment_period=? AND nip=?""", [assessment, death_nip]
                )
                applied += 1
        return {"status": "ok", "assessment_period": assessment, "applied": applied}

    def capture_live(self, assessment: str, payment: str, enforce_workflow: bool = False) -> dict[str, Any]:
        assessment, payment = _period(assessment), _period(payment)
        if payment != self._next_month(assessment):
            raise ValueError('Periode pembayaran harus satu bulan setelah periode penilaian')
        with self.connect() as conn:
            current = conn.execute('SELECT status FROM tukin_period WHERE assessment_period=?', [assessment]).fetchone()
            if current and current[0] in {'LOCKED','HISTORICAL_LOCKED','PAID'}:
                raise ValueError('Periode terkunci; hasil gabungan tidak boleh menimpa arsip pembayaran')
        if enforce_workflow:
            readiness = validate_final_readiness(self.db_path, assessment)
            if not readiness["ready"]:
                raise WorkflowNotReadyError(readiness)
        self.ensure_period(assessment, payment, "LIVE")
        year, month_number = map(int, assessment.split("-"))
        with self.connect() as conn:
            from backend.absence_cutover import assert_canonical_absence_ready
            assert_canonical_absence_ready(conn, assessment)

            def table_df(name: str) -> pd.DataFrame:
                try:
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{name}')").fetchall()}
                    if not cols or cols == {"_empty"}:
                        return pd.DataFrame()
                    return conn.execute(f'SELECT * FROM "{name}"').fetchdf()
                except Exception:
                    return pd.DataFrame()
            final = table_df("final_absensi")
            skp = table_df("final_skp")
            daily = table_df("hitung_harian")
            assessments = table_df("skp_assessment_monthly")
            death_records = table_df("sdm_death_record")
        if final.empty:
            raise ValueError("Final Absensi workflow aktif masih kosong")
        if 'bulan' in final.columns:
            final = final[final['bulan'].astype(str) == assessment]
            if final.empty:
                raise ValueError('Periode Final Absensi berbeda dari periode penilaian yang dipilih')
        if final['nip'].astype(str).str.strip().duplicated().any():
            raise ValueError('NIP Final Absensi duplikat; periksa sebelum sinkronisasi')
        skp_by_nip = {}
        if not skp.empty and "nip" in skp.columns:
            skp_by_nip = {str(row.get("nip", "")).strip(): row for row in skp.to_dict("records")}
        approved_by_nip = {}
        if not assessments.empty and {"nip", "assessment_period", "review_status"}.issubset(assessments.columns):
            approved = assessments[
                (assessments["assessment_period"].astype(str) == assessment)
                & (assessments["review_status"].astype(str) == "APPROVED")
            ]
            approved_by_nip = {str(row.get("nip", "")).strip(): row for row in approved.to_dict("records")}
        death_by_nip = {}
        if not death_records.empty and {"nip", "stop_effective_period"}.issubset(death_records.columns):
            death_by_nip = {str(row.get("nip", "")).strip(): row for row in death_records.to_dict("records")}
        master_by_nip = self._master_snapshots(assessment)
        snapshot_at = datetime.now()
        rows = []
        skp_rows = []
        for source_row, item in enumerate(final.to_dict("records"), 2):
            nip = _text(item.get("nip"))
            if not nip:
                continue
            death = death_by_nip.get(nip)
            if death is not None and assessment >= _text(death.get("stop_effective_period")):
                continue
            sr = skp_by_nip.get(nip, {})
            approved_skp = approved_by_nip.get(nip)
            master = master_by_nip.get(nip, {})
            if master.get('tukin_gross') is None:
                raise ValueError('Tukin bruto kanonis belum tersedia untuk salah satu NIP. Periksa tabel Gabungan sebelum menyimpan Final.')
            if approved_skp is not None:
                predicate = _predicate(
                    approved_skp.get("final_predicate") or approved_skp.get("recommended_predicate")
                    or approved_skp.get("source_predicate") or ""
                )
                status = _text(approved_skp.get("report_status")) or "REVIEW_REQUIRED"
                exception = "CUTI_MELAHIRKAN" if status == "EXEMPT_MATERNITY" else (
                    "SKP_EXCEPTION" if status == "EXEMPT_OTHER" else None
                )
            else:
                predicate = _predicate(sr.get("predicate_skp") or sr.get("predikat") or "")
                status, exception = _report_status(predicate, sr.get("report_status") or sr.get("tidak_buat_skp"))
                status = 'REVIEW_REQUIRED'
            if status in {"DATA_MISSING", "INVALID_PREDICATE"}:
                status = "REVIEW_REQUIRED"
            master_gross = master.get("tukin_gross")
            gross = _number(master_gross) if _text(master_gross) else _number(item.get("tukin"))
            master_status = "MATCHED" if _text(master_gross) else "NOT_FOUND"
            attendance_rate = _rate(item.get("potPct"))
            attendance_deduction = round(gross * .30 * attendance_rate / 100)
            skp_rate = PREDICATE_RATES.get(predicate, 0.0) if approved_skp is not None else _rate(
                sr.get("skpPct"), PREDICATE_RATES.get(predicate, 0.0)
            )
            skp_deduction = round(gross * .70 * skp_rate / 100)
            total = attendance_deduction + skp_deduction
            entitlement = max(0, gross-total)
            eligible = master.get("eligible") is not False and status != "REVIEW_REQUIRED"
            rows.append({
                "assessment_period": assessment, "payment_period": payment, "nip": nip,
                "sdm_id": master.get("sdm_id"),
                "nama": _text(item.get("nama")) or _text(master.get("nama")),
                "penilai": _text(master.get("penilai")), "kd_anak_satker": _text(master.get("kd_anak_satker")),
                "jabatan": _text(item.get("jabatan")) or _text(master.get("jabatan")),
                "unit": _text(master.get("unit")), "kelas_jabatan": master.get("kelas_jabatan"),
                "employment_status": _text(master.get("employment_status")),
                "tahap": "", "wilayah_3t": "3T" if master.get("wilayah_3t") is True else "",
                "status_sdm": _text(master.get("status_tendik")), "jabatan_sdm": _text(master.get("jabatan_osdm")), "predicate_skp": predicate.title(),
                "temporary_assignment_type": _text(master.get("temporary_assignment_type")),
                "temporary_assignment_detail": _text(master.get("temporary_assignment_detail")),
                "temporary_assignment_effective_month": _text(master.get("temporary_assignment_effective_month")),
                "report_status": status, "consecutive_missing": 0, "tahun": year,
                "bulan": MONTH_NAMES[month_number], "tukin_gross": gross,
                "skp_reduction_rate": skp_rate, "attendance_reduction_rate": attendance_rate,
                "skp_deduction": skp_deduction, "attendance_deduction": attendance_deduction,
                "manual_adjustment": 0, "total_deduction": total,
                "calculated_entitlement": entitlement, "paid_amount": entitlement if eligible else 0,
                "payment_status": "READY_TO_PAY" if eligible else "HOLD" if status == "REVIEW_REQUIRED" else "UNPAID",
                "payment_reason": "" if eligible else "Penilaian SKP belum valid/disetujui" if status == "REVIEW_REQUIRED" else (_text(master.get("eligibility_reason")) or "Tidak berhak Tukin menurut Master SDM"),
                "province": _text(master.get("provinsi")), "regency": _text(master.get("kabupaten_kota")),
                "account_number": _text(master.get("account_number")), "bank_name": _text(master.get("bank_name")),
                "account_holder": _text(master.get("account_holder")),
                "special_conditions": ",".join(filter(None, ["3T" if master.get("wilayah_3t") is True else "", exception or ""])), "evidence_status": "WORKFLOW",
                "calculation_version": "PERMENSOS-6-2023-V1", "source_mode": "LIVE",
                "master_version": master.get("master_version"), "snapshot_at": snapshot_at,
                "source_file": "WORKFLOW_INTERNAL", "source_row": source_row,
                "import_batch_id": "LIVE-" + assessment,
                "special_conditions": ",".join(filter(None, [
                    "MASTER_SDM_NOT_FOUND" if master_status == "NOT_FOUND" else "",
                    "3T" if master.get("wilayah_3t") is True else "",
                    exception or "",
                ])),
            })
            skp_rows.append({
                "assessment_period": assessment, "nip": nip, "nama": _text(item.get("nama")),
                "penilai": "", "jabatan": _text(item.get("jabatan")), "tahap": "",
                "predicate_raw": predicate.title(), "predicate_normalized": predicate.title(),
                "report_status": status, "consecutive_missing": 0,
                "skp_reduction_rate": skp_rate, "payment_eligible": eligible,
                "exception_code": exception, "source_file": "WORKFLOW_INTERNAL",
                "source_row": source_row, "import_batch_id": "LIVE-" + assessment,
            })
        self._replace_period("tukin_final_monthly", assessment, rows, merge=True)
        self._upsert_skp(assessment, skp_rows, replace_missing_only=False)
        self.recompute_skp_streaks(year)
        self._apply_skp_to_unlocked_final(assessment)
        if not daily.empty:
            daily_rows = daily.to_dict("records")
            # Avoid recursive period/final changes; directly normalize daily rows.
            try:
                self._import_daily(daily_rows, assessment, "WORKFLOW_INTERNAL", "LIVE-" + assessment)
            except ValueError:
                pass
        self.sync_skp_condition_decisions(assessment)
        self.apply_death_controls(assessment)
        with self.connect() as conn:
            conn.execute("UPDATE tukin_period SET status='REVIEW',updated_at=now() WHERE assessment_period=?", [assessment])
        return {"status": "ok", "captured": len(rows), "assessment_period": assessment}

    def sync_skp_condition_decisions(self, assessment: str, nip: str = "") -> dict[str, Any]:
        """Materialize approved SKP-governance decisions into Tukin when final data exists."""
        assessment = _period(assessment)
        clauses, params = ["assessment_period=?", "status='APPROVED'"], [assessment]
        if nip:
            clauses.append("nip=?"); params.append(_text(nip))
        with self.connect() as conn:
            decisions = conn.execute(
                f"SELECT * FROM skp_condition_decision WHERE {' AND '.join(clauses)} ORDER BY updated_at",
                params,
            ).fetchdf()
        synced = 0
        for decision in self._records(decisions):
            decision_nip = _text(decision.get("nip"))
            with self.connect() as conn:
                final_exists = conn.execute(
                    "SELECT 1 FROM tukin_final_monthly WHERE assessment_period=? AND nip=?",
                    [assessment, decision_nip],
                ).fetchone()
                if not final_exists:
                    continue
                effect = {"UNPAID": "UNPAID", "HOLD": "HOLD"}.get(
                    _text(decision.get("payment_effect")).upper(), "REVIEW"
                )
                condition_id = "SKPDEC-" + _text(decision.get("id"))
                conn.execute("DELETE FROM tukin_condition WHERE id=?", [condition_id])
                conn.execute(
                    """INSERT INTO tukin_condition
                       (id,nip,assessment_period,condition_code,start_date,end_date,effect_type,
                        effect_value,reason,evidence_ref,approved_by,approved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,now())""",
                    [condition_id, decision_nip, assessment, _text(decision.get("condition_code")),
                     decision.get("start_date"), decision.get("end_date"), effect, 0,
                     _text(decision.get("reason")),
                     " | ".join(filter(None, [_text(decision.get("document_number")), _text(decision.get("evidence_ref"))])),
                     _text(decision.get("approved_by"))],
                )
            self._apply_condition(
                assessment, decision_nip, _text(decision.get("condition_code")), effect,
                pd.to_datetime(decision.get("start_date"), errors="coerce").date()
                if decision.get("start_date") else None,
                pd.to_datetime(decision.get("end_date"), errors="coerce").date()
                if decision.get("end_date") else None,
            )
            with self.connect() as conn:
                conn.execute(
                    "UPDATE skp_condition_decision SET synced_to_tukin=true,synced_at=now(),updated_at=now() WHERE id=?",
                    [decision.get("id")],
                )
            synced += 1
        return {"status": "ok", "assessment_period": assessment, "synced": synced}

    def add_condition(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Store a manual condition and apply its explicit, auditable effect."""
        assessment = _period(_text(payload.get("assessment_period")))
        nip = _text(payload.get("nip"))
        if not nip:
            raise ValueError("NIP wajib diisi")
        code = _text(payload.get("condition_code")).upper().replace(" ", "_")
        if code not in {"3T", "HUKDIS", "CUTI_MELAHIRKAN", "LAINNYA"}:
            raise ValueError("Kondisi harus 3T, HUKDIS, CUTI_MELAHIRKAN, atau LAINNYA")
        effect = _text(payload.get("effect_type") or "REVIEW").upper()
        allowed = {"REVIEW", "HOLD", "UNPAID", "EXEMPT_SKP", "EXEMPT_ATTENDANCE", "EXEMPT_BOTH"}
        if effect not in allowed:
            raise ValueError("Efek kondisi tidak valid")
        start = pd.to_datetime(payload.get("start_date"), errors="coerce")
        end = pd.to_datetime(payload.get("end_date"), errors="coerce")
        start_date = None if pd.isna(start) else start.date()
        end_date = None if pd.isna(end) else end.date()
        if start_date and end_date and start_date > end_date:
            raise ValueError("Tanggal mulai tidak boleh setelah tanggal selesai")

        auto_rule = "MANUAL"
        if code == "CUTI_MELAHIRKAN":
            if not start_date or not end_date:
                raise ValueError("Tanggal mulai dan selesai wajib untuk cuti melahirkan")
            year, month = map(int, assessment.split("-"))
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
            if start_date <= month_start and end_date >= month_end:
                effect = "EXEMPT_BOTH"
                auto_rule = "FULL_MONTH_EXEMPT_BOTH"
            else:
                auto_rule = "PARTIAL_MONTH_ATTENDANCE_DATES_EXCLUDED_MANUAL_SKP_EFFECT"

        condition_id = _text(payload.get("id")) or str(uuid.uuid4())
        reason = _text(payload.get("reason"))
        evidence = _text(payload.get("evidence_ref"))
        approved_by = _text(payload.get("approved_by"))
        with self.connect() as conn:
            period_state = conn.execute(
                "SELECT status FROM tukin_period WHERE assessment_period=?", [assessment]
            ).fetchone()
            if period_state and _text(period_state[0]).upper() in {"LOCKED", "PAID", "HISTORICAL_LOCKED"}:
                raise ValueError("Periode terkunci atau sudah dibayar; kondisi tidak dapat diubah")
            exists = conn.execute(
                "SELECT 1 FROM tukin_final_monthly WHERE assessment_period=? AND nip=?",
                [assessment, nip],
            ).fetchone()
            if not exists:
                raise KeyError("Data final SDM pada periode tersebut belum tersedia")
            conn.execute("DELETE FROM tukin_condition WHERE id=?", [condition_id])
            conn.execute(
                """INSERT INTO tukin_condition
                   (id,nip,assessment_period,condition_code,start_date,end_date,effect_type,
                    effect_value,reason,evidence_ref,approved_by,approved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,CASE WHEN ?<>'' THEN now() ELSE NULL END)""",
                [condition_id, nip, assessment, code, start_date, end_date, effect,
                 _number(payload.get("effect_value")), reason, evidence, approved_by, approved_by],
            )
        self._apply_condition(assessment, nip, code, effect, start_date, end_date)
        return {"status": "ok", "id": condition_id, "assessment_period": assessment,
                "nip": nip, "condition_code": code, "effect_type": effect,
                "automatic_rule": auto_rule}

    def _apply_condition(self, assessment: str, nip: str, code: str, effect: str,
                         start_date: Optional[date], end_date: Optional[date]) -> None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT tukin_gross,skp_reduction_rate,attendance_reduction_rate,
                          skp_deduction,attendance_deduction,manual_adjustment,
                          payment_status,payment_reason,special_conditions,paid_amount
                   FROM tukin_final_monthly WHERE assessment_period=? AND nip=?""",
                [assessment, nip],
            ).fetchone()
            if not row:
                return
            gross, skp_rate, attendance_rate, skp_deduction, attendance_deduction, adjustment, pay_status, pay_reason, conditions, current_paid = row
            gross = float(gross or 0); skp_rate = float(skp_rate or 0); attendance_rate = float(attendance_rate or 0)
            skp_deduction = float(skp_deduction or 0); attendance_deduction = float(attendance_deduction or 0)
            adjustment = float(adjustment or 0)

            if code == "CUTI_MELAHIRKAN" and effect != "EXEMPT_BOTH" and start_date and end_date:
                daily = conn.execute(
                    """SELECT count(*) AS n,
                              coalesce(sum(CASE WHEN work_date NOT BETWEEN ? AND ? THEN reduction_rate ELSE 0 END),0) AS retained_rate
                       FROM tukin_attendance_daily WHERE assessment_period=? AND nip=?""",
                    [start_date, end_date, assessment, nip],
                ).fetchone()
                if daily and int(daily[0] or 0) > 0:
                    attendance_rate = min(100.0, float(daily[1] or 0))
                    attendance_deduction = round(gross * .30 * attendance_rate / 100)
            if effect in {"EXEMPT_SKP", "EXEMPT_BOTH"}:
                skp_rate = 0.0; skp_deduction = 0.0
                conn.execute(
                    """UPDATE tukin_skp_monthly SET report_status=?,
                       exception_code=?,skp_reduction_rate=0,payment_eligible=true,updated_at=now()
                       WHERE assessment_period=? AND nip=?""",
                    ["EXEMPT_MATERNITY" if code == "CUTI_MELAHIRKAN" else "EXEMPT_OTHER",
                     code, assessment, nip],
                )
            if effect in {"EXEMPT_ATTENDANCE", "EXEMPT_BOTH"}:
                attendance_rate = 0.0; attendance_deduction = 0.0
            total = max(0.0, skp_deduction + attendance_deduction - adjustment)
            entitlement = max(0.0, gross - total)
            # A neutral/review condition must never bypass another HOLD (for
            # example SKP not yet approved). Preserve the current payment state
            # unless this condition explicitly changes it.
            paid = float(current_paid or 0)
            if effect == "UNPAID":
                paid = 0.0; pay_status = "UNPAID"
                pay_reason = "Hukuman disiplin" if code == "HUKDIS" else f"Kondisi {code}"
            elif effect == "HOLD":
                paid = 0.0; pay_status = "HOLD"
                pay_reason = "Hukuman disiplin - pembayaran ditahan untuk verifikasi" if code == "HUKDIS" else f"Kondisi {code} - pembayaran ditahan"
            elif effect == "REVIEW" and pay_reason in {"Hukuman disiplin", "Hukuman disiplin - pembayaran ditahan untuk verifikasi"}:
                pay_status = "READY_TO_PAY"; pay_reason = ""; paid = entitlement
            elif effect in {"EXEMPT_SKP", "EXEMPT_ATTENDANCE", "EXEMPT_BOTH"} and pay_status in {"READY_TO_PAY", "PAID"}:
                paid = entitlement
            condition_list = [item for item in _text(conditions).split(",") if item]
            if code not in condition_list:
                condition_list.append(code)
            conn.execute(
                """UPDATE tukin_final_monthly SET skp_reduction_rate=?,attendance_reduction_rate=?,
                   skp_deduction=?,attendance_deduction=?,total_deduction=?,calculated_entitlement=?,
                   paid_amount=?,payment_status=?,payment_reason=?,special_conditions=?,updated_at=now()
                   WHERE assessment_period=? AND nip=?""",
                [skp_rate, attendance_rate, skp_deduction, attendance_deduction, total,
                 entitlement, paid, pay_status, pay_reason, ",".join(condition_list), assessment, nip],
            )

    def list_periods(self, year: int) -> dict[str, Any]:
        with self.connect() as conn:
            df = conn.execute(
                """WITH final_counts AS (
                         SELECT assessment_period,count(*) final_count
                         FROM tukin_final_monthly GROUP BY assessment_period
                       ), skp_counts AS (
                         SELECT assessment_period,count(*) skp_count
                         FROM tukin_skp_monthly GROUP BY assessment_period
                       ), daily_counts AS (
                         SELECT assessment_period,count(DISTINCT nip) daily_people_count
                         FROM tukin_attendance_daily GROUP BY assessment_period
                       )
                   SELECT p.assessment_period,p.payment_period,p.semester,p.status,p.source_mode,
                          coalesce(f.final_count,0) final_count,coalesce(s.skp_count,0) skp_count,
                          coalesce(d.daily_people_count,0) daily_people_count
                   FROM tukin_period p
                   LEFT JOIN final_counts f ON f.assessment_period=p.assessment_period
                   LEFT JOIN skp_counts s ON s.assessment_period=p.assessment_period
                   LEFT JOIN daily_counts d ON d.assessment_period=p.assessment_period
                   WHERE p.year=? ORDER BY p.assessment_period""",
                [year],
            ).fetchdf()
        return {"status": "ok", "year": year, "periods": self._records(df)}

    def list_period_audit(self, assessment: str) -> dict[str, Any]:
        assessment = _period(assessment)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tukin_period_audit (
                id VARCHAR PRIMARY KEY, assessment_period VARCHAR NOT NULL,
                action VARCHAR NOT NULL, old_status VARCHAR, new_status VARCHAR,
                actor VARCHAR NOT NULL, reason VARCHAR NOT NULL, created_at TIMESTAMP DEFAULT now()
            )""")
            frame = conn.execute(
                """SELECT assessment_period,action,old_status,new_status,actor,reason,created_at
                   FROM tukin_period_audit WHERE assessment_period=? ORDER BY created_at DESC""",
                [assessment],
            ).fetchdf()
        return {"status": "ok", "assessment_period": assessment, "rows": self._records(frame)}

    def list_final_runs(self, year: int) -> dict[str, Any]:
        with self.connect() as conn:
            df = conn.execute(
                """SELECT assessment_period,
                          min(payment_period) payment_period,
                          count(*) jumlah_sdm,
                          sum(tukin_gross) total_tukin_bruto,
                          sum(attendance_deduction) total_potongan_absen,
                          sum(skp_deduction) total_potongan_skp,
                          sum(total_deduction) total_potongan,
                          sum(paid_amount) total_final_tukin,
                          max(updated_at) saved_at
                   FROM tukin_final_monthly
                   WHERE assessment_period BETWEEN ? AND ?
                   GROUP BY assessment_period
                   ORDER BY assessment_period DESC""",
                [f"{year}-01", f"{year}-12"],
            ).fetchdf()
        rows = self._records(df)
        for row in rows:
            row["run_id"] = f"FINAL-{row['assessment_period']}"
        return {"status": "ok", "year": year, "runs": rows}

    def delete_final_run(self, assessment_period: str) -> dict[str, Any]:
        assessment_period = _period(assessment_period)
        raise ValueError("Penghapusan hasil final harus dilakukan melalui proses terotorisasi")

    def delete_final_run_authorized(self, assessment_period: str, actor: str, reason: str, confirmation: str) -> dict[str, Any]:
        assessment_period = _period(assessment_period)
        actor = _text(actor)
        reason = _text(reason)
        if not actor or not reason or confirmation != f"HAPUS FINAL {assessment_period}":
            raise ValueError("Penghapusan memerlukan petugas, alasan, dan konfirmasi yang tepat")
        with self.connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                period = conn.execute(
                    "SELECT status FROM tukin_period WHERE assessment_period=?", [assessment_period]
                ).fetchone()
                if period and period[0] in {"LOCKED", "PAID", "HISTORICAL_LOCKED"}:
                    raise ValueError("Periode terkunci atau sudah dibayar; hasil tidak boleh dihapus")
                count = conn.execute(
                    "SELECT count(*) FROM tukin_final_monthly WHERE assessment_period=?",
                    [assessment_period],
                ).fetchone()[0]
                conn.execute("DELETE FROM tukin_final_monthly WHERE assessment_period=?", [assessment_period])
                conn.execute("""CREATE TABLE IF NOT EXISTS tukin_admin_audit (
                    id VARCHAR PRIMARY KEY, action VARCHAR NOT NULL, assessment_period VARCHAR,
                    actor VARCHAR NOT NULL, reason VARCHAR NOT NULL, details VARCHAR,
                    created_at TIMESTAMP DEFAULT now()
                )""")
                conn.execute(
                    """INSERT INTO tukin_admin_audit
                       (id,action,assessment_period,actor,reason,details,created_at)
                       VALUES (?,?,?,?,?,?,now())""",
                    [str(uuid.uuid4()), "DELETE_FINAL_RUN", assessment_period, actor, reason, f"deleted={count}"],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"status": "ok", "run_id": f"FINAL-{assessment_period}", "deleted": int(count)}

    def delete_all_final_results(self, actor: str, reason: str, confirmation: str) -> dict[str, Any]:
        actor = _text(actor)
        reason = _text(reason)
        if not actor or not reason or confirmation != "HAPUS SEMUA HASIL FINAL":
            raise ValueError("Penghapusan memerlukan petugas, alasan, dan konfirmasi yang tepat")
        with self.connect() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                locked = conn.execute(
                    "SELECT count(*) FROM tukin_period WHERE status IN ('LOCKED','PAID','HISTORICAL_LOCKED')"
                ).fetchone()[0]
                if locked:
                    raise ValueError("Tidak dapat menghapus semua hasil karena ada periode terkunci atau sudah dibayar")
                count = conn.execute("SELECT count(*) FROM tukin_final_monthly").fetchone()[0]
                conn.execute("DELETE FROM tukin_final_monthly")
                conn.execute("""CREATE TABLE IF NOT EXISTS tukin_admin_audit (
                    id VARCHAR PRIMARY KEY, action VARCHAR NOT NULL, assessment_period VARCHAR,
                    actor VARCHAR NOT NULL, reason VARCHAR NOT NULL, details VARCHAR,
                    created_at TIMESTAMP DEFAULT now()
                )""")
                conn.execute(
                    """INSERT INTO tukin_admin_audit
                       (id,action,assessment_period,actor,reason,details,created_at)
                       VALUES (?,?,?,?,?,?,now())""",
                    [str(uuid.uuid4()), "DELETE_ALL_FINAL", None, actor, reason, f"deleted={count}"],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"status": "ok", "deleted": int(count)}

    def list_conditions(self, year: int) -> dict[str, Any]:
        """Read-only register of decisions already materialized into Tukin."""
        with self.connect() as conn:
            frame = conn.execute(
                """SELECT c.assessment_period,c.nip,coalesce(f.nama,'') nama,
                          c.condition_code,c.start_date,c.end_date,c.effect_type,c.reason,
                          c.evidence_ref,c.approved_by,c.approved_at,
                          CASE WHEN c.id LIKE 'SKPDEC-%' THEN 'PENILAIAN SKP'
                               ELSE 'DATA LAMA/MANUAL' END condition_source
                   FROM tukin_condition c
                   LEFT JOIN tukin_final_monthly f
                     ON f.assessment_period=c.assessment_period AND f.nip=c.nip
                   WHERE c.assessment_period BETWEEN ? AND ?
                   ORDER BY c.assessment_period DESC,nama,c.nip,c.condition_code""",
                [f"{year}-01", f"{year}-12"],
            ).fetchdf()
        return {"status": "ok", "year": year, "rows": self._records(frame)}

    def annual(self, year: int, q: str = "", page: int = 1, per_page: int = 50) -> dict[str, Any]:
        start, end = f"{year}-01", f"{year}-12"
        needle = f"%{q.strip().lower()}%"
        with self.read_cursor() as conn:
            people_sql = """
                WITH source_people AS (
                    SELECT nip,nama,jabatan,province,bank_name,account_number
                    FROM tukin_final_monthly WHERE assessment_period BETWEEN ? AND ?
                    UNION ALL
                    SELECT nip,nama,jabatan,'' province,'' bank_name,'' account_number
                    FROM tukin_skp_monthly WHERE assessment_period BETWEEN ? AND ?
                ), people AS (
                    SELECT nip,max(coalesce(nama,'')) nama,max(coalesce(jabatan,'')) jabatan,
                           max(coalesce(province,'')) province,max(coalesce(bank_name,'')) bank_name,
                           max(coalesce(account_number,'')) account_number
                    FROM source_people GROUP BY nip
                )
            """
            filter_sql = " WHERE lower(coalesce(nip,'')) LIKE ? OR lower(coalesce(nama,'')) LIKE ?" if q.strip() else ""
            base_params = [start, end, start, end]
            filter_params = [needle, needle] if q.strip() else []
            total = int(conn.execute(
                people_sql + "SELECT count(*) FROM people" + filter_sql,
                base_params + filter_params,
            ).fetchone()[0])
            page_people = conn.execute(
                people_sql + "SELECT * FROM people" + filter_sql +
                " ORDER BY lower(nama),nip LIMIT ? OFFSET ?",
                base_params + filter_params + [per_page, (page - 1) * per_page],
            ).fetchdf()
            aggregate_select = """SELECT coalesce(sum(tukin_gross),0),coalesce(sum(skp_deduction),0),
                          coalesce(sum(attendance_deduction),0),coalesce(sum(paid_amount),0),
                          count(*) FILTER (WHERE payment_status='UNPAID')
                   FROM tukin_final_monthly f {join_sql}
                   WHERE f.assessment_period BETWEEN ? AND ?"""
            if q.strip():
                aggregate_row = conn.execute(
                    people_sql + ", matched AS (SELECT nip FROM people" + filter_sql + ") " +
                    aggregate_select.format(join_sql="JOIN matched m ON m.nip=f.nip"),
                    base_params + filter_params + [start, end],
                ).fetchone()
            else:
                aggregate_row = conn.execute(
                    aggregate_select.format(join_sql=""), [start, end]
                ).fetchone()
            page_nips = page_people['nip'].astype(str).tolist() if not page_people.empty else []
            if page_nips:
                placeholders = ','.join('?' for _ in page_nips)
                final = conn.execute(
                    f"SELECT * FROM tukin_final_monthly WHERE assessment_period BETWEEN ? AND ? "
                    f"AND nip IN ({placeholders})", [start, end, *page_nips]
                ).fetchdf()
                skp = conn.execute(
                    f"SELECT * FROM tukin_skp_monthly WHERE assessment_period BETWEEN ? AND ? "
                    f"AND nip IN ({placeholders})", [start, end, *page_nips]
                ).fetchdf()
            else:
                final = conn.execute("SELECT * FROM tukin_final_monthly LIMIT 0").fetchdf()
                skp = conn.execute("SELECT * FROM tukin_skp_monthly LIMIT 0").fetchdf()
        people: dict[str, dict[str, Any]] = {}
        for identity in page_people.to_dict("records"):
            nip = _text(identity.get("nip")); person = people.setdefault(nip, self._person_base(nip))
            self._fill_identity(person, identity)
        for row in final.to_dict("records"):
            nip = _text(row.get("nip")); person = people.setdefault(nip, self._person_base(nip))
            self._fill_identity(person, row)
            person["months"][int(str(row["assessment_period"])[5:7])-1].update(self._final_month(row))
        for row in skp.to_dict("records"):
            nip = _text(row.get("nip")); person = people.setdefault(nip, self._person_base(nip))
            self._fill_identity(person, row)
            month = person["months"][int(str(row["assessment_period"])[5:7])-1]
            month.update({
                "period": str(row["assessment_period"]), "predicate": _text(row.get("predicate_normalized")),
                "report_status": _text(row.get("report_status")),
                "consecutive_missing": int(row.get("consecutive_missing") or 0),
                "skp_rate": float(row.get("skp_reduction_rate") or 0),
                "payment_eligible": bool(row.get("payment_eligible", True)),
            })
        values = list(people.values())
        for person in values:
            self._summarize_person(person)
        values.sort(key=lambda p: (p["nama"].lower(), p["nip"]))
        aggregate = {
            "people": total,
            "gross": float(aggregate_row[0] or 0),
            "skp_deduction": float(aggregate_row[1] or 0),
            "attendance_deduction": float(aggregate_row[2] or 0),
            "paid": float(aggregate_row[3] or 0),
            "unpaid_months": int(aggregate_row[4] or 0),
        }
        return {"status": "ok", "year": year, "page": page, "per_page": per_page,
                "total": total, "pages": max(1, math.ceil(total/per_page)),
                "aggregate": aggregate, "rows": values}

    @staticmethod
    def _person_base(nip: str) -> dict[str, Any]:
        return {"nip": nip, "nama": "", "jabatan": "", "province": "", "bank_name": "",
                "account_number": "", "months": [{"month": i, "month_name": MONTH_NAMES[i], "period": ""} for i in range(1,13)]}

    @staticmethod
    def _fill_identity(person, row):
        for target, source in (("nama","nama"),("jabatan","jabatan"),("province","province"),
                               ("bank_name","bank_name"),("account_number","account_number")):
            value = _text(row.get(source))
            if value: person[target] = value

    @staticmethod
    def _final_month(row):
        return {
            "period": str(row["assessment_period"]), "payment_period": _text(row.get("payment_period")),
            "predicate": _text(row.get("predicate_skp")), "report_status": _text(row.get("report_status")),
            "gross": float(row.get("tukin_gross") or 0), "skp_rate": float(row.get("skp_reduction_rate") or 0),
            "attendance_rate": float(row.get("attendance_reduction_rate") or 0),
            "skp_deduction": float(row.get("skp_deduction") or 0),
            "attendance_deduction": float(row.get("attendance_deduction") or 0),
            "total_deduction": float(row.get("total_deduction") or 0),
            "entitlement": float(row.get("calculated_entitlement") or 0),
            "paid": float(row.get("paid_amount") or 0), "payment_status": _text(row.get("payment_status")),
            "payment_reason": _text(row.get("payment_reason")),
            "special_conditions": _text(row.get("special_conditions")),
            "bank_name": _text(row.get("bank_name")), "account_number": _text(row.get("account_number")),
            "account_holder": _text(row.get("account_holder")), "unit": _text(row.get("unit")),
            "kelas_jabatan": row.get("kelas_jabatan"), "master_version": row.get("master_version"),
            "temporary_assignment_type": _text(row.get("temporary_assignment_type")),
            "temporary_assignment_detail": _text(row.get("temporary_assignment_detail")),
            "temporary_assignment_effective_month": _text(row.get("temporary_assignment_effective_month")),
            "death_record_id": _text(row.get("death_record_id")),
            "recipient_type": _text(row.get("recipient_type")),
            "recipient_name": _text(row.get("recipient_name")),
            "recipient_relationship": _text(row.get("recipient_relationship")),
            "recipient_bank_name": _text(row.get("recipient_bank_name")),
            "recipient_account_number": _text(row.get("recipient_account_number")),
            "recipient_account_holder": _text(row.get("recipient_account_holder")),
            "beneficiary_document_ref": _text(row.get("beneficiary_document_ref")),
        }

    @staticmethod
    def _summarize_person(person):
        months = person["months"]
        completed = [m for m in months if m.get("period")]
        predicate_counts = {}
        for month in completed:
            pred = month.get("predicate") or month.get("report_status") or "Belum Ada"
            predicate_counts[pred] = predicate_counts.get(pred, 0) + 1
        def block(items):
            items = [m for m in items if m.get("period")]
            return {
                "months_available": len(items), "gross": sum(m.get("gross",0) for m in items),
                "skp_deduction": sum(m.get("skp_deduction",0) for m in items),
                "attendance_deduction": sum(m.get("attendance_deduction",0) for m in items),
                "paid": sum(m.get("paid",0) for m in items),
                "unpaid_months": sum(1 for m in items if m.get("payment_status") == "UNPAID"),
            }
        person["semester_1"] = block(months[:6]); person["semester_2"] = block(months[6:])
        person["summary"] = {**block(completed), "predicate_counts": predicate_counts,
                             "max_missing_streak": max((m.get("consecutive_missing",0) for m in completed), default=0)}

    def person_detail(self, nip: str, year: int) -> dict[str, Any]:
        result = self.annual(year, q=nip, page=1, per_page=100)
        person = next((row for row in result["rows"] if row["nip"] == nip), None)
        if person is None:
            raise KeyError("SDM tidak ditemukan")
        with self.connect() as conn:
            daily = conn.execute(
                """SELECT assessment_period,work_date,day_name,check_in,check_out,attendance_type,
                          attendance_status,reduction_rate,deduction_amount,reason,evidence_ref
                   FROM tukin_attendance_daily WHERE nip=? AND assessment_period BETWEEN ? AND ?
                   ORDER BY work_date""", [nip, f"{year}-01", f"{year}-12"]
            ).fetchdf()
            conditions = conn.execute(
                "SELECT * FROM tukin_condition WHERE nip=? AND assessment_period BETWEEN ? AND ? ORDER BY assessment_period,created_at",
                [nip, f"{year}-01", f"{year}-12"],
            ).fetchdf()
            death = conn.execute(
                "SELECT * FROM sdm_death_record WHERE nip=?", [nip]
            ).fetchdf()
            death_audit = conn.execute(
                "SELECT action,old_value,new_value,actor,created_at FROM sdm_death_audit WHERE nip=? ORDER BY created_at",
                [nip],
            ).fetchdf()
        return {"status": "ok", "year": year, "person": person,
                "daily": self._records(daily), "conditions": self._records(conditions),
                "death": self._records(death)[0] if not death.empty else None,
                "death_audit": self._records(death_audit)}

    def lock_period(self, assessment: str, status: str, actor: Optional[str] = None, reason: Optional[str] = None) -> dict[str, Any]:
        assessment = _period(assessment); status = _text(status).upper()
        if status not in {"DRAFT", "REVIEW", "APPROVED", "LOCKED", "PAID", "HISTORICAL_LOCKED"}:
            raise ValueError("Status periode tidak valid")
        legacy_call = actor is None and reason is None
        if not legacy_call and (not _text(actor) or not _text(reason)):
            raise ValueError("Petugas dan alasan perubahan status wajib diisi")
        actor = _text(actor) or "EVKIN"
        reason = _text(reason) or "Perubahan status periode"
        with self.connect() as conn:
            exists = conn.execute("SELECT status FROM tukin_period WHERE assessment_period=?", [assessment]).fetchone()
            if not exists: raise KeyError("Periode tidak ditemukan")
            old_status = str(exists[0] or "DRAFT")
            canonical = None
            has_canonical = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='tukin_final_decision'"
            ).fetchone()[0]
            if has_canonical:
                canonical = conn.execute(
                    """SELECT status FROM tukin_final_decision WHERE assessment_period=?
                       ORDER BY created_at DESC LIMIT 1""", [assessment]
                ).fetchone()
            if canonical and canonical[0] == "PAID" and status != "PAID":
                raise ValueError("Final Decision canonical sudah PAID dan tidak dapat diturunkan")
            if canonical and status == "PAID":
                raise ValueError("Gunakan payment action pada Final Decision canonical, bukan status periode legacy")
            allowed = {
                "DRAFT": {"DRAFT", "REVIEW"},
                "REVIEW": {"DRAFT", "REVIEW", "APPROVED"},
                "APPROVED": {"REVIEW", "APPROVED", "LOCKED"},
                "LOCKED": {"LOCKED", "PAID"},
                "PAID": {"PAID"},
                "HISTORICAL_LOCKED": {"HISTORICAL_LOCKED"},
            }
            if not legacy_call and status not in allowed.get(old_status, {old_status}):
                raise ValueError(f"Transisi status tidak valid: {old_status} ke {status}")
            if not legacy_call and status in {"APPROVED", "LOCKED", "PAID"}:
                incomplete = conn.execute(
                    """SELECT count(*) FROM tukin_final_monthly
                       WHERE assessment_period=? AND (payment_status='HOLD'
                         OR calculated_entitlement IS NULL)""", [assessment]
                ).fetchone()[0]
                if incomplete and status in {"LOCKED", "PAID"}:
                    raise ValueError("Periode masih memiliki data ditahan/tidak lengkap")
            conn.execute(
                "UPDATE tukin_period SET status=?,updated_at=now(),locked_at=CASE WHEN ? IN ('LOCKED','PAID','HISTORICAL_LOCKED') THEN now() ELSE locked_at END WHERE assessment_period=?",
                [status, status, assessment],
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS tukin_period_audit (
                id VARCHAR PRIMARY KEY, assessment_period VARCHAR NOT NULL,
                action VARCHAR NOT NULL, old_status VARCHAR, new_status VARCHAR,
                actor VARCHAR NOT NULL, reason VARCHAR NOT NULL, created_at TIMESTAMP DEFAULT now()
            )""")
            conn.execute(
                """INSERT INTO tukin_period_audit
                   (id,assessment_period,action,old_status,new_status,actor,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,now())""",
                [str(uuid.uuid4()), assessment, "STATUS_CHANGE", old_status, status, actor, reason],
            )
            if status == "PAID" and exists[0] != "PAID":
                payable = conn.execute(
                    """SELECT nip,paid_amount FROM tukin_final_monthly
                       WHERE assessment_period=? AND payment_status NOT IN ('UNPAID','HOLD')""",
                    [assessment],
                ).fetchall()
                conn.execute(
                    """UPDATE tukin_final_monthly SET payment_status='PAID',updated_at=now()
                       WHERE assessment_period=? AND payment_status NOT IN ('UNPAID','HOLD')""",
                    [assessment],
                )
                if payable:
                    conn.executemany(
                        """INSERT INTO tukin_payment_event
                           (id,assessment_period,nip,status,amount,reason_code,created_by)
                           VALUES (?,?,?,?,?,?,?)""",
                        [(str(uuid.uuid4()), assessment, str(nip), "PAID", float(amount or 0),
                          "PERIOD_MARKED_PAID", actor) for nip, amount in payable],
                    )
        return {"status": "ok", "assessment_period": assessment, "period_status": status}

    def export_annual(self, year: int) -> io.BytesIO:
        annual = self.annual(year, page=1, per_page=1_000_000)
        people = annual["rows"]
        summary_rows, matrix_pred, matrix_skp, matrix_abs, matrix_paid, final_rows = [], [], [], [], [], []
        for p in people:
            s = p["summary"]
            summary_rows.append({"NIP": p["nip"], "Nama": p["nama"], "Jabatan": p["jabatan"],
                                 "Provinsi": p["province"], "Bulan Tersedia": s["months_available"],
                                 "Streak Tidak SKP Maks": s["max_missing_streak"], "Tukin Bruto": s["gross"],
                                 "Potongan SKP": s["skp_deduction"], "Potongan Absensi": s["attendance_deduction"],
                                 "Total Dibayarkan": s["paid"], "Bulan Tidak Dibayarkan": s["unpaid_months"]})
            bases = [{"NIP": p["nip"], "Nama": p["nama"]} for _ in range(4)]
            for month in p["months"]:
                label = month["month_name"]
                bases[0][label] = month.get("predicate") or month.get("report_status") or "BELUM ADA DATA"
                bases[1][label] = month.get("skp_deduction", 0) if month.get("period") else None
                bases[2][label] = month.get("attendance_deduction", 0) if month.get("period") else None
                bases[3][label] = month.get("paid", 0) if month.get("period") else None
                if month.get("period"):
                    final_rows.append({"NIP": p["nip"], "Nama": p["nama"], "Periode Penilaian": month["period"],
                                       "Periode Pembayaran": month.get("payment_period"), "Predikat": month.get("predicate"),
                                       "Status Laporan": month.get("report_status"), "Tukin Bruto": month.get("gross",0),
                                       "Potongan SKP": month.get("skp_deduction",0), "Potongan Absensi": month.get("attendance_deduction",0),
                                       "Total Potongan": month.get("total_deduction",0), "Hak Tukin": month.get("entitlement",0),
                                       "Dibayarkan": month.get("paid",0), "Status Pembayaran": month.get("payment_status"),
                                       "Alasan": month.get("payment_reason"), "Kondisi": month.get("special_conditions"),
                                       "Bank": month.get("bank_name"), "NO REKENING": month.get("account_number"),
                                       "Pemilik Rekening": month.get("account_holder"), "Unit": month.get("unit"),
                                       "Kelas Jabatan": month.get("kelas_jabatan"), "Versi Master": month.get("master_version"),
                                       "Jenis Penerima": month.get("recipient_type"),
                                       "Nama Penerima": month.get("recipient_name"),
                                       "Hubungan Penerima": month.get("recipient_relationship"),
                                       "Bank Penerima": month.get("recipient_bank_name"),
                                       "NO REKENING PENERIMA": month.get("recipient_account_number"),
                                       "Pemilik Rekening Penerima": month.get("recipient_account_holder"),
                                       "Dokumen Ahli Waris": month.get("beneficiary_document_ref")})
            matrix_pred.append(bases[0]); matrix_skp.append(bases[1]); matrix_abs.append(bases[2]); matrix_paid.append(bases[3])
        periods = self.list_periods(year)["periods"]
        with self.connect() as conn:
            imports = conn.execute(
                "SELECT * FROM tukin_import_batch WHERE assessment_period BETWEEN ? AND ? ORDER BY created_at",
                [f"{year}-01", f"{year}-12"],
            ).fetchdf()
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            sheets = {
                "REKAP TAHUNAN": pd.DataFrame(summary_rows),
                "SEMESTER I": self._semester_frame(people, 1),
                "SEMESTER II": self._semester_frame(people, 2),
                "MATRIKS PREDIKAT": pd.DataFrame(matrix_pred),
                "MATRIKS POT SKP": pd.DataFrame(matrix_skp),
                "MATRIKS POT ABSEN": pd.DataFrame(matrix_abs),
                "MATRIKS DIBAYARKAN": pd.DataFrame(matrix_paid),
                "FINAL BULANAN": pd.DataFrame(final_rows),
                "KELENGKAPAN PERIODE": pd.DataFrame(periods),
                "RIWAYAT IMPORT": imports,
            }
            money_columns = {"Tukin Bruto", "Potongan SKP", "Potongan Absensi", "Total Dibayarkan", "Total Potongan", "Hak Tukin", "Dibayarkan"}
            for name, frame in sheets.items():
                if frame.empty and len(frame.columns) == 0:
                    frame = pd.DataFrame({"Keterangan": ["Belum ada data pada tahun ini"]})
                frame.to_excel(writer, sheet_name=name[:31], index=False)
                ws = writer.sheets[name[:31]]; wb = writer.book
                header_fmt = wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#0F766E", "border": 0, "align": "center"})
                money_fmt = wb.add_format({"num_format": "#,##0"})
                text_fmt = wb.add_format({"num_format": "@"})
                ws.freeze_panes(1, 2); ws.autofilter(0, 0, max(len(frame), 1), max(len(frame.columns)-1, 0))
                for col, column in enumerate(frame.columns):
                    ws.write(0, col, column, header_fmt)
                    width = min(34, max(12, len(str(column))+2))
                    fmt = money_fmt if column in money_columns or name in {"MATRIKS POT SKP","MATRIKS POT ABSEN","MATRIKS DIBAYARKAN"} and col >= 2 else None
                    if column in {"NIP", "NO REKENING", "Nomor Rekening"}: fmt = text_fmt
                    ws.set_column(col, col, width, fmt)
        output.seek(0)
        return output

    @staticmethod
    def _semester_frame(people, semester):
        rows=[]; key=f"semester_{semester}"
        for p in people:
            s=p[key]
            rows.append({"NIP":p["nip"],"Nama":p["nama"],"Jabatan":p["jabatan"],"Bulan Tersedia":s["months_available"],
                         "Tukin Bruto":s["gross"],"Potongan SKP":s["skp_deduction"],"Potongan Absensi":s["attendance_deduction"],
                         "Dibayarkan":s["paid"],"Bulan Tidak Dibayarkan":s["unpaid_months"]})
        return pd.DataFrame(rows)

    def export_final(self, assessment: str) -> io.BytesIO:
        assessment = _period(assessment)
        with self.connect() as conn:
            has_canonical = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='tukin_final_decision'"
            ).fetchone()[0]
            decision = conn.execute(
                """SELECT id FROM tukin_final_decision WHERE assessment_period=?
                   ORDER BY created_at DESC LIMIT 1""", [assessment]
            ).fetchone() if has_canonical else None
        if decision:
            # Compatibility wrapper: new Final exports always use the locked canonical snapshot.
            from backend.final_tukin import FinalTukinService
            return FinalTukinService(self.db_path).export_final_decision(decision[0])
        with self.connect() as conn:
            # Historical read compatibility only; no new canonical Final Decision is created here.
            df = conn.execute("SELECT * FROM tukin_final_monthly WHERE assessment_period=? ORDER BY nama,nip", [assessment]).fetchdf()
        if df.empty: raise KeyError("Data final periode belum tersedia")
        rename = {"penilai":"Penilai","nip":"NIP","kd_anak_satker":"KD ANAK SATKER","nama":"Nama","jabatan":"Jabatan",
                  "unit":"UNIT KERJA","kelas_jabatan":"KELAS JABATAN","employment_status":"STATUS KEPEGAWAIAN",
                  "temporary_assignment_type":"PENUGASAN SEMENTARA","temporary_assignment_detail":"DETAIL PENUGASAN",
                  "temporary_assignment_effective_month":"BERLAKU MULAI PENUGASAN",
                  "tahap":"Tahap","wilayah_3t":"3T","status_sdm":"SDM","jabatan_sdm":"SDM JABATAN","predicate_skp":"Predikat Kinerja Periodik",
                  "report_status":"STATUS LAPORAN SKP","tahun":"Tahun","bulan":"Bulan","skp_reduction_rate":"Potongan SKP (%)",
                  "attendance_reduction_rate":"Potongan Absensi (%)","tukin_gross":"Tukin","skp_deduction":"Potongan SKP Final",
                  "attendance_deduction":"Potongan Absen Final","total_deduction":"Potongan Final","calculated_entitlement":"HAK TUKIN TERHITUNG",
                  "paid_amount":"FINAL TUKIN DIBAYAR","payment_status":"STATUS PEMBAYARAN","payment_reason":"ALASAN STATUS",
                  "province":"PROVINSI","regency":"KABUPATEN","account_number":"NO REKENING","bank_name":"BANK",
                  "account_holder":"PEMILIK REKENING","special_conditions":"KONDISI KHUSUS","evidence_status":"STATUS EVIDEN",
                  "master_version":"VERSI MASTER","snapshot_at":"WAKTU SNAPSHOT",
                  "death_record_id":"ID CATATAN MENINGGAL","recipient_type":"JENIS PENERIMA",
                  "recipient_name":"NAMA PENERIMA","recipient_relationship":"HUBUNGAN PENERIMA",
                  "recipient_bank_name":"BANK PENERIMA","recipient_account_number":"NO REKENING PENERIMA",
                  "recipient_account_holder":"PEMILIK REKENING PENERIMA",
                  "beneficiary_document_ref":"DOKUMEN AHLI WARIS"}
        columns = [col for col in rename if col in df.columns]
        out_df = df[columns].rename(columns=rename)
        output=io.BytesIO()
        with pd.ExcelWriter(output,engine="xlsxwriter") as writer:
            out_df.to_excel(writer,index=False,sheet_name="FINAL TUKIN")
            ws=writer.sheets["FINAL TUKIN"]; wb=writer.book
            header=wb.add_format({"bold":True,"font_color":"#FFFFFF","bg_color":"#0F766E","align":"center","valign":"vcenter"})
            money=wb.add_format({"num_format":"#,##0"}); text=wb.add_format({"num_format":"@"})
            ws.freeze_panes(1,4); ws.autofilter(0,0,len(out_df),len(out_df.columns)-1); ws.set_row(0,30)
            for i,col in enumerate(out_df.columns):
                ws.write(0,i,col,header); fmt=text if col in {"NIP","NO REKENING","NO REKENING PENERIMA"} else money if any(x in col for x in ("Tukin","Potongan Final","DIBAYAR","TERHITUNG")) else None
                ws.set_column(i,i,min(32,max(12,len(col)+2)),fmt)
        output.seek(0); return output

    @staticmethod
    def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
        if df.empty: return []
        clean = df.astype(object).where(pd.notnull(df), None)
        records = clean.to_dict("records")
        for record in records:
            for key, value in list(record.items()):
                if isinstance(value, (datetime, date, pd.Timestamp)): record[key] = value.isoformat()
                elif hasattr(value, "item"):
                    try: record[key] = value.item()
                    except Exception: pass
        return records


def create_router(db_path_getter: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/api/tukin", tags=["tukin-annual"])
    service = lambda: TukinAnnualService(db_path_getter())

    @router.get("/periods")
    def periods(year: int = Query(2026, ge=2000, le=2100)):
        return service().list_periods(year)

    @router.get("/final-results/runs")
    def final_result_runs(year: int = Query(2026, ge=2000, le=2100)):
        return service().list_final_runs(year)

    @router.delete("/final-results/runs/{assessment_period}")
    def delete_final_result_run(assessment_period: str, payload: dict[str, Any] | None = None):
        try:
            payload = payload or {}
            return service().delete_final_run_authorized(
                assessment_period, _text(payload.get("actor")), _text(payload.get("reason")), _text(payload.get("confirmation"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/final-results")
    def delete_all_final_results(payload: dict[str, Any] | None = None):
        payload = payload or {}
        try:
            return service().delete_all_final_results(
                _text(payload.get("actor")), _text(payload.get("reason")), _text(payload.get("confirmation"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/history/import")
    def import_history(payload: dict[str, Any]):
        try: return service().import_rows(payload)
        except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc: raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/periods/capture-live")
    def capture_live(payload: dict[str, Any]):
        try: return service().capture_live(
            _text(payload.get("assessment_period")), _text(payload.get("payment_period")), enforce_workflow=True
        )
        except WorkflowNotReadyError as exc:
            raise HTTPException(status_code=409, detail={
                "code": "WORKFLOW_NOT_READY",
                "message": str(exc),
                "period": exc.result["period"],
                "blocking_reasons": exc.result["blocking_reasons"],
                "checks": exc.result["checks"],
            }) from exc
        except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/periods/status")
    def period_status(payload: dict[str, Any]):
        try: return service().lock_period(
            _text(payload.get("assessment_period")), _text(payload.get("status")),
            _text(payload.get("actor")), _text(payload.get("reason"))
        )
        except (ValueError, KeyError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/periods/{assessment_period}/audit")
    def period_audit(assessment_period: str):
        try: return service().list_period_audit(assessment_period)
        except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/conditions")
    def add_condition(payload: dict[str, Any]):
        try: return service().add_condition(payload)
        except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/conditions")
    def conditions(year: int = Query(2026, ge=2000, le=2100)):
        return service().list_conditions(year)

    @router.get("/annual")
    def annual(year: int = Query(2026, ge=2000, le=2100), q: str = "", page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500)):
        return service().annual(year, q, page, per_page)

    @router.get("/people/{nip}")
    def person(nip: str, year: int = Query(2026, ge=2000, le=2100)):
        try: return service().person_detail(nip, year)
        except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/export/annual")
    def export_annual(year: int = Query(2026, ge=2000, le=2100)):
        stream=service().export_annual(year)
        return StreamingResponse(stream,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition":f'attachment; filename="REKAP_TUKIN_{year}.xlsx"'})

    @router.get("/export/final")
    def export_final(assessment_period: str = Query(...)):
        try: stream=service().export_final(assessment_period)
        except (ValueError,KeyError) as exc: raise HTTPException(status_code=404,detail=str(exc)) from exc
        filename=f"FINAL_TUKIN_{assessment_period}.xlsx"
        return StreamingResponse(stream,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition":f'attachment; filename="{filename}"'})

    return router

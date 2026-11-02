"""Canonical shared SDM master used by attendance, SKP, and Tukin modules."""
from __future__ import annotations

import importlib
import hashlib
import json
import math
import re
import threading
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Optional

import duckdb
from backend.lazy_imports import lazy_module

pd = lazy_module("pandas")
from fastapi import APIRouter, HTTPException, Query


ensure_shared_master_schema = importlib.import_module(
    "backend.migrations.035_create_shared_sdm_master"
).ensure_shared_master_schema

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()


def _clean(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _number(value: Any) -> float:
    from backend.tukin_annual import _number as parse_number
    return parse_number(value)


ALIASES = {
    "nip": ("nip", "nomorindukpegawai"),
    "nama": ("nama", "namasdm", "namapegawai"),
    "jenis_sdm": ("jenissdm", "sdm", "statussdm"),
    "employment_status": ("statuskepegawaian", "statusaktif", "statuspegawai"),
    "jabatan": ("jabatan", "jabatanformal"),
    "unit": ("unit", "unitkerja", "satker", "satuankerja"),
    "kd_anak_satker": ("kdanaksatker", "kdanaksakter", "kodeanaksatker"),
    "provinsi": ("provinsi",),
    "kabupaten_kota": ("kabupatenkota", "kabupaten", "kabkota"),
    "penilai": ("penilai", "atasanpenilai"),
    "kelas_jabatan": ("kelasjabatan", "kelas", "grade"),
    "tukin_gross": ("tukin", "tukinbruto", "besarantukin"),
    "eligible": ("berhaktukin", "eligible", "kelayakanpembayaran"),
    "eligibility_reason": ("alasantidakberhak", "alasankelayakan"),
    "wilayah_3t": ("3t", "wilayah3t", "status3t"),
    "status_tendik": ("statustendik",),
    "jabatan_osdm": ("jabatanosdm", "sdmjabatan"),
    "bank_name": ("bank", "namabank"),
    "account_number": ("norekening", "nomorrekening", "rekening"),
    "account_holder": ("namapemilikrekening", "pemilikrekening", "namarekening"),
}

IDENTITY_FIELDS = (
    "nama", "jenis_sdm", "employment_status", "jabatan", "unit",
    "kd_anak_satker", "provinsi", "kabupaten_kota", "penilai",
)
ASSIGNMENT_FIELDS = ("jabatan", "unit", "kd_anak_satker", "provinsi", "kabupaten_kota", "penilai")
PROFILE_FIELDS = ("kelas_jabatan", "tukin_gross", "eligible", "eligibility_reason", "wilayah_3t", "status_tendik", "jabatan_osdm")
BANK_FIELDS = ("bank_name", "account_number", "account_holder")


def _header_mapping(row: Mapping[str, Any]) -> dict[str, str]:
    normalized = {_clean(header): str(header) for header in row.keys()}
    result: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if _clean(alias) in normalized:
                result[field] = normalized[_clean(alias)]
                break
    return result


def _bool(value: Any, default: bool) -> bool:
    raw = _text(value).upper()
    if not raw:
        return default
    if raw in {"0", "FALSE", "TIDAK", "NONAKTIF", "NON AKTIF", "TIDAK BERHAK", "UNPAID"}:
        return False
    return True


INDONESIAN_MONTHS = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}
LOCKED_PERIOD_STATUSES = {"LOCKED", "PAID", "HISTORICAL_LOCKED"}
INACTIVE_STATUSES = {"TIDAK AKTIF", "NONAKTIF", "NON AKTIF", "BERHENTI", "KELUAR", "MENINGGAL"}


class SharedMasterService:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._backfilling = False

    def connect(self):
        conn = duckdb.connect(self.db_path)
        with _SCHEMA_LOCK:
            if self.db_path not in _SCHEMA_READY:
                ensure_shared_master_schema(conn)
                _SCHEMA_READY.add(self.db_path)
        return conn

    @staticmethod
    def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
        mapping = _header_mapping(row)
        get = lambda field: row.get(mapping[field]) if field in mapping else None
        provided = set(mapping)
        normalized = {
            "nip": _text(get("nip")), "nama": _text(get("nama")),
            "jenis_sdm": _text(get("jenis_sdm")),
            "employment_status": _text(get("employment_status")) or "AKTIF",
            "jabatan": _text(get("jabatan")), "unit": _text(get("unit")),
            "kd_anak_satker": _text(get("kd_anak_satker")), "provinsi": _text(get("provinsi")),
            "kabupaten_kota": _text(get("kabupaten_kota")), "penilai": _text(get("penilai")),
            "kelas_jabatan": int(_number(get("kelas_jabatan"))) if _text(get("kelas_jabatan")) else None,
            "tukin_gross": _number(get("tukin_gross")),
            "eligible": _bool(get("eligible"), True),
            "eligibility_reason": _text(get("eligibility_reason")),
            "wilayah_3t": _text(get("wilayah_3t")).upper() in {"3T", "YA", "YES", "TRUE", "1"},
            "status_tendik": _text(get("status_tendik")), "jabatan_osdm": _text(get("jabatan_osdm")),
            "bank_name": _text(get("bank_name")), "account_number": _text(get("account_number")),
            "account_holder": _text(get("account_holder")), "_provided": provided,
        }
        return normalized

    @staticmethod
    def _status_is_active(value: Any) -> bool:
        return _text(value).upper() not in INACTIVE_STATUSES

    def _preview_token(self, rows: list[Mapping[str, Any]], effective_date: Any) -> str:
        """Bind confirmation to exact input, effective date, and current master versions."""
        effective = pd.to_datetime(effective_date or date.today(), errors="coerce")
        if pd.isna(effective):
            raise ValueError("Tanggal efektif tidak valid")
        normalized_rows = [
            {str(key): _text(value) for key, value in sorted(dict(row).items(), key=lambda item: str(item[0]))}
            for row in rows
        ]
        nips = sorted({self.normalize_row(row)["nip"] for row in rows if self.normalize_row(row)["nip"]})
        versions = []
        if nips:
            with self.connect() as conn:
                versions = conn.execute(
                    "SELECT nip,master_version,updated_at FROM sdm_master WHERE nip IN (SELECT * FROM UNNEST(?)) ORDER BY nip",
                    [nips],
                ).fetchall()
        payload = {
            "rows": normalized_rows,
            "effective_date": effective.date().isoformat(),
            "versions": [[_text(value) for value in row] for row in versions],
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _backfill_legacy_if_empty(self) -> None:
        if self._backfilling:
            return
        with self.connect() as conn:
            count = conn.execute("SELECT count(*) FROM sdm_master").fetchone()[0]
            if count:
                return
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info('master_data')").fetchall()}
                if not columns or columns == {"_empty"}:
                    return
                rows = conn.execute("SELECT * FROM master_data").fetchdf().to_dict("records")
            except Exception:
                return
        if rows:
            self._backfilling = True
            try:
                self.ingest_rows(rows, date.today(), "LEGACY_BACKFILL", "SYSTEM", confirmed=True, refresh_projection=False)
            finally:
                self._backfilling = False

    def preview(self, rows: list[Mapping[str, Any]], effective_date: Any = None) -> dict[str, Any]:
        if not self._backfilling:
            self._backfill_legacy_if_empty()
        normalized, rejected, duplicates, seen = [], [], 0, set()
        for index, source in enumerate(rows, 2):
            item = self.normalize_row(source)
            if not item["nip"]:
                rejected.append({"row": index, "reason": "NIP kosong"}); continue
            if item["nip"] in seen:
                duplicates += 1; rejected.append({"row": index, "nip": item["nip"], "reason": "NIP duplikat dalam file"}); continue
            seen.add(item["nip"]); item["source_row"] = index; normalized.append(item)
        nips = [item["nip"] for item in normalized]
        current: dict[str, dict[str, Any]] = {}
        if nips:
            with self.connect() as conn:
                frame = conn.execute(
                    "SELECT * FROM sdm_master WHERE nip IN (SELECT * FROM UNNEST(?))", [nips]
                ).fetchdf()
                current = {str(row["nip"]): row for row in frame.to_dict("records")}
                profiles = self._current_history_map(conn, "sdm_tukin_profile_history", nips)
                banks = self._current_history_map(conn, "sdm_bank_history", nips)
        else:
            profiles, banks = {}, {}
        results, new_count, changed_count, unchanged_count = [], 0, 0, 0
        for item in normalized:
            old = {**current.get(item["nip"], {}), **profiles.get(item["nip"], {}), **banks.get(item["nip"], {})}
            if not old:
                status, changes = "NEW", [field for field in IDENTITY_FIELDS + PROFILE_FIELDS + BANK_FIELDS if field in item["_provided"]]
                new_count += 1
            else:
                changes = []
                for field in IDENTITY_FIELDS + PROFILE_FIELDS + BANK_FIELDS:
                    if field not in item["_provided"]:
                        continue
                    new_value = item[field]
                    if isinstance(new_value, str) and not new_value:
                        continue
                    if self._comparable(old.get(field)) != self._comparable(new_value):
                        changes.append(field)
                status = "CHANGED" if changes else "UNCHANGED"
                changed_count += status == "CHANGED"; unchanged_count += status == "UNCHANGED"
            results.append({"row": item["source_row"], "nip": item["nip"], "nama": item["nama"],
                            "status": status, "changed_fields": changes})
        assignment = self._temporary_assignment_plan(rows, effective_date)
        return {"status": "ok", "row_count": len(rows), "valid_count": len(normalized),
                "new_count": new_count, "changed_count": changed_count,
                "unchanged_count": unchanged_count, "rejected_count": len(rejected),
                "duplicate_count": duplicates, "preview": results[:500], "rejections": rejected[:200],
                "preview_token": self._preview_token(rows, effective_date), **assignment}

    @staticmethod
    def _flag_columns(rows: list[Mapping[str, Any]], effective_date: Any = None) -> tuple[list[tuple[str, str]], list[str]]:
        if not rows:
            return [], []
        parsed = pd.to_datetime(effective_date or date.today(), errors="coerce")
        default_year = date.today().year if pd.isna(parsed) else parsed.year
        headers: list[str] = []
        seen_headers = set()
        for row in rows:
            for header in row.keys():
                text = str(header)
                if text not in seen_headers:
                    seen_headers.add(text); headers.append(text)
        columns, invalid = [], []
        month_names = "|".join(INDONESIAN_MONTHS)
        pattern = re.compile(rf"^flagtendik({month_names})(20\d{{2}})?$")
        seen_periods: dict[str, str] = {}
        for header in headers:
            cleaned = _clean(header)
            if not cleaned.startswith("flagtendik"):
                continue
            match = pattern.fullmatch(cleaned)
            if not match:
                invalid.append(header); continue
            year = int(match.group(2) or default_year)
            period = f"{year:04d}-{INDONESIAN_MONTHS[match.group(1)]:02d}"
            if period in seen_periods:
                invalid.append(header); invalid.append(seen_periods[period]); continue
            seen_periods[period] = header
            columns.append((period, header))
        unique_invalid = list(dict.fromkeys(invalid))
        return sorted(columns), unique_invalid

    @staticmethod
    def _latest_assignment(events: list[dict[str, Any]], period: str, inclusive: bool = True) -> Optional[dict[str, Any]]:
        eligible = [event for event in events if event["effective_month"] <= period if inclusive or event["effective_month"] < period]
        return max(eligible, key=lambda event: event["effective_month"], default=None)

    @staticmethod
    def _assignment_value(raw: Any) -> tuple[str, str]:
        text = _text(raw)
        if text == "-":
            return "SDM PKH", ""
        return "TENDIK", text

    @staticmethod
    def _locked_periods(conn) -> set[str]:
        try:
            return {str(row[0]) for row in conn.execute(
                "SELECT assessment_period FROM tukin_period WHERE status IN ('LOCKED','PAID','HISTORICAL_LOCKED')"
            ).fetchall()}
        except duckdb.CatalogException:
            return set()

    @staticmethod
    def _change_hits_locked_period(events: list[dict[str, Any]], period: str, locked_periods: set[str]) -> bool:
        later = sorted(event["effective_month"] for event in events if event["effective_month"] > period)
        next_period = later[0] if later else None
        return any(locked >= period and (next_period is None or locked < next_period) for locked in locked_periods)

    def _temporary_assignment_plan(self, rows: list[Mapping[str, Any]], effective_date: Any = None) -> dict[str, Any]:
        columns, invalid_headers = self._flag_columns(rows, effective_date)
        empty = {
            "assignment_column_count": len(columns), "assignment_invalid_headers": invalid_headers,
            "assignment_invalid_header_count": len(invalid_headers), "assignment_new_count": 0,
            "assignment_changed_count": 0, "assignment_unchanged_count": 0,
            "assignment_continued_count": 0, "assignment_rejected_count": 0,
            "assignment_preview": [], "assignment_rejections": [], "_assignment_events": [],
        }
        if not columns:
            return empty
        nips = list(dict.fromkeys(_text(row.get(_header_mapping(row).get("nip", ""))) for row in rows))
        nips = [nip for nip in nips if nip]
        with self.connect() as conn:
            existing_rows = conn.execute(
                "SELECT * FROM sdm_temporary_assignment_history WHERE nip IN (SELECT * FROM UNNEST(?)) ORDER BY nip,effective_month",
                [nips],
            ).fetchdf() if nips else pd.DataFrame()
            locked_periods = self._locked_periods(conn)
        by_nip: dict[str, list[dict[str, Any]]] = {nip: [] for nip in nips}
        for event in self._records(existing_rows):
            by_nip.setdefault(str(event["nip"]), []).append(event)
        preview, rejections, planned = [], [], []
        new_count = changed_count = unchanged_count = continued_count = 0
        seen_nips = set()
        for source_row, row in enumerate(rows, 2):
            mapping = _header_mapping(row); nip = _text(row.get(mapping.get("nip", "")))
            if not nip or nip in seen_nips:
                continue
            seen_nips.add(nip)
            events = [dict(event) for event in by_nip.get(nip, [])]
            for period, header in columns:
                raw = _text(row.get(header))
                current = self._latest_assignment(events, period)
                if not raw:
                    if current:
                        continued_count += 1
                    else:
                        rejections.append({"row": source_row, "nip": nip, "period": period,
                                           "reason": "FLAG bulan pertama kosong dan belum ada histori penugasan"})
                    continue
                assignment_type, detail = self._assignment_value(raw)
                exact = next((event for event in events if event["effective_month"] == period), None)
                previous = self._latest_assignment(events, period, inclusive=False)
                old_type = _text((exact or previous or {}).get("assignment_type"))
                old_detail = _text((exact or previous or {}).get("assignment_detail"))
                if old_type == assignment_type and old_detail == detail:
                    unchanged_count += 1
                    preview.append({"row": source_row, "nip": nip, "period": period, "source_value": raw,
                                    "previous_type": old_type, "assignment_type": assignment_type,
                                    "assignment_detail": detail, "status": "UNCHANGED"})
                    continue
                if self._change_hits_locked_period(events, period, locked_periods):
                    rejections.append({"row": source_row, "nip": nip, "period": period,
                                       "reason": "Perubahan memengaruhi periode Tukin yang sudah dikunci"})
                    continue
                action = "CHANGED" if (exact or previous) else "NEW"
                new_count += action == "NEW"; changed_count += action == "CHANGED"
                event = {"nip": nip, "effective_month": period, "assignment_type": assignment_type,
                         "assignment_detail": detail, "raw_value": raw, "source_row": source_row,
                         "previous_type": old_type, "previous_detail": old_detail, "status": action}
                planned.append(event)
                events = [item for item in events if item["effective_month"] != period] + [event]
                preview.append({"row": source_row, "nip": nip, "period": period, "source_value": raw,
                                "previous_type": old_type, "assignment_type": assignment_type,
                                "assignment_detail": detail, "status": action})
        return {**empty, "assignment_new_count": int(new_count), "assignment_changed_count": int(changed_count),
                "assignment_unchanged_count": int(unchanged_count), "assignment_continued_count": int(continued_count),
                "assignment_rejected_count": len(rejections), "assignment_preview": preview[:1000],
                "assignment_rejections": rejections[:500], "_assignment_events": planned}

    @staticmethod
    def _comparable(value: Any) -> Any:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return ""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 4)
        return _text(value).upper()

    @staticmethod
    def _current_history_map(conn, table: str, nips: list[str]) -> dict[str, dict[str, Any]]:
        if not nips:
            return {}
        frame = conn.execute(
            f"""SELECT * EXCLUDE(rn) FROM (
                    SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn
                    FROM {table} WHERE nip IN (SELECT * FROM UNNEST(?))
                      AND valid_to IS NULL
                  ) WHERE rn=1""", [nips]
        ).fetchdf()
        return {str(row["nip"]): row for row in frame.to_dict("records")}

    def ingest_rows(self, rows: list[Mapping[str, Any]], effective_date: Any = None,
                    source_file: str = "MANUAL", approved_by: str = "EVKIN",
                    confirmed: bool = False, refresh_projection: bool = True,
                    preview_token: str = "", require_preview_token: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("Perubahan master harus dipreview dan dikonfirmasi")
        effective = pd.to_datetime(effective_date or date.today(), errors="coerce")
        if pd.isna(effective):
            raise ValueError("Tanggal efektif tidak valid")
        effective = effective.date()
        if effective > date.today():
            raise ValueError("Tanggal efektif Master SDM tidak boleh di masa depan")
        preview = self.preview(rows, effective)
        if require_preview_token and (not preview_token or preview_token != preview.get("preview_token")):
            raise ValueError("Preview sudah tidak berlaku. Lakukan preview ulang sebelum menyimpan")
        if preview.get("rejected_count"):
            first = preview["rejections"][0]
            raise ValueError(f"Seluruh impor dibatalkan: baris {first.get('row')} ditolak ({first.get('reason')})")
        if preview.get("assignment_invalid_header_count"):
            raise ValueError("Header FLAG TENDIK tidak valid atau duplikat: " + ", ".join(preview["assignment_invalid_headers"]))
        if preview.get("assignment_rejected_count"):
            first = preview["assignment_rejections"][0]
            raise ValueError(f"Penugasan belum valid pada baris {first.get('row')}, periode {first.get('period')}: {first.get('reason')}")
        batch_id = str(uuid.uuid4())
        normalized, seen = [], set()
        for source_row, source in enumerate(rows, 2):
            item = self.normalize_row(source)
            if not item["nip"] or item["nip"] in seen:
                continue
            seen.add(item["nip"]); item["source_row"] = source_row; normalized.append(item)
        with self.connect() as conn:
            existing_versions = {str(nip): int(version or 1) for nip, version in conn.execute(
                "SELECT nip,master_version FROM sdm_master WHERE nip IN (SELECT * FROM UNNEST(?))",
                [[item["nip"] for item in normalized]],
            ).fetchall()} if normalized else {}
            conn.execute("BEGIN TRANSACTION")
            try:
                for item in normalized:
                    self._apply_row(conn, item, effective, source_file, approved_by, batch_id)
                assignment_nips = set()
                for event in preview.get("_assignment_events", []):
                    master = conn.execute("SELECT sdm_id FROM sdm_master WHERE nip=?", [event["nip"]]).fetchone()
                    if not master:
                        raise ValueError(f"NIP penugasan {event['nip']} tidak ditemukan pada Master SDM")
                    conn.execute("DELETE FROM sdm_temporary_assignment_history WHERE nip=? AND effective_month=?",
                                 [event["nip"], event["effective_month"]])
                    conn.execute(
                        """INSERT INTO sdm_temporary_assignment_history
                           (id,sdm_id,nip,effective_month,assignment_type,assignment_detail,raw_value,
                            source_file,source_row,change_reason,approved_by,import_batch_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [str(uuid.uuid4()), master[0], event["nip"], event["effective_month"],
                         event["assignment_type"], event["assignment_detail"], event["raw_value"],
                         source_file, event["source_row"], "Perubahan FLAG TENDIK bulanan", approved_by, batch_id],
                    )
                    old_value = " / ".join(filter(None, [event.get("previous_type"), event.get("previous_detail")]))
                    new_value = " / ".join(filter(None, [event["assignment_type"], event["assignment_detail"]]))
                    self._record_change(conn, batch_id, event["nip"], "temporary_assignment",
                                        old_value, new_value, date.fromisoformat(event["effective_month"] + "-01"),
                                        source_file, approved_by)
                    assignment_nips.add(event["nip"])
                for nip in assignment_nips:
                    if nip in existing_versions:
                        conn.execute("UPDATE sdm_master SET master_version=?,updated_at=now() WHERE nip=?",
                                     [existing_versions[nip] + 1, nip])
                conn.execute(
                    """INSERT INTO sdm_master_import_batch
                       (id,source_file,effective_date,row_count,new_count,changed_count,unchanged_count,rejected_count,status,approved_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [batch_id, source_file, effective, len(rows), preview["new_count"], preview["changed_count"],
                     preview["unchanged_count"], preview["rejected_count"], "COMPLETED", approved_by],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK"); raise
        projection_warning = ""
        if refresh_projection:
            try:
                self.refresh_legacy_projection()
            except Exception as exc:
                # The canonical transaction is already committed. A derived
                # compatibility projection must never make that write appear
                # rolled back to callers; it can be rebuilt on the next read.
                projection_warning = f"Projection master_data perlu dibangun ulang: {exc}"
        result = {key: value for key, value in preview.items() if not key.startswith("_")}
        return {**result, "batch_id": batch_id, "effective_date": effective.isoformat(),
                "committed": len(normalized), "assignment_committed": len(preview.get("_assignment_events", [])),
                "projection_status": "warning" if projection_warning else "ok",
                "projection_warning": projection_warning}

    def _apply_row(self, conn, item, effective: date, source_file: str, approved_by: str, batch_id: str) -> None:
        existing_row = conn.execute("SELECT * FROM sdm_master WHERE nip=?", [item["nip"]]).fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info('sdm_master')").fetchall()]
        existing = dict(zip(columns, existing_row)) if existing_row else {}
        if not existing:
            sdm_id = str(uuid.uuid4())
            initial_active = self._status_is_active(item["employment_status"])
            conn.execute(
                """INSERT INTO sdm_master
                   (sdm_id,nip,nama,jenis_sdm,employment_status,jabatan,unit,kd_anak_satker,
                    provinsi,kabupaten_kota,penilai,active,master_version,valid_from,source_file,source_row)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [sdm_id, item["nip"], item["nama"], item["jenis_sdm"], item["employment_status"],
                 item["jabatan"], item["unit"], item["kd_anak_satker"], item["provinsi"],
                 item["kabupaten_kota"], item["penilai"], initial_active, 1, effective, source_file, item["source_row"]],
            )
            self._insert_active_history(conn, sdm_id, item["nip"], initial_active, effective,
                                        "Status awal Master SDM", approved_by, source_file)
            self._insert_assignment(conn, sdm_id, item, effective, source_file, approved_by, "SDM baru")
            self._insert_profile(conn, sdm_id, item, effective, source_file, approved_by, "SDM baru")
            self._insert_bank(conn, sdm_id, item, effective, source_file, approved_by, "SDM baru")
            for field in IDENTITY_FIELDS + PROFILE_FIELDS + BANK_FIELDS:
                if field in item["_provided"] and self._comparable(item[field]) != "":
                    self._record_change(conn, batch_id, item["nip"], field, "", item[field], effective, source_file, approved_by)
            return

        sdm_id = str(existing["sdm_id"]); changes = []
        requested_inactive = "employment_status" in item["_provided"] and not self._status_is_active(item["employment_status"])
        requested_reactivation = ("employment_status" in item["_provided"] and
                                  self._status_is_active(item["employment_status"]) and not bool(existing.get("active")))
        if requested_reactivation:
            raise ValueError(f"NIP {item['nip']} nonaktif hanya dapat diaktifkan melalui aksi Aktifkan")
        for field in IDENTITY_FIELDS:
            if field not in item["_provided"] or (isinstance(item[field], str) and not item[field]):
                continue
            if self._comparable(existing.get(field)) != self._comparable(item[field]):
                changes.append(field)
                self._record_change(conn, batch_id, item["nip"], field, existing.get(field), item[field], effective, source_file, approved_by)
        if changes:
            assignments = set(changes) & set(ASSIGNMENT_FIELDS)
            if assignments:
                self._close_current(conn, "sdm_assignment_history", item["nip"], effective)
                merged = {**existing, **{field: item[field] for field in changes}}
                self._insert_assignment(conn, sdm_id, merged, effective, source_file, approved_by, "Perubahan " + ", ".join(sorted(assignments)))
            sets = ",".join(f'"{field}"=?' for field in changes)
            conn.execute(
                f"UPDATE sdm_master SET {sets},master_version=master_version+1,source_file=?,source_row=?,updated_at=now() WHERE nip=?",
                [item[field] for field in changes] + [source_file, item["source_row"], item["nip"]],
            )

        current_profile = self._current_history_map(conn, "sdm_tukin_profile_history", [item["nip"]]).get(item["nip"], {})
        profile_changes = self._changed_history_fields(item, current_profile, PROFILE_FIELDS)
        if profile_changes:
            self._close_current(conn, "sdm_tukin_profile_history", item["nip"], effective)
            merged = {**current_profile, **{field: item[field] for field in profile_changes}}
            self._insert_profile(conn, sdm_id, merged, effective, source_file, approved_by, "Perubahan " + ", ".join(profile_changes))
            for field in profile_changes:
                self._record_change(conn, batch_id, item["nip"], field, current_profile.get(field), item[field], effective, source_file, approved_by)
        current_bank = self._current_history_map(conn, "sdm_bank_history", [item["nip"]]).get(item["nip"], {})
        bank_changes = self._changed_history_fields(item, current_bank, BANK_FIELDS)
        if bank_changes:
            self._close_current(conn, "sdm_bank_history", item["nip"], effective)
            merged = {**current_bank, **{field: item[field] for field in bank_changes}}
            self._insert_bank(conn, sdm_id, merged, effective, source_file, approved_by, "Perubahan " + ", ".join(bank_changes))
            for field in bank_changes:
                self._record_change(conn, batch_id, item["nip"], field, current_bank.get(field), item[field], effective, source_file, approved_by)
        if (profile_changes or bank_changes) and not changes:
            conn.execute(
                "UPDATE sdm_master SET master_version=master_version+1,source_file=?,source_row=?,updated_at=now() WHERE nip=?",
                [source_file, item["source_row"], item["nip"]],
            )
        if requested_inactive and bool(existing.get("active")):
            self._set_active_in_transaction(conn, item["nip"], False, effective,
                                            "Status kepegawaian dari impor", approved_by, source_file, batch_id)

    @staticmethod
    def _insert_active_history(conn, sdm_id, nip, active, effective, reason, approved_by, source_file):
        conn.execute(
            """INSERT INTO sdm_active_history
               (id,sdm_id,nip,active,valid_from,reason,approved_by,source_file)
               VALUES (?,?,?,?,?,?,?,?)""",
            [str(uuid.uuid4()), sdm_id, nip, active, effective, reason, approved_by, source_file],
        )

    def _set_active_in_transaction(self, conn, nip, active, effective, reason, approved_by, source_file, batch_id):
        row = conn.execute("SELECT sdm_id,active FROM sdm_master WHERE nip=?", [nip]).fetchone()
        if not row:
            raise KeyError("SDM tidak ditemukan")
        current = conn.execute(
            "SELECT valid_from FROM sdm_active_history WHERE nip=? AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1",
            [nip],
        ).fetchone()
        if current and effective < current[0]:
            raise ValueError(f"Tanggal efektif {effective} lebih lama dari histori status aktif NIP {nip}")
        if current and effective == current[0]:
            conn.execute("DELETE FROM sdm_active_history WHERE nip=? AND valid_to IS NULL AND valid_from=?", [nip, effective])
        elif current:
            conn.execute("UPDATE sdm_active_history SET valid_to=? WHERE nip=? AND valid_to IS NULL", [effective - timedelta(days=1), nip])
        self._insert_active_history(conn, row[0], nip, active, effective, reason, approved_by, source_file)
        conn.execute(
            "UPDATE sdm_master SET active=?,employment_status=?,master_version=master_version+1,updated_at=now() WHERE nip=?",
            [active, "AKTIF" if active else "TIDAK AKTIF", nip],
        )
        self._record_change(conn, batch_id, nip, "active", row[1], active, effective, source_file, approved_by)
        conn.execute("UPDATE sdm_change_request SET reason=? WHERE nip=? AND batch_id=? AND field_name='active'", [reason, nip, batch_id])

    def _changed_history_fields(self, item, current, fields):
        changed = []
        for field in fields:
            if field not in item["_provided"]:
                continue
            value = item[field]
            if isinstance(value, str) and not value:
                continue
            if self._comparable(current.get(field)) != self._comparable(value):
                changed.append(field)
        return changed

    @staticmethod
    def _close_current(conn, table, nip, effective):
        current = conn.execute(f"SELECT valid_from FROM {table} WHERE nip=? AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1", [nip]).fetchone()
        if current and effective < current[0]:
            raise ValueError(f"Tanggal efektif {effective} lebih lama dari histori aktif NIP {nip}")
        if current and effective == current[0]:
            conn.execute(f"DELETE FROM {table} WHERE nip=? AND valid_to IS NULL AND valid_from=?", [nip, effective])
        else:
            conn.execute(f"UPDATE {table} SET valid_to=? WHERE nip=? AND valid_to IS NULL", [effective - timedelta(days=1), nip])

    @staticmethod
    def _insert_assignment(conn, sdm_id, item, effective, source_file, approved_by, reason):
        conn.execute(
            """INSERT INTO sdm_assignment_history
               (id,sdm_id,nip,jabatan,unit,kd_anak_satker,provinsi,kabupaten_kota,penilai,
                valid_from,source_file,change_reason,approved_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [str(uuid.uuid4()), sdm_id, item["nip"], item.get("jabatan"), item.get("unit"),
             item.get("kd_anak_satker"), item.get("provinsi"), item.get("kabupaten_kota"),
             item.get("penilai"), effective, source_file, reason, approved_by],
        )

    @staticmethod
    def _insert_profile(conn, sdm_id, item, effective, source_file, approved_by, reason):
        conn.execute(
            """INSERT INTO sdm_tukin_profile_history
               (id,sdm_id,nip,kelas_jabatan,tukin_gross,eligible,eligibility_reason,wilayah_3t,
                status_tendik,jabatan_osdm,valid_from,source_file,change_reason,approved_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [str(uuid.uuid4()), sdm_id, item["nip"], item.get("kelas_jabatan"), item.get("tukin_gross") or 0,
             item.get("eligible", True), item.get("eligibility_reason"), item.get("wilayah_3t", False),
             item.get("status_tendik"), item.get("jabatan_osdm"), effective, source_file, reason, approved_by],
        )

    @staticmethod
    def _insert_bank(conn, sdm_id, item, effective, source_file, approved_by, reason):
        conn.execute(
            """INSERT INTO sdm_bank_history
               (id,sdm_id,nip,bank_name,account_number,account_holder,valid_from,source_file,change_reason,approved_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [str(uuid.uuid4()), sdm_id, item["nip"], item.get("bank_name"), item.get("account_number"),
             item.get("account_holder"), effective, source_file, reason, approved_by],
        )

    @staticmethod
    def _record_change(conn, batch_id, nip, field, old, new, effective, source_file, approved_by):
        conn.execute(
            """INSERT INTO sdm_change_request
               (id,batch_id,nip,field_name,old_value,new_value,effective_date,status,source_file,reviewed_by,reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,now())""",
            [str(uuid.uuid4()), batch_id, nip, field, _text(old), _text(new), effective,
             "APPROVED", source_file, approved_by],
        )

    def refresh_legacy_projection(self) -> int:
        with self.connect() as conn:
            df = conn.execute(
                """WITH p AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_tukin_profile_history WHERE valid_from<=CURRENT_DATE AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)) WHERE rn=1
                   ), b AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_bank_history WHERE valid_from<=CURRENT_DATE AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)) WHERE rn=1
                   ), t AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY effective_month DESC,created_at DESC) rn FROM sdm_temporary_assignment_history WHERE effective_month<=strftime(CURRENT_DATE,'%Y-%m')) WHERE rn=1
                   )
                   SELECT m.nip,m.nama,m.provinsi,m.kabupaten_kota AS "kabupatenKota",
                          m.kd_anak_satker AS "kdAnakSatker",coalesce(t.assignment_detail,p.jabatan_osdm,'') AS "jabatanOsdm",
                          coalesce(t.assignment_type,p.status_tendik,'') AS "statusTendik",m.jabatan,
                          coalesce(p.tukin_gross,0) AS tukin,m.unit,m.penilai,m.jenis_sdm AS "jenisSdm",
                          m.employment_status AS "statusKepegawaian",p.kelas_jabatan AS "kelasJabatan",
                          p.wilayah_3t AS "wilayah3T",p.eligible AS "berhakTukin",
                          b.bank_name AS bank,b.account_number AS "noRekening",b.account_holder AS "namaPemilikRekening"
                   FROM sdm_master m LEFT JOIN p ON p.nip=m.nip LEFT JOIN b ON b.nip=m.nip LEFT JOIN t ON t.nip=m.nip
                   WHERE m.active=true ORDER BY m.nama,m.nip"""
            ).fetchdf()
            conn.execute('DROP TABLE IF EXISTS "master_data"')
            if df.empty:
                conn.execute('CREATE TABLE "master_data" (_empty TEXT)')
            else:
                conn.register("tmp_shared_projection", df)
                conn.execute('CREATE TABLE "master_data" AS SELECT * FROM tmp_shared_projection')
                conn.unregister("tmp_shared_projection")
        return len(df)

    def list(self, q: str = "", page: int = 1, per_page: int = 50, active: Optional[bool] = None,
             nips: Optional[list[str]] = None, jabatan: str = "", unit: str = "", sort: str = "") -> dict[str, Any]:
        self._backfill_legacy_if_empty()
        where, params = [], []
        if q:
            where.append("(lower(m.nip) LIKE ? OR lower(m.nama) LIKE ? OR lower(m.jabatan) LIKE ?)")
            params.extend([f"%{q.lower()}%"] * 3)
        if active is not None:
            where.append("m.active=?"); params.append(active)
        if nips is not None:
            where.append("m.nip IN (SELECT * FROM UNNEST(?))"); params.append(nips)
        for column, value in (("jabatan", jabatan), ("unit", unit)):
            if value:
                where.append(f"m.{column}=?"); params.append(value)
        sort_column = {"nip": "m.nip", "nama": "m.nama", "jabatan": "m.jabatan",
                       "tukin": "p.tukin_gross", "provinsi": "m.provinsi",
                       "kabupatenKota": "m.kabupaten_kota", "kdAnakSatker": "m.kd_anak_satker",
                       "jabatanOsdm": "jabatan_osdm", "statusTendik": "status_tendik"}.get(sort, "m.nama")
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT count(*) FROM sdm_master m{where_sql}", params).fetchone()[0]
            offset = (page - 1) * per_page
            df = conn.execute(
                f"""WITH p AS (
                       SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_tukin_profile_history WHERE valid_from<=CURRENT_DATE AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)) WHERE rn=1
                     ), b AS (
                       SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_bank_history WHERE valid_from<=CURRENT_DATE AND (valid_to IS NULL OR valid_to>=CURRENT_DATE)) WHERE rn=1
                     ), t AS (
                       SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY effective_month DESC,created_at DESC) rn FROM sdm_temporary_assignment_history WHERE effective_month<=strftime(CURRENT_DATE,'%Y-%m')) WHERE rn=1
                     )
                     SELECT m.*,p.kelas_jabatan,p.tukin_gross,p.eligible,p.eligibility_reason,p.wilayah_3t,
                            coalesce(t.assignment_type,p.status_tendik) AS status_tendik,
                            coalesce(t.assignment_detail,p.jabatan_osdm) AS jabatan_osdm,
                            t.assignment_type AS temporary_assignment_type,t.assignment_detail AS temporary_assignment_detail,
                            t.effective_month AS temporary_assignment_effective_month,
                            b.bank_name,b.account_number,b.account_holder
                     FROM sdm_master m LEFT JOIN p ON p.nip=m.nip LEFT JOIN b ON b.nip=m.nip LEFT JOIN t ON t.nip=m.nip
                     {where_sql} ORDER BY {sort_column},m.nip LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchdf()
            issues = conn.execute("SELECT count(*) FROM sdm_validation_issue WHERE status='OPEN'").fetchone()[0]
        return {"status": "ok", "rows": self._records(df), "total": int(total), "page": page,
                "per_page": per_page, "pages": max(1, math.ceil(total / per_page)), "open_issues": int(issues)}

    def detail(self, nip: str) -> dict[str, Any]:
        result = self.list(q=nip, page=1, per_page=100)
        person = next((row for row in result["rows"] if row["nip"] == nip), None)
        if not person:
            raise KeyError("SDM tidak ditemukan")
        with self.connect() as conn:
            assignment = conn.execute("SELECT * FROM sdm_assignment_history WHERE nip=? ORDER BY valid_from DESC", [nip]).fetchdf()
            profiles = conn.execute("SELECT * FROM sdm_tukin_profile_history WHERE nip=? ORDER BY valid_from DESC", [nip]).fetchdf()
            banks = conn.execute("SELECT * FROM sdm_bank_history WHERE nip=? ORDER BY valid_from DESC", [nip]).fetchdf()
            temporary_assignments = conn.execute(
                "SELECT * FROM sdm_temporary_assignment_history WHERE nip=? ORDER BY effective_month DESC", [nip]
            ).fetchdf()
            changes = conn.execute("SELECT * FROM sdm_change_request WHERE nip=? ORDER BY created_at DESC LIMIT 500", [nip]).fetchdf()
            try:
                snapshots = conn.execute(
                    """SELECT assessment_period,payment_period,nama,jabatan,unit,kelas_jabatan,tukin_gross,
                              temporary_assignment_type,temporary_assignment_detail,temporary_assignment_effective_month,
                              paid_amount,payment_status,bank_name,account_number,master_version,snapshot_at
                       FROM tukin_final_monthly WHERE nip=? ORDER BY assessment_period DESC""", [nip]
                ).fetchdf()
            except Exception:
                snapshots = pd.DataFrame()
        return {"status": "ok", "person": person, "assignments": self._records(assignment),
                "profiles": self._records(profiles), "banks": self._records(banks),
                "temporary_assignments": self._records(temporary_assignments),
                "changes": self._records(changes), "snapshots": self._records(snapshots)}

    def set_active(self, nip: str, active: bool, reason: str, approved_by: str, effective_date: Any = None) -> dict[str, Any]:
        effective = pd.to_datetime(effective_date or date.today(), errors="coerce")
        if pd.isna(effective) or effective.date() > date.today():
            raise ValueError("Tanggal efektif status tidak valid atau berada di masa depan")
        if not _text(reason):
            raise ValueError("Alasan perubahan status wajib diisi")
        if not _text(approved_by):
            raise ValueError("Identitas penyetuju wajib diisi")
        with self.connect() as conn:
            change_id = "MANUAL-" + str(uuid.uuid4())
            self._set_active_in_transaction(conn, nip, active, effective.date(), reason, approved_by, "MANUAL", change_id)
        self.refresh_legacy_projection()
        return {"status": "ok", "nip": nip, "active": active}

    def deactivate_all(self, reason: str, approved_by: str, effective_date: Any = None) -> dict[str, Any]:
        if not _text(reason) or not _text(approved_by):
            raise ValueError("Alasan dan identitas penyetuju wajib diisi")
        effective = pd.to_datetime(effective_date or date.today(), errors="coerce")
        if pd.isna(effective) or effective.date() > date.today():
            raise ValueError("Tanggal efektif reset tidak valid atau berada di masa depan")
        with self.connect() as conn:
            nips = [row[0] for row in conn.execute("SELECT nip FROM sdm_master WHERE active=true").fetchall()]
            conn.execute("BEGIN TRANSACTION")
            try:
                batch_id = "RESET-" + str(uuid.uuid4())
                for nip in nips:
                    self._set_active_in_transaction(conn, nip, False, effective.date(), reason, approved_by, "ADMIN_RESET", batch_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.refresh_legacy_projection()
        return {"status": "ok", "deactivated": len(nips), "recoverable": True}

    def reconcile_workflow(self) -> dict[str, Any]:
        self._backfill_legacy_if_empty()
        observations = []
        with self.connect() as conn:
            masters = {str(row[0]): _text(row[1]) for row in conn.execute("SELECT nip,nama FROM sdm_master").fetchall()}
            conn.execute("UPDATE sdm_validation_issue SET status='RESOLVED',resolved_by='SYSTEM',resolved_at=now() WHERE status='OPEN' AND source_table IN ('raw_import','hitung_harian','rekap_sdm','final_absensi','final_skp')")
            for table in ("raw_import", "hitung_harian", "rekap_sdm", "final_absensi", "final_skp"):
                try:
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
                    nip_col = next((c for c in columns if _clean(c) == "nip"), None)
                    nama_col = next((c for c in columns if _clean(c) in {"nama", "namasdm", "namapegawai"}), None)
                    if not nip_col:
                        continue
                    select = f'SELECT DISTINCT CAST("{nip_col}" AS VARCHAR) nip' + (f',CAST("{nama_col}" AS VARCHAR) nama' if nama_col else ",'' nama") + f' FROM "{table}"'
                    for nip, nama in conn.execute(select).fetchall():
                        observations.append((table, _text(nip), _text(nama)))
                except Exception:
                    continue
            seen = set(); created = 0
            for table, nip, nama in observations:
                key = (table, nip)
                if not nip or key in seen:
                    continue
                seen.add(key)
                if nip not in masters:
                    issue = ("UNKNOWN_NIP", "ERROR", nama, "", "NIP workflow tidak ditemukan pada Master SDM")
                elif nama and masters[nip] and _clean(nama) != _clean(masters[nip]):
                    issue = ("NAME_MISMATCH", "WARNING", nama, masters[nip], "Nama sumber berbeda dari Master SDM")
                else:
                    continue
                conn.execute(
                    """INSERT INTO sdm_validation_issue
                       (id,nip,issue_type,severity,source_table,source_value,master_value,details)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [str(uuid.uuid4()), nip, *issue[:2], table, issue[2], issue[3], issue[4]],
                ); created += 1
        return {"status": "ok", "observations": len(observations), "issues_created": created}

    def validation_issues(self, status: str = "OPEN", page: int = 1, per_page: int = 100) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT count(*) FROM sdm_validation_issue WHERE status=?", [status]).fetchone()[0]
            df = conn.execute("SELECT * FROM sdm_validation_issue WHERE status=? ORDER BY severity,created_at DESC LIMIT ? OFFSET ?",
                              [status, per_page, (page - 1) * per_page]).fetchdf()
        return {"status": "ok", "rows": self._records(df), "total": int(total),
                "page": page, "pages": max(1, math.ceil(total / per_page))}

    def snapshot_map(self, assessment_period: str, fallback_latest_profile: bool = False) -> dict[str, dict[str, Any]]:
        year, month = map(int, assessment_period.split("-")); as_of = date(year, month, monthrange(year, month)[1])
        self._backfill_legacy_if_empty()
        with self.connect() as conn:
            df = conn.execute(
                """WITH p AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_tukin_profile_history WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)) WHERE rn=1
                   ), b AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_bank_history WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)) WHERE rn=1
                   ), a AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn FROM sdm_assignment_history WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)) WHERE rn=1
                   ), t AS (
                     SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY nip ORDER BY effective_month DESC,created_at DESC) rn FROM sdm_temporary_assignment_history WHERE effective_month<=?) WHERE rn=1
                   ), v AS (
                     SELECT nip,count(DISTINCT batch_id) AS master_version
                     FROM sdm_change_request WHERE effective_date<=? GROUP BY nip
                   ), act AS (
                     SELECT * EXCLUDE(rn) FROM (
                       SELECT *,row_number() OVER(PARTITION BY nip ORDER BY valid_from DESC,created_at DESC) rn
                       FROM sdm_active_history WHERE valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
                     ) WHERE rn=1
                   )
                   SELECT m.sdm_id,m.nip,m.nama,coalesce(a.jabatan,m.jabatan) AS jabatan,
                          coalesce(a.unit,m.unit) AS unit,coalesce(a.kd_anak_satker,m.kd_anak_satker) AS kd_anak_satker,
                          coalesce(a.provinsi,m.provinsi) AS provinsi,coalesce(a.kabupaten_kota,m.kabupaten_kota) AS kabupaten_kota,
                          coalesce(a.penilai,m.penilai) AS penilai,
                           CASE WHEN coalesce(act.active,true)=false THEN 'TIDAK AKTIF' ELSE 'AKTIF' END AS employment_status,
                          coalesce(v.master_version,1) AS master_version,p.kelas_jabatan,p.tukin_gross,
                           CASE WHEN coalesce(act.active,true)=false THEN false ELSE p.eligible END AS eligible,
                          p.wilayah_3t,coalesce(t.assignment_type,p.status_tendik) AS status_tendik,
                          coalesce(t.assignment_detail,p.jabatan_osdm) AS jabatan_osdm,
                          t.assignment_type AS temporary_assignment_type,
                          t.assignment_detail AS temporary_assignment_detail,
                          t.effective_month AS temporary_assignment_effective_month,
                          b.bank_name,b.account_number,b.account_holder
                   FROM sdm_master m LEFT JOIN p ON p.nip=m.nip LEFT JOIN b ON b.nip=m.nip
                   LEFT JOIN a ON a.nip=m.nip LEFT JOIN t ON t.nip=m.nip
                   LEFT JOIN v ON v.nip=m.nip LEFT JOIN act ON act.nip=m.nip""",
                 [as_of, as_of, as_of, as_of, as_of, as_of, assessment_period, as_of, as_of, as_of],
            ).fetchdf()
        records = self._records(df)
        if fallback_latest_profile:
            with self.connect() as conn:
                latest = conn.execute(
                    """SELECT * EXCLUDE(rn) FROM (
                           SELECT *,row_number() OVER(
                               PARTITION BY nip ORDER BY valid_from DESC,created_at DESC
                           ) rn
                           FROM sdm_tukin_profile_history
                       ) WHERE rn=1"""
                ).fetchdf()
            latest_by_nip = {str(row["nip"]): row for row in self._records(latest)}
            for row in records:
                profile = latest_by_nip.get(str(row["nip"]))
                if not profile:
                    continue
                if row.get("tukin_gross") is None:
                    row["tukin_gross"] = profile.get("tukin_gross")
                if row.get("eligible") is None:
                    row["eligible"] = profile.get("eligible")
                if row.get("kelas_jabatan") is None:
                    row["kelas_jabatan"] = profile.get("kelas_jabatan")
                if row.get("wilayah_3t") is None:
                    row["wilayah_3t"] = profile.get("wilayah_3t")
                if not row.get("status_tendik"):
                    row["status_tendik"] = profile.get("status_tendik")
                if not row.get("jabatan_osdm"):
                    row["jabatan_osdm"] = profile.get("jabatan_osdm")
                row["_profile_source"] = "MASTER SDM TERKINI"
        return {str(row["nip"]): row for row in records}

    @staticmethod
    def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
        if df.empty:
            return []
        clean = df.astype(object).where(pd.notnull(df), None)
        records = clean.to_dict("records")
        for record in records:
            for key, value in list(record.items()):
                if isinstance(value, (date, datetime, pd.Timestamp)):
                    record[key] = value.isoformat()
                elif hasattr(value, "item"):
                    try: record[key] = value.item()
                    except Exception: pass
        return records


def create_router(db_path_getter: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/api/shared-master", tags=["shared-sdm-master"])
    svc = lambda: SharedMasterService(db_path_getter())

    @router.get("")
    def list_master(q: str = "", page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500), active: Optional[bool] = None):
        return svc().list(q, page, per_page, active)

    @router.post("/lookup-tukin")
    def lookup_tukin(payload: dict[str, Any]):
        requested = payload.get("nips")
        if not isinstance(requested, list) or any(not isinstance(nip, str) for nip in requested):
            raise HTTPException(400, "nips harus berupa daftar teks NIP")
        nips = list(dict.fromkeys(nip.strip() for nip in requested if nip.strip()))
        if not nips:
            return {"status": "ok", "rows": [], "missing_nips": []}
        result = svc().list(nips=nips, per_page=len(nips), active=True)
        rows = [{"nip": row["nip"], "nama": row.get("nama"), "jabatan": row.get("jabatan"),
                 "unit": row.get("unit"), "tukin": row.get("tukin_gross"),
                 "master_version": row.get("master_version")} for row in result["rows"]]
        found = {row["nip"] for row in rows}
        return {"status": "ok", "rows": rows, "missing_nips": [nip for nip in nips if nip not in found]}

    @router.post("/preview")
    def preview(payload: dict[str, Any]):
        rows = payload.get("rows") or []
        if not isinstance(rows, list): raise HTTPException(400, "rows harus berupa list")
        try: return svc().preview(rows, payload.get("effective_date"))
        except ValueError as exc: raise HTTPException(400, str(exc)) from exc

    @router.post("/commit")
    def commit(payload: dict[str, Any]):
        rows = payload.get("rows") or []
        approved_by = _text(payload.get("approved_by"))
        if not approved_by: raise HTTPException(400, "Identitas penyetuju wajib diisi")
        try:
            return svc().ingest_rows(rows, payload.get("effective_date"), _text(payload.get("source_file")) or "IMPORT",
                                     approved_by, bool(payload.get("confirmed")), preview_token=_text(payload.get("preview_token")),
                                     require_preview_token=True)
        except ValueError as exc: raise HTTPException(400, str(exc)) from exc

    @router.post("/reconcile")
    def reconcile(): return svc().reconcile_workflow()

    @router.get("/validation")
    def validation(status: str = "OPEN", page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=500)):
        return svc().validation_issues(status.upper(), page, per_page)

    @router.post("/{nip}/active")
    def active_status(nip: str, payload: dict[str, Any]):
        try: return svc().set_active(nip, bool(payload.get("active")), _text(payload.get("reason")),
                                     _text(payload.get("approved_by")), payload.get("effective_date"))
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc
        except ValueError as exc: raise HTTPException(400, str(exc)) from exc

    @router.post("/admin/deactivate-all")
    def deactivate_all(payload: dict[str, Any]):
        if payload.get("confirmation") != "NONAKTIFKAN SEMUA SDM":
            raise HTTPException(400, "Konfirmasi administratif tidak sesuai")
        try:
            return svc().deactivate_all(_text(payload.get("reason")), _text(payload.get("approved_by")),
                                        payload.get("effective_date"))
        except ValueError as exc: raise HTTPException(400, str(exc)) from exc

    @router.get("/{nip}")
    def detail(nip: str):
        try: return svc().detail(nip)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    return router

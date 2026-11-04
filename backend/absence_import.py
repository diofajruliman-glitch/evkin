"""Backend Import Engine V1 for EVKIN Absensi.

The module is deliberately separate from the frozen calculation engine.  It
keeps uploads and validation in an import-scoped staging area and only promotes
one explicitly selected period to ``raw_import`` inside a transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import duckdb
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from backend.absence_shadow_engine import (
    detect_import_columns,
    detect_zone,
    fmt_time,
    normalize_excel_time,
    normalize_raw_row,
    parse_date,
    parse_time,
)
from backend.runtime_guard import (
    DATABASE_GATE,
    assert_current_generation,
    begin_runtime_reset,
    current_reset_generation,
    finish_runtime_reset,
)


IMPORT_ENGINE_VERSION = "ABSENCE_IMPORT_BACKEND_V1_2026_09"
CSV_EABSENSI_ENGINE_VERSION = "CSV_EABSENSI_SPECIAL_2026_09_V1"
CSV_EABSENSI_SOURCE_FORMAT = "csv_eabsensi"
# Evidence-based default: the existing import contract permits an 18-hour
# session.  A night check-in starts at 20:00, therefore its latest possible
# following checkout is 14:00 the next day.  Keep this centralized.
CSV_EABSENSI_NIGHT_IN_START_MIN = 20 * 60
CSV_EABSENSI_MAX_SESSION_MIN = 18 * 60
CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN = 14 * 60
DEFAULT_WALI_IMPORT_MAX_ROWS = 500_000
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TERMINAL_STATUSES = {
    "READY", "COMMITTED", "ALREADY_COMMITTED", "RESET", "FAILED",
    "STALE_GENERATION", "STALE_SOURCE", "CANCELLED",
}
ATTENDANCE_DERIVED_TABLES = (
    "workflow_records", "hitung_harian", "anomali", "rekap_sdm", "final_absensi",
    "final_skp", "final_kombinasi", "final_combined_skp",
)
ACTIVE_STATUSES = {"UPLOADING", "STAGED", "VALIDATING", "NORMALIZING", "COMMITTING"}
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evkin-import")
_TEMP_DIRECTORY_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


class ImportAuthority(str, Enum):
    IMPORT_JS_LEGACY = "IMPORT_JS_LEGACY"
    IMPORT_BACKEND_V1 = "IMPORT_BACKEND_V1"
    CSV_EABSENSI_SPECIAL = "CSV_EABSENSI_SPECIAL"


@dataclass(frozen=True)
class ImportAuthorityConfig:
    authority: ImportAuthority
    csv_eabsensi_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "ImportAuthorityConfig":
        raw = str(os.environ.get("EVKIN_IMPORT_AUTHORITY", ImportAuthority.IMPORT_JS_LEGACY.value)).strip().upper()
        try:
            authority = ImportAuthority(raw)
        except ValueError as exc:
            raise ImportEngineError("INVALID_IMPORT_AUTHORITY", f"Authority import tidak dikenal: {raw}") from exc
        enabled = str(os.environ.get("EVKIN_CSV_EABSENSI_SPECIAL_ENABLED", "false")).strip().lower()
        return cls(authority=authority, csv_eabsensi_enabled=enabled in {"1", "true", "yes", "on"})

    def route(self, source_format: str) -> ImportAuthority:
        """Resolve the explicit engine without any silent fallback."""
        normalized = str(source_format or "").strip().lower()
        if normalized == "wali":
            return ImportAuthority.IMPORT_JS_LEGACY
        if normalized == "standard":
            return self.authority
        if normalized == CSV_EABSENSI_SOURCE_FORMAT:
            if not self.csv_eabsensi_enabled:
                raise ImportEngineError(
                    "CSV_EABSENSI_SPECIAL_NOT_ENABLED",
                    "CSV eAbsensi Special belum diaktifkan pada environment ini.",
                    409,
                )
            return ImportAuthority.CSV_EABSENSI_SPECIAL
        raise ImportEngineError(
            "INVALID_SOURCE_FORMAT",
            "source_format harus standard, wali, atau csv_eabsensi",
        )


class ImportEngineError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0])


def _legacy_raw_import_periods(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Return verified periods from a pre-Backend-Import raw table."""
    headers = [row[0] for row in conn.execute('DESCRIBE "raw_import"').fetchall()]
    mapping = detect_import_columns(headers)
    date_key = "tanggal" if "tanggal" in mapping else "tanggalPulang" if "tanggalPulang" in mapping else None
    if date_key is None:
        raise ImportEngineError(
            "RAW_IMPORT_SCHEMA_CONFLICT",
            "raw_import legacy tidak mempunyai kolom tanggal yang dapat diverifikasi.",
        )
    column = headers[mapping[date_key]].replace('"', '""')
    periods: set[str] = set()
    cursor = conn.execute(f'SELECT "{column}" FROM "raw_import"')
    while True:
        rows = cursor.fetchmany(10_000)
        if not rows:
            break
        for (value,) in rows:
            if value is None or str(value).strip() == "":
                continue
            parsed = parse_date(value)
            if not parsed:
                raise ImportEngineError(
                    "RAW_IMPORT_SCHEMA_CONFLICT",
                    "raw_import legacy berisi tanggal yang tidak dapat diverifikasi sebelum migrasi canary.",
                )
            periods.add(str(parsed["monthKey"]))
    return periods


def ensure_absence_import_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS absence_import_batch (
            import_id VARCHAR PRIMARY KEY,
            filename VARCHAR NOT NULL,
            file_type VARCHAR NOT NULL,
            file_size BIGINT NOT NULL,
            file_sha256 VARCHAR NOT NULL,
            schema_fingerprint VARCHAR,
            period VARCHAR,
            periods_json VARCHAR,
            source_format VARCHAR NOT NULL,
            operator VARCHAR NOT NULL,
            reset_generation BIGINT NOT NULL,
            engine_version VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            upload_path VARCHAR,
            workflow_config_json VARCHAR,
            pair_cross_day BOOLEAN DEFAULT false,
            total_rows BIGINT DEFAULT 0,
            staged_rows BIGINT DEFAULT 0,
            normalized_rows BIGINT DEFAULT 0,
            valid_rows BIGINT DEFAULT 0,
            invalid_rows BIGINT DEFAULT 0,
            duplicate_rows BIGINT DEFAULT 0,
            missing_nip_rows BIGINT DEFAULT 0,
            invalid_nip_rows BIGINT DEFAULT 0,
            invalid_date_rows BIGINT DEFAULT 0,
            invalid_time_rows BIGINT DEFAULT 0,
            cross_day_rows BIGINT DEFAULT 0,
            wali_rows BIGINT DEFAULT 0,
            wib_rows BIGINT DEFAULT 0,
            wita_rows BIGINT DEFAULT 0,
            wit_rows BIGINT DEFAULT 0,
            progress DOUBLE DEFAULT 0,
            duplicate_of_import_id VARCHAR,
            predecessor_import_id VARCHAR,
            error_code VARCHAR,
            error_message VARCHAR,
            timing_json VARCHAR,
            created_at TIMESTAMP DEFAULT now(),
            staged_at TIMESTAMP,
            validated_at TIMESTAMP,
            completed_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    conn.execute("ALTER TABLE absence_import_batch ADD COLUMN IF NOT EXISTS pair_cross_day BOOLEAN DEFAULT false")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS absence_import_staging (
            import_id VARCHAR NOT NULL,
            source_row BIGINT NOT NULL,
            raw_json VARCHAR NOT NULL,
            raw_fingerprint VARCHAR NOT NULL,
            nip VARCHAR,
            nama VARCHAR,
            jabatan VARCHAR,
            tanggal VARCHAR,
            tanggal_pulang VARCHAR,
            jam_masuk VARCHAR,
            jam_pulang VARCHAR,
            masuk_min INTEGER,
            pulang_min INTEGER,
            masuk_norm_min INTEGER,
            pulang_norm_min INTEGER,
            masuk_norm_min_add INTEGER,
            pulang_norm_min_add INTEGER,
            zona VARCHAR,
            provinsi VARCHAR,
            kabupaten VARCHAR,
            jenis VARCHAR,
            source_format VARCHAR,
            period VARCHAR,
            cross_day BOOLEAN DEFAULT false,
            duplicate_row BOOLEAN DEFAULT false,
            valid BOOLEAN DEFAULT false,
            severity VARCHAR,
            error_codes VARCHAR,
            normalized_json VARCHAR,
            PRIMARY KEY(import_id, source_row)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_abs_import_batch_status ON absence_import_batch(status,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_abs_import_stage_period ON absence_import_staging(import_id,period)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_abs_import_stage_key ON absence_import_staging(import_id,nip,tanggal)")


def recover_interrupted_imports(db_path: str | Path) -> int:
    with duckdb.connect(str(db_path)) as conn:
        ensure_absence_import_schema(conn)
        count = conn.execute(
            "SELECT count(*) FROM absence_import_batch WHERE status IN ('UPLOADING','VALIDATING','NORMALIZING','COMMITTING')"
        ).fetchone()[0]
        conn.execute("""
            UPDATE absence_import_batch SET status='FAILED',error_code='PROCESS_RESTARTED',
              error_message='Backend restart ketika import belum mencapai status terminal.',
              completed_at=now(),updated_at=now()
            WHERE status IN ('UPLOADING','VALIDATING','NORMALIZING','COMMITTING')
        """)
        return int(count)


def _header_norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _validate_headers(headers: list[str], source_format: str) -> dict[str, int]:
    mapping = detect_import_columns(headers)
    if source_format == CSV_EABSENSI_SOURCE_FORMAT:
        by_norm = {_header_norm(name): index for index, name in enumerate(headers)}
        required = ("nip", "tglfinger", "absenpagi", "absensore", "jnshadir")
        missing = [name for name in required if name not in by_norm]
        if missing:
            raise ImportEngineError(
                "REQUIRED_COLUMN_MISSING",
                "Kolom wajib CSV eAbsensi tidak ditemukan: " + ", ".join(missing)
                + ". NM_JABATAN tidak diperlukan. Kolom terbaca: " + ", ".join(headers),
            )
        return by_norm
    if source_format == "wali":
        by_norm = {_header_norm(name): index for index, name in enumerate(headers)}
        required = ("tglfinger", "nmpeg", "nip", "jnshadir", "nmjabatan", "absenpagi", "absensore")
        missing = [name for name in required if name not in by_norm]
    else:
        normalized_headers = {_header_norm(name) for name in headers}
        eabsensi_signature = {"tglfinger", "absenpagi", "absensore", "jnshadir"}
        if eabsensi_signature.issubset(normalized_headers):
            raise ImportEngineError(
                "CSV_EABSENSI_SPECIAL_REQUIRED",
                "File ini berformat CSV eAbsensi; gunakan jalur Import CSV eAbsensi khusus.",
            )
        missing = [name for name in ("nip", "nama") if name not in mapping]
        if "tanggal" not in mapping and "tanggalPulang" not in mapping:
            missing.append("tanggal")
        if "jamMasuk" not in mapping and "jamPulang" not in mapping:
            missing.append("jamMasuk/jamPulang")
    if missing:
        raise ImportEngineError(
            "REQUIRED_COLUMN_MISSING",
            "Kolom wajib tidak ditemukan: " + ", ".join(missing) + ". Kolom terbaca: " + ", ".join(headers),
        )
    return mapping


def _nonblank_rows(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for row in rows:
        if any(value is not None and str(value).strip() for value in row.values()):
            yield row


def _arrow_rows(reader: Any) -> Iterator[dict[str, Any]]:
    for batch in reader:
        names = batch.schema.names
        columns = [batch.column(index).to_pylist() for index in range(len(names))]
        for offset in range(batch.num_rows):
            yield {name: columns[index][offset] for index, name in enumerate(names)}


def _col_idx_from_ref(ref: str) -> int:
    idx = 0
    for ch in ref:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - 64)
        else:
            break
    return idx - 1


def _estimate_xlsx_row_count(path: Path) -> int | None:
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                    with z.open(name) as f:
                        head = f.read(16384).decode("utf-8", errors="ignore")
                        m = re.search(r'<dimension ref="([^"]+)"', head)
                        if m:
                            parts = m.group(1).split(":")
                            if len(parts) == 2:
                                row_str = re.sub(r"^[A-Za-z]+", "", parts[1])
                                if row_str.isdigit():
                                    return int(row_str)
                    break
    except Exception:
        pass
    return None


def _fast_xlsx_file_rows(path: Path) -> tuple[list[str], Iterator[dict[str, Any]]]:
    import zipfile
    from lxml import etree

    zf = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        tree = etree.fromstring(zf.read("xl/sharedStrings.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for si in tree.findall("s:si", ns):
            t = si.find("s:t", ns)
            if t is not None:
                shared.append(t.text or "")
            else:
                shared.append("".join(r.findtext("s:t", "", ns) for r in si.findall("s:r", ns)))

    sheet_name = None
    for name in zf.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            sheet_name = name
            break
    if not sheet_name:
        zf.close()
        raise ImportEngineError("EMPTY_FILE", "Workbook tidak mempunyai worksheet.")

    f = zf.open(sheet_name)
    headers: list[str] | None = None
    row_iter = etree.iterparse(f, tag="{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row")

    for event, elem in row_iter:
        cells: list[Any] = [None] * (len(headers) if headers else 30)
        for c in elem:
            r = c.get("r")
            if not r:
                continue
            if len(r) >= 2 and "A" <= r[1] <= "Z":
                if len(r) >= 3 and "A" <= r[2] <= "Z":
                    ci = (ord(r[0]) - 64) * 676 + (ord(r[1]) - 64) * 26 + (ord(r[2]) - 65)
                else:
                    ci = (ord(r[0]) - 64) * 26 + (ord(r[1]) - 65)
            else:
                ci = ord(r[0]) - 65
            if ci >= len(cells):
                cells.extend([None] * (ci - len(cells) + 1))
            t = c.get("t")
            v = None
            for child in c:
                if child.tag.endswith("v"):
                    v = child.text
                    break
                elif child.tag.endswith("is"):
                    v = child.findtext("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t", "")
                    break
            if t == "s" and v and v.isdigit() and int(v) < len(shared):
                val = shared[int(v)]
            elif t == "b":
                val = v == "1"
            else:
                val = v if v != "" else None
            cells[ci] = val
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]
        if headers is None:
            if any(c is not None and str(c).strip() for c in cells):
                while cells and cells[-1] is None:
                    cells.pop()
                headers = [str(x or "").strip() or f"Kolom{i+1}" for i, x in enumerate(cells)]
                break

    if not headers:
        f.close()
        zf.close()
        raise ImportEngineError("EMPTY_FILE", "Workbook tidak mempunyai header.")

    def generate() -> Iterator[dict[str, Any]]:
        nonlocal headers
        header_len = len(headers)
        try:
            for event, elem in row_iter:
                cells: list[Any] = [None] * header_len
                has_val = False
                for c in elem:
                    r = c.get("r")
                    if not r:
                        continue
                    if len(r) >= 2 and "A" <= r[1] <= "Z":
                        if len(r) >= 3 and "A" <= r[2] <= "Z":
                            ci = (ord(r[0]) - 64) * 676 + (ord(r[1]) - 64) * 26 + (ord(r[2]) - 65)
                        else:
                            ci = (ord(r[0]) - 64) * 26 + (ord(r[1]) - 65)
                    else:
                        ci = ord(r[0]) - 65
                    if ci < header_len:
                        t = c.get("t")
                        v = None
                        for child in c:
                            if child.tag.endswith("v"):
                                v = child.text
                                break
                            elif child.tag.endswith("is"):
                                v = child.findtext("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t", "")
                                break
                        if t == "s" and v and v.isdigit() and int(v) < len(shared):
                            val = shared[int(v)]
                        elif t == "b":
                            val = v == "1"
                        else:
                            val = v if v != "" else None
                        cells[ci] = val
                        if val is not None and str(val).strip():
                            has_val = True
                elem.clear()
                parent = elem.getparent()
                if parent is not None:
                    while elem.getprevious() is not None:
                        del parent[0]
                if has_val:
                    yield {headers[i]: cells[i] for i in range(header_len)}
        finally:
            f.close()
            zf.close()

    return headers, generate()


def _file_rows(path: Path, file_type: str) -> tuple[list[str], Iterator[dict[str, Any]]]:
    if file_type in {"csv", "parquet"}:
        scan = "read_csv(?, header=true, auto_detect=true, all_varchar=true, sample_size=-1)" if file_type == "csv" else "read_parquet(?)"
        conn = duckdb.connect()
        conn.execute("SET memory_limit='128MB'")
        conn.execute("SET threads=2")
        cursor = conn.execute(f"SELECT * FROM {scan}", [str(path)])
        headers = [item[0] for item in cursor.description]
        reader = cursor.to_arrow_reader(batch_size=8192)

        def generate() -> Iterator[dict[str, Any]]:
            try:
                yield from _nonblank_rows(_arrow_rows(reader))
            finally:
                conn.close()

        return headers, generate()
    if file_type == "xlsx":
        try:
            return _fast_xlsx_file_rows(path)
        except Exception as exc:
            _LOGGER.warning("Fast streaming XLSX reader gagal (%s); fallback ke openpyxl.", exc)
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook[workbook.sheetnames[0]]
            iterator = sheet.iter_rows(values_only=True)
            first = next(iterator, None)
            if not first:
                workbook.close()
                raise ImportEngineError("EMPTY_FILE", "Workbook tidak mempunyai header.")
            headers = [str(value or "").strip() or f"Kolom{index + 1}" for index, value in enumerate(first)]

            def generate() -> Iterator[dict[str, Any]]:
                try:
                    for values in iterator:
                        row = {name: values[index] if index < len(values) else None for index, name in enumerate(headers)}
                        if any(value is not None and str(value).strip() for value in row.values()):
                            yield row
                finally:
                    workbook.close()

            return headers, generate()
    raise ImportEngineError("UNSUPPORTED_FILE", f"Format file tidak didukung: {file_type}")


def _raw_hash(row: dict[str, Any], headers: list[str]) -> str:
    material = "\x1f".join("" if row.get(name) is None else str(row.get(name)).strip() for name in headers)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _date_key(value: Any) -> str | None:
    parsed = parse_date(value)
    return parsed["key"] if parsed else None


def _canonical_stage_row(
    import_id: str,
    source_row: int,
    row: dict[str, Any],
    headers: list[str],
    mapping: dict[str, int],
    source_format: str,
) -> tuple[Any, ...]:
    normalized = normalize_raw_row(row, headers, mapping, f"imp-{source_row:012d}")
    row_json = _json(row)
    raw_hash = hashlib.sha256(row_json.encode("utf-8")).hexdigest()

    def get(field: str) -> Any:
        idx = mapping.get(field)
        if idx is not None and idx < len(headers):
            return row.get(headers[idx])
        return ""

    nip_raw = str(get("nip") or "").strip()
    date = parse_date(get("tanggal")) or parse_date(get("tanggalPulang"))
    in_raw = get("jamMasuk")
    out_raw = get("jamPulang")
    in_time = parse_time(in_raw)
    out_time = parse_time(out_raw)
    errors: list[str] = []
    warnings: list[str] = []
    if not nip_raw:
        errors.append("MISSING_NIP")
    elif not re.fullmatch(r"[A-Za-z0-9._/-]+", nip_raw):
        # Legacy accepts every non-blank NIP. Keep it promotable but visible.
        warnings.append("INVALID_NIP")
    if not date:
        errors.append("INVALID_DATE")
    if in_raw is not None and str(in_raw).strip() and in_time is None:
        errors.append("INVALID_CHECK_IN")
    if out_raw is not None and str(out_raw).strip() and out_time is None:
        errors.append("INVALID_CHECK_OUT")
    if in_time is None:
        warnings.append("MISSING_CHECK_IN")
    if out_time is None:
        warnings.append("MISSING_CHECK_OUT")
    for code in (row.get("_wali_error_codes") or []) + (row.get("_import_error_codes") or []):
        if code not in errors:
            errors.append(code)
    if normalized is None:
        normalized = {}
    issues = errors + warnings
    severity = "BLOCKER" if errors else ("WARNING" if warnings else "ACCEPTED_LEGACY")
    date_key = date["key"] if date else None
    return (
        import_id, source_row, row_json, raw_hash,
        normalized.get("nip") or nip_raw or None,
        normalized.get("nama") or str(get("nama") or "").strip(),
        normalized.get("jabatan") or str(get("jabatan") or "").strip(),
        date_key,
        normalized.get("tanggalPulang") or "",
        normalize_excel_time(in_raw), normalize_excel_time(out_raw),
        normalized.get("masukMin"), normalized.get("pulangMin"),
        normalized.get("masukNormMin"), normalized.get("pulangNormMin"),
        normalized.get("masukNormMinAdd"), normalized.get("pulangNormMinAdd"),
        normalized.get("zona") or detect_zone(get("provinsi"), get("zona")),
        normalized.get("provinsi") or str(get("provinsi") or "").strip(),
        normalized.get("kabupaten") or str(get("kabupaten") or "").strip(),
        normalized.get("jenis") or str(get("jenis") or "").strip(),
        source_format, date_key[:7] if date_key else None,
        bool(normalized.get("crossDay")), False, not errors, severity,
        "[]" if not issues else _json(issues),
        # Normalized values already have typed columns beside raw_json.  Do not
        # duplicate every row as a second large JSON object.
        None,
    )


STAGE_COLUMNS = (
    "import_id", "source_row", "raw_json", "raw_fingerprint", "nip", "nama", "jabatan",
    "tanggal", "tanggal_pulang", "jam_masuk", "jam_pulang", "masuk_min", "pulang_min",
    "masuk_norm_min", "pulang_norm_min", "masuk_norm_min_add", "pulang_norm_min_add", "zona",
    "provinsi", "kabupaten", "jenis", "source_format", "period", "cross_day", "duplicate_row",
    "valid", "severity", "error_codes", "normalized_json",
)


import pyarrow as pa

STAGE_ARROW_TYPES = [
    pa.string(), pa.int64(), pa.string(), pa.string(), pa.string(),
    pa.string(), pa.string(), pa.string(), pa.string(), pa.string(),
    pa.string(), pa.int32(), pa.int32(), pa.int32(), pa.int32(),
    pa.int32(), pa.int32(), pa.string(), pa.string(), pa.string(),
    pa.string(), pa.string(), pa.string(), pa.bool_(), pa.bool_(),
    pa.bool_(), pa.string(), pa.string(), pa.string(),
]


def _insert_stage_buffer(conn: duckdb.DuckDBPyConnection, buffer: list[tuple[Any, ...]]) -> None:
    """Bulk insert a bounded Arrow batch using columnar arrays with explicit types."""
    if not buffer:
        return
    cols = [
        pa.array([row[idx] for row in buffer], type=STAGE_ARROW_TYPES[idx])
        for idx in range(len(STAGE_COLUMNS))
    ]
    table = pa.Table.from_arrays(cols, names=STAGE_COLUMNS)
    conn.register("_absence_import_arrow", table)
    try:
        conn.execute("INSERT INTO absence_import_staging SELECT * FROM _absence_import_arrow")
    finally:
        conn.unregister("_absence_import_arrow")


def _sql_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _csv_headers(path: Path) -> list[str]:
    conn = duckdb.connect()
    try:
        cursor = conn.execute(
            "SELECT * FROM read_csv(?, header=true, auto_detect=true, all_varchar=true, sample_size=-1) LIMIT 0",
            [str(path)],
        )
        return [item[0] for item in cursor.description]
    finally:
        conn.close()


def _stage_csv_eabsensi_rows(
    conn: duckdb.DuckDBPyConnection,
    import_id: str,
    path: Path,
    headers: list[str],
    pair_cross_day: bool = False,
) -> tuple[int, int, dict[str, float]]:
    """Sessionize eAbsensi CSV events without materializing the file in Python.

    Routing is source-mode based.  Job title is optional metadata and never
    participates in routing, pairing, or a business key.
    """
    by_norm = {_header_norm(name): name for name in headers}
    _validate_headers(headers, CSV_EABSENSI_SOURCE_FORMAT)
    quoted = {key: _sql_ident(value) for key, value in by_norm.items()}

    def clean(key: str) -> str:
        column = quoted.get(key)
        if not column:
            return "CAST(NULL AS VARCHAR)"
        return (
            f"CASE WHEN lower(trim(coalesce(cast({column} AS VARCHAR),''))) IN ('','null') "
            f"THEN NULL ELSE trim(cast({column} AS VARCHAR)) END"
        )

    name_key = "nmpeg" if "nmpeg" in quoted else "nama" if "nama" in quoted else ""
    job_key = "nmjabatan" if "nmjabatan" in quoted else "jabatan" if "jabatan" in quoted else ""
    raw_json_args = ",".join(
        f"{_sql_literal(header)},{_sql_ident(header)}" for header in headers
    )
    raw_hash_args = ",".join(
        f"coalesce(trim(cast({_sql_ident(header)} AS VARCHAR)),'')" for header in headers
    )
    timings: dict[str, float] = {}

    started = time.perf_counter()
    conn.execute(f"""
        CREATE TEMP TABLE _eabs_source AS
        WITH input AS (
            SELECT row_number() OVER () AS source_row,
                   json_object({raw_json_args}) AS raw_json,
                   sha256(concat_ws(chr(31),{raw_hash_args})) AS raw_fingerprint,
                   {clean('nip')} AS nip,
                   {clean(name_key)} AS nama,
                   {clean(job_key)} AS jabatan,
                   {clean('tglfinger')} AS finger_raw,
                   {clean('absenpagi')} AS in_raw,
                   {clean('absensore')} AS out_raw,
                   {clean('jnshadir')} AS jenis,
                   {clean('latitude')} AS latitude,{clean('longitude')} AS longitude,
                   {clean('latitudesore')} AS latitude_sore,{clean('longitudesore')} AS longitude_sore
            FROM read_csv(?, header=true, auto_detect=true, all_varchar=true, sample_size=-1, parallel=false)
        ), parsed AS (
            SELECT *,try_cast(finger_raw AS DATE) AS finger_date,
                   coalesce(try_cast(in_raw AS TIMESTAMP),
                            try_cast(finger_raw AS DATE) + try_cast(in_raw AS TIME)) AS in_ts,
                   coalesce(try_cast(out_raw AS TIMESTAMP),
                            try_cast(finger_raw AS DATE) + try_cast(out_raw AS TIME)) AS out_ts
            FROM input
        )
        SELECT *,array_to_string(list_filter([
                   CASE WHEN nip IS NULL THEN 'MISSING_NIP' END,
                   CASE WHEN finger_date IS NULL THEN 'INVALID_DATE' END,
                   CASE WHEN in_raw IS NOT NULL AND in_ts IS NULL THEN 'INVALID_CHECK_IN' END,
                   CASE WHEN out_raw IS NOT NULL AND out_ts IS NULL THEN 'INVALID_CHECK_OUT' END
               ],error_value -> error_value IS NOT NULL),chr(31)) AS source_errors
        FROM parsed
    """, [str(path)])
    source_total, unique_total = conn.execute(
        "SELECT count(*),count(DISTINCT raw_fingerprint) FROM _eabs_source"
    ).fetchone()
    conn.execute("""
        CREATE TEMP TABLE _eabs_unique_source AS
        SELECT * EXCLUDE(duplicate_rank) FROM (
            SELECT *,row_number() OVER(PARTITION BY raw_fingerprint ORDER BY source_row) AS duplicate_rank
            FROM _eabs_source
        ) WHERE duplicate_rank=1
    """)
    conn.execute("DROP TABLE _eabs_source")
    timings["source_stage_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    if pair_cross_day:
        conn.execute("""
            CREATE TEMP TABLE _eabs_event_all AS
            SELECT nip,in_ts AS event_datetime,'CHECK_IN' AS event_type,0 AS event_order,
                   source_row,finger_date AS tgl_finger,nama,jabatan,jenis AS jnshadir,
                   raw_json,source_errors
            FROM _eabs_unique_source WHERE nip IS NOT NULL AND finger_date IS NOT NULL AND in_ts IS NOT NULL
            UNION ALL
            SELECT nip,out_ts,'CHECK_OUT',1,source_row,finger_date,nama,jabatan,jenis,
                   raw_json,source_errors
            FROM _eabs_unique_source WHERE nip IS NOT NULL AND finger_date IS NOT NULL AND out_ts IS NOT NULL
        """)
        conn.execute("""
            CREATE TEMP TABLE _eabs_event AS
            SELECT * EXCLUDE(event_duplicate_rank) FROM (
                SELECT *,row_number() OVER(
                    PARTITION BY nip,event_type,event_datetime ORDER BY source_row,event_order
                ) AS event_duplicate_rank
                FROM _eabs_event_all
            ) WHERE event_duplicate_rank=1
        """)
        event_total, unique_event_total = conn.execute(
            "SELECT (SELECT count(*) FROM _eabs_event_all),(SELECT count(*) FROM _eabs_event)"
        ).fetchone()
        conn.execute("""
            CREATE TEMP TABLE _eabs_order_base AS
            SELECT row_number() OVER(
                       ORDER BY nip,event_datetime,source_row,event_order
                   ) AS event_sequence,*
            FROM _eabs_event
        """)
        conn.execute("""
            CREATE TEMP TABLE _eabs_ordered AS
            SELECT *,sum(CASE WHEN event_type='CHECK_IN' THEN 1 ELSE 0 END) OVER(
                       PARTITION BY nip ORDER BY event_datetime,source_row,event_order
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS in_group
            FROM _eabs_order_base
        """)
    else:
        event_total, unique_event_total = conn.execute("""
            WITH events AS (
                SELECT nip,'CHECK_IN' event_type,in_ts event_datetime FROM _eabs_unique_source
                WHERE nip IS NOT NULL AND finger_date IS NOT NULL AND in_ts IS NOT NULL
                UNION ALL
                SELECT nip,'CHECK_OUT',out_ts FROM _eabs_unique_source
                WHERE nip IS NOT NULL AND finger_date IS NOT NULL AND out_ts IS NOT NULL
            )
            SELECT count(*),count(DISTINCT (nip,event_type,event_datetime)) FROM events
        """).fetchone()
    timings["sort_event_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    if pair_cross_day:
        conn.execute(f"""
            CREATE TEMP TABLE _eabs_pair AS
            SELECT * EXCLUDE(pair_rank) FROM (
                SELECT i.event_sequence AS in_sequence,o.event_sequence AS out_sequence,
                       o.event_datetime AS out_datetime,o.source_row AS out_source_row,
                       o.nama AS out_nama,o.jabatan AS out_jabatan,o.jnshadir AS out_jenis,
                       o.raw_json AS out_raw_json,o.source_errors AS out_source_errors,
                       row_number() OVER(PARTITION BY i.event_sequence
                           ORDER BY o.event_datetime,o.source_row,o.event_order) AS pair_rank
                FROM _eabs_ordered i
                JOIN _eabs_ordered o
                  ON o.nip=i.nip AND o.in_group=i.in_group
                 AND o.event_type='CHECK_OUT' AND o.event_sequence>i.event_sequence
                 AND datediff('minute',i.event_datetime,o.event_datetime)
                     BETWEEN 0 AND {CSV_EABSENSI_MAX_SESSION_MIN}
                 AND (
                      cast(i.event_datetime AS DATE)=cast(o.event_datetime AS DATE)
                      OR (
                          extract(hour FROM i.event_datetime)*60+extract(minute FROM i.event_datetime)
                              >= {CSV_EABSENSI_NIGHT_IN_START_MIN}
                          AND extract(hour FROM o.event_datetime)*60+extract(minute FROM o.event_datetime)
                              <= {CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN}
                      )
                 )
                WHERE i.event_type='CHECK_IN'
            ) WHERE pair_rank=1
        """)
        conn.execute(f"""
            CREATE TEMP TABLE _eabs_record_raw AS
            SELECT i.nip,cast(i.event_datetime AS DATE) AS work_date,
                   coalesce(i.nama,p.out_nama) AS nama,coalesce(i.jabatan,p.out_jabatan) AS jabatan,
                   coalesce(i.jnshadir,p.out_jenis) AS jenis,
                   i.event_datetime AS in_datetime,p.out_datetime,
                   i.raw_json,array_to_string(list_filter([
                       nullif(i.source_errors,''),nullif(p.out_source_errors,'')
                   ],error_value -> error_value IS NOT NULL),chr(31)) AS source_errors,
                   i.source_row AS in_source_row,p.out_source_row
            FROM _eabs_ordered i LEFT JOIN _eabs_pair p ON p.in_sequence=i.event_sequence
            WHERE i.event_type='CHECK_IN'
            UNION ALL
            SELECT o.nip,
                   CASE WHEN extract(hour FROM o.event_datetime)*60+extract(minute FROM o.event_datetime)
                              <= {CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN}
                        THEN cast(cast(o.event_datetime AS DATE)-INTERVAL 1 DAY AS DATE)
                        ELSE cast(o.event_datetime AS DATE) END AS work_date,
                   o.nama,o.jabatan,o.jnshadir,NULL,o.event_datetime,o.raw_json,o.source_errors,
                   NULL,o.source_row
            FROM _eabs_ordered o
            WHERE o.event_type='CHECK_OUT'
              AND NOT EXISTS(SELECT 1 FROM _eabs_pair p WHERE p.out_sequence=o.event_sequence)
        """)
    else:
        # Normal mode deliberately ignores physical CSV order for punches.  It
        # produces exactly one daily record per NIP/work date using the earliest
        # valid check-in and latest valid check-out.  Source order is used only
        # as a deterministic metadata tie-breaker, matching legacy's first-row
        # metadata preference without making pairing adjacency-dependent.
        conn.execute("""
            CREATE TEMP TABLE _eabs_record_raw AS
            SELECT nip,finger_date AS work_date,
                   first(nama ORDER BY source_row) FILTER (nama IS NOT NULL) AS nama,
                   first(jabatan ORDER BY source_row) FILTER (jabatan IS NOT NULL) AS jabatan,
                   first(jenis ORDER BY source_row) FILTER (jenis IS NOT NULL) AS jenis,
                   min(in_ts) AS in_datetime,max(out_ts) AS out_datetime,
                   first(raw_json ORDER BY source_row) AS raw_json,
                   string_agg(source_errors,chr(31) ORDER BY source_row)
                       FILTER (source_errors IS NOT NULL AND source_errors<>'') AS source_errors,
                   min(source_row) FILTER (in_ts IS NOT NULL) AS in_source_row,
                   min(source_row) FILTER (out_ts IS NOT NULL) AS out_source_row
            FROM _eabs_unique_source
            WHERE nip IS NOT NULL AND finger_date IS NOT NULL
              AND (in_ts IS NOT NULL OR out_ts IS NOT NULL)
            GROUP BY nip,finger_date
        """)
    conn.execute("""
        CREATE TEMP TABLE _eabs_record AS
        SELECT i.nip,cast(i.work_date AS DATE) AS work_date,
               first(nama ORDER BY coalesce(in_source_row,out_source_row)) FILTER (nama IS NOT NULL) AS nama,
               first(jabatan ORDER BY coalesce(in_source_row,out_source_row)) FILTER (jabatan IS NOT NULL) AS jabatan,
               first(jenis ORDER BY coalesce(in_source_row,out_source_row)) FILTER (jenis IS NOT NULL) AS jenis,
               min(in_datetime) AS in_datetime,max(out_datetime) AS out_datetime,
               first(raw_json ORDER BY coalesce(in_source_row,out_source_row)) AS raw_json,
               string_agg(source_errors,chr(31) ORDER BY coalesce(in_source_row,out_source_row))
                   FILTER (source_errors IS NOT NULL AND source_errors<>'') AS source_errors,
               min(in_source_row) AS in_source_row,min(out_source_row) AS out_source_row
        FROM _eabs_record_raw i
        GROUP BY i.nip,cast(i.work_date AS DATE)
    """)
    conn.execute("""
        CREATE TEMP TABLE _eabs_all_record AS
        SELECT * FROM _eabs_record
        UNION ALL
        SELECT s.nip,s.finger_date,s.nama,s.jabatan,s.jenis,s.in_ts,s.out_ts,s.raw_json,s.source_errors,
               s.source_row,s.source_row
        FROM _eabs_unique_source s
        WHERE s.nip IS NULL OR s.finger_date IS NULL
           OR (s.in_ts IS NULL AND s.out_ts IS NULL AND (s.in_raw IS NOT NULL OR s.out_raw IS NOT NULL))
    """)
    conn.execute("DROP TABLE _eabs_unique_source")
    conn.execute("DROP TABLE _eabs_record")
    conn.execute("DROP TABLE _eabs_record_raw")
    if pair_cross_day:
        for table in ("_eabs_pair", "_eabs_ordered", "_eabs_order_base", "_eabs_event", "_eabs_event_all"):
            conn.execute(f"DROP TABLE {table}")
    timings["pair_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    conn.execute("""
        CREATE TEMP TABLE _eabs_output AS
        SELECT row_number() OVER(
                   ORDER BY work_date NULLS LAST,nip NULLS LAST,
                            coalesce(in_datetime,out_datetime),
                            coalesce(in_source_row,out_source_row)
               ) AS output_row,*
        FROM _eabs_all_record
    """)
    conn.execute("""
        INSERT INTO absence_import_staging
        WITH prepared AS (
            SELECT *,array_to_string(list_filter([nullif(source_errors,''),
                       CASE WHEN in_datetime IS NULL THEN 'MISSING_CHECK_IN' END,
                       CASE WHEN out_datetime IS NULL THEN 'MISSING_CHECK_OUT' END
                   ],issue_value -> issue_value IS NOT NULL),chr(31)) AS issues
            FROM _eabs_output
        )
        SELECT ?,output_row,raw_json,
               sha256(concat_ws(chr(31),coalesce(nip,''),coalesce(cast(work_date AS VARCHAR),''),
                    coalesce(cast(in_datetime AS VARCHAR),''),coalesce(cast(out_datetime AS VARCHAR),''))),
               nip,coalesce(nama,''),coalesce(jabatan,''),cast(work_date AS VARCHAR),
               CASE WHEN out_datetime IS NULL OR cast(out_datetime AS DATE)=work_date THEN ''
                    ELSE cast(cast(out_datetime AS DATE) AS VARCHAR) END,
               CASE WHEN in_datetime IS NULL THEN '' ELSE strftime(in_datetime,'%H:%M:%S') END,
               CASE WHEN out_datetime IS NULL THEN '' ELSE strftime(out_datetime,'%H:%M:%S') END,
               CASE WHEN in_datetime IS NULL THEN NULL ELSE extract(hour FROM in_datetime)::INTEGER*60+extract(minute FROM in_datetime)::INTEGER END,
               CASE WHEN out_datetime IS NULL THEN NULL ELSE extract(hour FROM out_datetime)::INTEGER*60+extract(minute FROM out_datetime)::INTEGER END,
               CASE WHEN in_datetime IS NULL THEN NULL ELSE extract(hour FROM in_datetime)::INTEGER*60+extract(minute FROM in_datetime)::INTEGER END,
               CASE WHEN out_datetime IS NULL THEN NULL ELSE extract(hour FROM out_datetime)::INTEGER*60+extract(minute FROM out_datetime)::INTEGER END,
               CASE WHEN in_datetime IS NULL THEN NULL ELSE extract(hour FROM in_datetime)::INTEGER*60+extract(minute FROM in_datetime)::INTEGER END,
               CASE WHEN out_datetime IS NULL THEN NULL ELSE extract(hour FROM out_datetime)::INTEGER*60+extract(minute FROM out_datetime)::INTEGER END,
               'WIB','','',coalesce(jenis,''),?,strftime(work_date,'%Y-%m'),
               out_datetime IS NOT NULL AND cast(out_datetime AS DATE)<>work_date,
               false,
               coalesce(source_errors,'')='',
               CASE WHEN coalesce(source_errors,'')<>'' THEN 'BLOCKER'
                    WHEN in_datetime IS NULL OR out_datetime IS NULL THEN 'WARNING'
                    ELSE 'ACCEPTED_LEGACY' END,
               CASE WHEN issues='' THEN '[]' ELSE to_json(string_split(issues,chr(31))) END,
               json_object(
                   'check_in_datetime',CASE WHEN in_datetime IS NULL THEN NULL ELSE strftime(in_datetime,'%Y-%m-%d %H:%M:%S') END,
                   'check_out_datetime',CASE WHEN out_datetime IS NULL THEN NULL ELSE strftime(out_datetime,'%Y-%m-%d %H:%M:%S') END,
                   'in_source_row',in_source_row,'out_source_row',out_source_row,
                   'engine',?
               )
        FROM prepared
    """, [import_id, CSV_EABSENSI_SOURCE_FORMAT, CSV_EABSENSI_ENGINE_VERSION])
    stage_count = int(conn.execute(
        "SELECT count(*) FROM absence_import_staging WHERE import_id=?", [import_id]
    ).fetchone()[0])
    duplicate_count = int(source_total - unique_total + event_total - unique_event_total)
    timings["canonical_prepare_ms"] = (time.perf_counter() - started) * 1000
    timings.update({
        "source_rows": float(source_total),
        "unique_source_rows": float(unique_total),
        "duplicate_source_rows": float(source_total - unique_total),
        "unique_event_rows": float(unique_event_total),
        "duplicate_event_rows": float(event_total - unique_event_total),
        "canonical_rows": float(stage_count),
        "unique_nip": float(conn.execute(
            "SELECT count(DISTINCT nip) FROM _eabs_output WHERE nip IS NOT NULL"
        ).fetchone()[0]),
        "pair_cross_day": bool(pair_cross_day),
        "complete_pairs": float(conn.execute(
            "SELECT count(*) FROM _eabs_output WHERE in_datetime IS NOT NULL AND out_datetime IS NOT NULL"
        ).fetchone()[0]),
        "check_in_only": float(conn.execute(
            "SELECT count(*) FROM _eabs_output WHERE in_datetime IS NOT NULL AND out_datetime IS NULL"
        ).fetchone()[0]),
        "check_out_only": float(conn.execute(
            "SELECT count(*) FROM _eabs_output WHERE in_datetime IS NULL AND out_datetime IS NOT NULL"
        ).fetchone()[0]),
        "morning_out_cutoff_min": float(CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN),
    })
    return stage_count, duplicate_count, timings


WALI_SOURCE_COLUMNS = (
    "source_row", "raw_fingerprint", "raw_json", "nip", "nama", "jabatan",
    "finger_date", "in_date", "out_date", "in_min", "out_min", "source_errors",
)
WALI_EVENT_COLUMNS = (
    "nip", "event_date", "minute", "source_row", "event_order",
    "nama", "jabatan", "raw_json", "source_errors", "is_empty",
)
WALI_RECORD_COLUMNS = (
    "nip", "work_date", "nama", "jabatan", "in_min", "out_date",
    "out_min", "raw_json", "source_errors", "record_order",
)


def _wali_source_values(source_row: int, row: dict[str, Any], headers: list[str]) -> tuple[Any, ...]:
    index = {_header_norm(name): name for name in headers}
    value = lambda name: row.get(index.get(name, ""))
    normalized_cells = [
        "" if item is None or str(item).strip().lower() == "null" else str(item).strip()
        for item in (row.get(header) for header in headers)
    ]
    finger = parse_date(value("tglfinger"))
    in_date = parse_date(value("tanggalmasuk")) or finger
    out_date = parse_date(value("tanggalpulang")) or finger
    in_raw, out_raw = value("absenpagi"), value("absensore")
    in_min, out_min = parse_time(in_raw), parse_time(out_raw)
    source_errors = []
    if not str(value("nip") or "").strip():
        source_errors.append("MISSING_NIP")
    if not in_date and not out_date:
        source_errors.append("INVALID_DATE")
    if in_raw is not None and str(in_raw).strip() and in_min is None:
        source_errors.append("INVALID_CHECK_IN")
    if out_raw is not None and str(out_raw).strip() and out_min is None:
        source_errors.append("INVALID_CHECK_OUT")
    return (
        source_row, hashlib.sha256("\x1f".join(normalized_cells).encode("utf-8")).hexdigest(),
        _json(row), str(value("nip") or "").strip(), str(value("nmpeg") or "").strip(),
        str(value("jabatan") or value("nmjabatan") or "").strip(),
        finger["key"] if finger else None, in_date["key"] if in_date else None,
        out_date["key"] if out_date else None,
        in_min, out_min, "\x1f".join(source_errors),
    )


def _stage_wali_rows(
    conn: duckdb.DuckDBPyConnection,
    import_id: str,
    rows: Iterable[dict[str, Any]],
    headers: list[str],
) -> tuple[int, int, dict[str, float]]:
    """Stage Wali rows using DuckDB-backed dedupe, sort, pair, and work-date merge."""
    import pyarrow as pa

    conn.execute("""
        CREATE TEMP TABLE _wali_source (
            source_row BIGINT, raw_fingerprint VARCHAR, raw_json VARCHAR, nip VARCHAR, nama VARCHAR,
            jabatan VARCHAR, finger_date VARCHAR, in_date VARCHAR, out_date VARCHAR,
            in_min INTEGER, out_min INTEGER, source_errors VARCHAR
        )
    """)
    conn.execute("""
        CREATE TEMP TABLE _wali_event (
            nip VARCHAR, event_date VARCHAR, minute INTEGER, source_row BIGINT, event_order INTEGER,
            nama VARCHAR, jabatan VARCHAR, raw_json VARCHAR, source_errors VARCHAR, is_empty BOOLEAN
        )
    """)
    conn.execute("""
        CREATE TEMP TABLE _wali_record (
            nip VARCHAR, work_date VARCHAR, nama VARCHAR, jabatan VARCHAR,
            in_min INTEGER, out_date VARCHAR, out_min INTEGER,
            raw_json VARCHAR, source_errors VARCHAR, record_order BIGINT
        )
    """)
    conn.execute("""
        CREATE TEMP TABLE _wali_rejected (
            source_row BIGINT, raw_json VARCHAR, nip VARCHAR, nama VARCHAR,
            jabatan VARCHAR, reject_date VARCHAR, source_errors VARCHAR
        )
    """)

    def insert_buffer(table: str, columns: tuple[str, ...], buffer: list[tuple[Any, ...]]) -> None:
        arrow = pa.Table.from_pylist([dict(zip(columns, values)) for values in buffer])
        conn.register("_wali_arrow", arrow)
        try:
            conn.execute(f"INSERT INTO {table} SELECT * FROM _wali_arrow")
        finally:
            conn.unregister("_wali_arrow")

    timings: dict[str, float] = {}
    started = time.perf_counter()
    source_buffer: list[tuple[Any, ...]] = []
    source_count = 0
    raw_limit = os.environ.get("EVKIN_WALI_IMPORT_MAX_ROWS", str(DEFAULT_WALI_IMPORT_MAX_ROWS)).strip()
    if not raw_limit.isdigit() or int(raw_limit) < 1:
        raise ImportEngineError(
            "INVALID_WALI_IMPORT_LIMIT",
            "EVKIN_WALI_IMPORT_MAX_ROWS harus berupa bilangan bulat positif.",
            500,
        )
    max_rows = int(raw_limit)
    if max_rows > DEFAULT_WALI_IMPORT_MAX_ROWS:
        raise ImportEngineError(
            "INVALID_WALI_IMPORT_LIMIT",
            f"EVKIN_WALI_IMPORT_MAX_ROWS tidak boleh melebihi batas UAT {DEFAULT_WALI_IMPORT_MAX_ROWS:,}.",
            500,
        )
    for source_count, row in enumerate(rows, 1):
        if source_count > max_rows:
            raise ImportEngineError(
                "WALI_IMPORT_LIMIT_EXCEEDED",
                f"Import Wali dibatasi {max_rows:,} baris sumber; berkas memuat setidaknya {source_count:,}. "
                "Pecah file menjadi beberapa bagian atau naikkan EVKIN_WALI_IMPORT_MAX_ROWS setelah UAT kapasitas.",
                413,
            )
        source_buffer.append(_wali_source_values(source_count, row, headers))
        if len(source_buffer) >= 5000:
            insert_buffer("_wali_source", WALI_SOURCE_COLUMNS, source_buffer)
            source_buffer.clear()
    if source_buffer:
        insert_buffer("_wali_source", WALI_SOURCE_COLUMNS, source_buffer)
    timings["source_stage_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    source_total, unique_total = conn.execute(
        "SELECT count(*),count(DISTINCT raw_fingerprint) FROM _wali_source"
    ).fetchone()
    duplicate_source = int(source_total - unique_total)
    conn.execute("""
        CREATE TEMP TABLE _wali_unique_source AS
        SELECT source_row,raw_json,nip,nama,jabatan,in_date,out_date,in_min,out_min,source_errors
        FROM (
            SELECT *,row_number() OVER (
                PARTITION BY raw_fingerprint ORDER BY source_row
            ) AS duplicate_rank
            FROM _wali_source
        )
        WHERE duplicate_rank=1
    """)
    event_buffer: list[tuple[Any, ...]] = []
    rejected_buffer: list[tuple[Any, ...]] = []
    last_source_row = 0
    while True:
        source_batch = conn.execute("""
            SELECT source_row,raw_json,nip,nama,jabatan,in_date,out_date,in_min,out_min,source_errors
            FROM _wali_unique_source
            WHERE source_row>?
            ORDER BY source_row
            LIMIT 8192
        """, [last_source_row]).fetchall()
        if not source_batch:
            break
        for source_row, raw_json, nip, nama, jabatan, in_date, out_date, in_min, out_min, source_errors in source_batch:
            last_source_row = source_row
            if not nip or not (in_date or out_date):
                rejected_buffer.append((
                    source_row, raw_json, nip, nama, jabatan, in_date or out_date, source_errors,
                ))
                continue
            if in_date and in_min is not None:
                event_buffer.append((nip, in_date, in_min, source_row, 0, nama, jabatan, raw_json, source_errors, False))
            if out_date and out_min is not None:
                event_buffer.append((nip, out_date, out_min, source_row, 1, nama, jabatan, raw_json, source_errors, False))
            if (in_date or out_date) and in_min is None and out_min is None:
                event_buffer.append((
                    nip, in_date or out_date, 0, source_row, 2,
                    nama, jabatan, raw_json, source_errors, True,
                ))
        if event_buffer:
            insert_buffer("_wali_event", WALI_EVENT_COLUMNS, event_buffer)
            event_buffer.clear()
        if rejected_buffer:
            insert_buffer(
                "_wali_rejected",
                ("source_row", "raw_json", "nip", "nama", "jabatan", "reject_date", "source_errors"),
                rejected_buffer,
            )
            rejected_buffer.clear()
    timings["dedupe_event_build_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    conn.execute("""
        CREATE TEMP TABLE _wali_ordered_event AS
        SELECT row_number() OVER (
            ORDER BY nip,is_empty DESC,
                     CASE WHEN is_empty THEN '' ELSE event_date END,
                     CASE WHEN is_empty THEN 0 ELSE minute END,
                     source_row,event_order
        ) AS event_sequence, *
        FROM _wali_event
    """)
    record_buffer: list[tuple[Any, ...]] = []
    current_nip: str | None = None
    pending: tuple[Any, ...] | None = None
    record_order = 0
    def add_record(
        nip: str, work_date: str, nama: str, jabatan: str,
        in_min: int | None, out_date: str | None, out_min: int | None,
        raw_json: str, source_errors: str,
    ) -> None:
        nonlocal record_order
        record_buffer.append(
            (nip, work_date, nama, jabatan, in_min, out_date, out_min, raw_json, source_errors, record_order)
        )
        record_order += 1
        if len(record_buffer) >= 5000:
            insert_buffer("_wali_record", WALI_RECORD_COLUMNS, record_buffer)
            record_buffer.clear()

    def flush_pending() -> None:
        nonlocal pending
        if pending is not None:
            nip, event_date, minute, _, _, nama, jabatan, raw_json, source_errors, _ = pending
            add_record(nip, event_date, nama, jabatan, minute, None, None, raw_json, source_errors)
            pending = None

    last_event_sequence = 0
    while True:
        event_batch = conn.execute("""
            SELECT event_sequence,nip,event_date,minute,source_row,event_order,
                   nama,jabatan,raw_json,source_errors,is_empty
            FROM _wali_ordered_event
            WHERE event_sequence>?
            ORDER BY event_sequence
            LIMIT 8192
        """, [last_event_sequence]).fetchall()
        if not event_batch:
            break
        for event_sequence, *event in event_batch:
            last_event_sequence = event_sequence
            nip, event_date, minute, _, _, nama, jabatan, raw_json, source_errors, is_empty = event
            if nip != current_nip:
                flush_pending()
                current_nip = nip
                record_order = 0
            if is_empty:
                add_record(nip, event_date, nama, jabatan, None, None, None, raw_json, source_errors)
            elif pending is None:
                pending = event
            else:
                _, pending_date, pending_minute, _, _, _, _, _, pending_errors, _ = pending
                elapsed = (
                    (date.fromisoformat(event_date) - date.fromisoformat(pending_date)).days * 1440
                    + int(minute) - int(pending_minute)
                )
                if 0 <= elapsed <= 18 * 60:
                    start = pending
                    add_record(
                        nip, start[1], start[5], start[6], start[2], event_date, int(minute),
                        start[7], "\x1f".join(filter(None, (pending_errors, source_errors))),
                    )
                    pending = None
                else:
                    flush_pending()
                    pending = event
    flush_pending()
    if record_buffer:
        insert_buffer("_wali_record", WALI_RECORD_COLUMNS, record_buffer)
        record_buffer.clear()
    timings["sort_pair_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    conn.execute("""
        CREATE TEMP TABLE _wali_merged AS
        WITH representatives AS (
            SELECT *,row_number() OVER (
                PARTITION BY nip,work_date ORDER BY record_order
            ) AS representative_rank
            FROM _wali_record
        ), arrivals AS (
            SELECT nip,work_date,min(in_min) AS in_min
            FROM _wali_record GROUP BY nip,work_date
        ), checkout_ranked AS (
            SELECT nip,work_date,out_date,out_min,row_number() OVER (
                PARTITION BY nip,work_date ORDER BY out_date DESC,out_min DESC,record_order
            ) AS checkout_rank
            FROM _wali_record WHERE out_min IS NOT NULL
        ), issue_codes AS (
            SELECT nip,work_date,
                   list_distinct(flatten(list(string_split(source_errors, chr(31))))) AS source_errors
            FROM _wali_record GROUP BY nip,work_date
        )
        SELECT row_number() OVER (ORDER BY r.work_date,r.nip) AS output_sequence,
               r.nip,r.work_date,r.nama,r.jabatan,a.in_min,c.out_date,c.out_min,
               r.raw_json,i.source_errors
        FROM representatives r
        JOIN arrivals a USING(nip,work_date)
        JOIN issue_codes i USING(nip,work_date)
        LEFT JOIN checkout_ranked c
          ON c.nip=r.nip AND c.work_date=r.work_date AND c.checkout_rank=1
        WHERE r.representative_rank=1
    """)
    canonical_headers = [
        "NIP", "Nama", "Jabatan", "Tanggal", "Tanggal Pulang",
        "Jam Masuk", "Jam Pulang", "Jenis", "Zona",
    ]
    canonical_mapping = _validate_headers(canonical_headers, "standard")
    stage_buffer: list[tuple[Any, ...]] = []
    stage_count = 0
    last_output_sequence = 0
    while True:
        record_batch = conn.execute("""
            SELECT output_sequence,nip,work_date,nama,jabatan,in_min,out_date,out_min,
                   raw_json,source_errors
            FROM _wali_merged
            WHERE output_sequence>?
            ORDER BY output_sequence
            LIMIT 8192
        """, [last_output_sequence]).fetchall()
        if not record_batch:
            break
        for output_sequence, nip, work_date, nama, jabatan, in_min, out_date, out_min, raw_json, source_errors in record_batch:
            last_output_sequence = output_sequence
            row = {
                "NIP": nip, "Nama": nama, "Jabatan": jabatan, "Tanggal": work_date,
                "Tanggal Pulang": out_date or "",
                "Jam Masuk": fmt_time(in_min) if in_min is not None else "",
                "Jam Pulang": fmt_time(out_min) if out_min is not None else "",
                "Jenis": "Bekerja di Kantor", "Zona": "WIB", "_raw": json.loads(raw_json),
                "_wali_error_codes": [code for code in source_errors if code],
            }
            stage_count += 1
            stage_buffer.append(_canonical_stage_row(
                import_id, stage_count, row, canonical_headers, canonical_mapping, "wali",
            ))
            if len(stage_buffer) >= 5000:
                _insert_stage_buffer(conn, stage_buffer)
                stage_buffer.clear()
    if stage_buffer:
        _insert_stage_buffer(conn, stage_buffer)
        stage_buffer.clear()
    paired_work_date_rows = stage_count
    rejected_source_row = 0
    rejected_count = 0
    while True:
        rejected_rows = conn.execute("""
            SELECT source_row,raw_json,nip,nama,jabatan,reject_date,source_errors
            FROM _wali_rejected
            WHERE source_row>?
            ORDER BY source_row
            LIMIT 8192
        """, [rejected_source_row]).fetchall()
        if not rejected_rows:
            break
        for source_row, raw_json, nip, nama, jabatan, reject_date, source_errors in rejected_rows:
            rejected_source_row = source_row
            rejected_count += 1
            stage_count += 1
            rejected = {
                "NIP": nip or "", "Nama": nama or "", "Jabatan": jabatan or "",
                "Tanggal": reject_date or "", "Tanggal Pulang": "",
                "Jam Masuk": "", "Jam Pulang": "", "Jenis": "Bekerja di Kantor",
                "Zona": "WIB", "_raw": json.loads(raw_json),
                "_wali_error_codes": [code for code in source_errors.split("\x1f") if code],
            }
            stage_buffer.append(_canonical_stage_row(
                import_id, stage_count, rejected, canonical_headers, canonical_mapping, "wali",
            ))
            if len(stage_buffer) >= 5000:
                _insert_stage_buffer(conn, stage_buffer)
                stage_buffer.clear()
    if stage_buffer:
        _insert_stage_buffer(conn, stage_buffer)
    timings["merge_output_stage_ms"] = (time.perf_counter() - started) * 1000
    timings.update({
        "source_rows": float(source_total),
        "duplicate_source_rows": float(duplicate_source),
        "paired_work_date_rows": float(paired_work_date_rows),
        "rejected_source_rows": float(rejected_count),
    })
    return stage_count, duplicate_source, timings


class AbsenceImportService:
    def __init__(
        self,
        db_path: str | Path,
        upload_dir: str | Path | None = None,
        authority_config: ImportAuthorityConfig | None = None,
        generation_reader: Callable[[], int] = current_reset_generation,
        generation_validator: Callable[[int], None] = assert_current_generation,
        write_gate: threading.Lock = DATABASE_GATE,
    ):
        self.db_path = str(db_path)
        self.upload_dir = Path(upload_dir or Path(self.db_path).parent / "import-uploads")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.authority_config = authority_config or ImportAuthorityConfig.from_environment()
        self.generation_reader = generation_reader
        self.generation_validator = generation_validator
        self.write_gate = write_gate

    def _connect(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(self.db_path)
        raw_limit = str(os.environ.get("EVKIN_IMPORT_DUCKDB_MEMORY_LIMIT", "1GB")).strip().upper()
        limit = raw_limit if re.fullmatch(r"\d+(?:MB|GB)", raw_limit) else "1GB"
        spill = Path(tempfile.gettempdir()) / "evkin-import-duckdb-spill"
        spill.mkdir(parents=True, exist_ok=True)
        conn.execute(f"SET memory_limit='{limit}'")
        # Import queries are dominated by large sort/window operations.  One
        # worker keeps their per-thread buffers bounded and DuckDB can spill the
        # set-based pipeline to the dedicated temp directory when required.
        conn.execute("SET threads=1")
        conn.execute("SET preserve_insertion_order=false")
        database_key = str(Path(self.db_path).resolve())
        with _TEMP_DIRECTORY_LOCK:
            active_spill = Path(str(
                conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
            ))
            if active_spill.resolve() != spill.resolve():
                try:
                    conn.execute("SET temp_directory=?", [str(spill)])
                except duckdb.NotImplementedException as exc:
                    # The database may already have used its temp directory
                    # through another EVKIN connection. DuckDB cannot switch
                    # it then; keep its existing spill location instead of
                    # failing the upload before a batch is even created.
                    if "cannot switch temporary directory after the current one has been used" not in str(exc).lower():
                        conn.close()
                        raise
                    if not active_spill.is_dir() or not os.access(active_spill, os.W_OK):
                        conn.close()
                        raise
                    _LOGGER.warning(
                        "DuckDB temp_directory already in use for %s; keeping %s for CSV import spill",
                        database_key,
                        active_spill,
                    )
        return conn

    @staticmethod
    def _active_workflow(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        if not _table_exists(conn, "workflow_config"):
            return {}
        cursor = conn.execute("SELECT * FROM workflow_config LIMIT 1")
        values = cursor.fetchone()
        if values is None:
            return {}
        config = dict(zip((item[0] for item in cursor.description), values))
        workflow = config.get("_workflow")
        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except json.JSONDecodeError:
                return {}
        return workflow if isinstance(workflow, dict) else {}

    @staticmethod
    def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
        if not _table_exists(conn, table):
            return []
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]

    def authority_info(self) -> dict[str, Any]:
        special_route = (
            ImportAuthority.CSV_EABSENSI_SPECIAL.value
            if self.authority_config.csv_eabsensi_enabled else "DISABLED"
        )
        return {"authority": self.authority_config.authority.value,
                "default_authority": ImportAuthority.IMPORT_JS_LEGACY.value,
                "engine_version": IMPORT_ENGINE_VERSION,
                "csv_eabsensi_engine_version": CSV_EABSENSI_ENGINE_VERSION,
                "routes": {
                    "standard": self.authority_config.route("standard").value,
                    "wali": self.authority_config.route("wali").value,
                    CSV_EABSENSI_SOURCE_FORMAT: special_route,
                },
                "csv_eabsensi_enabled": self.authority_config.csv_eabsensi_enabled,
                "wali_real_world_parity": "PENDING"}

    def save_upload(self, upload: UploadFile, *, operator: str, source_format: str,
                    period: str | None = None, workflow_config_json: str | None = None,
                    pair_cross_day: bool = False) -> dict[str, Any]:
        filename = Path(upload.filename or "upload.bin").name
        suffix = Path(filename).suffix.lower().lstrip(".")
        file_type = "xlsx" if suffix in {"xlsx", "xlsm"} else suffix
        if file_type not in {"csv", "xlsx", "parquet"}:
            raise ImportEngineError("UNSUPPORTED_FILE", "Gunakan XLSX, CSV, atau Parquet.")
        source_format = str(source_format or "standard").strip().lower()
        if source_format not in {"standard", "wali", CSV_EABSENSI_SOURCE_FORMAT}:
            raise ImportEngineError(
                "INVALID_SOURCE_FORMAT", "source_format harus standard, wali, atau csv_eabsensi"
            )
        if source_format == CSV_EABSENSI_SOURCE_FORMAT and file_type != "csv":
            raise ImportEngineError(
                "CSV_EABSENSI_REQUIRES_CSV",
                "Mode CSV eAbsensi hanya menerima file .csv; tidak ada fallback ke Excel atau Standard.",
                415,
            )
        if period and not PERIOD_RE.fullmatch(period):
            raise ImportEngineError("INVALID_PERIOD", "Periode wajib YYYY-MM")
        import_id = str(uuid.uuid4())
        target = self.upload_dir / f"{import_id}.{file_type}"
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if not size:
            target.unlink(missing_ok=True)
            raise ImportEngineError("EMPTY_FILE", "File upload kosong")
        generation = int(self.generation_reader())
        with self._connect() as conn:
            ensure_absence_import_schema(conn)
            active = self._active_workflow(conn)
            active_import_id = str(active.get("import_id") or "").strip()
            active_period = str(active.get("month") or "").strip()
            duplicate = None
            if active_import_id and active_period and (not period or period == active_period):
                duplicate = conn.execute(
                    """SELECT import_id FROM absence_import_batch
                       WHERE import_id=? AND file_sha256=? AND source_format=?
                         AND coalesce(pair_cross_day,false)=? AND status='COMMITTED'
                         AND period=?
                       LIMIT 1""",
                    [active_import_id, digest.hexdigest(), source_format,
                     bool(pair_cross_day) if source_format == CSV_EABSENSI_SOURCE_FORMAT else False,
                     active_period],
                ).fetchone()
            initial_status = (
                "ALREADY_COMMITTED"
                if duplicate and source_format == CSV_EABSENSI_SOURCE_FORMAT else "STAGED"
            )
            conn.execute("""
                INSERT INTO absence_import_batch
                (import_id,filename,file_type,file_size,file_sha256,period,source_format,operator,
                 reset_generation,engine_version,status,upload_path,workflow_config_json,pair_cross_day,
                 duplicate_of_import_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [import_id, filename, file_type, size, digest.hexdigest(), period, source_format,
                  operator.strip() or "unknown", generation,
                  CSV_EABSENSI_ENGINE_VERSION if source_format == CSV_EABSENSI_SOURCE_FORMAT else IMPORT_ENGINE_VERSION,
                  initial_status, str(target), workflow_config_json,
                  bool(pair_cross_day) if source_format == CSV_EABSENSI_SOURCE_FORMAT else False,
                  duplicate[0] if duplicate else None])
        return self.get(import_id)

    def submit_stage(self, import_id: str) -> dict[str, Any]:
        current = self.get(import_id)
        if current["status"] != "STAGED":
            return current
        _EXECUTOR.submit(self.stage, import_id)
        return self.get(import_id)

    def stage(self, import_id: str) -> dict[str, Any]:
        batch = self.get(import_id)
        if batch["status"] != "STAGED":
            return batch
        started = time.perf_counter()
        try:
            self.generation_validator(int(batch["reset_generation"]))
            path = Path(batch["upload_path"])
            if _sha256_file(path) != batch["file_sha256"]:
                raise ImportEngineError("STALE_SOURCE", "File upload berubah setelah checksum dibuat")
            source_format = batch["source_format"]
            if source_format == CSV_EABSENSI_SOURCE_FORMAT:
                if batch["file_type"] != "csv":
                    raise ImportEngineError(
                        "CSV_EABSENSI_REQUIRES_CSV", "Mode CSV eAbsensi hanya menerima file .csv", 415
                    )
                headers = _csv_headers(path)
                rows: Iterable[dict[str, Any]] = ()
            else:
                headers, rows = _file_rows(path, batch["file_type"])
            mapping = _validate_headers(headers, source_format)
            schema_fingerprint = hashlib.sha256(_json(headers).encode()).hexdigest()
            # Keep one bounded DuckDB connection for all chunks.  The global
            # write gate is acquired only around each short flush, so reset is
            # not blocked for the complete file-normalization duration.
            with self._connect() as conn:
                with self.write_gate:
                    ensure_absence_import_schema(conn)
                    conn.execute("DELETE FROM absence_import_staging WHERE import_id=?", [import_id])
                    conn.execute("UPDATE absence_import_batch SET status='VALIDATING',schema_fingerprint=?,progress=5,updated_at=now() WHERE import_id=?",
                                 [schema_fingerprint, import_id])
                duplicate_source = 0
                wali_timings: dict[str, float] = {}
                if source_format == "wali":
                    _, duplicate_source, wali_timings = _stage_wali_rows(conn, import_id, rows, headers)
                elif source_format == CSV_EABSENSI_SOURCE_FORMAT:
                    _, duplicate_source, wali_timings = _stage_csv_eabsensi_rows(
                        conn, import_id, path, headers, bool(batch.get("pair_cross_day"))
                    )
                buffer: list[tuple[Any, ...]] = []
                count = 0
                if source_format == "standard":
                    estimated_total = _estimate_xlsx_row_count(path) if batch.get("file_type") == "xlsx" else None
                    if estimated_total and estimated_total > 0:
                        with self.write_gate:
                            conn.execute("UPDATE absence_import_batch SET total_rows=? WHERE import_id=?",
                                         [estimated_total, import_id])
                    batch_size = 5000
                    for count, row in enumerate(rows, 1):
                        buffer.append(_canonical_stage_row(import_id, count, row, headers, mapping, source_format))
                        if len(buffer) >= batch_size:
                            with self.write_gate:
                                _insert_stage_buffer(conn, buffer)
                                progress = min(92.0, round(10.0 + (count / max(1, estimated_total)) * 82.0, 1)) if estimated_total else min(90.0, round(10.0 + (count / (count + 50000)) * 80.0, 1))
                                conn.execute("UPDATE absence_import_batch SET status='NORMALIZING',staged_rows=?,normalized_rows=?,progress=?,updated_at=now() WHERE import_id=?",
                                             [count, count, progress, import_id])
                            buffer.clear()
                    if buffer:
                        with self.write_gate:
                            _insert_stage_buffer(conn, buffer)
                            progress = 92.0 if estimated_total else 90.0
                            conn.execute("UPDATE absence_import_batch SET status='NORMALIZING',staged_rows=?,normalized_rows=?,progress=?,updated_at=now() WHERE import_id=?",
                                         [count, count, progress, import_id])
                with self.write_gate:
                    if source_format != CSV_EABSENSI_SOURCE_FORMAT:
                        conn.execute("""
                        UPDATE absence_import_staging SET duplicate_row=true
                        WHERE import_id=? AND nip IS NOT NULL AND tanggal IS NOT NULL
                          AND (nip,tanggal) IN (
                            SELECT nip,tanggal FROM absence_import_staging
                            WHERE import_id=? GROUP BY nip,tanggal HAVING count(*)>1
                          )
                        """, [import_id, import_id])
                    summary = conn.execute("""
                        SELECT count(*),count(*) FILTER(valid),count(*) FILTER(NOT valid),
                          count(*) FILTER(duplicate_row),
                          count(*) FILTER(error_codes LIKE '%MISSING_NIP%'),
                          count(*) FILTER(error_codes LIKE '%INVALID_NIP%'),
                          count(*) FILTER(error_codes LIKE '%INVALID_DATE%'),
                          count(*) FILTER(error_codes LIKE '%INVALID_CHECK_%'),
                          count(*) FILTER(cross_day),count(*) FILTER(source_format='wali'),
                          count(*) FILTER(zona='WIB'),count(*) FILTER(zona='WITA'),count(*) FILTER(zona='WIT')
                        FROM absence_import_staging WHERE import_id=?
                    """, [import_id]).fetchone()
                    periods = [row[0] for row in conn.execute(
                        "SELECT DISTINCT period FROM absence_import_staging WHERE import_id=? AND period IS NOT NULL ORDER BY period",
                        [import_id],
                    ).fetchall()]
                    requested = batch.get("period")
                    selected = requested or (periods[0] if len(periods) == 1 else None)
                    if requested and requested not in periods:
                        raise ImportEngineError("PERIOD_NOT_PRESENT", f"Periode {requested} tidak ada di file")
                    code = "MULTI_PERIOD_REQUIRES_SELECTION" if len(periods) > 1 and not requested else None
                    message = "File memuat beberapa periode; pilih periode secara eksplisit sebelum commit." if code else None
                    elapsed = (time.perf_counter() - started) * 1000
                    total_rows = (
                        int(wali_timings.get("source_rows", summary[0] + duplicate_source))
                        if source_format == CSV_EABSENSI_SOURCE_FORMAT else summary[0] + duplicate_source
                    )
                    conn.execute("""
                        UPDATE absence_import_batch SET status='READY',period=?,periods_json=?,total_rows=?,
                          staged_rows=?,normalized_rows=?,valid_rows=?,invalid_rows=?,duplicate_rows=?,
                          missing_nip_rows=?,invalid_nip_rows=?,invalid_date_rows=?,invalid_time_rows=?,
                          cross_day_rows=?,wali_rows=?,wib_rows=?,wita_rows=?,wit_rows=?,progress=100,
                          error_code=?,error_message=?,timing_json=?,staged_at=now(),validated_at=now(),updated_at=now()
                        WHERE import_id=?
                    """, [selected, _json(periods), total_rows, summary[0], summary[0],
                          summary[1], summary[2], summary[3] + duplicate_source, *summary[4:], code, message,
                          _json({"stage_validate_normalize_ms": elapsed, **wali_timings}), import_id])
            return self.get(import_id)
        except ImportEngineError as exc:
            status = "STALE_SOURCE" if exc.code == "STALE_SOURCE" else (
                "STALE_GENERATION" if exc.code == "STALE_GENERATION" else "FAILED")
            self._fail(import_id, status, exc.code, exc.message)
            return self.get(import_id)
        except Exception as exc:
            self._fail(import_id, "FAILED", "IMPORT_PARSE_FAILED", str(exc))
            return self.get(import_id)

    def _fail(self, import_id: str, status: str, code: str, message: str, *, acquire_gate: bool = True) -> None:
        context = self.write_gate if acquire_gate else nullcontext()
        with context, self._connect() as conn:
            ensure_absence_import_schema(conn)
            conn.execute("UPDATE absence_import_batch SET status=?,error_code=?,error_message=?,completed_at=now(),updated_at=now() WHERE import_id=?",
                         [status, code, message[:2000], import_id])

    def commit(self, import_id: str, *, period: str | None = None, accept_invalid: bool = False,
               acquire_gate: bool = True, before_commit: Callable[[duckdb.DuckDBPyConnection], None] | None = None) -> dict[str, Any]:
        batch = self.get(import_id)
        if batch["status"] != "READY":
            raise ImportEngineError("IMPORT_NOT_READY", f"Batch belum READY: {batch['status']}")
        selected = period or batch.get("period")
        periods = batch.get("periods") or []
        if not selected or selected not in periods:
            raise ImportEngineError("PERIOD_SELECTION_REQUIRED", "Pilih satu periode yang terdapat di file")
        if int(batch.get("invalid_rows") or 0) and not accept_invalid:
            raise ImportEngineError("INVALID_ROWS_REQUIRE_CONFIRMATION", "Ada invalid row; commit memerlukan konfirmasi eksplisit")
        if batch.get("duplicate_of_import_id"):
            with self._connect() as duplicate_conn:
                active = self._active_workflow(duplicate_conn)
            duplicate_is_authoritative = (
                str(active.get("import_id") or "") == str(batch["duplicate_of_import_id"])
                and str(active.get("month") or "") == str(selected)
            )
            if duplicate_is_authoritative:
                raise ImportEngineError("ALREADY_COMMITTED", "File ini sudah pernah berhasil diimport", 409)
        context = self.write_gate if acquire_gate else nullcontext()
        started = time.perf_counter()
        with context:
            try:
                self.generation_validator(int(batch["reset_generation"]))
            except Exception as exc:
                self._fail(import_id, "STALE_GENERATION", "STALE_GENERATION", "Reset generation berubah sebelum promote", acquire_gate=False)
                raise ImportEngineError("STALE_GENERATION", "Reset generation berubah sebelum promote", 409) from exc
            path = Path(batch["upload_path"])
            if not path.exists() or _sha256_file(path) != batch["file_sha256"]:
                self._fail(import_id, "STALE_SOURCE", "STALE_SOURCE", "Upload berubah sebelum promote", acquire_gate=False)
                raise ImportEngineError("STALE_SOURCE", "Upload berubah sebelum promote", 409)
            with self._connect() as conn:
                ensure_absence_import_schema(conn)
                conn.execute("BEGIN TRANSACTION")
                try:
                    conn.execute("UPDATE absence_import_batch SET status='COMMITTING',progress=99,updated_at=now() WHERE import_id=?", [import_id])
                    if before_commit:
                        before_commit(conn)
                    canonical_columns = {
                        "workflow_id", "import_id", "source_row", "nip", "nama", "jabatan", "tanggal",
                        "tanggal_pulang", "jam_masuk", "jam_pulang", "zona", "provinsi", "kabupaten",
                        "jenis", "source_format", "raw_json", "file_sha256",
                    }
                    if _table_exists(conn, "raw_import"):
                        existing = {row[0].lower() for row in conn.execute('DESCRIBE "raw_import"').fetchall()}
                        if existing != canonical_columns:
                            legacy_count = int(conn.execute('SELECT count(*) FROM "raw_import"').fetchone()[0])
                            legacy_periods = _legacy_raw_import_periods(conn) if legacy_count else set()
                            if legacy_periods - {selected}:
                                raise ImportEngineError(
                                    "RAW_IMPORT_SCHEMA_CONFLICT",
                                    "raw_import legacy memuat periode lain; schema tidak diganti agar period isolation tetap aman.",
                                )
                            conn.execute('DROP TABLE "raw_import"')
                    if not _table_exists(conn, "raw_import"):
                        conn.execute("""
                            CREATE TABLE raw_import (
                              workflow_id VARCHAR,import_id VARCHAR,source_row BIGINT,nip VARCHAR,nama VARCHAR,
                              jabatan VARCHAR,tanggal VARCHAR,tanggal_pulang VARCHAR,jam_masuk VARCHAR,jam_pulang VARCHAR,
                              zona VARCHAR,provinsi VARCHAR,kabupaten VARCHAR,jenis VARCHAR,source_format VARCHAR,
                              raw_json VARCHAR,file_sha256 VARCHAR
                            )
                        """)
                    previous = conn.execute(
                        "SELECT import_id FROM absence_import_batch WHERE period=? AND status='COMMITTED' ORDER BY completed_at DESC LIMIT 1",
                        [selected],
                    ).fetchone()
                    conn.execute("DELETE FROM raw_import WHERE substr(tanggal,1,7)=?", [selected])
                    workflow_id = f"backend-import-{import_id}"
                    conn.execute("""
                        INSERT INTO raw_import
                        SELECT ?,?,source_row,nip,nama,jabatan,tanggal,tanggal_pulang,jam_masuk,jam_pulang,
                               zona,provinsi,kabupaten,jenis,source_format,raw_json,?
                        FROM absence_import_staging
                        WHERE import_id=? AND period=? AND valid
                        ORDER BY source_row
                    """, [workflow_id, import_id, batch["file_sha256"], import_id, selected])
                    inserted = conn.execute("SELECT count(*) FROM raw_import WHERE import_id=?", [import_id]).fetchone()[0]
                    self._persist_workflow_config(conn, batch, selected, workflow_id, inserted)
                    conn.execute("""
                        UPDATE absence_import_batch SET status='COMMITTED',period=?,valid_rows=?,
                          predecessor_import_id=?,progress=100,completed_at=now(),updated_at=now(),
                          timing_json=? WHERE import_id=?
                    """, [selected, inserted, previous[0] if previous else None,
                          _json({**(batch.get("timings") or {}), "commit_ms": (time.perf_counter() - started) * 1000}), import_id])
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    code = exc.code if isinstance(exc, ImportEngineError) else "PROMOTION_FAILED"
                    message = exc.message if isinstance(exc, ImportEngineError) else str(exc)
                    conn.execute("""
                        UPDATE absence_import_batch
                        SET status='FAILED',error_code=?,error_message=?,completed_at=now(),updated_at=now()
                        WHERE import_id=?
                    """, [code, message[:2000], import_id])
                    if isinstance(exc, ImportEngineError):
                        raise
                    raise ImportEngineError("PROMOTION_FAILED", message, 500) from exc
        return self.get(import_id)

    def _persist_workflow_config(self, conn: duckdb.DuckDBPyConnection, batch: dict[str, Any],
                                 period: str, workflow_id: str, row_count: int) -> None:
        supplied = batch.get("workflow_config_json")
        config = json.loads(supplied) if supplied else None
        if config is None and _table_exists(conn, "workflow_config"):
            cursor = conn.execute("SELECT * FROM workflow_config LIMIT 1")
            existing = cursor.fetchone()
            if existing:
                config = dict(zip([item[0] for item in cursor.description], existing))
                for key, value in list(config.items()):
                    if isinstance(value, str) and value[:1] in "[{":
                        try:
                            config[key] = json.loads(value)
                        except json.JSONDecodeError:
                            pass
        if not isinstance(config, dict):
            raise ImportEngineError(
                "WORKFLOW_CONFIG_REQUIRED",
                "workflow_config canonical belum ada; upload harus membawa konfigurasi workflow aktif.",
            )
        workflow = config.get("_workflow")
        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except json.JSONDecodeError:
                workflow = {}
        workflow = dict(workflow or {})
        import_stats = workflow.get("importStats")
        import_stats = dict(import_stats) if isinstance(import_stats, dict) else {}
        # The uploaded config can belong to the previously active Legacy
        # workflow.  Never publish its row counts/months for the new backend
        # import: calculation-input treats these fields as the completeness
        # manifest for the canonical raw_import rows committed below.
        import_stats.update({
            "totalRaw": row_count,
            "totalRecord": row_count,
            "rejectedRows": int(batch.get("invalid_rows") or 0),
            "valid": row_count,
            "bermasalah": int(batch.get("invalid_rows") or 0),
            "months": [period],
        })
        workflow.update({
            "month": period, "workflow_id": workflow_id, "timeMode": workflow.get("timeMode") or "original",
            "importMode": batch["source_format"], "importStatus": "COMPLETE", "import_id": batch["import_id"],
            "raw_count": row_count, "file_sha256": batch["file_sha256"], "importStats": import_stats,
        })
        config["_workflow"] = workflow
        keys = list(config)
        conn.execute("DROP TABLE IF EXISTS _absence_import_config")
        projection = ",".join(f"? AS \"{key.replace(chr(34), chr(34) * 2)}\"" for key in keys)
        values = [_json(config[key]) if isinstance(config[key], (dict, list)) else config[key] for key in keys]
        conn.execute(f"CREATE TEMP TABLE _absence_import_config AS SELECT {projection}", values)
        conn.execute("CREATE OR REPLACE TABLE workflow_config AS SELECT * FROM _absence_import_config")

    def get(self, import_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            ensure_absence_import_schema(conn)
            cursor = conn.execute("SELECT * FROM absence_import_batch WHERE import_id=?", [import_id])
            row = cursor.fetchone()
            if not row:
                raise KeyError(import_id)
            result = {item[0]: _json_value(value) for item, value in zip(cursor.description, row)}
        for source, target, fallback in (("periods_json", "periods", []), ("timing_json", "timings", {})):
            try:
                result[target] = json.loads(result.get(source) or "null") or fallback
            except json.JSONDecodeError:
                result[target] = fallback
        result["ready_for_calculation"] = result["status"] == "COMMITTED"
        if result["status"] == "ALREADY_COMMITTED" and result.get("duplicate_of_import_id"):
            authoritative = self.get(str(result["duplicate_of_import_id"]))
            result["authoritative_import"] = {
                key: authoritative.get(key) for key in (
                    "import_id", "status", "period", "source_format", "pair_cross_day",
                    "total_rows", "valid_rows", "invalid_rows", "duplicate_rows",
                    "invalid_time_rows", "cross_day_rows", "timings",
                )
            }
        return result

    def use_committed(self, import_id: str) -> dict[str, Any]:
        batch = self.get(import_id)
        if batch["status"] == "ALREADY_COMMITTED":
            batch = self.get(str(batch.get("duplicate_of_import_id") or ""))
        if batch["status"] != "COMMITTED":
            raise ImportEngineError("IMPORT_NOT_COMMITTED", "Hasil import belum COMMITTED", 409)
        with self._connect() as conn:
            active = self._active_workflow(conn)
        if str(active.get("import_id") or "") != str(batch["import_id"]):
            raise ImportEngineError(
                "COMMITTED_IMPORT_NOT_ACTIVE",
                "Hasil import historis ini bukan workflow authoritative yang aktif.",
                409,
            )
        return batch

    def reset_period_preview(self, import_id: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            ensure_absence_import_schema(conn)
            active = self._active_workflow(conn)
            active_import_id = str(active.get("import_id") or "").strip()
            workflow_id = str(active.get("workflow_id") or "").strip()
            period = str(active.get("month") or "").strip()
            if not active_import_id or not workflow_id or not period:
                raise ImportEngineError("NO_ACTIVE_IMPORT", "Tidak ada workflow import aktif untuk direset", 409)
            if import_id and import_id not in {active_import_id, workflow_id}:
                raise ImportEngineError("WORKFLOW_SCOPE_MISMATCH", "Import bukan workflow aktif", 409)
            batch = self.get(active_import_id)
            if batch.get("source_format") != CSV_EABSENSI_SOURCE_FORMAT:
                raise ImportEngineError("CSV_WORKFLOW_REQUIRED", "Reset ini hanya untuk CSV eAbsensi aktif", 409)
            raw_count = int(conn.execute(
                "SELECT count(*) FROM raw_import WHERE workflow_id=?", [workflow_id]
            ).fetchone()[0]) if _table_exists(conn, "raw_import") else 0
            derived = {}
            for table in ATTENDANCE_DERIVED_TABLES:
                columns = self._table_columns(conn, table)
                if not columns:
                    continue
                lower = {name.lower(): name for name in columns}
                if "workflow_id" in lower:
                    count = conn.execute(
                        f'SELECT count(*) FROM "{table}" WHERE cast("{lower["workflow_id"]}" AS VARCHAR)=?',
                        [workflow_id],
                    ).fetchone()[0]
                elif "bulan" in lower:
                    count = conn.execute(
                        f'SELECT count(*) FROM "{table}" WHERE cast("{lower["bulan"]}" AS VARCHAR)=?',
                        [period],
                    ).fetchone()[0]
                else:
                    count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                derived[table] = int(count)
        return {
            "status": "READY", "confirmation": "RESET", "import_id": active_import_id,
            "workflow_id": workflow_id, "period": period, "source_format": batch["source_format"],
            "pair_cross_day": bool(batch.get("pair_cross_day")), "raw_import": raw_count,
            "derived": derived,
        }

    def reset_period(
        self, import_id: str, confirmation: str, *, acquire_gate: bool = True,
        before_commit: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
    ) -> dict[str, Any]:
        if str(confirmation or "").strip().upper() != "RESET":
            raise ImportEngineError("RESET_CONFIRMATION_REQUIRED", "Ketik RESET untuk melanjutkan", 400)
        preview = self.reset_period_preview(import_id)
        context = self.write_gate if acquire_gate else nullcontext()
        with context:
            generation = begin_runtime_reset(self.db_path)
            try:
                with self._connect() as conn:
                    conn.execute("BEGIN TRANSACTION")
                    try:
                        active = self._active_workflow(conn)
                        if (str(active.get("import_id") or "") != preview["import_id"]
                                or str(active.get("workflow_id") or "") != preview["workflow_id"]
                                or str(active.get("month") or "") != preview["period"]):
                            raise ImportEngineError("WORKFLOW_CHANGED", "Workflow aktif berubah; reset dibatalkan", 409)
                        deleted: dict[str, int] = {}
                        if _table_exists(conn, "raw_import"):
                            conn.execute("DELETE FROM raw_import WHERE workflow_id=?", [preview["workflow_id"]])
                            deleted["raw_import"] = preview["raw_import"]
                        for table in ATTENDANCE_DERIVED_TABLES:
                            columns = self._table_columns(conn, table)
                            if not columns:
                                continue
                            lower = {name.lower(): name for name in columns}
                            if "workflow_id" in lower:
                                conn.execute(
                                    f'DELETE FROM "{table}" WHERE cast("{lower["workflow_id"]}" AS VARCHAR)=?',
                                    [preview["workflow_id"]],
                                )
                            elif "bulan" in lower:
                                conn.execute(
                                    f'DELETE FROM "{table}" WHERE cast("{lower["bulan"]}" AS VARCHAR)=?',
                                    [preview["period"]],
                                )
                            else:
                                # These legacy tables are replace-only projections of the
                                # singleton active workflow, not multi-workflow archives.
                                conn.execute(f'DELETE FROM "{table}"')
                            deleted[table] = int(preview["derived"].get(table, 0))
                        conn.execute("DELETE FROM workflow_config")
                        conn.execute(
                            "UPDATE absence_import_batch SET status='RESET',error_code=NULL,"
                            "error_message=NULL,updated_at=now() WHERE import_id=?",
                            [preview["import_id"]],
                        )
                        if before_commit:
                            before_commit(conn)
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                return {**preview, "status": "RESET", "deleted": deleted,
                        "reset_generation": generation}
            finally:
                finish_runtime_reset(self.db_path)

    def preview(self, import_id: str, page: int = 1, page_size: int = 50, *, errors_only: bool = False) -> dict[str, Any]:
        page_size = min(100, max(1, int(page_size)))
        page = max(1, int(page))
        where = "import_id=?" + (" AND severity IN ('BLOCKER','WARNING')" if errors_only else "")
        with self._connect() as conn:
            ensure_absence_import_schema(conn)
            total = conn.execute(f"SELECT count(*) FROM absence_import_staging WHERE {where}", [import_id]).fetchone()[0]
            cursor = conn.execute(f"""
                SELECT source_row,nip,nama,jabatan,tanggal,tanggal_pulang,jam_masuk,jam_pulang,zona,
                       source_format,cross_day,duplicate_row,valid,severity,error_codes
                FROM absence_import_staging WHERE {where}
                ORDER BY source_row LIMIT ? OFFSET ?
            """, [import_id, page_size, (page - 1) * page_size])
            rows = [{item[0]: _json_value(value) for item, value in zip(cursor.description, values)}
                    for values in cursor.fetchall()]
        for row in rows:
            row["error_codes"] = json.loads(row.get("error_codes") or "[]")
        return {"import_id": import_id, "page": page, "page_size": page_size, "total": int(total), "rows": rows}

    def cancel(self, import_id: str, *, acquire_gate: bool = True) -> dict[str, Any]:
        context = self.write_gate if acquire_gate else nullcontext()
        with context, self._connect() as conn:
            ensure_absence_import_schema(conn)
            status = conn.execute("SELECT status FROM absence_import_batch WHERE import_id=?", [import_id]).fetchone()
            if not status:
                raise KeyError(import_id)
            if status[0] == "COMMITTED":
                raise ImportEngineError("COMMITTED_IMPORT_CANNOT_CANCEL", "Import yang sudah canonical tidak dapat dibatalkan")
            conn.execute("UPDATE absence_import_batch SET status='CANCELLED',completed_at=now(),updated_at=now() WHERE import_id=?", [import_id])
        return self.get(import_id)


def create_router(db_path_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/api/absence-import", tags=["absence-import"])

    def service() -> AbsenceImportService:
        return AbsenceImportService(db_path_provider())

    def failure(error: ImportEngineError) -> HTTPException:
        return HTTPException(error.status_code, {"code": error.code, "message": error.message})

    @router.get("/authority")
    def authority() -> dict[str, Any]:
        return service().authority_info()

    @router.get("/routing")
    def routing(source_format: str = Query(...)) -> dict[str, Any]:
        try:
            engine = service().authority_config.route(source_format)
            return {"source_format": source_format.lower(), "engine": engine.value,
                    "explicit": True, "silent_fallback": False}
        except ImportEngineError as exc:
            raise failure(exc) from exc

    @router.post("/upload")
    def upload(
        file: UploadFile = File(...), operator: str = Form(...), source_format: str = Form("standard"),
        period: str | None = Form(None), workflow_config_json: str | None = Form(None),
        pair_cross_day: bool = Form(False),
    ) -> dict[str, Any]:
        try:
            active = service()
            route = active.authority_config.route(source_format)
            expected = (
                ImportAuthority.CSV_EABSENSI_SPECIAL
                if str(source_format).lower() == CSV_EABSENSI_SOURCE_FORMAT
                else ImportAuthority.IMPORT_BACKEND_V1
            )
            if route != expected:
                code = "WALI_LEGACY_REQUIRED" if str(source_format).lower() == "wali" else "STANDARD_BACKEND_NOT_ENABLED"
                message = ("Absensi Wali tetap menggunakan JavaScript Legacy sampai real-world parity tersedia."
                           if code == "WALI_LEGACY_REQUIRED" else
                           "Backend Import V1 belum diaktifkan untuk standard import pada environment ini.")
                raise ImportEngineError(code, message, 409)
            item = active.save_upload(file, operator=operator, source_format=source_format,
                                         period=period, workflow_config_json=workflow_config_json,
                                         pair_cross_day=pair_cross_day)
            return active.submit_stage(item["import_id"])
        except ImportEngineError as exc:
            raise failure(exc) from exc

    @router.get("/{import_id}")
    def status(import_id: str) -> dict[str, Any]:
        try:
            return service().get(import_id)
        except KeyError as exc:
            raise HTTPException(404, "Import batch tidak ditemukan") from exc

    @router.get("/{import_id}/preview")
    def preview(import_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
        return service().preview(import_id, page, page_size)

    @router.get("/{import_id}/errors")
    def errors(import_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
        return service().preview(import_id, page, page_size, errors_only=True)

    @router.post("/{import_id}/commit")
    def commit(import_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        try:
            # Global HTTP middleware already owns DATABASE_GATE for this request.
            return service().commit(import_id, period=payload.get("period"),
                                    accept_invalid=bool(payload.get("accept_invalid")), acquire_gate=False)
        except ImportEngineError as exc:
            raise failure(exc) from exc

    @router.post("/{import_id}/use-committed")
    def use_committed(import_id: str) -> dict[str, Any]:
        try:
            return service().use_committed(import_id)
        except (ImportEngineError, KeyError) as exc:
            if isinstance(exc, ImportEngineError):
                raise failure(exc) from exc
            raise HTTPException(404, "Import batch tidak ditemukan") from exc

    @router.get("/{import_id}/reset-period/preview")
    def reset_period_preview(import_id: str) -> dict[str, Any]:
        try:
            return service().reset_period_preview(import_id)
        except ImportEngineError as exc:
            raise failure(exc) from exc

    @router.post("/{import_id}/reset-period")
    def reset_period(import_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            # Global HTTP middleware already owns DATABASE_GATE for this request.
            return service().reset_period(
                import_id, str((payload or {}).get("confirmation") or ""), acquire_gate=False,
            )
        except ImportEngineError as exc:
            raise failure(exc) from exc

    @router.post("/{import_id}/cancel")
    def cancel(import_id: str) -> dict[str, Any]:
        try:
            return service().cancel(import_id, acquire_gate=False)
        except ImportEngineError as exc:
            raise failure(exc) from exc

    return router


def shutdown_import_executor() -> None:
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)

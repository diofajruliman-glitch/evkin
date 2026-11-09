from __future__ import annotations

import importlib
import io
import json
import logging
import re
import sqlite3
import sys
import tempfile
import os
import shutil
from pathlib import Path
from typing import Any, Optional, List

from backend.lazy_imports import lazy_module

pd = lazy_module("pandas")
import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uuid
import json
from datetime import datetime
import concurrent.futures
import threading
from backend.runtime_guard import (
    DATABASE_GATE,
    StaleGenerationError,
    assert_current_generation,
    claim_database,
    current_reset_generation,
    database_job,
    initialize_runtime_state,
    serialize_database_requests,
)
from backend.final_integrity import final_manifest, assert_manifest, verify_final_xlsx
from backend.workflow_readiness import record_verification
from backend.security import ApiKeyMiddleware, CORS_HEADERS, CORS_METHODS, allowed_origins, configured_api_key
from backend.schema_sync import SchemaMismatchError, create_table_from_relation, insert_relation_by_name
from backend.reset_service import RESET_PROFILES, ResetService

# Executor for background compute jobs
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='evkin-jobs')
# Keep futures to allow cancellation from the main process
_job_futures: dict = {}
_workflow_read_base = None
_workflow_read_path = None
_workflow_read_lock = threading.RLock()
logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_ROOT / "assets"
INDEX_FILE = APP_ROOT / "index.html"

app = FastAPI(title="EVKIN Export API", version="1.1.0")
app.middleware('http')(serialize_database_requests)
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=CORS_METHODS,
    allow_headers=CORS_HEADERS,
)

@app.middleware("http")
async def no_cache_frontend(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path == "/index.html" or path.startswith("/assets/") or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Try to mount analytics app (backend.main) under /analytics if available
try:
    analytics_candidates = ['backend.main', 'main']
    analytics_mod = None
    for candidate in analytics_candidates:
        try:
            analytics_mod = importlib.import_module(candidate)
            break
        except ModuleNotFoundError:
            continue
    if analytics_mod and hasattr(analytics_mod, 'app'):
        app.mount('/analytics', analytics_mod.app)
        print('Mounted analytics app at /analytics')
except Exception as e:
    # Non-fatal if analytics module is absent; print to server logs
    print('Analytics app not mounted:', e)


def _resolve_self_module():
    for candidate in ('backend.evkin_export_api', 'evkin_export_api'):
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError:
            continue
    return sys.modules.get(__name__)


# Backwards-compatibility safety: some deployments / reloads may import the module in a way
# that later route definitions (jobs/export) are not present in OpenAPI. Register proxy job
# routes only if they are not already registered. These proxies forward to the actual
# implementation (defined later in this module) at request time, avoiding circular import
# issues while ensuring the endpoints are present for the frontend.

def _route_exists(path: str, method: str | None = None) -> bool:
    for r in app.routes:
        p = getattr(r, 'path', None)
        m = getattr(r, 'methods', None)
        if p == path:
            if method is None:
                return True
            if m and method.upper() in m:
                return True
    return False

if not _route_exists('/api/jobs/export', 'POST'):
    @app.post('/api/jobs/export')
    def create_export_job_proxy(payload: dict) -> dict:
        mod = _resolve_self_module()
        return getattr(mod, 'create_export_job')(payload)

if not _route_exists('/api/jobs/{job_id}', 'GET'):
    @app.get('/api/jobs/{job_id}')
    def api_get_job_proxy(job_id: str):
        mod = _resolve_self_module()
        return getattr(mod, 'api_get_job')(job_id)

if not _route_exists('/api/jobs', 'GET'):
    @app.get('/api/jobs')
    def api_list_jobs_proxy(limit: int = Query(50, ge=1, le=1000)):
        mod = _resolve_self_module()
        return getattr(mod, 'api_list_jobs')(limit)

if not _route_exists('/api/jobs/{job_id}/download', 'GET'):
    @app.get('/api/jobs/{job_id}/download')
    def api_download_job_result_proxy(job_id: str):
        mod = _resolve_self_module()
        return getattr(mod, 'api_download_job_result')(job_id)

if not _route_exists('/api/jobs/{job_id}/cancel', 'POST'):
    @app.post('/api/jobs/{job_id}/cancel')
    def api_cancel_job_proxy(job_id: str):
        mod = _resolve_self_module()
        return getattr(mod, 'api_cancel_job')(job_id)

if not _route_exists('/api/jobs/{job_id}/retry', 'POST'):
    @app.post('/api/jobs/{job_id}/retry')
    def api_retry_job_proxy(job_id: str):
        mod = _resolve_self_module()
        return getattr(mod, 'api_retry_job')(job_id)

STORAGE_DIR = Path(os.environ.get('EVKIN_STORAGE_DIR') or Path(__file__).resolve().parent)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(STORAGE_DIR / 'evkin.db')
# Use EVKIN_MASTER.duckdb in backend/data as the default master DB. Honor EVKIN_DUCKDB_PATH env var if set.
DUCKDB_PATH = os.environ.get('EVKIN_DUCKDB_PATH') or str(Path(__file__).resolve().parent / 'data' / 'EVKIN_MASTER.duckdb')  # primary workflow DB for heavy transforms

# Mount after DUCKDB_PATH is initialized because the legacy pengembalian
# module imports that setting while defining its compatibility helpers.

# Modul pengelolaan Tukin tahunan memakai DuckDB yang sama dengan workflow
# absensi agar hasil final aktif dapat ditangkap tanpa ekspor/impor ulang.
try:
    tukin_annual = importlib.import_module("backend.tukin_annual")
    app.include_router(tukin_annual.create_router(lambda: DUCKDB_PATH))
except Exception as exc:
    print("API Pengelolaan Tukin Tahunan tidak terpasang:", exc)

# Satu Master SDM bersama untuk workflow absensi, SKP, dan final Tukin.
# Tabel master_data tetap diproyeksikan agar modul lama tidak perlu dirombak sekaligus.
try:
    shared_master = importlib.import_module("backend.shared_master")
    app.include_router(shared_master.create_router(lambda: DUCKDB_PATH))
except Exception as exc:
    shared_master = None
    print("API Master SDM Bersama tidak terpasang:", exc)

# Workflow khusus Penilaian SKP: preview, klasifikasi massal, review,
# persetujuan, dokumentasi, dan rekap tahunan.
try:
    skp_assessment = importlib.import_module("backend.skp_assessment")
    app.include_router(skp_assessment.create_router(lambda: DUCKDB_PATH))
except Exception as exc:
    print("API Penilaian SKP tidak terpasang:", exc)
EXCEL_CELL_TEXT_LIMIT = 32767
from backend.tukin_combined import create_router as create_combined_router
app.include_router(create_combined_router(lambda: DUCKDB_PATH))
from backend.final_tukin import create_router as create_final_tukin_router
app.include_router(create_final_tukin_router(lambda: DUCKDB_PATH))
from backend.tukin_reconciliation import create_router as create_tukin_reconciliation_router
app.include_router(create_tukin_reconciliation_router(lambda: DUCKDB_PATH))
from backend.application_reset import create_router as create_reset_router
app.include_router(create_reset_router(lambda: DUCKDB_PATH, lambda: DB_PATH))
from backend.module_reset import create_router as create_module_reset_router
app.include_router(create_module_reset_router(lambda: DUCKDB_PATH))
from backend.absence_cutover import (
    create_router as create_absence_calculation_router,
    current_authority_config,
    recover_interrupted_batches,
    shutdown_calculation_executor,
)
app.include_router(create_absence_calculation_router(lambda: DUCKDB_PATH))
from backend.absence_import import (
    create_router as create_absence_import_router,
    recover_interrupted_imports,
    shutdown_import_executor,
)
app.include_router(create_absence_import_router(lambda: DUCKDB_PATH))
EXCEL_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
EXPORT_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ExportValidationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)
WORKFLOW_TABLES = {
    "config": "workflow_config",
    "master_data": "master_data",
    "raw_import": "raw_import",
    "processed_data": "workflow_records",
    "daily_data": "hitung_harian",
    "rekap_sdm": "rekap_sdm",
    "final_absensi": "final_absensi",
    "semifinal": "anomali",
    "final_skp": "final_skp",
    "final_kombinasi": "final_kombinasi",
    "final_combined_skp": "final_combined_skp",
}
# Derived workflow data only. Master SDM, workflow configuration, and imported
# source data are intentionally excluded from backend workflow reset.
RESETTABLE_WORKFLOW_TABLES = RESET_PROFILES['workflow_results'].tables

# Canonical SDM and archived Tukin are owned by their dedicated services.
# The legacy master_data projection is read-only to workflow clients.
WORKFLOW_WRITABLE = {key: table for key, table in WORKFLOW_TABLES.items() if key != 'master_data'}

# These fields are created conditionally by calc.js.  Normalizing them for
# every daily batch gives chunked sync one stable schema without inventing a
# business fallback: False/NULL are the same values used when the condition
# did not apply.
WORKFLOW_OPTIONAL_COLUMNS = {
    'daily_data': {'excused': False, '_lateMin': None, '_earlyMin': None},
}


def workflow_rows(source: str, value: Any) -> list:
    if source == 'config':
        if isinstance(value, list):
            if len(value) > 1:
                raise HTTPException(400, 'Konfigurasi workflow harus satu objek')
            value = value[0] if value else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise HTTPException(400, 'JSON konfigurasi tidak valid') from exc
        if value is None:
            return []
        if not isinstance(value, dict):
            raise HTTPException(400, 'Konfigurasi workflow harus berupa objek')
        return [{key: item for key, item in value.items() if key != 'masterPegawai'}]
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HTTPException(400, 'Data workflow harus berupa daftar objek')
    return value


def prepare_workflow_frame(source: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column, default in WORKFLOW_OPTIONAL_COLUMNS.get(source, {}).items():
        if column not in frame.columns:
            frame[column] = default
    frame = prepare_database_frame(frame)
    if source == 'raw_import':
        # Raw spreadsheet/CSV columns are intentionally schema-less.  Inferring
        # their DuckDB types per HTTP chunk is unsafe: an early batch containing
        # only numbers creates an INTEGER column, while a later blank/text value
        # then fails to append and leaves a partial canonical import.  Persist
        # every raw cell as text so every batch has one stable representation;
        # the browser importer performs the actual date/time/number parsing.
        for column in frame.columns:
            frame[column] = frame[column].map(
                lambda value: None if value is None or (
                    isinstance(value, float) and pd.isna(value)
                ) else str(value)
            ).astype('string')
    return frame


WORKFLOW_STRUCTURED_FIELDS = {
    'flags', 'issues', 'tanggal', 'tanggalMasukObj', 'tanggalPulangObj'
}
WORKFLOW_CONFIG_STRUCTURED_FIELDS = {
    'jamDasar', 'flexi', 'holidays', 'jabatanMaster', 'keterlambatanTiers',
    'pulangCepatTiers', 'jenisNoPotongan', 'leavePeriods', '_workflow'
}
WORKFLOW_PERIOD_COLUMNS = (
    'bulan', 'monthkey', 'periode', 'periode_id', 'assessment_period',
    'tanggalkey', 'tanggal'
)


@app.exception_handler(SchemaMismatchError)
async def workflow_schema_mismatch_handler(_request, exc: SchemaMismatchError):
    logger.error('Workflow persistence schema mismatch',
                 exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=409, content={'detail': {
        'code': 'WORKFLOW_PERSISTENCE_SCHEMA_MISMATCH',
        'message': 'Penyimpanan impor gagal karena struktur antar-batch tidak konsisten.',
        'stage': 'IMPORT_PERSISTENCE',
    }})
WORKFLOW_ID_COLUMNS = ('workflow_id', 'batch_id', 'import_batch_id', 'calculation_batch_id')


def decode_workflow_rows(source: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty or set(frame.columns) == {'_empty'}:
        return []
    rows = frame.to_dict(orient='records')
    structured_fields = WORKFLOW_CONFIG_STRUCTURED_FIELDS if source == 'config' else WORKFLOW_STRUCTURED_FIELDS
    for row in rows:
        for field in structured_fields:
            value = row.get(field)
            if isinstance(value, str) and value[:1] in ('[', '{'):
                try:
                    row[field] = json.loads(value)
                except ValueError:
                    pass
    return rows


def workflow_config_metadata(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep startup configuration small without changing the stored legacy shape."""
    if not config:
        return None
    result = json.loads(json.dumps(config, ensure_ascii=False, default=str))
    result.pop('masterPegawai', None)
    workflow = result.get('_workflow')
    if isinstance(workflow, dict):
        for key, row_keys in {
            'finalSKP': ('rows',),
            'finalKombinasi': ('rows',),
            'finalCombinedSKP': ('rows', 'skpRows', 'eabsensiRows', 'esdmRows', 'combinedRows'),
        }.items():
            value = workflow.get(key)
            if isinstance(value, dict):
                workflow[key] = {name: item for name, item in value.items() if name not in row_keys}
    return result


def workflow_table_columns(connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()]
    except Exception:
        return []


def read_workflow_config(connection) -> dict[str, Any] | None:
    """Read the one active workflow manifest, including legacy JSON-only rows."""
    try:
        cursor = connection.execute('SELECT * FROM "workflow_config" LIMIT 1')
        values = cursor.fetchone()
        if values is None:
            return None
        config = dict(zip((column[0] for column in cursor.description), values))
        if len(config) == 1:
            only_value = next(iter(config.values()))
            if isinstance(only_value, str) and only_value[:1] == '{':
                parsed = json.loads(only_value)
                if isinstance(parsed, dict):
                    config = parsed
        for field in WORKFLOW_CONFIG_STRUCTURED_FIELDS:
            value = config.get(field)
            if isinstance(value, str) and value[:1] in ('[', '{'):
                try:
                    config[field] = json.loads(value)
                except ValueError:
                    pass
        return config
    except (duckdb.Error, ValueError, TypeError):
        return None


def workflow_scope_sql(
    source: str,
    columns: list[str],
    periode: str | None,
    workflow_id: str | None,
) -> tuple[str, list[str]]:
    lower_columns = {column.lower(): column for column in columns}
    clauses: list[str] = []
    params: list[str] = []
    requested_workflow = str(workflow_id or '').strip()
    workflow_column = next((lower_columns[name] for name in WORKFLOW_ID_COLUMNS if name in lower_columns), None)
    if requested_workflow and workflow_column:
        clauses.append(f'CAST("{workflow_column}" AS VARCHAR) = ?')
        params.append(requested_workflow)
    # Legacy workflow tables contain one replace-only active dataset and do not
    # have a workflow column. Their scope is the active workflow_config manifest;
    # the dedicated calculation endpoint validates that manifest explicitly.

    requested_period = str(periode or '').strip()
    if requested_period:
        period_column = next((lower_columns[name] for name in WORKFLOW_PERIOD_COLUMNS if name in lower_columns), None)
        if period_column:
            if period_column.lower() in {'tanggal', 'tanggalkey'}:
                clauses.append(
                    f'(substr(CAST("{period_column}" AS VARCHAR), 1, 7) = ? OR '
                    f"strftime(try_strptime(CAST(\"{period_column}\" AS VARCHAR), '%d/%m/%Y'), '%Y-%m') = ?)"
                )
                params.append(requested_period)
            else:
                clauses.append(f'CAST("{period_column}" AS VARCHAR) = ?')
            params.append(requested_period)
        elif source == 'processed_data' and 'record' in lower_columns:
            record_column = lower_columns['record']
            clauses.append(
                f"coalesce(try(json_extract_string(\"{record_column}\", '$.tanggal.monthKey')), "
                f"try(json_extract_string(\"{record_column}\", '$.monthKey')), "
                f"substr(try(json_extract_string(\"{record_column}\", '$.tanggalKey')), 1, 7)) = ?"
            )
            params.append(requested_period)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


WORKFLOW_LIST_COLUMNS = {
    'raw_import': ('nip', 'nama', 'jabatan', 'tanggal', 'bulan', 'unit', 'jenis',
                   'batch_id', 'source_file', 'source_row', 'imported_at'),
    'daily_data': ('nip', 'nama', 'jabatan', 'bulan', 'tanggal', 'jamMasuk',
                   'jamPulang', 'durasi', 'metode', 'jenis', 'statusLengkap',
                   'totalHarian', 'flags'),
    'rekap_sdm': ('nip', 'nama', 'jabatan', 'bulan', 'jumlahRecord',
                  'hariEfektif', 'hariAdaAbsensi', 'hariKurang', 'jumlahHariAbsen',
                  'total', 'status'),
    'final_absensi': ('nip', 'nama', 'jabatan', 'bulan', 'status', 'tukin',
                      'tukinbruto', 'tukin_gross', 'potPct', 'nominal',
                      'finalDibayar', 'final_tukin_dibayar'),
}


def _workflow_filtered_sql(source: str, columns: list[str], periode: str | None,
                           workflow_id: str | None, search: str | None,
                           filter_value: str | None) -> tuple[str, list[Any]]:
    where_sql, params = workflow_scope_sql(source, columns, periode, workflow_id)
    clauses = [where_sql[7:]] if where_sql else []
    lower = {column.lower(): column for column in columns}
    needle = str(search or '').strip()
    if needle:
        candidates = [lower[name] for name in
                      ('nip', 'nama', 'jabatan', 'status', 'bulan', 'periode', 'unit')
                      if name in lower]
        if source == 'processed_data' and 'record' in lower:
            record = lower['record']
            candidates.extend([
                f"try(json_extract_string(\"{record}\", '$.nip'))",
                f"try(json_extract_string(\"{record}\", '$.nama'))",
                f"try(json_extract_string(\"{record}\", '$.jabatan'))",
            ])
            clauses.append('(' + ' OR '.join(f'lower(coalesce(CAST({item} AS VARCHAR), \'\')) LIKE ?'
                                               for item in candidates) + ')')
        elif candidates:
            clauses.append('(' + ' OR '.join(f'lower(coalesce(CAST("{item}" AS VARCHAR), \'\')) LIKE ?'
                                               for item in candidates) + ')')
        params.extend([f'%{needle.lower()}%'] * len(candidates))
    raw_filter = str(filter_value or '').strip()
    if raw_filter:
        try:
            parsed = json.loads(raw_filter) if raw_filter.startswith('{') else {
                raw_filter.split(':', 1)[0]: raw_filter.split(':', 1)[1]
            }
        except (ValueError, IndexError, json.JSONDecodeError) as exc:
            raise HTTPException(400, 'Filter harus JSON object atau column:value') from exc
        if not isinstance(parsed, dict):
            raise HTTPException(400, 'Filter harus berupa object')
        for requested, value in parsed.items():
            column = lower.get(str(requested).lower())
            if not column:
                raise HTTPException(400, f'Kolom filter tidak dikenal: {requested}')
            clauses.append(f'CAST("{column}" AS VARCHAR) = ?')
            params.append(str(value))
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


def _workflow_list_projection(source: str, columns: list[str]) -> str:
    wanted = WORKFLOW_LIST_COLUMNS.get(source)
    if not wanted:
        return '*'
    lower = {column.lower(): column for column in columns}
    selected = [f'"{lower[name.lower()]}"' for name in wanted if name.lower() in lower]
    return ','.join(selected) if selected else '*'


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT,
            status TEXT,
            payload TEXT,
            progress REAL,
            message TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_table TEXT,
            row_count INTEGER
        )
        """
    )
    conn.commit()


# Ensure jobs table exists at startup
_conn_for_jobs_init = get_connection()
try:
    ensure_jobs_table(_conn_for_jobs_init)
finally:
    _conn_for_jobs_init.close()


def ensure_duckdb_master_schema() -> dict:
    """Create workflow schema without removing locks or moving a live WAL."""
    Path(DUCKDB_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
    dconn = duckdb.connect(DUCKDB_PATH)
    try:
        for table in WORKFLOW_TABLES.values():
            dconn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (_empty TEXT)')
        dconn.execute('CREATE TABLE IF NOT EXISTS workflow_flags (k TEXT PRIMARY KEY, verified INTEGER, updated_at TEXT)')
        dconn.execute('CREATE TABLE IF NOT EXISTS workflow_meta (table_name TEXT PRIMARY KEY, row_count INTEGER)')
        dconn.commit()
    finally:
        dconn.close()
    return {"status": "ok", "duckdb": DUCKDB_PATH, "tables": list(WORKFLOW_TABLES.values())}


STARTUP_SCHEMA_TABLES = frozenset(WORKFLOW_TABLES.values()) | {
    'workflow_flags', 'workflow_meta', 'final_tukin_schema', 'master_skp_rules',
    'skp_import_batches', 'skp_results', 'final_tukin_batches',
    'final_tukin_results', 'tukin_calculation_batch',
    'tukin_calculation_result', 'tukin_final_decision',
}
STARTUP_SCHEMA_INDEXES = {
    'idx_tukin_calc_period', 'idx_tukin_calc_result_key', 'idx_tukin_final_period',
}


def startup_schema_is_current(db_path: str | None = None) -> bool:
    """Read-only fast path for the exact schema guaranteed by startup migrations."""
    path = str(db_path or DUCKDB_PATH)
    if not Path(path).is_file():
        return False
    try:
        with duckdb.connect(path, read_only=True) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()}
            if not STARTUP_SCHEMA_TABLES.issubset(tables):
                return False
            batch_columns = {row[1] for row in conn.execute(
                "PRAGMA table_info('skp_import_batches')"
            ).fetchall()}
            if not {'excluded_count', 'validation_report'}.issubset(batch_columns):
                return False
            versions = {int(row[0]) for row in conn.execute(
                'SELECT version FROM final_tukin_schema WHERE version IN (1, 2)'
            ).fetchall()}
            if versions != {1, 2}:
                return False
            indexes = {row[0] for row in conn.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE schema_name='main'"
            ).fetchall()}
            return STARTUP_SCHEMA_INDEXES.issubset(indexes)
    except (duckdb.Error, OSError, ValueError):
        return False


def workflow_read_cursor():
    """Reuse DuckDB catalog state while giving each request an isolated cursor."""
    global _workflow_read_base, _workflow_read_path
    path = str(Path(DUCKDB_PATH).resolve())
    with _workflow_read_lock:
        if _workflow_read_base is None or _workflow_read_path != path:
            if _workflow_read_base is not None:
                _workflow_read_base.close()
            _workflow_read_base = duckdb.connect(path)
            _workflow_read_path = path
        return _workflow_read_base.cursor()


@app.on_event("startup")
def startup_bootstrap() -> None:
    claim_database(DUCKDB_PATH)
    if not startup_schema_is_current():
        ensure_duckdb_master_schema()
        from backend.final_tukin import migrate as migrate_final_tukin
        migrate_final_tukin(DUCKDB_PATH)
    initialize_runtime_state(DUCKDB_PATH)
    recover_interrupted_batches(DUCKDB_PATH)
    recover_interrupted_imports(DUCKDB_PATH)
    with get_connection() as conn:
        conn.execute("UPDATE jobs SET status='failed', message='Backend dimulai ulang. Jalankan kembali proses ini.' WHERE status IN ('queued','running')")


@app.on_event('shutdown')
def shutdown_jobs():
    global _workflow_read_base
    _executor.shutdown(wait=True, cancel_futures=True)
    shutdown_calculation_executor()
    shutdown_import_executor()
    with _workflow_read_lock:
        if _workflow_read_base is not None:
            _workflow_read_base.close()
            _workflow_read_base = None


def create_workflow_indexes(conn: sqlite3.Connection, table: str) -> None:
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    if "bulan" in columns:
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_bulan" ON "{table}" ("bulan")')
    if "unit" in columns:
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_unit" ON "{table}" ("unit")')
    if "nip" in columns:
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_nip" ON "{table}" ("nip")')


def read_workflow_table(conn: sqlite3.Connection, table: str, bulan: str, unit: Optional[str], jabatan: Optional[str] = None) -> pd.DataFrame:
    """Backward-compatible read from SQLite for small workloads. Prefer DuckDB via read_workflow_table_global."""
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not columns or columns == ["_empty"]:
        return pd.DataFrame()
    query = f'SELECT * FROM "{table}"'
    filters = []
    params = []
    if "bulan" in columns:
        filters.append('"bulan" = ?')
        params.append(bulan)
    if unit and "unit" in columns:
        filters.append('"unit" = ?')
        params.append(unit)
    if jabatan and "jabatan" in columns:
        filters.append('"jabatan" = ?')
        params.append(jabatan)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    return pd.read_sql_query(query, conn, params=params)


def read_workflow_table_global(table: str, bulan: str, unit: Optional[str], jabatan: Optional[str] = None) -> pd.DataFrame:
    """Try reading workflow table from DuckDB first, fall back to SQLite.
    Returns a pandas DataFrame.

    Optimized: apply bulan/unit filtering in DuckDB SQL so large tables are filtered server-side.
    """
    # Try DuckDB
    try:
        import duckdb as _d
        dconn = _d.connect(DUCKDB_PATH)
        try:
            # Build SQL with optional filters to let DuckDB do the heavy lifting
            where_clauses = []
            params = []
            # check if table has those columns via pragma; if not, skip filters
            try:
                cols = {r[1] for r in dconn.execute(f"PRAGMA table_info('{table}')").fetchall()}
            except Exception:
                cols = set()
            sql = f'SELECT * FROM "{table}"'
            if 'bulan' in cols and bulan:
                where_clauses.append('bulan = ?')
                params.append(bulan)
            if 'unit' in cols and unit:
                where_clauses.append('unit = ?')
                params.append(unit)
            if 'jabatan' in cols and jabatan:
                where_clauses.append('jabatan = ?')
                params.append(jabatan)
            sql, params = build_export_query(table, bulan, cols, unit, jabatan)
            df = dconn.execute(sql, params).fetchdf()
            return df
        finally:
            dconn.close()
    except Exception:
        # fallback to sqlite
        conn = get_connection()
        try:
            return read_workflow_table(conn, table, bulan, unit, jabatan)
        finally:
            conn.close()


def prepare_excel_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert workflow values to safe, bounded Excel cell values with a lightweight fast path."""
    if frame.empty:
        return frame
    result = frame.copy()
    result.columns = [safe_excel_value(column) for column in result.columns]
    for column in result.columns:
        series = result[column]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
            continue
        result[column] = series.map(safe_excel_value)
    return result


def _write_workbook_stream(output: io.BytesIO, sheets: dict[str, pd.DataFrame]) -> None:
    """Write workbook to a BytesIO using a low-overhead xlsxwriter config for fast export."""
    with pd.ExcelWriter(output, engine='xlsxwriter', engine_kwargs={'options': {'in_memory': True}}) as writer:
        MAX_EXCEL_ROWS = 1_048_575

        def _write_df_split(df: pd.DataFrame, sheet_name: str) -> None:
            base = sheet_name[:28]
            if df.shape[0] <= MAX_EXCEL_ROWS:
                df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
                return
            total_rows = df.shape[0]
            parts = (total_rows + MAX_EXCEL_ROWS - 1) // MAX_EXCEL_ROWS
            for i in range(parts):
                start = i * MAX_EXCEL_ROWS
                end = min((i + 1) * MAX_EXCEL_ROWS, total_rows)
                part_name = f'{base}_{i + 1}'[:31]
                df.iloc[start:end].to_excel(writer, index=False, sheet_name=part_name)

        for sheet_name, df in sheets.items():
            if df is None or df.empty:
                continue
            _write_df_split(df, sheet_name)


def build_export_query(table: str, bulan: str, columns: set[str], unit: Optional[str] = None,
                       jabatan: Optional[str] = None) -> tuple[str, list[str]]:
    if not EXPORT_PERIOD_RE.fullmatch(str(bulan or "")):
        raise ExportValidationError("EXPORT_SOURCE_NOT_READY", f"Periode export tidak valid: {bulan!r}.")
    if table not in set(WORKFLOW_TABLES.values()):
        raise ExportValidationError("EXPORT_SOURCE_NOT_READY", f"Unsupported export source: {table}.")
    if not columns or columns == {"_empty"}:
        raise ExportValidationError("EXPORT_SOURCE_NOT_FOUND",
                                    f"Data sumber export '{table}' periode {bulan} belum tersedia.", 404)
    clauses = []
    params: list[str] = []
    lower = {key.lower(): key for key in columns}
    period_column = next((lower[key] for key in ("bulan", "periode", "monthkey", "tanggalkey", "tanggal") if key in lower), None)
    if period_column:
        if period_column.lower() == "bulan":
            clauses.append('"bulan" = ?')
        elif period_column.lower() in ("bulan", "periode"):
            clauses.append(f'"{period_column}" = ?')
        elif period_column.lower() == "tanggal":
            clauses.append("""coalesce(strftime(try_cast("tanggal" AS DATE), '%Y-%m'),
                strftime(try_strptime(cast("tanggal" AS VARCHAR), '%d/%m/%Y'), '%Y-%m'),
                strftime(try_strptime(cast("tanggal" AS VARCHAR), '%d-%m-%Y'), '%Y-%m'),
                substr(cast("tanggal" AS VARCHAR), 1, 7)) = ?""")
        else:
            clauses.append(f'substr(cast("{period_column}" AS VARCHAR), 1, 7) = ?')
        params.append(bulan)
    if unit and "unit" in columns:
        clauses.append('"unit" = ?')
        params.append(unit)
    if jabatan and "jabatan" in columns:
        clauses.append('"jabatan" = ?')
        params.append(jabatan)
    query = f'SELECT * FROM "{table}"'
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    return query, params


def read_workflow_table_duckdb(table: str, bulan: str, unit: Optional[str] = None, jabatan: Optional[str] = None) -> pd.DataFrame:
    """Read a workflow table directly from DuckDB applying filters in SQL.
    If DuckDB is unavailable or the table does not exist, raise HTTPException(503).
    This enforces DuckDB as the canonical data source when backend is active.
    """
    try:
        import duckdb as _d
        dconn = _d.connect(DUCKDB_PATH)
        try:
            try:
                cols = {r[1] for r in dconn.execute(f"PRAGMA table_info('{table}')").fetchall()}
            except Exception:
                cols = set()
            if not cols:
                raise ExportValidationError("EXPORT_SOURCE_NOT_FOUND",
                                            f"Data sumber export '{table}' belum tersedia.", 404)
            where_clauses = []
            params = []
            if 'bulan' in cols and bulan:
                where_clauses.append('bulan = ?')
                params.append(bulan)
            if 'unit' in cols and unit:
                where_clauses.append('unit = ?')
                params.append(unit)
            if 'jabatan' in cols and jabatan:
                where_clauses.append('jabatan = ?')
                params.append(jabatan)
            sql, params = build_export_query(table, bulan, cols, unit, jabatan)
            df = dconn.execute(sql, params).fetchdf()
            return df
        finally:
            dconn.close()
    except ExportValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail={
            "code": exc.code,
            "message": exc.message,
        }) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "code": "EXPORT_QUERY_ERROR",
            "message": f"Gagal menjalankan query export untuk sumber '{table}'.",
            "error": str(exc),
        }) from exc


def safe_excel_value(value: Any) -> Any:
    """Handle arrays/objects, invalid XML characters, and Excel's cell limit."""
    if isinstance(value, (list, tuple, set, dict)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        value = EXCEL_INVALID_XML_CHARS.sub("", value)
        return value[:EXCEL_CELL_TEXT_LIMIT]
    return value


def prepare_database_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Serialize nested browser values so SQLite can store every workflow field."""
    if frame.empty:
        return frame
    result = frame.copy()
    for column in result.columns:
        result[column] = result[column].map(serialize_database_value)
    return result


MASTER_SDM_COLUMNS = (
    "nip",
    "nama",
    "provinsi",
    "kabupatenKota",
    "kdAnakSatker",
    "jabatanOsdm",
    "statusTendik",
    "jabatan",
    "tukin",
)


def normalize_master_sdm_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize imported master SDM columns before writing to DuckDB."""
    if frame.empty:
        return pd.DataFrame(columns=MASTER_SDM_COLUMNS)
    aliases = {
        "kabupaten": "kabupatenKota",
        "kab_kota": "kabupatenKota",
        "kd_anaksatker": "kdAnakSatker",
        "kodeanaksatker": "kdAnakSatker",
        "jabatan_osdm": "jabatanOsdm",
        "status_tendik": "statusTendik",
        "tukinbruto": "tukin",
    }
    result = frame.copy()
    result.columns = [
        aliases.get(str(column).strip().lower(), str(column).strip())
        for column in result.columns
    ]
    for column in MASTER_SDM_COLUMNS:
        if column not in result.columns:
            result[column] = 0 if column == "tukin" else ""
    result = result[list(MASTER_SDM_COLUMNS)]
    result["nip"] = result["nip"].map(lambda value: str(value).strip() if pd.notna(value) else "")
    result = result[result["nip"] != ""].copy()
    result["tukin"] = pd.to_numeric(result["tukin"], errors="coerce").fillna(0)
    for column in MASTER_SDM_COLUMNS:
        if column != "tukin":
            result[column] = result[column].map(
                lambda value: str(value).strip() if pd.notna(value) else ""
            )
    return result.drop_duplicates(subset=["nip"], keep="last")


def serialize_database_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "evkin-export-api"}


@app.get("/api/health")
def api_health() -> dict:
    return health()


@app.get("/api/info")
def api_info() -> dict:
    return {
        "status": "ok",
        "service": "evkin-export-api",
        "frontend_served": INDEX_FILE.is_file(),
        "app_root": str(APP_ROOT),
    }


@app.get('/api/ping')
def api_ping() -> dict:
    """Lightweight endpoint to confirm connectivity from browser/clients.
    Returns status ok and server timestamp. Useful for quick network checks
    from the UI without exercising heavier endpoints.
    """
    return {"status": "ok", "time": datetime.utcnow().isoformat(), "service": "evkin-export-api"}


@app.get("/")
@app.get("/index.html")
def serve_frontend(request: Request) -> FileResponse:
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=404, detail="Frontend index.html not found")
    response = FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")
    key = configured_api_key()
    if (
        key
        and request.client
        and request.client.host in {'127.0.0.1', '::1'}
        and request.url.hostname in {'127.0.0.1', '::1', 'localhost'}
    ):
        response.set_cookie(key="evkin_local_api_key", value=key, samesite="strict", httponly=False)
    return response


@app.get("/api/workflow/health")
def workflow_health() -> dict:
    """Minimal readiness check for the master workflow database."""
    info = ensure_duckdb_master_schema()
    return {"status": "ok", "ready": True, **info}


@app.post("/api/workflow/sync")
def sync_workflow(payload: dict[str, Any]) -> dict:
    """Replace backend workflow tables with the browser's current workflow data.
    Store workflow tables in DuckDB (EVKIN_ABSENSI_DB) for efficient analytics.
    """
    invalid = set(payload) - set(WORKFLOW_WRITABLE)
    if invalid:
        raise HTTPException(400, 'Sumber bukan milik workflow: ' + ', '.join(sorted(invalid)))
    prepared = {source: workflow_rows(source, value) for source, value in payload.items()}
    ensure_duckdb_master_schema()
    table_map = {source: WORKFLOW_WRITABLE[source] for source in payload}
    counts = {}
    # write to DuckDB
    import duckdb as _duckdb
    dconn = _duckdb.connect(DUCKDB_PATH)
    try:
        dconn.execute('BEGIN TRANSACTION')
        for source, table in table_map.items():
            rows = prepared[source]
            if rows:
                frame = prepare_workflow_frame(source, rows)
                # register and create/replace
                dconn.register('tmp_frame', frame)
                create_table_from_relation(dconn, table, 'tmp_frame')
                try:
                    dconn.unregister('tmp_frame')
                except Exception:
                    pass
            else:
                dconn.execute(f'CREATE OR REPLACE TABLE "{table}" (_empty TEXT)')
            counts[source] = len(rows)
        dconn.commit()
    except Exception:
        dconn.rollback()
        raise
    finally:
        dconn.close()

    # Keep SQLite in sync for backward compatibility (lightweight copy of counts only)
    conn = get_connection()
    try:
        for source, table in table_map.items():
            # create a simple metadata table in SQLite to record presence
            conn.execute('CREATE TABLE IF NOT EXISTS workflow_meta (table_name TEXT PRIMARY KEY, row_count INTEGER)')
            conn.execute('REPLACE INTO workflow_meta (table_name, row_count) VALUES (?, ?)', (table, counts.get(source, 0)))
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "counts": counts}


@app.get("/api/workflow/state")
def get_workflow_state(
    periode: Optional[str] = Query(None),
    workflow_id: Optional[str] = Query(None),
) -> dict:
    """Return startup metadata only; large workflow rows are loaded lazily."""
    import duckdb as _duckdb
    dconn = workflow_read_cursor()
    config: dict[str, Any] | None = None
    row_counts: dict[str, int] = {}
    total_row_counts: dict[str, int] = {}
    try:
        config = read_workflow_config(dconn)
        workflow = config.get('_workflow', {}) if isinstance(config, dict) else {}
        periode = periode if isinstance(periode, str) else None
        workflow_id = workflow_id if isinstance(workflow_id, str) else None
        active_period = str(periode or workflow.get('month') or '').strip() or None
        active_workflow_id = str(
            workflow_id or workflow.get('workflow_id') or workflow.get('workflowId') or 'default'
        ).strip() or 'default'
        for source, table in WORKFLOW_TABLES.items():
            if source in {'master_data', 'config'}:
                continue
            try:
                columns = workflow_table_columns(dconn, table)
                if not columns or columns == ['_empty']:
                    total_row_counts[source] = 0
                    row_counts[source] = 0
                    continue
                total_row_counts[source] = int(
                    dconn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                where_sql, params = workflow_scope_sql(
                    source, columns, active_period, active_workflow_id
                )
                row_counts[source] = int(
                    dconn.execute(f'SELECT COUNT(*) FROM "{table}"{where_sql}', params).fetchone()[0]
                )
            except HTTPException:
                raise
            except Exception:
                total_row_counts[source] = 0
                row_counts[source] = 0
    finally:
        dconn.close()
    metadata_config = workflow_config_metadata(config)
    return {
        "status": "ok",
        "reset_generation": current_reset_generation(),
        "calculation_authority": current_authority_config().authority.value,
        "data": {"config": metadata_config},
        "meta": {
            "workflow_id": active_workflow_id,
            "periode": active_period,
            "status": "ACTIVE" if any(total_row_counts.values()) else "EMPTY",
            "row_counts": row_counts,
            "total_row_counts": total_row_counts,
            "default_page_size": 100,
        },
    }


@app.get("/api/workflow/data/{source}")
def get_workflow_data(
    source: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
    periode: Optional[str] = Query(None),
    workflow_id: Optional[str] = Query('default'),
    page_size: Optional[int] = Query(None, ge=1, le=500),
    search: Optional[str] = Query(None, max_length=200),
    filter_value: Optional[str] = Query(None, alias='filter', max_length=1000),
    sort: Optional[str] = Query(None, max_length=100),
    sort_dir: str = Query('asc', pattern='^(?i)(asc|desc)$'),
    include_detail: bool = Query(False),
) -> dict:
    """Read a lightweight workflow page; search/filter/sort run in DuckDB."""
    page_size = page_size if isinstance(page_size, int) else None
    search = search if isinstance(search, str) else None
    filter_value = filter_value if isinstance(filter_value, str) else None
    sort = sort if isinstance(sort, str) else None
    sort_dir = sort_dir if isinstance(sort_dir, str) and sort_dir.lower() in {'asc', 'desc'} else 'asc'
    include_detail = include_detail if isinstance(include_detail, bool) else False
    table = WORKFLOW_WRITABLE.get(source)
    if not table:
        raise HTTPException(404, f"Sumber workflow tidak ditemukan: {source}")
    import duckdb as _duckdb
    dconn = workflow_read_cursor()
    try:
        columns = workflow_table_columns(dconn, table)
        if not columns or columns == ['_empty']:
            effective_size = page_size or min(per_page, 500)
            return {
                "status": "ok", "source": source, "rows": [], "total": 0,
                "page": page, "page_size": effective_size, "per_page": effective_size,
                "total_pages": 0,
                "workflow_id": workflow_id or 'default', "periode": periode,
            }
        effective_size = page_size or min(per_page, 500)
        where_sql, params = _workflow_filtered_sql(
            source, columns, periode, workflow_id, search, filter_value
        )
        total = int(dconn.execute(f'SELECT COUNT(*) FROM "{table}"{where_sql}', params).fetchone()[0])
        lower = {column.lower(): column for column in columns}
        requested_sort = str(sort or '').strip().lower()
        sort_column = lower.get(requested_sort)
        if requested_sort and not sort_column:
            raise HTTPException(400, f'Kolom sort tidak dikenal: {sort}')
        order_sql = f'"{sort_column}" {sort_dir.upper()}, rowid' if sort_column else 'rowid'
        projection = '*' if include_detail else _workflow_list_projection(source, columns)
        offset = (page - 1) * effective_size
        frame = dconn.execute(
            f'SELECT rowid AS _row_id,{projection} FROM "{table}"{where_sql} '
            f'ORDER BY {order_sql} LIMIT ? OFFSET ?',
            list(params) + [effective_size, offset],
        ).fetchdf()
        rows = decode_workflow_rows(source, frame)
    finally:
        dconn.close()
    return {
        "status": "ok", "source": source, "rows": rows, "total": total,
        "page": page, "page_size": effective_size, "per_page": effective_size,
        "total_pages": (total + effective_size - 1) // effective_size if total else 0,
        "workflow_id": workflow_id or 'default', "periode": periode,
    }


@app.get("/api/workflow/calculation-input")
def get_workflow_calculation_input(
    workflow_id: str = Query(..., min_length=1, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=500),
) -> dict:
    """Return one verified page of canonical raw input for the active workflow.

    UI list pagination remains bounded.  The calculation client may walk every
    page explicitly, but can never read another workflow or an incomplete import.
    """
    dconn = workflow_read_cursor()
    try:
        config = read_workflow_config(dconn) or {}
        workflow = config.get('_workflow') if isinstance(config.get('_workflow'), dict) else {}
        active_workflow_id = str(
            workflow.get('workflow_id') or workflow.get('workflowId') or 'default'
        ).strip() or 'default'
        if workflow_id != active_workflow_id:
            raise HTTPException(409, detail={
                'code': 'WORKFLOW_SCOPE_MISMATCH',
                'message': 'Input kalkulasi bukan milik workflow aktif.',
                'requested_workflow_id': workflow_id,
                'active_workflow_id': active_workflow_id,
            })
        columns = workflow_table_columns(dconn, WORKFLOW_TABLES['raw_import'])
        if not columns or columns == ['_empty']:
            raise HTTPException(409, detail={
                'code': 'IMPORT_NOT_PERSISTED',
                'message': 'Data sumber impor canonical belum tersimpan.',
            })
        lower = {column.lower(): column for column in columns}
        workflow_column = next(
            (lower[name] for name in WORKFLOW_ID_COLUMNS if name in lower), None
        )
        where_sql = ''
        params: list[Any] = []
        if workflow_column:
            where_sql = f' WHERE CAST("{workflow_column}" AS VARCHAR)=?'
            params.append(workflow_id)
        persisted_rows = int(dconn.execute(
            f'SELECT COUNT(*) FROM "{WORKFLOW_TABLES["raw_import"]}"{where_sql}', params
        ).fetchone()[0])
        stats = workflow.get('importStats') if isinstance(workflow.get('importStats'), dict) else {}
        expected_source_rows = int(stats.get('totalRaw') or persisted_rows or 0)
        expected_calculation_rows = int(stats['totalRecord'] if stats.get('totalRecord') is not None else expected_source_rows)
        declared_status = str(workflow.get('importStatus') or '').upper()
        complete = persisted_rows > 0 and persisted_rows == expected_source_rows
        if declared_status and declared_status != 'COMPLETE':
            complete = False
        if not complete:
            raise HTTPException(409, detail={
                'code': 'IMPORT_INCOMPLETE',
                'message': 'Impor aktif belum tersimpan lengkap pada penyimpanan canonical.',
                'workflow_id': active_workflow_id,
                'expected_source_rows': expected_source_rows,
                'persisted_source_rows': persisted_rows,
                'import_status': declared_status or 'INCOMPLETE',
            })
        offset = (page - 1) * page_size
        frame = dconn.execute(
            f'SELECT * FROM "{WORKFLOW_TABLES["raw_import"]}"{where_sql} '
            'ORDER BY rowid LIMIT ? OFFSET ?', params + [page_size, offset]
        ).fetchdf()
        rows = decode_workflow_rows('raw_import', frame)
        total_pages = (persisted_rows + page_size - 1) // page_size
        return {
            'status': 'ok', 'stage': 'CALCULATION_INPUT', 'import_status': 'COMPLETE',
            'workflow_id': active_workflow_id, 'periode': workflow.get('month'),
            'periods': stats.get('months') if isinstance(stats.get('months'), list) else [],
            'expected_source_rows': expected_source_rows,
            'persisted_source_rows': persisted_rows,
            'expected_calculation_rows': expected_calculation_rows,
            'rows': rows, 'page': page, 'page_size': page_size,
            'total': persisted_rows, 'total_pages': total_pages,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception('Calculation input hydration failed for workflow=%s page=%s',
                         workflow_id, page)
        raise HTTPException(500, detail={
            'code': 'CALCULATION_INPUT_ERROR',
            'message': 'Backend gagal membaca input kalkulasi dari penyimpanan canonical.',
            'stage': 'CALCULATION_INPUT',
        }) from exc
    finally:
        dconn.close()


@app.get("/api/workflow/detail/{source}/{row_id}")
def get_workflow_detail(source: str, row_id: int) -> dict:
    """Fetch the full payload for one selected row only."""
    table = WORKFLOW_WRITABLE.get(source)
    if not table:
        raise HTTPException(404, f"Sumber workflow tidak ditemukan: {source}")
    with workflow_read_cursor() as dconn:
        columns = workflow_table_columns(dconn, table)
        if not columns or columns == ['_empty']:
            raise HTTPException(404, 'Record tidak ditemukan')
        frame = dconn.execute(
            f'SELECT rowid AS _row_id,* FROM "{table}" WHERE rowid=? LIMIT 1', [row_id]
        ).fetchdf()
    rows = decode_workflow_rows(source, frame)
    if not rows:
        raise HTTPException(404, 'Record tidak ditemukan')
    return {'status': 'ok', 'source': source, 'row': rows[0]}


@app.get("/api/workflow/summary/{source}")
def get_workflow_summary(source: str, periode: Optional[str] = Query(None),
                         workflow_id: Optional[str] = Query('default')) -> dict:
    """Return bounded KPI aggregates without materializing workflow rows."""
    periode = periode if isinstance(periode, str) else None
    workflow_id = workflow_id if isinstance(workflow_id, str) else 'default'
    table = WORKFLOW_WRITABLE.get(source)
    if not table:
        raise HTTPException(404, f"Sumber workflow tidak ditemukan: {source}")
    with workflow_read_cursor() as dconn:
        columns = workflow_table_columns(dconn, table)
        if not columns or columns == ['_empty']:
            return {'status': 'ok', 'source': source, 'summary': {
                'total': 0, 'distinct_sdm': 0, 'valid': 0, 'invalid': 0,
                'total_potongan': 0.0, 'total_nominal': 0.0,
            }, 'by_status': []}
        where_sql, params = workflow_scope_sql(source, columns, periode, workflow_id)
        lower = {column.lower(): column for column in columns}
        status_column = next((lower[name] for name in
                              ('status', 'statuslengkap', 'review_status', 'validation_status')
                              if name in lower), None)
        nip_expr = f'COUNT(DISTINCT "{lower["nip"]}")' if 'nip' in lower else '0'
        pot_column = next((lower[name] for name in ('total', 'totalharian', 'potpct') if name in lower), None)
        nominal_column = next((lower[name] for name in
                               ('finaldibayar', 'final_tukin_dibayar', 'nominal', 'paid')
                               if name in lower), None)
        if status_column:
            normalized = f'upper(coalesce(CAST("{status_column}" AS VARCHAR), \'\'))'
            valid_expr = (f"SUM(CASE WHEN {normalized} IN "
                          "('VALID','LENGKAP','BERSIH','APPROVED','DONE') THEN 1 ELSE 0 END)")
            invalid_expr = (f"SUM(CASE WHEN {normalized} IN "
                            "('INVALID','TIDAK LENGKAP','REJECTED','FAILED') THEN 1 ELSE 0 END)")
        else:
            valid_expr = 'COUNT(*)'
            invalid_expr = '0'
        result = dconn.execute(
            f'SELECT COUNT(*),{nip_expr},{valid_expr},{invalid_expr},'
            f'{f"coalesce(SUM(try_cast(\"{pot_column}\" AS DOUBLE)),0)" if pot_column else "0"},'
            f'{f"coalesce(SUM(try_cast(\"{nominal_column}\" AS DOUBLE)),0)" if nominal_column else "0"} '
            f'FROM "{table}"{where_sql}', params
        ).fetchone()
        by_status = []
        if status_column:
            status_rows = dconn.execute(
                f'SELECT CAST("{status_column}" AS VARCHAR) status,COUNT(*) total '
                f'FROM "{table}"{where_sql} GROUP BY 1 ORDER BY total DESC', params
            ).fetchall()
            by_status = [{'status': row[0], 'total': int(row[1])} for row in status_rows]
    keys = ('total', 'distinct_sdm', 'valid', 'invalid', 'total_potongan', 'total_nominal')
    summary = dict(zip(keys, result))
    for key in ('total', 'distinct_sdm', 'valid', 'invalid'):
        summary[key] = int(summary[key] or 0)
    for key in ('total_potongan', 'total_nominal'):
        summary[key] = float(summary[key] or 0)
    return {'status': 'ok', 'source': source, 'summary': summary, 'by_status': by_status}


@app.get("/api/master-sdm")
def get_master_sdm(q: Optional[str] = Query(None, description='Query string to search NIP/Nama/Jabatan'), page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=1000), jabatan: Optional[str] = Query(None), unit: Optional[str] = Query(None), sort: Optional[str] = Query(None, description='Sort column, e.g. nip or nama')) -> dict:
    """Read the SDM master list with optional pagination and filtering.
    - q: fulltext-like search against nip, nama, jabatan
    - page, per_page: pagination
    - jabatan, unit: exact filters
    - sort: column name to sort by (safe list applied)

    Returns rows, total count, page metadata.
    """
    ensure_duckdb_master_schema()

    # The shared SDM tables are the source of truth.  The legacy ``master_data``
    # table is only a compatibility projection and may legitimately be empty
    # after a browser workflow sync.  Final must therefore never depend on that
    # projection for its Tukin lookup.
    if shared_master is not None:
        canonical = shared_master.SharedMasterService(DUCKDB_PATH).list(
            q=q or "", page=page, per_page=per_page, active=True,
            jabatan=jabatan or "", unit=unit or "", sort=sort or ""
        )
        rows = []
        for source in canonical.get("rows", []):
            row = dict(source)
            row.update({
                "kabupatenKota": source.get("kabupaten_kota", ""),
                "kdAnakSatker": source.get("kd_anak_satker", ""),
                "jabatanOsdm": source.get("jabatan_osdm", ""),
                "statusTendik": source.get("status_tendik", ""),
                "jenisSdm": source.get("jenis_sdm", ""),
                "statusKepegawaian": source.get("employment_status", ""),
                "kelasJabatan": source.get("kelas_jabatan"),
                "wilayah3T": source.get("wilayah_3t", False),
                "berhakTukin": source.get("eligible", True),
                "tukin": source.get("tukin_gross", 0),
                "bank": source.get("bank_name", ""),
                "noRekening": source.get("account_number", ""),
                "namaPemilikRekening": source.get("account_holder", ""),
            })
            rows.append(row)
        return {
            "status": "ok", "rows": rows, "count": canonical.get("total", 0),
            "page": canonical.get("page", page), "per_page": canonical.get("per_page", per_page),
            "total_pages": canonical.get("pages", 0), "source": "shared-master",
        }

    raise HTTPException(503, 'Layanan Master SDM kanonis tidak tersedia')


@app.post("/api/master-sdm")
def upsert_master_sdm(payload: dict[str, Any]) -> dict:
    """Compatibility import; canonical history is always written first."""
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if rows is None and isinstance(payload, list):
        rows = payload
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="Payload harus berupa list rows")
    if not rows:
        raise HTTPException(status_code=400, detail="Daftar kosong tidak boleh digunakan untuk reset. Gunakan reset administratif Master SDM")
    if shared_master is None:
        raise HTTPException(status_code=503, detail="Layanan Master SDM kanonis tidak tersedia")
    try:
        result = shared_master.SharedMasterService(DUCKDB_PATH).ingest_rows(
            rows, datetime.now().date(), "LEGACY_MASTER_IMPORT", "OPERATOR_KOMPATIBILITAS", confirmed=True
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Master SDM gagal diperbarui: {exc}") from exc
    return {"status": "ok", "count": result["committed"], "rows": rows}


@app.post("/api/master-sdm/upsert")
def upsert_master_sdm_row(payload: dict[str, Any]) -> dict:
    """Upsert one or more master SDM rows by NIP.
    Accepts payload: { rows: [...] } or a single row dict or a list of rows.
    New rows override existing rows with the same 'nip'.
    """
    ensure_duckdb_master_schema()
    # Normalize payload to list of rows
    rows = None
    if isinstance(payload, dict) and 'rows' in payload:
        rows = payload.get('rows')
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        # single row object
        rows = [payload]

    if rows is None:
        rows = []

    # Accept a single row dict as well
    if isinstance(rows, dict):
        rows = [rows]

    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail='Payload harus berupa list rows atau objek row')

    # Basic validation: each row must have 'nip'
    for r in rows:
        if not isinstance(r, dict) or not r.get('nip'):
            raise HTTPException(status_code=400, detail="Setiap baris harus berupa objek dengan kunci 'nip'")

    if shared_master is None:
        raise HTTPException(status_code=503, detail="Layanan Master SDM kanonis tidak tersedia")
    try:
        shared_master.SharedMasterService(DUCKDB_PATH).ingest_rows(
            rows, datetime.now().date(), "LEGACY_MASTER_UPSERT", "OPERATOR_KOMPATIBILITAS", confirmed=True
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Master SDM gagal diperbarui: {exc}") from exc

    return {"status": "ok", "upserted": len(rows)}


@app.post("/api/master-sdm/delete")
def delete_master_sdm(payload: dict[str, Any]) -> dict:
    """Delete master SDM rows by NIP(s).
    Payload: { nips: ["nip1", "nip2"] } or list of nips.
    """
    ensure_duckdb_master_schema()
    nips = None
    if isinstance(payload, dict) and 'nips' in payload:
        nips = payload.get('nips')
    elif isinstance(payload, list):
        nips = payload
    elif isinstance(payload, dict) and payload.get('nip'):
        nips = [payload.get('nip')]

    if not nips:
        raise HTTPException(status_code=400, detail='Payload harus berisi daftar nips untuk dihapus')

    if isinstance(nips, str):
        nips = [nips]
    nips = [str(x) for x in nips]

    # Compatibility endpoint: in the canonical master this is a soft delete.
    # Historical payments and period snapshots therefore remain traceable.
    deactivated = 0
    if shared_master is not None:
        master_service = shared_master.SharedMasterService(DUCKDB_PATH)
        master_service.list(page=1, per_page=1)  # initializes/backfills legacy data once
        for nip in nips:
            try:
                master_service.set_active(nip, False, "Dinonaktifkan dari modul Master SDM", "OPERATOR_KOMPATIBILITAS")
                deactivated += 1
            except KeyError:
                pass

    return {"status": "ok", "deleted": 0, "deactivated": deactivated, "recoverable": True}


@app.get("/api/workflow/reset/preview")
@app.get("/api/workflow/reset-backend/preview")
def preview_legacy_workflow_reset() -> dict:
    """Legacy-path preview backed by the canonical workflow-results profile."""
    try:
        return ResetService(DUCKDB_PATH).preview('workflow_results')
    except (ValueError, OSError, duckdb.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflow/reset")
def reset_workflow(payload: dict[str, Any] | None = None) -> dict:
    """Legacy adapter: reset derived workflow data through ResetService."""
    try:
        return ResetService(DUCKDB_PATH).execute(
            'workflow_results', payload or {}, source='LEGACY_WORKFLOW_RESET_API'
        )
    except (ValueError, OSError, duckdb.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflow/reset-backend")
def reset_backend_workflow(payload: dict[str, Any] | None = None) -> dict:
    """Named endpoint for the UI's explicit backend workflow reset action."""
    return reset_workflow(payload)


@app.post("/api/workflow/delete-absensi")
def delete_absensi_workflow(payload: dict[str, Any]) -> dict:
    """Legacy phrase adapter for the canonical full-workflow reset profile."""
    if payload.get('confirmation') != 'HAPUS ABSENSI':
        raise HTTPException(status_code=400, detail='Konfirmasi HAPUS ABSENSI diperlukan.')
    service = ResetService(DUCKDB_PATH)
    plan = service.preview('workflow')
    try:
        return service.execute('workflow', {
            'preview_token': plan['preview_token'],
            'confirmation': plan['confirmation'],
            'actor': 'LEGACY_WORKFLOW_API',
            'reason': 'Compatibility reset HAPUS ABSENSI',
        }, source='LEGACY_DELETE_ABSENSI_API')
    except (ValueError, OSError, duckdb.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/workflow/sync-table")
def sync_workflow_table(payload: dict[str, Any]) -> dict:
    """Store one workflow table batch to keep large sync requests bounded.
    Writes directly into DuckDB for large imports (import_batches integration).
    """
    source = str(payload.get("source") or "")
    table = WORKFLOW_WRITABLE.get(source)
    if not table:
        raise HTTPException(status_code=400, detail=f"Workflow table tidak dikenal: {source}")
    rows = workflow_rows(source, payload.get("rows"))
    ensure_duckdb_master_schema()
    replace = bool(payload.get("replace", False))
    frame = prepare_workflow_frame(source, rows) if rows else None
    requested_workflow_id = str(payload.get('workflow_id') or 'default').strip() or 'default'
    requested_period = str(payload.get('periode') or '').strip() or None

    import duckdb as _duckdb
    dconn = _duckdb.connect(DUCKDB_PATH)
    try:
        dconn.execute('BEGIN TRANSACTION')
        target_columns = workflow_table_columns(dconn, table)
        scoped_where = ''
        scoped_params: list[str] = []
        if target_columns and target_columns != ['_empty']:
            target_lower = {column.lower() for column in target_columns}
            if any(name in target_lower for name in WORKFLOW_ID_COLUMNS):
                scoped_where, scoped_params = workflow_scope_sql(
                    source, target_columns, requested_period, requested_workflow_id
                )
        if rows:
            dconn.register('tmp_frame', frame)
            if replace and scoped_where:
                dconn.execute(f'DELETE FROM "{table}"{scoped_where}', scoped_params)
                insert_relation_by_name(
                    dconn, table, 'tmp_frame',
                    optional_columns=WORKFLOW_OPTIONAL_COLUMNS.get(source, {}).keys(),
                )
            elif replace:
                create_table_from_relation(dconn, table, 'tmp_frame')
            else:
                exists = dconn.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='main' AND table_name=?", [table]
                ).fetchone()[0]
                if exists:
                    insert_relation_by_name(dconn, table, 'tmp_frame')
                else:
                    create_table_from_relation(dconn, table, 'tmp_frame')
            try:
                dconn.unregister('tmp_frame')
            except Exception:
                pass
        elif replace and scoped_where:
            dconn.execute(f'DELETE FROM "{table}"{scoped_where}', scoped_params)
        elif replace:
            dconn.execute(f'CREATE OR REPLACE TABLE "{table}" (_empty TEXT)')
        # Chunk clients verify the active workflow count after every append.
        # Returning the whole shared table count breaks as soon as another
        # auditable workflow/import is retained in the canonical table.
        cnt = dconn.execute(
            f'SELECT COUNT(*) FROM "{table}"{scoped_where}', scoped_params
        ).fetchone()[0]
        dconn.commit()
    except Exception:
        dconn.rollback()
        raise
    finally:
        dconn.close()

    # Update sqlite meta
    conn = get_connection()
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS workflow_meta (table_name TEXT PRIMARY KEY, row_count INTEGER)')
        # Count was captured in the same transaction as the write.
        conn.execute('REPLACE INTO workflow_meta (table_name, row_count) VALUES (?, ?)', (table, cnt))
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "source": source, "rows": len(rows), "total_rows": int(cnt),
            "replace": replace, "workflow_id": requested_workflow_id,
            "periode": requested_period}


@app.post("/api/workflow/mark-semifinal")
def mark_semifinal(payload: dict[str, Any]):
    """Lightweight endpoint to record semifinal verification for a month.
    Stores a small flag in SQLite so the frontend can mark progress without re-uploading large workflow tables.
    Also writes the same flag into DuckDB workflow store so all workflow metadata is co-located.
    """
    bulan = str(payload.get('bulan') or '')
    verified = bool(payload.get('verified'))
    key = f'semifinal:{bulan}'

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", bulan):
        raise HTTPException(400, "Periode harus berformat YYYY-MM.")
    if verified:
        try:
            result = record_verification(DUCKDB_PATH, bulan)
        except ValueError as exc:
            raise HTTPException(409, {"code": "WORKFLOW_NOT_READY", "message": str(exc),
                                      "blocking_reasons": [str(exc)]}) from exc
        except Exception as exc:
            raise HTTPException(503, "Gagal menyimpan status semifinal ke DuckDB.") from exc
    else:
        import duckdb as _d_local
        with _d_local.connect(DUCKDB_PATH) as dconn_local:
            dconn_local.execute('CREATE TABLE IF NOT EXISTS workflow_flags (k TEXT PRIMARY KEY, verified INTEGER, updated_at TEXT)')
            dconn_local.execute(
                "INSERT OR REPLACE INTO workflow_flags (k, verified, updated_at) VALUES (?, 0, ?)",
                [key, datetime.utcnow().isoformat()],
            )
        result = {"status": "ok", "bulan": bulan, "verified": False}

    # Keep the legacy SQLite flag as a compatibility projection only after the
    # canonical DuckDB transaction has committed.
    conn = get_connection()
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS workflow_flags (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)')
        conn.execute('REPLACE INTO workflow_flags (k, v, updated_at) VALUES (?, ?, ?)',
                     (key, '1' if verified else '0', datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()

    return {**result, "bulan": bulan, "verified": verified}


@app.get("/api/workflow/flags")
def get_workflow_flags(bulan: Optional[str] = Query(None, description="Bulan filter e.g. 2026-08")):
    """Return workflow flag rows (from DuckDB where possible, fallback to SQLite).
    If 'bulan' is provided, only return flags that end with ':{bulan}' (e.g., 'semifinal:2026-08').
    """
    try:
        import duckdb as _d
        dconn = _d.connect(DUCKDB_PATH)
        try:
            try:
                df = dconn.execute("SELECT k, verified, updated_at FROM workflow_flags").fetchdf()
            except Exception:
                # Table may not exist
                return {"status": "ok", "flags": {}}
            if df.empty:
                return {"status": "ok", "flags": {}}
            if bulan:
                suffix = ':' + bulan
                # pandas str accessor is available on object dtype
                try:
                    df = df[df['k'].str.endswith(suffix)]
                except Exception:
                    # if for some reason df['k'] is not string-typed, coerce
                    df['k'] = df['k'].astype(str)
                    df = df[df['k'].str.endswith(suffix)]
            flags = {}
            for _, row in df.iterrows():
                flags[row['k']] = {"verified": bool(int(row['verified'])), "updated_at": row['updated_at']}
            return {"status": "ok", "flags": flags}
        finally:
            dconn.close()
    except Exception:
        # fallback to sqlite
        conn = get_connection()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS workflow_flags (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)')
            if bulan:
                key = f'semifinal:{bulan}'
                rows = conn.execute('SELECT k, v, updated_at FROM workflow_flags WHERE k = ?', (key,)).fetchall()
            else:
                rows = conn.execute('SELECT k, v, updated_at FROM workflow_flags').fetchall()
            flags = {}
            for r in rows:
                flags[r['k']] = {"verified": (r['v'] == '1'), "updated_at": r['updated_at']}
            return {"status": "ok", "flags": flags}
        finally:
            conn.close()


@app.post("/api/export/final")
def export_final(
    bulan: str = Query(..., description="Bulan yang akan diekspor, contoh: 2026-08"),
    unit: Optional[str] = Query(None),
    jabatan: Optional[str] = Query(None, description="Filter berdasarkan jabatan (optional)"),
    smart_mode: bool = Query(False),
):
    try:
        # Enforce reading from DuckDB workflow store (backend is canonical source when active)
        df = read_workflow_table_duckdb("final_absensi", bulan, unit, jabatan)
        if df is None or df.empty:
            # If table is missing or empty in DuckDB, report not found
            raise HTTPException(status_code=404, detail=f"Tidak ada data final untuk bulan {bulan} di backend DuckDB")

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            prepare_excel_frame(df).to_excel(writer, index=False, sheet_name="FINAL ABSENSI")

        output.seek(0)
        filename = f"EVKIN_FINAL_{bulan}_{'SMART' if smart_mode else 'FULL'}.xlsx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - minimal service guard
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/export/workbook")
def export_workbook(
    bulan: str = Query(...),
    unit: Optional[str] = Query(None),
    jabatan: Optional[str] = Query(None, description="Filter berdasarkan jabatan (optional)"),
    sheet_names: Optional[str] = Query(None, description="Daftar sheet yang dipilih, dipisah koma"),
):
    try:
        selected = []
        if sheet_names:
            selected = [s.strip() for s in sheet_names.split(",") if s.strip()]

        tables = {
            "DATA SUMBER": "raw_import",
            "HITUNG HARIAN": "hitung_harian",
            "REKAP SDM": "rekap_sdm",
            "FINAL ABSENSI": "final_absensi",
            "FINAL + SKP": "final_skp",
            "FINAL KOMBINASI": "final_kombinasi",
            "FINAL + SKP KOMBINASI": "final_combined_skp",
        }

        sheets = {}
        for name, table in tables.items():
            if selected and name not in selected:
                continue
            # Enforce DuckDB as canonical source for export when backend is active
            df_table = read_workflow_table_duckdb(table, bulan, unit, jabatan)
            sheets[name] = prepare_excel_frame(df_table)

        sheets = {name: frame for name, frame in sheets.items() if not frame.empty}
        if not sheets:
            raise HTTPException(status_code=404, detail=f"Tidak ada data pada backend DuckDB untuk bulan {bulan}")

        output = io.BytesIO()
        _write_workbook_stream(output, sheets)
        output.seek(0)
        filename = f"EVKIN_EXPORT_{bulan}.xlsx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - minimal service guard
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get('/api/import/template')
def api_import_template(source: str = Query(..., description='Sumber import (contoh: raw_import, final_absensi, master_data)'), format: str = Query('xlsx', description='xlsx or csv')):
    """Generate and return an example import template for the requested workflow source.
    Returns an XLSX (default) or CSV file the user can download to prepare imports.
    """
    try:
        def _template_for(source_key: str) -> Optional[pd.DataFrame]:
            s = (source_key or '').strip()
            if s == 'raw_import':
                cols = ['nip', 'nama', 'jabatan', 'unit', 'tanggal', 'zona', 'masukMin', 'pulangMin', 'masukNormMin', 'pulangNormMin', 'masukNormMinAdd', 'pulangNormMinAdd', 'jenis']
                example = ['10001', 'Pegawai A', 'Guru', 'SDM', '2026-08-01', 'WITA', '450', '1020', '', '', '', '', '']
                return pd.DataFrame([example], columns=cols)
            if s == 'final_absensi':
                cols = ['nip', 'nama', 'jabatan', 'bulan', 'unit', 'tukin', 'komponen', 'potPct', 'nominal', 'finalDibayar']
                example = ['10001', 'Pegawai A', 'Guru', '2026-08', 'SDM', 2500000, 750000, 2.5, 30000, 2455000]
                return pd.DataFrame([example], columns=cols)
            if s == 'master_data':
                cols = ['nip', 'nama', 'jabatan', 'unit', 'email', 'telp']
                example = ['10001', 'Pegawai A', 'Guru', 'SDM', 'a@example.com', '08123456789']
                return pd.DataFrame([example], columns=cols)
            if s in ('daily_data', 'hitung_harian'):
                cols = ['nip', 'nama', 'jabatan', 'bulan', 'tanggal', 'zona', 'jamMasuk', 'jamPulang', 'durasi', 'jamNormalisasi', 'jamMasukAsli', 'jamPulangAsli', 'durasiMin', 'metode', 'jenis']
                example = ['10001', 'Pegawai A', 'Guru', '2026-08', '2026-08-01', 'WIB', '07:35', '16:05', '8h 30m', '', '07:35', '16:05', 510, 'reguler', '']
                return pd.DataFrame([example], columns=cols)
            if s == 'rekap_sdm':
                cols = ['nip', 'nama', 'jabatan', 'bulan', 'unit', 'totalHarian', 'totalPotongan', 'finalDibayar']
                example = ['10001', 'Pegawai A', 'Guru', '2026-08', 'SDM', 20, 30000, 2455000]
                return pd.DataFrame([example], columns=cols)
            if s == 'semifinal' or s == 'anomali':
                cols = ['nip', 'tanggal', 'zona', 'issue', 'notes']
                example = ['10001', '2026-08-01', 'WITA', 'Missing clock-out', 'Periksa manual']
                return pd.DataFrame([example], columns=cols)
            if s in ('final_skp', 'final_kombinasi', 'final_combined_skp'):
                cols = ['nip', 'nama', 'jabatan', 'bulan', 'skpScore', 'finalValue']
                example = ['10001', 'Pegawai A', 'Guru', '2026-08', 85.5, 2455000]
                return pd.DataFrame([example], columns=cols)
            if s == 'config' or s == 'workflow_config':
                cols = ['k', 'v']
                example = ['jamDasar', '{"senKam": {"masuk": "07:30", "pulang": "16:00"}, "jumat": {"masuk": "07:30","pulang": "16:30"}}']
                return pd.DataFrame([example], columns=cols)
            # unknown source
            return None

        df = _template_for(source)
        if df is None:
            raise HTTPException(status_code=404, detail=f"Unknown import source: {source}")

        if (format or '').lower() == 'csv':
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            buf.seek(0)
            data = buf.getvalue().encode('utf-8')
            return StreamingResponse(io.BytesIO(data), media_type='text/csv', headers={"Content-Disposition": f'attachment; filename="TEMPLATE_{source}.csv"'})

        # default: xlsx
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine='xlsxwriter') as writer:
            prepare_excel_frame(df).to_excel(writer, index=False, sheet_name='TEMPLATE')
        out.seek(0)
        return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="TEMPLATE_{source}.xlsx"'})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/export/seed-demo")
def seed_demo_data() -> dict:
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS final_absensi")
    conn.execute(
        """
        CREATE TABLE final_absensi (
            nip TEXT,
            nama TEXT,
            jabatan TEXT,
            bulan TEXT,
            unit TEXT,
            tukin REAL,
            komponen REAL,
            potPct REAL,
            nominal REAL,
            finalDibayar REAL
        )
        """
    )

    demo_rows = [
        ("10001", "Pegawai A", "Guru", "2026-08", "SDM", 2500000, 750000, 2.5, 30000, 2455000),
        ("10002", "Pegawai B", "Guru", "2026-08", "SDM", 2600000, 780000, 1.8, 22000, 2578000),
        ("10003", "Pegawai C", "TU", "2026-08", "SDM", 2400000, 720000, 3.4, 41000, 2359000),
    ]
    conn.executemany(
        "INSERT INTO final_absensi (nip, nama, jabatan, bulan, unit, tukin, komponen, potPct, nominal, finalDibayar) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        demo_rows,
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Demo data seeded"}


# ------------------ Background compute jobs (hitung harian) ------------------
# Background export job implementation (background file generation for Excel/CSV)
import tempfile
import os

EXPORT_DIR = str(STORAGE_DIR / 'exports')
os.makedirs(EXPORT_DIR, exist_ok=True)


EXCEL_SHEET_ROWS = 1_048_576

DEFAULT_SHEET_HEADERS = {
    'DATA SUMBER': ['nip', 'nama', 'jabatan', 'unit', 'tanggal', 'zona', 'jamMasuk', 'jamPulang', 'durasi', 'jenis'],
    'HITUNG HARIAN': ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Hari', 'Jam Masuk', 'Jam Pulang', 'Durasi', 'Zona', 'Jam Normalisasi', 'Metode', 'Jenis Absensi', 'Status Hari Libur', 'Status Cuti', 'Status Duplikasi', 'Absensi Lengkap', 'Pot Keterlambatan', 'Pot Pulang Cepat', 'Pot Durasi', 'Pot Tidak Lengkap', 'Total Potongan Harian', 'Flag'],
    'REKAP SDM': ['NIP', 'Nama', 'Jabatan', 'Jumlah Hari Kerja', 'Jumlah Hari Masuk', 'Jumlah Hari Absen', 'Total Potongan', 'Keterangan'],
    'FINAL ABSENSI': ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Unit', 'Tukin', 'Komponen Kehadiran', 'Potongan %', 'Nominal Potongan', 'Final Dibayar'],
    'FINAL + SKP': ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Unit', 'Tukin', 'Final Absensi', 'Nilai SKP', 'Potongan SKP', 'Final Dibayar'],
    'FINAL KOMBINASI': ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Unit', 'Tukin', 'Potongan %', 'Final Dibayar'],
    'FINAL + SKP KOMBINASI': ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Unit', 'Tukin', 'Final Dibayar'],
    'SEMI FINAL': ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Status Duplikasi', 'Status Lengkap', 'Flag'],
    'ANOMALI': ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Status Duplikasi', 'Status Lengkap', 'Flag']
}



def run_export_job(job_id: str, bulan: str, unit: Optional[str], sheet_names: list, fmt: str = 'xlsx', jabatan: Optional[str] = None, expected_final: Optional[dict] = None, calculation_id: Optional[str] = None, job_generation: Optional[int] = None) -> str:
    """Background worker to generate export file. Returns path to result file.
    Writes progress updates to jobs table. Supports 'xlsx' and 'csv' formats (csv -> zip of per-sheet csvs).
    """
    conn = get_connection()
    temp_path = None
    verified_final = None
    generation = current_reset_generation() if job_generation is None else job_generation
    try:
        _update_job(job_id, status='running', started_at=_now_iso(), progress=0.01, message='Starting export')
        try:
            assert_current_generation(generation)
            if not EXPORT_PERIOD_RE.fullmatch(str(bulan or "")):
                raise ExportValidationError("EXPORT_SOURCE_NOT_READY", f"Periode export tidak valid: {bulan!r}.")
            if fmt not in ('xlsx', 'csv') or calculation_id:
                fmt = 'xlsx'
            # Build sheet -> table mapping same as export_workbook
            tables = {
                'DATA SUMBER': 'raw_import',
                'HITUNG HARIAN': 'hitung_harian',
                'REKAP SDM': 'rekap_sdm',
                'SEMI FINAL': 'hitung_harian',
                'ANOMALI': 'hitung_harian',
                'FINAL ABSENSI': 'final_absensi',
                'FINAL + SKP': 'final_skp',
                'FINAL KOMBINASI': 'final_kombinasi',
                'FINAL + SKP KOMBINASI': 'final_combined_skp',
            }
            selected = sheet_names or list(tables.keys())
            unsupported = [name for name in selected if name not in tables]
            if unsupported:
                raise ExportValidationError(
                    "EXPORT_SOURCE_NOT_READY",
                    "Unsupported export source: " + ", ".join(unsupported),
                )
            total_sheets = len(selected)
            written_sheets = 0

            # Prepare file paths
            result_path = None
            if fmt == 'xlsx':
                result_path = os.path.join(EXPORT_DIR, f'{job_id}.xlsx')
                temp_path = os.path.join(EXPORT_DIR, f'{job_id}.tmp.xlsx')
                import xlsxwriter
                with duckdb.connect(DUCKDB_PATH) as snapshot, xlsxwriter.Workbook(
                    temp_path, {'constant_memory': True, 'strings_to_formulas': False,
                                'strings_to_urls': False, 'use_zip64': True, 'default_date_format': 'yyyy-mm-dd hh:mm:ss'}
                ) as workbook:
                    snapshot.execute("BEGIN TRANSACTION")
                    batch_counts = {}
                    config = read_workflow_config(snapshot) or {}
                    active = config.get('_workflow') or {}
                    workflow_id = active.get('workflow_id') or active.get('workflowId') or 'default'
                    if calculation_id:
                        batch = snapshot.execute("""
                            SELECT b.daily_count, b.rekap_count, b.final_count, b.timing_json
                            FROM absence_calculation_canonical c
                            JOIN absence_calculation_batch b USING (calculation_id)
                            WHERE c.period=? AND c.calculation_id=? AND b.status='COMPLETED' AND b.canonical
                        """, [bulan, calculation_id]).fetchone()
                        if batch is None:
                            raise ValueError('Hasil periode sudah berubah. Buka ulang halaman Export.')
                        if (json.loads(batch[3] or '{}').get('source_workflow_id') or 'default') != workflow_id:
                            raise ValueError('Workbook aktif berubah. Jalankan Hitung Harian sebelum ekspor.')
                        batch_counts = dict(zip(('hitung_harian', 'rekap_sdm', 'final_absensi'), batch[:3]))
                    for name in selected:
                        table = tables[name]
                        _update_job(job_id, progress=0.05 + 0.9 * written_sheets / total_sheets,
                                    message=f'Menulis sheet {name}')
                        columns = {r[1] for r in snapshot.execute(f"PRAGMA table_info('{table}')").fetchall()}
                        try:
                            query, params = build_export_query(table, bulan, columns, unit, jabatan)
                        except ExportValidationError as exc:
                            if exc.status_code != 404 or table in batch_counts:
                                raise
                            sheet_headers = DEFAULT_SHEET_HEADERS.get(name, [name])
                            workbook.add_worksheet(name).write_row(0, 0, sheet_headers)
                            written_sheets += 1
                            continue
                        if table == 'raw_import' and 'workflow_id' in columns:
                            has_matching_wf = snapshot.execute('SELECT 1 FROM "raw_import" WHERE "workflow_id" = ? LIMIT 1', [workflow_id]).fetchone()
                            if has_matching_wf:
                                query += (' AND ' if ' WHERE ' in query else ' WHERE ') + '"workflow_id" = ?'
                                params.append(workflow_id)
                        if table in batch_counts:
                            if 'calculation_id' not in columns:
                                raise ValueError(f'Sumber {name} bukan hasil batch aktif.')
                            query += (' AND ' if ' WHERE ' in query else ' WHERE ') + '"calculation_id" = ?'
                            params.append(calculation_id)
                        if calculation_id and table.startswith('final_') and table not in batch_counts:
                            if 'calculation_id' not in columns:
                                raise ValueError(f'Sumber {name} belum disimpan dari Final aktif.')
                            query += (' AND ' if ' WHERE ' in query else ' WHERE ') + '"calculation_id" = ?'
                            params.append(calculation_id)
                        report = name in {'SEMI FINAL', 'ANOMALI'}
                        if report:
                            checks = []
                            if 'statusDuplikasi' in columns:
                                checks.append("lower(coalesce(cast(statusDuplikasi AS VARCHAR), '')) NOT IN ('', 'false', '0')")
                            if 'statusLengkap' in columns:
                                checks.append("statusLengkap = 'Tidak Lengkap'")
                            if 'flags' in columns:
                                checks.append("coalesce(cast(flags AS VARCHAR), '[]') NOT IN ('', '[]')" if name == 'SEMI FINAL'
                                              else "contains(coalesce(cast(flags AS VARCHAR), ''), 'PERLU_VALIDASI')")
                            query += (' AND ' if ' WHERE ' in query else ' WHERE ') + '(' + ' OR '.join(checks or ['FALSE']) + ')'
                        cursor = snapshot.execute(query + ' ORDER BY rowid', params)
                        headers = [item[0] for item in cursor.description]
                        sheet = workbook.add_worksheet(name)
                        sheet.write_row(0, 0, headers)
                        offset, part, row_count = 1, 1, 0
                        final_rows = []
                        while rows := cursor.fetchmany(4096):
                            for row in rows:
                                if offset == EXCEL_SHEET_ROWS:
                                    part += 1
                                    suffix = f' ({part})'
                                    sheet = workbook.add_worksheet(name[:31-len(suffix)] + suffix)
                                    sheet.write_row(0, 0, headers)
                                    offset = 1
                                values = [safe_excel_value(value) for value in row]
                                if sheet.write_row(offset, 0, values) != 0:
                                    raise ValueError(f'Baris {offset} gagal ditulis ke {name}')
                                offset += 1
                                row_count += 1
                                if row_count % 65_536 == 0:
                                    _update_job(job_id, message=f'{name}: {row_count:,} baris ditulis')
                                if name == 'FINAL ABSENSI':
                                    final_rows.append(dict(zip(headers, row)))
                        if table in batch_counts and not report and row_count != batch_counts[table]:
                            raise ValueError(f'Jumlah baris {name} berbeda dari batch aktif.')
                        if name == 'FINAL ABSENSI':
                            if final_rows:
                                verified_final = final_manifest(final_rows)
                                if expected_final is not None:
                                    assert_manifest(verified_final, expected_final)
                            elif expected_final is not None and expected_final.get('row_count', 0) > 0:
                                raise ValueError('Final Absensi di database kosong untuk periode yang dipilih')
                        written_sheets += 1
                if verified_final is not None and verified_final.get('row_count', 0) > 0:
                    _update_job(job_id, progress=.96, message='Memeriksa jumlah baris, NIP dan total nominal dalam Excel...')
                    verify_final_xlsx(temp_path, verified_final)
            else:
                # CSV: create zip of CSV files per sheet
                import zipfile
                result_path = os.path.join(EXPORT_DIR, f'{job_id}.zip')
                temp_path = os.path.join(EXPORT_DIR, f'{job_id}.tmp.zip')
                with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    for name in selected:
                        tbl = tables.get(name)
                        _update_job(job_id, progress=0.01 + 0.8 * (written_sheets / max(1, total_sheets)), message=f'Preparing CSV {name}')
                        try:
                            df = read_workflow_table_duckdb(tbl, bulan, unit, jabatan)
                        except HTTPException as exc:
                            if exc.status_code != 404:
                                raise
                            df = pd.DataFrame()
                        if name == 'FINAL ABSENSI':
                            verified_final = final_manifest(df.to_dict('records'))
                            if expected_final is not None:
                                assert_manifest(verified_final, expected_final)
                            if not verified_final['row_count']:
                                raise ValueError('Final Absensi kosong untuk periode yang dipilih')
                        csv_name = f"{name.replace(' ', '_')}.csv"
                        if df is None or df.empty:
                            # write placeholder
                            data = "-\n"
                            zf.writestr(csv_name, data)
                        else:
                            # stream df to csv bytes
                            csv_buf = df.to_csv(index=False).encode('utf-8')
                            if name == 'FINAL ABSENSI':
                                import io
                                reread = pd.read_csv(io.BytesIO(csv_buf), dtype=str, keep_default_na=False)
                                assert_manifest(final_manifest(reread.to_dict('records')), verified_final)
                            zf.writestr(csv_name, csv_buf)
                        written_sheets += 1
            # Publish under the reset fence; reading the snapshot and writing Excel need not block the app.
            message = ('Validasi lulus: ' + str(verified_final['row_count']) + ' baris Final; bruto ' + verified_final['gross'] + ', potongan ' + verified_final['deduction'] + ', dibayar ' + verified_final['paid']) if verified_final else 'Export complete'
            with DATABASE_GATE:
                assert_current_generation(generation)
                if calculation_id:
                    with duckdb.connect(DUCKDB_PATH) as latest:
                        active_id = latest.execute(
                            "SELECT calculation_id FROM absence_calculation_canonical WHERE period=?", [bulan]
                        ).fetchone()
                        active_manifest = (read_workflow_config(latest) or {}).get('_workflow') or {}
                        active_workflow = active_manifest.get('workflow_id') or active_manifest.get('workflowId') or 'default'
                        if active_id != (calculation_id,) or active_workflow != workflow_id:
                            raise ValueError('Hasil atau workbook berubah saat ekspor. Coba lagi.')
                os.replace(temp_path, result_path)
                if fmt == 'xlsx' and verified_final is not None:
                    Path(result_path + '.validation.json').write_text(json.dumps(verified_final), encoding='utf-8')
                _update_job(job_id, status='finished', finished_at=_now_iso(), progress=1.0, message=message, result_table=result_path)
            return result_path
        except Exception as e:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            code = e.code if isinstance(e, ExportValidationError) else 'EXPORT_QUERY_ERROR'
            message = e.message if isinstance(e, ExportValidationError) else str(e)
            _update_job(job_id, status='failed', finished_at=_now_iso(),
                        message=f'{code}: {message}', progress=0.0)
            raise
    finally:
        conn.close()

def _now_iso():
    return datetime.utcnow().isoformat() + 'Z'


def _update_job(job_id: str, **fields) -> None:
    conn = get_connection()
    try:
        sets = ', '.join([f'"{k}" = ?' for k in fields.keys()])
        params = list(fields.values()) + [job_id]
        conn.execute(f'UPDATE jobs SET {sets} WHERE id = ?', params)
        conn.commit()
    finally:
        conn.close()




def compute_harian_core(bulan: str, time_mode: str = 'original', unit: Optional[str] = None, job_id: Optional[str] = None) -> int:
    """
    Compute hitung_harian for given bulan and write to 'hitung_harian' table.
    This implementation ports the rules from calc.js and processes data in chunks,
    updating job progress to the jobs table and checking for cancellation.
    Returns number of rows written.
    """
    conn = get_connection()
    try:
        # Load configuration (workflow_config) if present
        cfg_row = None
        try:
            cur = conn.execute('SELECT * FROM workflow_config LIMIT 1')
            cfg_row = cur.fetchone()
        except Exception:
            cfg_row = None

        # Fallback defaults (mirror state.defaultConfig in frontend)
        default_cfg = {
            'holidays': [],
            'jabatanMaster': [],
            'masterPegawai': [],
            'jenisNoPotongan': [],
            'leavePeriods': [],
            'jamDasar': {'senKam': {'masuk': '07:30', 'pulang': '16:00'}, 'jumat': {'masuk': '07:30', 'pulang': '16:30'}},
            'flexi': {'enabled': True, 'masukStart': '07:31', 'masukEnd': '08:00', 'kompensasiSenKam': '16:30', 'kompensasiJumat': '17:00'},
            'keterlambatanTiers': [{'max': 30, 'pct': 0.5}, {'max': 60, 'pct': 1}, {'max': 90, 'pct': 1.5}, {'max': 120, 'pct': 2}, {'max': 9999, 'pct': 3}],
            'pulangCepatTiers': [{'max': 30, 'pct': 0.5}, {'max': 60, 'pct': 1}, {'max': 90, 'pct': 1.5}, {'max': 120, 'pct': 2}, {'max': 9999, 'pct': 3}],
            'minDurasiJam': 7.5,
            'potonganTidakLengkap': 3,
        }

        cfg = {}
        if cfg_row:
            # convert sqlite3.Row to dict and attempt to parse JSON fields
            rowd = dict(cfg_row)
            for k, v in rowd.items():
                if v is None:
                    continue
                if isinstance(v, str) and (v.startswith('{') or v.startswith('[')):
                    try:
                        cfg[k] = json.loads(v)
                    except Exception:
                        cfg[k] = v
                else:
                    cfg[k] = v
        # merge defaults
        for k, v in default_cfg.items():
            if k not in cfg:
                cfg[k] = v

        # Normalize some cfg keys
        if isinstance(cfg.get('holidays'), list):
            holiday_set = {h.get('d') for h in cfg.get('holidays', []) if isinstance(h, dict) and h.get('d')}
            holiday_name = {h.get('d'): h.get('n') for h in cfg.get('holidays', []) if isinstance(h, dict)}
        else:
            holiday_set = set()
            holiday_name = {}

        jab_method = {}
        for j in cfg.get('jabatanMaster') or []:
            name = (j.get('nama') if isinstance(j, dict) else None) or j
            metode = (j.get('metode') if isinstance(j, dict) else None) or 'reguler'
            if name:
                jab_method[str(name).strip().lower()] = metode

        peg_by_nip = {}
        if shared_master is None:
            raise RuntimeError('Layanan Master SDM kanonis tidak tersedia')
        master_rows = shared_master.SharedMasterService(DUCKDB_PATH).list(per_page=1000000, active=True)['rows']
        for p in master_rows:
            if isinstance(p, dict) and 'nip' in p:
                peg_by_nip[str(p['nip']).strip()] = p

        jenis_no = set([s.strip().lower() for s in (cfg.get('jenisNoPotongan') or []) if isinstance(s, str)])

        leave_periods = []
        for lp in cfg.get('leavePeriods') or []:
            if isinstance(lp, dict) and lp.get('nip') and lp.get('mulai') and lp.get('selesai'):
                leave_periods.append({'nip': str(lp.get('nip')).strip(), 'mulai': lp.get('mulai'), 'selesai': lp.get('selesai'), 'jenis': lp.get('jenis', '')})

        # Load raw_import filtered by bulan and unit
        # Prefer DuckDB for large imports
        try:
            import duckdb as _d
            dconn = _d.connect(DUCKDB_PATH)
            try:
                df_all = dconn.execute('SELECT * FROM "raw_import"').fetchdf()
            finally:
                dconn.close()
        except Exception:
            try:
                df_all = pd.read_sql_query('SELECT * FROM raw_import', conn)
            except Exception:
                return 0
        if df_all is None or (hasattr(df_all, 'empty') and df_all.empty):
            return 0

        # Normalize column names
        col_map = {c: c.strip().lower().replace(' ', '').replace('_', '') for c in df_all.columns}
        df_all.rename(columns=col_map, inplace=True)

        # Helper pick
        def pick(cols):
            for c in cols:
                if c in df_all.columns:
                    return c
            return None

        col_nip = pick(['nip', 'n'])
        col_nama = pick(['nama', 'name'])
        col_tanggal = pick(['tanggal', 'tgl', 'date'])
        col_tanggal_pulang = pick(['tanggalpulang', 'tglpulang', 'tanggalkeluar'])
        col_jam_masuk = pick(['jammansuk', 'jammasuk', 'masuk', 'jammasuk'])
        col_jam_pulang = pick(['jampulang', 'pulang', 'jampulang'])
        col_zona = pick(['zona', 'zone'])
        col_jenis = pick(['jenis', 'keterangan'])

        df_all[col_tanggal] = pd.to_datetime(df_all[col_tanggal], dayfirst=True, errors='coerce')
        df_all['bulan'] = df_all[col_tanggal].dt.strftime('%Y-%m')
        df = df_all[df_all['bulan'] == bulan].copy()
        if unit and 'unit' in df.columns:
            df = df[df['unit'] == unit].copy()
        if df.empty:
            return 0

        import numpy as np
        # parse times
        def parse_time_series(s):
            return pd.to_datetime(s.astype(str), format='%H:%M', errors='coerce')

        if col_jam_masuk in df.columns:
            tms = parse_time_series(df[col_jam_masuk])
            df['masukMin'] = tms.dt.hour * 60 + tms.dt.minute
        else:
            df['masukMin'] = np.nan
        if col_jam_pulang in df.columns:
            tps = parse_time_series(df[col_jam_pulang])
            df['pulangMin'] = tps.dt.hour * 60 + tps.dt.minute
        else:
            df['pulangMin'] = np.nan

        if col_tanggal_pulang and col_tanggal_pulang in df.columns:
            out_dates = pd.to_datetime(df[col_tanggal_pulang], dayfirst=True, errors='coerce')
            df['crossDay'] = out_dates.dt.normalize().gt(df[col_tanggal].dt.normalize())
        else:
            df['crossDay'] = False
        df['crossDay'] = df['crossDay'] | (
            df['masukMin'].notna() & df['pulangMin'].notna() & (df['pulangMin'] < df['masukMin'])
        )

        df['zona'] = df[col_zona] if (col_zona and col_zona in df.columns) else 'WIB'
        df['zona'] = df['zona'].fillna('WIB').astype(str).str.upper()

        zone_offset = {'WIB': 0, 'WITA': -60, 'WIT': -120}
        zone_offset_add = {'WIB': 0, 'WITA': 60, 'WIT': 120}
        df['masukNormMin'] = (df['masukMin'] + df['zona'].map(lambda z: zone_offset.get(z, 0))) % 1440
        df['pulangNormMin'] = (df['pulangMin'] + df['zona'].map(lambda z: zone_offset.get(z, 0))) % 1440
        df['masukNormMinAdd'] = (df['masukMin'] + df['zona'].map(lambda z: zone_offset_add.get(z, 0))) % 1440
        df['pulangNormMinAdd'] = (df['pulangMin'] + df['zona'].map(lambda z: zone_offset_add.get(z, 0))) % 1440

        # Prepare output table: create and remove existing bulan rows
        conn.execute('CREATE TABLE IF NOT EXISTS hitung_harian (nip TEXT, nama TEXT, jabatan TEXT, bulan TEXT, tanggal TEXT, zona TEXT, jamMasuk TEXT, jamPulang TEXT, durasi TEXT, jamNormalisasi TEXT, jamMasukAsli TEXT, jamPulangAsli TEXT, durasiMin REAL, metode TEXT, jenis TEXT, statusHariLibur TEXT, statusCuti TEXT, statusDuplikasi TEXT, statusLengkap TEXT, potKeterlambatan REAL, potPulangCepat REAL, potDurasi REAL, potTidakLengkap REAL, totalHarian REAL, flags TEXT)')
        try:
            if unit and 'unit' in df.columns:
                conn.execute('DELETE FROM hitung_harian WHERE bulan = ? AND unit = ?', (bulan, unit))
            else:
                conn.execute('DELETE FROM hitung_harian WHERE bulan = ?', (bulan,))
            conn.commit()
        except Exception:
            pass

        total = len(df)
        if total == 0:
            return 0

        chunk_size = 5000
        processed = 0
        rows_written = 0

        # utility functions
        def parse_time_to_min(s):
            try:
                return int(s.hour) * 60 + int(s.minute)
            except Exception:
                return None

        def fmt_time_min(m):
            if pd.isna(m) or m is None:
                return ''
            try:
                m = int(m)
                h = (m // 60) % 24
                mm = m % 60
                return f"{h:02d}:{mm:02d}"
            except Exception:
                return ''

        def fmt_dur_min(m):
            if pd.isna(m) or m is None:
                return ''
            try:
                m = int(m)
                h = m // 60
                mm = m % 60
                return f"{h}h {mm}m"
            except Exception:
                return ''

        def tier_pct(tiers, minutes):
            if minutes is None or pd.isna(minutes):
                return 0
            for t in tiers:
                if minutes <= t.get('max', 0):
                    return t.get('pct', 0)
            return tiers[-1].get('pct', 0) if tiers else 0

        def durasi_tier_pct(hours, minDurasi):
            if hours >= minDurasi:
                return 0
            if hours >= minDurasi - 0.5:
                return 0.5
            if hours >= minDurasi - 1.0:
                return 1
            if hours >= minDurasi - 1.5:
                return 1.5
            if hours >= minDurasi - 2.0:
                return 2
            return 3

        # process in chunks
        for start in range(0, total, chunk_size):
            end = min(start + chunk_size, total)
            chunk = df.iloc[start:end].copy()

            # check cancellation signal from jobs table
            if job_id:
                jr = conn.execute('SELECT status FROM jobs WHERE id = ?', (job_id,)).fetchone()
                if jr and jr['status'] == 'cancelled':
                    _update_job(job_id, status='cancelled', finished_at=_now_iso(), message='Cancelled by user', progress=processed / total if total else 0.0)
                    return rows_written

            # select useMasuk/usePulang
            if time_mode == 'original':
                chunk['useMasuk'] = chunk['masukMin']
                chunk['usePulang'] = chunk['pulangMin']
            elif time_mode == 'wib':
                chunk['useMasuk'] = chunk['masukNormMin']
                chunk['usePulang'] = chunk['pulangNormMin']
            elif time_mode == 'wib-add':
                chunk['useMasuk'] = chunk['masukNormMinAdd']
                chunk['usePulang'] = chunk['pulangNormMinAdd']
            else:
                chunk['useMasuk'] = chunk['masukMin']
                chunk['usePulang'] = chunk['pulangMin']

            chunk['actualPulang'] = chunk['usePulang'] + chunk['crossDay'].astype(int) * 1440
            chunk['durasiMin'] = (chunk['actualPulang'] - chunk['useMasuk']).clip(lower=0)

            # holiday/ weekend
            chunk['isHoliday'] = chunk[col_tanggal].dt.strftime('%Y-%m-%d').isin(holiday_set) | chunk[col_tanggal].dt.weekday.isin([5, 6])
            chunk['holidayLabel'] = chunk[col_tanggal].dt.strftime('%Y-%m-%d').map(lambda d: holiday_name.get(d, 'Akhir Pekan') if d in holiday_name or d in holiday_set else '')

            # jabatan & metode
            def resolve_jabatan_row(r):
                j = r.get('jabatan')
                return (str(j).strip() if pd.notna(j) else '') or 'Belum Ditentukan'

            chunk['jabatan_resolved'] = chunk.apply(resolve_jabatan_row, axis=1)
            chunk['metode'] = chunk['jabatan_resolved'].map(lambda j: 'durasi' if jab_method.get(str(j).strip().lower()) == 'durasi' else 'reguler')

            # leave periods (per-nip mask)
            chunk['statusCuti'] = ''
            for lp in leave_periods:
                mask = (chunk[col_nip].astype(str).str.strip() == lp['nip']) & (chunk[col_tanggal].dt.strftime('%Y-%m-%d') >= lp['mulai']) & (chunk[col_tanggal].dt.strftime('%Y-%m-%d') <= lp['selesai'])
                chunk.loc[mask, 'statusCuti'] = lp.get('jenis', '')

            # jenisNoPotongan
            chunk['jenis_norm'] = chunk[col_jenis].astype(str).fillna('').str.strip().str.lower()
            chunk['excused'] = chunk['jenis_norm'].isin(jenis_no)
            chunk.loc[chunk['excused'], 'statusCuti'] = chunk.loc[chunk['excused'], col_jenis]

            # completeness
            chunk['statusLengkap'] = chunk.apply(lambda r: 'Tidak Lengkap' if (pd.isna(r['useMasuk']) or pd.isna(r['usePulang'])) else 'Lengkap', axis=1)
            chunk['potTidakLengkap'] = chunk['statusLengkap'].map(lambda s: cfg.get('potonganTidakLengkap', 3) if s == 'Tidak Lengkap' else 0)

            # initialize penalties
            chunk['potKeterlambatan'] = 0.0
            chunk['potPulangCepat'] = 0.0
            chunk['potDurasi'] = 0.0
            chunk['flags'] = ''

            # apply metode specific rules
            for idx, r in chunk.iterrows():
                if r.get('statusCuti'):
                    chunk.at[idx, 'flags'] += 'PERIODE CUTI/SAKIT; '
                    chunk.at[idx, 'totalHarian'] = 0.0
                    continue
                if r.get('excused'):
                    chunk.at[idx, 'flags'] += 'JENIS BEBAS POTONGAN; '
                    chunk.at[idx, 'totalHarian'] = 0.0
                    continue
                if (r['metode'] == 'durasi' and r['jabatan_resolved'].lower() == 'wali asuh'
                        and r['statusLengkap'] == 'Tidak Lengkap'):
                    chunk.at[idx, 'potTidakLengkap'] = 0.0
                    chunk.at[idx, 'totalHarian'] = 0.0
                    continue
                if r.get('crossDay') and r['statusLengkap'] == 'Lengkap':
                    chunk.at[idx, 'flags'] += 'PULANG HARI BERIKUTNYA; '
                    chunk.at[idx, 'totalHarian'] = 0.0
                    continue
                if r['metode'] == 'durasi':
                    dur_hours = (r.get('durasiMin') or 0) / 60.0
                    pct = durasi_tier_pct(dur_hours, cfg.get('minDurasiJam', 7.5))
                    chunk.at[idx, 'potDurasi'] = pct
                else:
                    # reguler
                    if not r.get('isHoliday'):
                        dow = r[col_tanggal].weekday()
                        is_jumat = (dow == 4)
                        jd = cfg.get('jamDasar', {}).get('jumat' if is_jumat else 'senKam', {})
                        masuk_std = int(jd.get('masuk', '07:30').split(':')[0]) * 60 + int(jd.get('masuk', '07:30').split(':')[1])
                        pulang_std = int(jd.get('pulang', '16:00').split(':')[0]) * 60 + int(jd.get('pulang', '16:00').split(':')[1])

                        lateMin = max(0, (r.get('useMasuk') or 0) - masuk_std)
                        earlyMin = max(0, pulang_std - (r.get('actualPulang') or 0))

                        # FLEXI
                        fx = cfg.get('flexi', {}) or {}
                        if fx.get('enabled'):
                            fStart = int(fx.get('masukStart', '07:31').split(':')[0]) * 60 + int(fx.get('masukStart', '07:31').split(':')[1])
                            fEnd = int(fx.get('masukEnd', '08:00').split(':')[0]) * 60 + int(fx.get('masukEnd', '08:00').split(':')[1])
                            komp = int(fx.get('kompensasiJumat' if is_jumat else 'kompensasiSenKam', '16:30').split(':')[0]) * 60 + int(fx.get('kompensasiJumat' if is_jumat else 'kompensasiSenKam', '16:30').split(':')[1])
                            if (r.get('useMasuk') or 0) >= fStart and (r.get('useMasuk') or 0) <= fEnd and (r.get('actualPulang') or 0) > komp:
                                lateMin = 0
                                earlyMin = 0
                                chunk.at[idx, 'flags'] += 'FLEXI TIME (kompensasi); '

                        if lateMin > 0:
                            chunk.at[idx, 'potKeterlambatan'] = tier_pct(cfg.get('keterlambatanTiers', []), lateMin)
                        if earlyMin > 0:
                            chunk.at[idx, 'potPulangCepat'] = tier_pct(cfg.get('pulangCepatTiers', []), earlyMin)
                        chunk.at[idx, '_lateMin'] = lateMin
                        chunk.at[idx, '_earlyMin'] = earlyMin
                    else:
                        chunk.at[idx, 'flags'] += 'HARI LIBUR - TANPA POTONGAN REGULER; '

                # compute total
                total_pct = (chunk.at[idx, 'potKeterlambatan'] or 0) + (chunk.at[idx, 'potPulangCepat'] or 0) + (chunk.at[idx, 'potDurasi'] or 0) + (chunk.at[idx, 'potTidakLengkap'] or 0)
                chunk.at[idx, 'totalHarian'] = round(float(total_pct), 2)

            # format output columns
            chunk_out = pd.DataFrame()
            chunk_out['nip'] = chunk[col_nip] if col_nip in chunk.columns else ''
            chunk_out['nama'] = chunk[col_nama] if col_nama in chunk.columns else ''
            chunk_out['jabatan'] = chunk['jabatan_resolved']
            chunk_out['bulan'] = chunk['bulan']
            chunk_out['tanggal'] = chunk[col_tanggal].dt.strftime('%Y-%m-%d')
            chunk_out['zona'] = chunk['zona']
            chunk_out['jamMasuk'] = chunk['useMasuk'].map(fmt_time_min)
            chunk_out['jamPulang'] = chunk['usePulang'].map(fmt_time_min)
            chunk_out['durasi'] = chunk['durasiMin'].map(fmt_dur_min)
            def jam_norm_row(r):
                if time_mode == 'original':
                    return 'Original'
                if r.get('zona', 'WIB') == 'WIB':
                    return ''
                if time_mode == 'wib':
                    return f"{fmt_time_min(r.get('masukNormMin'))} - {fmt_time_min(r.get('pulangNormMin'))}"
                if time_mode == 'wib-add':
                    return f"{fmt_time_min(r.get('masukNormMinAdd'))} - {fmt_time_min(r.get('pulangNormMinAdd'))}"
                return ''
            chunk_out['jamNormalisasi'] = chunk.apply(jam_norm_row, axis=1)
            chunk_out['jamMasukAsli'] = chunk['masukMin'].map(fmt_time_min)
            chunk_out['jamPulangAsli'] = chunk['pulangMin'].map(fmt_time_min)
            chunk_out['durasiMin'] = chunk['durasiMin']
            chunk_out['metode'] = chunk['metode']
            chunk_out['jenis'] = chunk[col_jenis] if col_jenis in chunk.columns else ''
            chunk_out['statusHariLibur'] = chunk['isHoliday'].map(lambda v: 'Ya' if v else 'Tidak')
            chunk_out['statusCuti'] = chunk['statusCuti']
            chunk_out['statusDuplikasi'] = False
            chunk_out['statusLengkap'] = chunk['statusLengkap']
            chunk_out['potKeterlambatan'] = chunk['potKeterlambatan']
            chunk_out['potPulangCepat'] = chunk['potPulangCepat']
            chunk_out['potDurasi'] = chunk['potDurasi']
            chunk_out['potTidakLengkap'] = chunk['potTidakLengkap']
            chunk_out['totalHarian'] = chunk['totalHarian']
            chunk_out['flags'] = chunk['flags']

            # append to DB
            try:
                chunk_out.to_sql('hitung_harian', conn, if_exists='append', index=False)
            except Exception:
                pass

            written = len(chunk_out)
            processed += (end - start)
            rows_written += written
            # update progress
            if job_id:
                _update_job(job_id, progress=min(1.0, processed / total), message=f'Processed {processed} / {total}')

        return int(rows_written)
    finally:
        conn.close()


def compute_harian_duckdb(job_id: str, bulan: str, time_mode: str, unit: Optional[str], chunk_size: int = 50000) -> int:
    """
    Compute hitung_harian using DuckDB SQL in chunks and write results to DuckDB.
    Returns number of rows written.
    """
    import duckdb as _d
    dconn = _d.connect(DUCKDB_PATH)
    sqlite_conn = get_connection()
    try:
        # load config from DuckDB if available, else fallback to workflow_config in sqlite
        cfg = {}
        try:
            cfg_df = dconn.execute('SELECT * FROM "workflow_config"').fetchdf()
            if not cfg_df.empty:
                # workflow_config likely has JSON columns; take first row and parse fields that are JSON strings
                row = cfg_df.iloc[0].to_dict()
                for k, v in row.items():
                    if isinstance(v, str) and (v.startswith('{') or v.startswith('[')):
                        try:
                            cfg[k] = json.loads(v)
                        except Exception:
                            cfg[k] = v
                    else:
                        cfg[k] = v
        except Exception:
            # fallback to sqlite
            try:
                cur = sqlite_conn.execute('SELECT * FROM workflow_config LIMIT 1')
                r = cur.fetchone()
                if r:
                    rowd = dict(r)
                    for k, v in rowd.items():
                        if v is None:
                            continue
                        if isinstance(v, str) and (v.startswith('{') or v.startswith('[')):
                            try:
                                cfg[k] = json.loads(v)
                            except Exception:
                                cfg[k] = v
                        else:
                            cfg[k] = v
            except Exception:
                cfg = {}

        # apply defaults
        default_cfg = {
            'holidays': [],
            'jabatanMaster': [],
            'masterPegawai': [],
            'jenisNoPotongan': [],
            'leavePeriods': [],
            'jamDasar': {'senKam': {'masuk': '07:30', 'pulang': '16:00'}, 'jumat': {'masuk': '07:30', 'pulang': '16:30'}},
            'flexi': {'enabled': True, 'masukStart': '07:31', 'masukEnd': '08:00', 'kompensasiSenKam': '16:30', 'kompensasiJumat': '17:00'},
            'keterlambatanTiers': [{'max': 30, 'pct': 0.5}, {'max': 60, 'pct': 1}, {'max': 90, 'pct': 1.5}, {'max': 120, 'pct': 2}, {'max': 9999, 'pct': 3}],
            'pulangCepatTiers': [{'max': 30, 'pct': 0.5}, {'max': 60, 'pct': 1}, {'max': 90, 'pct': 1.5}, {'max': 120, 'pct': 2}, {'max': 9999, 'pct': 3}],
            'minDurasiJam': 7.5,
            'potonganTidakLengkap': 3,
        }
        for k, v in default_cfg.items():
            if k not in cfg:
                cfg[k] = v

        # helpers for SQL generation
        def build_tier_case(col_name, tiers):
            # returns SQL fragment: CASE WHEN col <= max1 THEN pct1 WHEN col <= max2 THEN pct2 ELSE pctN END
            if not tiers:
                return '0'
            parts = []
            for t in tiers:
                parts.append((t.get('max', 0), t.get('pct', 0)))
            case_sql = 'CASE'
            for mx, pct in parts:
                case_sql += f" WHEN {col_name} <= {mx} THEN {pct}"
            case_sql += f" ELSE {parts[-1][1]} END"
            return case_sql

        # prepare leave periods as temporary table if present
        leave_periods = cfg.get('leavePeriods') or []
        if leave_periods:
            vals = []
            for lp in leave_periods:
                nip = str(lp.get('nip', '')).replace("'", "''")
                mulai = str(lp.get('mulai', '')).replace("'", "''")
                selesai = str(lp.get('selesai', '')).replace("'", "''")
                jenis = str(lp.get('jenis', '')).replace("'", "''")
                vals.append(f"('{nip}','{mulai}','{selesai}','{jenis}')")
            vals_sql = ','.join(vals)
            dconn.execute(f"CREATE TEMPORARY TABLE _leave AS SELECT * FROM (VALUES {vals_sql}) AS v(nip, mulai, selesai, jenis)")
        else:
            dconn.execute("CREATE TEMPORARY TABLE _leave AS SELECT * FROM (VALUES ('','','','')) AS v(nip, mulai, selesai, jenis) WHERE FALSE")

        # build durasi CASE based on minDurasi
        minDur = float(cfg.get('minDurasiJam', 7.5))

        # parse jamDasar and flexi constants
        jd_senkam_masuk = cfg.get('jamDasar', {}).get('senKam', {}).get('masuk', '07:30')
        jd_senkam_pulang = cfg.get('jamDasar', {}).get('senKam', {}).get('pulang', '16:00')
        jd_jumat_masuk = cfg.get('jamDasar', {}).get('jumat', {}).get('masuk', '07:30')
        jd_jumat_pulang = cfg.get('jamDasar', {}).get('jumat', {}).get('pulang', '16:30')

        def time_to_min(t):
            h, m = str(t).split(':')
            return int(h) * 60 + int(m)
        sen_masuk_min = time_to_min(jd_senkam_masuk)
        sen_pulang_min = time_to_min(jd_senkam_pulang)
        jum_masuk_min = time_to_min(jd_jumat_masuk)
        jum_pulang_min = time_to_min(jd_jumat_pulang)

        fx = cfg.get('flexi', {}) or {}
        fx_enabled = bool(fx.get('enabled'))
        fx_masuk_start = time_to_min(fx.get('masukStart', '07:31'))
        fx_masuk_end = time_to_min(fx.get('masukEnd', '08:00'))
        fx_kompensasi_sen = time_to_min(fx.get('kompensasiSenKam', '16:30'))
        fx_kompensasi_jum = time_to_min(fx.get('kompensasiJumat', '17:00'))

        # ensure hitung_harian table exists empty for appending
        dconn.execute('''CREATE TABLE IF NOT EXISTS hitung_harian (
            nip TEXT, nama TEXT, jabatan TEXT, bulan TEXT, tanggal DATE, zona TEXT,
            jamMasuk TEXT, jamPulang TEXT, durasi TEXT, jamNormalisasi TEXT,
            jamMasukAsli TEXT, jamPulangAsli TEXT, durasiMin DOUBLE, metode TEXT,
            jenis TEXT, statusHariLibur TEXT, statusCuti TEXT,
            statusDuplikasi BOOLEAN, statusLengkap TEXT,
            potKeterlambatan DOUBLE, potPulangCepat DOUBLE, potDurasi DOUBLE,
            potTidakLengkap DOUBLE, totalHarian DOUBLE, flags TEXT
        )''')

        raw_columns = {str(name).lower(): str(name) for name in
                       dconn.execute('SELECT * FROM raw_import LIMIT 0').fetchdf().columns}

        # compute total rows to process
        count_q = f"SELECT COUNT(*) AS c FROM raw_import WHERE substr(tanggal,1,7) = '{bulan}'"
        if unit and unit != '' and 'unit' in raw_columns:
            count_q = f"SELECT COUNT(*) AS c FROM raw_import WHERE substr(tanggal,1,7) = '{bulan}' AND unit = '{unit}'"
        total = dconn.execute(count_q).fetchone()[0]
        if total == 0:
            return 0

        rows_written = 0
        processed = 0
        offset = 0
        while offset < total:
            limit = min(chunk_size, total - offset)
            # build SQL selecting transformed fields
            # choose useMasuk/usePulang depending on time_mode
            if time_mode == 'original':
                use_masuk_expr = 'masukMin'
                use_pulang_expr = 'pulangMin'
            elif time_mode == 'wib':
                use_masuk_expr = 'masukNormMin'
                use_pulang_expr = 'pulangNormMin'
            elif time_mode == 'wib-add':
                use_masuk_expr = 'masukNormMinAdd'
                use_pulang_expr = 'pulangNormMinAdd'
            else:
                use_masuk_expr = 'masukMin'
                use_pulang_expr = 'pulangMin'

            out_date_column = next((raw_columns[name] for name in
                                    ('tanggal_pulang', 'tanggalpulang', 'tglpulang', 'tanggalkeluar')
                                    if name in raw_columns), None)
            if out_date_column:
                safe_out_date = '"' + out_date_column.replace('"', '""') + '"'
                out_date_is_later = (
                    f"coalesce(try_cast({safe_out_date} AS DATE) > try_cast(tanggal AS DATE), FALSE)"
                )
            else:
                out_date_is_later = 'FALSE'
            cross_day_expr = (
                f"(({use_pulang_expr} < {use_masuk_expr}) OR ({out_date_is_later}))"
            )
            actual_pulang_expr = (
                f"CASE WHEN {cross_day_expr} THEN {use_pulang_expr} + 1440 "
                f"ELSE {use_pulang_expr} END"
            )
            duration_expr = f"greatest(0, {actual_pulang_expr} - {use_masuk_expr})"
            hours_expr = f"({duration_expr} / 60.0)"
            is_friday_expr = "strftime('%w', try_cast(tanggal AS DATE)) = '5'"
            standard_in_expr = (
                f"CASE WHEN {is_friday_expr} THEN {jum_masuk_min} ELSE {sen_masuk_min} END"
            )
            standard_out_expr = (
                f"CASE WHEN {is_friday_expr} THEN {jum_pulang_min} ELSE {sen_pulang_min} END"
            )
            late_min_expr = f"greatest(0, {use_masuk_expr} - {standard_in_expr})"
            early_min_expr = f"greatest(0, {standard_out_expr} - {actual_pulang_expr})"
            late_case = build_tier_case(late_min_expr, cfg.get('keterlambatanTiers', []))
            early_case = build_tier_case(early_min_expr, cfg.get('pulangCepatTiers', []))
            durasi_case = (
                f"CASE WHEN {hours_expr} >= {minDur} THEN 0 "
                f"WHEN {hours_expr} >= {minDur - 0.5} THEN 0.5 "
                f"WHEN {hours_expr} >= {minDur - 1.0} THEN 1 "
                f"WHEN {hours_expr} >= {minDur - 1.5} THEN 1.5 "
                f"WHEN {hours_expr} >= {minDur - 2.0} THEN 2 ELSE 3 END"
            )
            duration_jobs = [
                str(item.get('nama') or '').strip().lower()
                for item in (cfg.get('jabatanMaster') or [])
                if isinstance(item, dict) and item.get('metode') == 'durasi' and item.get('nama')
            ]
            duration_job_values = ', '.join(
                "'" + name.replace("'", "''") + "'" for name in duration_jobs
            )
            duration_method_expr = (
                f"lower(coalesce(jabatan, '')) IN ({duration_job_values})"
                if duration_job_values else 'FALSE'
            )
            complete_expr = f"({use_masuk_expr} IS NOT NULL AND {use_pulang_expr} IS NOT NULL)"
            late_pct_expr = (
                f"CASE WHEN {complete_expr} AND NOT ({cross_day_expr}) AND NOT ({duration_method_expr}) "
                f"AND {late_min_expr} > 0 THEN ({late_case}) ELSE 0 END"
            )
            early_pct_expr = (
                f"CASE WHEN {complete_expr} AND NOT ({cross_day_expr}) AND NOT ({duration_method_expr}) "
                f"AND {early_min_expr} > 0 THEN ({early_case}) ELSE 0 END"
            )
            duration_pct_expr = (
                f"CASE WHEN {complete_expr} AND NOT ({cross_day_expr}) AND ({duration_method_expr}) "
                f"THEN ({durasi_case}) ELSE 0 END"
            )
            incomplete_pct_expr = (
                f"CASE WHEN NOT {complete_expr} AND NOT (({duration_method_expr}) "
                f"AND lower(trim(coalesce(jabatan, ''))) = 'wali asuh') "
                f"THEN {cfg.get('potonganTidakLengkap', 3)} ELSE 0 END"
            )
            total_pct_expr = (
                f"({incomplete_pct_expr} + {late_pct_expr} + "
                f"{early_pct_expr} + {duration_pct_expr})"
            )
            flags_expr = (
                f"CASE WHEN {cross_day_expr} THEN 'PULANG HARI BERIKUTNYA' ELSE '' END"
            )

            # Query to compute chunk
            sql = f"""INSERT INTO hitung_harian (
                      nip, nama, jabatan, bulan, tanggal, zona, jamMasuk, jamPulang,
                      durasi, jamNormalisasi, jamMasukAsli, jamPulangAsli, durasiMin,
                      metode, jenis, statusHariLibur, statusCuti, statusDuplikasi,
                      statusLengkap, potTidakLengkap, potKeterlambatan,
                      potPulangCepat, potDurasi, totalHarian, flags
                  ) """ \
                  f"SELECT raw_import.nip AS nip, raw_import.nama AS nama, COALESCE(raw_import.jabatan, '') AS jabatan, substr(raw_import.tanggal,1,7) AS bulan, raw_import.tanggal::DATE AS tanggal, raw_import.zona, " \
                  f"printf('%02d:%02d', CAST(({use_masuk_expr} / 60) AS INTEGER), CAST(({use_masuk_expr} % 60) AS INTEGER)) AS jamMasuk, " \
                  f"printf('%02d:%02d', CAST(({use_pulang_expr} / 60) AS INTEGER), CAST(({use_pulang_expr} % 60) AS INTEGER)) AS jamPulang, " \
                  f"printf('%dh %dm', CAST(({duration_expr} / 60) AS INTEGER), CAST(({duration_expr} % 60) AS INTEGER)) AS durasi, " \
                  f"CASE WHEN '{time_mode}' = 'original' THEN 'Original' WHEN zona = 'WIB' THEN '' WHEN '{time_mode}' = 'wib' THEN printf('%02d:%02d', CAST((masukNormMin/60) AS INTEGER), CAST((masukNormMin%60) AS INTEGER)) || ' - ' || printf('%02d:%02d', CAST((pulangNormMin/60) AS INTEGER), CAST((pulangNormMin%60) AS INTEGER)) WHEN '{time_mode}' = 'wib-add' THEN printf('%02d:%02d', CAST((masukNormMinAdd/60) AS INTEGER), CAST((masukNormMinAdd%60) AS INTEGER)) || ' - ' || printf('%02d:%02d', CAST((pulangNormMinAdd/60) AS INTEGER), CAST((pulangNormMinAdd%60) AS INTEGER)) ELSE '' END AS jamNormalisasi, " \
                  f"printf('%02d:%02d', CAST((masukMin/60) AS INTEGER), CAST((masukMin%60) AS INTEGER)) AS jamMasukAsli, " \
                  f"printf('%02d:%02d', CAST((pulangMin/60) AS INTEGER), CAST((pulangMin%60) AS INTEGER)) AS jamPulangAsli, " \
                  f"({duration_expr})::DOUBLE AS durasiMin, " \
                  f"CASE WHEN ({duration_method_expr}) THEN 'durasi' ELSE 'reguler' END AS metode, " \
                  f"COALESCE(raw_import.jenis, '') AS jenis, " \
                  f"CASE WHEN substr(tanggal,1,10) IN ({', '.join([f"'{h.get('d')}'" for h in (cfg.get('holidays') or []) if isinstance(h, dict) and h.get('d')])}) THEN 'Ya' ELSE 'Tidak' END AS statusHariLibur, " \
                  f"COALESCE(lp.jenis, '') AS statusCuti, " \
                  f"FALSE AS statusDuplikasi, " \
                  f"CASE WHEN {use_masuk_expr} IS NULL OR {use_pulang_expr} IS NULL THEN 'Tidak Lengkap' ELSE 'Lengkap' END AS statusLengkap, " \
                  f"({incomplete_pct_expr}) AS potTidakLengkap, " \
                  f"({late_pct_expr}) AS potKeterlambatan, " \
                  f"({early_pct_expr}) AS potPulangCepat, " \
                  f"({duration_pct_expr}) AS potDurasi, " \
                  f"({total_pct_expr})::DOUBLE AS totalHarian, " \
                  f"{flags_expr} AS flags " \
                  f"FROM raw_import LEFT JOIN _leave lp ON (CAST(raw_import.nip AS TEXT) = CAST(lp.nip AS TEXT) AND raw_import.tanggal::DATE BETWEEN lp.mulai::DATE AND lp.selesai::DATE) " \
                  f"WHERE substr(raw_import.tanggal,1,7) = '{bulan}' "
            if unit and unit != '' and 'unit' in raw_columns:
                sql += f" AND unit = '{unit}' "
            sql += f" LIMIT {limit} OFFSET {offset}"

            dconn.execute(sql)
            offset += limit
            processed += limit
            rows_written += dconn.execute('SELECT COUNT(*) FROM hitung_harian').fetchone()[0] - rows_written
            # update progress in sqlite
            progress = min(1.0, processed / total)
            _update_job(job_id, progress=progress, message=f'Processed {processed} / {total}', status='running')
            # check cancel
            jr = sqlite_conn.execute('SELECT status FROM jobs WHERE id = ?', (job_id,)).fetchone()
            if jr and jr['status'] == 'cancelled':
                _update_job(job_id, status='cancelled', finished_at=_now_iso(), message='Cancelled by user', progress=processed / total if total else 0.0)
                return rows_written
        # finished
        return rows_written
    finally:
        try:
            dconn.close()
        except Exception:
            pass
        sqlite_conn.close()


@database_job
def run_compute_harian_job(job_id: str, bulan: str, time_mode: str, unit: Optional[str],
                           job_generation: int):
    # Called in background process
    try:
        # Revalidate only after the worker owns DATABASE_GATE. A reset that won
        # the gate after dispatch invalidates this job before its first write.
        assert_current_generation(job_generation)
        _update_job(job_id, status='running', started_at=_now_iso(), progress=0.0, message='Started')
        # Run compute using DuckDB SQL
        rows = compute_harian_duckdb(job_id, bulan, time_mode, unit)
        _update_job(job_id, status='finished', finished_at=_now_iso(), progress=1.0, message='Completed', result_table='hitung_harian', row_count=rows)
    except StaleGenerationError as exc:
        _update_job(job_id, status='stale_after_reset', finished_at=_now_iso(), progress=0.0,
                    message='STALE_AFTER_RESET: ' + str(exc))
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        _update_job(job_id, status='failed', finished_at=_now_iso(), progress=0.0, message=str(exc) + '\n' + tb)


@app.post('/api/compute/harians')
def api_compute_harians(bulan: str = Query(..., description='Bulan contoh: 2026-08'), timeMode: str = Query('original'), unit: Optional[str] = Query(None)):
    """Start an asynchronous compute job for hitung_harian."""
    job_id = str(uuid.uuid4())
    job_generation = current_reset_generation()
    payload = {'bulan': bulan, 'timeMode': timeMode, 'unit': unit,
               'reset_generation': job_generation}
    conn = get_connection()
    try:
        ensure_jobs_table(conn)
        conn.execute('INSERT INTO jobs (id, type, status, payload, progress, message) VALUES (?, ?, ?, ?, ?, ?)', (job_id, 'hitung_harian', 'queued', json.dumps(payload, ensure_ascii=False), 0.0, 'Queued'))
        conn.commit()
    finally:
        conn.close()
    # Keep DuckDB jobs in the server process and preserve queue cancellation.
    future = _executor.submit(run_compute_harian_job, job_id, bulan, timeMode, unit, job_generation)
    _job_futures[job_id] = future
    return {'job_id': job_id, 'status': 'queued'}


@app.post('/api/jobs/export')
def create_export_job(payload: dict) -> dict:
    """Create a background export job that generates an XLSX or ZIP(CSV) file in backend/exports.
    Payload should include: bulan (YYYY-MM), optional unit, optional jabatan, optional sheet_names
    (list of sheet display names), optional format ('xlsx'|'csv'). Returns the created job id.
    """
    bulan = str(payload.get('bulan') or '')
    if not bulan:
        raise HTTPException(status_code=400, detail={
            "code": "EXPORT_SOURCE_NOT_READY",
            "message": "Periode export wajib diisi.",
        })
    if not EXPORT_PERIOD_RE.fullmatch(bulan):
        raise HTTPException(status_code=400, detail={
            "code": "EXPORT_SOURCE_NOT_READY",
            "message": f"Periode export tidak valid: {bulan}.",
        })
    unit = payload.get('unit')
    jabatan = payload.get('jabatan')
    sheet_names = payload.get('sheet_names') or []
    unsupported = [name for name in sheet_names if name not in {
        'DATA SUMBER', 'HITUNG HARIAN', 'REKAP SDM', 'SEMI FINAL', 'ANOMALI', 'FINAL ABSENSI',
        'FINAL + SKP', 'FINAL KOMBINASI', 'FINAL + SKP KOMBINASI',
    }]
    if unsupported:
        raise HTTPException(status_code=400, detail={
            "code": "EXPORT_SOURCE_NOT_READY",
            "message": "Unsupported export source: " + ", ".join(unsupported),
        })
    fmt = str(payload.get('format') or 'xlsx').lower()
    if fmt not in ('xlsx', 'csv'):
        fmt = 'xlsx'
    new_job_id = uuid.uuid4().hex
    conn = get_connection()
    try:
        ensure_jobs_table(conn)
        conn.execute('INSERT INTO jobs (id, type, status, payload, progress, message, started_at, finished_at, result_table) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (new_job_id, 'export', 'queued', json.dumps({'bulan': bulan, 'unit': unit, 'jabatan': jabatan, 'sheet_names': sheet_names, 'format': fmt}, ensure_ascii=False), 0.0, 'Queued', None, None, None))
        conn.commit()
    finally:
        conn.close()
    future = _executor.submit(run_export_job, new_job_id, bulan, unit, sheet_names, fmt, jabatan, payload.get('expected_final'), payload.get('calculation_id'), current_reset_generation())
    _job_futures[new_job_id] = future
    return {'status': 'ok', 'job_id': new_job_id}


@app.get('/api/jobs/{job_id}')
def api_get_job(job_id: str):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Job not found')
        return dict(row)
    finally:
        conn.close()


@app.get('/api/jobs')
def api_list_jobs(limit: int = Query(50, ge=1, le=1000)):
    conn = get_connection()
    try:
        rows = conn.execute('SELECT * FROM jobs ORDER BY started_at DESC NULLS LAST LIMIT ?', (limit,)).fetchall()
        return {'status': 'ok', 'jobs': [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get('/api/jobs/{job_id}/download')
def api_download_job_result(job_id: str):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Job not found')
        if row['status'] != 'finished' or not row['result_table']:
            raise HTTPException(status_code=404, detail='Result not available')
        path = row['result_table']
        # If stored as absolute path, use it; otherwise assume relative to EXPORT_DIR
        if not os.path.isabs(path):
            path = os.path.join(EXPORT_DIR, path)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail='Result file missing')
        if path.endswith('.xlsx'):
            media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        elif path.endswith('.zip'):
            media = 'application/zip'
        else:
            media = 'application/octet-stream'
        download_name = os.path.basename(path)
        try:
            payload = json.loads(row['payload'] or '{}')
            bulan = payload.get('bulan')
            if bulan and path.endswith('.xlsx'):
                download_name = f"EVKIN_ABSENSI_{bulan}.xlsx"
        except Exception:
            pass
        return FileResponse(path, media_type=media, filename=download_name)
    finally:
        conn.close()


@app.get('/api/compute/harians/result')
def api_compute_harians_result(bulan: str = Query(...), unit: Optional[str] = Query(None), limit: int = Query(50)):
    df = read_workflow_table_global('hitung_harian', bulan, unit)
    if df.empty:
        return {'rows': [], 'count': 0}
    df = df.head(limit)
    return {'rows': df.to_dict(orient='records'), 'count': len(df)}


@app.post('/api/jobs/{job_id}/cancel')
def api_cancel_job(job_id: str):
    # existing cancel behavior remains

    """Cancel a queued/running job. Attempts to cancel the running future and marks job as cancelled in DB."""
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Job tidak ditemukan')
        status = row['status']
        if status in ('finished', 'failed', 'cancelled'):
            return {'job_id': job_id, 'status': status}
        # attempt to cancel future
        fut = _job_futures.get(job_id)
        if fut:
            fut.cancel()
        _update_job(job_id, status='cancelled', finished_at=_now_iso(), message='Cancelled by user', progress=row['progress'] or 0.0)
        return {'job_id': job_id, 'status': 'cancelled'}
    finally:
        conn.close()


@app.post('/api/jobs/{job_id}/retry')
def api_retry_job(job_id: str):
    """Retry a failed or cancelled job by enqueuing a new job with same payload."""
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail='Job tidak ditemukan')
        payload = row['payload']
        try:
            payload_obj = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            payload_obj = {}
        new_job_id = str(uuid.uuid4())
        job_generation = current_reset_generation()
        payload_obj['reset_generation'] = job_generation
        ensure_jobs_table(conn)
        conn.execute('INSERT INTO jobs (id, type, status, payload, progress, message) VALUES (?, ?, ?, ?, ?, ?)', (new_job_id, row['type'] or 'hitung_harian', 'queued', json.dumps(payload_obj, ensure_ascii=False), 0.0, 'Retried'))
        conn.commit()
        # submit
        _job_futures[new_job_id] = _executor.submit(
            run_compute_harian_job, new_job_id, payload_obj.get('bulan'),
            payload_obj.get('timeMode', 'original'), payload_obj.get('unit'), job_generation
        )
        return {'job_id': new_job_id, 'status': 'queued'}
    finally:
        conn.close()





if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

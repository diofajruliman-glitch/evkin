from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import duckdb
from backend.lazy_imports import lazy_module

pd = lazy_module("pandas")
from typing import Optional
import os
from pathlib import Path
from fastapi.responses import StreamingResponse
import io
from backend.runtime_guard import claim_database, initialize_runtime_state, serialize_database_requests
from backend.security import ApiKeyMiddleware, CORS_HEADERS, CORS_METHODS, allowed_origins

# Use same DuckDB file as backend by default (configurable via EVKIN_DUCKDB_PATH env var)
# Default now points to backend/data/EVKIN_MASTER.duckdb to act as the single source of truth.
DUCKDB_PATH = os.environ.get('EVKIN_DUCKDB_PATH') or str(Path(__file__).resolve().parent / 'data' / 'EVKIN_MASTER.duckdb')
# Optional API key to protect analytics endpoints. Read at request time so tests and runtime overrides work after import.

app = FastAPI(title="HR - DuckDB Analytics API")
app.middleware('http')(serialize_database_requests)
app.add_middleware(ApiKeyMiddleware)


@app.on_event('startup')
def claim_analytics_database():
    claim_database(DUCKDB_PATH)
    initialize_runtime_state(DUCKDB_PATH)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=False,
    allow_methods=CORS_METHODS,
    allow_headers=CORS_HEADERS,
)

# Avoid holding a long-lived DuckDB connection at import time. Each request opens
# a dedicated connection to prevent file locks when the main EVKIN backend also
# accesses the same workflow database.
def get_conn():
    return duckdb.connect(database=DUCKDB_PATH, read_only=False)


# Attempt to mount generic_compare_api if present (non-fatal)
try:
    import importlib as _ilm
    _gcmp = _ilm.import_module('backend.generic_compare_api')
    app.include_router(_gcmp.router, prefix='')
    print('Mounted generic_compare_api at /compare')
except Exception:
    pass

# Attempt to mount audit_api if present (non-fatal)
try:
    import importlib as _ilm2
    _audit = _ilm2.import_module('backend.audit_api')
    app.include_router(_audit.router, prefix='')
    print('Mounted audit_api')
except Exception:
    pass


def _require_api_key(request: Request):
    expected_key = os.environ.get('EVKIN_API_KEY') or os.environ.get('EVKIN_ANALYTICS_API_KEY')
    if not expected_key:
        return True
    key = request.headers.get('x-evkin-api-key') or request.headers.get('x-api-key') or request.headers.get('authorization') or request.cookies.get('evkin_local_api_key')
    if key and key.lower().startswith('bearer '):
        key = key.split(' ', 1)[1]
    if key != expected_key:
        raise HTTPException(status_code=401, detail='Unauthorized')
    return True

# Simple health check
@app.get("/health")
def health():
    return {"status": "ok", "service": "evkin-analytics"}

# Pydantic model for search parameters
class PeopleQuery(BaseModel):
    offset: int = 0
    limit: int = 100
    q: Optional[str] = None

@app.get("/people")
def list_people(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000), q: Optional[str] = Query(None)):
    """Return paginated people. If q provided, performs a case-insensitive search on name."""
    _require_api_key(request)
    try:
        base_sql = "SELECT * FROM people"
        params = []
        if q:
            # Use parameter binding to avoid SQL injection
            sql = base_sql + " WHERE name ILIKE '%' || ? || '%' ORDER BY id LIMIT ? OFFSET ?"
            params = [q, limit, offset]
        else:
            sql = base_sql + " ORDER BY id LIMIT ? OFFSET ?"
            params = [limit, offset]
        with get_conn() as con:
            df = con.execute(sql, params).fetchdf()
        return {"rows": df.to_dict(orient="records"), "count": len(df)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/load-csv")
def load_csv(request: Request, payload: dict):
    _require_api_key(request)
    """Load CSV into DuckDB 'people' table. Overwrites existing table.
    Example: POST /load-csv with JSON body {"path":"/full/path/to/file.csv"}
    """
    path = str(payload.get('path') or '')
    if not path:
        raise HTTPException(status_code=400, detail='Missing path')
    try:
        with get_conn() as con:
            # using DuckDB's read_csv_auto for speed; fallback to pandas if necessary
            con.execute(f"CREATE OR REPLACE TABLE people AS SELECT * FROM read_csv_auto(?)", [path])
        return {"status": "loaded"}
    except Exception as e:
        # fallback using pandas
        try:
            df = pd.read_csv(path)
            with get_conn() as con:
                con.register('temp_df', df)
                con.execute('CREATE OR REPLACE TABLE people AS SELECT * FROM temp_df')
                try:
                    con.unregister('temp_df')
                except Exception:
                    pass
            return {"status": "loaded_pandas", "rows": len(df)}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"duckdb_error: {e}; pandas_error: {e2}")


@app.get('/workflow/flags')
def workflow_flags(request: Request, bulan: Optional[str] = Query(None)):
    _require_api_key(request)
    """Read workflow_flags from DuckDB; if bulan provided filter keys ending with :{bulan}."""
    try:
        with get_conn() as con:
            try:
                df = con.execute("SELECT k, verified, updated_at FROM workflow_flags").fetchdf()
            except Exception:
                return {"status": "ok", "flags": {}}
            if df.empty:
                return {"status": "ok", "flags": {}}
            if bulan:
                suffix = ':' + bulan
                try:
                    df = df[df['k'].str.endswith(suffix)]
                except Exception:
                    df['k'] = df['k'].astype(str)
                    df = df[df['k'].str.endswith(suffix)]
            flags = {}
            for _, row in df.iterrows():
                flags[row['k']] = {"verified": bool(int(row['verified'])), "updated_at": row['updated_at']}
            return {"status": "ok", "flags": flags}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/stats/counts')
def stats_counts(request: Request, bulan: Optional[str] = Query(None)):
    _require_api_key(request)
    """Return row counts for main workflow tables from DuckDB (fast)."""
    tables = ['raw_import', 'hitung_harian', 'rekap_sdm', 'final_absensi']
    results = {}
    try:
        with get_conn() as con:
            for t in tables:
                try:
                    if bulan:
                        cnt = con.execute(f"SELECT COUNT(*) AS c FROM \"{t}\" WHERE bulan = ?", [bulan]).fetchone()[0]
                    else:
                        cnt = con.execute(f"SELECT COUNT(*) AS c FROM \"{t}\"").fetchone()[0]
                except Exception:
                    cnt = 0
                results[t] = int(cnt)
        return {"status": "ok", "counts": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/stats/per-unit')
def stats_per_unit(request: Request, bulan: Optional[str] = Query(None)):
    _require_api_key(request)
    """Return aggregated counts per unit from final_absensi (or hitung_harian if final missing)."""
    try:
        table = 'final_absensi'
        with get_conn() as con:
            try:
                if bulan:
                    df = con.execute(f"SELECT unit, COUNT(*) AS cnt FROM \"{table}\" WHERE bulan = ? GROUP BY unit", [bulan]).fetchdf()
                else:
                    df = con.execute(f"SELECT unit, COUNT(*) AS cnt FROM \"{table}\" GROUP BY unit").fetchdf()
            except Exception:
                # fallback to hitung_harian
                table = 'hitung_harian'
                if bulan:
                    df = con.execute(f"SELECT unit, COUNT(*) AS cnt FROM \"{table}\" WHERE bulan = ? GROUP BY unit", [bulan]).fetchdf()
                else:
                    df = con.execute(f"SELECT unit, COUNT(*) AS cnt FROM \"{table}\" GROUP BY unit").fetchdf()
            flags = {row['unit'] if row['unit'] is not None else '': int(row['cnt']) for _, row in df.iterrows()} if not df.empty else {}
        return {"status": "ok", "by_unit": flags}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/stats/per-jabatan')
def stats_per_jabatan(request: Request, bulan: Optional[str] = Query(None)):
    _require_api_key(request)
    """Aggregate counts per jabatan from final_absensi (fallback to hitung_harian)."""
    try:
        table = 'final_absensi'
        with get_conn() as con:
            try:
                if bulan:
                    df = con.execute(f"SELECT jabatan, COUNT(*) AS cnt FROM \"{table}\" WHERE bulan = ? GROUP BY jabatan", [bulan]).fetchdf()
                else:
                    df = con.execute(f"SELECT jabatan, COUNT(*) AS cnt FROM \"{table}\" GROUP BY jabatan").fetchdf()
            except Exception:
                table = 'hitung_harian'
                if bulan:
                    df = con.execute(f"SELECT jabatan, COUNT(*) AS cnt FROM \"{table}\" WHERE bulan = ? GROUP BY jabatan", [bulan]).fetchdf()
                else:
                    df = con.execute(f"SELECT jabatan, COUNT(*) AS cnt FROM \"{table}\" GROUP BY jabatan").fetchdf()
            res = {row['jabatan'] if row['jabatan'] is not None else '': int(row['cnt']) for _, row in df.iterrows()} if not df.empty else {}
        return {"status": "ok", "by_jabatan": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/stats/per-status')
def stats_per_status(request: Request, bulan: Optional[str] = Query(None)):
    _require_api_key(request)
    """Aggregate counts by status (e.g., statusLengkap) from hitung_harian."""
    try:
        table = 'hitung_harian'
        with get_conn() as con:
            try:
                if bulan:
                    df = con.execute(f"SELECT statusLengkap AS status, COUNT(*) AS cnt FROM \"{table}\" WHERE bulan = ? GROUP BY statusLengkap", [bulan]).fetchdf()
                else:
                    df = con.execute(f"SELECT statusLengkap AS status, COUNT(*) AS cnt FROM \"{table}\" GROUP BY statusLengkap").fetchdf()
            except Exception:
                return {"status": "ok", "by_status": {}}
            res = {row['status'] if row['status'] is not None else '': int(row['cnt']) for _, row in df.iterrows()} if not df.empty else {}
        return {"status": "ok", "by_status": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/export/aggregates')
def export_aggregates(request: Request, tipe: str = Query(..., description='one of per-unit, per-jabatan, per-status'), bulan: Optional[str] = Query(None), unit: Optional[str] = Query(None)):
    _require_api_key(request)
    """Return CSV of requested aggregate type. Optional unit filter limits results to a specific unit."""
    try:
        params = []
        where_clauses = []
        if bulan:
            where_clauses.append('bulan = ?')
            params.append(bulan)
        if unit:
            where_clauses.append('unit = ?')
            params.append(unit)
        where_sql = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        with get_conn() as con:
            if tipe == 'per-unit':
                # per-unit ignores unit filter (it would be redundant) but supports bulan filter
                sql = f"SELECT unit, COUNT(*) AS cnt FROM \"final_absensi\"{(' WHERE bulan = ?' if bulan else '')} GROUP BY unit"
                rows = con.execute(sql, ([bulan] if bulan else [])).fetchdf()
                rows.columns = ['unit', 'count']
            elif tipe == 'per-jabatan':
                sql = f"SELECT jabatan, COUNT(*) AS cnt FROM \"final_absensi\"{where_sql} GROUP BY jabatan"
                rows = con.execute(sql, params).fetchdf()
                rows.columns = ['jabatan', 'count']
            elif tipe == 'per-status':
                sql = f"SELECT statusLengkap AS status, COUNT(*) AS cnt FROM \"hitung_harian\"{where_sql} GROUP BY statusLengkap"
                rows = con.execute(sql, params).fetchdf()
                rows.columns = ['status', 'count']
            else:
                raise HTTPException(status_code=400, detail='Invalid tipe')
            # Convert to CSV
            buf = io.StringIO()
            rows.to_csv(buf, index=False)
            buf.seek(0)
            suffix = (f'{unit}_' if unit else '') + (bulan or 'all')
            filename = f'aggregates_{tipe}_{suffix}.csv'
            return StreamingResponse(io.BytesIO(buf.getvalue().encode('utf-8')), media_type='text/csv', headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

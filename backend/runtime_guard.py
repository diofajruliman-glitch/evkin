"""Single-writer database gate and persistent reset-generation guard."""
import atexit
import asyncio
import functools
import os
from pathlib import Path
import threading

import duckdb


DATABASE_GATE = threading.Lock()
_STATE_LOCK = threading.RLock()
_owners = {}
_runtime_db_path = None
_reset_in_progress = False
_reset_epoch = 0
_reset_generation = 0

GENERATION_HEADER = 'X-EVKIN-Generation'
RUNTIME_TABLE = 'system_runtime_state'
RESET_PATHS = {
    '/api/admin/reset-operational/confirm',
    '/api/workflow/reset',
    '/api/workflow/reset-backend',
    '/api/workflow/delete-absensi',
}


class StaleGenerationError(RuntimeError):
    pass


def _is_reset_path(path: str) -> bool:
    return path in RESET_PATHS or path.startswith('/api/admin/module-reset/')


def _ensure_state_table(conn) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {RUNTIME_TABLE} (
               state_key VARCHAR PRIMARY KEY,
               reset_generation BIGINT NOT NULL,
               reset_status VARCHAR NOT NULL,
               updated_at TIMESTAMP NOT NULL DEFAULT now()
           )"""
    )
    conn.execute(
        f"""INSERT INTO {RUNTIME_TABLE}
               (state_key,reset_generation,reset_status,updated_at)
               VALUES ('GLOBAL',0,'READY',now()) ON CONFLICT DO NOTHING"""
    )


def _read_state(conn) -> tuple[int, str]:
    _ensure_state_table(conn)
    row = conn.execute(
        f"SELECT reset_generation,reset_status FROM {RUNTIME_TABLE} WHERE state_key='GLOBAL'"
    ).fetchone()
    return int(row[0]), str(row[1] or 'READY').upper()


def initialize_runtime_state(path) -> dict:
    """Load persistent state and recover conservatively from an interrupted reset."""
    global _runtime_db_path, _reset_generation, _reset_in_progress, _reset_epoch
    resolved = str(Path(path).resolve())
    with duckdb.connect(resolved) as conn:
        generation, status = _read_state(conn)
        if status != 'READY':
            generation += 1
            conn.execute(
                f"""UPDATE {RUNTIME_TABLE} SET reset_generation=?,reset_status='READY',updated_at=now()
                    WHERE state_key='GLOBAL'""", [generation]
            )
            status = 'READY'
    with _STATE_LOCK:
        _runtime_db_path = resolved
        _reset_generation = generation
        _reset_in_progress = status != 'READY'
        _reset_epoch = generation
    return {'reset_generation': generation, 'reset_status': status}


def current_runtime_state() -> dict:
    with _STATE_LOCK:
        return {
            'reset_generation': int(_reset_generation),
            'reset_status': 'RESETTING' if _reset_in_progress else 'READY',
        }


def current_reset_generation() -> int:
    return int(current_runtime_state()['reset_generation'])


def begin_runtime_reset(path, conn=None) -> int:
    """Persist RESETTING and advance generation before destructive work starts.

    Generation remains advanced even when the reset later rolls back. This is
    conservative: clients overlapping a reset attempt must hydrate again.
    """
    global _reset_generation, _reset_in_progress, _reset_epoch, _runtime_db_path
    own = conn is None
    connection = conn or duckdb.connect(str(Path(path).resolve()))
    try:
        generation, _ = _read_state(connection)
        generation += 1
        connection.execute(
            f"""UPDATE {RUNTIME_TABLE} SET reset_generation=?,reset_status='RESETTING',updated_at=now()
                WHERE state_key='GLOBAL'""", [generation]
        )
    finally:
        if own:
            connection.close()
    with _STATE_LOCK:
        _runtime_db_path = str(Path(path).resolve())
        _reset_generation = generation
        _reset_epoch = generation
        _reset_in_progress = True
    return generation


def finish_runtime_reset(path) -> int:
    """Persist READY and always release the process reset flag."""
    global _reset_in_progress, _reset_generation, _reset_epoch
    generation = current_reset_generation()
    try:
        with duckdb.connect(str(Path(path).resolve())) as conn:
            _ensure_state_table(conn)
            conn.execute(
                f"""UPDATE {RUNTIME_TABLE} SET reset_generation=?,reset_status='READY',updated_at=now()
                    WHERE state_key='GLOBAL'""", [generation]
            )
    finally:
        with _STATE_LOCK:
            _reset_generation = generation
            _reset_epoch = generation
            _reset_in_progress = False
    return generation


def assert_current_generation(generation: int) -> None:
    state = current_runtime_state()
    if state['reset_status'] != 'READY' or int(generation) != state['reset_generation']:
        raise StaleGenerationError(
            f"Job generation {generation} tidak sama dengan generation aktif {state['reset_generation']}"
        )


def claim_database(path):
    key = str(Path(path).resolve()).casefold()
    if key in _owners:
        return
    lock_path = Path(str(Path(path).resolve()) + '.server.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError('Database sudah dimiliki backend lain. Gunakan server EVKIN yang sudah aktif; jangan jalankan server kedua.') from exc
    _owners[key] = handle


@atexit.register
def release_databases():
    for handle in _owners.values():
        handle.close()
    _owners.clear()


def database_job(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        with DATABASE_GATE:
            return function(*args, **kwargs)
    return run


def _error_response(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse
    state = current_runtime_state()
    return JSONResponse({'detail': {
        'code': code,
        'message': message,
        'reset_generation': state['reset_generation'],
    }}, status_code=status_code, headers={GENERATION_HEADER: str(state['reset_generation'])})


def _client_generation(request):
    value = request.headers.get(GENERATION_HEADER)
    if value is None or str(value).strip() == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def serialize_database_requests(request, call_next):
    """Validate generation, then serialize every DuckDB mutation."""
    method = request.method.upper()
    mutating = method not in {'GET', 'HEAD', 'OPTIONS'}
    path = request.url.path
    reset_request = mutating and _is_reset_path(path)
    state = current_runtime_state()

    if mutating and not reset_request:
        if state['reset_status'] != 'READY':
            return _error_response(423, 'RESET_IN_PROGRESS',
                                   'Reset aplikasi sedang berlangsung. Tunggu lalu muat ulang halaman.')
        client_generation = _client_generation(request)
        if client_generation is None:
            return _error_response(409, 'GENERATION_REQUIRED',
                                   'Generation aplikasi wajib dikirim untuk operasi perubahan data.')
        if client_generation != state['reset_generation']:
            return _error_response(409, 'STALE_CLIENT_AFTER_RESET',
                                   'Data operasional telah direset dari sesi lain. Halaman harus dimuat ulang sebelum melanjutkan.')
    else:
        client_generation = None

    if request.scope.get('evkin_database_gate'):
        response = await call_next(request)
        response.headers[GENERATION_HEADER] = str(current_reset_generation())
        return response

    # Job polling and static/readiness endpoints remain responsive. Mutating
    # job dispatch is deliberately not exempt from the write fence.
    exempt = (path in {'/health', '/api/health', '/api/info', '/api/ping', '/', '/index.html'}
              or path.startswith('/assets/')
              or (path.startswith('/api/jobs') and not mutating))
    if exempt:
        response = await call_next(request)
        response.headers[GENERATION_HEADER] = str(current_reset_generation())
        return response

    request_generation = state['reset_generation']
    while not DATABASE_GATE.acquire(blocking=False):
        await asyncio.sleep(.05)
    try:
        current = current_runtime_state()
        if mutating and not reset_request:
            if current['reset_status'] != 'READY':
                return _error_response(423, 'RESET_IN_PROGRESS',
                                       'Reset aplikasi sedang berlangsung. Tunggu lalu muat ulang halaman.')
            if client_generation != current['reset_generation'] or request_generation != current['reset_generation']:
                return _error_response(409, 'STALE_CLIENT_AFTER_RESET',
                                       'Reset terjadi saat permintaan menunggu. Muat ulang halaman sebelum mengubah data.')
        request.scope['evkin_database_gate'] = True
        response = await call_next(request)
        response.headers[GENERATION_HEADER] = str(current_reset_generation())
        return response
    finally:
        DATABASE_GATE.release()

import json
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import duckdb
import pytest

from backend.application_reset import ApplicationResetService, CONFIRMATION
from backend.runtime_guard import (
    DATABASE_GATE,
    GENERATION_HEADER,
    StaleGenerationError,
    begin_runtime_reset,
    current_reset_generation,
    current_runtime_state,
    finish_runtime_reset,
    initialize_runtime_state,
    serialize_database_requests,
)


def service(tmp_path):
    path = tmp_path / 'app.duckdb'
    with duckdb.connect(str(path)) as conn:
        for name in ('sdm_master', 'sdm_bank_history', 'sdm_death_record', 'master_data',
                     'skp_rule_config', 'raw_import', 'skp_assessment_monthly',
                     'tukin_final_monthly', 'pengembalian_monthly_master', 'workflow_config'):
            conn.execute(f'CREATE TABLE {name}(id INTEGER, value VARCHAR)')
            conn.execute(f"INSERT INTO {name} VALUES (1,'original')")
    return ApplicationResetService(path)


def confirmation(svc):
    return {'confirmation': CONFIRMATION, 'actor': 'Petugas', 'reason': 'Uji reset',
            'preview_token': svc.preview()['preview_token']}


def table_fingerprint(conn, table, order_by):
    rows = conn.execute(f'SELECT * FROM {table} ORDER BY {order_by}').fetchall()
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def test_reset_preserves_master_and_verified_backup(tmp_path):
    svc = service(tmp_path)
    result = svc.confirm(confirmation(svc))
    assert result['deleted_rows'] == 5
    with duckdb.connect(str(svc.db_path)) as conn:
        for table in ('sdm_master','sdm_bank_history','sdm_death_record','master_data','skp_rule_config'):
            assert conn.execute(f'SELECT * FROM {table}').fetchall() == [(1, 'original')]
        for table in ('raw_import','skp_assessment_monthly','tukin_final_monthly','pengembalian_monthly_master','workflow_config'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone() == (0,)
        assert conn.execute('SELECT actor,reason FROM application_reset_audit').fetchone() == ('Petugas','Uji reset')
    backup = Path(result['backup_path'])
    with duckdb.connect(str(backup / 'app.duckdb'), read_only=True) as conn:
        assert conn.execute('SELECT * FROM raw_import').fetchall() == [(1,'original')]
    assert json.loads((backup / 'manifest.json').read_text())['state'] == 'BACKUP_VERIFIED'


def test_operational_reset_preserves_rich_master_config_schema_and_index(tmp_path):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute('DROP TABLE sdm_master')
        conn.execute('''CREATE TABLE sdm_master(
            nip VARCHAR PRIMARY KEY, nama VARCHAR NOT NULL, unit_kerja VARCHAR,
            status VARCHAR, nilai_tukin DECIMAL(18,2))''')
        conn.executemany('INSERT INTO sdm_master VALUES (?,?,?,?,?)', [
            ('198001', 'Ani', 'Keuangan', 'AKTIF', 7500000),
            ('198002', 'Budi', 'SDM', 'NONAKTIF', 6250000),
            ('198003', 'Citra', 'Teknis', 'AKTIF', 8100000),
        ])
        conn.execute('CREATE INDEX idx_sdm_master_unit ON sdm_master(unit_kerja)')
        conn.execute('CREATE TABLE skp_indicator_config(id VARCHAR, weight DECIMAL(8,2))')
        conn.execute("INSERT INTO skp_indicator_config VALUES ('KINERJA', 35.50)")
        conn.execute('DROP TABLE sdm_bank_history')
        conn.execute('CREATE TABLE sdm_bank_history(nip VARCHAR, rekening VARCHAR, valid_from DATE)')
        conn.executemany('INSERT INTO sdm_bank_history VALUES (?,?,?)', [
            ('198001', '001-A', '2025-01-01'), ('198002', '002-B', '2025-06-01'),
        ])
        master_rows = conn.execute('SELECT count(*) FROM sdm_master').fetchone()[0]
        key_fingerprint = hashlib.sha256(json.dumps(
            conn.execute('SELECT nip FROM sdm_master ORDER BY nip').fetchall()
        ).encode()).hexdigest()
        material_fingerprint = table_fingerprint(conn, 'sdm_master', 'nip')
        history_fingerprint = table_fingerprint(conn, 'sdm_bank_history', 'nip,valid_from')
        config_before = conn.execute('SELECT * FROM skp_rule_config').fetchall()
        indicator_config_before = conn.execute('SELECT * FROM skp_indicator_config').fetchall()
        schema_before = conn.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_name='sdm_master' ORDER BY ordinal_position"
        ).fetchall()

    generation_before = initialize_runtime_state(svc.db_path)['reset_generation']
    result = svc.confirm(confirmation(svc))

    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM sdm_master').fetchone()[0] == master_rows
        assert hashlib.sha256(json.dumps(
            conn.execute('SELECT nip FROM sdm_master ORDER BY nip').fetchall()
        ).encode()).hexdigest() == key_fingerprint
        assert table_fingerprint(conn, 'sdm_master', 'nip') == material_fingerprint
        assert table_fingerprint(conn, 'sdm_bank_history', 'nip,valid_from') == history_fingerprint
        assert conn.execute('SELECT * FROM skp_rule_config').fetchall() == config_before
        assert conn.execute('SELECT * FROM skp_indicator_config').fetchall() == indicator_config_before
        assert conn.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_name='sdm_master' ORDER BY ordinal_position"
        ).fetchall() == schema_before
        assert conn.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name='idx_sdm_master_unit'"
        ).fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (0,)
        runtime = conn.execute(
            "SELECT reset_generation,reset_status FROM system_runtime_state WHERE state_key='GLOBAL'"
        ).fetchone()
        assert runtime == (generation_before + 1, 'READY')
    assert Path(result['backup_path']).joinpath('app.duckdb').is_file()


def test_reset_generation_is_persistent_and_returned(tmp_path):
    svc = service(tmp_path)
    before = initialize_runtime_state(svc.db_path)['reset_generation']
    result = svc.confirm(confirmation(svc))
    assert result['reset_generation'] == before + 1
    assert current_runtime_state() == {'reset_generation': before + 1, 'reset_status': 'READY'}
    # Simulate a backend restart by loading the value from DuckDB again.
    assert initialize_runtime_state(svc.db_path)['reset_generation'] == before + 1


def test_failed_reset_advances_generation_but_restores_ready(tmp_path, monkeypatch):
    svc = service(tmp_path)
    payload = confirmation(svc)
    before = current_reset_generation()
    monkeypatch.setattr(svc, '_delete_table', lambda *_: (_ for _ in ()).throw(ValueError('forced')))
    with pytest.raises(ValueError, match='forced'):
        svc.confirm(payload)
    assert current_runtime_state() == {'reset_generation': before + 1, 'reset_status': 'READY'}
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute("SELECT reset_status FROM system_runtime_state WHERE state_key='GLOBAL'").fetchone() == ('READY',)


@pytest.mark.parametrize('field,value', [('actor',''), ('reason',''), ('confirmation','RESET'), ('preview_token','stale')])
def test_requires_confirmation(tmp_path, field, value):
    svc = service(tmp_path)
    payload = confirmation(svc)
    payload[field] = value
    with pytest.raises(ValueError):
        svc.confirm(payload)
    assert not (tmp_path / 'reset-backups').exists()


def test_same_count_changed_content_invalidates_preview(tmp_path):
    svc = service(tmp_path)
    payload = confirmation(svc)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute("UPDATE raw_import SET value='changed'")
    with pytest.raises(ValueError, match='Data berubah'):
        svc.confirm(payload)


@pytest.mark.parametrize('status', ['LOCKED','PAID','HISTORICAL_LOCKED','CLOSED','ARCHIVED'])
def test_locked_period_cannot_be_reset(tmp_path, status):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute('CREATE TABLE tukin_period(status VARCHAR)')
        conn.execute('INSERT INTO tukin_period VALUES (?)',[status])
    assert svc.preview()['can_reset'] is False
    with pytest.raises(ValueError, match='terkunci/sudah dibayar'):
        svc.confirm(confirmation(svc))


def test_backup_failure_does_not_delete(tmp_path, monkeypatch):
    svc = service(tmp_path)
    def fail(*args):
        raise OSError('Disk penuh')
    monkeypatch.setattr(svc, '_backup', fail)
    with pytest.raises(OSError):
        svc.confirm(confirmation(svc))
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (1,)


def test_unknown_populated_table_blocks_reset(tmp_path):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute('CREATE TABLE unknown_data AS SELECT 1 id')
    assert svc.preview()['can_reset'] is False


def test_running_jobs_block_reset(tmp_path):
    svc = service(tmp_path)
    svc.jobs_path = tmp_path / 'jobs.db'
    with sqlite3.connect(svc.jobs_path) as conn:
        conn.execute('CREATE TABLE jobs(status TEXT)')
        conn.execute("INSERT INTO jobs VALUES ('running')")
    assert svc.preview()['can_reset'] is False


def test_repeated_reset_keeps_audit(tmp_path):
    svc = service(tmp_path)
    svc.confirm(confirmation(svc))
    svc.confirm(confirmation(svc))
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM application_reset_audit').fetchone() == (2,)


def test_operational_reset_classifies_master_audit_and_working_data(tmp_path):
    svc = service(tmp_path)
    audit_tables = ('skp_assessment_audit', 'skp_indicator_config_audit',
                    'pengembalian_period_audit')
    with duckdb.connect(str(svc.db_path)) as conn:
        for table in audit_tables:
            conn.execute(f'CREATE TABLE {table}(id INTEGER, value VARCHAR)')
            conn.execute(f"INSERT INTO {table} VALUES (1, 'evidence')")
    preview = svc.preview()
    categories = {item['table']: item['category'] for item in preview['tables']}
    assert categories['sdm_master'] == 'PRESERVE_MASTER'
    assert categories['raw_import'] == 'DELETE_OPERATIONAL'
    assert all(categories[table] == 'PRESERVE_AUDIT' for table in audit_tables)
    svc.confirm(confirmation(svc))
    with duckdb.connect(str(svc.db_path)) as conn:
        for table in audit_tables:
            assert conn.execute(f'SELECT value FROM {table}').fetchone() == ('evidence',)
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (0,)


def test_delete_failure_rolls_back_all_tables(tmp_path, monkeypatch):
    svc = service(tmp_path)
    original = svc._delete_table
    def fail_after_delete(conn, item):
        original(conn, item)
        if item['table'] == 'tukin_final_monthly':
            raise ValueError('Simulasi kegagalan setelah DELETE')
    monkeypatch.setattr(svc, '_delete_table', fail_after_delete)
    with pytest.raises(ValueError, match='Simulasi'):
        svc.confirm(confirmation(svc))
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM skp_assessment_monthly').fetchone() == (1,)


def test_operational_protected_corruption_fails_and_rolls_back(tmp_path, monkeypatch):
    svc = service(tmp_path)
    payload = confirmation(svc)
    before_generation = current_reset_generation()
    original = svc._delete_table
    mutated = False

    def corrupt_protected_inside_transaction(conn, item):
        nonlocal mutated
        original(conn, item)
        if not mutated:
            conn.execute("UPDATE sdm_master SET value='corrupted' WHERE id=1")
            mutated = True

    monkeypatch.setattr(svc, '_delete_table', corrupt_protected_inside_transaction)
    with pytest.raises(ValueError, match='pelestarian Master/audit'):
        svc.confirm(payload)

    assert current_runtime_state() == {
        'reset_generation': before_generation + 1, 'reset_status': 'READY'
    }
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT * FROM sdm_master').fetchall() == [(1, 'original')]
        assert conn.execute('SELECT * FROM raw_import').fetchall() == [(1, 'original')]
        assert conn.execute(
            "SELECT reset_generation,reset_status FROM system_runtime_state WHERE state_key='GLOBAL'"
        ).fetchone() == (before_generation + 1, 'READY')
        assert 'application_reset_audit' not in {
            row[0] for row in conn.execute('SHOW TABLES').fetchall()
        }


def test_duplicate_rows_changed_invalidate_preview(tmp_path):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute("INSERT INTO raw_import VALUES (1,'original')")
    payload = confirmation(svc)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute("UPDATE raw_import SET value='changed'")
    with pytest.raises(ValueError, match='Data berubah'):
        svc.confirm(payload)


def test_api_preview_and_confirmation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.application_reset import create_router
    svc = service(tmp_path)
    monkeypatch.setenv('EVKIN_API_KEY', 'reset-test-key')
    headers = {'X-EVKIN-API-Key': 'reset-test-key'}
    app = FastAPI()
    app.include_router(create_router(lambda: str(svc.db_path), lambda: None))
    client = TestClient(app)
    assert client.get('/api/admin/reset-operational/preview', headers=headers).json()['can_reset'] is True
    assert client.post('/api/admin/reset-operational/confirm', json={}, headers=headers).status_code == 400
    assert client.post('/api/admin/reset-operational/confirm', json=confirmation(svc), headers=headers).status_code == 200


def test_reset_gate_rejects_other_writes(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend import runtime_guard
    app = FastAPI()
    app.middleware('http')(runtime_guard.serialize_database_requests)
    @app.post('/api/jobs/export')
    def write():
        return {'unexpected': True}
    monkeypatch.setattr(runtime_guard, '_reset_in_progress', True)
    response = TestClient(app).post('/api/jobs/export')
    assert response.status_code == 423
    assert response.json()['detail']['code'] == 'RESET_IN_PROGRESS'


def test_generation_guard_accepts_current_and_rejects_missing_or_stale(tmp_path):
    path = tmp_path / 'guard.duckdb'
    generation = initialize_runtime_state(path)['reset_generation']
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.middleware('http')(serialize_database_requests)
    @app.post('/write')
    def write():
        return {'ok': True}
    client = TestClient(app)
    assert client.post('/write').json()['detail']['code'] == 'GENERATION_REQUIRED'
    assert client.post('/write', headers={GENERATION_HEADER: str(generation)}).status_code == 200
    begin_runtime_reset(path)
    finish_runtime_reset(path)
    stale = client.post('/write', headers={GENERATION_HEADER: str(generation)})
    assert stale.status_code == 409
    assert stale.json()['detail']['code'] == 'STALE_CLIENT_AFTER_RESET'
    fresh = current_reset_generation()
    assert client.post('/write', headers={GENERATION_HEADER: str(fresh)}).status_code == 200


def test_waiting_stale_mutation_is_rechecked_after_reset(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    path = tmp_path / 'waiting-write.duckdb'
    generation = initialize_runtime_state(path)['reset_generation']
    writes = []
    app = FastAPI()
    app.middleware('http')(serialize_database_requests)

    @app.post('/write')
    def write():
        writes.append('written')
        return {'ok': True}

    DATABASE_GATE.acquire()
    response = {}
    try:
        thread = threading.Thread(target=lambda: response.setdefault(
            'value', TestClient(app).post('/write', headers={GENERATION_HEADER: str(generation)})
        ))
        thread.start()
        time.sleep(0.1)
        begin_runtime_reset(path)
        finish_runtime_reset(path)
    finally:
        DATABASE_GATE.release()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response['value'].status_code == 409
    assert response['value'].json()['detail']['code'] == 'STALE_CLIENT_AFTER_RESET'
    assert writes == []


def test_mutation_with_gate_first_is_removed_by_following_reset(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    svc = service(tmp_path)
    generation = initialize_runtime_state(svc.db_path)['reset_generation']
    entered = threading.Event()
    release_mutation = threading.Event()
    app = FastAPI()
    app.middleware('http')(serialize_database_requests)

    @app.post('/write')
    def write():
        with duckdb.connect(str(svc.db_path)) as conn:
            conn.execute("INSERT INTO raw_import VALUES (2,'overlap')")
        entered.set()
        assert release_mutation.wait(5)
        return {'ok': True}

    mutation_response = {}
    reset_result = {}
    mutation = threading.Thread(target=lambda: mutation_response.setdefault(
        'value', TestClient(app).post('/write', headers={GENERATION_HEADER: str(generation)})
    ))
    mutation.start()
    assert entered.wait(5)

    def reset_after_gate():
        with DATABASE_GATE:
            reset_result['value'] = svc.confirm(confirmation(svc))

    reset = threading.Thread(target=reset_after_gate)
    reset.start()
    time.sleep(0.1)
    assert reset.is_alive(), 'reset must wait until the mutation releases DATABASE_GATE'
    release_mutation.set()
    mutation.join(timeout=5)
    reset.join(timeout=15)
    assert not mutation.is_alive() and not reset.is_alive()
    assert mutation_response['value'].status_code == 200
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (0,)
    assert reset_result['value']['reset_generation'] == generation + 1


def test_restart_recovers_persisted_resetting_and_invalidates_old_generation(tmp_path):
    path = tmp_path / 'restart-recovery.duckdb'
    original = initialize_runtime_state(path)['reset_generation']
    assert begin_runtime_reset(path) == original + 1
    recovered = initialize_runtime_state(path)
    assert recovered == {'reset_generation': original + 2, 'reset_status': 'READY'}
    with pytest.raises(StaleGenerationError):
        from backend.runtime_guard import assert_current_generation
        assert_current_generation(original)


def test_read_waits_for_database_gate_then_recovers_without_deadlock(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    initialize_runtime_state(tmp_path / 'read-gate.duckdb')
    app = FastAPI()
    app.middleware('http')(serialize_database_requests)

    @app.get('/read')
    def read():
        return {'ok': True}

    response = {}
    DATABASE_GATE.acquire()
    try:
        started = time.perf_counter()
        thread = threading.Thread(target=lambda: response.setdefault(
            'value', TestClient(app).get('/read')
        ))
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive(), 'read request should wait while reset/write gate is held'
    finally:
        DATABASE_GATE.release()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response['value'].status_code == 200
    assert response['value'].json() == {'ok': True}
    assert time.perf_counter() - started < 5

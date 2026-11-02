import json
import sqlite3

import duckdb
import pytest
from fastapi import HTTPException
from backend import evkin_export_api as api


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = str(tmp_path / 'ownership.duckdb')
    monkeypatch.setattr(api, 'DUCKDB_PATH', path)
    monkeypatch.setattr(api, 'get_connection', lambda: sqlite3.connect(':memory:'))
    api.ensure_duckdb_master_schema()
    with duckdb.connect(path) as conn:
        conn.execute("CREATE OR REPLACE TABLE master_data AS SELECT 'MASTER' nip, 2000000 tukin")
        conn.execute("CREATE TABLE sdm_master AS SELECT 'MASTER' nip")
        conn.execute("CREATE TABLE tukin_final_monthly AS SELECT 'ARCHIVE' nip")
        conn.execute("CREATE OR REPLACE TABLE raw_import AS SELECT 'PARTICIPANT' nip")
    return path


def test_partial_final_sync_preserves_master_sources_and_archive(isolated):
    api.sync_workflow({'final_absensi': [{'nip': 'PARTICIPANT', 'tukin': 2000000}]})
    with duckdb.connect(isolated) as conn:
        for table, nip in [('master_data', 'MASTER'), ('sdm_master', 'MASTER'),
                           ('raw_import', 'PARTICIPANT'), ('tukin_final_monthly', 'ARCHIVE')]:
            assert conn.execute(f'SELECT nip FROM {table}').fetchall() == [(nip,)]
        assert conn.execute('SELECT nip FROM final_absensi').fetchall() == [('PARTICIPANT',)]


def test_configuration_nested_values_and_workflow_records_roundtrip(isolated):
    record = {'nip':'001234567890123456', 'tanggal':{'monthKey':'2026-08'}, 'deleted':True}
    api.sync_workflow({'config':{'leavePeriods':[{'nip':'001'}], 'jamDasar':{'senKam':{'masuk':'08:00'}},
                                '_workflow':{'month':'2026-08'}},
                       'processed_data':[{'record':json.dumps(record)}],
                       'daily_data':[{'nip':'001','flags':['Tidak lengkap']}]})
    state = api.get_workflow_state()
    result = state['data']
    assert result['config']['jamDasar']['senKam']['masuk']=='08:00'
    assert result['config']['leavePeriods'][0]['nip']=='001'
    assert set(result) == {'config'}
    assert state['meta']['total_row_counts']['daily_data'] == 1
    daily = api.get_workflow_data('daily_data', 1, 100, None, 'default')
    processed = api.get_workflow_data('processed_data', 1, 100, '2026-08', 'default')
    assert daily['rows'][0]['flags']==['Tidak lengkap']
    assert json.loads(processed['rows'][0]['record'])==record


@pytest.mark.parametrize('source', ['master_data', 'sdm_master', 'tukin_final_monthly'])
def test_workflow_rejects_writes_to_other_owners(isolated, source):
    with pytest.raises(HTTPException):
        api.sync_workflow({source: []})
    with pytest.raises(HTTPException):
        api.sync_workflow_table({'source': source, 'rows': [], 'replace': True})


def test_config_is_identical_for_bulk_and_chunked_routes_without_master_cache(isolated):
    config = {'masterPegawai': [{'nip': 'STALE'}], 'minDurasiJam': 7.5}
    api.sync_workflow({'config': json.dumps(config)})
    assert api.get_workflow_state()['data']['config'] == {'minDurasiJam': 7.5}
    api.sync_workflow_table({'source': 'config', 'rows': [json.dumps(config)], 'replace': True})
    state = api.get_workflow_state()['data']
    assert state['config'] == {'minDurasiJam': 7.5}
    assert 'master_data' not in state


def test_state_is_metadata_only_and_small_for_large_workflow(isolated):
    rows = [{'nip': str(index), 'tanggal': '2026-08-01', 'payload': 'x' * 200} for index in range(1200)]
    api.sync_workflow({'raw_import': rows, 'daily_data': rows})
    result = api.get_workflow_state()
    assert set(result['data']) == {'config'}
    assert result['meta']['total_row_counts']['raw_import'] == 1200
    assert result['meta']['total_row_counts']['daily_data'] == 1200
    assert len(json.dumps(result, default=str).encode('utf-8')) < 20_000


def test_workflow_data_paginates_and_filters_period(isolated):
    rows = [
        {'nip': f'A-{index:03d}', 'tanggal': '2026-08-01'} for index in range(135)
    ] + [
        {'nip': f'B-{index:03d}', 'tanggal': '2026-09-01'} for index in range(15)
    ]
    api.sync_workflow({'raw_import': rows})
    first = api.get_workflow_data('raw_import', 1, 100, '2026-08', 'default')
    second = api.get_workflow_data('raw_import', 2, 100, '2026-08', 'default')
    assert first['total'] == 135 and first['total_pages'] == 2 and len(first['rows']) == 100
    assert len(second['rows']) == 35
    assert all(str(row['tanggal']).startswith('2026-08') for row in first['rows'] + second['rows'])


def test_scoped_chunk_replace_preserves_other_workflow(isolated):
    with duckdb.connect(isolated) as conn:
        conn.execute("CREATE OR REPLACE TABLE raw_import(workflow_id VARCHAR,nip VARCHAR,tanggal VARCHAR)")
        conn.execute("INSERT INTO raw_import VALUES ('OTHER','KEEP','2026-08-01')")
    first = api.sync_workflow_table({'source':'raw_import', 'workflow_id':'ACTIVE', 'replace':True,
                                     'rows':[{'workflow_id':'ACTIVE','nip':'FIRST','tanggal':'2026-08-01'}]})
    second = api.sync_workflow_table({'source':'raw_import', 'workflow_id':'ACTIVE', 'replace':False,
                                      'rows':[{'workflow_id':'ACTIVE','nip':'SECOND','tanggal':'2026-08-02'}]})
    with duckdb.connect(isolated) as conn:
        stored = conn.execute('SELECT workflow_id,nip FROM raw_import ORDER BY workflow_id,nip').fetchall()
    assert stored == [('ACTIVE','FIRST'), ('ACTIVE','SECOND'), ('OTHER','KEEP')]
    assert first['total_rows'] == 1
    assert second['total_rows'] == 2


def test_chunked_import_matches_bulk_without_touching_master_domains(isolated):
    rows = [{'nip': f'{index:04d}', 'tanggal': '2026-08-01'} for index in range(205)]
    with duckdb.connect(isolated) as conn:
        conn.execute("CREATE TABLE pengembalian_master_guard AS SELECT 'RETURN' marker")
    api.sync_workflow({'raw_import': rows})
    with duckdb.connect(isolated) as conn:
        bulk = conn.execute('SELECT nip,tanggal FROM raw_import ORDER BY nip').fetchall()
    api.sync_workflow_table({'source':'raw_import', 'rows':rows[:100], 'replace':True})
    api.sync_workflow_table({'source':'raw_import', 'rows':rows[100:200], 'replace':False})
    api.sync_workflow_table({'source':'raw_import', 'rows':rows[200:], 'replace':False})
    with duckdb.connect(isolated) as conn:
        chunked = conn.execute('SELECT nip,tanggal FROM raw_import ORDER BY nip').fetchall()
        assert conn.execute('SELECT nip FROM master_data').fetchall() == [('MASTER',)]
        assert conn.execute('SELECT nip FROM sdm_master').fetchall() == [('MASTER',)]
        assert conn.execute('SELECT marker FROM pengembalian_master_guard').fetchall() == [('RETURN',)]
    assert chunked == bulk
    assert len(chunked) == len(set(chunked)) == len(rows)


def test_bulk_failure_rolls_back_previous_tables(isolated, monkeypatch):
    original = api.prepare_database_frame
    def fail_second(frame):
        if 'fail' in frame.columns:
            raise ValueError('Simulated failure')
        return original(frame)
    monkeypatch.setattr(api, 'prepare_database_frame', fail_second)
    with pytest.raises(ValueError):
        api.sync_workflow({'raw_import': [{'nip': 'NEW'}], 'final_absensi': [{'fail': True}]})
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT nip FROM raw_import').fetchall() == [('PARTICIPANT',)]


def test_chunk_failure_preserves_original_table(isolated, monkeypatch):
    def fail(frame):
        raise ValueError('Simulated failure')
    monkeypatch.setattr(api, 'prepare_database_frame', fail)
    with pytest.raises(ValueError):
        api.sync_workflow_table({'source': 'raw_import', 'rows': [{'nip': 'NEW'}], 'replace': True})
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT nip FROM raw_import').fetchall() == [('PARTICIPANT',)]


def _daily_row_without_excused(nip='001'):
    # This is the 37-column shape produced for ordinary attendance rows.  The
    # conditional ``excused`` field makes leave rows the 38-column variant.
    return {
        '_id': f'row-{nip}', 'nip': nip, 'nama': 'Pegawai', 'jabatan': 'Staf',
        'metode': 'reguler', 'tanggal': '2026-08-03', 'tanggalKey': '2026-08-03',
        'monthKey': '2026-08', 'hari': 'Senin', 'jamMasuk': '07:30',
        'jamPulang': '16:00', 'tanggalMasuk': '2026-08-03',
        'tanggalPulang': '2026-08-03', 'masukAt': '', 'pulangAt': '',
        'jamMasukAsli': '07:30', 'jamPulangAsli': '16:00',
        'jamNormalisasi': 'Original', 'zona': 'WIB', 'durasiMin': 510,
        'durasi': '8h 30m', 'jenis': '', 'statusHariLibur': 'Tidak',
        'statusCuti': '', 'statusDuplikasi': '', 'statusLengkap': 'Lengkap',
        'potKeterlambatan': 0, 'potPulangCepat': 0, 'potDurasi': 0,
        'potHariKurang': 0, 'potTidakLengkap': 0, 'totalHarian': 0,
        'flags': [], 'statusJadwal': 'ADA_JADWAL', 'statusAbsensi': 'NORMAL',
        '_lateMin': 0, '_earlyMin': 0,
    }


def test_hitung_harian_chunk_accepts_conditional_38th_column_by_name(isolated):
    ordinary = _daily_row_without_excused('001')
    assert len(ordinary) == 37
    api.sync_workflow_table({'source': 'daily_data', 'rows': [ordinary], 'replace': True})

    leave = _daily_row_without_excused('002')
    leave['excused'] = True
    # Reverse the source order as well: neither count nor position may control
    # the target mapping.
    leave = dict(reversed(list(leave.items())))
    api.sync_workflow_table({'source': 'daily_data', 'rows': [leave], 'replace': False})

    with duckdb.connect(isolated) as conn:
        stored = conn.execute(
            'SELECT nip,tanggal,excused,_lateMin,_earlyMin FROM hitung_harian ORDER BY nip'
        ).fetchall()
    assert stored == [
        ('001', '2026-08-03', False, 0, 0),
        ('002', '2026-08-03', True, 0, 0),
    ]


def test_chunk_append_maps_reordered_source_columns_by_name(isolated):
    api.sync_workflow_table({
        'source': 'raw_import', 'rows': [{'nip': 'FIRST', 'tanggal': '2026-08-01'}],
        'replace': True,
    })
    api.sync_workflow_table({
        'source': 'raw_import', 'rows': [{'tanggal': '2026-08-02', 'nip': 'SECOND'}],
        'replace': False,
    })
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT nip,tanggal FROM raw_import ORDER BY nip').fetchall() == [
            ('FIRST', '2026-08-01'), ('SECOND', '2026-08-02')
        ]


def test_chunk_append_rejects_missing_required_column_without_partial_insert(isolated):
    api.sync_workflow_table({
        'source': 'raw_import', 'rows': [{'nip': 'FIRST', 'tanggal': '2026-08-01'}],
        'replace': True,
    })
    with pytest.raises(ValueError, match=r'Schema mismatch for raw_import: missing required columns: tanggal'):
        api.sync_workflow_table({
            'source': 'raw_import', 'rows': [{'nip': 'SECOND'}], 'replace': False,
        })
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT nip,tanggal FROM raw_import').fetchall() == [
            ('FIRST', '2026-08-01')
        ]


def test_chunk_append_rejects_unapproved_extra_column_without_partial_insert(isolated):
    api.sync_workflow_table({
        'source': 'raw_import', 'rows': [{'nip': 'FIRST', 'tanggal': '2026-08-01'}],
        'replace': True,
    })
    with pytest.raises(ValueError, match=r'Schema mismatch for raw_import: unexpected source columns: temporary_debug_column'):
        api.sync_workflow_table({
            'source': 'raw_import',
            'rows': [{'nip': 'SECOND', 'tanggal': '2026-08-02', 'temporary_debug_column': 'debug'}],
            'replace': False,
        })
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT nip,tanggal FROM raw_import').fetchall() == [
            ('FIRST', '2026-08-01')
        ]


def test_delete_absensi_requires_confirmation_and_preserves_other_owners(isolated):
    with pytest.raises(HTTPException):
        api.delete_absensi_workflow({})
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT COUNT(*) FROM raw_import').fetchone()[0] == 1
        for table in api.WORKFLOW_WRITABLE.values():
            conn.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT \'WORKFLOW\' nip')
        for table in ['skp_assessments', 'pengembalian_archive']:
            conn.execute(f'CREATE TABLE "{table}" AS SELECT \'PRESERVE\' nip')
    result = api.delete_absensi_workflow({'confirmation': 'HAPUS ABSENSI'})
    assert result['deleted_rows'] == len(set(api.WORKFLOW_WRITABLE.values()))
    with duckdb.connect(isolated) as conn:
        for table in api.WORKFLOW_WRITABLE.values():
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        for table in ['master_data', 'sdm_master', 'tukin_final_monthly', 'skp_assessments', 'pengembalian_archive']:
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 1
    assert api.delete_absensi_workflow({'confirmation': 'HAPUS ABSENSI'})['deleted_rows'] == 0


def test_delete_absensi_rolls_back_all_tables_on_failure(isolated, monkeypatch):
    with duckdb.connect(isolated) as conn:
        conn.execute("CREATE OR REPLACE TABLE hitung_harian AS SELECT 'DAILY' nip")
    original = api.ResetService._delete_table
    def fail_after_delete(service, conn, item):
        original(service, conn, item)
        if item['table'] == 'raw_import':
            raise ValueError('Simulasi kegagalan legacy reset')
    monkeypatch.setattr(api.ResetService, '_delete_table', fail_after_delete)
    with pytest.raises(HTTPException, match='Simulasi'):
        api.delete_absensi_workflow({'confirmation': 'HAPUS ABSENSI'})
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT COUNT(*) FROM raw_import').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM hitung_harian').fetchone()[0] == 1


def test_legacy_backend_reset_uses_workflow_results_profile(isolated):
    with duckdb.connect(isolated) as conn:
        conn.execute("CREATE OR REPLACE TABLE hitung_harian AS SELECT 'DAILY' nip")
    preview = api.preview_legacy_workflow_reset()
    result = api.reset_backend_workflow({
        'preview_token': preview['preview_token'],
        'confirmation': preview['confirmation'],
    })
    assert result['target_tables'] == preview['target_tables']
    with duckdb.connect(isolated) as conn:
        assert conn.execute('SELECT count(*) FROM hitung_harian').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (1,)
        audit = json.loads(conn.execute(
            'SELECT manifest_json FROM application_reset_audit'
        ).fetchone()[0])
        assert audit['source'] == 'LEGACY_WORKFLOW_RESET_API'

import sqlite3
import duckdb
from openpyxl import load_workbook

import pandas as pd
import pytest
from fastapi import HTTPException
from backend import evkin_export_api as api


def setup_export(monkeypatch, tmp_path):
    monkeypatch.setattr(api, 'EXPORT_DIR', str(tmp_path))
    monkeypatch.setattr(api, 'DUCKDB_PATH', str(tmp_path / 'export.duckdb'))
    monkeypatch.setattr(api, 'get_connection', lambda: sqlite3.connect(':memory:'))
    monkeypatch.setattr(api, '_update_job', lambda *args, **kwargs: None)


def test_final_export_preserves_every_cell_beyond_ui_and_batch_limits(monkeypatch, tmp_path):
    setup_export(monkeypatch, tmp_path)
    rows = [{'nip': f'001234567890{i:06d}', 'nama': f'Pegawai {i}',
             'jabatan': 'Pendamping', 'bulan': '2026-09', 'tukin': 2000000 + i,
             'komponen': 600000, 'potPct': 10, 'nominal': 60000,
             'finalDibayar': 1940000 + i} for i in range(2005)]
    frame = pd.DataFrame(rows)
    with duckdb.connect(api.DUCKDB_PATH) as db:
        db.register('source', frame)
        db.execute('CREATE TABLE final_absensi AS SELECT * FROM source')
    monkeypatch.setattr(api, 'EXCEL_SHEET_ROWS', 1001)
    path = api.run_export_job('complete', '2026-09', None, ['FINAL ABSENSI'])
    workbook = load_workbook(path, read_only=True)
    try:
        assert workbook.sheetnames == ['FINAL ABSENSI', 'FINAL ABSENSI (2)', 'FINAL ABSENSI (3)']
        actual = [list(row) for sheet in workbook for row in list(sheet.values)[1:]]
        assert actual == frame.values.tolist()
    finally:
        workbook.close()


def test_all_selected_sheets_match_snapshot_and_period(monkeypatch, tmp_path):
    setup_export(monkeypatch, tmp_path)
    mapping = {'DATA SUMBER':'raw_import', 'HITUNG HARIAN':'hitung_harian',
               'REKAP SDM':'rekap_sdm', 'FINAL ABSENSI':'final_absensi',
               'FINAL + SKP':'final_skp', 'FINAL KOMBINASI':'final_kombinasi',
               'FINAL + SKP KOMBINASI':'final_combined_skp'}
    with duckdb.connect(api.DUCKDB_PATH) as db:
        for name, table in mapping.items():
            month = 'tanggal' if table == 'raw_import' else 'monthKey' if table == 'hitung_harian' else 'bulan'
            value = '2026-09-03' if month == 'tanggal' else '2026-09'
            other = '2026-08-03' if month == 'tanggal' else '2026-08'
            db.execute(f'CREATE TABLE {table} AS SELECT ? AS nip, ? AS "{month}", ? AS jabatan, '
                       '100 AS tukin, 3 AS nominal, 97 AS finalDibayar, 3.0 AS potongan '
                       'UNION ALL SELECT ?, ?, ?, 100, 0, 100, 0.0',
                       ['001234567890123456', value, '=SUM(1,2)', 'OUTSIDE', other, 'Lain'])
    workbook = load_workbook(api.run_export_job('all', '2026-09', None, list(mapping)), read_only=True)
    try:
        assert workbook.sheetnames == list(mapping)
        for sheet in workbook:
            header, row = list(sheet.values)
            assert dict(zip(header, row))['nip'] == '001234567890123456'
            assert row[2:] == ('=SUM(1,2)', 100, 3, 97, 3)
    finally:
        workbook.close()


def test_export_database_error_does_not_create_successful_empty_sheet(monkeypatch, tmp_path):
    setup_export(monkeypatch, tmp_path)
    monkeypatch.setattr(api, 'DUCKDB_PATH', str(tmp_path / 'missing' / 'db.duckdb'))
    with pytest.raises(duckdb.IOException):
        api.run_export_job('failed', '2026-09', None, ['FINAL ABSENSI'])
    assert not (tmp_path / 'failed.xlsx').exists()


def test_export_query_is_explicit_and_rejects_invalid_sources():
    query, params = api.build_export_query(
        'final_absensi', '2026-08', {'nip', 'bulan', 'unit'}, 'SDM', None
    )
    assert query == 'SELECT * FROM "final_absensi" WHERE "bulan" = ? AND "unit" = ?'
    assert params == ['2026-08', 'SDM']
    with pytest.raises(api.ExportValidationError, match='Unsupported export source'):
        api.build_export_query('final_tukin', '2026-08', {'nip'})
    with pytest.raises(api.ExportValidationError, match='Periode export tidak valid'):
        api.build_export_query('final_absensi', '', {'nip'})


def test_export_missing_source_returns_business_error_not_unbound_local(monkeypatch, tmp_path):
    monkeypatch.setattr(api, 'DUCKDB_PATH', str(tmp_path / 'missing.duckdb'))
    with pytest.raises(HTTPException) as error:
        api.read_workflow_table_duckdb('final_absensi', '2026-08')
    assert error.value.status_code == 404
    detail = error.value.detail
    assert isinstance(detail, dict)
    assert detail['code'] == 'EXPORT_SOURCE_NOT_FOUND'
    assert 'UnboundLocalError' not in str(detail)

def test_export_does_not_publish_after_reset(monkeypatch, tmp_path):
    setup_export(monkeypatch, tmp_path)
    with duckdb.connect(api.DUCKDB_PATH) as db:
        db.execute("CREATE TABLE hitung_harian AS SELECT '2026-09' monthKey, '001' nip")
    checks = []
    def check_generation(generation):
        checks.append(generation)
        if len(checks) > 1:
            raise RuntimeError("Generation berubah setelah reset")
    monkeypatch.setattr(api, 'assert_current_generation', check_generation)
    with pytest.raises(RuntimeError, match="Generation berubah"):
        api.run_export_job('after-reset', '2026-09', None, ['HITUNG HARIAN'], job_generation=7)
    assert checks == [7, 7]
    assert not list(tmp_path.glob('*.xlsx'))

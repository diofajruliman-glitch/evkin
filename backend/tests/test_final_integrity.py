import subprocess
import sys
import duckdb
import pandas as pd
import pytest
from backend.final_integrity import final_manifest, assert_manifest, verify_final_xlsx
from backend.runtime_guard import claim_database
from backend.tests.test_final_export_complete import setup_export
from backend import evkin_export_api as api


def test_manifest_preserves_duplicates_and_detects_nip_changes():
    row = dict(nip='001234567890123456', tukin=100, nominal=10, finalDibayar=90)
    original = final_manifest([row, row])
    assert original['row_count'] == 2
    assert original['unique_nip'] == 1
    with pytest.raises(ValueError):
        assert_manifest(final_manifest([row]), original)
    with pytest.raises(ValueError):
        assert_manifest(final_manifest([row, dict(row, nip='001234567890123457')]), original)


def test_mismatch_never_publishes_excel(monkeypatch, tmp_path):
    setup_export(monkeypatch, tmp_path)
    rows = [dict(nip='001234567890123456', tukin=100, nominal=10, finalDibayar=90)]
    with duckdb.connect(api.DUCKDB_PATH) as db:
        db.register('source', pd.DataFrame(rows))
        db.execute('CREATE TABLE final_absensi AS SELECT * FROM source')
    with pytest.raises(ValueError, match='Validasi Final gagal'):
        api.run_export_job('mismatch', '2026-09', None, ['FINAL ABSENSI'], expected_final=final_manifest([]))
    assert not list(tmp_path.glob('*.xlsx'))


def test_excel_changed_amount_rejected(tmp_path):
    rows = [dict(nip='001234567890123456', tukin=100, nominal=10, finalDibayar=90)]
    path = tmp_path / 'changed.xlsx'
    pd.DataFrame([dict(rows[0], finalDibayar=99)]).to_excel(path, sheet_name='FINAL ABSENSI', index=False)
    with pytest.raises(ValueError, match='Validasi Final gagal'):
        verify_final_xlsx(path, final_manifest(rows))


def test_second_process_cannot_claim_database(tmp_path):
    database = str(tmp_path / 'isolated.duckdb')
    claim_database(database)
    code = 'from backend.runtime_guard import claim_database; import sys; claim_database(sys.argv[1])'
    result = subprocess.run([sys.executable, '-c', code, database], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'backend lain' in result.stderr


def test_lock_released_when_owner_exits(tmp_path):
    database = str(tmp_path / 'released.duckdb')
    code = 'from backend.runtime_guard import claim_database; import sys; claim_database(sys.argv[1])'
    for _ in range(2):
        result = subprocess.run([sys.executable, '-c', code, database], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

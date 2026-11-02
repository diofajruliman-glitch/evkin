import duckdb
import pytest

from backend.module_reset import ModuleResetService
from backend.reset_service import RESET_PROFILES, ResetSpec
from backend.runtime_guard import initialize_runtime_state


def service(tmp_path):
    path = tmp_path / 'modules.duckdb'
    with duckdb.connect(str(path)) as conn:
        for table in ('master_sdm', 'pengembalian_period', 'raw_import', 'hitung_harian',
                      'skp_assessment_monthly', 'final_tukin_results', 'tukin_period'):
            conn.execute(f'CREATE TABLE {table}(id INTEGER)')
            conn.execute(f'INSERT INTO {table} VALUES (1)')
    return ModuleResetService(path)


def test_module_reset_preserves_protected_tables(tmp_path):
    svc = service(tmp_path)
    before = initialize_runtime_state(svc.db_path)['reset_generation']
    preview = svc.preview_module('workflow')
    result = svc.reset_module_data('workflow', {
        'preview_token': preview['preview_token'], 'confirmation': preview['confirmation'],
    })
    assert result['deleted_rows'] == 2
    assert result['reset_generation'] == before + 1
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM hitung_harian').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM master_sdm').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM pengembalian_period').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM application_reset_audit').fetchone() == (1,)


def test_protected_table_cannot_be_targeted(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setitem(RESET_PROFILES, 'unsafe', ResetSpec(
        'unsafe', ('master_sdm',), 'RESET UNSAFE', 'none'
    ))
    with pytest.raises(ValueError, match='protected'):
        svc.preview_module('unsafe')


def test_preview_and_execute_use_identical_targets(tmp_path):
    svc = service(tmp_path)
    preview = svc.preview_module('workflow')
    result = svc.reset_module_data('workflow', {
        'preview_token': preview['preview_token'], 'confirmation': preview['confirmation'],
    })
    assert result['target_tables'] == preview['target_tables']
    assert set(result['deleted']) == set(preview['target_tables'])
    assert result['deleted_rows'] == preview['deleted_rows']


def test_invalid_module_and_confirmation_are_rejected(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(ValueError, match='tidak dikenal'):
        svc.preview_module('unknown')
    preview = svc.preview_module('workflow')
    with pytest.raises(ValueError, match='Konfirmasi'):
        svc.reset_module_data('workflow', {
            'preview_token': preview['preview_token'], 'confirmation': 'SALAH',
        })


def test_module_reset_rolls_back_and_does_not_audit_failure(tmp_path, monkeypatch):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute('CREATE INDEX idx_raw_import_value ON raw_import(id)')
    preview = svc.preview_module('workflow')
    original = svc._delete_table
    calls = 0
    def fail_second(conn, item):
        nonlocal calls
        calls += 1
        original(conn, item)
        if calls == 2:
            raise ValueError('Simulasi kegagalan reset')
    monkeypatch.setattr(svc, '_delete_table', fail_second)
    with pytest.raises(ValueError, match='Simulasi'):
        svc.reset_module_data('workflow', {
            'preview_token': preview['preview_token'], 'confirmation': preview['confirmation'],
        })
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (1,)
        assert conn.execute('SELECT count(*) FROM hitung_harian').fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name='idx_raw_import_value'"
        ).fetchone() == (1,)
        assert 'application_reset_audit' not in {
            row[0] for row in conn.execute('SHOW TABLES').fetchall()
        }


def test_module_reset_restores_secondary_indexes_after_clearing_wide_table(tmp_path):
    svc = service(tmp_path)
    with duckdb.connect(str(svc.db_path)) as conn:
        conn.execute('DROP TABLE raw_import')
        columns = ', '.join(
            [f'c{index} VARCHAR' for index in range(1, 3)]
            + ['c3 INTEGER']
            + [f'c{index} VARCHAR' for index in range(4, 15)]
        )
        conn.execute(f'CREATE TABLE raw_import ({columns})')
        values = ['7e772040-6af9-4cf5-a38d-910423e15884', '2026-08', 1] + [None] * 11
        conn.execute(
            f"INSERT INTO raw_import VALUES ({','.join('?' for _ in values)})", values
        )
        conn.execute('CREATE INDEX idx_raw_import_period ON raw_import(c1, c2)')

    preview = svc.preview_module('workflow')
    result = svc.reset_module_data('workflow', {
        'preview_token': preview['preview_token'], 'confirmation': preview['confirmation'],
    })

    assert result['deleted']['raw_import'] == 1
    with duckdb.connect(str(svc.db_path)) as conn:
        assert conn.execute('SELECT count(*) FROM raw_import').fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name='idx_raw_import_period'"
        ).fetchone() == (1,)


@pytest.mark.parametrize('profile', sorted(RESET_PROFILES))
def test_every_module_profile_deletes_only_its_declared_scope(tmp_path, profile):
    path = tmp_path / f'{profile}.duckdb'
    all_targets = sorted({table for spec in RESET_PROFILES.values() for table in spec.tables})
    with duckdb.connect(str(path)) as conn:
        conn.execute('CREATE TABLE sdm_master(nip VARCHAR, unit_kerja VARCHAR, status VARCHAR)')
        conn.execute("INSERT INTO sdm_master VALUES ('198001','Keuangan','AKTIF')")
        conn.execute('CREATE TABLE skp_rule_config(rule_id VARCHAR, value VARCHAR)')
        conn.execute("INSERT INTO skp_rule_config VALUES ('R1','persistent')")
        for table in all_targets:
            conn.execute(f'CREATE TABLE "{table}"(id INTEGER, value VARCHAR)')
            conn.execute(f'INSERT INTO "{table}" VALUES (1,?)', [table])

    svc = ModuleResetService(path)
    preview = svc.preview_module(profile)
    generation_before = initialize_runtime_state(path)['reset_generation']
    result = svc.reset_module_data(profile, {
        'preview_token': preview['preview_token'],
        'confirmation': preview['confirmation'],
    })

    expected_targets = set(RESET_PROFILES[profile].tables)
    assert set(result['target_tables']) == expected_targets
    assert result['reset_generation'] == generation_before + 1
    with duckdb.connect(str(path)) as conn:
        for table in all_targets:
            count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            assert count == (0 if table in expected_targets else 1), table
        assert conn.execute('SELECT * FROM sdm_master').fetchall() == [
            ('198001', 'Keuangan', 'AKTIF')
        ]
        assert conn.execute('SELECT * FROM skp_rule_config').fetchall() == [
            ('R1', 'persistent')
        ]
        assert conn.execute(
            "SELECT reset_generation,reset_status FROM system_runtime_state WHERE state_key='GLOBAL'"
        ).fetchone() == (generation_before + 1, 'READY')

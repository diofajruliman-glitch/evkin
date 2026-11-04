import duckdb
import pytest

from backend.workflow_readiness import (
    record_verification,
    validate_final_readiness,
)


PERIOD = "2026-08"


def setup_db(tmp_path, rekap_rows=None):
    path = str(tmp_path / "workflow.duckdb")
    with duckdb.connect(path) as conn:
        conn.execute("CREATE TABLE hitung_harian (nip VARCHAR, tanggalKey VARCHAR, bulan VARCHAR)")
        conn.execute("CREATE TABLE anomali (nip VARCHAR, tanggalKey VARCHAR, bulan VARCHAR)")
        conn.execute("CREATE TABLE rekap_sdm (nip VARCHAR, bulan VARCHAR, jumlahRecord INTEGER)")
        daily = [("001", "2026-08-01", PERIOD), ("002", "2026-08-01", PERIOD)]
        conn.executemany("INSERT INTO hitung_harian VALUES (?, ?, ?)", daily)
        conn.executemany("INSERT INTO anomali VALUES (?, ?, ?)", daily)
        conn.executemany(
            "INSERT INTO rekap_sdm VALUES (?, ?, ?)",
            rekap_rows or [("001", PERIOD, 1), ("002", PERIOD, 1)],
        )
    return path


def test_verification_persists_and_readiness_reload(tmp_path):
    path = setup_db(tmp_path)
    assert validate_final_readiness(path, PERIOD)["checks"]["semifinal_verified"] is False
    assert record_verification(path, PERIOD)["verified"] is True
    result = validate_final_readiness(path, PERIOD)
    assert result["checks"]["semifinal_verified"] is True
    assert result["ready"] is True


def test_verification_materializes_semifinal_from_canonical_daily_and_is_repeatable(tmp_path):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute("CREATE OR REPLACE TABLE anomali (_empty VARCHAR)")
    first = record_verification(path, PERIOD)
    second = record_verification(path, PERIOD)
    assert first["rows"] == 2
    assert second["verified"] is True
    with duckdb.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM anomali WHERE bulan = ?", [PERIOD]).fetchone()[0] == 2


def test_final_gate_rejects_unverified_and_reports_reason(tmp_path):
    path = setup_db(tmp_path)
    result = validate_final_readiness(path, PERIOD)
    assert result["ready"] is False
    assert "Verifikasi Semi Final belum tercatat di database." in result["blocking_reasons"]


def test_duplicate_daily_key_blocks_readiness(tmp_path):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute("INSERT INTO hitung_harian VALUES ('001', '2026-08-01', ?)", [PERIOD])
    result = validate_final_readiness(path, PERIOD)
    assert result["checks"]["harian_duplicates"] is True


def test_duplicate_rekap_nip_blocks_readiness(tmp_path):
    path = setup_db(tmp_path, [("001", PERIOD, 1), ("001", PERIOD, 1), ("002", PERIOD, 1)])
    result = validate_final_readiness(path, PERIOD)
    assert result["checks"]["rekap_duplicates"] is True
    assert result["checks"]["rekap_consistent"] is False


def test_rekap_mismatch_blocks_readiness(tmp_path):
    path = setup_db(tmp_path, [("001", PERIOD, 2), ("002", PERIOD, 1)])
    result = validate_final_readiness(path, PERIOD)
    assert result["checks"]["rekap_consistent"] is False
    assert any("Rekap SDM tidak konsisten" in reason for reason in result["blocking_reasons"])


def test_upstream_change_invalidates_verification(tmp_path):
    path = setup_db(tmp_path)
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        conn.execute("INSERT INTO hitung_harian VALUES ('003', '2026-08-02', ?)", [PERIOD])
    result = validate_final_readiness(path, PERIOD)
    assert result["checks"]["stale_state"] is True
    assert result["ready"] is False


def test_invalid_period_is_blocking(tmp_path):
    result = validate_final_readiness(setup_db(tmp_path), "2026-13")
    assert result["ready"] is False
    assert result["checks"]["period_valid"] is False


def test_verification_preserves_other_period_snapshot_and_approval(tmp_path):
    path = setup_db(tmp_path)
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        # September has unapproved daily data and an older approved snapshot.
        conn.execute("INSERT INTO hitung_harian VALUES ('003', '2026-09-01', '2026-09')")
        conn.execute("INSERT INTO anomali VALUES ('old', '2026-09-01', '2026-09')")
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        assert conn.execute("SELECT nip FROM anomali WHERE bulan='2026-09'").fetchall() == [('old',)]
        conn.execute("INSERT INTO hitung_harian VALUES ('004', '2026-08-02', ?)", [PERIOD])
    record_verification(path, '2026-09')
    with duckdb.connect(path) as conn:
        assert conn.execute("SELECT nip FROM anomali WHERE bulan='2026-09'").fetchall() == [('003',)]
        assert conn.execute("SELECT count(*) FROM anomali WHERE bulan=?", [PERIOD]).fetchone()[0] == 2
    assert validate_final_readiness(path, PERIOD)['checks']['stale_state'] is True


@pytest.mark.parametrize('date_column', ['tanggalKey', 'tanggal', 'tgl_finger'])
def test_snapshot_isolation_with_date_only_schema(tmp_path, date_column):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute('ALTER TABLE hitung_harian DROP COLUMN bulan')
        conn.execute('ALTER TABLE anomali DROP COLUMN bulan')
        if date_column != 'tanggalKey':
            conn.execute(f'ALTER TABLE hitung_harian RENAME tanggalKey TO {date_column}')
            conn.execute(f'ALTER TABLE anomali RENAME tanggalKey TO {date_column}')
        conn.execute("INSERT INTO anomali VALUES ('old', '2026-09-01')")
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM anomali WHERE nip='old'").fetchone()[0] == 1
    assert validate_final_readiness(path, PERIOD)['ready'] is True


@pytest.mark.parametrize('table', ['raw_import', 'workflow_config'])
def test_import_and_config_changes_invalidate_approval(tmp_path, table):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute('CREATE TABLE raw_import (nip VARCHAR, tanggal VARCHAR, jam_masuk VARCHAR)')
        conn.execute("INSERT INTO raw_import VALUES ('001', '2026-08-01', '08:00')")
        conn.execute('CREATE TABLE workflow_config (minDurasiJam DOUBLE)')
        conn.execute('INSERT INTO workflow_config VALUES (7.5)')
    record_verification(path, PERIOD)
    assert validate_final_readiness(path, PERIOD)['ready'] is True
    with duckdb.connect(path) as conn:
        if table == 'raw_import':
            conn.execute("UPDATE raw_import SET jam_masuk='10:00'")
        else:
            conn.execute('UPDATE workflow_config SET minDurasiJam=8')
    result = validate_final_readiness(path, PERIOD)
    assert result['checks']['stale_state'] is True
    assert result['ready'] is False


def test_other_period_import_does_not_invalidate_approval(tmp_path):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute('CREATE TABLE raw_import (nip VARCHAR, tanggal VARCHAR)')
        conn.execute("INSERT INTO raw_import VALUES ('001', '2026-08-01')")
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        conn.execute("INSERT INTO raw_import VALUES ('002', '2026-09-01')")
    assert validate_final_readiness(path, PERIOD)['ready'] is True


def test_verification_rolls_back_snapshot_if_metadata_write_fails(tmp_path):
    path = setup_db(tmp_path)
    with duckdb.connect(path) as conn:
        conn.execute("DELETE FROM anomali WHERE nip='002'")
        conn.execute('CREATE TABLE workflow_readiness_meta (incompatible INTEGER)')
    with pytest.raises(duckdb.Error):
        record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        assert conn.execute('SELECT nip FROM anomali').fetchall() == [('001',)]
        assert not conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='workflow_flags'").fetchone()[0]


def test_approval_without_signature_requires_reverification(tmp_path):
    path = setup_db(tmp_path)
    record_verification(path, PERIOD)
    with duckdb.connect(path) as conn:
        conn.execute('DROP TABLE workflow_readiness_meta')
    assert validate_final_readiness(path, PERIOD)['ready'] is False


def test_large_readiness_uses_database_aggregates(tmp_path, monkeypatch):
    from backend import workflow_readiness

    path = str(tmp_path / 'large.duckdb')
    with duckdb.connect(path) as conn:
        conn.execute("""
            CREATE TABLE hitung_harian AS
            SELECT printf('%018d', i // 20) nip,
                   CAST(DATE '2026-08-01' + CAST(i % 20 AS INTEGER) AS VARCHAR) tanggalKey,
                   '2026-08' bulan, repeat('detail ', 100) flags
            FROM range(100000) t(i)
        """)
        conn.execute('CREATE TABLE anomali AS SELECT * FROM hitung_harian WHERE false')
        conn.execute('''CREATE TABLE rekap_sdm AS
                        SELECT nip, bulan, count(*) jumlahRecord
                        FROM hitung_harian GROUP BY nip, bulan''')

    def forbid_full_materialization(*args, **kwargs):
        raise AssertionError('Readiness must not load full workflow tables into Python')

    monkeypatch.setattr(workflow_readiness, '_rows', forbid_full_materialization)
    assert record_verification(path, PERIOD)['rows'] == 100000
    assert validate_final_readiness(path, PERIOD)['ready'] is True
    with duckdb.connect(path) as conn:
        conn.execute("UPDATE rekap_sdm SET jumlahRecord=19 WHERE nip='000000000000000123'")
    result = validate_final_readiness(path, PERIOD)
    assert result['checks']['rekap_consistent'] is False

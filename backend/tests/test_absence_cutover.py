import json
import threading
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.absence_cutover as cutover
from backend.absence_cutover import (
    AbsenceCalculationError,
    AbsenceCalculationService,
    AuthorityConfig,
    CalculationAuthority,
    assert_canonical_absence_ready,
    current_authority_config,
    ensure_absence_calculation_schema,
    readiness_gate,
    recover_interrupted_batches,
    compare_shadow_reference,
    create_router,
)


ROOT = Path(__file__).resolve().parents[2]
GOLDEN_CASES = ROOT / "backend/tests/fixtures/absence_js_golden_cases_2026_09.json"


def fixture_database(tmp_path: Path) -> Path:
    path = tmp_path / "absence-cutover.duckdb"
    config = json.loads(GOLDEN_CASES.read_text(encoding="utf-8"))["config"]
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            '''CREATE TABLE raw_import (
                "Tanggal" VARCHAR,"NIP" VARCHAR,"Nama" VARCHAR,"Jabatan" VARCHAR,
                "Provinsi" VARCHAR,"Jam Datang" VARCHAR,"Jam Pulang" VARCHAR,"Jenis" VARCHAR
            )'''
        )
        conn.executemany(
            'INSERT INTO raw_import VALUES (?,?,?,?,?,?,?,?)',
            [
                ("2026-08-03", "A-1", "Anon A", "Staf", "DKI Jakarta", "07:30", "16:00", ""),
                ("2026-08-04", "A-1", "Anon A", "Staf", "DKI Jakarta", "07:31", "15:29", ""),
                ("2026-09-01", "B-1", "Anon B", "Staf", "Bali", "08:00", "16:30", ""),
            ],
        )
        conn.execute(
            '''CREATE TABLE workflow_config (
                saved BOOLEAN,effectiveDaysMode VARCHAR,effectiveDaysManual BIGINT,
                holidays VARCHAR,jabatanMaster VARCHAR,jamDasar VARCHAR,flexi VARCHAR,
                keterlambatanTiers VARCHAR,pulangCepatTiers VARCHAR,minDurasiJam DOUBLE,
                potonganTidakLengkap BIGINT,potonganHariKurangPerHari DOUBLE,
                jenisNoPotongan VARCHAR,leavePeriods VARCHAR,_workflow VARCHAR
            )'''
        )
        conn.execute(
            "INSERT INTO workflow_config VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                True, config["effectiveDaysMode"], config["effectiveDaysManual"],
                json.dumps(config["holidays"]), json.dumps(config["jabatanMaster"]),
                json.dumps(config["jamDasar"]), json.dumps(config["flexi"]),
                json.dumps(config["keterlambatanTiers"]), json.dumps(config["pulangCepatTiers"]),
                config["minDurasiJam"], config["potonganTidakLengkap"],
                config["potonganHariKurangPerHari"], json.dumps(config["jenisNoPotongan"]),
                json.dumps(config["leavePeriods"]), json.dumps({"timeMode": "original"}),
            ],
        )
        conn.execute(
            '''CREATE TABLE master_data (
                nip VARCHAR,nama VARCHAR,jabatan VARCHAR,tukin_gross BIGINT,tukin BIGINT
            )'''
        )
        conn.executemany(
            "INSERT INTO master_data VALUES (?,?,?,?,?)",
            [("A-1", "Anon A", "Staf", 5_000_001, 5_000_001),
             ("B-1", "Anon B", "Staf", 4_000_001, 4_000_001)],
        )
        ensure_absence_calculation_schema(conn)
    return path


def generation_controls(value=7):
    state = {"value": value}

    def reader():
        return state["value"]

    def validator(expected):
        if int(expected) != state["value"]:
            raise RuntimeError("stale generation")

    return state, reader, validator


def service(path, authority=CalculationAuthority.PYTHON_CANONICAL, *, before_commit=None):
    state, reader, validator = generation_controls()
    instance = AbsenceCalculationService(
        path,
        authority_config=AuthorityConfig(authority, authority == CalculationAuthority.PYTHON_CANONICAL),
        generation_reader=reader,
        generation_validator=validator,
        write_gate=threading.Lock(),
        before_commit=before_commit,
    )
    return instance, state


def rows(conn, table, period):
    columns = {row[0].lower(): row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()}
    period_column = columns.get("bulan") or columns.get("monthkey")
    return conn.execute(
        f'SELECT * FROM "{table}" WHERE CAST("{period_column}" AS VARCHAR)=? ORDER BY nip',
        [period],
    ).fetchall()


def test_feature_flag_default_remains_js_legacy(monkeypatch):
    monkeypatch.delenv("EVKIN_ABSENCE_AUTHORITY", raising=False)
    monkeypatch.delenv("EVKIN_ABSENCE_CANONICAL_ENABLED", raising=False)
    assert current_authority_config() == AuthorityConfig(CalculationAuthority.JS_LEGACY, False)
    gate = readiness_gate()
    assert gate["ready"] is True
    assert {"WITA", "WIT", "WALI", "CROSS_DAY"} <= set(gate["required_golden_coverage"])


def test_js_legacy_mode_does_not_start_python_or_fallback(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path, CalculationAuthority.JS_LEGACY)
    with pytest.raises(AbsenceCalculationError, match="JS_LEGACY") as error:
        svc.calculate_sync("2026-08", "tester")
    assert error.value.code == "JS_LEGACY_AUTHORITY"
    with duckdb.connect(str(path)) as conn:
        assert conn.execute("SELECT count(*) FROM absence_calculation_batch").fetchone()[0] == 0


def test_python_canonical_batch_persists_all_stages_atomically(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    batch = svc.calculate_sync("2026-08", "tester")
    assert batch["status"] == "COMPLETED"
    assert batch["canonical"] is True
    assert batch["raw_count"] == batch["daily_count"] == 2
    assert batch["rekap_count"] == batch["final_count"] == 1
    with duckdb.connect(str(path)) as conn:
        assert len(rows(conn, "hitung_harian", "2026-08")) == 2
        assert len(rows(conn, "rekap_sdm", "2026-08")) == 1
        assert len(rows(conn, "final_absensi", "2026-08")) == 1
        pointer = conn.execute(
            "SELECT calculation_id FROM absence_calculation_canonical WHERE period='2026-08'"
        ).fetchone()
        assert pointer == (batch["calculation_id"],)
        assert_canonical_absence_ready(
            conn, "2026-08", AuthorityConfig(CalculationAuthority.PYTHON_CANONICAL, True)
        )


def test_mid_persistence_failure_rolls_back_all_three_stages(tmp_path, monkeypatch):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    original = cutover._replace_period

    def fail_on_rekap(conn, table, result_rows, period, period_column, calculation_id):
        if table == "rekap_sdm":
            raise RuntimeError("forced persistence failure")
        return original(conn, table, result_rows, period, period_column, calculation_id)

    monkeypatch.setattr(cutover, "_replace_period", fail_on_rekap)
    batch = svc.calculate_sync("2026-08", "tester")
    assert batch["status"] == "FAILED"
    assert batch["error_code"] == "CALCULATION_FAILED"
    with duckdb.connect(str(path)) as conn:
        for table in ("hitung_harian", "rekap_sdm", "final_absensi"):
            assert conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]
            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM absence_calculation_canonical").fetchone()[0] == 0


def test_source_change_before_commit_is_stale_and_does_not_publish(tmp_path):
    path = fixture_database(tmp_path)

    def mutate_source(_calculation_id, _source):
        with duckdb.connect(str(path)) as conn:
            conn.execute("UPDATE raw_import SET \"Jam Pulang\"='15:00' WHERE \"NIP\"='A-1'")

    svc, _ = service(path, before_commit=mutate_source)
    batch = svc.calculate_sync("2026-08", "tester")
    assert batch["status"] == "STALE_SOURCE"
    assert batch["error_code"] == "SOURCE_CHANGED_DURING_CALCULATION"
    with duckdb.connect(str(path)) as conn:
        assert conn.execute("SELECT count(*) FROM absence_calculation_canonical").fetchone()[0] == 0


def test_reset_generation_change_prevents_commit(tmp_path):
    path = fixture_database(tmp_path)
    holder = {}

    def change_generation(_calculation_id, _source):
        holder["state"]["value"] += 1

    svc, state = service(path, before_commit=change_generation)
    holder["state"] = state
    batch = svc.calculate_sync("2026-08", "tester")
    assert batch["status"] == "STALE_GENERATION"
    with duckdb.connect(str(path)) as conn:
        assert conn.execute("SELECT count(*) FROM absence_calculation_canonical").fetchone()[0] == 0


def test_same_period_second_active_batch_is_rejected(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    first = svc.submit("2026-08", "operator-a")
    assert first["status"] == "PENDING"
    with pytest.raises(AbsenceCalculationError) as error:
        svc.submit("2026-08", "operator-b")
    assert error.value.code == "CALCULATION_ALREADY_ACTIVE"


def test_different_periods_are_isolated_and_rerun_replaces_only_its_period(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    august = svc.calculate_sync("2026-08", "operator", "initial August")
    september = svc.calculate_sync("2026-09", "operator", "initial September")
    rerun = svc.calculate_sync("2026-08", "operator", "verified rerun")
    assert {august["status"], september["status"], rerun["status"]} == {"COMPLETED"}
    assert rerun["previous_calculation_id"] == august["calculation_id"]
    with duckdb.connect(str(path)) as conn:
        assert len(rows(conn, "final_absensi", "2026-08")) == 1
        assert len(rows(conn, "final_absensi", "2026-09")) == 1
        pointers = dict(conn.execute(
            "SELECT period,calculation_id FROM absence_calculation_canonical"
        ).fetchall())
        assert pointers == {
            "2026-08": rerun["calculation_id"],
            "2026-09": september["calculation_id"],
        }


def test_shadow_batch_never_replaces_canonical_results(tmp_path):
    path = fixture_database(tmp_path)
    canonical, _ = service(path)
    first = canonical.calculate_sync("2026-08", "operator")
    shadow, _ = service(path, CalculationAuthority.PYTHON_SHADOW)
    batch = shadow.calculate_sync("2026-08", "canary", "shadow comparison")
    assert batch["status"] == "SHADOW_ONLY"
    assert batch["canonical"] is False
    assert batch["mismatch_count"] == 0
    assert batch["fields_compared"] > 0
    with duckdb.connect(str(path)) as conn:
        pointer = conn.execute(
            "SELECT calculation_id FROM absence_calculation_canonical WHERE period='2026-08'"
        ).fetchone()[0]
        assert pointer == first["calculation_id"]


def test_failed_or_shadow_batch_is_not_accepted_downstream(tmp_path):
    path = fixture_database(tmp_path)
    with duckdb.connect(str(path)) as conn:
        ensure_absence_calculation_schema(conn)
        conn.execute(
            """INSERT INTO absence_calculation_batch
               (calculation_id,period,authority,engine,calculation_version,reset_generation,
                source_formula_fingerprint,status,canonical,requested_by,final_count)
               VALUES ('bad','2026-08','PYTHON_CANONICAL','PYTHON','v',1,'x','FAILED',false,'x',1)"""
        )
        conn.execute(
            "INSERT INTO absence_calculation_canonical VALUES ('2026-08','bad','v','PYTHON','x',1,now())"
        )
        with pytest.raises(AbsenceCalculationError) as error:
            assert_canonical_absence_ready(
                conn, "2026-08", AuthorityConfig(CalculationAuthority.PYTHON_CANONICAL, True)
            )
        assert error.value.code == "CANONICAL_ABSENCE_NOT_READY"


def test_rerun_is_business_deterministic_with_new_audit_identity(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    first = svc.calculate_sync("2026-08", "operator", "first")
    with duckdb.connect(str(path)) as conn:
        before = conn.execute(
            "SELECT nip,bulan,potPct,tukin,komponen,nominal,finalDibayar FROM final_absensi"
        ).fetchall()
    second = svc.calculate_sync("2026-08", "operator", "rerun")
    with duckdb.connect(str(path)) as conn:
        after = conn.execute(
            "SELECT nip,bulan,potPct,tukin,komponen,nominal,finalDibayar FROM final_absensi"
        ).fetchall()
    assert before == after
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["calculation_id"] != second["calculation_id"]


def test_failure_never_silently_falls_back_to_js(tmp_path, monkeypatch):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    monkeypatch.setattr(cutover, "calculate_source", lambda _source: (_ for _ in ()).throw(RuntimeError("boom")))
    batch = svc.calculate_sync("2026-08", "operator")
    assert batch["status"] == "FAILED"
    assert batch["engine"] == "PYTHON"
    assert batch["canonical"] is False
    with duckdb.connect(str(path)) as conn:
        assert conn.execute("SELECT count(*) FROM absence_calculation_canonical").fetchone()[0] == 0


def test_batch_status_survives_frontend_reload_and_new_service_instance(tmp_path):
    path = fixture_database(tmp_path)
    first_service, _ = service(path)
    pending = first_service.submit("2026-08", "operator")
    second_service, _ = service(path)
    assert second_service.get(pending["calculation_id"])["status"] == "PENDING"
    second_service.run_batch(pending["calculation_id"])
    assert first_service.get(pending["calculation_id"])["status"] == "COMPLETED"


def test_restart_recovery_terminalizes_pending_and_running_batches(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    pending = svc.submit("2026-08", "operator")
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            "UPDATE absence_calculation_batch SET status='RUNNING' WHERE calculation_id=?",
            [pending["calculation_id"]],
        )
    assert recover_interrupted_batches(path) == 1
    recovered = svc.get(pending["calculation_id"])
    assert recovered["status"] == "FAILED"
    assert recovered["error_code"] == "PROCESS_RESTARTED"


def test_python_canonical_requires_separate_activation_gate(tmp_path):
    path = fixture_database(tmp_path)
    state, reader, validator = generation_controls()
    svc = AbsenceCalculationService(
        path,
        authority_config=AuthorityConfig(CalculationAuthority.PYTHON_CANONICAL, False),
        generation_reader=reader,
        generation_validator=validator,
        write_gate=threading.Lock(),
    )
    with pytest.raises(AbsenceCalculationError) as error:
        svc.submit("2026-08", "operator")
    assert error.value.code == "PYTHON_CANONICAL_NOT_ENABLED"


def test_shadow_comparator_treats_persisted_null_as_sparse_missing_property():
    daily = {field: 0 for field in cutover.COMPARISON_FIELDS["daily"]}
    daily.update({"nip": "A-1", "tanggalKey": "2026-08-03", "monthKey": "2026-08"})
    persisted = dict(daily, _lateMin=None, _earlyMin=None)
    sparse = {key: value for key, value in daily.items() if key not in {"_lateMin", "_earlyMin"}}
    comparison = compare_shadow_reference(
        {"daily": [persisted], "rekap": [], "final_absensi": []},
        {"daily": [sparse], "rekap": [], "final_absensi": []},
    )
    assert comparison["mismatches"] == 0


def test_latest_static_route_is_not_captured_as_calculation_id(tmp_path):
    path = fixture_database(tmp_path)
    svc, _ = service(path)
    batch = svc.calculate_sync("2026-08", "tester")
    app = FastAPI()
    app.include_router(create_router(lambda: str(path)))
    with TestClient(app) as client:
        response = client.get("/api/absence/calculation/latest?period=2026-08")
    assert response.status_code == 200
    assert response.json()["calculation_id"] == batch["calculation_id"]

def test_canonical_export_matches_application_pages_and_rejects_superseded_batch(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from backend import evkin_export_api as api
    from backend.tests.test_final_export_complete import setup_export

    setup_export(monkeypatch, tmp_path)
    path = fixture_database(tmp_path)
    monkeypatch.setattr(api, "DUCKDB_PATH", str(path))
    with duckdb.connect(str(path)) as conn:
        conn.execute("""UPDATE raw_import SET "Jam Pulang" = '' WHERE "Tanggal" = '2026-08-03'""")
    svc, _ = service(path)
    period = "2026-08"
    batch = svc.calculate_sync(period, "tester")
    assert batch["status"] == "COMPLETED", batch.get("error_message")
    reused = svc.calculate_sync(period, "tester", reuse=True)
    assert reused["status"] == "COMPLETED"
    assert reused["calculation_id"] != batch["calculation_id"]
    assert reused["published_calculation_id"] == batch["calculation_id"]
    assert reused["counts"] == batch["counts"]
    sources = {"HITUNG HARIAN":"daily_data", "REKAP SDM":"rekap_sdm", "FINAL ABSENSI":"final_absensi",
               "FINAL + SKP":"final_skp", "FINAL KOMBINASI":"final_kombinasi",
               "FINAL + SKP KOMBINASI":"final_combined_skp"}
    with duckdb.connect(str(path)) as conn:
        for table in ("final_skp", "final_kombinasi", "final_combined_skp"):
            conn.execute(f"CREATE TABLE {table} AS SELECT *, 87.5 AS variantRate FROM final_absensi")
    workbook = load_workbook(api.run_export_job("canonical", period, None,
        ["DATA SUMBER", *sources, "SEMI FINAL", "ANOMALI"], calculation_id=batch["calculation_id"]), read_only=True)
    try:
        assert len(list(workbook["DATA SUMBER"].values)) == 3
        for name in ["SEMI FINAL", "ANOMALI"]:
            header, row = list(workbook[name].values)
            report = dict(zip(header, row))
            assert report["statusLengkap"] == "Tidak Lengkap"
            assert report["totalHarian"] == 3
        for name, source in sources.items():
            header, *excel_rows = list(workbook[name].values)
            actual = [dict(zip(header, row)) for row in excel_rows]
            expected = []
            for page in range(1, len(actual) + 1):
                result = api.get_workflow_data(source, page=page, per_page=1, periode=period,
                                               workflow_id="default", include_detail=True)
                expected.extend(result["rows"])
            assert len(actual) == len(expected)
            fields = {"daily_data": ("nip", "jabatan", "metode", "totalHarian"),
                      "rekap_sdm": ("nip", "jabatan", "total"),
                      "final_absensi": ("nip", "tukin", "nominal", "finalDibayar")}.get(source, ("nip", "variantRate"))
            assert [tuple(row[key] for key in fields) for row in actual] == [
                tuple(row[key] for key in fields) for row in expected]
    finally:
        workbook.close()
    with duckdb.connect(str(path)) as conn:
        conn.execute("""UPDATE raw_import SET "Jam Datang" = '07:39' WHERE "Tanggal" = '2026-08-04'""")
    updated = svc.calculate_sync(period, "tester", reuse=True)
    assert updated["status"] == "COMPLETED"
    assert updated["published_calculation_id"] == updated["calculation_id"]
    with pytest.raises(ValueError, match="Hasil periode sudah berubah"):
        api.run_export_job("superseded", period, None, ["HITUNG HARIAN"],
                           calculation_id=batch["calculation_id"])
    assert not (tmp_path / "superseded.xlsx").exists()

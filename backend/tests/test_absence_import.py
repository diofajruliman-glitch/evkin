from __future__ import annotations

import csv
import io
import json
import subprocess
from pathlib import Path

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from starlette.datastructures import UploadFile

import backend.absence_import as absence_import
from backend.absence_import import (
    AbsenceImportService,
    CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN,
    CSV_EABSENSI_SOURCE_FORMAT,
    ImportAuthority,
    ImportAuthorityConfig,
    ImportEngineError,
    create_router,
    ensure_absence_import_schema,
    recover_interrupted_imports,
)
from backend.absence_cutover import load_canonical_source
from backend.absence_shadow_engine import parse_date
from backend.runtime_guard import current_reset_generation, initialize_runtime_state


HEADERS = ["NIP", "Nama", "Jabatan", "Tanggal", "Jam Masuk", "Jam Pulang", "Zona", "Provinsi", "Jenis"]
CONFIG = {
    "holidays": [], "jabatanMaster": [], "jamDasar": {}, "flexi": {},
    "keterlambatanTiers": [], "pulangCepatTiers": [], "jenisNoPotongan": [],
    "leavePeriods": [], "_workflow": {"timeMode": "original"},
}
ROOT = Path(__file__).resolve().parents[2]
WALI_FIXTURE = ROOT / "backend/tests/fixtures/absence_wali_golden_cases_2026_09.json"
WALI_GOLDEN = ROOT / "backend/tests/fixtures/absence_wali_golden_output_2026_09.json"
WALI_LEGACY_RUNNER = ROOT / "backend/tests/absence_wali_legacy_runner.cjs"
EABS_LEGACY_PARITY_RUNNER = ROOT / "backend/tests/csv_eabsensi_legacy_parity_runner.cjs"
EABS_HEADERS = [
    "tgl_finger", "NM_PEG", "nip", "jnshadir", "NM_UNIT_ES2", "NM_KANTOR",
    "absen_pagi", "absen_sore", "latitude", "longitude", "latitude_sore", "longitude_sore",
]


def csv_bytes(rows, headers=HEADERS):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def wali_csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode()


def eabs_csv_bytes(rows, headers=EABS_HEADERS):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def eabs_row(nip, day, masuk="", pulang="", jenis="Bekerja di Kantor", **extra):
    result = {
        "tgl_finger": day, "NM_PEG": f"Pegawai {nip}", "nip": nip, "jnshadir": jenis,
        "NM_UNIT_ES2": "U", "NM_KANTOR": "K", "absen_pagi": masuk, "absen_sore": pulang,
        "latitude": "", "longitude": "", "latitude_sore": "", "longitude_sore": "",
    }
    result.update(extra)
    return result


def row(nip="1001", date="2026-08-01", masuk="07:30", pulang="16:00", zone="WIB", **extra):
    result = {"NIP": nip, "Nama": f"Pegawai {nip}", "Jabatan": "Guru", "Tanggal": date,
              "Jam Masuk": masuk, "Jam Pulang": pulang, "Zona": zone, "Provinsi": "", "Jenis": ""}
    result.update(extra)
    return result


@pytest.fixture
def service(tmp_path):
    db = tmp_path / "uat.duckdb"
    return AbsenceImportService(
        db, tmp_path / "uploads",
        authority_config=ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1),
        generation_reader=lambda: 7,
        generation_validator=lambda generation: None if generation == 7 else (_ for _ in ()).throw(RuntimeError("stale")),
    )


def upload(service, content, filename="absence.csv", source_format="standard", period=None):
    item = service.save_upload(
        UploadFile(io.BytesIO(content), filename=filename), operator="uat", source_format=source_format,
        period=period, workflow_config_json=json.dumps(CONFIG),
    )
    return service.stage(item["import_id"])


def test_default_authority_remains_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv("EVKIN_IMPORT_AUTHORITY", raising=False)
    monkeypatch.delenv("EVKIN_CSV_EABSENSI_SPECIAL_ENABLED", raising=False)
    instance = AbsenceImportService(tmp_path / "db.duckdb", tmp_path / "upload")
    assert instance.authority_info()["authority"] == "IMPORT_JS_LEGACY"
    assert instance.authority_info()["routes"]["csv_eabsensi"] == "DISABLED"
    assert instance.authority_info()["csv_eabsensi_enabled"] is False


def test_source_format_router_is_explicit_and_wali_always_legacy():
    backend = ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1)
    canary = ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1, csv_eabsensi_enabled=True)
    legacy = ImportAuthorityConfig(ImportAuthority.IMPORT_JS_LEGACY)
    assert backend.route("standard") == ImportAuthority.IMPORT_BACKEND_V1
    assert backend.route("wali") == ImportAuthority.IMPORT_JS_LEGACY
    assert legacy.route("standard") == ImportAuthority.IMPORT_JS_LEGACY
    assert legacy.route("wali") == ImportAuthority.IMPORT_JS_LEGACY
    assert canary.route(CSV_EABSENSI_SOURCE_FORMAT) == ImportAuthority.CSV_EABSENSI_SPECIAL
    with pytest.raises(ImportEngineError) as disabled:
        backend.route(CSV_EABSENSI_SOURCE_FORMAT)
    assert disabled.value.code == "CSV_EABSENSI_SPECIAL_NOT_ENABLED"
    with pytest.raises(ImportEngineError) as error:
        backend.route("unknown")
    assert error.value.code == "INVALID_SOURCE_FORMAT"


@pytest.fixture
def eabs_service(tmp_path):
    return AbsenceImportService(
        tmp_path / "eabs.duckdb", tmp_path / "eabs-uploads",
        authority_config=ImportAuthorityConfig(
            ImportAuthority.IMPORT_BACKEND_V1, csv_eabsensi_enabled=True,
        ),
        generation_reader=lambda: 7,
        generation_validator=lambda generation: None if generation == 7 else (_ for _ in ()).throw(RuntimeError("stale")),
    )


def upload_eabs(service, rows, headers=EABS_HEADERS, filename="eabsensi.csv", period=None,
                 pair_cross_day=True):
    item = service.save_upload(
        UploadFile(io.BytesIO(eabs_csv_bytes(rows, headers)), filename=filename),
        operator="uat", source_format=CSV_EABSENSI_SOURCE_FORMAT, period=period,
        workflow_config_json=json.dumps(CONFIG), pair_cross_day=pair_cross_day,
    )
    return service.stage(item["import_id"])


def test_csv_eabsensi_normal_mode_is_order_independent_and_daily(eabs_service):
    rows = [
        eabs_row("NORMAL", "2026-08-08", "", "2026-08-08 17:00", jenis="Upacara"),
        eabs_row("NORMAL", "2026-08-08", "2026-08-08 08:00", ""),
        eabs_row("NORMAL", "2026-08-08", "2026-08-08 07:00", ""),
        eabs_row("NORMAL", "2026-08-08", "", "2026-08-08 16:00"),
        eabs_row("INONLY", "2026-08-08", "2026-08-08 07:30", ""),
        eabs_row("OUTONLY", "2026-08-08", "", "2026-08-08 16:30"),
    ]
    batch = upload_eabs(eabs_service, rows, pair_cross_day=False)
    assert batch["status"] == "READY"
    preview = eabs_service.preview(batch["import_id"], page_size=20)["rows"]
    by_nip = {row["nip"]: row for row in preview}
    assert (by_nip["NORMAL"]["jam_masuk"], by_nip["NORMAL"]["jam_pulang"]) == ("07:00:00", "17:00:00")
    with duckdb.connect(eabs_service.db_path) as conn:
        assert conn.execute(
            "SELECT jenis FROM absence_import_staging WHERE import_id=? AND nip='NORMAL'",
            [batch["import_id"]],
        ).fetchone() == ("Upacara",)
    assert by_nip["INONLY"]["jam_pulang"] == ""
    assert by_nip["OUTONLY"]["jam_masuk"] == ""
    assert batch["timings"]["pair_cross_day"] is False
    assert batch["timings"]["complete_pairs"] == 1
    assert batch["timings"]["check_in_only"] == 1
    assert batch["timings"]["check_out_only"] == 1


def test_csv_eabsensi_cross_day_is_explicit_opt_in(eabs_service):
    rows = [
        eabs_row("NIGHT-OFF", "2026-08-10", "2026-08-10 21:30", ""),
        eabs_row("NIGHT-OFF", "2026-08-11", "", "2026-08-11 06:45"),
    ]
    off = upload_eabs(eabs_service, rows, filename="night-off.csv", pair_cross_day=False)
    off_rows = eabs_service.preview(off["import_id"], page_size=20)["rows"]
    assert [(row["tanggal"], row["cross_day"]) for row in off_rows] == [
        ("2026-08-10", False), ("2026-08-11", False),
    ]
    on_rows = [dict(row, nip="NIGHT-ON", NM_PEG="Pegawai NIGHT-ON") for row in rows]
    on = upload_eabs(eabs_service, on_rows, filename="night-on.csv", pair_cross_day=True)
    on_preview = eabs_service.preview(on["import_id"], page_size=20)["rows"]
    assert len(on_preview) == 1
    assert (on_preview[0]["tanggal"], on_preview[0]["tanggal_pulang"], on_preview[0]["cross_day"]) == (
        "2026-08-10", "2026-08-11", True,
    )


def test_csv_eabsensi_ui_fixture_normal_and_cross_day_output(eabs_service):
    headers = [
        "tgl_finger", "NM_PEG", "nip", "jnshadir", "NM_UNIT_ES2", "NM_KANTOR",
        "NM_JABATAN", "absen_pagi", "absen_sore", "latitude", "longitude",
        "latitude_sore", "longitude_sore",
    ]
    rows = [
        eabs_row("UI-TEST", "2026-08-09", "2026-08-09 07:00", "", NM_JABATAN="Pegawai"),
        eabs_row("UI-TEST", "2026-08-09", "", "2026-08-09 16:00", NM_JABATAN="Pegawai"),
        eabs_row("UI-TEST", "2026-08-10", "2026-08-10 21:30", "", NM_JABATAN="Pegawai"),
        eabs_row("UI-TEST", "2026-08-11", "", "2026-08-11 06:45", NM_JABATAN="Pegawai"),
    ]
    batch = upload_eabs(
        eabs_service, rows, headers=headers, filename="ui-routing-eabsensi.csv",
        pair_cross_day=True,
    )
    assert batch["status"] == "READY"
    assert batch["source_format"] == CSV_EABSENSI_SOURCE_FORMAT
    assert batch["timings"]["source_rows"] == 4
    assert batch["timings"]["unique_nip"] == 1
    assert batch["timings"]["complete_pairs"] == 2
    assert batch["timings"]["pair_cross_day"] is True
    preview = eabs_service.preview(batch["import_id"], page_size=20)["rows"]
    same_day = next(item for item in preview if item["tanggal"] == "2026-08-09")
    cross_day = next(item for item in preview if item["tanggal"] == "2026-08-10")
    assert (same_day["jam_masuk"], same_day["jam_pulang"]) == ("07:00:00", "16:00:00")
    assert (cross_day["tanggal"], cross_day["tanggal_pulang"],
            cross_day["jam_masuk"], cross_day["jam_pulang"]) == (
        "2026-08-10", "2026-08-11", "21:30:00", "06:45:00",
    )


def test_import_connections_do_not_reset_temp_directory_after_staging(eabs_service):
    rows = [
        eabs_row("CONNECT-1", "2026-08-09", "2026-08-09 07:00", "2026-08-09 16:00"),
        eabs_row("CONNECT-2", "2026-08-09", "2026-08-09 07:30", "2026-08-09 16:30"),
    ]
    batch = upload_eabs(
        eabs_service, rows, filename="repeated-connection.csv", pair_cross_day=False,
    )
    assert batch["status"] == "READY"
    assert eabs_service.get(batch["import_id"])["status"] == "READY"
    assert eabs_service.preview(batch["import_id"])["total"] == 2


def test_each_import_connection_uses_configured_temp_directory(service, monkeypatch):
    real_connect = duckdb.connect
    temp_directory_sets = []

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args, **kwargs):
            if str(query).strip().lower().startswith("set temp_directory"):
                temp_directory_sets.append(str(query))
            return self.connection.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.connection.__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(
        absence_import.duckdb, "connect",
        lambda *args, **kwargs: TrackedConnection(real_connect(*args, **kwargs)),
    )
    for _ in range(2):
        connection = service._connect()
        active_spill = Path(connection.execute(
            "SELECT current_setting('temp_directory')"
        ).fetchone()[0])
        assert active_spill.resolve() == (
            Path(absence_import.tempfile.gettempdir()) / "evkin-import-duckdb-spill"
        ).resolve()
        connection.execute("SELECT 1").fetchall()
        connection.close()
    assert len(temp_directory_sets) == 2


def test_upload_survives_temp_directory_already_used_by_database(service, monkeypatch):
    real_connect = duckdb.connect
    temp_directory_sets = []
    existing_spill = None

    class ExistingTempDirectoryConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args, **kwargs):
            nonlocal existing_spill
            normalized = str(query).strip().lower()
            if normalized.startswith("select current_setting('temp_directory')"):
                result = self.connection.execute(query, *args, **kwargs)
                existing_spill = Path(result.fetchone()[0])
                existing_spill.mkdir(parents=True, exist_ok=True)
                return _SingleRowResult(str(existing_spill))
            if normalized.startswith("set temp_directory"):
                temp_directory_sets.append(str(query))
                raise duckdb.NotImplementedException(
                    "Not implemented Error: Cannot switch temporary directory after the current one has been used"
                )
            return self.connection.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.connection.__exit__(exc_type, exc_value, traceback)

    class _SingleRowResult:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    monkeypatch.setattr(
        absence_import.duckdb, "connect",
        lambda *args, **kwargs: ExistingTempDirectoryConnection(real_connect(*args, **kwargs)),
    )

    uploaded = service.save_upload(
        UploadFile(io.BytesIO(csv_bytes([row("TEMP-DIR")])), filename="temp-dir.csv"),
        operator="uat", source_format="standard", workflow_config_json=json.dumps(CONFIG),
    )

    assert uploaded["status"] == "STAGED"
    assert existing_spill is not None and existing_spill.is_dir()
    assert len(temp_directory_sets) == 2


def test_eabsensi_signature_is_rejected_from_standard_backend(eabs_service):
    headers = EABS_HEADERS + ["NM_JABATAN"]
    rows = [eabs_row("ROUTE", "2026-08-09", "2026-08-09 07:00", "2026-08-09 16:00")]
    batch = upload(
        eabs_service, eabs_csv_bytes(rows, headers), filename="eabsensi-standard.csv",
        source_format="standard",
    )
    assert batch["status"] == "FAILED"
    assert batch["error_code"] == "CSV_EABSENSI_SPECIAL_REQUIRED"


def test_csv_eabsensi_matches_legacy_canonical_contract_for_equivalent_input(eabs_service, tmp_path):
    legacy_fixture = {
        "headers": ["NIP", "Nama", "Jabatan", "Tanggal", "Jam Masuk", "Jam Pulang", "Zona", "Provinsi", "Kabupaten", "Jenis"],
        "rows": [
            ["P1", "Pegawai P1", "Guru", "2026-08-03", "06:46:26", "17:41:22", "WIB", "", "", "Bekerja di Kantor"],
            ["P2", "Pegawai P2", "", "2026-08-04", "07:30:00", "", "WIB", "", "", "Upacara"],
            ["P3", "Pegawai P3", "", "2026-08-05", "", "16:15:00", "WIB", "", "", "Bekerja di Kantor"],
        ],
    }
    fixture_path = tmp_path / "legacy-parity.json"
    fixture_path.write_text(json.dumps(legacy_fixture), encoding="utf-8")
    legacy = json.loads(subprocess.run(
        ["node", str(EABS_LEGACY_PARITY_RUNNER), str(fixture_path)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout)
    csv_rows = [
        eabs_row("P1", "2026-08-03", "2026-08-03 06:46:26", "", NM_JABATAN="Guru"),
        eabs_row("P1", "2026-08-03", "", "2026-08-03 17:41:22", NM_JABATAN="Guru"),
        eabs_row("P2", "2026-08-04", "2026-08-04 07:30:00", "", jenis="Upacara"),
        eabs_row("P3", "2026-08-05", "", "2026-08-05 16:15:00"),
    ]
    headers = EABS_HEADERS + ["NM_JABATAN"]
    batch = upload_eabs(eabs_service, csv_rows, headers=headers, pair_cross_day=False)
    eabs_service.commit(batch["import_id"], period="2026-08")
    with duckdb.connect(eabs_service.db_path) as conn:
        cursor = conn.execute("""
            SELECT nip,nama,jabatan,tanggal,tanggal_pulang,jam_masuk,jam_pulang,
                   zona,provinsi,kabupaten,jenis
            FROM raw_import WHERE import_id=? ORDER BY nip,tanggal
        """, [batch["import_id"]])
        names = [item[0] for item in cursor.description]
        backend_rows = [dict(zip(names, values)) for values in cursor.fetchall()]
    for row in backend_rows:
        row["cross_day"] = bool(row["tanggal_pulang"] and row["tanggal_pulang"] != row["tanggal"])
    assert backend_rows == legacy


def test_csv_eabsensi_invalid_timestamp_is_visible_and_never_silent(eabs_service):
    batch = upload_eabs(eabs_service, [
        eabs_row("BAD-TIME", "2026-08-03", "not-a-time", "2026-08-03 17:00:00"),
    ], pair_cross_day=False)
    assert batch["status"] == "READY"
    assert batch["invalid_rows"] == 1
    error = eabs_service.preview(batch["import_id"], errors_only=True)["rows"][0]
    assert error["severity"] == "BLOCKER"
    assert "INVALID_CHECK_IN" in error["error_codes"]
    with pytest.raises(ImportEngineError):
        eabs_service.commit(batch["import_id"], period="2026-08")


def test_csv_eabsensi_same_file_can_use_a_different_explicit_pairing_mode(eabs_service):
    content = eabs_csv_bytes([
        eabs_row("MODE", "2026-08-10", "2026-08-10 21:30", ""),
        eabs_row("MODE", "2026-08-11", "", "2026-08-11 06:45"),
    ])
    off_item = eabs_service.save_upload(
        UploadFile(io.BytesIO(content), filename="mode.csv"), operator="uat",
        source_format=CSV_EABSENSI_SOURCE_FORMAT, workflow_config_json=json.dumps(CONFIG),
        pair_cross_day=False,
    )
    off = eabs_service.stage(off_item["import_id"])
    eabs_service.commit(off["import_id"], period="2026-08")
    on_item = eabs_service.save_upload(
        UploadFile(io.BytesIO(content), filename="mode.csv"), operator="uat",
        source_format=CSV_EABSENSI_SOURCE_FORMAT, workflow_config_json=json.dumps(CONFIG),
        pair_cross_day=True,
    )
    assert on_item["duplicate_of_import_id"] is None
    assert eabs_service.stage(on_item["import_id"])["status"] == "READY"


def test_csv_eabsensi_is_fail_closed_and_job_title_is_never_required(eabs_service):
    no_job_headers = [header for header in EABS_HEADERS if header.lower() not in {"nm_jabatan", "jabatan"}]
    batch = upload_eabs(eabs_service, [eabs_row("NOJOB", "2026-08-08", "07:30", "16:00")], no_job_headers)
    assert batch["status"] == "READY", batch
    assert eabs_service.preview(batch["import_id"])["rows"][0]["jabatan"] == ""
    job_headers = EABS_HEADERS + ["NM_JABATAN"]
    null_job = eabs_row("NULLJOB", "2026-08-09", "07:30", "16:00")
    null_job["NM_JABATAN"] = "NULL"
    null_batch = upload_eabs(eabs_service, [null_job], job_headers, filename="null-job.csv")
    assert null_batch["status"] == "READY"
    assert eabs_service.preview(null_batch["import_id"])["rows"][0]["jabatan"] == ""
    with pytest.raises(ImportEngineError) as wrong_file:
        eabs_service.save_upload(
            UploadFile(io.BytesIO(b"not xlsx"), filename="eabsensi.xlsx"), operator="uat",
            source_format=CSV_EABSENSI_SOURCE_FORMAT, workflow_config_json=json.dumps(CONFIG),
        )
    assert wrong_file.value.code == "CSV_EABSENSI_REQUIRES_CSV"


def test_csv_eabsensi_golden_sessionization_20_cases(eabs_service):
    rows = [
        # 1 normal same day; 2 split rows.
        eabs_row("C01", "2026-08-08", "2026-08-08 07:30", "2026-08-08 16:00"),
        eabs_row("C02", "2026-08-08", "2026-08-08 07:31", ""),
        eabs_row("C02", "2026-08-08", "", "2026-08-08 16:01"),
        # 3/4 night boundaries and 5 next-day checkout.
        eabs_row("C03", "2026-08-08", "2026-08-08 20:00", ""),
        eabs_row("C03", "2026-08-09", "", "2026-08-09 06:00"),
        eabs_row("C04", "2026-08-08", "2026-08-08 23:59:59", ""),
        eabs_row("C04", "2026-08-09", "", "2026-08-09 06:15"),
        # 6 orphan morning checkout.
        eabs_row("C06", "2026-08-09", "NULL", "2026-08-09 06:00"),
        # 7 consecutive nights; 8 no phantom next-day record.
        eabs_row("C07", "2026-08-08", "2026-08-08 23:00", ""),
        eabs_row("C07", "2026-08-09", "", "2026-08-09 06:00"),
        eabs_row("C07", "2026-08-09", "2026-08-09 21:00", ""),
        eabs_row("C07", "2026-08-10", "", "2026-08-10 06:00"),
        eabs_row("C08", "2026-08-08", "2026-08-08 23:00", ""),
        eabs_row("C08", "2026-08-09", "", "2026-08-09 06:00"),
        eabs_row("C08", "2026-08-09", "null", ""),
        # 9 reversed source order.
        eabs_row("C09", "2026-08-09", "", "2026-08-09 06:00"),
        eabs_row("C09", "2026-08-08", "2026-08-08 22:00", ""),
        # 10 exact duplicate event/source row.
        eabs_row("C10", "2026-08-08", "2026-08-08 07:30", "2026-08-08 16:00"),
        eabs_row("C10", "2026-08-08", "2026-08-08 07:30", "2026-08-08 16:00"),
        # 11 missing IN; 12 missing OUT.
        eabs_row("C11", "2026-08-08", "", "2026-08-08 16:00"),
        eabs_row("C12", "2026-08-08", "2026-08-08 07:30", ""),
        # 13 cross month; 14 cross year.
        eabs_row("C13", "2026-08-31", "2026-08-31 23:00", ""),
        eabs_row("C13", "2026-09-01", "", "2026-09-01 06:00"),
        eabs_row("C14", "2026-12-31", "2026-12-31 23:00", ""),
        eabs_row("C14", "2027-01-01", "", "2027-01-01 06:00"),
        # 15 jnshadir preserved; 16 no job title is the entire fixture; 17 metadata job blank/null irrelevant.
        eabs_row("C15", "2026-08-08", "07:30", "16:00", jenis="Upacara"),
        eabs_row("C17", "2026-08-08", "07:30", "16:00"),
        # 18 interleaved employees.
        eabs_row("C18A", "2026-08-08", "07:00", ""),
        eabs_row("C18B", "2026-08-08", "07:10", ""),
        eabs_row("C18A", "2026-08-08", "", "16:00"),
        eabs_row("C18B", "2026-08-08", "", "16:10"),
        # 19 same timestamp tie; 20 one row containing both punches.
        eabs_row("C19", "2026-08-08", "08:00", "08:00"),
        eabs_row("C20", "2026-08-08", "09:00", "17:00"),
    ]
    # Explicitly reverse all rows: actual datetime, not CSV order, controls pairing.
    batch = upload_eabs(eabs_service, list(reversed(rows)))
    assert batch["status"] == "READY", batch
    assert batch["duplicate_rows"] >= 1
    preview = eabs_service.preview(batch["import_id"], 1, 100)["rows"]
    grouped = {}
    for item in preview:
        grouped.setdefault(item["nip"], []).append(item)
    assert (grouped["C01"][0]["tanggal"], grouped["C01"][0]["jam_masuk"], grouped["C01"][0]["jam_pulang"]) == ("2026-08-08", "07:30:00", "16:00:00")
    assert (grouped["C02"][0]["tanggal"], grouped["C02"][0]["jam_masuk"], grouped["C02"][0]["jam_pulang"]) == ("2026-08-08", "07:31:00", "16:01:00")
    assert grouped["C03"][0]["tanggal"] == "2026-08-08" and grouped["C03"][0]["cross_day"] is True
    assert grouped["C04"][0]["jam_masuk"] == "23:59:59" and grouped["C04"][0]["cross_day"] is True
    assert grouped["C06"][0]["tanggal"] == "2026-08-08" and grouped["C06"][0]["jam_masuk"] == ""
    assert [(row["tanggal"], row["jam_masuk"], row["jam_pulang"]) for row in grouped["C07"]] == [
        ("2026-08-08", "23:00:00", "06:00:00"), ("2026-08-09", "21:00:00", "06:00:00")
    ]
    assert len(grouped["C08"]) == 1 and grouped["C08"][0]["tanggal"] == "2026-08-08"
    assert grouped["C09"][0]["tanggal"] == "2026-08-08" and grouped["C09"][0]["jam_pulang"] == "06:00:00"
    assert len(grouped["C10"]) == 1
    assert grouped["C11"][0]["jam_masuk"] == "" and grouped["C11"][0]["jam_pulang"] == "16:00:00"
    assert grouped["C12"][0]["jam_pulang"] == ""
    assert grouped["C13"][0]["tanggal"] == "2026-08-31" and grouped["C13"][0]["cross_day"] is True
    assert grouped["C14"][0]["tanggal"] == "2026-12-31" and grouped["C14"][0]["cross_day"] is True
    assert [(item["jam_masuk"], item["jam_pulang"]) for item in grouped["C18A"]] == [("07:00:00", "16:00:00")]
    assert [(item["jam_masuk"], item["jam_pulang"]) for item in grouped["C18B"]] == [("07:10:00", "16:10:00")]
    assert len(grouped["C19"]) == 1 and (grouped["C19"][0]["jam_masuk"], grouped["C19"][0]["jam_pulang"]) == ("08:00:00", "08:00:00")
    assert len(grouped["C20"]) == 1 and (grouped["C20"][0]["jam_masuk"], grouped["C20"][0]["jam_pulang"]) == ("09:00:00", "17:00:00")
    with duckdb.connect(eabs_service.db_path) as conn:
        assert conn.execute(
            "SELECT jenis FROM absence_import_staging WHERE import_id=? AND nip='C15'", [batch["import_id"]]
        ).fetchone() == ("Upacara",)
        assert conn.execute(
            "SELECT count(*) FROM absence_import_staging WHERE import_id=? AND jabatan<>''", [batch["import_id"]]
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT normalized_json FROM absence_import_staging WHERE import_id=? AND nip='C13'", [batch["import_id"]]
        ).fetchone()[0].find('2026-09-01 06:00:00') >= 0
        assert conn.execute(
            "SELECT normalized_json FROM absence_import_staging WHERE import_id=? AND nip='C04'", [batch["import_id"]]
        ).fetchone()[0].find('2026-08-08 23:59:59') >= 0
    assert CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN == 14 * 60


def test_csv_eabsensi_canonical_handoff_keeps_work_date_and_cross_period_checkout(eabs_service):
    batch = upload_eabs(eabs_service, [
        eabs_row("AUG31", "2026-08-31", "2026-08-31 23:00", ""),
        eabs_row("AUG31", "2026-09-01", "", "2026-09-01 06:00"),
    ])
    assert batch["status"] == "READY"
    assert batch["periods"] == ["2026-08"]
    committed = eabs_service.commit(batch["import_id"], period="2026-08")
    assert committed["status"] == "COMMITTED"
    with duckdb.connect(eabs_service.db_path) as conn:
        conn.execute("CREATE TABLE master_data(nip VARCHAR,nama VARCHAR,jabatan VARCHAR,tukin DOUBLE)")
        conn.execute("INSERT INTO master_data VALUES ('AUG31','Pegawai AUG31','',1000)")
        canonical_row = conn.execute("""
            SELECT tanggal,tanggal_pulang,jam_masuk,jam_pulang,source_format
            FROM raw_import WHERE nip='AUG31'
        """).fetchone()
        source = load_canonical_source(conn, "2026-08")
    assert canonical_row == (
        "2026-08-31", "2026-09-01", "23:00:00", "06:00:00", CSV_EABSENSI_SOURCE_FORMAT,
    )
    record = source["records"][0]
    assert record["tanggal"]["key"] == "2026-08-31"
    assert record["masukRaw"] == "23:00:00" and record["pulangRaw"] == "06:00:00"
    assert record["crossDay"] is True


@pytest.mark.parametrize(("authority", "source_format", "code"), [
    ("IMPORT_BACKEND_V1", "wali", "WALI_LEGACY_REQUIRED"),
    ("IMPORT_JS_LEGACY", "standard", "STANDARD_BACKEND_NOT_ENABLED"),
])
def test_upload_api_rejects_wrong_engine_without_silent_fallback(
    tmp_path, monkeypatch, authority, source_format, code,
):
    monkeypatch.setenv("EVKIN_IMPORT_AUTHORITY", authority)
    app = FastAPI()
    db = tmp_path / f"{source_format}.duckdb"
    app.include_router(create_router(lambda: str(db)))
    response = TestClient(app).post(
        "/api/absence-import/upload",
        data={"operator": "uat", "source_format": source_format},
        files={"file": ("sample.csv", csv_bytes([row("1")]), "text/csv")},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert not db.exists(), "Rejected routing must not create or mutate the database"


def test_migration_051_is_additive_idempotent_and_preserves_existing_data(tmp_path):
    db = tmp_path / "migration.duckdb"
    with duckdb.connect(db) as conn:
        conn.execute("CREATE TABLE protected_master(nip VARCHAR PRIMARY KEY, value INTEGER)")
        conn.execute("INSERT INTO protected_master VALUES ('A', 7)")
        before = conn.execute("SELECT * FROM protected_master").fetchall()
        ensure_absence_import_schema(conn)
        ensure_absence_import_schema(conn)
        after = conn.execute("SELECT * FROM protected_master").fetchall()
        tables = {item[0] for item in conn.execute("SHOW TABLES").fetchall()}
    assert before == after == [("A", 7)]
    assert {"absence_import_batch", "absence_import_staging"} <= tables


def test_csv_mapping_normalization_timezone_cross_day_and_preview(service):
    batch = upload(service, csv_bytes([
        row("1", masuk="0.3125", pulang="16:00", zone="WIB"),
        row("2", date="2026-08-02", masuk="23:00:31", pulang="01:00", zone="WITA"),
        row("3", date="2026-08-03", masuk="07:30", pulang="16:00", zone="WIT"),
    ]))
    assert batch["status"] == "READY"
    assert (batch["wib_rows"], batch["wita_rows"], batch["wit_rows"]) == (1, 1, 1)
    assert batch["cross_day_rows"] == 1
    preview = service.preview(batch["import_id"], page=1, page_size=2)
    assert preview["total"] == 3 and len(preview["rows"]) == 2
    assert preview["rows"][0]["jam_masuk"] == "07:30:00"


def test_alias_columns_and_excel_serial_date(service):
    headers = ["Nomor Pegawai", "Nama Pegawai", "Date", "Check In", "Check Out"]
    batch = upload(service, csv_bytes([{
        "Nomor Pegawai": "ABC-1", "Nama Pegawai": "A", "Date": "46235", "Check In": "0.5", "Check Out": "0.75",
    }], headers))
    assert batch["status"] == "READY"
    preview = service.preview(batch["import_id"])["rows"][0]
    assert preview["nip"] == "ABC-1" and preview["jam_masuk"] == "12:00:00"


def test_invalid_rows_are_visible_and_commit_requires_confirmation(service):
    batch = upload(service, csv_bytes([
        row("", date="bad", masuk="xx", pulang=""), row("2", date="2026-08-01"),
    ]))
    assert batch["invalid_rows"] == 1
    errors = service.preview(batch["import_id"], errors_only=True)
    assert errors["total"] == 1
    with pytest.raises(ImportEngineError, match="konfirmasi"):
        service.commit(batch["import_id"])
    committed = service.commit(batch["import_id"], accept_invalid=True)
    assert committed["status"] == "COMMITTED" and committed["valid_rows"] == 1


def test_malformed_time_blocks_but_legacy_missing_punch_remains_warning(service):
    batch = upload(service, csv_bytes([
        row("BAD", masuk="not-a-time", pulang="16:00"),
        row("MISSING", masuk="", pulang=""),
    ]))
    assert batch["invalid_rows"] == 1
    errors = service.preview(batch["import_id"], errors_only=True)
    by_nip = {item["nip"]: item for item in errors["rows"]}
    assert by_nip["BAD"]["severity"] == "BLOCKER"
    assert "INVALID_CHECK_IN" in by_nip["BAD"]["error_codes"]
    assert by_nip["MISSING"]["severity"] == "WARNING"
    assert "MISSING_CHECK_IN" in by_nip["MISSING"]["error_codes"]


def test_missing_checkout_alone_does_not_require_confirmation(eabs_service):
    headers = EABS_HEADERS + ["NM_JABATAN"]
    batch = upload_eabs(eabs_service, [
        eabs_row("SHIFT01", "2026-08-03", "2026-08-03 20:00", "", NM_JABATAN="Wali Asuh"),
    ], headers=headers)
    assert batch["invalid_rows"] == 0
    warning, = eabs_service.preview(batch["import_id"], errors_only=True)["rows"]
    assert warning["severity"] == "WARNING"
    assert warning["error_codes"] == ["MISSING_CHECK_OUT"]
    assert eabs_service.commit(batch["import_id"])["status"] == "COMMITTED"


def test_duplicate_key_is_flagged_not_silently_appended(service):
    batch = upload(service, csv_bytes([row("1"), row("1", masuk="08:00")]))
    assert batch["duplicate_rows"] == 2
    assert all(item["duplicate_row"] for item in service.preview(batch["import_id"])["rows"])


def test_multi_period_requires_explicit_selection(service):
    batch = upload(service, csv_bytes([row("1", "2026-08-01"), row("2", "2026-09-01")]))
    assert batch["period"] is None
    assert batch["error_code"] == "MULTI_PERIOD_REQUIRES_SELECTION"
    with pytest.raises(ImportEngineError, match="Pilih satu periode"):
        service.commit(batch["import_id"])
    committed = service.commit(batch["import_id"], period="2026-08")
    assert committed["period"] == "2026-08"


def test_backend_commit_replaces_stale_legacy_import_stats(eabs_service):
    legacy_config = {
        **CONFIG,
        "saved": True,
        "_workflow": {
            "workflow_id": "legacy-workflow",
            "month": "2026-07",
            "importMode": "standard",
            "importStatus": "COMPLETE",
            "importStats": {
                "totalRaw": 99,
                "totalRecord": 98,
                "rejectedRows": 1,
                "valid": 98,
                "bermasalah": 1,
                "months": ["2026-07"],
            },
        },
    }
    rows = [
        eabs_row("CSV-1", "2026-08-01", "2026-08-01 07:30", "2026-08-01 16:00"),
        eabs_row("CSV-2", "2026-08-02", "2026-08-02 07:30", "2026-08-02 16:00"),
    ]
    item = eabs_service.save_upload(
        UploadFile(io.BytesIO(eabs_csv_bytes(rows)), filename="eabsensi.csv"),
        operator="uat", source_format=CSV_EABSENSI_SOURCE_FORMAT,
        workflow_config_json=json.dumps(legacy_config), pair_cross_day=False,
    )
    batch = eabs_service.stage(item["import_id"])
    committed = eabs_service.commit(batch["import_id"], period="2026-08")

    with duckdb.connect(eabs_service.db_path) as conn:
        workflow = json.loads(conn.execute("SELECT _workflow FROM workflow_config").fetchone()[0])
        canonical_count = conn.execute(
            "SELECT count(*) FROM raw_import WHERE workflow_id=?",
            [f"backend-import-{batch['import_id']}"],
        ).fetchone()[0]

    assert committed["status"] == "COMMITTED"
    assert canonical_count == 2
    assert workflow["workflow_id"] == f"backend-import-{batch['import_id']}"
    assert workflow["month"] == "2026-08"
    assert workflow["importMode"] == CSV_EABSENSI_SOURCE_FORMAT
    assert workflow["importStats"] == {
        "totalRaw": canonical_count,
        "totalRecord": canonical_count,
        "rejectedRows": 0,
        "valid": canonical_count,
        "bermasalah": 0,
        "months": ["2026-08"],
    }


def test_period_isolation_and_same_period_replacement(service):
    aug = upload(service, csv_bytes([row("A1", "2026-08-01"), row("A2", "2026-08-02")]))
    service.commit(aug["import_id"])
    sep = upload(service, csv_bytes([row("S1", "2026-09-01")]), filename="sep.csv")
    service.commit(sep["import_id"])
    aug2 = upload(service, csv_bytes([row("A3", "2026-08-03")]), filename="aug-v2.csv")
    done = service.commit(aug2["import_id"])
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute("SELECT nip FROM raw_import WHERE substr(tanggal,1,7)='2026-08'").fetchall() == [("A3",)]
        assert conn.execute("SELECT nip FROM raw_import WHERE substr(tanggal,1,7)='2026-09'").fetchall() == [("S1",)]
        assert conn.execute("SELECT import_id FROM raw_import WHERE nip='A3'").fetchone() == (aug2["import_id"],)
        workflow = json.loads(conn.execute("SELECT _workflow FROM workflow_config").fetchone()[0])
        assert workflow["month"] == "2026-08"
        assert workflow["import_id"] == aug2["import_id"]
        conn.execute("CREATE TABLE master_data(nip VARCHAR,nama VARCHAR,jabatan VARCHAR,tukin DOUBLE)")
        conn.execute("INSERT INTO master_data VALUES ('A3','Pegawai A3','Guru',1000)")
        source = load_canonical_source(conn, "2026-08")
        assert len(source["records"]) == 1
        assert source["workflow"]["import_id"] == aug2["import_id"]
    assert done["predecessor_import_id"] == aug["import_id"]
    assert done["ready_for_calculation"] is True


def test_duplicate_committed_file_is_rejected(eabs_service):
    rows = [eabs_row("1", "2026-08-01", "2026-08-01 07:30", "2026-08-01 16:00")]
    first = upload_eabs(eabs_service, rows, pair_cross_day=False)
    eabs_service.commit(first["import_id"], period="2026-08")
    second = upload_eabs(eabs_service, rows, filename="copy.csv", pair_cross_day=False)
    assert second["duplicate_of_import_id"] == first["import_id"]
    assert second["status"] == "ALREADY_COMMITTED"
    assert second["authoritative_import"]["import_id"] == first["import_id"]
    assert eabs_service.use_committed(second["import_id"])["import_id"] == first["import_id"]


def test_duplicate_guard_ignores_failed_and_non_authoritative_history(service):
    first_content = csv_bytes([row("FIRST")])
    first = upload(service, first_content)
    service.commit(first["import_id"])
    newer = upload(service, csv_bytes([row("NEWER")]), filename="newer.csv")
    service.commit(newer["import_id"])

    historical_retry = upload(service, first_content, filename="historical.csv")
    assert historical_retry["status"] == "READY"
    assert historical_retry["duplicate_of_import_id"] is None

    failed_content = csv_bytes([row("FAILED")])
    failed = upload(service, failed_content, filename="failed.csv")
    with duckdb.connect(service.db_path) as conn:
        conn.execute(
            "UPDATE absence_import_batch SET status='FAILED' WHERE import_id=?",
            [failed["import_id"]],
        )
    failed_retry = upload(service, failed_content, filename="failed-retry.csv")
    assert failed_retry["status"] == "READY"
    assert failed_retry["duplicate_of_import_id"] is None


def test_reset_active_csv_period_allows_identical_reimport_and_preserves_other_data(tmp_path):
    db = tmp_path / "period-reset.duckdb"
    initialize_runtime_state(db)
    svc = AbsenceImportService(
        db, tmp_path / "uploads",
        authority_config=ImportAuthorityConfig(
            ImportAuthority.IMPORT_BACKEND_V1, csv_eabsensi_enabled=True,
        ),
    )
    rows = [eabs_row("CSV-1", "2026-08-01", "2026-08-01 07:30", "2026-08-01 16:00")]
    first = upload_eabs(svc, rows, pair_cross_day=False)
    committed = svc.commit(first["import_id"], period="2026-08")
    duplicate = upload_eabs(svc, rows, filename="same.csv", pair_cross_day=False)
    assert duplicate["status"] == "ALREADY_COMMITTED"
    assert svc.use_committed(duplicate["import_id"])["import_id"] == committed["import_id"]

    old_ready = upload_eabs(
        svc, [eabs_row("OLD", "2026-09-01", "2026-09-01 07:30", "2026-09-01 16:00")],
        filename="old-ready.csv", pair_cross_day=False,
    )
    with duckdb.connect(str(db)) as conn:
        conn.execute("CREATE TABLE master_data(nip VARCHAR, nama VARCHAR)")
        conn.execute("INSERT INTO master_data VALUES ('MASTER','Tetap')")
        conn.execute("CREATE TABLE master_tukin(id VARCHAR)")
        conn.execute("INSERT INTO master_tukin VALUES ('TUKIN-KEEP')")
        conn.execute("CREATE TABLE skp_import_source(id VARCHAR)")
        conn.execute("INSERT INTO skp_import_source VALUES ('SKP-KEEP')")
        conn.execute("CREATE TABLE pengembalian_period(id VARCHAR)")
        conn.execute("INSERT INTO pengembalian_period VALUES ('RETURN-KEEP')")
        conn.execute("CREATE TABLE application_settings(id VARCHAR)")
        conn.execute("INSERT INTO application_settings VALUES ('SETTING-KEEP')")
        conn.execute("INSERT INTO raw_import SELECT * FROM raw_import LIMIT 0")
        row_values = conn.execute(
            "SELECT * FROM raw_import WHERE workflow_id=? LIMIT 1",
            [f"backend-import-{first['import_id']}"],
        ).fetchone()
        columns = [item[0] for item in conn.description]
        other = dict(zip(columns, row_values))
        other.update({"workflow_id": "OTHER-WORKFLOW", "import_id": "OTHER-IMPORT",
                      "tanggal": "2026-07-01", "nip": "OTHER"})
        conn.execute(
            f"INSERT INTO raw_import ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [other[column] for column in columns],
        )
        conn.execute("CREATE TABLE hitung_harian(bulan VARCHAR,nip VARCHAR)")
        conn.execute("INSERT INTO hitung_harian VALUES ('2026-08','CSV-1'),('2026-07','OTHER')")
        conn.execute("CREATE TABLE rekap_sdm(bulan VARCHAR,nip VARCHAR)")
        conn.execute("INSERT INTO rekap_sdm VALUES ('2026-08','CSV-1'),('2026-07','OTHER')")
        conn.execute("CREATE TABLE final_absensi(bulan VARCHAR,nip VARCHAR)")
        conn.execute("INSERT INTO final_absensi VALUES ('2026-08','CSV-1'),('2026-07','OTHER')")
        protected_before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("master_data", "master_tukin", "skp_import_source",
                          "pengembalian_period", "application_settings")
        }

    generation_before = current_reset_generation()
    plan = svc.reset_period_preview(first["import_id"])
    assert plan["raw_import"] == 1
    reset = svc.reset_period(first["import_id"], "RESET")
    assert reset["status"] == "RESET"
    assert reset["reset_generation"] == generation_before + 1
    assert svc.get(first["import_id"])["status"] == "RESET"
    with duckdb.connect(str(db)) as conn:
        assert conn.execute("SELECT count(*) FROM raw_import WHERE workflow_id=?", [plan["workflow_id"]]).fetchone()[0] == 0
        assert conn.execute("SELECT nip FROM raw_import WHERE workflow_id='OTHER-WORKFLOW'").fetchall() == [("OTHER",)]
        for table, rows_before in protected_before.items():
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows_before
        assert conn.execute("SELECT * FROM hitung_harian").fetchall() == [("2026-07", "OTHER")]
        assert conn.execute("SELECT * FROM rekap_sdm").fetchall() == [("2026-07", "OTHER")]
        assert conn.execute("SELECT * FROM final_absensi").fetchall() == [("2026-07", "OTHER")]
        assert conn.execute("SELECT count(*) FROM workflow_config").fetchone()[0] == 0

    with pytest.raises(ImportEngineError) as stale:
        svc.commit(old_ready["import_id"], period="2026-09")
    assert stale.value.code == "STALE_GENERATION"

    repeated = upload_eabs(svc, rows, filename="same-after-reset.csv", pair_cross_day=False)
    assert repeated["status"] == "READY"
    recommitted = svc.commit(repeated["import_id"], period="2026-08")
    assert recommitted["status"] == "COMMITTED"


def test_reset_active_csv_period_rolls_back_on_failure(tmp_path):
    db = tmp_path / "period-reset-rollback.duckdb"
    initialize_runtime_state(db)
    svc = AbsenceImportService(
        db, tmp_path / "uploads",
        authority_config=ImportAuthorityConfig(
            ImportAuthority.IMPORT_BACKEND_V1, csv_eabsensi_enabled=True,
        ),
    )
    batch = upload_eabs(
        svc, [eabs_row("CSV-1", "2026-08-01", "2026-08-01 07:30", "2026-08-01 16:00")],
        pair_cross_day=False,
    )
    svc.commit(batch["import_id"], period="2026-08")

    def fail(_conn):
        raise RuntimeError("injected reset failure")

    with pytest.raises(RuntimeError, match="injected reset failure"):
        svc.reset_period(batch["import_id"], "RESET", before_commit=fail)
    assert svc.get(batch["import_id"])["status"] == "COMMITTED"
    with duckdb.connect(str(db)) as conn:
        assert conn.execute("SELECT count(*) FROM raw_import").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workflow_config").fetchone()[0] == 1


def test_generation_change_blocks_atomic_promote(tmp_path):
    active = {"value": 4}
    svc = AbsenceImportService(
        tmp_path / "db.duckdb", tmp_path / "upload",
        authority_config=ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1),
        generation_reader=lambda: active["value"],
        generation_validator=lambda value: None if value == active["value"] else (_ for _ in ()).throw(RuntimeError("stale")),
    )
    batch = upload(svc, csv_bytes([row("1")]))
    active["value"] = 5
    with pytest.raises(ImportEngineError) as error:
        svc.commit(batch["import_id"])
    assert error.value.code == "STALE_GENERATION"
    with duckdb.connect(svc.db_path) as conn:
        assert not any(item[0] == "raw_import" for item in conn.execute("SHOW TABLES").fetchall())


def test_failure_inside_transaction_rolls_back_everything(service):
    old = upload(service, csv_bytes([row("OLD")]))
    service.commit(old["import_id"])
    with duckdb.connect(service.db_path) as conn:
        old_config = conn.execute("SELECT _workflow FROM workflow_config").fetchone()[0]
    new = upload(service, csv_bytes([row("NEW")]), filename="new.csv")

    def fail(conn):
        conn.execute("CREATE TABLE injected(value INTEGER)")
        raise RuntimeError("injected")

    with pytest.raises(ImportEngineError, match="injected") as error:
        service.commit(new["import_id"], before_commit=fail)
    assert error.value.code == "PROMOTION_FAILED"
    assert service.get(new["import_id"])["status"] == "FAILED"
    assert service.get(new["import_id"])["error_code"] == "PROMOTION_FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute("SELECT nip FROM raw_import").fetchall() == [("OLD",)]
        assert conn.execute("SELECT _workflow FROM workflow_config").fetchone()[0] == old_config
        assert "injected" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


def test_same_period_legacy_raw_schema_is_replaced_safely(service):
    batch = upload(service, csv_bytes([row("NEW", date="2026-08-03")]))
    with duckdb.connect(service.db_path) as conn:
        conn.execute('CREATE TABLE raw_import("Tanggal" VARCHAR,"NIP" VARCHAR,"Nama" VARCHAR,"Jam Datang" VARCHAR,"Jam Pulang" VARCHAR)')
        conn.execute("INSERT INTO raw_import VALUES ('2026-08-01','OLD','Old','07:30','16:00')")
    committed = service.commit(batch["import_id"])
    assert committed["status"] == "COMMITTED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute("SELECT nip,tanggal FROM raw_import").fetchall() == [("NEW", "2026-08-03")]


def test_legacy_raw_schema_with_other_period_is_not_discarded(service):
    batch = upload(service, csv_bytes([row("NEW", date="2026-08-03")]))
    with duckdb.connect(service.db_path) as conn:
        conn.execute('CREATE TABLE raw_import("Tanggal" VARCHAR,"NIP" VARCHAR,"Nama" VARCHAR,"Jam Datang" VARCHAR,"Jam Pulang" VARCHAR)')
        conn.execute("INSERT INTO raw_import VALUES ('2026-09-01','KEEP','Keep','07:30','16:00')")
    with pytest.raises(ImportEngineError) as error:
        service.commit(batch["import_id"])
    assert error.value.code == "RAW_IMPORT_SCHEMA_CONFLICT"
    assert service.get(batch["import_id"])["status"] == "FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute('SELECT "NIP" FROM raw_import').fetchall() == [("KEEP",)]


def test_xlsx_streaming_import(service, tmp_path):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Absensi")
    sheet.append(HEADERS)
    sheet.append(list(row("X1").values()))
    output = io.BytesIO()
    book.save(output)
    batch = upload(service, output.getvalue(), filename="absence.xlsx")
    assert batch["status"] == "READY" and batch["valid_rows"] == 1


def test_parquet_direct_scan_import(service, tmp_path):
    source = tmp_path / "source.parquet"
    conn = duckdb.connect()
    conn.execute("""
        COPY (SELECT 'P1' NIP,'Parquet' Nama,'Guru' Jabatan,'2026-08-01' Tanggal,
                     '07:30' "Jam Masuk",'16:00' "Jam Pulang",'WIB' Zona,'' Provinsi,'' Jenis)
        TO ? (FORMAT PARQUET)
    """, [str(source)])
    conn.close()
    batch = upload(service, source.read_bytes(), filename="absence.parquet")
    assert batch["status"] == "READY" and batch["valid_rows"] == 1


@pytest.mark.parametrize(("filename", "content", "error_code"), [
    ("corrupt.csv", b"\xff\x00", "IMPORT_PARSE_FAILED"),
    ("corrupt.xlsx", b"not-a-workbook", "IMPORT_PARSE_FAILED"),
    ("missing.csv", csv_bytes([{"Unrelated": "value"}], ["Unrelated"]), "REQUIRED_COLUMN_MISSING"),
])
def test_corrupt_files_and_missing_columns_fail_without_canonical_data(service, filename, content, error_code):
    batch = upload(service, content, filename=filename)
    assert batch["status"] == "FAILED" and batch["error_code"] == error_code
    with duckdb.connect(service.db_path) as conn:
        assert "raw_import" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


def test_wali_pairing_cross_midnight_and_source_duplicates(service):
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [
        {"tgl_finger": "2026-08-01", "nm_peg": "W", "nip": "W1", "jns_hadir": "", "nm_jabatan": "Wali Asuh", "absen_pagi": "20:00", "absen_sore": ""},
        {"tgl_finger": "2026-08-02", "nm_peg": "W", "nip": "W1", "jns_hadir": "", "nm_jabatan": "Wali Asuh", "absen_pagi": "", "absen_sore": "06:00"},
    ]
    rows.append(dict(rows[-1]))
    batch = upload(service, csv_bytes(rows, headers), source_format="wali")
    assert batch["status"] == "READY" and batch["wali_rows"] == 1
    assert batch["duplicate_rows"] == 1
    preview = service.preview(batch["import_id"])["rows"][0]
    assert preview["cross_day"] is True and preview["source_format"] == "wali"


def test_wali_streaming_pairing_matches_frozen_legacy_golden(service):
    fixture = json.loads(WALI_FIXTURE.read_text(encoding="utf-8"))
    legacy_current = json.loads(subprocess.run(
        ["node", str(WALI_LEGACY_RUNNER), str(WALI_FIXTURE)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout)
    expected = json.loads(WALI_GOLDEN.read_text(encoding="utf-8"))
    assert legacy_current == expected
    batch = upload(
        service, wali_csv_bytes(fixture["headers"], fixture["rows"]),
        filename="wali-golden.csv", source_format="wali",
    )
    assert batch["status"] == "READY"
    assert batch["timings"]["source_rows"] == len(fixture["rows"])
    assert batch["timings"]["duplicate_source_rows"] == expected["duplicate_source_rows"]
    with service._connect() as conn:
        cursor = conn.execute("""
            SELECT nip,nama,jabatan,tanggal,tanggal_pulang,masuk_min,pulang_min,
                   zona,cross_day,source_format
            FROM absence_import_staging
            WHERE import_id=?
            ORDER BY tanggal,nip
        """, [batch["import_id"]])
        actual_rows = cursor.fetchall()
    actual = [{
        "nip": item[0], "nama": item[1], "jabatan": item[2], "date": item[3],
        "out_date": parse_date(item[4])["key"] if item[4] else "",
        "check_in": item[5], "check_out": item[6],
        "zone": item[7], "cross_day": item[8], "source_format": item[9],
    } for item in actual_rows]
    expected_material = [{
        key: item[key] for key in (
            "nip", "nama", "jabatan", "date", "out_date", "check_in", "check_out",
            "zone", "cross_day", "source_format",
        )
    } for item in expected["records"]]
    assert actual == expected_material
    assert len(actual) == expected["records"].__len__()
    assert actual[2]["check_in"] == 360 and actual[2]["check_out"] == 0
    assert actual[3]["check_out"] is None
    assert actual[-1]["out_date"] == "2027-01-01"
    malformed = next(
        item for item in service.preview(batch["import_id"], page=1, page_size=100)["rows"]
        if item["nip"] == "N10"
    )
    assert malformed["severity"] == "BLOCKER"
    assert {"INVALID_CHECK_IN", "INVALID_CHECK_OUT"} <= set(malformed["error_codes"])
    with duckdb.connect(service.db_path) as conn:
        conn.execute("CREATE TABLE master_data(nip VARCHAR,nama VARCHAR,jabatan VARCHAR,tukin DOUBLE)")
        conn.executemany(
            "INSERT INTO master_data VALUES (?,?,?,?)",
            [(f"N{index}", f"Person {index}", "Wali", 1000) for index in range(1, 13)],
        )
    committed = service.commit(batch["import_id"], period="2026-08", accept_invalid=True)
    assert committed["status"] == "COMMITTED"
    with duckdb.connect(service.db_path) as conn:
        workflow = json.loads(conn.execute("SELECT _workflow FROM workflow_config").fetchone()[0])
        assert workflow["importMode"] == "wali"
        assert workflow["import_id"] == batch["import_id"]
        canonical = load_canonical_source(conn, "2026-08")
    assert canonical["workflow"]["import_id"] == batch["import_id"]
    assert canonical["records"] and all(item["sourceFormat"] == "wali" for item in canonical["records"])


def test_wali_large_staging_flushes_bounded_buffers(service):
    size = 10_000
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = []
    for index in range(size):
        rows.append([
            f"2026-08-{(index // 2) % 28 + 1:02d}", f"Person {index // 2}", f"W{index // 2:06d}",
            "", "Wali", "07:30" if index % 2 == 0 else "",
            "" if index % 2 == 0 else "16:00",
        ])
    batch = upload(
        service, wali_csv_bytes(headers, rows), filename="wali-large.csv", source_format="wali",
    )
    assert batch["status"] == "READY"
    assert batch["timings"]["source_rows"] == size
    assert batch["timings"]["paired_work_date_rows"] == size // 2
    assert batch["valid_rows"] == size // 2
    assert service.preview(batch["import_id"], page=1, page_size=100)["total"] == size // 2


def test_wali_failure_after_output_flush_does_not_promote_partial_data(service, monkeypatch):
    size = 5_001
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [[
        f"2026-08-{index % 28 + 1:02d}", f"Person {index}", f"FAIL{index:06d}",
        "", "Wali", "07:30", "16:00",
    ] for index in range(size)]
    item = service.save_upload(
        UploadFile(io.BytesIO(wali_csv_bytes(headers, rows)), filename="wali-fail.csv"),
        operator="uat", source_format="wali", workflow_config_json=json.dumps(CONFIG),
    )
    original = absence_import._canonical_stage_row

    def fail_after_first_output_batch(import_id, source_row, *args, **kwargs):
        if source_row > 5000:
            raise RuntimeError("injected Wali output failure")
        return original(import_id, source_row, *args, **kwargs)

    monkeypatch.setattr(absence_import, "_canonical_stage_row", fail_after_first_output_batch)
    failed = service.stage(item["import_id"])
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "IMPORT_PARSE_FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM absence_import_staging WHERE import_id=?", [item["import_id"]]
        ).fetchone()[0] == 5000
        assert "raw_import" not in {row[0] for row in conn.execute("SHOW TABLES").fetchall()}


def test_wali_row_limit_fails_explicitly_before_canonical_promote(service, monkeypatch):
    monkeypatch.setenv("EVKIN_WALI_IMPORT_MAX_ROWS", "2")
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [
        ["2026-08-01", "Limit", "L1", "", "Wali", "07:00", "16:00"],
        ["2026-08-02", "Limit", "L2", "", "Wali", "07:00", "16:00"],
        ["2026-08-03", "Limit", "L3", "", "Wali", "07:00", "16:00"],
    ]
    batch = upload(
        service, wali_csv_bytes(headers, rows), filename="wali-over-limit.csv", source_format="wali",
    )
    assert batch["status"] == "FAILED"
    assert batch["error_code"] == "WALI_IMPORT_LIMIT_EXCEEDED"
    assert "setidaknya 3" in batch["error_message"]
    with duckdb.connect(service.db_path) as conn:
        assert "raw_import" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


def test_wali_row_limit_cannot_exceed_validated_capacity(service, monkeypatch):
    monkeypatch.setenv("EVKIN_WALI_IMPORT_MAX_ROWS", "500001")
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [["2026-08-01", "Capacity", "C1", "", "Wali", "07:00", "16:00"]]
    batch = upload(
        service, wali_csv_bytes(headers, rows), filename="wali-invalid-limit.csv", source_format="wali",
    )
    assert batch["status"] == "FAILED"
    assert batch["error_code"] == "INVALID_WALI_IMPORT_LIMIT"
    with duckdb.connect(service.db_path) as conn:
        assert "raw_import" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


def test_wali_source_rows_missing_nip_or_date_remain_blockers(service):
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [
        ["2026-08-01", "No NIP", "", "", "Wali", "07:00", "16:00"],
        ["invalid", "No date", "ND1", "", "Wali", "07:00", "16:00"],
    ]
    batch = upload(
        service, wali_csv_bytes(headers, rows), filename="wali-invalid-identity.csv", source_format="wali",
    )
    errors = service.preview(batch["import_id"], errors_only=True)
    assert errors["total"] == 2
    by_name = {item["nama"]: set(item["error_codes"]) for item in errors["rows"]}
    assert "MISSING_NIP" in by_name["No NIP"]
    assert "INVALID_DATE" in by_name["No date"]


def test_truncated_wali_xlsx_fails_without_canonical_data(service):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Wali")
    sheet.append(["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"])
    sheet.append(["2026-08-01", "Truncated", "T1", "", "Wali", "07:00", "16:00"])
    content = io.BytesIO()
    workbook.save(content)
    damaged = content.getvalue()[: len(content.getvalue()) // 2]
    batch = upload(service, damaged, filename="truncated-wali.xlsx", source_format="wali")
    assert batch["status"] == "FAILED"
    assert batch["error_code"] == "IMPORT_PARSE_FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert "raw_import" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


def test_wali_staging_database_write_failure_does_not_promote(service, monkeypatch):
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [["2026-08-01", "DB failure", "DB1", "", "Wali", "07:00", "16:00"]]
    item = service.save_upload(
        UploadFile(io.BytesIO(wali_csv_bytes(headers, rows)), filename="wali-db-fail.csv"),
        operator="uat", source_format="wali", workflow_config_json=json.dumps(CONFIG),
    )
    monkeypatch.setattr(
        absence_import, "_insert_stage_buffer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected DB write failure")),
    )
    failed = service.stage(item["import_id"])
    assert failed["status"] == "FAILED" and failed["error_code"] == "IMPORT_PARSE_FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM absence_import_staging WHERE import_id=?", [item["import_id"]]
        ).fetchone()[0] == 0
        assert "raw_import" not in {row[0] for row in conn.execute("SHOW TABLES").fetchall()}


def test_wali_reset_generation_overlap_blocks_commit(tmp_path):
    active = {"value": 3}
    svc = AbsenceImportService(
        tmp_path / "wali-generation.duckdb", tmp_path / "uploads",
        authority_config=ImportAuthorityConfig(ImportAuthority.IMPORT_BACKEND_V1),
        generation_reader=lambda: active["value"],
        generation_validator=lambda generation: None if generation == active["value"] else (_ for _ in ()).throw(RuntimeError("stale")),
    )
    headers = ["tgl_finger", "nm_peg", "nip", "jns_hadir", "nm_jabatan", "absen_pagi", "absen_sore"]
    rows = [["2026-08-01", "Stale", "STALE1", "", "Wali", "07:00", "16:00"]]
    batch = upload(
        svc, wali_csv_bytes(headers, rows), filename="wali-stale.csv", source_format="wali",
    )
    active["value"] = 4
    with pytest.raises(ImportEngineError) as error:
        svc.commit(batch["import_id"])
    assert error.value.code == "STALE_GENERATION"
    with duckdb.connect(svc.db_path) as conn:
        assert "raw_import" not in {item[0] for item in conn.execute("SHOW TABLES").fetchall()}


@pytest.mark.parametrize("status", ["VALIDATING", "NORMALIZING", "COMMITTING"])
def test_recovery_marks_inflight_batches_failed(tmp_path, status):
    db = tmp_path / "db.duckdb"
    with duckdb.connect(db) as conn:
        ensure_absence_import_schema(conn)
        conn.execute("""
            INSERT INTO absence_import_batch(import_id,filename,file_type,file_size,file_sha256,source_format,
              operator,reset_generation,engine_version,status) VALUES ('x','x.csv','csv',1,'a','standard','u',1,'v',?)
        """, [status])
    assert recover_interrupted_imports(db) == 1
    with duckdb.connect(db) as conn:
        assert conn.execute("SELECT status,error_code FROM absence_import_batch WHERE import_id='x'").fetchone() == ("FAILED", "PROCESS_RESTARTED")


def test_large_csv_is_bounded_and_complete(service):
    count = 20_000
    content = csv_bytes([row(str(index), f"2026-08-{index % 28 + 1:02d}") for index in range(count)])
    batch = upload(service, content, filename="large.csv")
    assert batch["status"] == "READY" and batch["staged_rows"] == count


def test_preview_and_error_pages_are_bounded_and_complete(service):
    batch = upload(service, csv_bytes([
        row(f"BAD-{index}", masuk="invalid") for index in range(120)
    ]))
    first = service.preview(batch["import_id"], page=1, page_size=50)
    next_page = service.preview(batch["import_id"], page=2, page_size=50)
    oversized = service.preview(batch["import_id"], page=1, page_size=1000)
    first_errors = service.preview(batch["import_id"], page=1, page_size=1000, errors_only=True)
    next_errors = service.preview(batch["import_id"], page=2, page_size=100, errors_only=True)
    assert len(first["rows"]) == 50 and first["rows"][0]["source_row"] == 1
    assert next_page["rows"][0]["source_row"] == 51
    assert oversized["page_size"] == 100 and len(oversized["rows"]) == 100
    assert first_errors["total"] == 120 and len(first_errors["rows"]) == 100
    assert next_errors["rows"][0]["source_row"] == 101
    assert "INVALID_CHECK_IN" in next_errors["rows"][0]["error_codes"]


def test_partial_staging_failure_never_promotes_canonical_rows(service, monkeypatch):
    content = csv_bytes([row(str(index)) for index in range(5001)])
    item = service.save_upload(
        UploadFile(io.BytesIO(content), filename="staging-failure.csv"), operator="uat",
        source_format="standard", workflow_config_json=json.dumps(CONFIG),
    )
    original = absence_import._canonical_stage_row

    def fail_after_first_flush(*args, **kwargs):
        if args[1] == 5001:
            raise RuntimeError("injected stage failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(absence_import, "_canonical_stage_row", fail_after_first_flush)
    failed = service.stage(item["import_id"])
    assert failed["status"] == "FAILED" and failed["error_code"] == "IMPORT_PARSE_FAILED"
    with duckdb.connect(service.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM absence_import_staging WHERE import_id=?", [item["import_id"]]
        ).fetchone()[0] == 5000
        assert "raw_import" not in {row[0] for row in conn.execute("SHOW TABLES").fetchall()}

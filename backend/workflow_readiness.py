"""Server-side readiness checks for the attendance-to-Tukin workflow."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import duckdb


PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class WorkflowNotReadyError(ValueError):
    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__("Workflow belum siap untuk Final Tukin.")


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0])


def _rows(conn: duckdb.DuckDBPyConnection, table: str, period: str | None = None) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    if not columns or columns == {"_empty"}:
        return []
    query = f'SELECT * FROM "{table}"'
    params: list[Any] = []
    if period and "bulan" in columns:
        query += " WHERE bulan = ?"
        params.append(period)
    return conn.execute(query, params).fetchdf().to_dict(orient="records")


def _period_rows(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        value = row.get("bulan") or row.get("monthKey")
        if value is None:
            value = str(row.get("tanggalKey") or row.get("tanggal") or "")[:7]
        if str(value) == period:
            result.append(row)
    return result


def _nip(row: dict[str, Any]) -> str:
    return str(row.get("nip") or row.get("NIP") or "").strip()


def _date_key(row: dict[str, Any]) -> str:
    return str(row.get("tanggalKey") or row.get("tanggal") or row.get("tgl_finger") or "").strip()


def _source_signature(rows: list[dict[str, Any]]) -> str:
    normalized = [{str(k): v for k, v in row.items()} for row in rows]
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _period_clause(columns: set[str], period: str) -> tuple[str, list[str]]:
    if "bulan" in columns:
        return ' WHERE CAST("bulan" AS VARCHAR) = ?', [period]
    if "monthKey" in columns:
        return ' WHERE CAST("monthKey" AS VARCHAR) = ?', [period]
    if "tanggalKey" in columns:
        return ' WHERE substr(CAST("tanggalKey" AS VARCHAR), 1, 7) = ?', [period]
    return "", []


def _relation_signature(conn: duckdb.DuckDBPyConnection, table: str, period: str) -> str:
    """Return an order-independent signature without transferring full rows to Python."""
    if not _table_exists(conn, table):
        return "0:0:0"
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not columns or columns == ["_empty"]:
        return "0:0:0"
    where, params = _period_clause(set(columns), period)
    quoted = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in sorted(columns))
    count, total, xor_value = conn.execute(
        f'SELECT count(*), coalesce(sum(hash({quoted})), 0), '
        f'coalesce(bit_xor(hash({quoted})), 0) FROM "{table}"{where}',
        params,
    ).fetchone()
    return f"{int(count)}:{int(total)}:{int(xor_value)}"


def _database_signature(conn: duckdb.DuckDBPyConnection, period: str) -> str:
    payload = "|".join([
        _relation_signature(conn, "hitung_harian", period),
        _relation_signature(conn, "anomali", period),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _flag(conn: duckdb.DuckDBPyConnection, period: str) -> bool:
    if not _table_exists(conn, "workflow_flags"):
        return False
    columns = {row[1] for row in conn.execute('PRAGMA table_info("workflow_flags")').fetchall()}
    if "verified" not in columns:
        return False
    row = conn.execute(
        "SELECT verified FROM workflow_flags WHERE k = ? ORDER BY updated_at DESC LIMIT 1",
        [f"semifinal:{period}"],
    ).fetchone()
    return bool(row and int(row[0] or 0))


def _stale(conn: duckdb.DuckDBPyConnection, period: str) -> bool:
    if not _table_exists(conn, "workflow_readiness_meta"):
        return False
    row = conn.execute(
        "SELECT source_signature FROM workflow_readiness_meta WHERE period = ?",
        [period],
    ).fetchone()
    return bool(row and row[0] != _database_signature(conn, period))


def validate_final_readiness(db_path: str, period: str) -> dict[str, Any]:
    """Validate all server-owned gates before a Final Tukin is created."""
    result: dict[str, Any] = {
        "ready": False,
        "period": period,
        "checks": {
            "period_valid": bool(PERIOD_RE.fullmatch(str(period or ""))),
            "harian_available": False,
            "harian_duplicates": False,
            "semifinal_available": False,
            "semifinal_verified": False,
            "rekap_available": False,
            "rekap_duplicates": False,
            "rekap_consistent": False,
            "stale_state": False,
        },
        "blocking_reasons": [],
    }
    if not result["checks"]["period_valid"]:
        result["blocking_reasons"].append("Periode harus berformat YYYY-MM.")
        return result

    with duckdb.connect(str(db_path), read_only=True) as conn:
        daily = _rows(conn, "hitung_harian", period)
        semifinal = _rows(conn, "anomali", period)
        rekap = _rows(conn, "rekap_sdm", period)
        if _table_exists(conn, "hitung_harian") and not any("bulan" == row[1] for row in conn.execute('PRAGMA table_info("hitung_harian")').fetchall()):
            daily = _period_rows(_rows(conn, "hitung_harian"), period)
        if _table_exists(conn, "anomali") and not any("bulan" == row[1] for row in conn.execute('PRAGMA table_info("anomali")').fetchall()):
            semifinal = _period_rows(_rows(conn, "anomali"), period)
        if _table_exists(conn, "rekap_sdm") and not any("bulan" == row[1] for row in conn.execute('PRAGMA table_info("rekap_sdm")').fetchall()):
            rekap = _period_rows(_rows(conn, "rekap_sdm"), period)
        result["checks"]["harian_available"] = bool(daily)
        result["checks"]["semifinal_available"] = bool(semifinal)
        result["checks"]["rekap_available"] = bool(rekap)

        daily_keys = [(_nip(row), _date_key(row)) for row in daily]
        daily_dups = [key for key, count in Counter(daily_keys).items() if key[0] and count > 1]
        result["checks"]["harian_duplicates"] = bool(daily_dups)
        if not daily:
            result["blocking_reasons"].append("Hasil Hitung Harian belum tersedia.")
        if daily_dups:
            result["blocking_reasons"].append(f"Ditemukan {len(daily_dups)} duplikat NIP/tanggal pada Hitung Harian.")
        if not semifinal:
            result["blocking_reasons"].append("Hasil Semi Final belum tersedia.")

        verified = _flag(conn, period)
        result["checks"]["semifinal_verified"] = verified
        if not verified:
            result["blocking_reasons"].append("Verifikasi Semi Final belum tercatat di database.")

        rekap_keys = [(_nip(row), str(row.get("bulan") or period)) for row in rekap]
        rekap_dups = [key for key, count in Counter(rekap_keys).items() if not key[0] or count > 1]
        result["checks"]["rekap_duplicates"] = bool(rekap_dups)
        if not rekap:
            result["blocking_reasons"].append("Rekap SDM belum tersedia.")
        if rekap_dups:
            result["blocking_reasons"].append("Rekap SDM memiliki NIP kosong atau duplikat.")

        daily_by_nip = Counter(_nip(row) for row in daily if _nip(row))
        rekap_by_nip = Counter(_nip(row) for row in rekap if _nip(row))
        mismatches = []
        for nip in sorted(set(daily_by_nip) | set(rekap_by_nip)):
            actual = next((int(row.get("jumlahRecord") or 0) for row in rekap if _nip(row) == nip), 0)
            if daily_by_nip[nip] != actual:
                mismatches.append((nip, daily_by_nip[nip], actual))
        result["checks"]["rekap_consistent"] = bool(rekap) and not rekap_dups and not mismatches and set(daily_by_nip) == set(rekap_by_nip)
        if mismatches or set(daily_by_nip) != set(rekap_by_nip):
            result["blocking_reasons"].append(
                f"Rekap SDM tidak konsisten: {len(daily_by_nip)} NIP Harian, {len(rekap_by_nip)} NIP Rekap."
            )

        if _stale(conn, period):
            result["checks"]["stale_state"] = True
            result["blocking_reasons"].append("Data hulu berubah setelah verifikasi; hitung dan verifikasi ulang.")

    result["ready"] = not result["blocking_reasons"]
    return result


def record_verification(db_path: str, period: str) -> dict[str, Any]:
    """Persist verification only after the daily and semifinal snapshot is valid."""
    if not PERIOD_RE.fullmatch(str(period or "")):
        raise ValueError("Periode harus berformat YYYY-MM.")
    with duckdb.connect(str(db_path)) as conn:
        if not _table_exists(conn, "hitung_harian"):
            raise ValueError("Hasil Hitung Harian belum tersedia.")
        daily_columns = {row[1] for row in conn.execute('PRAGMA table_info("hitung_harian")').fetchall()}
        if not daily_columns or daily_columns == {"_empty"}:
            raise ValueError("Hasil Hitung Harian belum tersedia.")
        where, params = _period_clause(daily_columns, period)
        daily_count = int(conn.execute(f'SELECT count(*) FROM "hitung_harian"{where}', params).fetchone()[0])
        if not daily_count:
            raise ValueError("Hasil Hitung Harian belum tersedia.")
        nip_column = "nip" if "nip" in daily_columns else ("NIP" if "NIP" in daily_columns else None)
        date_column = next((name for name in ("tanggalKey", "tanggal", "tgl_finger") if name in daily_columns), None)
        if not nip_column or not date_column:
            raise ValueError("Kolom NIP/tanggal pada Hitung Harian tidak lengkap.")
        q_nip = f'"{nip_column}"'
        q_date = f'"{date_column}"'
        duplicates = int(conn.execute(
            f'SELECT count(*) FROM (SELECT {q_nip}, {q_date}, count(*) n '
            f'FROM "hitung_harian"{where} GROUP BY {q_nip}, {q_date} HAVING count(*) > 1)',
            params,
        ).fetchone()[0])
        if duplicates:
            raise ValueError("Masih ada duplikat NIP/tanggal pada Hitung Harian.")
        # Semi Final is an approved snapshot of the canonical daily result.
        # Materialize it inside DuckDB instead of uploading the same large row
        # set from the browser a second time.
        conn.execute('CREATE OR REPLACE TABLE "anomali" AS SELECT * FROM "hitung_harian"')
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workflow_readiness_meta (period VARCHAR PRIMARY KEY, source_signature VARCHAR, updated_at TIMESTAMP)"
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_flags (k VARCHAR PRIMARY KEY, verified INTEGER, updated_at TIMESTAMP)")
        flag_columns = {row[1] for row in conn.execute('PRAGMA table_info("workflow_flags")').fetchall()}
        if "verified" not in flag_columns:
            conn.execute("ALTER TABLE workflow_flags ADD COLUMN verified INTEGER DEFAULT 0")
        if "updated_at" not in flag_columns:
            conn.execute("ALTER TABLE workflow_flags ADD COLUMN updated_at TIMESTAMP")
        conn.execute(
            "INSERT OR REPLACE INTO workflow_flags (k, verified, updated_at) VALUES (?, 1, ?)",
            [f"semifinal:{period}", now],
        )
        conn.execute(
            "INSERT OR REPLACE INTO workflow_readiness_meta (period, source_signature, updated_at) VALUES (?, ?, ?)",
            [period, _database_signature(conn, period), now],
        )
    return {"status": "ok", "period": period, "verified": True, "rows": daily_count}

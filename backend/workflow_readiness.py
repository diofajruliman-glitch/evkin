"""Server-side readiness checks for the attendance-to-Tukin workflow."""
from __future__ import annotations

import hashlib
import json
import re
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
            value = str(row.get("tanggalKey") or row.get("tanggal") or row.get("tgl_finger") or "")[:7]
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
    for column in ("tanggalKey", "tanggal", "tgl_finger"):
        if column in columns:
            return f' WHERE substr(CAST("{column}" AS VARCHAR), 1, 7) = ?', [period]
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
        _relation_signature(conn, "raw_import", period),
        _relation_signature(conn, "workflow_config", period),
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
        return _flag(conn, period)
    row = conn.execute(
        "SELECT source_signature FROM workflow_readiness_meta WHERE period = ?",
        [period],
    ).fetchone()
    return not row or row[0] != _database_signature(conn, period)


def _readiness_relation(conn: duckdb.DuckDBPyConnection, table: str, period: str) -> tuple[str, list[str]]:
    """Project only gate inputs; never materialize attendance detail in Python."""
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()} if _table_exists(conn, table) else set()
    where, params = _period_clause(columns, period)
    if not where or columns == {"_empty"}:
        return "SELECT '' nip, '' date_key, 0 records WHERE false", []
    nip = next((f'"{key}"' for key in ('nip', 'NIP') if key in columns), "NULL")
    day = next((f'"{key}"' for key in ('tanggalKey', 'tanggal', 'tgl_finger') if key in columns), "NULL")
    records = 'coalesce(TRY_CAST("jumlahRecord" AS BIGINT), 0)' if 'jumlahRecord' in columns else '0'
    return (f'SELECT trim(coalesce(CAST({nip} AS VARCHAR), \'\')) nip, '
            f'trim(coalesce(CAST({day} AS VARCHAR), \'\')) date_key, '
            f'{records} records FROM "{table}"{where}', params)


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
        daily_sql, daily_params = _readiness_relation(conn, "hitung_harian", period)
        semifinal_sql, semifinal_params = _readiness_relation(conn, "anomali", period)
        rekap_sql, rekap_params = _readiness_relation(conn, "rekap_sdm", period)
        # Return one small row per employee, instead of all daily records.
        daily = conn.execute(f'SELECT nip, count(*) FROM ({daily_sql}) GROUP BY nip', daily_params).fetchall()
        semifinal = conn.execute(f'SELECT EXISTS(SELECT 1 FROM ({semifinal_sql}))', semifinal_params).fetchone()[0]
        rekap = conn.execute(
            f'SELECT nip, count(*), min(records) FROM ({rekap_sql}) GROUP BY nip', rekap_params,
        ).fetchall()
        result["checks"]["harian_available"] = bool(daily)
        result["checks"]["semifinal_available"] = bool(semifinal)
        result["checks"]["rekap_available"] = bool(rekap)

        daily_dups = conn.execute(
            f"SELECT count(*) FROM (SELECT nip, date_key FROM ({daily_sql}) "
            "WHERE nip <> '' GROUP BY nip, date_key HAVING count(*) > 1)", daily_params,
        ).fetchone()[0]
        result["checks"]["harian_duplicates"] = bool(daily_dups)
        if not daily:
            result["blocking_reasons"].append("Hasil Hitung Harian belum tersedia.")
        if daily_dups:
            result["blocking_reasons"].append(f"Ditemukan {daily_dups} duplikat NIP/tanggal pada Hitung Harian.")
        if not semifinal:
            result["blocking_reasons"].append("Hasil Semi Final belum tersedia.")

        verified = _flag(conn, period)
        result["checks"]["semifinal_verified"] = verified
        if not verified:
            result["blocking_reasons"].append("Verifikasi Semi Final belum tercatat di database.")

        rekap_dups = any(not nip or count > 1 for nip, count, _ in rekap)
        result["checks"]["rekap_duplicates"] = bool(rekap_dups)
        if not rekap:
            result["blocking_reasons"].append("Rekap SDM belum tersedia.")
        if rekap_dups:
            result["blocking_reasons"].append("Rekap SDM memiliki NIP kosong atau duplikat.")

        daily_by_nip = {nip: count for nip, count in daily if nip}
        rekap_by_nip = {nip: records for nip, _, records in rekap if nip}
        mismatches = [nip for nip in daily_by_nip.keys() | rekap_by_nip.keys()
                      if daily_by_nip.get(nip, 0) != rekap_by_nip.get(nip, 0)]
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
        conn.execute("BEGIN TRANSACTION")
        try:
            if not _table_exists(conn, "hitung_harian"):
                raise ValueError("Hasil Hitung Harian belum tersedia.")
            daily_columns = {row[1] for row in conn.execute('PRAGMA table_info("hitung_harian")').fetchall()}
            if not daily_columns or daily_columns == {"_empty"}:
                raise ValueError("Hasil Hitung Harian belum tersedia.")
            where, params = _period_clause(daily_columns, period)
            if not where:
                raise ValueError("Kolom periode pada Hitung Harian tidak tersedia.")
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
            # Keep previously approved snapshots for every other period, including
            # columns retained by older snapshots. DuckDB aligns the union by name.
            if _table_exists(conn, "anomali"):
                snapshot_columns = {row[1] for row in conn.execute('PRAGMA table_info("anomali")').fetchall()}
                if snapshot_columns != {"_empty"}:
                    snapshot_where, snapshot_params = _period_clause(snapshot_columns, period)
                    if not snapshot_where:
                        raise ValueError("Kolom periode pada Semi Final tidak tersedia.")
                    predicate = snapshot_where.removeprefix(" WHERE ")
                    if snapshot_columns == daily_columns:
                        conn.execute(f'DELETE FROM "anomali"{snapshot_where}', snapshot_params)
                        conn.execute(
                            f'INSERT INTO "anomali" BY NAME SELECT * FROM "hitung_harian"{where}', params,
                        )
                    else:
                        # Compatibility path for snapshots with an older schema.
                        conn.execute(
                            f'CREATE TEMP TABLE verified_snapshot AS '
                            f'SELECT * FROM "anomali" WHERE NOT ({predicate}) OR ({predicate}) IS NULL '
                            f'UNION ALL BY NAME SELECT * FROM "hitung_harian"{where}',
                            snapshot_params + snapshot_params + params,
                        )
                        conn.execute('CREATE OR REPLACE TABLE "anomali" AS SELECT * FROM verified_snapshot')
                else:
                    conn.execute(f'CREATE OR REPLACE TABLE "anomali" AS SELECT * FROM "hitung_harian"{where}', params)
            else:
                conn.execute(f'CREATE TABLE "anomali" AS SELECT * FROM "hitung_harian"{where}', params)
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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"status": "ok", "period": period, "verified": True, "rows": daily_count}

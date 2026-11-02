"""Safe destructive maintenance helpers for DuckDB-backed modules."""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Sequence, Tuple


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESET_LOCK = threading.Lock()


def is_invalidated_duckdb_error(exc: BaseException) -> bool:
    """Return True when a DuckDB connection must be recreated."""
    message = str(exc).lower()
    markers = (
        "database has been invalidated",
        "previous fatal error",
        "must be restarted prior to being used again",
        "connection is closed",
        "failed to delete all rows from index",
    )
    return any(marker in message for marker in markers)


def _quoted(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Nama objek database tidak valid: {identifier!r}")
    return f'"{identifier}"'


def _safe_rollback(conn) -> None:
    """Rollback without masking the original error when DuckDB is invalidated."""
    try:
        conn.rollback()
    except Exception:
        pass


def _restore_indexes(conn, indexes) -> None:
    if not indexes:
        return
    conn.execute("BEGIN")
    try:
        for _, _, create_sql in indexes:
            conn.execute(create_sql)
        conn.commit()
    except Exception:
        _safe_rollback(conn)
        raise


@contextmanager
def temporarily_drop_secondary_indexes(conn, tables: Iterable[str]):
    """Drop secondary indexes while a caller performs a transactional deletion.

    The caller must begin its transaction inside this context. DuckDB needs the
    index drops committed before DELETE, while the actual deletes can still be
    kept atomic by the caller.
    """
    requested = list(dict.fromkeys(str(table) for table in tables))
    for table in requested:
        _quoted(table)

    with _RESET_LOCK:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
        targets = [table for table in requested if table in existing]
        if not targets:
            yield
            return

        placeholders = ",".join("?" for _ in targets)
        index_rows = conn.execute(
            f"SELECT schema_name, index_name, sql FROM duckdb_indexes() "
            f"WHERE table_name IN ({placeholders}) ORDER BY index_name",
            targets,
        ).fetchall()
        dropped = []
        try:
            for index_row in index_rows:
                schema_name, index_name, _ = index_row
                conn.execute(
                    f"DROP INDEX IF EXISTS {_quoted(schema_name)}.{_quoted(index_name)}"
                )
                dropped.append(index_row)
            yield
        except BaseException as operation_error:
            _safe_rollback(conn)
            try:
                _restore_indexes(conn, dropped)
            except Exception as restore_error:
                raise RuntimeError(
                    f"Operasi reset gagal dan indeks tidak dapat dipulihkan: {restore_error}"
                ) from operation_error
            raise
        else:
            _restore_indexes(conn, dropped)


def clear_tables_without_secondary_indexes(conn, tables: Iterable[str]) -> Dict[str, Any]:
    """Empty tables while temporarily removing their secondary ART indexes.

    DuckDB 1.5 can invalidate a database instance when a large DELETE touches an
    ART index on a wide table ("Failed to delete all rows from index").  Dropping
    only the user-created secondary indexes before DELETE avoids that engine
    path.  The index drop must be committed before DELETE: DuckDB otherwise
    keeps the old ART index alive until commit and triggers the same fatal bug.
    Index definitions are restored immediately after the data transaction.
    """
    requested = list(dict.fromkeys(str(table) for table in tables))
    deleted: Dict[str, int] = {}
    with temporarily_drop_secondary_indexes(conn, requested):
        try:
            conn.execute("BEGIN")
            for table in requested:
                if conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_name=?",
                    [table],
                ).fetchone():
                    deleted[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]
                    )
                    conn.execute(f"DELETE FROM {_quoted(table)}")
            conn.commit()
        except Exception:
            _safe_rollback(conn)
            raise

        return {
            "status": "ok",
            "deleted_tables": deleted,
            "deleted_rows": sum(deleted.values()),
        }


def delete_where_without_secondary_indexes(
    conn, operations: Sequence[Tuple[str, str, Sequence[Any]]]
) -> Dict[str, Any]:
    """Delete scoped rows atomically while avoiding DuckDB ART-index invalidation.

    Table names are validated identifiers. WHERE clauses are internal SQL supplied
    by the caller and values remain parameterized. Operations execute in the given
    order so child rows can be removed before their parent/source rows.
    """
    normalized = [(str(table), str(where), list(params))
                  for table, where, params in operations]
    for table, where, _ in normalized:
        _quoted(table)
        if not where.strip():
            raise ValueError("Scoped delete wajib memiliki kondisi WHERE")
    with _RESET_LOCK:
        existing = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema()"
        ).fetchall()}
        normalized = [item for item in normalized if item[0] in existing]
        tables = list(dict.fromkeys(item[0] for item in normalized))
        if not tables:
            return {"status": "ok", "deleted_tables": {}, "deleted_rows": 0}
        placeholders = ",".join("?" for _ in tables)
        index_rows = conn.execute(
            f"SELECT schema_name,index_name,sql FROM duckdb_indexes() "
            f"WHERE table_name IN ({placeholders}) ORDER BY index_name", tables
        ).fetchall()
        deleted: Dict[str, int] = {}
        indexes_dropped = False
        try:
            for schema_name, index_name, _ in index_rows:
                conn.execute(f"DROP INDEX IF EXISTS {_quoted(schema_name)}.{_quoted(index_name)}")
            indexes_dropped = True
            conn.execute("BEGIN")
            for table, where, params in normalized:
                count = int(conn.execute(
                    f"SELECT COUNT(*) FROM {_quoted(table)} WHERE {where}", params
                ).fetchone()[0])
                conn.execute(f"DELETE FROM {_quoted(table)} WHERE {where}", params)
                deleted[table] = deleted.get(table, 0) + count
            conn.commit()
            conn.execute("BEGIN")
            for _, _, create_sql in index_rows:
                conn.execute(create_sql)
            conn.commit()
        except Exception:
            _safe_rollback(conn)
            if indexes_dropped:
                try:
                    conn.execute("BEGIN")
                    for _, _, create_sql in index_rows:
                        conn.execute(create_sql)
                    conn.commit()
                except Exception:
                    _safe_rollback(conn)
            raise
        return {"status": "ok", "deleted_tables": deleted,
                "deleted_rows": sum(deleted.values())}

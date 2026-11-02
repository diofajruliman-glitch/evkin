"""Canonical preview/confirm service for EVKIN module and operational resets."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from backend.duckdb_maintenance import (
    is_invalidated_duckdb_error,
    temporarily_drop_secondary_indexes,
)
from backend.runtime_guard import (
    begin_runtime_reset,
    finish_runtime_reset,
    initialize_runtime_state,
)


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class ResetSpec:
    name: str
    tables: tuple[str, ...]
    confirmation: str
    preserved_note: str


MODULE_PROTECTED_TABLES = frozenset({
    'master_data', 'master_sdm', 'master_unit_kerja', 'master_jabatan', 'master_tukin',
    'sdm_master', 'sdm_assignment_history', 'sdm_tukin_profile_history', 'sdm_bank_history',
    'sdm_temporary_assignment_history', 'sdm_change_request', 'sdm_active_history',
    'sdm_validation_issue', 'sdm_master_import_batch', 'skp_rule_config', 'master_skp_rules',
    'audit_log', 'application_reset_audit', 'final_tukin_schema', 'system_runtime_state',
    'skp_assessment_audit', 'skp_indicator_config', 'skp_indicator_config_audit',
    'skp_assessment_rule', 'skp_assessment_rule_audit', 'skp_non_submission_rule',
    'skp_condition_decision_audit', 'tukin_period_audit', 'tukin_admin_audit',
    'pengembalian_tukin', 'pengembalian_uang_makan', 'rekap_pengembalian', 'rekonsiliasi',
    'bnba', 'pengembalian_period', 'pengembalian_archive_dataset',
    'pengembalian_archive_dataset_row', 'pengembalian_payment_transaction',
    'pengembalian_final', 'pengembalian_obligation', 'pengembalian_progress_import_batch',
    'pengembalian_progress_transaction', 'pengembalian_sp2d_lineage',
    'pengembalian_master_obligation', 'pengembalian_period_audit',
})

_PRESERVE_MODULES = "Master SDM, konfigurasi aturan, dan audit tetap disimpan."

RESET_PROFILES = {
    'workflow': ResetSpec(
        'workflow',
        ('final_combined_skp', 'final_kombinasi', 'final_skp', 'final_absensi',
         'anomali', 'rekap_sdm', 'hitung_harian', 'workflow_records', 'raw_import',
         'workflow_config'),
        'RESET WORKFLOW', _PRESERVE_MODULES,
    ),
    'workflow_results': ResetSpec(
        'workflow_results',
        ('final_absensi', 'anomali', 'rekap_sdm', 'hitung_harian'),
        'RESET WORKFLOW RESULTS',
        "Data sumber, konfigurasi workflow, dan Master SDM tetap disimpan.",
    ),
    'skp': ResetSpec(
        'skp',
        ('skp_meeting_member', 'skp_meeting_batch', 'skp_assessment_exception',
         'skp_condition_decision', 'skp_results', 'skp_assessment_monthly',
         'skp_period_finalization', 'skp_import_source', 'skp_import_batch', 'skp_import_batches'),
        'RESET SKP', _PRESERVE_MODULES,
    ),
    'final_tukin': ResetSpec(
        'final_tukin', ('final_tukin_results', 'final_tukin_batches'),
        'RESET FINAL TUKIN', _PRESERVE_MODULES,
    ),
    'pengelolaan_tukin': ResetSpec(
        'pengelolaan_tukin',
        ('tukin_payment_event', 'tukin_condition', 'tukin_combined_adjustment',
         'tukin_attendance_daily', 'tukin_final_monthly', 'tukin_skp_monthly',
         'tukin_import_batch', 'tukin_period'),
        'RESET PENGELOLAAN TUKIN', _PRESERVE_MODULES,
    ),
}

OPERATIONAL_CONFIRMATION = 'RESET TOTAL TANPA MASTER SDM'
OPERATIONAL_PRESERVED = {
    'master_data', 'master_sdm', 'master_unit_kerja', 'master_jabatan',
    'master_skp_rules', 'final_tukin_schema', 'master_tukin', 'skp_rule_config',
    'audit_log', 'application_reset_audit', 'system_runtime_state',
}

# Operational reset uses an explicit, auditable precedence.  Prefix matching is
# deliberately last: audit/history tables often share an operational prefix
# (for example ``skp_assessment_audit``) but are evidence, not working data.
PRESERVE_MASTER = frozenset({
    'master_data', 'master_sdm', 'master_unit_kerja', 'master_jabatan',
    'master_tukin', 'master_skp_rules', 'skp_rule_config', 'final_tukin_schema',
    'skp_indicator_config', 'skp_assessment_rule', 'skp_non_submission_rule',
    'pengembalian_master_obligation',
})
PRESERVE_AUDIT = frozenset({
    'audit_log', 'application_reset_audit', 'skp_assessment_audit',
    'skp_indicator_config_audit', 'skp_condition_decision_audit',
    'skp_assessment_rule_audit',
    'tukin_period_audit', 'tukin_admin_audit', 'pengembalian_period_audit',
})
OPERATIONAL_TABLES = {
    'raw_import', 'hitung_harian', 'rekap_sdm', 'anomali', 'bnba',
    'rekonsiliasi', 'import_batch', 'processing_jobs', 'rounding_jobs',
    'rekap_pengembalian',
}
OPERATIONAL_PREFIXES = (
    'workflow_', 'absensi_', 'final_', 'skp_', 'tukin_', 'pengembalian_',
    'generic_compare_',
)


class ResetService:
    def __init__(self, db_path, jobs_path=None):
        self.db_path = Path(db_path).resolve()
        self.jobs_path = Path(jobs_path).resolve() if jobs_path else None

    @staticmethod
    def _existing(conn) -> set[str]:
        return {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='BASE TABLE'"
        ).fetchall()}

    @staticmethod
    def _digest(conn, schema: str, table: str) -> tuple[int, str]:
        target = f'{quote(schema)}.{quote(table)}'
        count, xor_hash, sum_hash = conn.execute(
            f"SELECT count(*),coalesce(bit_xor(hash(t)),0)::VARCHAR,"
            f"coalesce(sum(hash(t)::HUGEINT),0)::VARCHAR FROM {target} t"
        ).fetchone()
        return int(count), f'{xor_hash}:{sum_hash}'

    @staticmethod
    def _token(plan: dict) -> str:
        stable = {
            'profile': plan['profile'],
            # Runtime status/generation necessarily changes when the reset
            # fence is entered and is protected from deletion. It must not
            # invalidate the user's already-verified destructive plan.
            'tables': [item for item in plan['tables']
                       if item.get('table') != 'system_runtime_state'],
            'blockers': plan.get('blockers', []),
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _operational_category(name: str) -> str:
        if (name in PRESERVE_AUDIT or name.endswith('_audit')
                or name.startswith('audit_')):
            return 'PRESERVE_AUDIT'
        if (name in PRESERVE_MASTER or name in OPERATIONAL_PRESERVED
                or name.startswith('sdm_') or name.startswith('schema_migration')):
            return 'PRESERVE_MASTER'
        if name in OPERATIONAL_TABLES or name.startswith(OPERATIONAL_PREFIXES):
            return 'DELETE_OPERATIONAL'
        return 'UNKNOWN'

    def _fixed_plan(self, conn, profile: str) -> dict:
        spec = RESET_PROFILES.get(profile)
        if spec is None:
            raise ValueError(f'Modul reset tidak dikenal: {profile}')
        protected = set(spec.tables) & MODULE_PROTECTED_TABLES
        if protected:
            raise ValueError(f'Reset dibatalkan; tabel protected terpetakan: {sorted(protected)}')
        existing = self._existing(conn)
        tables = []
        for table in spec.tables:
            count, digest = self._digest(conn, 'main', table) if table in existing else (0, '0:0')
            tables.append({
                'schema': 'main', 'table': table, 'rows': count, 'digest': digest,
                'action': 'DELETE', 'exists': table in existing,
            })
        plan = {
            'status': 'ok', 'profile': profile, 'module': profile,
            'confirmation': spec.confirmation, 'target_tables': list(spec.tables),
            'tables': tables, 'deleted_rows': sum(item['rows'] for item in tables),
            'blockers': [], 'can_reset': True,
            'protected_tables': sorted(MODULE_PROTECTED_TABLES),
            'preserved_note': spec.preserved_note,
        }
        plan['preview_token'] = self._token(plan)
        return plan

    def _operational_inventory(self, conn) -> list[dict]:
        result = []
        for schema, name in conn.execute(
            """SELECT table_schema,table_name FROM information_schema.tables
               WHERE table_catalog=current_database() AND table_type='BASE TABLE'
               ORDER BY table_schema,table_name"""
        ).fetchall():
            count, digest = self._digest(conn, schema, name)
            category = self._operational_category(name) if schema == 'main' else 'UNKNOWN'
            action = ('KEEP' if category.startswith('PRESERVE_')
                      else 'DELETE' if category == 'DELETE_OPERATIONAL'
                      else 'UNKNOWN')
            result.append({'schema': schema, 'table': name, 'rows': count,
                           'digest': digest, 'action': action, 'category': category,
                           'exists': True})
        return result

    def _operational_blockers(self, conn, inventory: list[dict]) -> list[str]:
        blockers = [f"Tabel belum dipetakan: {r['schema']}.{r['table']}"
                    for r in inventory if r['action'] == 'UNKNOWN' and r['rows']]
        for item in inventory:
            if item['action'] != 'DELETE' or not item['rows']:
                continue
            name = item['table']
            columns = {row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=? AND table_name=?", [item['schema'], name]
            ).fetchall()}
            for column in ('status', 'payment_status'):
                if column in columns and (name.startswith(('tukin_', 'pengembalian_'))
                                          or name == 'master_tukin'):
                    locked = conn.execute(
                        f"SELECT count(*) FROM {quote(name)} WHERE "
                        f"upper(trim({quote(column)}::VARCHAR)) IN "
                        "('LOCKED','HISTORICAL_LOCKED','CLOSED','ARCHIVED','PAID',"
                        "'TERKUNCI','SUDAH DIBAYAR','DIBAYARKAN')"
                    ).fetchone()[0]
                    if locked:
                        blockers.append(f'{name}: {locked} data terkunci/sudah dibayar')
        if self.jobs_path and self.jobs_path.exists():
            with sqlite3.connect(str(self.jobs_path)) as jobs:
                if jobs.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone():
                    active = jobs.execute(
                        "SELECT count(*) FROM jobs WHERE lower(status) IN "
                        "('queued','pending','running')"
                    ).fetchone()[0]
                    if active:
                        blockers.append(f'{active} proses backend masih aktif')
        return blockers

    def _operational_plan(self, conn) -> dict:
        inventory = self._operational_inventory(conn)
        blockers = self._operational_blockers(conn, inventory)
        plan = {
            'status': 'ok', 'profile': 'operational', 'module': 'operational',
            'confirmation': OPERATIONAL_CONFIRMATION,
            'target_tables': [item['table'] for item in inventory if item['action'] == 'DELETE'],
            'tables': inventory,
            'deleted_rows': sum(item['rows'] for item in inventory if item['action'] == 'DELETE'),
            'blockers': blockers, 'can_reset': not blockers,
            'protected_tables': [item['table'] for item in inventory if item['action'] == 'KEEP'],
            'preserved_note': ('Master SDM dan riwayat, aturan potongan SKP, audit sistem, '
                               'riwayat job, backup dan file ekspor tetap disimpan.'),
        }
        plan['preview_token'] = self._token(plan)
        return plan

    def _plan(self, conn, profile: str) -> dict:
        return self._operational_plan(conn) if profile == 'operational' else self._fixed_plan(conn, profile)

    def preview(self, profile: str) -> dict:
        if not self.db_path.is_file():
            raise ValueError('Database aplikasi tidak ditemukan')
        initialize_runtime_state(self.db_path)
        with duckdb.connect(str(self.db_path)) as conn:
            return self._plan(conn, profile)

    def _backup(self, conn, plan: dict, reset_id: str) -> Path:
        folder = self.db_path.parent / 'reset-backups' / reset_id
        folder.mkdir(parents=True, exist_ok=False)
        target = folder / self.db_path.name
        source_name = conn.execute('SELECT current_database()').fetchone()[0]
        conn.execute("ATTACH '" + str(target).replace("'", "''") + "' AS reset_backup")
        try:
            conn.execute(f'COPY FROM DATABASE {quote(source_name)} TO reset_backup')
        finally:
            conn.execute('DETACH reset_backup')
        with duckdb.connect(str(target), read_only=True) as backup:
            if self._operational_inventory(backup) != plan['tables']:
                raise ValueError('Verifikasi backup gagal; reset dibatalkan')
        if self.jobs_path and self.jobs_path.exists():
            with sqlite3.connect(str(self.jobs_path)) as source, sqlite3.connect(str(folder / 'jobs.sqlite')) as dest:
                source.backup(dest)
        return folder

    def _delete_table(self, conn, item: dict) -> None:
        conn.execute(f"DELETE FROM {quote(item['schema'])}.{quote(item['table'])}")

    @staticmethod
    def _snapshot(conn, tables: Iterable[str]) -> dict[str, tuple[int, str, str]]:
        existing = ResetService._existing(conn)
        snapshots = {}
        for table in tables:
            if table not in existing:
                continue
            columns = conn.execute(
                "SELECT column_name,data_type,is_nullable,column_default "
                "FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            schema_digest = hashlib.sha256(
                json.dumps(columns, default=str, separators=(',', ':')).encode()
            ).hexdigest()
            count, row_digest = ResetService._digest(conn, 'main', table)
            snapshots[table] = (count, row_digest, schema_digest)
        return snapshots

    @staticmethod
    def _write_audit(conn, reset_id: str, actor: str, reason: str,
                     backup_path: str, audit_payload: dict) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS application_reset_audit (
            id VARCHAR PRIMARY KEY, created_at TIMESTAMP DEFAULT now(),
            actor VARCHAR, reason VARCHAR, backup_path VARCHAR, manifest_json VARCHAR)""")
        conn.execute(
            "INSERT INTO application_reset_audit"
            "(id,actor,reason,backup_path,manifest_json) VALUES (?,?,?,?,?)",
            [reset_id, actor, reason, backup_path, json.dumps(audit_payload, sort_keys=True)],
        )

    def execute(self, profile: str, payload: dict, *, source: str = 'API') -> dict:
        """Run every destructive reset behind the same persistent generation fence.

        A failed/rolled-back reset still advances generation. This deliberately
        invalidates clients that overlapped the attempt, while READY is restored
        in ``finally`` so normal writes cannot remain permanently blocked.
        """
        payload = payload or {}
        initialize_runtime_state(self.db_path)
        with duckdb.connect(str(self.db_path)) as conn:
            plan = self._plan(conn, profile)
            actor = str(payload.get('actor') or '').strip()
            reason = str(payload.get('reason') or '').strip()
            if profile == 'operational' and (not actor or not reason):
                raise ValueError('Frasa konfirmasi, nama petugas, dan alasan reset wajib diisi')
            if payload.get('confirmation') != plan['confirmation']:
                raise ValueError('Konfirmasi reset tidak sesuai')
            if payload.get('preview_token') != plan['preview_token']:
                raise ValueError('Data berubah atau preview belum dilakukan. Preview ulang sebelum reset')
            if plan['blockers']:
                raise ValueError('; '.join(plan['blockers']))

        generation = begin_runtime_reset(self.db_path)
        try:
            result = self._execute_fenced(profile, payload, source=source)
            result['reset_generation'] = generation
            return result
        finally:
            finish_runtime_reset(self.db_path)

    def _execute_fenced(self, profile: str, payload: dict, *, source: str = 'API') -> dict:
        payload = payload or {}
        actor = str(payload.get('actor') or '').strip()
        reason = str(payload.get('reason') or '').strip()
        if profile == 'operational' and (not actor or not reason):
            raise ValueError('Frasa konfirmasi, nama petugas, dan alasan reset wajib diisi')
        actor = actor or source
        reason = reason or f'Reset profile {profile}'
        with duckdb.connect(str(self.db_path)) as conn:
            plan = self._plan(conn, profile)
            if payload.get('confirmation') != plan['confirmation']:
                raise ValueError('Konfirmasi reset tidak sesuai')
            if payload.get('preview_token') != plan['preview_token']:
                raise ValueError('Data berubah atau preview belum dilakukan. Preview ulang sebelum reset')
            if plan['blockers']:
                raise ValueError('; '.join(plan['blockers']))

            reset_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex
            backup_path = ''
            if profile == 'operational':
                folder = self._backup(conn, plan, reset_id)
                backup_path = str(folder)
                manifest = {**plan, 'reset_id': reset_id, 'actor': actor, 'reason': reason,
                            'backup_path': backup_path, 'state': 'BACKUP_VERIFIED'}
                (folder / 'manifest.json').write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
                )

            # Runtime generation/status and the reset audit are expected to
            # change. Every other protected table must retain both its complete
            # row multiset and schema through the destructive transaction.
            dynamic_protected = {'application_reset_audit', 'system_runtime_state'}
            protected_tables = (
                {item['table'] for item in plan['tables'] if item['action'] == 'KEEP'}
                if profile == 'operational'
                else set(MODULE_PROTECTED_TABLES)
            ) - dynamic_protected
            protected_before = self._snapshot(conn, protected_tables)
            target_tables = [
                item['table'] for item in plan['tables']
                if item['action'] == 'DELETE' and item.get('exists', True)
            ]
            with temporarily_drop_secondary_indexes(conn, target_tables):
                conn.execute('BEGIN TRANSACTION')
                try:
                    current = self._plan(conn, profile)
                    if current['preview_token'] != plan['preview_token']:
                        raise ValueError('Data berubah setelah preview; reset dibatalkan')
                    deleted = {}
                    for item in plan['tables']:
                        if item['action'] == 'DELETE' and item.get('exists', True):
                            self._delete_table(conn, item)
                            deleted[item['table']] = item['rows']
                        elif item['action'] == 'DELETE':
                            deleted[item['table']] = 0
                    audit_payload = {
                        'profile': profile, 'source': source, 'deleted': deleted,
                        'deleted_rows': sum(deleted.values()), 'target_tables': plan['target_tables'],
                    }
                    self._write_audit(conn, reset_id, actor, reason, backup_path, audit_payload)

                    for item in plan['tables']:
                        if item['action'] == 'DELETE' and item.get('exists', True):
                            if conn.execute(
                                f"SELECT count(*) FROM {quote(item['schema'])}.{quote(item['table'])}"
                            ).fetchone()[0]:
                                raise ValueError(f"Verifikasi penghapusan gagal: {item['table']}")
                    if self._snapshot(conn, protected_before) != protected_before:
                        raise ValueError('Verifikasi pelestarian Master/audit gagal')
                    conn.execute('COMMIT')
                except Exception as exc:
                    try:
                        conn.execute('ROLLBACK')
                    except duckdb.Error as rollback_error:
                        if not is_invalidated_duckdb_error(rollback_error):
                            raise rollback_error from exc
                    raise
        return {
            'status': 'ok', 'reset_id': reset_id, 'profile': profile, 'module': profile,
            'deleted': deleted, 'deleted_rows': sum(deleted.values()),
            'target_tables': plan['target_tables'], 'protected_tables': plan['protected_tables'],
            'preserved_note': plan['preserved_note'], 'backup_path': backup_path or None,
        }

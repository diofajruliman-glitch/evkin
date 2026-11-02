"""Period-keyed review of canonical SDM, approved SKP and attendance.

This projection never modifies approved assessments or historical payments.
Both the paginated screen and full Excel export use the same calculation.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import hashlib
import importlib
import json
import re
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
import duckdb
from backend.lazy_imports import lazy_module

pd = lazy_module("pandas")

from backend.shared_master import SharedMasterService
from backend.skp_assessment import (
    VALID_PREDICATES as PREDICATE_RATES,
    annotate_skp_tukin_provenance,
    resolve_skp_for_tukin,
)


ensure_integration_schema = importlib.import_module(
    "backend.migrations.048_create_canonical_tukin_integration"
).ensure_canonical_tukin_integration_schema
CALCULATION_VERSION = "TUKIN-70-30-V1"
VALID_SCOPES = {'workflow', 'attendance', 'all'}


def number(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def money(value):
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def calculate_tukin_amounts(gross, attendance_rate, skp_rate):
    """Single canonical 30% attendance + 70% SKP calculation path."""
    gross_value = number(gross)
    attendance_value = number(attendance_rate)
    skp_value = number(skp_rate)
    if (gross_value is None or attendance_value is None or skp_value is None
            or gross_value < 0 or not 0 <= attendance_value <= 100
            or not 0 <= skp_value <= 100):
        return None
    gross_rupiah = money(gross_value)
    attendance_component = money(Decimal(gross_rupiah) * Decimal('.30'))
    # Complementary components preserve the integer-Rupiah reconciliation
    # invariant while retaining the existing 30/70 business formula.
    skp_component = gross_rupiah - attendance_component
    attendance_deduction = money(Decimal(attendance_component) * attendance_value / 100)
    skp_deduction = money(Decimal(skp_component) * skp_value / 100)
    total_deduction = attendance_deduction + skp_deduction
    return {
        'tukin_gross': gross_rupiah,
        'attendance_component': attendance_component,
        'skp_component': skp_component,
        'attendance_deduction': attendance_deduction,
        'skp_deduction': skp_deduction,
        'total_deduction': total_deduction,
        'calculated_entitlement': max(0, gross_rupiah - total_deduction),
    }


def keyed(rows):
    counts = Counter(str(row.get('nip') or '').strip() for row in rows)
    return ({str(row.get('nip') or '').strip(): row for row in rows},
            {nip for nip, count in counts.items() if count > 1})


def resolve_gross(person, *sources):
    """Resolve the gross Tukin while keeping Master SDM as the preferred source."""
    master_source = person.get('_profile_source') if person else None
    candidates = [
        (master_source or 'MASTER SDM', person.get('tukin_gross') if person else None),
    ]
    for source_name, source in sources:
        if source:
            candidates.append((source_name, source.get('tukin_gross')))
            candidates.append((source_name, source.get('tukin')))
            candidates.append((source_name, source.get('tukinBruto')))
    valid = []
    for source_name, value in candidates:
        parsed = number(value)
        if parsed is not None and parsed >= 0:
            valid.append((parsed, source_name))
            if parsed > 0:
                return parsed, source_name
    if valid:
        return valid[0]
    return None, ''


def combine(master, attendance, assessments, history, period, conditions=()):
    attendance_map, attendance_duplicates = keyed(attendance)
    assessment_map, assessment_duplicates = keyed(assessments)
    history_map, history_duplicates = keyed(history)
    conditions_by_nip = {}
    for condition in conditions:
        conditions_by_nip.setdefault(str(condition.get('nip') or '').strip(), []).append(condition)
    output = []
    # Keep unmatched source NIPs visible as well as every canonical SDM.
    for nip in sorted(set(master) | set(attendance_map) | set(assessment_map) | set(history_map)):
        if not nip:
            continue
        person = master.get(nip, {})
        absence = attendance_map.get(nip)
        skp = assessment_map.get(nip)
        previous_skp = history_map.get(nip)
        issues = []
        gross, gross_source = resolve_gross(person, ('FINAL/HISTORI', absence), ('SKP', skp), ('HISTORI SKP', previous_skp))
        if not person:
            issues.append('NIP tidak ditemukan di Master SDM periode ini')
        if gross is None or gross < 0:
            gross = None
            issues.append('Tukin bruto belum valid')
        abs_rate = number(absence.get('potPct')) if absence else None
        if nip in attendance_duplicates:
            issues.append('NIP absensi duplikat'); abs_rate = None
        if abs_rate is None or not 0 <= abs_rate <= 100:
            abs_rate = None
            issues.append('Final Absensi belum tersedia/valid')
        predicate = ''
        skp_rate = None
        report_status = ''
        readiness_input = dict(skp or {})
        if (skp and str(skp.get('report_status') or '') != 'SUBMITTED'
                and previous_skp and readiness_input.get('final_reduction_rate') is None):
            readiness_input['final_reduction_rate'] = previous_skp.get('skp_reduction_rate')
        skp_readiness = resolve_skp_for_tukin(
            readiness_input if skp else None,
            bool((skp or {}).get('_direct_final_import')),
            attendance_participant=bool(absence),
        )
        if nip in assessment_duplicates or nip in history_duplicates:
            issues.append('NIP penilaian SKP duplikat')
        elif skp and skp_readiness['ready']:
            predicate = str(skp.get('final_predicate') or '').strip().upper()
            report_status = str(skp.get('report_status') or '')
            skp_rate = number(skp.get('final_reduction_rate'))
            if skp_rate is None:
                skp_rate = number(PREDICATE_RATES.get(predicate))
            # Verified non-submission/exception rules are calculated by the SKP
            # service, not inferred from a blank or '-' in an imported file.
            if report_status != 'SUBMITTED':
                skp_rate = number(previous_skp.get('skp_reduction_rate')) if previous_skp else None
        elif skp_readiness['ready_mode'] == 'FALLBACK':
            predicate = 'SKP TIDAK TERSEDIA'
            report_status = 'MISSING_FROM_FINAL_IMPORT'
            skp_rate = number(skp_readiness['fallback_rate'])
        else:
            issues.append(skp_readiness['reason'])
        if skp_rate is None or not 0 <= skp_rate <= 100:
            skp_rate = None
            if skp_readiness['ready_mode'] == 'NORMAL':
                issues.append('Predikat/aturan potongan SKP belum valid')
        amounts = calculate_tukin_amounts(gross, abs_rate, skp_rate)
        abs_deduction = amounts['attendance_deduction'] if amounts else None
        skp_deduction = amounts['skp_deduction'] if amounts else None
        total = amounts['total_deduction'] if amounts else None
        eligible = person.get('eligible') is True
        unpaid = person.get('eligible') is False
        held = False
        skp_blocked = False
        if not eligible:
            issues.append(str(person.get('eligibility_reason') or (
                'Master SDM: tidak berhak Tukin' if person.get('eligible') is False
                else 'Kelayakan Master SDM periode ini belum tersedia')))
        if previous_skp and previous_skp.get('payment_eligible') is False:
            eligible = False
            skp_blocked = True
            issues.append('SKP: pembayaran ditahan/tidak memenuhi syarat')
        for condition in conditions_by_nip.get(nip, []):
            if (str(condition.get('nip') or '').strip() == nip
                    and condition.get('status') == 'APPROVED'
                    and condition.get('payment_effect') in {'HOLD','UNPAID'}):
                eligible = False
                unpaid = unpaid or condition.get('payment_effect') == 'UNPAID'
                held = held or condition.get('payment_effect') == 'HOLD'
                issues.append('Keputusan kondisi: ' + str(condition.get('payment_effect')) + ' — ' + str(condition.get('reason') or ''))
        complete = gross is not None and total is not None and not attendance_duplicates.intersection({nip}) and not assessment_duplicates.intersection({nip}) and not history_duplicates.intersection({nip})
        output.append({
            'assessment_period':period, 'nip':nip,
            'nama':person.get('nama') or (skp or absence or {}).get('nama') or '',
            'jabatan':person.get('jabatan') or '', 'unit':person.get('unit') or '',
            'tukin_gross':amounts['tukin_gross'] if amounts else money(gross) if gross is not None else None,
            'tukin_gross_source': gross_source,
            'skp_component':amounts['skp_component'] if amounts else money(gross * Decimal('.70')) if gross is not None else None,
            'attendance_component':amounts['attendance_component'] if amounts else money(gross * Decimal('.30')) if gross is not None else None,
            'predicate_skp':predicate.title(), 'report_status':report_status,
            'skp_ready_for_tukin': skp_readiness['ready_mode'] == 'NORMAL',
            'skp_calculation_ready': bool(skp_readiness['ready']),
            'skp_source_type': skp_readiness['source_type'],
            'skp_source_status': skp_readiness['source_status'],
            'skp_calculation_mode': skp_readiness['ready_mode'],
            'skp_fallback_policy': skp_readiness['fallback_policy'],
            'skp_description': skp_readiness['description'],
            'skp_source_label': (
                'Import Hasil Final' if skp_readiness['source_status'] == 'FINAL_IMPORTED'
                else 'Internal' if skp_readiness['source_status'] in {'INTERNAL_APPROVED','INTERNAL_NOT_FINAL'}
                else 'Tidak Tersedia' if skp_readiness['source_status'] == 'MISSING_FROM_FINAL_IMPORT'
                else 'Import Hasil Final Bermasalah' if skp_readiness['source_status'] == 'INVALID_FINAL_IMPORT'
                else 'Belum Tersedia'
            ),
            'skp_readiness_reason': skp_readiness['reason'],
            'skp_readiness_reason_code': skp_readiness['reason_code'],
            'skp_rate':float(skp_rate) if skp_rate is not None else None,
            'attendance_rate':float(abs_rate) if abs_rate is not None else None,
            'skp_deduction':skp_deduction, 'attendance_deduction':abs_deduction,
            'total_deduction':total,
            'calculated_entitlement':amounts['calculated_entitlement'] if complete else None,
            'payable_estimate':amounts['calculated_entitlement'] if complete and eligible else None,
            'status':'LENGKAP' if complete and eligible else 'PERLU REVIEW',
            'data_complete':bool(complete), 'payment_block':'HOLD' if held else 'UNPAID' if unpaid else 'HOLD' if skp_blocked else '',
            'master_available': bool(person), 'eligible_tukin': bool(eligible),
            'duplicate_nip': bool(attendance_duplicates.intersection({nip})
                                  or assessment_duplicates.intersection({nip})
                                  or history_duplicates.intersection({nip})),
            'wilayah_3t':'3T' if person.get('wilayah_3t') is True else 'NON_3T' if person else '',
            'status_tendik':person.get('status_tendik') or '',
            'issues':'; '.join(issues), 'master_version':person.get('master_version'),
        })
    return output


def build(db_path, period, canonical=False):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        raise ValueError('Periode penilaian harus YYYY-MM')
    master = SharedMasterService(db_path).snapshot_map(period, fallback_latest_profile=True)
    with duckdb.connect(db_path) as conn:
        # The gate is a no-op while JS_LEGACY/PYTHON_SHADOW is configured.
        # Once PYTHON_CANONICAL is explicitly activated, Tukin may only consume
        # a COMPLETED canonical Absence batch for this period.
        from backend.absence_cutover import assert_canonical_absence_ready
        assert_canonical_absence_ready(conn, period)

        def read(table, period_column=None):
            exists = conn.execute('SELECT count(*) FROM information_schema.tables WHERE table_name=?', [table]).fetchone()[0]
            if not exists:
                return []
            columns = {row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()}
            if period_column and period_column not in columns:
                return []
            query = f'SELECT * FROM "{table}"'
            params = []
            if period_column:
                query += f' WHERE "{period_column}"=?'; params = [period]
            frame = conn.execute(query, params).fetchdf()
            return frame.astype(object).where(pd.notnull(frame), None).to_dict('records')
        raw_final = read('final_absensi')
        archived = read('tukin_final_monthly', 'assessment_period')
        assessments = annotate_skp_tukin_provenance(
            conn, period, read('skp_assessment_monthly', 'assessment_period')
        )
        history = read('tukin_skp_monthly', 'assessment_period')
        conditions = read('skp_condition_decision', 'assessment_period')
        adjustments = read('tukin_combined_adjustment', 'assessment_period')
        periods = read('tukin_period', 'assessment_period')
        attendance = [{
            **row,
            'assessment_period': period,
            'workflow_status': 'FINAL',
            'attendance_reduction_rate': row.get('potPct'),
            'source_calculation_id': row.get('calculation_id') or row.get('id'),
            'source_batch_id': row.get('import_batch_id') or row.get('batch_id'),
            'finalized_at': row.get('finalized_at') or row.get('updated_at'),
            '_source':'Final Absensi Workflow',
        } for row in raw_final if str(row.get('bulan') or row.get('monthKey') or '') == period]
        # Keep prior imported/saved monthly results for NIPs outside this
        # flexible workflow batch. Current matching-period rows take priority.
        live_nips = {str(row.get('nip') or '').strip() for row in attendance}
        if not canonical:
            attendance += [{**row, 'potPct':row.get('attendance_reduction_rate'), '_source':'Histori Absensi Bulanan'} for row in archived
                           if row.get('assessment_period') == period and str(row.get('nip') or '').strip() not in live_nips]
    rows = combine(master, attendance, assessments, history, period, conditions)
    source_map, _ = keyed(attendance)
    adjustment_map, _ = keyed(adjustments)
    archived_map, _ = keyed(archived)
    assessment_sources = {}
    condition_sources = {}
    for item in assessments:
        assessment_sources.setdefault(str(item.get('nip') or '').strip(), []).append(item)
    for item in conditions:
        condition_sources.setdefault(str(item.get('nip') or '').strip(), []).append(item)
    for row in rows:
        source = source_map.get(row['nip'])
        row['attendance_participant'] = source is not None
        row['workflow_participant'] = row['nip'] in live_nips
        row['attendance_source'] = source.get('_source', '') if source else ''
        row['attendance_source_id'] = str((source or {}).get('source_calculation_id') or fingerprint([period, source]) if source else '')
        row['attendance_batch_id'] = str((source or {}).get('source_batch_id') or '')
        assessment = (assessment_sources.get(row['nip']) or [{}])[0]
        row['skp_source_id'] = (f"{period}:{row['nip']}" if assessment else '')
        row['skp_batch_id'] = str(assessment.get('import_batch_id') or '')
        row['skp_source_version'] = str(assessment.get('rule_version') or assessment.get('indicator_config_snapshot') or '')
        row['skp_workflow_status'] = str(assessment.get('review_status') or '')
        row['skp_approved_at'] = assessment.get('approved_at')
        # Include approved source records so a changed decision requires review,
        # even when it happens to produce the same monetary result.
        row['source_fingerprint'] = fingerprint([row, source,
            sorted(assessment_sources.get(row['nip'], []), key=lambda r: fingerprint([r])),
            sorted(condition_sources.get(row['nip'], []), key=lambda r: fingerprint([r]))])
        apply_adjustment(row, adjustment_map.get(row['nip']))
        row['period_status'] = periods[0].get('status') if periods else 'DRAFT'
        row['period_locked'] = row['period_status'] in {'LOCKED','HISTORICAL_LOCKED','PAID'} or (archived_map.get(row['nip']) or {}).get('payment_status') in {'PAID','LOCKED','HISTORICAL_LOCKED'}
    return rows


def integration_readiness(db_path, period, scope='workflow'):
    """Return scope-consistent readiness without mutating source workflows."""
    rows = scoped_rows(db_path, period, scope)
    master_universe = SharedMasterService(db_path).snapshot_map(period, fallback_latest_profile=True)
    attendance_nips = {row['nip'] for row in rows if row.get('attendance_participant')}
    skp_ready_nips = {
        row['nip'] for row in rows
        if row.get('skp_source_id') and row.get('skp_ready_for_tukin') and row.get('skp_rate') is not None
    }
    fallback_rows = [row for row in rows if row.get('skp_source_status') == 'MISSING_FROM_FINAL_IMPORT']
    ready = [row for row in rows if row.get('status') == 'LENGKAP' and row.get('attendance_participant')]
    blockers = [
        {'nip': row['nip'], 'nama': row.get('nama') or '', 'issues': row.get('issues') or 'Data sumber belum lengkap'}
        for row in rows if row not in ready
    ]
    current_fingerprint = fingerprint(rows)
    latest_calculation = None
    final_status = 'DRAFT'
    with duckdb.connect(db_path) as conn:
        tables = {item[0] for item in conn.execute('SHOW TABLES').fetchall()}
        if 'tukin_calculation_batch' in tables:
            candidates = conn.execute(
                """SELECT id,version,status,source_fingerprint,calculated_at
                   FROM tukin_calculation_batch WHERE assessment_period=?
                   ORDER BY version DESC""", [period]
            ).fetchall()
            found = next((item for item in candidates if calculation_scope(conn, item[0]) == scope), None)
            if found:
                latest_calculation = {
                    'calculation_id': found[0], 'version': found[1],
                    'status': 'STALE' if found[3] != current_fingerprint else found[2],
                    'calculated_at': str(found[4]),
                    'scope': scope,
                }
        if 'tukin_final_decision' in tables:
            found = conn.execute(
                """SELECT status,calculation_id,calculation_version
                   FROM tukin_final_decision WHERE assessment_period=?
                   ORDER BY created_at DESC LIMIT 1""", [period]
            ).fetchone()
            if found:
                final_status = found[0]
    blocker_counts = _blocker_counts(rows)
    blocked_count = len(rows) - len(ready)
    can_calculate = bool(ready)
    calculation_current = bool(latest_calculation and latest_calculation['status'] == 'CALCULATED')
    can_finalize = bool(
        calculation_current and not blocked_count and ready
        and final_status not in {'FINAL', 'PAID'}
    )
    finalize_blockers = []
    if not latest_calculation:
        finalize_blockers.append('Belum ada calculation snapshot untuk cakupan ini')
    elif latest_calculation['status'] == 'STALE':
        finalize_blockers.append('Snapshot berubah setelah perhitungan; lakukan Hitung Ulang Tukin')
    elif latest_calculation['status'] != 'CALCULATED':
        finalize_blockers.append('Calculation snapshot belum berstatus CALCULATED')
    if blocked_count:
        finalize_blockers.append(f'{blocked_count} peserta masih memiliki mismatch/blocker')
    if not ready:
        finalize_blockers.append('Belum ada hasil yang siap difinalisasi')
    if final_status == 'FINAL':
        finalize_blockers.append('Periode sudah memiliki Final Tukin')
    elif final_status == 'PAID':
        finalize_blockers.append('Periode sudah berstatus PAID')
    return {
        'assessment_period': period,
        'period': period,
        'scope': scope,
        'target_count': len(rows),
        'complete_count': len(ready),
        'blocked_count': blocked_count,
        'can_calculate': can_calculate,
        'can_finalize': can_finalize,
        'blocker_counts': blocker_counts,
        'finalize_blockers': finalize_blockers,
        'master_universe': len(master_universe),
        'master_sdm': sum(bool(row.get('master_available')) for row in rows),
        'attendance_final': len(attendance_nips),
        'skp_ready': len(skp_ready_nips),
        'skp_normal_ready': len(skp_ready_nips),
        'skp_missing_fallback': len(fallback_rows),
        # Storage/API compatibility: this column now means canonical final SKP,
        # not that every source underwent internal manual approval.
        'skp_approved': len(skp_ready_nips),
        'mismatch': blocked_count,
        'ready': len(ready),
        'warning_count': len(fallback_rows),
        'warnings': {'skp_missing_fallback': len(fallback_rows)},
        'warning_rows': [
            {'nip': row['nip'], 'nama': row.get('nama') or '',
             'source_status': row.get('skp_source_status'),
             'calculation_mode': row.get('skp_calculation_mode'),
             'fallback_policy': row.get('skp_fallback_policy'),
             'description': row.get('skp_description')}
            for row in fallback_rows[:500]
        ],
        'tukin_gross_ready': sum(row.get('tukin_gross') is not None for row in rows),
        # Compatibility alias for callers that still require an all-ready set.
        'source_ready': bool(rows) and not blockers,
        'checks': {
            'final_absensi': bool(rows) and len(attendance_nips) == len(rows),
            'final_skp': bool(rows) and len(skp_ready_nips) + len(fallback_rows) == len(rows),
            'skp_resolved': bool(rows) and all(row.get('skp_calculation_ready') for row in rows),
            'master_sdm': bool(rows) and all(row.get('master_available') for row in rows),
            'tukin_bruto': bool(rows) and all(row.get('tukin_gross') is not None for row in rows),
        },
        'blockers': blockers[:500],
        'source_fingerprint': current_fingerprint,
        'calculation': latest_calculation,
        'final_status': final_status,
    }


def create_calculation_snapshot(db_path, period, actor, expected='', scope='workflow'):
    """Persist an immutable, versioned snapshot from the canonical preview."""
    actor = str(actor or '').strip()
    if not actor:
        raise ValueError('Petugas perhitungan wajib diisi')
    rows = scoped_rows(db_path, period, scope)
    source_fingerprint = fingerprint(rows)
    if expected and expected != source_fingerprint:
        raise ValueError('Sumber berubah; validasi ulang sebelum menghitung')
    ready = [row for row in rows if row.get('status') == 'LENGKAP' and row.get('attendance_participant')]
    if not ready:
        raise ValueError('Belum ada data dengan Master SDM, Absensi Final, dan SKP final/fallback yang lengkap')
    readiness = integration_readiness(db_path, period, scope)
    calculation_id = str(uuid.uuid4())
    calculated_at = datetime.now()
    with duckdb.connect(db_path) as conn:
        ensure_integration_schema(conn)
        conn.execute('BEGIN TRANSACTION')
        try:
            latest = conn.execute(
                """SELECT id,coalesce(version,0) FROM tukin_calculation_batch
                   WHERE assessment_period=? ORDER BY version DESC LIMIT 1""", [period]
            ).fetchone()
            version = int(latest[1]) + 1 if latest else 1
            conn.execute(
                """INSERT INTO tukin_calculation_batch
                   (id,assessment_period,version,calculation_version,status,source_fingerprint,
                    master_count,attendance_final_count,skp_approved_count,mismatch_count,ready_count,
                    calculated_by,calculated_at,supersedes_calculation_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [calculation_id, period, version, CALCULATION_VERSION, 'CALCULATED', source_fingerprint,
                 readiness['master_sdm'], readiness['attendance_final'], readiness['skp_approved'],
                 readiness['mismatch'], readiness['ready'], actor, calculated_at, latest[0] if latest else None],
            )
            inserts = []
            for row in rows:
                adjustment = int(row.get('manual_adjustment') or 0)
                snapshot_status = 'READY' if row in ready else 'HOLD'
                result_id = str(uuid.uuid4())
                snapshot = {**row, 'id': result_id, 'calculation_id': calculation_id,
                             'calculation_version': CALCULATION_VERSION, 'version': version,
                            'scope': scope, 'target_count': len(rows),
                            'snapshot_status': snapshot_status, 'calculated_at': calculated_at.isoformat()}
                inserts.append([
                    result_id, calculation_id, CALCULATION_VERSION, period, row['nip'], snapshot_status,
                    row.get('tukin_gross'), row.get('attendance_rate'), row.get('attendance_deduction'),
                    row.get('skp_rate'), row.get('skp_deduction'), adjustment,
                    row.get('total_deduction'), row.get('final_tukin'), row.get('master_version'),
                    row.get('attendance_source_id'), row.get('attendance_batch_id'), row.get('skp_source_id'),
                    row.get('skp_source_version'), row.get('skp_batch_id'), row.get('source_fingerprint'),
                    row.get('issues'), json.dumps(snapshot, ensure_ascii=False, default=str), calculated_at,
                ])
            conn.executemany(
                """INSERT INTO tukin_calculation_result
                   (id,calculation_id,calculation_version,assessment_period,nip,status,tukin_gross,
                    attendance_rate,attendance_deduction,skp_rate,skp_deduction,adjustment,total_deduction,
                    tukin_paid,master_version,attendance_source_id,attendance_batch_id,skp_source_id,
                    skp_source_version,skp_batch_id,source_fingerprint,issues,snapshot,calculated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", inserts,
            )
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    return {
        'status': 'CALCULATED', 'calculation_id': calculation_id, 'version': version,
        'calculation_version': CALCULATION_VERSION, 'ready': len(ready),
        'mismatch': len(rows) - len(ready), 'source_fingerprint': source_fingerprint,
        'scope': scope, 'target_count': len(rows),
    }


def calculation_snapshot_rows(db_path, calculation_id):
    with duckdb.connect(db_path) as conn:
        ensure_integration_schema(conn)
        found = conn.execute(
            'SELECT snapshot FROM tukin_calculation_result WHERE calculation_id=? ORDER BY nip',
            [calculation_id],
        ).fetchall()
    if not found:
        raise ValueError('Calculation snapshot tidak ditemukan')
    return [json.loads(row[0]) for row in found]


def export_calculation_snapshot(db_path, calculation_id):
    rows = calculation_snapshot_rows(db_path, calculation_id)
    output = io.BytesIO()
    frame = pd.DataFrame(rows, columns=COLUMNS).rename(columns=COLUMNS)
    with pd.ExcelWriter(output, engine='xlsxwriter', engine_kwargs={'options':{'strings_to_formulas':False, 'strings_to_urls':False}}) as writer:
        frame.to_excel(writer, sheet_name='Hasil Perhitungan', index=False)
        sheet = writer.sheets['Hasil Perhitungan']
        sheet.freeze_panes(1, 3)
        sheet.autofilter(0, 0, len(rows), len(COLUMNS)-1)
        numeric_format = writer.book.add_format({'num_format':'#,##0'})
        for key in ['tukin_gross','skp_component','attendance_component','skp_deduction',
                    'attendance_deduction','total_deduction','calculated_entitlement',
                    'payable_estimate','manual_adjustment','final_tukin']:
            sheet.set_column(list(COLUMNS).index(key), list(COLUMNS).index(key), 23, numeric_format)
    output.seek(0)
    return output


def apply_adjustment(row, adjustment=None):
    row.update(manual_adjustment=0, adjustment_reason='', adjustment_actor='',
               adjustment_stale=False, final_tukin=row['payable_estimate'],
               payment_status='SIAP DIREVIEW' if row['payable_estimate'] is not None else 'BELUM LENGKAP / DITAHAN')
    if row.get('data_complete') and row.get('payment_block') == 'UNPAID':
        row.update(final_tukin=0, payment_status='TIDAK DIBAYARKAN')
    elif row.get('payment_block') == 'HOLD':
        row['payment_status'] = 'DITAHAN'
    if not adjustment:
        return
    amount = number(adjustment.get('amount'))
    row.update(manual_adjustment=float(amount) if amount is not None else None,
               adjustment_reason=adjustment.get('reason', ''), adjustment_actor=adjustment.get('actor', ''))
    if amount == 0:
        return  # An audited cancellation must not hold later calculations.
    stale = adjustment.get('source_fingerprint') != row['source_fingerprint']
    final = number(row['payable_estimate'])
    final = final + amount if final is not None and amount is not None else None
    if stale or final is None or not 0 <= final <= row['tukin_gross']:
        row.update(adjustment_stale=True, final_tukin=None, status='PERLU REVIEW', payment_status='PENYESUAIAN PERLU REVIEW')
        row['issues'] = '; '.join(filter(None, [row['issues'], 'Periksa ulang penyesuaian: sumber berubah atau nominal tidak valid']))
    else:
        row['final_tukin'] = money(final)


def select_scope(rows, scope):
    if scope not in VALID_SCOPES:
        raise ValueError('Cakupan harus workflow, attendance atau all')
    if scope == 'workflow':
        return [row for row in rows if row.get('workflow_participant')]
    return [row for row in rows if row.get('attendance_participant')] if scope == 'attendance' else rows


def scoped_rows(db_path, period, scope='workflow'):
    """Build exactly the population used by preview, readiness and calculation.

    Workflow is driven by live Final Absensi. Attendance intentionally preserves
    the existing live-plus-history meaning, while all remains the audit view over
    Master SDM and every available source.
    """
    if scope not in VALID_SCOPES:
        raise ValueError('Cakupan harus workflow, attendance atau all')
    rows = build(db_path, period, canonical=scope == 'workflow')
    return select_scope(rows, scope)


def calculation_scope(conn, calculation_id):
    found = conn.execute(
        'SELECT snapshot FROM tukin_calculation_result WHERE calculation_id=? ORDER BY nip LIMIT 1',
        [calculation_id],
    ).fetchone()
    if not found:
        return 'all'
    try:
        scope = json.loads(found[0]).get('scope', 'all')
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = 'all'
    return scope if scope in VALID_SCOPES else 'all'


def filter_skp_status(rows, status='all'):
    status = str(status or 'all').lower()
    if status == 'all':
        return rows
    predicates = {
        'normal': lambda row: row.get('skp_calculation_mode') == 'NORMAL',
        'final_imported': lambda row: row.get('skp_source_status') == 'FINAL_IMPORTED',
        'internal_approved': lambda row: row.get('skp_source_status') == 'INTERNAL_APPROVED',
        'missing': lambda row: row.get('skp_source_status') == 'MISSING_FROM_FINAL_IMPORT',
        'problem': lambda row: row.get('skp_source_status') in {
            'INVALID_FINAL_IMPORT', 'INTERNAL_NOT_FINAL', 'SKP_NOT_FOUND'
        },
    }
    if status not in predicates:
        raise ValueError('Filter SKP tidak valid')
    return [row for row in rows if predicates[status](row)]


def _blocker_counts(rows):
    counts = {
        'missing_master': 0,
        'invalid_gross_tukin': 0,
        'missing_final_attendance': 0,
        'skp_not_approved': 0,
        'skp_internal_not_approved': 0,
        'skp_final_import_invalid': 0,
        'duplicate_nip': 0,
        'ineligible_tukin': 0,
    }
    for row in rows:
        counts['missing_master'] += not row.get('master_available')
        counts['invalid_gross_tukin'] += row.get('tukin_gross') is None
        counts['missing_final_attendance'] += not row.get('attendance_participant') or row.get('attendance_rate') is None
        internal_pending = row.get('skp_readiness_reason_code') == 'INTERNAL_NOT_APPROVED'
        invalid_import = str(row.get('skp_readiness_reason_code') or '').startswith('FINAL_IMPORT_')
        counts['skp_internal_not_approved'] += internal_pending
        counts['skp_final_import_invalid'] += invalid_import
        counts['skp_not_approved'] += not row.get('skp_calculation_ready')
        counts['duplicate_nip'] += bool(row.get('duplicate_nip'))
        counts['ineligible_tukin'] += not row.get('eligible_tukin')
    return {key: int(value) for key, value in counts.items()}


def ensure_adjustment_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS tukin_combined_adjustment (
        assessment_period VARCHAR, nip VARCHAR, amount BIGINT, reason VARCHAR, actor VARCHAR,
        source_fingerprint VARCHAR, updated_at TIMESTAMP DEFAULT now(),
        PRIMARY KEY (assessment_period, nip))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tukin_combined_adjustment_audit (
        id VARCHAR PRIMARY KEY, assessment_period VARCHAR, nip VARCHAR,
        old_value VARCHAR, new_value VARCHAR, actor VARCHAR, created_at TIMESTAMP DEFAULT now())''')


def save_adjustment(db_path, payload):
    period = str(payload.get('assessment_period') or '')
    nip = str(payload.get('nip') or '').strip()
    amount = number(payload.get('amount'))
    actor = str(payload.get('actor') or '').strip()
    reason = str(payload.get('reason') or '').strip()
    if amount is None or amount != amount.to_integral_value() or not actor or not reason:
        raise ValueError('Nominal harus rupiah bulat; petugas dan alasan wajib diisi')
    if len(actor) > 150 or len(reason) > 2000:
        raise ValueError('Nama petugas maksimal 150 dan alasan maksimal 2000 karakter')
    # Runtime DATABASE_GATE serializes this operation with source changes.
    # Adjustment and audit are committed atomically.
    with duckdb.connect(db_path) as conn:
        ensure_adjustment_schema(conn)
        conn.execute('BEGIN TRANSACTION')
        try:
            has_periods = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='tukin_period'").fetchone()[0]
            lock = conn.execute('SELECT status FROM tukin_period WHERE assessment_period=?', [period]).fetchone() if has_periods else None
            if lock and lock[0] in {'LOCKED', 'HISTORICAL_LOCKED', 'PAID'}:
                raise ValueError('Periode terkunci atau sudah dibayar')
            row = next((r for r in build(db_path, period) if r['nip'] == nip), None)
            cancelling = row and amount == 0 and bool(row['adjustment_reason'])
            if not row or not row['attendance_participant'] or (row['payable_estimate'] is None and not cancelling):
                raise ValueError('Penyesuaian memerlukan peserta absensi dengan Master dan SKP lengkap serta layak dibayar')
            if row['period_locked']:
                raise ValueError('Hasil bulanan terkunci atau sudah dibayar')
            if payload.get('expected') != fingerprint([row]):
                raise ValueError('Data berubah. Hitung Gabungan kembali sebelum menyimpan penyesuaian')
            final = Decimal(row['payable_estimate']) + amount if row['payable_estimate'] is not None else None
            if not cancelling and (final is None or not 0 <= final <= row['tukin_gross']):
                raise ValueError('Final setelah penyesuaian harus antara nol dan Tukin bruto')
            old = conn.execute('SELECT amount,reason,actor,source_fingerprint FROM tukin_combined_adjustment WHERE assessment_period=? AND nip=?', [period,nip]).fetchone()
            old = dict(zip(['amount','reason','actor','source_fingerprint'], old)) if old else None
            new = {'amount':int(amount), 'reason':reason, 'actor':actor, 'source_fingerprint':row['source_fingerprint']}
            conn.execute('''INSERT INTO tukin_combined_adjustment
                (assessment_period,nip,amount,reason,actor,source_fingerprint,updated_at)
                VALUES (?,?,?,?,?,?,now())
                ON CONFLICT (assessment_period,nip) DO UPDATE SET amount=excluded.amount,
                reason=excluded.reason,actor=excluded.actor,source_fingerprint=excluded.source_fingerprint,updated_at=now()''',
                [period,nip,int(amount),reason,actor,row['source_fingerprint']])
            conn.execute('''INSERT INTO tukin_combined_adjustment_audit
                (id,assessment_period,nip,old_value,new_value,actor,created_at)
                VALUES (?,?,?,?,?,?,now())''',
                [str(uuid.uuid4()),period,nip,json.dumps(old,default=str),json.dumps(new),actor])
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    return {'status':'ok'}


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


COLUMNS = {
    'assessment_period':'Periode Penilaian', 'nip':'NIP', 'nama':'Nama', 'jabatan':'Jabatan', 'unit':'Unit',
    'tukin_gross':'Tukin Bruto', 'skp_component':'Komponen SKP 70%', 'attendance_component':'Komponen Absensi 30%',
    'predicate_skp':'Predikat SKP', 'report_status':'Status Laporan SKP', 'skp_rate':'Potongan SKP (%)',
    'attendance_rate':'Potongan Absensi (%)', 'skp_deduction':'Nominal Potongan SKP',
    'attendance_deduction':'Nominal Potongan Absensi', 'total_deduction':'Total Potongan',
    'calculated_entitlement':'Hasil Perhitungan', 'payable_estimate':'Estimasi Dibayar',
    'status':'Kelengkapan', 'issues':'Perlu Diperiksa', 'master_version':'Versi Master',
    'attendance_source':'Sumber Absensi', 'manual_adjustment':'Penyesuaian (Rp)',
    'adjustment_reason':'Alasan Penyesuaian', 'adjustment_actor':'Petugas Penyesuaian',
    'final_tukin':'Final Dasar Tukin', 'tukin_gross_source':'Sumber Tukin Bruto',
    'payment_status':'Status Pembayaran / Review',
    'wilayah_3t':'Wilayah 3T', 'status_tendik':'Status Tendik', 'period_status':'Status Periode',
    'calculation_id':'Calculation ID', 'version':'Calculation Version',
    'calculation_version':'Versi Mesin Hitung', 'snapshot_status':'Status Snapshot',
    'attendance_source_id':'Absensi Source ID', 'attendance_batch_id':'Absensi Batch ID',
    'skp_source_id':'SKP Source ID', 'skp_source_version':'SKP Source Version',
    'skp_batch_id':'SKP Batch ID', 'skp_approved_at':'SKP Approved At',
    # Audit columns are appended to preserve the positions of the legacy export.
    'skp_source_label':'Sumber SKP', 'skp_source_status':'Status Sumber SKP',
    'skp_calculation_mode':'Mode Perhitungan SKP', 'skp_fallback_policy':'Kebijakan Fallback SKP',
    'skp_description':'Keterangan SKP', 'skp_workflow_status':'Status Workflow SKP',
    'skp_ready_for_tukin':'SKP Normal Siap Tukin',
}


def create_router(db_path_getter):
    router = APIRouter(prefix='/api/tukin/combined', tags=['tukin-combined'])

    @router.get('')
    def preview(assessment_period: str, page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=500), q: str = '', scope: str = 'all', skp_status: str = 'all'):
        try:
            rows = scoped_rows(db_path_getter(), assessment_period, scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        summary = {
            'total': len(rows),
            'complete': sum(row.get('final_tukin') is not None for row in rows),
            'review': sum(row.get('final_tukin') is None for row in rows),
            'unpaid': sum(row.get('payment_status') == 'TIDAK DIBAYARKAN' for row in rows),
            'gross': sum(int(row.get('tukin_gross') or 0) for row in rows),
            'skp_deduction': sum(int(row.get('skp_deduction') or 0) for row in rows),
            'attendance_deduction': sum(int(row.get('attendance_deduction') or 0) for row in rows),
            'total_deduction': sum(int(row.get('total_deduction') or 0) for row in rows),
            'entitlement': sum(int(row.get('final_tukin') or 0) for row in rows),
        }
        try:
            filtered = filter_skp_status(rows, skp_status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        filtered = [row for row in filtered if not q or q.lower() in (row['nip']+' '+str(row['nama'])).lower()]
        start = (page-1)*per_page
        visible = [{**row, 'edit_token':fingerprint([row])} for row in filtered[start:start+per_page]]
        return {'rows':visible, 'total':len(filtered), 'summary':summary, 'page':page,
                'fingerprint':fingerprint(rows), 'scope':scope, 'skp_status':skp_status}

    @router.get('/readiness')
    def readiness(assessment_period: str, scope: str = 'workflow'):
        try:
            return integration_readiness(db_path_getter(), assessment_period, scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post('/calculate')
    def calculate(payload: dict):
        try:
            return create_calculation_snapshot(
                db_path_getter(), str(payload.get('assessment_period') or ''),
                payload.get('calculated_by'), str(payload.get('expected') or ''),
                str(payload.get('scope') or 'workflow'),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/calculations')
    def calculations(assessment_period: str):
        with duckdb.connect(db_path_getter()) as conn:
            ensure_integration_schema(conn)
            frame = conn.execute(
                """SELECT id calculation_id,assessment_period,version,calculation_version,status,
                          source_fingerprint,master_count,attendance_final_count,skp_approved_count,
                          mismatch_count,ready_count,calculated_by,calculated_at,supersedes_calculation_id
                   FROM tukin_calculation_batch WHERE assessment_period=? ORDER BY version DESC""",
                [assessment_period],
            ).fetchdf()
            rows = frame.astype(object).where(pd.notnull(frame), None).to_dict('records')
            for row in rows:
                row['scope'] = calculation_scope(conn, row['calculation_id'])
        return {'rows': rows}

    @router.get('/calculations/{calculation_id}/export')
    def export_saved_calculation(calculation_id: str):
        try:
            output = export_calculation_snapshot(db_path_getter(), calculation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return StreamingResponse(output, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition':f'attachment; filename="HASIL_PERHITUNGAN_{calculation_id}.xlsx"'})

    @router.post('/adjustment')
    def adjust(payload: dict):
        try:
            return save_adjustment(db_path_getter(), payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/adjustment/audit')
    def adjustment_audit(assessment_period: str, nip: str):
        with duckdb.connect(db_path_getter()) as conn:
            if not conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='tukin_combined_adjustment_audit'").fetchone()[0]:
                return {'rows':[]}
            data = conn.execute('''SELECT old_value,new_value,actor,created_at
                FROM tukin_combined_adjustment_audit WHERE assessment_period=? AND nip=?
                ORDER BY created_at DESC''', [assessment_period,nip]).fetchall()
        return {'rows':[{'old_value':json.loads(r[0]),'new_value':json.loads(r[1]),'actor':r[2],'created_at':str(r[3])} for r in data]}

    @router.get('/export')
    def export(assessment_period: str, expected: str = '', scope: str = 'all'):
        try:
            rows = select_scope(build(db_path_getter(), assessment_period), scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if expected and expected != fingerprint(rows):
            raise HTTPException(409, 'Data berubah setelah tampilan dimuat. Klik Hitung Gabungan lagi sebelum ekspor.')
        output = io.BytesIO()
        frame = pd.DataFrame(rows, columns=COLUMNS).rename(columns=COLUMNS)
        with pd.ExcelWriter(output, engine='xlsxwriter', engine_kwargs={'options':{'strings_to_formulas':False, 'strings_to_urls':False}}) as writer:
            frame.to_excel(writer, sheet_name='Gabungan SKP Absensi', index=False)
            sheet = writer.sheets['Gabungan SKP Absensi']
            sheet.freeze_panes(1, 3)
            sheet.autofilter(0, 0, len(rows), len(COLUMNS)-1)
            sheet.set_column(0, 1, 22); sheet.set_column(2, 4, 30); sheet.set_column(5, 18, 23)
            sheet.set_column(18, 18, 65)
            sheet.set_column(20, len(COLUMNS)-1, 26)
            numeric_format = writer.book.add_format({'num_format':'#,##0'})
            for key in ['tukin_gross','skp_component','attendance_component','skp_deduction','attendance_deduction','total_deduction','calculated_entitlement','payable_estimate','manual_adjustment','final_tukin']:
                col = list(COLUMNS).index(key)
                sheet.set_column(col, col, 23, numeric_format)
        output.seek(0)
        return StreamingResponse(output, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition':f'attachment; filename="REVIEW_GABUNGAN_{assessment_period}.xlsx"'})
    return router

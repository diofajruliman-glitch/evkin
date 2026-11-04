"""Reconcile displayed Final -> database snapshot -> generated XLSX."""
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

FIELDS = ('nip', 'tukin', 'nominal', 'finalDibayar')


def rupiah(value):
    if value is None or str(value).strip() == '':
        raise ValueError('Nominal Final kosong')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Nominal Final tidak valid')
    return str(int(number.quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def final_manifest(rows):
    normalized = []
    for row in rows:
        nip = str(row.get('nip') or '').strip()
        if not nip:
            raise ValueError('NIP Final kosong')
        normalized.append([nip, *[rupiah(row.get(key)) for key in FIELDS[1:]]])
    # Sort serialized rows, preserving duplicates (a set alone loses rows).
    lines = sorted(json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in normalized)
    digest = hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()
    return {'row_count': len(normalized), 'unique_nip': len({row[0] for row in normalized}),
            'gross': str(sum(int(row[1]) for row in normalized)),
            'deduction': str(sum(int(row[2]) for row in normalized)),
            'paid': str(sum(int(row[3]) for row in normalized)), 'sha256': digest}


def assert_manifest(actual, expected):
    if actual != expected:
        raise ValueError('Validasi Final gagal: jumlah baris, NIP, atau nominal berbeda dengan Final yang diperiksa. Buka kembali Final dan ekspor ulang.')


def verify_final_xlsx(path, expected):
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        names = [name for name in workbook.sheetnames
                 if name == 'FINAL ABSENSI' or name.startswith('FINAL ABSENSI (')]
        if 'FINAL ABSENSI' not in names:
            raise ValueError('Sheet FINAL ABSENSI tidak ditemukan')

        def rows():
            for name in names:
                entries = iter(workbook[name].values)
                headers = next(entries, ())
                if not set(FIELDS).issubset(headers):
                    raise ValueError('Kolom Final tidak lengkap dalam Excel')
                positions = {field: headers.index(field) for field in FIELDS}
                for row in entries:
                    yield {field: row[index] for field, index in positions.items()}

        actual = final_manifest(rows())
        assert_manifest(actual, expected)
        return actual
    finally:
        workbook.close()

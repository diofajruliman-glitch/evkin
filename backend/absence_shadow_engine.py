"""Read-only Python shadow of the legacy EVKIN JavaScript absence engine.

This module is deliberately isolated from production endpoints and persistence.
JavaScript remains the calculation authority.  The functions here reproduce the
ABSENCE_JS_BASELINE_2026_09 data contract, including the complete next-day
attendance exemption and imported-workbook job precedence requested on 2026-10-13. Historical source fingerprints
remain unchanged; the updated calculation reference is stored separately.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BASELINE_ID = "ABSENCE_JS_BASELINE_2026_09"
BASELINE_SOURCE_SHA256 = {
    "assets/js/utils.js": "5688219983fc60595d94f346eea446e0017167925209910ac11dc576023bb451",
    "assets/js/data.js": "2cce3d441bf841bb7cac2e071e210e79585445f44b56fcb9b89bd3dbb2b32f7c",
    "assets/js/state.js": "a8bb9b95b676ca282f9921ef8a0823c317c7778758a65e2a94d022de6d73134d",
    "assets/js/import.js": "24232d62278414d81f2fc2b819df49e256039cb64a4c63cb03bcaf82d01497b4",
    "assets/js/import-wali.js": "7677b1f865729ad5ae748be63f103856a5b3eae878ec7272c1c3074f82127657",
    "assets/js/calc.js": "1bd39a7e3200959d28b06023149b5d3379b1f8e0d727f950d0cf0cb5298f2b6b",
    "assets/js/harian.js": "2ecd15eb1e390736132d0ec6498848544a44dee3e79cd05735a11e069a7f2a04",
    "assets/js/semifinal.js": "8fd7fb8c1d78df16cb86a4027c14ae16e261afec074cb2e2fd5c4e29d644cbfc",
    "assets/js/rekap.js": "c94c37e4ed549f702e81e7eaf6cd3c067f23cd5d0b44f3cebae1e825d70959ef",
    "assets/js/final.js": "14badb92ecf785f3e8450f9b650438d0ece2526e15ee6fea4129ae5a3b937b47",
    "assets/js/app.js": "b90e9108a2558497e66450aad3ef8d2a1b3c3616e4ac4105d479f2631ba1b535",
}
BASELINE_GOLDEN_SHA256 = "15ac3c67be87436f8ea16867cf1ead1c467c7ddbcdfbde6f89bfaf0ec9f664d7"
# Approved source-retention/checkout-date rules; historical hashes stay intact.
IMPORT_SOURCE_SHA256_20261105 = {
    "assets/js/calc.js": "026faab3014e14ce530b3af367d67f8ea117ff9f1d24319d2ffdaae730c6610f",
    "assets/js/import.js": "c32dfde7022f9266f5092fb8ed0b59ee1d1ad961dd04357d1d7c8c3e42a2bd9f",
    "assets/js/state.js": "f034f68bd0973a7dd5aa6160e47e0f289e2e03bac31a98ad39808f8869b52162",
}

MONTHS_ID = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)
DAYS_ID = ("Minggu", "Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu")

ZONE_BY_PROVINCE = {
    "bali": "WITA", "nusa tenggara barat": "WITA", "ntb": "WITA",
    "nusa tenggara timur": "WITA", "ntt": "WITA",
    "kalimantan selatan": "WITA", "kalimantan timur": "WITA", "kalimantan utara": "WITA",
    "sulawesi selatan": "WITA", "sulawesi tenggara": "WITA", "sulawesi tengah": "WITA",
    "sulawesi utara": "WITA", "sulawesi barat": "WITA", "gorontalo": "WITA",
    "maluku": "WIT", "maluku utara": "WIT", "papua": "WIT", "papua barat": "WIT",
    "papua tengah": "WIT", "papua pegunungan": "WIT", "papua selatan": "WIT",
    "papua barat daya": "WIT",
}
ZONE_OFFSET = {"WIB": 0, "WITA": -60, "WIT": -120}
ZONE_OFFSET_ADD = {"WIB": 0, "WITA": 60, "WIT": 120}

IMPORT_HEADER_MAP = (
    ("nip", ("nip", "nomorpegawai", "noindukpegawai")),
    ("nama", ("nama", "namapegawai", "namalengkap", "nmpeg", "name")),
    ("jabatan", ("jabatan", "posisi", "jabatanpegawai", "nmjabatan")),
    ("tanggal", ("tanggal", "tanggalmasuk", "tglmasuk", "tgl", "tanggalkerja", "tglfinger", "date", "tanggalabsen")),
    ("tanggalPulang", ("tanggalpulang", "tglpulang", "tanggalkeluar")),
    ("hari", ("hari", "day")),
    ("jamMasuk", ("jammasuk", "masuk", "checkin", "jamin", "scanmasuk", "waktumasuk", "jamdatang", "absenpagi", "in")),
    ("jamPulang", ("jampulang", "pulang", "checkout", "jamout", "scanpulang", "waktupulang", "jamkeluar", "absensore", "out")),
    ("zona", ("zona", "zonawaktu", "timezone", "tz")),
    ("provinsi", ("provinsi", "prov", "province")),
    ("kabupaten", ("kabupaten", "kabkota", "kota", "kab", "kabupatenkota")),
    ("jenis", ("jenis", "jenisabsensi", "keterangan", "ket", "statusabsen", "jenisabsen", "jnshadir", "tipe")),
)


class ShadowEngineError(ValueError):
    """Predictable, comparison-safe shadow-engine error."""


class DuplicateAttendanceError(ShadowEngineError):
    pass


def js_number(value: Any) -> float:
    """Subset of JavaScript Number() used by the frozen calculation paths."""
    if value is None:
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def js_or_zero(value: Any) -> float:
    number = js_number(value)
    return 0.0 if not math.isfinite(number) or number == 0 else number


def _compact_number(value: float) -> int | float:
    if math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def js_math_round(value: Any) -> int:
    """JavaScript Math.round for finite values (ties toward +infinity)."""
    number = js_number(value)
    if not math.isfinite(number):
        raise ShadowEngineError("Math.round menerima nilai non-finite")
    return math.floor(number + 0.5)


def js_round2(value: Any, *, coerce: bool = False) -> int | float:
    number = js_or_zero(value) if coerce else js_number(value)
    if not math.isfinite(number):
        raise ShadowEngineError("round2 menerima nilai non-finite")
    return _compact_number(js_math_round(number * 100) / 100)


def _date_parts(year: int, month: int, day: int) -> dict[str, Any]:
    return {
        "y": year, "m": month, "d": day,
        "key": f"{year}-{month:02d}-{day:02d}",
        "str": f"{day:02d}/{month:02d}/{year}",
        "monthKey": f"{year}-{month:02d}",
    }


def _normalized_datetime(date: dict[str, Any]) -> datetime:
    # Date.UTC normalizes invalid day-of-month values; reproduce that behavior.
    return datetime(int(date["y"]), int(date["m"]), 1, tzinfo=timezone.utc) + timedelta(days=int(date["d"]) - 1)


def parse_date(value: Any) -> dict[str, Any] | None:
    """Legacy-compatible date parser; intentionally accepts explicit 31-Feb style dates."""
    if value is None or value == "":
        return None
    year = month = day = None
    if isinstance(value, datetime):
        year, month, day = value.year, value.month, value.day
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 60:
            return None
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=math.floor(value - 25569))
        year, month, day = dt.year, dt.month, dt.day
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text) and float(text) >= 60:
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=math.floor(float(text) - 25569))
            year, month, day = dt.year, dt.month, dt.day
        elif match := re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text):
            year, month, day = map(int, match.groups())
        elif match := re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text):
            day, month, year = map(int, match.groups())
        elif match := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2})", text):
            day, month, short_year = map(int, match.groups())
            year = 2000 + short_year
        else:
            # Best-effort equivalent of browser Date parsing for ISO-like input.
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            year, month, day = parsed.year, parsed.month, parsed.day
    if not year or not month or not day or month < 1 or month > 12 or day < 1 or day > 31:
        return None
    return _date_parts(int(year), int(month), int(day))


def add_days(date: dict[str, Any], days: int) -> dict[str, Any]:
    dt = _normalized_datetime(date) + timedelta(days=days)
    return _date_parts(dt.year, dt.month, dt.day)


def day_of_week(date: dict[str, Any]) -> int:
    # Python Monday=0; JavaScript Sunday=0.
    return (_normalized_datetime(date).weekday() + 1) % 7


def day_name(date: dict[str, Any]) -> str:
    return DAYS_ID[day_of_week(date)]


def is_weekend(date: dict[str, Any]) -> bool:
    return day_of_week(date) in (0, 6)


def _format_seconds(total_seconds: int) -> str:
    normalized = total_seconds % 86400
    hours = normalized // 3600
    minutes = (normalized % 3600) // 60
    seconds = normalized % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def normalize_excel_time(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return ""
        decimal = re.sub(r"\s+", "", text).replace(",", ".")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", decimal):
            return normalize_excel_time(float(decimal))
        match = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::(\d{2}))?", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            second = int(match.group(3) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
                return f"{hour:02d}:{minute:02d}:{second:02d}"
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        number = float(value)
        if number < 0:
            return ""
        if number >= 1:
            fraction = number - math.floor(number)
            if fraction == 0:
                return ""
            seconds = js_math_round(fraction * 86400) % 86400
            return _format_seconds(seconds)
        seconds = js_math_round(number * 86400) % 86400
        return _format_seconds(seconds)
    return ""


def parse_time(value: Any) -> int | None:
    canonical = normalize_excel_time(value)
    if canonical == "":
        return None
    hour, minute, second = map(int, canonical.split(":"))
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 60 + minute + (1 if second >= 30 else 0)


def fmt_time(minutes: Any) -> str:
    if minutes is None or minutes == "":
        return ""
    normalized = int(minutes) % 1440
    return f"{normalized // 60:02d}:{normalized % 60:02d}"


def duration_minutes(check_in: int | None, check_out: int | None) -> int | None:
    if check_in is None or check_out is None:
        return None
    duration = check_out - check_in
    if duration <= 0:
        duration += 1440
    return duration


def fmt_duration(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes // 60}j {minutes % 60:02d}m"


def detect_zone(province: Any, explicit: Any) -> str:
    if explicit:
        zone = str(explicit).strip().upper()
        if zone in ("WIB", "WITA", "WIT"):
            return zone
    return ZONE_BY_PROVINCE.get(str(province or "").strip().lower(), "WIB")


def _wrap(minutes: int | None, adjustment: int) -> int | None:
    return None if minutes is None else (minutes + adjustment) % 1440


def _header_norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def detect_import_columns(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        normalized = _header_norm(header)
        for field, aliases in IMPORT_HEADER_MAP:
            if field not in mapping and normalized in aliases:
                mapping[field] = index
                break
    for index, header in enumerate(headers):
        normalized = _header_norm(header)
        for field, aliases in IMPORT_HEADER_MAP:
            if field not in mapping and any(alias in normalized for alias in aliases):
                mapping[field] = index
    return mapping


def normalize_raw_row(row: dict[str, Any], headers: list[str], column_map: dict[str, int], row_id: str) -> dict[str, Any] | None:
    values = [row.get(header) for header in headers]
    get = lambda field: values[column_map[field]] if field in column_map else ""
    nip = str(get("nip") or "").strip()
    check_in_raw = normalize_excel_time(get("jamMasuk"))
    check_out_raw = normalize_excel_time(get("jamPulang"))
    in_date_raw = get("tanggal")
    date = parse_date(in_date_raw)
    out_date = parse_date(get("tanggalPulang")) if "tanggalPulang" in column_map else None
    checkout_only_date = (
        not str(in_date_raw or "").strip() and out_date is not None
        and bool(check_out_raw) and 6 <= int(check_out_raw[:2]) < 9
    )
    if checkout_only_date:
        date = add_days(out_date, -1)
    elif date is None:
        date = out_date
    check_in = parse_time(check_in_raw)
    check_out = parse_time(check_out_raw)
    issues: list[str] = []
    if not nip:
        issues.append("NIP kosong")
    if not date:
        issues.append("Tanggal tidak valid")
    if get("jamMasuk") is not None and str(get("jamMasuk")).strip() and check_in is None:
        issues.append("Jam masuk tidak valid")
    if get("jamPulang") is not None and str(get("jamPulang")).strip() and check_out is None:
        issues.append("Jam pulang tidak valid")
    if check_in is None:
        issues.append("Jam masuk kosong")
    if check_out is None:
        issues.append("Jam pulang kosong")
    if not date or not nip:
        return None
    province = str(get("provinsi") or "").strip()
    zone = detect_zone(province, get("zona"))
    cross_day = bool(out_date and out_date["key"] > date["key"])
    if not cross_day and check_in is not None and check_out is not None and check_out < check_in:
        cross_day = True
    return {
        "_id": row_id,
        "nip": nip,
        "nama": str(get("nama") or "").strip(),
        "jabatan": str(get("jabatan") or "").strip(),
        "tanggal": date,
        "tanggalPulang": out_date["str"] if out_date else "",
        "hari": str(get("hari") or "").strip() or day_name(date),
        "masukRaw": check_in_raw, "pulangRaw": check_out_raw,
        "masukMin": check_in, "pulangMin": check_out,
        "masukNormMin": _wrap(check_in, ZONE_OFFSET[zone]),
        "pulangNormMin": _wrap(check_out, ZONE_OFFSET[zone]),
        "masukNormMinAdd": _wrap(check_in, ZONE_OFFSET_ADD[zone]),
        "pulangNormMinAdd": _wrap(check_out, ZONE_OFFSET_ADD[zone]),
        "provinsi": province, "kabupaten": str(get("kabupaten") or "").strip(),
        "zona": zone, "jenis": str(get("jenis") or "").strip(),
        "crossDay": cross_day, "issues": issues, "dupFlag": False, "deleted": False,
    }


def normalize_raw_rows(headers: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    column_map = detect_import_columns(headers)
    records = []
    for index, row in enumerate(rows):
        normalized = normalize_raw_row(row, headers, column_map, f"rw-{index:06d}")
        if normalized:
            records.append(normalized)
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(f"{record['nip']}|{record['tanggal']['key']}", []).append(record)
    for group in groups.values():
        if len(group) > 1:
            for record in group:
                record["dupFlag"] = True
    return records


def normalize_import_case(item: dict[str, Any]) -> dict[str, Any]:
    headers = ("NIP", "Nama", "Jabatan", "Tanggal", "Tanggal Pulang", "Jam Masuk", "Jam Pulang", "Zona", "Provinsi", "Jenis")
    values = (item.get("nip"), item.get("name"), item.get("job"), item.get("date"), item.get("out_date"), item.get("in"), item.get("out"), item.get("zone"), item.get("province"), item.get("kind"))
    raw = dict(zip(headers, values))
    raw["Jam Masuk"] = normalize_excel_time(item.get("in"))
    raw["Jam Pulang"] = normalize_excel_time(item.get("out"))
    nip = str(item.get("nip") or "").strip()
    date = parse_date(item.get("date")) or parse_date(item.get("out_date"))
    out_date = parse_date(item.get("out_date")) if item.get("out_date") not in (None, "") else None
    check_in_raw = normalize_excel_time(item.get("in"))
    check_out_raw = normalize_excel_time(item.get("out"))
    check_in = parse_time(check_in_raw)
    check_out = parse_time(check_out_raw)
    issues: list[str] = []
    if not nip:
        issues.append("NIP kosong")
    if not date:
        issues.append("Tanggal tidak valid")
    if item.get("in") is not None and str(item.get("in")).strip() and check_in is None:
        issues.append("Jam masuk tidak valid")
    if item.get("out") is not None and str(item.get("out")).strip() and check_out is None:
        issues.append("Jam pulang tidak valid")
    if not item.get("partial"):
        if check_in is None:
            issues.append("Jam masuk kosong")
        if check_out is None:
            issues.append("Jam pulang kosong")
    if not date or not nip:
        return {"id": item["id"], "invalid": True, "raw": raw, "normalized": None}
    zone = detect_zone(item.get("province"), item.get("zone"))
    cross_day = bool(out_date and out_date["key"] > date["key"])
    if not cross_day and check_in is not None and check_out is not None and check_out < check_in:
        cross_day = True
    data = {
        "nip": nip,
        "nama": str(item.get("name") or "").strip(),
        "jabatan": str(item.get("job") or "").strip(),
        "tanggal": date,
        "tanggalPulang": out_date["str"] if out_date else "",
        "hari": day_name(date),
        "masukRaw": check_in_raw,
        "pulangRaw": check_out_raw,
        "masukMin": check_in,
        "pulangMin": check_out,
        "masukNormMin": _wrap(check_in, ZONE_OFFSET[zone]),
        "pulangNormMin": _wrap(check_out, ZONE_OFFSET[zone]),
        "masukNormMinAdd": _wrap(check_in, ZONE_OFFSET_ADD[zone]),
        "pulangNormMinAdd": _wrap(check_out, ZONE_OFFSET_ADD[zone]),
        "provinsi": str(item.get("province") or "").strip(),
        "kabupaten": "",
        "zona": zone,
        "jenis": str(item.get("kind") or "").strip(),
        "crossDay": cross_day,
        "issues": issues,
        "dupFlag": False,
        "deleted": False,
    }
    return {"id": item["id"], "invalid": False, "raw": raw, "normalized": data}


@dataclass(frozen=True)
class CalculationContext:
    config: dict[str, Any]
    holiday_set: frozenset[str]
    holiday_names: dict[str, str]
    job_methods: dict[str, str]
    employees: dict[str, dict[str, Any]]
    no_deduction_kinds: frozenset[str]


def build_context(config: dict[str, Any], employees: Iterable[dict[str, Any]] = ()) -> CalculationContext:
    holidays = config.get("holidays") or []
    jobs = config.get("jabatanMaster") or []
    employee_map = {str(row.get("nip", "")).strip(): dict(row) for row in employees}
    return CalculationContext(
        config=config,
        holiday_set=frozenset(str(row.get("d", "")) for row in holidays),
        holiday_names={str(row.get("d", "")): str(row.get("n") or "Libur") for row in holidays},
        job_methods={str(row.get("nama") or "").strip().lower(): row.get("metode") for row in jobs},
        employees=employee_map,
        no_deduction_kinds=frozenset(str(value).strip().lower() for value in (config.get("jenisNoPotongan") or [])),
    )


def resolve_job(record: dict[str, Any], context: CalculationContext) -> str:
    job = record.get("jabatan", "")
    return str(job or "").strip() or "Belum Ditentukan"


def method_of(job: str, context: CalculationContext) -> str:
    return "durasi" if context.job_methods.get(job.strip().lower()) == "durasi" else "reguler"


def _leave_period(nip: Any, date_key: str, context: CalculationContext) -> dict[str, Any] | None:
    key = str(nip).strip()
    for period in context.config.get("leavePeriods") or []:
        if str(period.get("nip", "")).strip() == key and str(period.get("mulai", "")) <= date_key <= str(period.get("selesai", "")):
            return period
    return None


def _tier_percentage(tiers: list[dict[str, Any]], minutes: int) -> int | float:
    for tier in tiers:
        if minutes <= js_number(tier.get("max")):
            return _compact_number(js_or_zero(tier.get("pct")))
    return _compact_number(js_or_zero(tiers[-1].get("pct"))) if tiers else 0


def duration_tier_percentage(hours: float, config: dict[str, Any]) -> int | float:
    minimum = js_number(config.get("minDurasiJam"))
    if hours >= minimum:
        return 0
    if hours >= minimum - 0.5:
        return 0.5
    if hours >= minimum - 1.0:
        return 1
    if hours >= minimum - 1.5:
        return 1.5
    if hours >= minimum - 2.0:
        return 2
    return 3


def times_for_mode(record: dict[str, Any], mode: str) -> tuple[int | None, int | None]:
    if mode == "wib":
        return record.get("masukNormMin"), record.get("pulangNormMin")
    if mode == "wib-add":
        return record.get("masukNormMinAdd"), record.get("pulangNormMinAdd")
    return record.get("masukMin"), record.get("pulangMin")


def compute_record(record: dict[str, Any], context: CalculationContext, mode: str) -> dict[str, Any]:
    config = context.config
    job = resolve_job(record, context)
    method = method_of(job, context)
    check_in, check_out = times_for_mode(record, mode)
    date = record["tanggal"]
    holiday = date["key"] in context.holiday_set or is_weekend(date)
    wali = record.get("sourceFormat") == "wali"
    holiday_label = context.holiday_names.get(date["key"], "Libur Nasional") if date["key"] in context.holiday_set else ("Akhir Pekan" if is_weekend(date) else "")
    result: dict[str, Any] = {
        "_id": record.get("_id"),
        "nip": record.get("nip"),
        "nama": record.get("nama"),
        "jabatan": job,
        "metode": method,
        "tanggal": date["str"],
        "tanggalKey": date["key"],
        "monthKey": date["monthKey"],
        "hari": day_name(date),
        "jamMasuk": fmt_time(check_in),
        "jamPulang": fmt_time(check_out),
        "tanggalMasuk": (record.get("tanggalMasukObj") or date)["str"],
        "tanggalPulang": (record.get("tanggalPulangObj") or {}).get("str") or record.get("tanggalPulang") or date["str"],
        "masukAt": record.get("masukAt") or "",
        "pulangAt": record.get("pulangAt") or "",
        "jamMasukAsli": fmt_time(record.get("masukMin")),
        "jamPulangAsli": fmt_time(record.get("pulangMin")),
        "jamNormalisasi": "Original" if mode == "original" else ("" if record.get("zona") == "WIB" else f"{fmt_time(check_in)} - {fmt_time(check_out)}"),
        "zona": record.get("zona"),
        "durasiMin": None,
        "durasi": "",
        "jenis": record.get("jenis") or "",
        "statusHariLibur": f"Ya ({holiday_label})" if holiday else "Tidak",
        "statusCuti": "",
        "statusDuplikasi": "Duplikasi" if record.get("dupFlag") else "",
        "statusLengkap": "",
        "potKeterlambatan": 0,
        "potPulangCepat": 0,
        "potDurasi": 0,
        "potHariKurang": 0,
        "potTidakLengkap": 0,
        "totalHarian": 0,
        "flags": [],
        "statusJadwal": "OFF" if holiday else "ADA_JADWAL",
        "statusAbsensi": "PERLU_VALIDASI",
    }
    if record.get("dupFlag"):
        result["flags"].append("ANOMALI DUPLIKASI TANGGAL")
    if record.get("issues"):
        result["flags"].append("PERLU_VALIDASI")
    if holiday:
        result["flags"].append("ABSEN HARI LIBUR")
    if record.get("shiftFlag"):
        result["flags"].append(record["shiftFlag"])
    if record.get("crossDay"):
        result["flags"].append("PULANG HARI BERIKUTNYA")
    if wali and holiday:
        result["statusAbsensi"] = "ABSEN_DI_LUAR_JADWAL"
        result["flags"].append("ABSEN_DI_LUAR_JADWAL")
    actual_check_out = check_out + 1440 if record.get("crossDay") and check_out is not None else check_out
    duration = (
        max(0, actual_check_out - check_in)
        if record.get("crossDay") and check_in is not None and actual_check_out is not None
        else duration_minutes(check_in, check_out)
    )
    result["durasiMin"] = duration
    result["durasi"] = fmt_duration(duration)

    leave = _leave_period(record.get("nip"), date["key"], context)
    if leave:
        result["statusCuti"] = leave.get("jenis")
        result["flags"].append("PERIODE CUTI/SAKIT")
        result["excused"] = True
        return result
    kind = record.get("jenis")
    if kind and str(kind).strip().lower() in context.no_deduction_kinds:
        result["statusCuti"] = kind
        result["flags"].append("JENIS BEBAS POTONGAN")
        result["excused"] = True
        return result
    if holiday:
        result["statusLengkap"] = "Tidak Lengkap" if check_in is None or check_out is None else "Lengkap"
        result["statusAbsensi"] = "ABSEN_DI_LUAR_JADWAL"
        result["flags"].append("HARI LIBUR - TANPA POTONGAN")
        return result
    if check_in is None or check_out is None:
        result["statusLengkap"] = "Tidak Lengkap"
        result["statusAbsensi"] = "LUPA_ABSEN_MASUK" if check_in is None else "LUPA_ABSEN_PULANG"
        if method == "durasi" and job.lower() == "wali asuh":
            result["statusAbsensi"] = "PERLU_VALIDASI"
            result["flags"].append("REKAMAN SHIFT TIDAK LENGKAP - CEK REKAP HARI KERJA")
            return result
        result["potTidakLengkap"] = _compact_number(js_number(config.get("potonganTidakLengkap")))
        result["totalHarian"] = result["potTidakLengkap"]
        result["flags"].append("ABSENSI TIDAK LENGKAP")
        return result
    result["statusLengkap"] = "Lengkap"
    if wali:
        result["statusAbsensi"] = "LINTAS_HARI" if record.get("crossDay") else "NORMAL"
    if record.get("crossDay"):
        result["statusAbsensi"] = "LINTAS_HARI"
        if method == "reguler":
            result["_lateMin"] = result["_earlyMin"] = 0
            return result
    if method == "durasi":
        result["potDurasi"] = duration_tier_percentage(duration / 60, config)
    else:
        friday = day_of_week(date) == 5
        schedule = config["jamDasar"]["jumat" if friday else "senKam"]
        standard_in = parse_time(schedule["masuk"])
        standard_out = parse_time(schedule["pulang"])
        late = max(0, check_in - standard_in)
        early = max(0, standard_out - actual_check_out)
        flexi = config.get("flexi") or {}
        if flexi.get("enabled"):
            start = parse_time(flexi.get("masukStart"))
            end = parse_time(flexi.get("masukEnd"))
            if check_in >= start and check_in <= end:
                compensation = parse_time(flexi.get("kompensasiJumat" if friday else "kompensasiSenKam"))
                if check_out > compensation:
                    late = early = 0
                    result["flags"].append("FLEXI TIME (kompensasi)")
        if late > 0:
            result["potKeterlambatan"] = _tier_percentage(config.get("keterlambatanTiers") or [], late)
        if early > 0:
            result["potPulangCepat"] = _tier_percentage(config.get("pulangCepatTiers") or [], early)
        result["_lateMin"] = late
        result["_earlyMin"] = early
    result["totalHarian"] = js_round2(result["potKeterlambatan"] + result["potPulangCepat"] + result["potDurasi"] + result["potTidakLengkap"])
    return result


def effective_date_keys(month: str, context: CalculationContext) -> set[str]:
    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    next_month = datetime(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1, tzinfo=timezone.utc)
    keys: set[str] = set()
    for offset in range((next_month - start).days):
        current = start + timedelta(days=offset)
        key = current.strftime("%Y-%m-%d")
        if current.weekday() < 5 and key not in context.holiday_set:
            keys.add(key)
    return keys


def effective_days(month: str, context: CalculationContext) -> int | float:
    config = context.config
    if config.get("effectiveDaysMode") == "manual":
        return _compact_number(js_or_zero(config.get("effectiveDaysManual")))
    return len(effective_date_keys(month, context))


def compute_rekap(
    records: list[dict[str, Any]], context: CalculationContext, *, roster: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not records and not roster:
        raise ShadowEngineError("Belum ada hasil Hitung Harian.")
    seen: set[str] = set()
    for record in records:
        key = record.get("tanggalKey")
        if key in seen:
            raise DuplicateAttendanceError("Ada duplikat NIP/tanggal. Periksa dan koreksi di Semi Final sebelum Rekap.")
        seen.add(key)
    if records:
        first = records[0]
    else:
        job = resolve_job(roster, context)
        first = {**roster, "jabatan": job, "metode": method_of(job, context)}
    month = first.get("monthKey") or first.get("bulan") or str(first.get("tanggalKey") or "")[:7]
    present_rows = [row for row in records if row.get("jamMasuk") or row.get("jamPulang")]
    date_keys = {row.get("tanggalKey") for row in present_rows}
    present_all = set(date_keys)
    present_all.update(row.get("tanggalKey") for row in records if row.get("excused") or row.get("statusCuti"))
    nip = str(first.get("nip", "")).strip()
    for leave in context.config.get("leavePeriods") or []:
        if str(leave.get("nip", "")).strip() != nip:
            continue
        current = parse_date(leave.get("mulai"))
        end = parse_date(leave.get("selesai"))
        guard = 0
        while current and end and current["key"] <= end["key"] and guard < 400:
            if current["monthKey"] == month:
                present_all.add(current["key"])
            current = add_days(current, 1)
            guard += 1
    days = effective_days(month, context)
    if first.get("metode") == "reguler":
        attendance_days = sum(1 for key in effective_date_keys(month, context) if key in present_all)
    else:
        attendance_days = sum(1 for key in present_all if str(key).startswith(month))
    missing_days = max(0, days - attendance_days)
    sum_late = js_round2(sum(js_or_zero(row.get("potKeterlambatan")) for row in records))
    sum_early = js_round2(sum(js_or_zero(row.get("potPulangCepat")) for row in records))
    sum_duration = js_round2(sum(js_or_zero(row.get("potDurasi")) for row in records))
    sum_incomplete = js_round2(sum(js_or_zero(row.get("potTidakLengkap")) for row in records))
    missing_deduction = js_round2(missing_days * js_number(context.config.get("potonganHariKurangPerHari")))
    if first.get("metode") == "durasi":
        included_missing = missing_deduction if attendance_days < days else 0
        total_raw = js_round2(sum_late + sum_early + sum_duration + included_missing)
    elif not records:
        # Undated roster entries have no daily penalty to duplicate.
        total_raw = missing_deduction
    else:
        total_raw = js_round2(sum_late + sum_early + sum_incomplete)
    total = min(100, total_raw)
    status = "Bersih"
    if total > 0:
        status = "Ada Potongan"
    if total_raw > 100:
        status = "Dibatasi 100%"
    return {
        "nip": first.get("nip"), "nama": first.get("nama"), "jabatan": first.get("jabatan"),
        "metode": first.get("metode"), "bulan": month, "jumlahRecord": len(records),
        "hariEfektif": days, "hariAdaAbsensi": attendance_days, "hariKurang": missing_days,
        "jumlahHariAbsen": len(date_keys), "potKeterlambatan": sum_late,
        "potPulangCepat": sum_early, "potDurasi": sum_duration,
        "potHariKurang": missing_deduction, "potTidakLengkap": sum_incomplete,
        "totalRaw": total_raw, "total": total, "dupDates": 0, "status": status,
    }


def compute_final_absensi(nip: str, name: str, job: str, month: str, gross: Any, attendance_rate: Any) -> dict[str, Any]:
    percentage = min(100, js_or_zero(attendance_rate))
    percentage = _compact_number(percentage)
    result: dict[str, Any] = {"nip": nip, "nama": name, "jabatan": job, "bulan": month}
    gross_number = js_number(gross)
    if gross is None or not math.isfinite(gross_number):
        result.update({"tukin": None, "potPct": percentage, "komponen": None, "nominal": None, "finalDibayar": None})
        return result
    gross_rounded = js_math_round(gross_number)
    component = js_math_round(gross_rounded * 0.30)
    nominal = js_math_round(component * (percentage / 100))
    result.update({"tukin": gross_rounded, "potPct": percentage, "komponen": component, "nominal": nominal, "finalDibayar": js_math_round(gross_rounded - nominal)})
    return result


def compute_legacy_sync(final: dict[str, Any], skp_rate: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    rate = js_or_zero(skp_rate)
    gross = js_or_zero(final.get("tukin"))
    attendance_nominal = js_or_zero(final.get("nominal"))
    skp_nominal = gross * 0.70 * (min(100, rate) / 100)
    final_skp = {**final, "skpPct": _compact_number(rate), "nominalSKP": _compact_number(skp_nominal), "finalDibayarSKP": _compact_number(gross - attendance_nominal - skp_nominal)}
    attendance_rate = js_or_zero(final.get("potPct"))
    nominal_attendance = gross * 0.30 * attendance_rate / 100
    nominal_skp = gross * 0.70 * rate / 100
    weighted = js_round2(attendance_rate * 0.30 + rate * 0.70)
    final_combined = {
        **final,
        "potonganAbsensiPct": _compact_number(attendance_rate),
        "nominalPotonganAbsensi": _compact_number(nominal_attendance),
        "potonganSKPPct": _compact_number(rate),
        "nominalPotonganSKP": _compact_number(nominal_skp),
        "totalPotonganPct": weighted,
        "totalNominalPotongan": _compact_number(nominal_attendance + nominal_skp),
        "finalDibayarKombinasiSKP": _compact_number(gross - nominal_attendance - nominal_skp),
    }
    return final_skp, final_combined


def _calculation_record(item: dict[str, Any]) -> dict[str, Any]:
    date = parse_date(item.get("date"))
    if not date:
        raise ShadowEngineError(f"Tanggal calculation case tidak valid: {item.get('id')}")
    check_in = parse_time(item.get("in"))
    check_out = parse_time(item.get("out"))
    zone = item.get("zone") or "WIB"
    out_date = add_days(date, 1) if item.get("cross_day") else date
    return {
        "_id": item["id"], "nip": item["nip"], "nama": item["id"], "jabatan": item.get("job"),
        "tanggal": date, "tanggalPulang": out_date["str"] if item.get("cross_day") else "",
        "tanggalMasukObj": date, "tanggalPulangObj": out_date,
        "masukMin": check_in, "pulangMin": check_out,
        "masukNormMin": _wrap(check_in, ZONE_OFFSET.get(zone, 0)),
        "pulangNormMin": _wrap(check_out, ZONE_OFFSET.get(zone, 0)),
        "masukNormMinAdd": _wrap(check_in, ZONE_OFFSET_ADD.get(zone, 0)),
        "pulangNormMinAdd": _wrap(check_out, ZONE_OFFSET_ADD.get(zone, 0)),
        "zona": zone, "provinsi": "", "kabupaten": "", "jenis": item.get("kind") or "",
        "crossDay": bool(item.get("cross_day")), "sourceFormat": item.get("source_format") or "standard",
        "shiftFlag": "SHIFT LINTAS HARI" if item.get("cross_day") else "",
        "issues": item.get("issues") or [], "dupFlag": bool(item.get("duplicate")),
    }


def calculate_fixture_case(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    employee_rows = [] if item.get("master_missing") else [{"nip": item["nip"], "nama": item["id"], "jabatan": item.get("master_job") or item.get("job"), "tukin": item.get("gross")}]
    context = build_context(config, employee_rows)
    mode = item.get("time_mode") or "original"
    record = _calculation_record(item)
    effective_in, effective_out = times_for_mode(record, mode)
    records = [record]
    if item.get("duplicate"):
        records.append({**record, "_id": item["id"] + "-duplicate"})
    daily = [compute_record(row, context, mode) for row in records]
    rekap = None
    rekap_error = None
    try:
        rekap = compute_rekap(daily, context)
    except DuplicateAttendanceError as error:
        rekap_error = str(error)
    attendance_rate = item.get("final_attendance_rate") if item.get("final_attendance_rate") is not None else (rekap.get("total") if rekap else None)
    final_absensi = None
    final_skp = None
    final_combined = None
    if attendance_rate is not None:
        final_absensi = compute_final_absensi(item["nip"], item["id"], item.get("job"), str(item["date"])[:7], item.get("gross"), attendance_rate)
        final_skp, final_combined = compute_legacy_sync(final_absensi, item.get("skp_rate"))
    return {
        "id": item["id"], "nip": item["nip"],
        "normalized_input": {
            "date": record["tanggal"]["key"], "check_in_minutes": record["masukMin"],
            "check_out_minutes": record["pulangMin"], "check_in_effective": effective_in,
            "check_out_effective": effective_out, "zone": record["zona"], "time_mode": mode,
            "cross_day": bool(item.get("cross_day")),
        },
        "daily": daily, "rekap": rekap, "rekap_error": rekap_error,
        "final_input": {
            "gross": item.get("gross"), "attendance_rate": attendance_rate,
            "skp_rate": item.get("skp_rate") if item.get("skp_rate") is not None else None,
            "skp_status": item.get("skp_status") or None,
        },
        "final_absensi": final_absensi, "final_skp": final_skp, "final_combined": final_combined,
    }


def calculate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    if fixture.get("contract_version") != BASELINE_ID:
        raise ShadowEngineError(f"Baseline tidak didukung: {fixture.get('contract_version')}")
    config = fixture["config"]
    return {
        "contract_version": fixture["contract_version"],
        "cases": [calculate_fixture_case(item, config) for item in fixture.get("cases") or []],
        "normalization_cases": [normalize_import_case(item) for item in fixture.get("normalization_cases") or []],
    }


def pair_wali_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure compatibility helper for the existing chronological <=18h Wali pairing."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event.get("nip", "")), []).append(dict(event))
    pairs: list[dict[str, Any]] = []
    for nip, rows in grouped.items():
        rows.sort(key=lambda row: _normalized_datetime(row["date"]).timestamp() // 60 + int(row["time"]))
        index = 0
        while index < len(rows):
            event = rows[index]
            following = rows[index + 1] if index + 1 < len(rows) else None
            start = _normalized_datetime(event["date"]).timestamp() // 60 + int(event["time"])
            elapsed = None
            if following:
                end = _normalized_datetime(following["date"]).timestamp() // 60 + int(following["time"])
                elapsed = int(end - start)
            paired = following if elapsed is not None and 0 <= elapsed <= 18 * 60 else None
            pairs.append({"nip": nip, "in": event, "out": paired, "elapsed_minutes": elapsed if paired else None})
            index += 2 if paired else 1
    return pairs


def merge_wali_work_dates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge Wali rows by NIP/work date using legacy earliest-in/latest-out rules."""
    groups: dict[str, dict[str, Any]] = {}
    for source in records:
        record = dict(source)
        key = f"{record.get('nip')}|{record['tanggal']['key']}"
        current = groups.get(key)
        if current is None:
            groups[key] = record
            continue
        incoming_in = record.get("masukMin")
        current_in = current.get("masukMin")
        if current_in is None and incoming_in is not None or incoming_in is not None and current_in is not None and incoming_in < current_in:
            current["masukMin"] = incoming_in
            current["masukAt"] = record.get("masukAt")
            current["tanggalMasukObj"] = record.get("tanggalMasukObj")
        incoming_out = record.get("pulangMin")
        if current.get("pulangMin") is None and incoming_out is not None or incoming_out is not None and str(record.get("pulangAt") or "") > str(current.get("pulangAt") or ""):
            current["pulangMin"] = incoming_out
            current["pulangAt"] = record.get("pulangAt")
            current["tanggalPulangObj"] = record.get("tanggalPulangObj")
        out_date = current.get("tanggalPulangObj")
        current["tanggalPulang"] = out_date["str"] if out_date else ""
        current["crossDay"] = bool(out_date and out_date["key"] != current["tanggal"]["key"])
        current["shiftFlag"] = "SHIFT LINTAS HARI" if current["crossDay"] else ("SHIFT NORMAL" if current.get("masukMin") is not None and current.get("pulangMin") is not None else "DATA TIDAK LENGKAP")
        current["issues"] = []
        if current.get("masukMin") is None:
            current["issues"].append("Jam masuk kosong")
        if current.get("pulangMin") is None:
            current["issues"].append("Jam pulang kosong")
    return sorted(groups.values(), key=lambda row: (row["tanggal"]["key"], str(row.get("nip") or "")))


def validate_source_fingerprint(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    differences = []
    actual = {}
    for relative, expected in {**BASELINE_SOURCE_SHA256, **IMPORT_SOURCE_SHA256_20261105}.items():
        path = root / relative
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() if path.exists() else None
        actual[relative] = digest
        if digest != expected:
            differences.append({"file": relative, "expected": expected, "actual": digest})
    return {"baseline": BASELINE_ID, "valid": not differences, "files": actual, "differences": differences}


def validate_golden_fingerprint(path: str | Path) -> dict[str, Any]:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return {"valid": digest == BASELINE_GOLDEN_SHA256, "expected": BASELINE_GOLDEN_SHA256, "actual": digest}


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        output: list[tuple[str, Any]] = []
        for key in sorted(value):
            output.extend(_flatten(value[key], f"{prefix}.{key}" if prefix else key))
        return output
    if isinstance(value, list):
        output = []
        for index, item in enumerate(value):
            output.extend(_flatten(item, f"{prefix}[{index}]"))
        return output
    return [(prefix, value)]


def _numeric_delta(reference: Any, candidate: Any) -> int | float | None:
    if isinstance(reference, (int, float)) and not isinstance(reference, bool) and isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        return candidate - reference
    return None


def compare_outputs(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    stages = {
        "NORMALIZATION": [(row["id"], row, next((item for item in candidate.get("normalization_cases", []) if item.get("id") == row["id"]), None)) for row in reference.get("normalization_cases", [])],
        "HARIAN": [], "REKAP": [], "FINAL_ABSENSI": [], "LEGACY_SYNC_PAYLOAD": [],
    }
    candidate_cases = {row.get("id"): row for row in candidate.get("cases", [])}
    for row in reference.get("cases", []):
        other = candidate_cases.get(row.get("id")) or {}
        case_id = row["id"]
        stages["HARIAN"].append((case_id, {"normalized_input": row.get("normalized_input"), "daily": row.get("daily")}, {"normalized_input": other.get("normalized_input"), "daily": other.get("daily")}))
        stages["REKAP"].append((case_id, {"rekap": row.get("rekap"), "rekap_error": row.get("rekap_error")}, {"rekap": other.get("rekap"), "rekap_error": other.get("rekap_error")}))
        stages["FINAL_ABSENSI"].append((case_id, {"final_input": row.get("final_input"), "final_absensi": row.get("final_absensi")}, {"final_input": other.get("final_input"), "final_absensi": other.get("final_absensi")}))
        stages["LEGACY_SYNC_PAYLOAD"].append((case_id, {"final_skp": row.get("final_skp"), "final_combined": row.get("final_combined")}, {"final_skp": other.get("final_skp"), "final_combined": other.get("final_combined")}))
    details: list[dict[str, Any]] = []
    summaries = []
    for stage, rows in stages.items():
        fields = matches = mismatches = 0
        for case_id, expected, actual in rows:
            expected_flat = dict(_flatten(expected))
            actual_flat = dict(_flatten(actual))
            for field in sorted(set(expected_flat) | set(actual_flat)):
                fields += 1
                left = expected_flat.get(field, {"__missing__": True})
                right = actual_flat.get(field, {"__missing__": True})
                if left == right and type(left) is type(right):
                    matches += 1
                elif isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool) and left == right:
                    matches += 1
                else:
                    mismatches += 1
                    details.append({
                        "case": case_id, "stage": stage, "field": field,
                        "js_value": left, "python_value": right,
                        "match": False, "delta": _numeric_delta(left, right),
                        "classification": "UNEXPLAINED_MISMATCH",
                    })
        summaries.append({"stage": stage, "cases": len(rows), "fields": fields, "matches": matches, "mismatches": mismatches})
    unexplained = sum(row["mismatches"] for row in summaries)
    return {
        "baseline": BASELINE_ID,
        "stages": summaries,
        "total_fields": sum(row["fields"] for row in summaries),
        "exact_matches": sum(row["matches"] for row in summaries),
        "mismatches": unexplained,
        "unexplained_mismatches": unexplained,
        "details": details,
        "classification": "EXACT_MATCH" if unexplained == 0 else "UNEXPLAINED_MISMATCH",
    }


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

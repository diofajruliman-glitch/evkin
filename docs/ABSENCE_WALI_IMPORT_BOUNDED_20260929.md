# Backend Import V1 — Bounded Wali Path

## Scope

This change bounds Wali Asuh/Wali Asrama event import only. Standard CSV,
Parquet, and XLSX parsing, calculation formulas, Reset, and production authority
are unchanged.

## Processing and parity

The backend streams nonblank source rows into import-scoped DuckDB temporary
tables in batches of 5,000. Duplicate source rows are counted and removed by a
DuckDB window over the source fingerprint, retaining the first source row as
the legacy importer does.

Normalized punches are stored as event rows. DuckDB orders them by NIP, UTC
calendar date, minute, source row, and in-before-out column order. Python reads
at most 8,192 sorted events per batch and holds only one pending event while
pairing. Adjacent events pair when their elapsed time is from zero through
18 hours inclusive. Empty-punch rows retain legacy source ordering ahead of
that NIP's punch timeline.

Pair results are inserted in batches of at most 5,000. A set-based DuckDB
aggregation then chooses the legacy representative row, earliest check-in,
and latest absolute checkout per NIP/work date. Invalid identity/date source
rows and malformed-time blocker codes remain visible in staging.

The frozen JS Wali runner and fixture cover ordinary day, cross-midnight,
exactly 18 hours, over 18 hours, duplicate input, repeated/interleaved NIPs,
multiple pairs, multiple dates, year boundary, empty/malformed punches, and
weekend/job classification. The backend normalized material comparison is
field-for-field with the frozen legacy result.

## Safety ceiling

`EVKIN_WALI_IMPORT_MAX_ROWS` defaults to `500000` source rows. It may be lowered
for a deployment, but values above the tested 500,000-row ceiling are rejected
as invalid configuration until a later capacity UAT raises the ceiling.
An input beyond the configured row limit fails staging with
`WALI_IMPORT_LIMIT_EXCEEDED`; it cannot be committed. The limit is checked while
reading the file, before event sorting, pairing, or canonical promotion.

The ceiling is based on the measured 500,000-row successful import (493.9 MB
peak RSS in the isolated benchmark) and a failed 1,000,000-row run during
development before this guard existed. The present guard rejects 1M and 2M
inputs before event processing, with no canonical writes. Split large files
only at boundaries that keep all punches for a NIP/work period together; do
not split a shift's check-in and checkout across files.

## UAT results

The report `../backend/reports/absence_import_wali_benchmark_20260929.json`
contains isolated temporary-DB benchmarks and the guarded 1M/2M rejection
measurements. The historical read-only database available for the previous
import parity run is marked `standard`, has a legacy canonical `raw_import`
schema, and does not contain the original Wali event source. Real-world Wali
parity therefore remains pending; no production source data was fabricated or
modified.

Production defaults remain `IMPORT_JS_LEGACY` and `JS_LEGACY`.

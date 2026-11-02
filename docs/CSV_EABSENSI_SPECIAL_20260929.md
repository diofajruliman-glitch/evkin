# CSV eAbsensi Special Import — 2026-09-29

## Scope and routing

`CSV_EABSENSI_SPECIAL` is an opt-in, source-selected path. It does not replace
the existing Excel importer, Standard Backend Import V1, or calculation
authority. The UI exposes three explicit modes:

| Operator mode | Input | Engine |
|---|---|---|
| Import Absensi Standard | CSV/Parquet | `IMPORT_BACKEND_V1` |
| Import Excel Existing | XLSX/XLSM/XLS | Existing JavaScript `processFile` import |
| Import CSV eAbsensi | CSV only | `CSV_EABSENSI_SPECIAL` / Python + DuckDB |

The special route requires `source_format=csv_eabsensi` and an enabled
`EVKIN_CSV_EABSENSI_SPECIAL_ENABLED` setting. The standard Windows launcher
enables this route by default; deployments that start the backend another way
must explicitly set the variable to `true`.
There is no routing based on job title or `jnshadir`; a non-CSV upload in the
special mode fails closed.

## Processing contract

The special parser requires `nip`, `tgl_finger`, `absen_pagi`, `absen_sore`,
and `jnshadir`. `NM_JABATAN` is optional metadata. Blank strings and text
`NULL` (case-insensitive and trimmed) produce no punch event. Empty-punch
source rows do not create attendance sessions. Invalid, non-empty punch text
remains visible as a blocker.

DuckDB reads and stages the CSV without materializing all source rows as a
Python list. It removes exact source duplicates, projects `CHECK_IN` /
`CHECK_OUT` events, deduplicates identical NIP/type/timestamp events, and sorts
by NIP, absolute datetime, source row, and event order. SQL performs the
session pairing and inserts the staged canonical rows set-wise. Staging keeps
full check-in/out datetimes in `normalized_json`; the canonical import contract
contains work date, check-in time, checkout time, checkout date, and source
format. `jnshadir` is retained in the canonical `jenis` field.

Same-day punches pair on their date. Cross-date pairing is constrained to a
20:00-or-later check-in, a chronological checkout no later than 14:00, and an
elapsed interval of at most 18 hours. The orphan morning-out cutoff is
centralized at `CSV_EABSENSI_MORNING_OUT_CUTOFF_MIN = 840` (14:00 inclusive).
This is derived from the existing 18-hour attendance pairing limit and the
explicit 20:00 night-check-in boundary (20:00 + 18 hours = 14:00); it is not a
claim that a representative production eAbsensi file was audited. A checkout
only in that window maps to the previous work date, with check-in left null.

The complete event set is paired before the operator selects a canonical
period. When a file contains multiple work periods, the UI keeps the staged
eAbsensi batch open and lets the operator preview, select one period, and
commit that same batch. A next-month/year checkout can therefore complete a
prior-month/year work date without creating a checkout-date attendance row. The existing
calculation source loader was exercised from a temporary database and received
the expected `tanggal=work_date`, `jamMasuk`, `jamPulang`, and `crossDay`.
No formula or calculation authority was changed.

## Golden and regression UAT

The isolated golden suite covers same-day and split-row attendance, 20:00 and
23:59:59 check-ins, cross-date checkout, orphan morning checkout, consecutive
night sessions, no phantom date from null punches, reversed source order,
duplicate events/source rows, missing punches, month/year boundaries,
`Upacara`, missing/null job title, interleaved employees, timestamp ties, and
one-row IN+OUT input.

`EXCEL_EXISTING_REGRESSION = 0`: the existing `assets/js/import.js` parser and
pairing code were not changed; the existing Excel mode calls that same
`processFile` path. The frozen legacy-import golden, Excel-time import test,
and JavaScript regression suite passed.

`STANDARD_CSV_REGRESSION = 0`: Standard CSV continues to submit
`source_format=standard`; the special parser is only selected by the explicit
eAbsensi mode. Frozen standard normalization comparisons and the backend
Standard Import V1 tests passed.

## Temporary-database performance UAT

The reproducible generator and measurements are in
`../backend/tools/benchmark_csv_eabsensi.py` and
`../backend/reports/csv_eabsensi_special_benchmark_20260929.json`. All runs
used isolated temporary DuckDB databases and exercised regular/split rows,
night sessions, consecutive sessions, orphan checkouts, missing punches,
duplicates, `Upacara`, and interleaved NIPs. Wall time includes file copy,
stage, and commit; `stage` includes parser/validation, SQL sort/pair, and
canonical preparation. RSS is process peak working set.

| Raw rows | Events after dedupe | Canonical rows | Stage (s) | Sort (s) | Pair (s) | Canonical prepare (s) | Total (s) | Rows/s | Peak RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100,000 | 112,500 | 70,833 | 3.265 | 0.785 | 0.293 | 0.994 | 4.409 | 22,681 | 397.8 MB |
| 500,000 | 562,491 | 354,164 | 17.756 | 6.101 | 1.740 | 4.922 | 21.071 | 23,730 | 456.6 MB |
| 1,000,000 | 1,124,976 | 708,321 | 42.146 | 15.754 | 4.751 | 10.854 | 48.363 | 20,677 | 483.3 MB |
| 2,000,000 | 2,249,949 | 1,416,640 | 120.157 | 49.543 | 16.199 | 30.450 | 132.785 | 15,062 | 523.0 MB |
| 2,500,000 | 2,812,436 | 1,770,817 | 168.748 | 67.589 | 26.219 | 40.877 | 186.136 | 13,431 | 546.2 MB |

Every measured batch reached `COMMITTED`. No production database was opened.
The 2.5M run satisfies the minimum raw-row capacity target in this environment.

## Authorities and decision

| Surface | Current authority |
|---|---|
| Standard import | `IMPORT_BACKEND_V1` |
| Excel Existing / Wali | `IMPORT_JS_LEGACY` |
| Calculation | `JS_LEGACY` |
| CSV eAbsensi Special | Disabled by default; opt-in only |

`CROSS_PERIOD_CHECKOUT_BLOCKER = NO` — verified with an August 31 check-in and
September 1 checkout in the staged and canonical source contract.

`CALCULATION_CONTRACT_BLOCKER = NO` — verified on temporary canonical data;
no formulas were changed.

Full regression completed: `478 passed` Python tests and `20 passed` JavaScript
test files. Existing deprecation warnings were reported by the suite.

**Decision: `READY_FOR_CSV_EABSENSI_SPECIAL_CANARY`**. This authorizes only a
controlled, opt-in canary after explicit operator/environment enablement; it
does not activate the special route in production. Standard, Excel, Wali, and
calculation authorities remain as shown above.

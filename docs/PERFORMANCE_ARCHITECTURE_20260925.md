# EVKIN Performance Architecture Audit — 2026-09-25

## Scope and safety

- Production operational data was not imported and operational reset was not run.
- Runtime measurements used `.codex-work/performance-baseline/baseline.duckdb`, a copy of the empty-operational database.
- Scale benchmarks used a fresh temporary DuckDB for every scale.
- Business formulas, canonical TUKIN workflow, Master SDM, Master Pengembalian, calculation snapshots, and Final TUKIN decisions were not changed.

## Baseline database

Physical file size: 418,394,112 bytes (399.01 MiB). DuckDB reports 3,671 blocks, 577 used and 3,094 free. This explains why the physical/high-water database remains large after reset even though operational tables are empty. No compaction was run on production.

Largest live row counts:

| Table | Rows | Approximate text payload | Retention |
|---|---:|---:|---|
| `sdm_change_request` | 587,264 | 138,752,914 chars | Master history; preserve |
| `master_data` | 32,626 | 17,021,289 chars | Master; preserve |
| `sdm_master` | 32,626 | 9,639,881 chars | Master; preserve |
| `sdm_assignment_history` | 32,626 | 9,570,616 chars | Master history; preserve |
| `sdm_tukin_profile_history` | 32,626 | 7,934,545 chars | Master history; preserve |
| `sdm_bank_history` | 32,626 | 7,401,426 chars | Master history; preserve |
| `sdm_active_history` | 32,626 | 5,579,046 chars | Master history; preserve |

All audited operational tables (`raw_import`, `absensi_raw`, `absensi_hasil`, `hitung_harian`, `rekap_sdm`, `final_absensi`, SKP/TUKIN/Pengembalian working tables) contained zero rows.

Copy-based backend baseline before the reusable read-catalog optimization:

| Measurement | Result |
|---|---:|
| Backend startup to health | 11.69 s (cold, affected by schema checks/host cache) |
| Main HTML | 71.84 ms cold / 19.75 ms warm |
| Workflow state | 739.24 ms cold / 752.46 ms warm |
| Empty workflow first page | 644.45 ms cold / 686.37 ms warm |
| Pengelolaan TUKIN annual page | 586.93 ms cold / 399.32 ms warm |
| Backend working set | 155,582,464 bytes |
| Backend private bytes | 490,852,352 bytes |

After retaining a process-local DuckDB catalog connection and using isolated cursors:

| Measurement | Result |
|---|---:|
| Backend startup to health | 21.66 s cold (variable; no startup improvement claimed) |
| Main HTML | 56.24 ms cold / 28.57 ms warm |
| Workflow state | 645.67 ms cold / 215.11 ms warm |
| Empty workflow first page | 93.87 ms cold / 73.76 ms warm |
| Pengelolaan TUKIN annual page | 492.40 ms cold / 158.39 ms warm |
| Backend working set | 203,087,872 bytes |
| Backend private bytes | 535,691,264 bytes |

The read cache trades about 45–48 MiB process memory for materially lower repeated query latency. Page response size remains bounded.

## Root causes found

1. `assets/js/state.js::ensureWorkflowDataForModule` requested `all:true` for Harian, Semi Final, Rekap, Final, and Export. `loadWorkflowSource` then paged until every row had been appended to browser arrays.
2. `state.raw`, `state.data`, `state.daily`, `state.rekap`, `state.semifinal`, and `state.final` are global arrays. The UI filtered/sorted those arrays and only sliced after all rows were resident.
3. Imported attendance was persisted twice: structured `raw_import` plus a per-row JSON copy in `workflow_records.record` (`processed_data`).
4. `TukinAnnualService.annual` loaded every final/SKP row for a year into pandas/Python, built all people, then sliced the requested page.
5. Several export, calculation, comparison, and legacy fallback paths still intentionally use `SELECT *` and materialize full filtered datasets. Those paths are listed under remaining bottlenecks.

## Implemented architecture

### Workflow list API

`GET /api/workflow/data/{source}` now supports `page`, `page_size` (maximum 500), compatibility `per_page`, `search`, `filter`, `sort`, and `sort_dir`. Search/filter/count/order/limit/offset execute in DuckDB. List projections exclude detail-only columns for raw, daily, recap, and final views. Every returned list row includes `_row_id`.

Response includes `rows`, `total`, `page`, `page_size`, `per_page`, and `total_pages`.

`GET /api/workflow/detail/{source}/{row_id}` fetches one complete row on demand.

`GET /api/workflow/summary/{source}` uses `COUNT`, `COUNT DISTINCT`, filtered `SUM/CASE`, and `GROUP BY` in DuckDB. It returns bounded total/valid/invalid/SDM/potongan/nominal/status aggregates.

No speculative index was added. Synthetic scans through 500,000 rows remained below 78 ms for summary and below 33 ms for page/search/filter queries; DuckDB's scan/pruning behavior was sufficient for these access patterns.

### Frontend memory

Opening Import, Harian, Semi Final, Rekap, and Export now loads only the first server page. Page metadata is kept in `state.workflowPages`. Full legacy hydration is deferred to an explicit calculation action; small (<=5,000 row) interrupted legacy imports retain automatic repair compatibility.

This removes the previous direct relationship between opening a workflow page and total operational row count. Existing calculation formulas are unchanged.

### Pengelolaan TUKIN

`TukinAnnualService.annual` now:

1. counts/filter/sorts distinct people in SQL;
2. fetches only the active page of NIPs;
3. fetches monthly detail only for those NIPs;
4. computes annual KPI totals in SQL.

Detail remains on demand through `/api/tukin/people/{nip}`. Export may still request all rows intentionally.

### Import and duplicate storage

The existing sync chunk is 7,500 rows (bounded to 5,000–10,000) and reports batch/row progress. New imports persist `raw_import` as the canonical source and clear `workflow_records` instead of writing a duplicate JSON record per row. Legacy `workflow_records` remains readable. Calculation records are reconstructed from canonical raw rows when required. Provenance columns already supported by list/storage (`batch_id`, `source_file`, `source_row`, `imported_at`) are retained where present.

### Reset audit safety

Operational inventory now assigns one explicit category before an action:

- `PRESERVE_MASTER`
- `PRESERVE_AUDIT`
- `DELETE_OPERATIONAL`
- `UNKNOWN` (blocks a populated reset)

Audit precedence is evaluated before operational prefixes. In particular, `skp_assessment_audit`, `skp_indicator_config_audit`, `pengembalian_period_audit`, all `*_audit` tables, `audit_log`, and `application_reset_audit` are preserved. No reset was executed against production.

## Full-table-load audit

Risky paths found and addressed:

| File/function | Previous risk | Result |
|---|---|---|
| `assets/js/state.js::loadWorkflowSource/ensureWorkflowDataForModule` | Load all pages into browser | Page-only on module open |
| `backend/evkin_export_api.py::get_workflow_data` | `SELECT *`, no search/filter/sort, max 1,000 | projected server page, max 500, server query features |
| `backend/tukin_annual.py::annual` | full-year pandas frames before slicing | SQL page + SQL aggregation |
| `assets/js/app.js::syncImportedWorkflow` | duplicate JSON `processed_data` | canonical raw only for new imports |

Still intentional/bounded or remaining:

- `read_workflow_table_*` and annual/final export paths materialize a filtered export dataset; exports should move to fully streaming writers for multi-million-row CSV/ZIP workloads.
- `compute_harian_pandas` has a legacy pandas fallback that reads a filtered source frame. The primary DuckDB compute path inserts in chunks.
- `final_tukin.records`, generic comparison, and selected Pengembalian archive/export functions load full data within an explicitly selected period/run/case. They are not list-page endpoints, but large exports/comparisons remain memory-sensitive.
- Browser calculation actions still hydrate complete legacy calculation inputs/results because the formulas remain in JavaScript. Opening pages is bounded, but fully eliminating browser hydration during calculation requires a separately validated formula-parity migration to backend jobs. This phase deliberately did not rewrite or change formulas.

## Synthetic benchmark

Fresh temporary DuckDB per scale; first page always 100 rows.

| Rows | Import | Processing/group | Summary | First page | Search | Filter | Detail | DB size |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 2531.40 ms* | 19.34 ms | 12.28 ms | 9.87 ms | 5.40 ms | 4.99 ms | 6.27 ms | 798,720 B |
| 50,000 | 349.56 ms | 193.89 ms | 52.05 ms | 17.66 ms | 10.22 ms | 10.35 ms | 5.53 ms | 1,585,152 B |
| 100,000 | 636.74 ms | 849.23 ms | 63.81 ms | 23.97 ms | 28.33 ms | 20.10 ms | 7.18 ms | 2,371,584 B |
| 500,000 | 5180.67 ms | 182.51 ms | 77.25 ms | 25.87 ms | 32.69 ms | 23.99 ms | 4.08 ms | 7,614,464 B |

`*` Includes cold Python/DuckDB/tracemalloc initialization; later scales are the meaningful steady-state comparison. Page/detail result cardinality stayed constant as total rows increased.

## Verification

- Python tests: 360 passed in three isolated groups (139 + 104 + 117).
- Focused performance/reset/workflow/TUKIN suite: 47 passed.
- JavaScript workflow tests: 12 passed.
- Python compilation and JavaScript syntax checks passed.
- Synthetic benchmark completed at 10k, 50k, 100k, and 500k.
- `git diff --check` passed.

No commit or push was made.

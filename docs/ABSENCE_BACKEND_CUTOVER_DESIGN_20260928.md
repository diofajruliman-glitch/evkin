# EVKIN Absence Backend Cutover Design — 2026-09-28

Decision target: `READY_FOR_CONTROLLED_PYTHON_CANARY`

The production default remains `JS_LEGACY`. This infrastructure does not activate Python canonical calculation.

## Authority

One backend setting, `EVKIN_ABSENCE_AUTHORITY`, accepts exactly:

- `JS_LEGACY` — current browser calculation and persistence remain authoritative.
- `PYTHON_SHADOW` — backend calculates and compares with persisted JS results, records a `SHADOW_ONLY` batch, and never publishes it.
- `PYTHON_CANONICAL` — backend calculation may publish atomically only when the separate `EVKIN_ABSENCE_CANONICAL_ENABLED=1` gate and frozen parity fingerprints are valid.

An invalid authority fails startup/config resolution. Python failure never invokes JavaScript automatically.

## Flow

```text
Browser command (period, actor, reason, X-EVKIN-Generation)
  -> POST /api/absence/calculate
  -> persistent PENDING batch
  -> backend worker
       -> generation check
       -> canonical raw_import + workflow_config + Master SDM
       -> source/input fingerprint
       -> RUNNING
       -> Python normalization/Harian/Rekap/Final in memory
       -> validate counts, keys, and period scope
       -> write gate
       -> generation recheck + source fingerprint recheck
       -> one DuckDB transaction
            replace period in hitung_harian
            replace period in rekap_sdm
            replace period in final_absensi
            update canonical pointer
            mark batch COMPLETED/CANONICAL
       -> COMMIT
  -> browser GET status/latest and hydrates canonical workflow tables
```

For `PYTHON_SHADOW`, the transaction that replaces business tables is skipped. Comparison totals and mismatch details use anonymized key hashes and are stored in batch timing/audit metadata.

## Schema and lifecycle

`absence_calculation_batch` stores calculation ID, period, authority, engine/version, reset generation, input and formula fingerprints, operator, rerun reason, predecessor, stage counts, comparison counts, timings, status, error code/message, and timestamps.

`absence_calculation_canonical` is the single active pointer per period. It can only be written in the same transaction that publishes all three business stages.

Lifecycle:

```text
PENDING -> RUNNING -> COMPLETED (canonical only)
                   -> SHADOW_ONLY
                   -> FAILED
                   -> STALE_SOURCE
                   -> STALE_GENERATION
```

Only `COMPLETED` plus `canonical=true`, a matching canonical pointer, and matching Final rows are accepted when Python canonical authority is active.

## Concurrency and recovery

- One active `PENDING/RUNNING` batch is permitted per period.
- Per-period locks allow calculations for different periods to execute independently.
- The existing global database write gate serializes only source capture/status writes and final persistence, not the CPU calculation phase.
- A reset-generation change makes the worker `STALE_GENERATION` before publication.
- A material raw/config/master change makes the worker `STALE_SOURCE` before publication.
- Reruns create a new auditable batch and atomically replace only the requested period; no uncontrolled duplicate result rows are appended.
- Startup marks abandoned `PENDING/RUNNING` batches `FAILED / PROCESS_RESTARTED`; no batch remains RUNNING forever.
- Browser closure does not own the job. Status remains queryable by calculation ID or latest period.

## Source contract and fingerprint

The browser supplies no calculation rows. Python reads:

- `raw_import` rows scoped by normalized period;
- singleton `workflow_config`, including calendar, rules, leave periods, and workflow time mode;
- relevant `master_data` identity/job/Tukin fields for NIPs in the period;
- frozen JavaScript formula hashes, frozen golden snapshot hash, Python engine source hash, and Python calculation version.

The fingerprint is recalculated after acquiring the persistence gate. A mismatch prevents all business-table writes.

## Period-atomic persistence

Existing rows outside the requested period are staged and preserved. Each stage is replaced with `UNION ALL BY NAME` inside one DuckDB transaction. Every new row receives `calculation_id`, `calculation_version`, and `calculation_engine`. A failure at any stage rolls back Harian, Rekap, Final, canonical pointer, and the batch completion transition together; the batch is then terminalized as failed in a separate audit write.

## Downstream Tukin

While authority is `JS_LEGACY` or `PYTHON_SHADOW`, existing Tukin behavior is unchanged. Under explicit `PYTHON_CANONICAL`, both combined-Tukin and annual live capture require a completed canonical pointer and Final rows belonging to that batch. `RUNNING`, `FAILED`, stale, or shadow results are rejected. No Tukin formula was changed.

## Frontend infrastructure

The existing authenticated request wrapper now exposes:

- `getAbsenceAuthority()`;
- `startAbsenceCalculation(period, actor, reason)`;
- `getAbsenceCalculation(calculationId)`.

Generation headers and stale-reset handling remain centralized. These helpers are intentionally not wired as the default Harian button path in this stage. Controlled canary/UAT can call them explicitly. A future canonical UI switch will use the same helpers, poll status after reload, hydrate `daily_data`, `rekap_sdm`, and `final_absensi`, and must not call the legacy JS calculation or sync its old state.

## Rollback

Rollback is a recorded configuration action:

```text
EVKIN_ABSENCE_AUTHORITY=PYTHON_CANONICAL
  -> EVKIN_ABSENCE_AUTHORITY=JS_LEGACY
  -> backend restart/reload configuration
```

Calculation history and canonical metadata remain auditable. JavaScript files remain installed. There is no silent per-request fallback: failed Python calculation remains failed until an operator explicitly changes authority or starts a new batch.

## API

- `GET /api/absence/authority`
- `POST /api/absence/calculate`
- `GET /api/absence/calculation/{calculation_id}`
- `GET /api/absence/calculation/latest?period=YYYY-MM`

The POST returns a persistent PENDING batch immediately. GET responses include status, authority/version, fingerprints, counts, timings, duration, errors, and shadow mismatch summary when applicable.

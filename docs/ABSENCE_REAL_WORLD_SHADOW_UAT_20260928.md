# EVKIN Absence Real-World Shadow UAT — 2026-09-28

Baseline: `ABSENCE_JS_BASELINE_2026_09`

Decision: `READY_FOR_BACKEND_CUTOVER_DESIGN`

Production calculation authority remains `JAVASCRIPT`. This UAT did not perform a cutover.

## Safety and source

- Source: existing reset-backup DuckDB snapshot, copied byte-for-byte to a temporary directory before UAT.
- Source access: read-only; all calculation and comparison ran from the temporary copy.
- Source database SHA-256: `a34675d2d75297083ff63f006f302057c1873b0fc97a8de0f1283b154d457081`.
- Temporary frozen input was anonymized before either engine ran. NIP values were replaced with salted HMAC identifiers, names were replaced, and unit-work values were removed.
- No production workflow table was mutated.

## Dataset

| Metric | Value |
|---|---:|
| Period | 2026-08 |
| Raw / normalized / Harian rows | 36,965 / 36,965 / 36,965 |
| Unique SDM | 1,563 |
| Unique dates | 31 |
| Rekap / Final rows | 1,563 / 1,563 |
| Missing check-in | 131 |
| Missing check-out | 1,291 |
| Both punches missing | 0 |
| Cross-day rows | 4,808 |
| Wali Asuh/Asrama rows | 34,047 |
| Late / early rows | 86 / 38 |
| Flexi-compensated rows | 158 |
| Holiday/weekend rows | 9,050 |
| Duplicate / invalid rows | 0 / 0 |

Only WIB was present. WITA, WIT, duplicate, invalid, excused/leave, and both-punches-missing were not fabricated; their compatibility remains covered by the frozen synthetic golden suite.

## Identical input gate

Frozen, JavaScript, and Python all reported the same canonical input fingerprint:

`78fa5cf92138e4485a48b4071237a944dbaf1ad008057981e04667ac0b3d60fe`

The runner fails closed if these three values differ.

## Field-level parity

| Stage | Rows | Fields | Exact matches | Mismatches |
|---|---:|---:|---:|---:|
| Normalization | 36,965 | 592,862 | 592,862 | 0 |
| Harian | 36,965 | 1,287,361 | 1,287,361 | 0 |
| Rekap | 1,563 | 29,697 | 29,697 | 0 |
| Final Absensi | 1,563 | 14,067 | 14,067 | 0 |
| Legacy sync contract | 1,563 | 18,756 | 18,756 | 0 |
| Legacy sync combined | 1,563 | 25,008 | 25,008 | 0 |
| **Total** |  | **1,967,751** | **1,967,751** | **0** |

`UNEXPLAINED_MISMATCH = 0`

WIB: 36,965 rows, 0 mismatches. Wali Asuh/Asrama: 34,047 rows, 0 mismatches. Cross-day: 4,808 rows, 0 mismatches.

## Repeatability and frozen baseline

- JavaScript real-world run 1 equals run 2: PASS.
- Python real-world run 1 equals run 2: PASS.
- JavaScript formula source fingerprints: PASS.
- Frozen golden snapshot fingerprint: PASS (`15ac3c67be87436f8ea16867cf1ead1c467c7ddbcdfbde6f89bfaf0ec9f664d7`).
- Golden parity: 8,491 / 8,491 fields PASS.

## Performance observation

These are single-machine observations, not a performance guarantee. The JavaScript Rekap stage includes the production UI chunk/yield behavior.

| Stage | JavaScript | Python shadow |
|---|---:|---:|
| Load | included in runner startup | 76 ms |
| Normalization | 907 ms | 781 ms |
| Harian | 198 ms | 584 ms |
| Rekap | 19,368 ms | 168 ms |
| Final | 2 ms | 8 ms |
| Legacy sync contract | 6 ms | <1 ms |
| Calculation total | 20,481 ms | 1,617 ms |
| Process wall time | 20,749 ms | 1,969 ms |
| Python serialization | n/a | 282 ms |

Second-run wall time was 20,402 ms for JavaScript and 2,086 ms for Python. Node reported maximum RSS of approximately 210,608 KiB. Python peak RSS was not instrumented; no memory error or abnormal growth was observed.

## Regression

- Python targeted shadow tests: 7 PASS.
- Full Python suite: 418 PASS, 0 FAIL (63 pre-existing deprecation/user warnings).
- Frontend/Node test files: 18 PASS, 0 FAIL.
- Python compilation, JavaScript syntax, and `git diff --check`: PASS.

No production JavaScript calculation file was changed for this UAT. Changes are limited to the Python shadow import-normalization compatibility path, read-only UAT tooling, a VM-based JavaScript reference runner, regression coverage, and this report.

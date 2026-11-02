# EVKIN Absensi — Audit Formula JavaScript & Golden Baseline

Tanggal baseline: 28 September 2026  
Identifier: `ABSENCE_JS_BASELINE_2026_09`  
Git HEAD saat audit: `03a097959e2b2d6120238f4b59f8e4504bf97166`  
Authority: JavaScript legacy tetap calculation authority; belum ada Python replacement engine.

## Executive conclusion

`READY_FOR_PYTHON_SHADOW_ENGINE`

Mesin aktif dapat dibekukan menjadi kontrak terukur. Golden baseline berisi 76 kasus kalkulasi, 15 kasus normalisasi, 77 output Harian, 75 Rekap, dan 75 Final Absensi. Dua eksekusi input identik menghasilkan struktur JSON identik. Snapshot mencakup seluruh field runtime, bukan hanya total akhir.

Ada perilaku legacy yang harus dipertahankan atau diputuskan secara eksplisit sebelum cutover: jam masuk sama dengan jam pulang berarti 1.440 menit; kompensasi flexi mensyaratkan pulang `>` batas; parser tanggal menerima tanggal kalender semu seperti 31 Februari pada pola eksplisit; `Math.round` berbeda dari Python `round`; persentase negatif tidak di-clamp di Final; formula nominal pada tampilan Final dan payload sync tidak selalu identik.

## Calculation inventory

| Fungsi | File:line | Input | Proses/output | Caller |
|---|---|---|---|---|
| `excelSerialToDate` | `assets/js/utils.js:15` | serial Excel | UTC day → `{y,m,d}` | `parseDate` |
| `parseDate` / `makeDate` | `assets/js/utils.js:23`, `:52` | Date, serial, string | tanggal canonical, key/monthKey | import, calendar, engine |
| `dow` / `isWeekend` / `addDays` | `assets/js/utils.js:62`, `:67`, `:69` | date object | weekday UTC, weekend, rollover | Harian, Rekap, runner |
| `normalizeExcelTime` | `assets/js/utils.js:82` | Excel fraction/string | `HH:mm:ss` atau kosong | import, `parseTime` |
| `parseTime` / `fmtTime` | `assets/js/utils.js:120`, `:130` | raw time/minutes | integer minute; second >=30 rounds up | import, calc, edit |
| `durationMin` / `fmtDur` | `assets/js/utils.js:137`, `:144` | minute in/out | duration, wrap +1 day saat `<=0` | `computeRecord` |
| `round2` / `fmtRupiah` | `assets/js/utils.js:151`, `:156` | numeric-like | coercion + `Math.round` | UI/aggregate |
| `detectZone` + offset maps | `assets/js/data.js:41-53` | province/explicit zone | WIB/WITA/WIT, -60/-120 and reverse | import |
| `defaultConfig` | `assets/js/state.js:26` | built-in master | schedules, flexi, tiers, defaults | startup/normalizer |
| `normalizeConfig` | `assets/js/state.js:61` | partial config | validated/fallback config | every calc context |
| `effectiveAuto` / `effectiveDays` | `assets/js/config.js:7`, `:23` | month/config | weekday minus holiday/manual | Rekap |
| `detectColumns` | `assets/js/import.js:39` | headers | alias → field indexes | standard import |
| `processSingleRow` | `assets/js/import.js:72` | raw row/map | date/time/zone/issues/cross-day | import batches |
| `mergeStandardSplitRecords` | `assets/js/import.js:172` | normalized rows | combine missing halves by NIP/date | standard import |
| `mergeWaliShiftRecords` | `assets/js/import.js:194` | Wali rows | chronological pairing up to 18h | standard special detection |
| `parseRows` / `buildRecords` | `assets/js/import-wali.js:150`, `:225` | Wali AOA/events | dedupe source + chronological shift pairing | Wali importer |
| `makeRecord` / `mergeSameWorkDate` | `assets/js/import-wali.js:198`, `:249` | Wali events | shift flags, earliest-in/latest-out | Wali importer |
| `buildContext` | `assets/js/calc.js:10` | config/master | holiday, jabatan, employee, no-cut maps | Harian |
| `resolveJabatan` / `methodOf` | `assets/js/calc.js:30`, `:37` | record/context | canonical Master job; duration/regular | `computeRecord` |
| `inLeavePeriod` | `assets/js/calc.js:42` | NIP/date/config | inclusive leave match | `computeRecord` |
| `tierPct` | `assets/js/calc.js:51` | tiers/minutes | first `minutes <= max` | regular penalties |
| `durasiTierPct` | `assets/js/calc.js:56` | hours/minimum | duration-band percentage | duration jobs |
| `timesForMode` | `assets/js/calc.js:67` | record/mode | original, WIB subtract, WIB add | `computeRecord` |
| `computeRecord` | `assets/js/calc.js:81` | normalized record/context/mode | full Harian row | `harian.process`, golden runner |
| calc `round2` | `assets/js/calc.js:212` | number | 2-decimal `Math.round` | Harian/Rekap |
| `harian.process` | `assets/js/harian.js:8` | `state.data`, server Master | batches 500, calls engine, persists/syncs | Harian UI/Semi Final |
| `recomputeDup` / `recalcChain` | `assets/js/semifinal.js:10`, `:17` | state rows | duplicate flags + rerun Harian | edit/delete Semi Final |
| `rekap.process` | `assets/js/rekap.js:13` | verified Daily/config | group/month sums/calendar/cap | Rekap UI |
| `masterTukinOf` / `pairWorkflowTukin` | `assets/js/final.js:164`, `:185` | NIP/backend Master | authoritative gross lookup | Final |
| `final.process` / private `recalc` | `assets/js/final.js:198`, `:214` | Rekap + gross | 30% attendance component/nominal/pay | Final UI |
| `combinedView` | `assets/js/final.js:709` | Final + source percentages + SKP | 30/70 view formula | Final combined tab |
| `syncBackendWorkflow` | `assets/js/app.js:81` | all workflow state | rebuilds final SKP/combined payload | every persistence stage |

## Dependency map

```text
file/CSV/XLSX or backend raw_import
  -> import.js / import-wali.js normalization
  -> EVKIN.state.data (browser memory; canonical raw persisted to DuckDB)
  -> harian.process -> calc.computeRecord
  -> EVKIN.state.daily / daily_data
  -> Semi Final duplicate review/edit/delete/verification
  -> rekap.process
  -> EVKIN.state.rekap / rekap_sdm
  -> final.process + backend Master Tukin lookup
  -> EVKIN.state.final / final_absensi
  -> app.syncBackendWorkflow payload
  -> tukin_combined reads final_absensi.potPct
  -> immutable calculation snapshot / Final Tukin
```

| Tahap | Input | Engine | Output | Persisted | Recalculation |
|---|---|---|---|---|---|
| Import | file/raw backend | `import.js`, `import-wali.js` | normalized attendance | raw canonical in DuckDB; runtime state | yes on import/hydration |
| Harian | `state.data` + backend Master + config | `calc.computeRecord` | Daily rows | DuckDB `daily_data`/`hitung_harian`; cache legacy | every Run; all imported months |
| Semi Final | Daily + edit/delete | `recomputeDup`, `harian.process` | revalidated Daily | derived sync | edit/delete reruns Harian only |
| Rekap | verified Daily | `rekap.process` | NIP+month aggregate | `rekap_sdm` | explicit process/rerun |
| Final Absensi | Rekap + server Master gross | `final.recalc` | attendance nominal/pay preview | `final_absensi` | process/render/edit gross |
| Tukin | `final_absensi.potPct`, SKP, Master | backend `calculate_tukin_amounts` | calculation snapshot | canonical Tukin tables | separate backend calculation |

IndexedDB is persistence/cache only and is not read by any formula in this dependency path.

## Input normalization contract

| Raw | Normalized behavior | Evidence |
|---|---|---|
| `null`, `undefined`, `""`, whitespace time | empty → `null` minute | `utils.js:23-24`, `:82-101`, `:120-127` |
| numeric `0` or string `"0"` | `00:00:00` → minute 0 | `utils.js:89-113` |
| `HH:mm`, `HH.mm`, optional seconds | canonical `HH:mm:ss`; invalid range → empty | `utils.js:92-101` |
| seconds 00–29 / 30–59 | minute unchanged / +1 | `utils.js:127` |
| Excel time fraction | rounded seconds modulo one day | `utils.js:104-113` |
| Excel datetime number >=1 | integer date discarded, fractional clock retained; integer-only is empty | `utils.js:106-110` |
| Excel date serial >=60 | UTC date from epoch offset 25569 | `utils.js:15-19`, `:28-35` |
| explicit yyyy-mm-dd / dd-mm-yyyy / dd-mm-yy | direct field extraction | `utils.js:36-41` |
| other date string | browser `new Date(string)` local getters | `utils.js:42-45` |
| explicit WITA/WIT | subtract 60/120 in WIB mode; add in reverse mode | `data.js:41-53`, `calc.js:67-77` |
| output time outside 0..1439 | modulo 1440 | `utils.js:130-133`, `import.js:118-121` |
| check-out date later or clock-out < clock-in | `crossDay=true` | `import.js:110-112` |
| missing NIP or both dates invalid | row rejected; raw retained | `import.js:100-115` |
| duplicate standard NIP/date | `dupFlag`; rows remain | `semifinal.js:10-14` |
| missing Master | daily falls back record identity/job; Final gross stays unavailable | `calc.js:30-39`, `final.js:164-175` |

Important quirk: explicit date patterns only validate month 1..12 and day 1..31, not actual calendar validity (`utils.js:48-49`). Python must reproduce this or the contract must deliberately reject it before cutover.

## Harian formula specification

Branch order is material (`calc.js:134-208`): leave period → no-deduction kind → holiday/OFF → incomplete punch → duration/regular formula.

### Regular attendance

- Monday–Thursday schedule 07:30–16:00; Friday 07:30–16:30 (`state.js:37-40`).
- `late = max(0, actualIn - standardIn)` and `early = max(0, standardOut - actualOut)` (`calc.js:179-186`).
- Both use inclusive upper tier: 1–30 min=0.5%; 31–60=1%; 61–90=1.5%; 91–120=2%; >=121=3% (`state.js:46-50`, `calc.js:51-54`).
- Daily total is additive and rounded to two decimals; there is no daily cap (`calc.js:208-213`).

### Missing punches and OFF days

- Either punch missing on a scheduled day produces only 3%, not late/early plus 3 (`calc.js:161-168`).
- Both missing resolves to `LUPA_ABSEN_MASUK` because the in-punch check wins (`calc.js:164`).
- Weekend/configured holiday is OFF and 0%, including incomplete punches (`calc.js:153-159`).
- Leave period and configured no-deduction kind are 0% and `excused=true` (`calc.js:134-150`).

### Flexi time

- Eligible check-in interval is inclusive 07:31..08:00 (`state.js:41-45`, `calc.js:188-198`).
- Compensation is 16:30 Monday–Thursday and 17:00 Friday.
- Both late and early penalties become zero only when checkout is strictly greater than compensation. Checkout exactly at compensation does not qualify.
- Flexi does not change `durasiMin`; it only zeroes late/early variables.

### Duration jobs

For configured duration roles (`calc.js:56-64`, `:173-176`):

| Actual duration | Deduction |
|---|---:|
| >=7h30 | 0% |
| 7h00..7h29 | 0.5% |
| 6h30..6h59 | 1% |
| 6h00..6h29 | 1.5% |
| 5h30..5h59 | 2% |
| <5h30 | 3% |

The configured minimum is the anchor; lower boundaries are `min - 0.5/1/1.5/2` hours. There is no separate runtime `shortageMinutes` output; the golden contract records actual `durasiMin` and `potDurasi`.

## Rekap specification

- Requires Daily and `semifinalVerified`; duplicate NIP+date aborts the whole run before replacing old Rekap (`rekap.js:13-34`).
- Group key is `month|NIP` (`rekap.js:18-24`).
- Auto effective dates are Mon–Fri excluding configured holidays; manual mode uses configured count (`config.js:7-25`, `rekap.js:48-59`).
- Attendance is any row with at least one punch; excused dates and configured leave dates count present (`rekap.js:65-86`).
- Regular counts presence only on effective-date set. Duration jobs count all present dates in month (`rekap.js:88-94`).
- `hariKurang=max(0,effective-present)`; displayed `potHariKurang=round2(hariKurang*rate)` (`rekap.js:94-102`).
- Regular total = late + early + incomplete. It excludes duration and missing-day deduction (`rekap.js:110-113`).
- Duration total = late + early + duration + missing-day deduction only if present < effective. It excludes incomplete (`rekap.js:104-109`).
- Aggregate columns and total use `round2`; published `total=min(100,totalRaw)` with no lower clamp (`rekap.js:98-116`).

Rekap does not recalculate the Harian thresholds. It aggregates Daily fields. Calendar-day construction is duplicated between `config.effectiveAuto` and the effective-date set in Rekap.

## Final Absensi and downstream contract

Final formula (`final.js:214-232`):

```text
tukin = Math.round(gross)
component30 = Math.round(tukin * 0.30)
potPct = min(100, Number(rekap.total) || 0)
nominal = Math.round(component30 * potPct / 100)
finalDibayar = Math.round(tukin - nominal)
```

Missing/invalid gross preserves percentage but sets gross/component/nominal/payment to null. Negative percentage is not lower-clamped.

| Field | Type/equivalence | Source | Meaning | Used by |
|---|---|---|---|---|
| `nip` | exact string | workflow/Master | employee key | all Tukin joins |
| `nama`, `jabatan` | exact string | backend Master fallback Rekap | identity/display | snapshot/export |
| `bulan` | exact `YYYY-MM` | Rekap | assessment period | Tukin filter |
| `potPct` | exact numeric | Rekap `total` | attendance reduction rate | `tukin_combined.py:132`, `:268-277`; `tukin_annual.py:802` |
| `tukin` | exact integer/null | Master | legacy gross preview | compatibility/fallback |
| `komponen` | exact integer/null | Final | 30% attendance component | legacy view/sync |
| `nominal` | exact integer/null | Final | attendance deduction | legacy view/export |
| `finalDibayar` | exact integer/null | Final | legacy gross minus attendance deduction | legacy view/export |

Canonical backend Tukin recalculates from gross, `potPct`, and SKP (`backend/tukin_combined.py:52-75`); only `nip`, period, and `potPct` are the material Absensi-to-Tukin calculation contract. The remaining fields remain exact compatibility/output fields.

## Existing shift and Wali behavior

- Generic duration wraps a checkout earlier than or equal to check-in by +1,440 minutes (`utils.js:137-141`). It supports one implicit midnight crossing only.
- Wali importer sorts events per NIP and pairs the next event when elapsed is 0..18 hours inclusive (`import-wali.js:225-246`). At 18h+1, the first event is incomplete and a new record starts.
- Multiple Wali records on one work date merge to earliest check-in and latest absolute checkout (`import-wali.js:249-284`).
- `sourceFormat='wali'` controls NORMAL/LINTAS_HARI display status, but Master/configured jabatan controls whether duration formula is used (`calc.js:83-88`, `:170-176`).
- Weekend/holiday Wali is OFF and receives no deduction, even with punches.
- There is no Master Shift schedule, rotating roster, off-shift calendar, required monthly effective-hour accumulator, timezone-aware instant/DST model, or multi-day duration beyond the one-day wrap. Those are future features and are not part of this baseline.

## Duplicated calculation logic

| Rule | Location A | Location B | Same behavior? |
|---|---|---|---|
| 2-decimal round | `calc.js:212` | `utils.js:156` | same for ordinary finite numeric inputs; utils coerces first |
| timezone modulo | `import.js:118-121` | `semifinal.js:246-252` | same |
| effective weekdays/holidays | `config.js:7-20` | `rekap.js:48-59` | same source config; one returns count, one builds key set |
| Wali event pairing/merge | `import.js:194-240` | `import-wali.js:225-284` | same 18h concept, separate input paths and not byte-identical |
| Final attendance nominal | `final.js:214-232` | `app.js:94-121` | base Final row reused, but SKP/combined nominal is recalculated in app |
| combined 30/70 formula | `final.js:721-748` | `app.js:109-121` | **different rounding**: view rounds components/nominals, sync payload retains floats |
| Final Tukin 30/70 | JS Final/app | `tukin_combined.py:52-75` | backend Decimal/integer and complementary 70%; must be compared explicitly |

Semi Final does not contain a second Harian formula; it deliberately calls `harian.process` (`semifinal.js:17-31`). Rekap does not recompute late/early/duration bands.

## Numeric, null, and date/time risks for Python

| Risk | Current JS behavior | Python concern | Required golden test |
|---|---|---|---|
| half rounding | `Math.round(x)` ties toward +infinity | `round` is bankers | money ending .5, negative .5 |
| binary float | normal IEEE-754 Number | Decimal changes intermediate values | 30/70 payload case Rp1,000,002 |
| truthy/falsy | `Number(x)||0`; empty/null/NaN →0 | `None`, empty, NaN differ | null/blank/invalid/zero |
| `null` vs missing | `undefined` often same branch as null | dict missing/None split | missing properties |
| negative rate | Final caps only upper bound | validators may reject/lower-cap | negative rate contract test before cutover |
| date parsing | explicit regex plus browser parser | `datetime.fromisoformat` is stricter | all formats + malformed |
| invalid calendar date | 31-day range only for explicit patterns | Python constructor rejects | 31-Feb fixture if compatibility required |
| Date object timezone | local getters | backend timezone may differ | fixed TZ CI matrix |
| Excel epoch/fraction | UTC epoch and rounded seconds | library epoch/1904 option | serial + half-second boundaries |
| midnight | `out<=in` adds 24h | naïve subtraction gives <=0 | equal time, 23:59→00:00 |
| timezone | fixed minute offsets and modulo | aware datetime changes dates | WIB/WITA/WIT modes |
| duplicate ordering | source order retained until named sorts | dict/group order may differ | duplicate and ordering snapshot |
| string sorting | browser `localeCompare` for Rekap names | locale/collation differs | accented/mixed-case names before cutover |
| integer division | all JS arithmetic floating | Python `/` okay, `//` not | every duration boundary |
| NaN serialization | JSON converts NaN to null | Python JSON rejects by strict policy | malformed numeric paths |

Equivalence policy: dates, times, integer minutes, booleans, flags/status/category, NIP/IDs, percentages, Rekap fields, and integer Rupiah are exact match. No tolerance is allowed for money. A float tolerance may only be introduced for an explicitly identified legacy sync float field, field-by-field, after recording the exact JS binary result; it must never be a blanket tolerance.

## Golden dataset and output

Files:

- Input: `backend/tests/fixtures/absence_js_golden_cases_2026_09.json`
- Frozen output: `backend/tests/fixtures/absence_js_golden_output_2026_09.json`
- Runner: `backend/tests/legacy_calculation_runner.cjs`
- Regression: `backend/tests/test_absence_js_golden.cjs`

| Category | Cases |
|---|---:|
| check-in tiers | 11 |
| checkout tiers | 11 |
| flexi boundaries | 7 |
| duration thresholds | 15 |
| missing punches | 3 |
| shift/midnight/year rollover | 5 |
| WIB/WITA/WIT modes | 4 |
| Wali Asuh/Asrama/Master override | 4 |
| data quality/duplicate/missing Master | 6 |
| leave/no-cut/holiday | 3 |
| calendar month/leap/year | 4 |
| Final/money/null/cap | 3 |
| normalization-only | 15 |

Total calculation inputs: 76 across 76 synthetic SDM and six periods. One duplicate case creates 77 Daily rows and intentionally prevents its Rekap/Final; output is 75 Rekap and 75 Final rows. Recorded material fields include normalized input minutes, effective timezone minutes, all runtime Daily fields (`durasiMin`, `_lateMin`, `_earlyMin`, every deduction, status, flags), complete Rekap, Final Absensi, Final+SKP sync payload, and combined sync payload.

No production or operational database sample was read. No approved anonymized real-world fixture was present. The safe substitute is the synthetic UAT-style sample above; therefore real-world distribution parity remains an explicit Shadow Engine/UAT task, not a hidden assumption in this baseline.

## Repeatability and hashes

Two fresh Node VM runs matched the 279,455-byte golden JSON exactly. Regression compares parsed structures twice and to the frozen snapshot.

Key SHA-256 hashes (current working-tree content, because the repository was already dirty before this audit):

| Artifact | SHA-256 |
|---|---|
| `calc.js` | `d3fcce342a32222417bbe6843355284c3973d783a254199893e5205e8b545c69` |
| `utils.js` | `891a4417df038c1f5f892de4b7240603df72bd7aed02fd3300e7c10b1b6681a0` |
| `import.js` | `6f3cc8251f502a466b16078debb407e39f80c53b29843e2f2d85c32f6374b4ec` |
| `import-wali.js` | `4576b67eccc41487808a7721caaaa0febc0955b40e5ad2ac26c6ebaa12525b20` |
| `rekap.js` | `581d1a651d6e463b6ddbe0e816a6f284e39be45d2c264ef4523f5f4ed45ae240` |
| `final.js` | `3edc360107986872db8a6becf7d4da35c40937b5cb740e3f06ff600fcbae01d1` |
| golden input | `73e5ddf2be3f94ce0afb488d526573f8c7990a0117b5b8a2759f1a1afe7ff9a2` |
| golden output | `15ac3c67be87436f8ea16867cf1ead1c467c7ddbcdfbde6f89bfaf0ec9f664d7` |

## Benchmark baseline

Node v26.7.0, Windows x64, pure synchronous `computeRecord`, after 1,000-row warm-up:

| Records | Time | Throughput | Approx. heap delta |
|---:|---:|---:|---:|
| 1,000 | 9.165 ms | 109,105/s | -183,608 B (GC noise) |
| 10,000 | 99.346 ms | 100,658/s | 3,755,288 B |
| 35,000 | 300.748 ms | 116,377/s | 11,066,608 B |

This is a calculation microbenchmark, not end-to-end import/persistence/render time. `computeRecord` is synchronous, while `harian.process` yields to the event loop every 500 records (`harian.js:23-31`), so UI blocking occurs in bounded batches rather than one uninterrupted 35k loop.

## Python shadow-engine port scope

Port in this order without connecting it to production authority:

1. `utils.js` date/time/Excel parsing, wrap, duration, formatting-relevant normalization.
2. `data.js` timezone detection/offsets and config normalization in `state.js`.
3. standard/Wali normalization and 18-hour pairing semantics.
4. `calc.js` context resolution, branch ordering, regular tiers, flexi, duration tiers, and exact output fields.
5. `config.effectiveDays` plus `rekap.process` aggregation/duplicate hard-fail/cap.
6. `final.js` attendance calculation as one explicit contract, plus a separately named compatibility contract for `app.js` sync floats.
7. comparison-only adapter emitting the same JSON structure and exact field comparison.

JavaScript remains authority until the Python shadow output reaches `ZERO UNEXPLAINED MISMATCH` on the frozen dataset and an approved anonymized real-world sample.

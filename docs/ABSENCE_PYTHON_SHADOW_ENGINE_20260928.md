# EVKIN Absensi — Python Shadow Engine

Tanggal: 28 September 2026  
Baseline: `ABSENCE_JS_BASELINE_2026_09`  
Status authority: `JAVASCRIPT`  
Status Python: shadow/comparison only

## Architecture

Shadow engine berada di `backend/absence_shadow_engine.py` dan tidak diimpor oleh
router, endpoint, worker, atau persistence production.

```text
frozen input fixture
  ├─> legacy_calculation_runner.cjs ─> frozen JS golden
  └─> absence_shadow_engine.py ─────> Python candidate
                                      ↓
                              compare_outputs()
                                      ↓
                  exact match / unexplained mismatch
```

Pembagian fungsi:

- JS compatibility: `js_number`, `js_math_round`, `js_round2`.
- Date/time: `parse_date`, `normalize_excel_time`, `parse_time`, `duration_minutes`.
- Import normalization: `normalize_import_case`, timezone detection dan modulo.
- Harian: `compute_record` dengan urutan branch legacy.
- Rekap: `effective_date_keys`, `effective_days`, `compute_rekap`.
- Final view: `compute_final_absensi`.
- Legacy sync payload: `compute_legacy_sync`, terpisah dari view contract.
- Wali: `pair_wali_events`, `merge_wali_work_dates`.
- Audit: source/golden fingerprint dan `compare_outputs`.

## Compatibility behavior

Shadow meniru perilaku legacy, termasuk:

- `Math.round` ties menuju positif, bukan Python bankers rounding.
- explicit `YYYY-MM-DD` menerima day 1..31 walaupun tanggal kalender tidak nyata.
- detik >=30 menaikkan integer menit.
- Excel datetime memakai hanya fractional clock untuk kolom waktu.
- check-out <= check-in menambah 1.440 menit; equal time berarti 24 jam.
- timezone memakai fixed offset dan modulo, bukan timezone-aware instant.
- branch Harian tetap leave → no-deduction → OFF → incomplete → formula.
- duplicate NIP/date tetap hard-fail Rekap.
- Final view integer Rupiah dan payload sync float dipertahankan sebagai dua contract.

## Golden parity

| Stage | Cases | Fields | Exact match | Mismatch |
|---|---:|---:|---:|---:|
| Normalization | 15 | 528 | 528 | 0 |
| Harian | 76 | 3,379 | 3,379 | 0 |
| Rekap | 76 | 1,502 | 1,502 | 0 |
| Final Absensi | 76 | 980 | 980 | 0 |
| Legacy sync payload | 76 | 2,102 | 2,102 | 0 |
| **Total** | **91 unique fixture cases** | **8,491** | **8,491** | **0** |

`UNEXPLAINED_MISMATCH = 0`

Tidak ada mismatch yang diklasifikasikan `EXPLAINED_COMPATIBILITY`; seluruh field
yang dibandingkan merupakan `EXACT_MATCH`.

## Fingerprint protection

Engine menyimpan SHA-256 file JavaScript formula dan SHA-256 frozen golden output.
CLI menandai `baseline_stale_warning=true` dan exit code 2 bila salah satu berubah.
Snapshot tidak diregenerasi otomatis.

Current result:

- source fingerprint: valid;
- golden fingerprint: valid;
- baseline stale warning: false.

## Real-world parity

`REAL_WORLD_PARITY_PENDING`

Tidak ada export anonim, salinan database, atau fixture real-world yang disetujui.
Tidak ada data operasional yang dibaca atau dimodifikasi. Kondisi ini tidak
menghalangi implementasi shadow, tetapi merupakan blocker sebelum Python dapat
dipertimbangkan sebagai calculation authority.

## Performance

Microbenchmark murni `computeRecord`/`compute_record`, setelah warm-up:

| Engine | Records | Time | Records/s |
|---|---:|---:|---:|
| JavaScript Node v26.7 | 1,000 | 9.165 ms | 109,105 |
| Python 3.14.7 | 1,000 | 21.163 ms | 47,253 |
| JavaScript Node v26.7 | 10,000 | 99.346 ms | 100,658 |
| Python 3.14.7 | 10,000 | 231.094 ms | 43,272 |
| JavaScript Node v26.7 | 35,000 | 300.748 ms | 116,377 |
| Python 3.14.7 | 35,000 | 794.220 ms | 44,068 |

Python shadow sekitar 2.3–2.6 kali lebih lambat pada benchmark ini. Penyebab utama
yang terlihat adalah loop row-by-row, pembuatan dictionary output lengkap, parsing
dan formatting berulang. Tidak ada database round-trip dalam angka tersebut.
Optimasi berikutnya dapat mem-preparse config/schedule dan mengurangi object copy,
tetapi hanya setelah real-world parity; formula dan urutan branch tidak boleh diubah.

## Validation

- Python shadow targeted: 6 passed.
- Cross-language JS runner == frozen golden: passed.
- Python shadow == frozen golden: 8,491/8,491 fields.
- Full Python: 417 passed, 0 failed, 63 existing warnings.
- Node/frontend: 18/18 test files passed.
- No production endpoint or frontend dependency added.

## Decision

`READY_FOR_SHADOW_REAL_WORLD_UAT`

Production flow remains:

```text
Browser JavaScript → Harian → Rekap → Final Absensi → existing persistence
```

Python is not a production calculation authority and cannot be promoted until an
approved real-world sample also reaches `ZERO UNEXPLAINED MISMATCH`.

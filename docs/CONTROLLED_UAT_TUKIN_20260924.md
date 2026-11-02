# Controlled UAT / Shadow Reconciliation TUKIN

Tanggal audit: 24 September 2026  
Periode: `2026-08`  
Mode: `SHADOW_READ_ONLY`

## Definisi populasi

Master SDM adalah master referensi, bukan target perhitungan periodik. Service membangun
processing scope dari NIP yang benar-benar muncul pada import/workflow assessment. Untuk
periode ini tidak ada baris assessment/import SKP yang tersimpan, sehingga digunakan
`Final Absensi` sebagai fallback paling traceable dan gap linkage dilaporkan eksplisit.
Fallback ini tidak membuat calculation dan tidak mengubah formula.

| Populasi | Jumlah |
| --- | ---: |
| Master population | 32.626 |
| Processing/import population | 1.563 |
| Assessment Final/Approved | 0 |
| Final Absensi dalam processing scope | 1.563 |
| TUKIN eligible | 0 |
| Calculated | 0 |

## Legacy comparable population

| Populasi legacy | Jumlah |
| --- | ---: |
| Legacy total | 32.527 |
| Legacy in-scope/comparable | 1.563 |
| Legacy out-of-scope | 30.964 |

`LEGACY_OUT_OF_SCOPE` tidak dibandingkan nominalnya dengan canonical. Pegawai Master yang
tidak masuk processing scope juga bukan `MISSING_CANONICAL`.

## Status dan blocker

| Status | Jumlah |
| --- | ---: |
| MATCH | 0 |
| EXPLAINED_DIFFERENCE | 0 |
| UNEXPLAINED_DIFFERENCE | 0 |
| MISSING_ASSESSMENT | 1.563 |
| MISSING_ATTENDANCE | 0 |
| MISSING_MASTER | 0 |
| MISSING_CANONICAL | 0 |
| DUPLICATE | 0 |
| SOURCE_INVALID | 0 |
| STALE | 0 |
| OUT_OF_SCOPE | 30.964 |

Blocker utama adalah workflow readiness: `SKP Approved = 0` dan linkage batch/workflow
assessment belum tersedia. Karena assessment belum FINAL/APPROVED, engine tidak membuat
calculation dan tidak mengklasifikasikan 1.563 NIP sebagai `MISSING_CANONICAL`.

## Nominal

Nominal legacy yang dilaporkan service hanya berasal dari 1.563 NIP comparable. Nilai ini
belum menjadi acceptance nominal karena tidak ada eligible/canonical calculation dan otoritas
legacy masih `UNVERIFIED`. Legacy 30.964 NIP out-of-scope dikeluarkan dari totals.

## Kesimpulan asumsi engine lama

Jalur rekonsiliasi lama **salah apabila** memakai seluruh Master SDM atau seluruh legacy
sebagai target TUKIN periode. Perbedaan 32.626 Master versus processing population 1.563
adalah perbedaan cakupan yang sah, bukan error. Perbaikan service kini memisahkan:

- `MASTER_POPULATION`
- `PROCESSING_POPULATION`
- `FINAL_ASSESSMENT_POPULATION`
- `FINAL_ATTENDANCE_POPULATION`
- `TUKIN_ELIGIBLE_POPULATION`
- `CALCULATED_POPULATION`
- `LEGACY_TOTAL`, `LEGACY_IN_SCOPE`, `LEGACY_OUT_OF_SCOPE`

## Verdict readiness

**FAIL - NEEDS INVESTIGATION**, bukan karena calculated lebih kecil dari Master, melainkan
karena workflow assessment belum siap, source batch belum terlacak, eligible population
belum terbentuk, dan authority legacy belum terverifikasi. UAT dapat diulang setelah batch
Penilaian SKP diimport/workflow, status assessment FINAL/APPROVED tersedia, Final Absensi
terikat ke batch/periode yang sama, lalu calculation snapshot dibuat.

## Keselamatan

- Database utama dibuka `read_only=True`.
- Reconciliation tidak menjalankan DDL, calculation, Final Decision, payment, atau update.
- Tidak membuat SKP Approved massal, Final Absensi palsu, atau menyalin legacy menjadi canonical.
- Tidak ada perubahan database produksi, commit, atau push.

API untuk mengulang:

- `GET /api/tukin/reconciliation/periods`
- `POST /api/tukin/reconciliation/run`
- `GET /api/tukin/reconciliation/detail`
- `POST /api/tukin/reconciliation/export`

Payload minimum:

```json
{"periode":"2026-08","comparison_source":"final_tukin_results"}
```

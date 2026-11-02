# Controlled UAT Source Audit

Tanggal: 24 September 2026  
Mode: read-only audit; tidak ada calculation, approval, payment, atau production write.

## 1. Source SKP yang ditemukan

| Source | Periode | NIP distinct | Status/evidence | Final/Approved | Batch/source ID | Canonical assessment |
|---|---|---:|---|---:|---|---|
| `skp_assessment_monthly` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `tukin_skp_monthly` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_results` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_import_batch` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_import_batches` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_import_source` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_meeting_batch/member` | none | 0 | none | 0 | none | Tidak aman: kosong |
| `skp_assessment_audit` | `2026-08` | 32.483 | `IMPORT`, `IMPORT_FINAL`, `APPROVE` | Evidence only | audit row IDs, no assessment batch linkage | Tidak aman sebagai canonical |

`skp_assessment_audit` memiliki histori `IMPORT_FINAL` pada 32.476 NIP dan `APPROVE`
pada 100 NIP, dengan timestamp 8-12 September 2026 dan actor `EVKIN`. Ini adalah bukti
provenance yang berguna, tetapi bukan source canonical yang dapat dipakai menghitung karena
row assessment aktif, `skp_import_batch`, dan source batch aktif kosong. Nilai SKP tidak
direkonstruksi dari audit atau nominal legacy.

File/template yang ditemukan:

- `assets/data/contoh_skp_import.csv`: contoh saja, bukan data UAT.
- `docs/SKP_FINAL_IMPORT.md`: mendokumentasikan jalur Final SKP yang sah.
- `backend/skp_assessment.py` dan migration 036: schema dan workflow source yang seharusnya
  menjadi canonical setelah import yang dapat ditelusuri.

## 2. Kandidat processing scope

Urutan evaluasi yang digunakan:

1. explicit workflow/batch population;
2. SKP assessment/import batch;
3. TUKIN calculation/import batch;
4. Final Absensi;
5. Master SDM tidak pernah dipakai sebagai scope otomatis.

Temuan periode `2026-08`:

| Candidate source | NIP | Quality | Status |
|---|---:|---|---|
| explicit workflow population | 0 teridentifikasi | UNKNOWN | Tidak ada tabel population eksplisit |
| SKP assessment/import batch | 0 | UNKNOWN | Semua tabel kosong |
| `final_tukin_results` calculation batch `510a6ac6-ad53-4d17-8a22-9d67d1c2d02b` | 32.527 | TRACEABLE | Ada batch, version/status/timestamp; bukan bukti official approval |
| `final_absensi` | 1.563 | FALLBACK | Scope yang saat ini dipakai engine |
| Master SDM | 32.626 | tidak berlaku | Referensi saja |

Engine reconciliation saat ini tetap menggunakan `Final Absensi` fallback untuk UAT aktif.
Scanner audit melaporkan calculation/import batch sebagai kandidat scope yang lebih traceable,
tetapi tidak menaikkan kualitasnya menjadi `AUTHORITATIVE`.

## 3. Period scan

Database saat ini hanya memiliki periode Tukin/SKP yang dapat dipindai berikut:

| Period | Master | Processing candidate | Scope source | Quality | Final Absensi | SKP available | SKP Final/Approved current | Eligible estimate | Legacy in-scope | Legacy source | Legacy quality |
|---|---:|---:|---|---|---:|---:|---:|---:|---:|---|---|
| `2026-08` | 32.626 | 32.527 | TUKIN calculation/import batch | TRACEABLE | 1.563 | 0 | 0 | 0 | 32.527 | `final_tukin_results` | TRACEABLE_LEGACY |

Catatan: angka processing `1.563` pada Controlled UAT aktif adalah scope operasional
`Final Absensi` fallback. Angka `32.527` adalah kandidat scope yang ditemukan dari calculation
batch legacy dan belum boleh diperlakukan sebagai official workflow population.

Intersection eligible tetap 0 karena current `SKP Final/Approved = 0`. Histori audit tidak
dihitung sebagai eligible current.

## 4. Legacy verification

| Source | Metadata | Verification |
|---|---|---|
| `final_tukin_results` | calculation batch ID, version, statuses `FINALIZED/HOLD`, created/calculated timestamp 11 Sep 2026 | `TRACEABLE_LEGACY`, bukan `VERIFIED_OFFICIAL` |
| `tukin_final_monthly` | tabel tersedia tetapi kosong | `NOT_COMPARABLE` |
| official export/approval reference | tidak ditemukan | `UNVERIFIED` |

Legacy tidak dijadikan baseline resmi. Nominal comparison hanya boleh dilakukan setelah
pemilik proses memberikan export resmi atau bukti penetapan yang dapat diverifikasi.

## 5. Ranking kandidat

- **BEST DATA COVERAGE:** `2026-08`, karena memiliki Final Absensi 1.563, legacy 32.527,
  dan evidence histori SKP audit; eligible current tetap 0.
- **BEST PROVENANCE:** `2026-08` hanya pada level `TRACEABLE`, bukan authoritative, karena
  calculation batch memiliki ID/version/timestamp dan audit SKP memiliki actor/timestamp.
  Tidak ada periode dengan source canonical approved yang lengkap.
- **CURRENT UAT PERIOD:** `2026-08`.
- **Kandidat utama nominal UAT:** belum ada. `2026-08` adalah kandidat investigasi terbaik,
  bukan periode yang siap calculation/payment.

## 6. Jawaban khusus 2026-08

Processing aktif berjumlah 1.563 karena engine memakai Final Absensi sebagai fallback ketika
tidak menemukan assessment/import batch yang aktif. Tidak ditemukan source scope yang
`AUTHORITATIVE`. Ditemukan calculation batch legacy yang `TRACEABLE`, tetapi bukan bukti
workflow official. Tidak ditemukan SKP canonical yang sah pada tabel aktif; audit histori
menunjukkan evidence `IMPORT_FINAL/APPROVE`, namun tidak dapat menggantikan row assessment
dan batch lineage.

Periode ini **belum layak dilanjutkan untuk nominal UAT**. Karena SKP Approved current tetap
0, tidak boleh ada calculation.

## 7. Migration dan file

Migration diperlukan hanya untuk memulihkan/menyediakan linkage source yang hilang:
assessment row aktif, import batch, dan lineage workflow population. Migration tidak boleh
menciptakan nilai SKP dari legacy atau mengubah formula.

File yang berubah pada fase ini:

- `backend/tukin_uat_audit.py`: scanner read-only source/period/intersection.
- `backend/tests/test_tukin_uat_audit.py`: regression coverage.
- `docs/CONTROLLED_UAT_SOURCE_AUDIT_20260924.md`: laporan audit ini.

## 8. Test

- Audit + reconciliation targeted tests: **12 passed**.
- Full suite setelah helper audit ditambahkan: **352 passed**.
- `git diff --check`: **PASS**.

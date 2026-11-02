# Hardening Final TUKIN Canonical — 24 September 2026

## Flow setelah hardening

`tukin_final_decision` adalah source of truth Final baru. Setiap keputusan menunjuk satu
`tukin_calculation_batch.id`, sedangkan nominal per NIP dibaca tanpa kalkulasi ulang dari
`tukin_calculation_result`.

Alurnya:

`Final Decision -> Calculation Snapshot -> Absensi Final + SKP Approved -> Master SDM`

Approval memerlukan calculation berstatus `CALCULATED`, mempunyai baris siap Final, dan
fingerprint sumber yang masih current. Perubahan sumber menghasilkan indikator `STALE` dan
memerlukan calculation version baru. Keputusan Final lama dan nominal snapshot lama tidak
diubah.

## Jalur legacy yang ditemukan

- `POST /api/final-tukin/preview` dahulu membaca `rekap_sdm`, Master SDM, dan SKP terbaru,
  lalu menjalankan formula Final sendiri.
- `POST /api/final-tukin/finalize` dahulu menulis `final_tukin_batches/results`.
- `GET /api/final-tukin/batches`, `/results/{id}`, `/export/{id}`, dan
  `POST /archive/{id}` dahulu mengelola snapshot Final legacy.
- `assets/js/final-tukin.js` dahulu sepenuhnya menggunakan endpoint tersebut.
- `/api/tukin/*` masih menggunakan `tukin_final_monthly` untuk histori tahunan, import
  historis, dan compatibility reporting.

## Compatibility setelah hardening

- `/preview` menjadi read-only adapter ke calculation snapshot terbaru.
- `/finalize` menjadi adapter approval; `preview_signature` lama diperlakukan sebagai
  `calculation_id`, bukan signature hasil kalkulasi ulang.
- `/batches` dan `/results/{id}` membaca calculation canonical.
- `/export/{id}` hanya mengekspor calculation yang sudah mempunyai Final Decision canonical.
- `/archive/{id}` mengembalikan HTTP 410 karena snapshot canonical immutable.
- Endpoint lama diberi metadata/deklarasi deprecated.
- Fungsi dan tabel `final_tukin_batches/results` tetap ada untuk histori serta regression
  evidence, tetapi UI dan route nominal baru tidak menulisnya.

## `tukin_final_monthly`

Tabel ini tetap dipertahankan sebagai histori legacy dan dependency laporan tahunan. Jika
suatu periode sudah memiliki Final Decision canonical, endpoint ekspor bulanan lama otomatis
mengekspor Final Decision + calculation snapshot. Payment lewat status periode legacy juga
ditolak untuk periode canonical; payment harus memakai Final Decision.

Periode historis tanpa Final Decision masih dapat dibaca/diekspor melalui compatibility path.
Fallback ini tidak membuat Final Decision baru dan tidak mengalahkan data canonical.

## Versioning dan payment safety

Final v1 tetap immutable ketika source berubah atau calculation v2 dibuat. Selama v1 belum
`PAID`, v2 yang current dapat disetujui sebagai Final baru; v1 tetap tercatat dan ditampilkan
sebagai superseded secara derivatif. Hanya keputusan Final terbaru pada periode tersebut yang
dapat ditandai `PAID`. Setelah `PAID`, approval version baru dan downgrade ditolak.

## UI dan export

UI Final menampilkan calculation version, bruto, komponen/potongan Absensi dan SKP,
adjustment, total potongan, Tukin dibayar, status calculation/Final, calculated/approved/paid
timestamp, serta source ID/batch/version. `SOURCE UPDATED / SNAPSHOT STALE` ditampilkan tanpa
mengubah transaksi historis.

Export Final memiliki sheet `FINAL DECISION` dan `FINAL TUKIN`. Sheet metadata memuat decision
ID, calculation ID/version, status, actor, timestamp, payment reference, serta source status.

## Remaining risk

- Method kalkulasi legacy internal tetap disimpan untuk histori dan direct regression tests;
  tidak ada route/UI Final baru yang memanggilnya. Kandidat removal setelah masa deprecation.
- `tukin_final_monthly` masih dipakai laporan dan import histori tahunan. Migrasi laporan
  tahunan penuh perlu fase terpisah agar histori lama tidak rusak.
- FK fisik belum ditambahkan; integritas calculation/decision saat ini dijaga transaction dan
  validasi service.
- UAT rekonsiliasi satu periode produksi nyata masih wajib sebelum rollout pembayaran.

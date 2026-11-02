# Integrasi Canonical Absensi, SKP, dan TUKIN

Tanggal: 24 September 2026

## Keputusan arsitektur

Alur canonical baru bersifat additive dan tidak menghapus jalur histori:

```text
sdm_master + histori sdm_*
          │
          ├── final_absensi (NIP + assessment_period)
          │
          └── skp_assessment_monthly APPROVED (NIP + assessment_period)
                         │
                         ▼
             tukin_combined (validasi + mesin 70/30)
                         │
                         ▼
       tukin_calculation_batch / tukin_calculation_result
                         │
                         ▼
              tukin_final_decision FINAL
                         │
                         ▼
              tukin_final_decision PAID
```

`NIP + periode YYYY-MM` adalah satu-satunya key integrasi. Nama, unit, urutan Excel, dan nomor baris tidak dipakai untuk memasangkan sumber.

## Source of truth

| Domain | Canonical source | Catatan |
|---|---|---|
| Identitas dan bruto | `sdm_master` + tabel histori `sdm_*` | Snapshot efektif per periode melalui `SharedMasterService` |
| Absensi final | `final_absensi` | Adapter menambahkan status `FINAL`, source ID/batch, dan finalized timestamp bila tersedia |
| SKP final | `skp_assessment_monthly` dengan `review_status='APPROVED'` | `tukin_skp_monthly` hanya histori/rule streak, bukan sumber approval |
| Calculation | `tukin_calculation_batch` + `tukin_calculation_result` | Immutable per `calculation_id`, version bertambah saat hitung ulang |
| Final TUKIN | `tukin_final_decision` | Menunjuk tepat satu `calculation_id` dan version; tidak menyimpan hasil hitung ulang |
| Pembayaran | `tukin_final_decision.status='PAID'` | Hanya transisi dari `FINAL`; tidak ada downgrade biasa |

## Mesin hitung

Fungsi canonical adalah `backend.tukin_combined.calculate_tukin_amounts`:

- komponen Absensi = 30% Tukin bruto;
- komponen SKP = sisa komplementer 70% agar rekonsiliasi rupiah selalu tepat;
- potongan Absensi = komponen Absensi × persen potongan Absensi;
- potongan SKP = komponen SKP × persen potongan SKP;
- hasil = bruto − kedua potongan;
- pembulatan tetap `ROUND_HALF_UP` ke rupiah.

Preview Pengelolaan TUKIN dan compatibility calculator Final memakai helper yang sama. Formula Absensi upstream tidak diubah.

Klasifikasi SKP canonical berada di `backend.skp_assessment`:

- `classify_skp_report_status` untuk status laporan;
- `canonical_skp_reduction_rate` untuk predikat/streak.

`tukin_annual` dan `final_tukin` menggunakan helper tersebut sehingga urutan klasifikasi tidak lagi berbeda.

## Readiness

Endpoint `GET /api/tukin/combined/readiness?assessment_period=YYYY-MM` mengembalikan:

- jumlah Master SDM;
- jumlah Absensi Final;
- jumlah SKP Approved;
- mismatch;
- siap dihitung;
- calculation terbaru (`CALCULATED` atau `STALE`);
- status Final (`DRAFT`, `FINAL`, atau `PAID`).

Mismatch tetap terlihat tetapi tidak memperoleh status snapshot `READY` dan tidak dianggap nominal siap Final.

## Snapshot dan lineage

`POST /api/tukin/combined/calculate` membuat version baru dan tidak mengubah version lama. Setiap hasil menyimpan:

- `calculation_id`, version, dan versi mesin hitung;
- NIP + periode;
- bruto, rate/nominal Absensi, rate/nominal SKP, adjustment, total, dan dibayar;
- versi Master SDM;
- Absensi source ID dan batch ID;
- SKP source ID, source version, batch ID, dan approved timestamp di JSON snapshot;
- fingerprint sumber dan timestamp perhitungan.

Perubahan sumber setelah calculation membuat status readiness calculation menjadi `STALE`. Snapshot lama dan Final yang sudah menunjuknya tidak berubah.

## Final dan pembayaran

`POST /api/final-tukin/approve-calculation` memeriksa bahwa snapshot masih current lalu membuat keputusan `FINAL` yang terkunci. Endpoint ini tidak menjalankan formula.

`POST /api/final-tukin/decisions/{id}/paid` hanya menerima keputusan `FINAL`, mewajibkan petugas dan referensi pembayaran, lalu mengubah status menjadi `PAID`. Pemanggilan ulang atau downgrade ditolak.

Ekspor alur baru memakai `GET /api/tukin/combined/calculations/{calculation_id}/export` dan membaca JSON snapshot tersimpan.
Ekspor untuk proses pembayaran memakai `GET /api/final-tukin/decisions/{decision_id}/export`; endpoint ini hanya menerima keputusan berstatus `FINAL` atau `PAID`.

## Backward compatibility dan batas fase

- `tukin_final_monthly`, import histori, dan endpoint Final legacy tidak dihapus agar data/API lama tetap dapat dibaca.
- UI Pengelolaan TUKIN diarahkan ke alur canonical baru.
- Jalur legacy `/api/final-tukin/finalize` sudah menjadi compatibility adapter deprecated ke approval calculation canonical; jalur ini tidak lagi menjalankan formula atau menulis snapshot legacy baru.
- Foreign key fisik belum ditambahkan karena DuckDB lama dan tabel historis perlu diaudit sebelum constraint diberlakukan. Integritas referensi dijaga oleh service dan regression test pada fase ini.
- Snapshot tidak mengubah atau menulis balik `final_absensi` maupun `skp_assessment_monthly`.

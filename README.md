# EVKIN ABSENSI v3.1

Dokumentasi database arsip pengembalian: [Master Pengembalian V2](docs/MASTER_PENGEMBALIAN_V2.md).
### Perhitungan Potongan Kehadiran dan Tukin ASN Berbasis Absensi

Aplikasi web **lokal/offline** (HTML5 + CSS3 + JavaScript vanilla + SheetJS) untuk menghitung
potongan kehadiran/Tunjangan Kinerja (Tukin) ASN dari data absensi. Workflow menggunakan
browser dan **backend lokal DuckDB** untuk menyimpan data dan hasil perhitungan.

---

## 1. Cara Menjalankan Aplikasi

1. Ekstrak paket ZIP.
2. Atur token lokal sebelum menjalankan aplikasi (PowerShell):
   ```powershell
   $env:EVKIN_API_KEY = '<token-lokal-yang-kuat>'
   ```
3. Jalankan **`RUN_EVKIN_HTTP.bat`** untuk memulai backend dan membuka aplikasi melalui HTTP lokal. Launcher meneruskan token ke tab EVKIN melalui fragmen URL, menyimpannya hanya di `sessionStorage`, lalu langsung membersihkan fragmen dari address bar.
4. Pastikan indikator **Backend aktif** sebelum menjalankan workflow. Dependensi Python harus tersedia (lihat `backend/requirements.txt`).
   - Library Excel (SheetJS) dan ikon (Lucide) sudah **dibundel offline** di folder `assets/vendor/`.
5. Konfigurasi dan hasil workflow disimpan di **DuckDB backend**. Refresh memuat kembali state dari backend.

### Setup Python reproducible

Gunakan Python 3.14 dan environment lokal terpisah:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt
```

Untuk development dan test, gunakan `backend\requirements-dev.txt`, yang juga memasang dependency runtime.
Dependency langsung dan transitif dikunci oleh `backend\requirements-lock.txt`:

```powershell
.\.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
```

### Quality checks

Fast regression Python lokal yang sama dengan CI dapat dijalankan dengan:

```powershell
.\.venv\Scripts\python backend\tools\run_fast_python_tests.py
```

CI Windows memeriksa dependency pinned, registry migration,
Python compile, security/reset/schema-sync/pengembalian/core workflow, seluruh test frontend,
dan memastikan runtime database/log/export/`.env` tidak ikut di-track. Full Python regression
tersedia melalui GitHub Actions `workflow_dispatch` dengan opsi `full_regression`.

Sebelum menjalankan migration, validasi registry tanpa menyentuh database:

```powershell
py -3.14 backend\migrations\migrate.py --validate-only
```

Nama migration baru wajib mengikuti `NNN_description.py` dan nomor versinya wajib unik.
Collision historis yang dipertahankan validator dibatasi pada pasangan nama file yang eksplisit.

Konfigurasi keamanan opsional:

- `EVKIN_ALLOWED_ORIGINS`: daftar origin eksplisit dipisahkan koma. Default mengikuti `EVKIN_HTTP_PORT` untuk `127.0.0.1` dan `localhost`.
- `EVKIN_ALLOW_INSECURE_LOCAL_DEV=1`: bypass autentikasi yang hanya boleh digunakan secara eksplisit untuk test/development. Jangan gunakan dengan data riil.
- `EVKIN_ANALYTICS_API_KEY`: nama lama yang masih diterima sebagai fallback bila `EVKIN_API_KEY` belum diatur; migrasikan ke `EVKIN_API_KEY`.

> Struktur:
> ```
> index.html
> assets/
>   css/style.css
>   js/*.js         (modul aplikasi)
>   vendor/         (xlsx.full.min.js, lucide.min.js — offline)
>   data/           (contoh data)
> README.md
> ```

---

## 2. Workflow Wajib (berurutan, saling bergantung)

```
IMPORT DATA → KONFIGURASI → HITUNG HARIAN → SEMI FINAL → REKAP SDM → FINAL → EXPORT EXCEL
```

Setiap tahap **terkunci (🔒)** sampai tahap sebelumnya selesai. Status pada sidebar & stepper:
- ✓ Hijau = selesai/valid
- ⚠ Kuning = perlu verifikasi
- 🔒 Abu-abu = terkunci
- Merah = error/anomali

---

## 3. Format Data Sumber (Excel / CSV)

Kolom minimal (nama kolom fleksibel, dikenali otomatis):

| Kolom | Wajib | Keterangan |
|------|:----:|-----------|
| NIP | ✓ | Nomor induk pegawai |
| Nama | ✓ | Nama pegawai |
| Tanggal | ✓ | Tanggal kerja (dd/mm/yyyy, yyyy-mm-dd, atau serial Excel) |
| Jam Masuk | ✓ | HH:MM (boleh kosong → dihitung 3%) |
| Jam Pulang | ✓ | HH:MM (boleh kosong → dihitung 3%) |
| Hari | – | Opsional (dihitung otomatis dari tanggal) |
| Zona | – | WIB/WITA/WIT (opsional, dideteksi dari Provinsi) |
| Provinsi | – | Untuk deteksi zona waktu |
| Kabupaten | – | Informasi |
| Jenis / Keterangan | – | mis. Sakit, Cuti Tahunan, Tugas Kedinasan |
| Tanggal Pulang | – | Untuk CSV lintas hari (opsional) |
| Jabatan | – | Bisa dari sini atau dari Master Pegawai |

**CSV lintas hari:** Jika absensi melewati tengah malam (mis. masuk 10/08 22:00, pulang 11/08 06:00),
tanggal kerja tetap **tanggal jam masuk (10/08)**, durasi dihitung lintas tengah malam (8 jam).
Record tidak dipindahkan ke tanggal 11.
Jika jam masuk dan jam pulang lengkap serta pulang pada hari berikutnya, potongan harian **0%**
untuk metode Reguler maupun Durasi. Jika salah satu jam kosong, aturan absensi tidak lengkap tetap berlaku.

**Master Pegawai** (Konfigurasi → tab Master Pegawai), format: `NIP, Nama, Jabatan, Tukin` (Tukin Bruto).
Digunakan sebagai data pegawai dan nilai Tukin pada Final.
Jabatan untuk Hitung Harian mengikuti kolom jabatan workbook absensi terbaru, tanpa ditimpa
Master Pegawai. Kolom jabatan kosong ditampilkan sebagai `Belum Ditentukan`.
Metode Reguler/Durasi mengikuti pemetaan Master Jabatan pada Konfigurasi.

> Contoh file tersedia di `assets/data/contoh_absensi.csv` dan `assets/data/contoh_pegawai.csv`.

---

## 4. Metode Perhitungan

### A. Jabatan DURASI AKTUAL — Wali Asuh, Wali Asrama, Satpam
Shift hanya sebagai **flag/informasi**, bukan dasar potongan. Dasar = durasi aktual (Jam Masuk→Pulang).
Minimum 7,5 jam:
- ≥ 7,5 jam → 0%
- kurang ≤ 30 menit → 0,5%
- kurang ≤ 1 jam → 1%
- kurang ≤ 1,5 jam → 1,5%
- kurang ≤ 2 jam → 2%
- ≤ 5 jam → 3%

### B. Jabatan REGULER — jabatan lainnya
Jam dasar: Senin–Kamis 07:30–16:00, Jumat 07:30–16:30. Menghitung keterlambatan & pulang cepat
(tarif berjenjang, dapat diedit di Konfigurasi), dengan **Flexi Time**:
- Masuk 07:31–08:00 **dan** pulang > 16:30 (Sen–Kam) / > 17:00 (Jumat) → keterlambatan **tidak** dipotong.
- Jika tidak memenuhi kompensasi → hitung keterlambatan/pulang cepat normal.

### C. Aturan umum
- **Absensi tidak lengkap** (jam masuk/pulang kosong) → **3%**, bukan keterlambatan, bukan hari kurang (tidak ada penalti ganda).
- **Hari kurang** = 3% × jumlah hari kekurangan (hari efektif − hari ada absensi). Reguler: hanya Sen–Jum non-libur. Durasi: semua hari.
- **Cuti/Sakit periode** dan jenis absensi tertentu (Tugas Kedinasan, Sakit, Cuti Melahirkan, Cuti Tahunan, Upacara) → 0%.
- **Duplikasi** = NIP sama + Tanggal sama + >1 record → diberi flag, dihitung 1 hari untuk "Jumlah Hari Absen".
- Potongan diakumulasikan dan **dibatasi maksimum 100%/bulan**.

---

**Wali Asuh (metode Durasi):** jam masuk/pulang yang kosong menjadi catatan pemeriksaan, tanpa potongan harian karena rekaman tidak lengkap. Tanggal dengan minimal satu rekaman tetap dihitung hadir, termasuk akhir pekan atau libur. Tanggal tanpa rekaman tidak langsung dipotong; Rekap menghitung selisih antara hari efektif dan jumlah hari hadir/kredit yang sah.

## 5. Konfigurasi
- **Kalender & Hari Efektif:** hari libur nasional 2026 bawaan (dapat diedit/ditambah). Mode hari efektif **Otomatis** (dari kalender) atau **Manual** (override).
- **Master Jabatan:** peta jabatan → metode (Durasi/Reguler), dapat diubah.
- **Jam Dasar & Flexi Time:** dapat disesuaikan.
- **Aturan Potongan:** tarif keterlambatan, pulang cepat, durasi, tidak lengkap, hari kurang.
- **Zona & Cuti:** deteksi WIB/WITA/WIT dari provinsi; periode cuti/sakit; mode waktu Original / Normalisasi WIB.
- **Master Pegawai:** unggah NIP/Nama/Jabatan/Tukin.

---

## 6. Zona Waktu (Original vs Normalisasi WIB)
Data sumber tidak ditimpa. Kolom `Jam Asli`, `Zona`, dan `Jam Normalisasi` disediakan terpisah.
Pada Hitung Harian pilih mode **Original** atau **Normalisasi WIB** (WITA −1 jam, WIT −2 jam ke WIB).

---

## 7. Final Tukin
- Komposisi: Kinerja/SKP 70% + Kehadiran 30%.
- Komponen Kehadiran = Tukin Bruto × 30%.
- Nominal Potongan = Komponen Kehadiran × Total Potongan Kehadiran %.
- Final Tukin Dibayar = Tukin Bruto − Nominal Potongan.
- **Final + SKP** (opsional): potongan SKP dari sumber lain (tidak dicampur otomatis), dihitung dari komponen 70%.
- **Final Kombinasi** (opsional): sumber Aplikasi / e-Absensi / ESDM (terpisah); bila >1 sumber dipilih diambil **potongan terkecil**.

---

## 8. Export Excel

Pada launcher dengan mode Python canonical, **Hitung Periode Aktif** memproses satu
periode dan menyimpan Harian, Rekap, dan Final langsung ke DuckDB. Hasil periode
yang sumber, aturan, dan Master SDM-nya belum berubah dapat dipakai kembali. Halaman
Harian, Semi Final, dan Rekap mengambil halaman yang sedang ditampilkan.
Ekspor berjalan sebagai job backend dari batch periode yang sama. File XLSX
memuat seluruh baris sheet terpilih, bukan hanya halaman yang sedang tampil.
Sheet yang melebihi 1.048.575 baris data (ditambah header) dilanjutkan pada
sheet bernomor. Excel baru tersedia untuk diunduh setelah pemeriksaan Final.

Tersedia setelah Final. Sheet: `DATA SUMBER`, `HITUNG HARIAN`, `REKAP SDM`, `SEMI FINAL`,
`FINAL ABSENSI`, `ANOMALI`, serta `FINAL + SKP`, `FINAL KOMBINASI`, dan `FINAL + SKP KOMBINASI` bila diaktifkan.
Dilengkapi header, autofilter, freeze pane, serta format tanggal/jam/persen/Rupiah dan baris total.

---

## 9. Troubleshooting
- **"Kolom wajib tidak ditemukan"** → pastikan ada kolom NIP, Nama, Tanggal, Jam Masuk, Jam Pulang.
- **Tanggal/jam salah baca** → gunakan format konsisten (dd/mm/yyyy dan HH:MM) atau format tanggal/jam asli Excel.
- **Tahap terkunci** → selesaikan tahap sebelumnya; Final terbuka setelah "VERIFIKASI SEMI FINAL".
- **Ingin mulai ulang** → Dashboard → "Reset Semua" (menghapus data & konfigurasi di localStorage).
- **Buka via file:// tidak jalan** → gunakan browser modern; seluruh dependensi sudah offline sehingga tidak butuh internet.

---

**Copyright © Dio Fajrul Iman**

---

## Prioritas
Akurasi perhitungan · Keterkaitan workflow · Auditability · Kemudahan verifikasi · Export Excel.

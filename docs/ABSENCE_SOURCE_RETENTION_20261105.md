# Aturan impor absensi, 5 November 2026

Perubahan ini mengikuti penjelasan pengguna untuk ekspor ESDM Oktober 2026.

- Kedua tanggal dan kedua jam kosong: simpan baris asli sebagai sumber bulanan,
  dengan `tanggal = NULL` dan informasi `NO_ATTENDANCE_DATE`. Jangan membuat
  tanggal, jam, hari hadir, ataupun hari libur. Periode berasal dari Bulan/Tahun
  di sumber atau periode unggahan yang dipilih secara eksplisit.
- Tanggal masuk kosong dan tanggal pulang tersedia: jam pulang 06:00:00 sampai
  08:59:59 ditempatkan pada tanggal kerja sebelumnya. Jam di luar rentang tersebut
  tetap memakai tanggal pulang. Tanggal masuk yang memang tersedia tetap menjadi
  acuan. Jam masuk tidak dibuat untuk melengkapi checkout saja.
- Tanggal/jam berisi teks yang tidak valid, NIP kosong, dan punch tanpa tanggal
  yang bisa ditentukan tetap menjadi blocker.
- Semua baris sumber dipertahankan di `raw_import`. Untuk perhitungan backend
  Standard, kelompok NIP/tanggal kerja digabung memakai jam masuk paling awal
  dan checkout absolut paling akhir. Jabatan, nama, dan jenis pada baris pertama
  dipertahankan. ID seluruh rekaman sumber tetap melekat pada hasil normalisasi.
- Jabatan impor menentukan metode perhitungan. Jabatan master tidak mengganti
  jabatan impor. Nilai bruto dicocokkan berdasarkan NIP.
- Wali Asuh tetap memakai aturan durasi dan kekurangan hari bulanan yang sudah
  ada: punch tidak lengkap tidak otomatis menambah penalti harian Wali Asuh;
  kekurangan target hari dikenai 3% per hari sesuai konfigurasi. Roster yang hanya
  berisi sumber tanpa tanggal tetap mendapat rekap bulanan tanpa baris harian
  buatan. Rumus baris bertanggal lainnya tetap mengikuti mesin yang sudah ada.

`raw_import.bulan` menyimpan periode laporan agar baris tanpa tanggal tetap
tercakup dalam pemuatan, penggantian periode, dan fingerprint sumber. Skema
backend lama mendapat kolom ini secara aditif. Penggantian impor dilakukan di
dalam transaksi dan tidak menghapus periode lain.

Manifest membedakan `totalRaw` (seluruh sumber), `totalRecord` (rekaman bertanggal
sebelum penggabungan harian), dan `nonAttendanceRows`. Nilai nol rekaman bertanggal
tidak diganti dengan jumlah sumber. Batas 50 pada panel masalah hanya batas
pratinjau; blocker diprioritaskan dan informasi tanpa presensi dipisahkan.

Versi impor: `ABSENCE_IMPORT_BACKEND_V1_2026_11_05`.
Versi kalkulasi: `ABSENCE_PY_2026_11_V4`.
Hash baseline historis dan golden tetap dipertahankan. Hash sumber untuk aturan
impor baru disimpan terpisah dan gate tetap memeriksa perubahan yang tidak dikenal.

Uji file asli dilakukan di database sementara dengan konfigurasi bawaan aplikasi.
File sumber dan database operasional tidak diubah oleh pengujian tersebut. Uji
bruto menggunakan kasus regresi sintetis dengan NIP yang cocok dan tidak cocok;
hasil uji file asli bukan penetapan pembayaran.

# Audit Penilaian SKP dan Pengelolaan TUKIN

Tanggal audit: 24 September 2026  
Ruang lingkup: `backend/skp_assessment.py`, `backend/tukin_annual.py`, `backend/tukin_combined.py`, skema/migrasi terkait, kontrol API, pengujian terotomasi, dan pemeriksaan agregat `backend/data/EVKIN_MASTER.duckdb`.

## Kesimpulan eksekutif

Implementasi telah memiliki fondasi kontrol yang baik: preview dan konfirmasi impor SKP, status review/persetujuan, audit perubahan, aturan tiga bulan tidak membuat SKP, pembekuan snapshot Master SDM, kontrol periode, rekam pembayaran, serta ekspor tahunan. Namun sistem **belum layak dinyatakan aman untuk penetapan dan pembayaran TUKIN produksi** sebelum temuan prioritas kritis dan tinggi di bawah ditutup.

Penilaian keseluruhan: **perlu perbaikan material (high risk)**.

| Prioritas | Jumlah | Dampak utama |
|---|---:|---|
| Kritis | 1 | Data periode yang sudah dibayar masih dapat diubah |
| Tinggi | 4 | Penutupan periode terhambat, impor parsial, klasifikasi berbeda, nominal salah diterima |
| Sedang | 3 | Jejak audit dan pemisahan kewenangan belum kuat |

Database operasional yang diperiksa belum berisi transaksi pada tabel workflow baru (`skp_assessment_monthly`, `tukin_period`, `tukin_final_monthly`, dan tabel terkait semuanya kosong). Karena itu, audit data produksi hanya dapat memeriksa kesiapan skema; efektivitas kontrol atas transaksi nyata belum dapat dibuktikan.

## Temuan

### F-01 — Kritis — Periode `PAID` masih dapat diubah melalui kondisi manual

`TukinAnnualService.add_condition()` tidak memeriksa status `tukin_period` sebelum menyimpan kondisi dan menjalankan `_apply_condition()`. Reproduksi pada database sementara membuktikan baris dengan status `PAID` dan nilai Rp10.000.000 dapat diubah menjadi `UNPAID` dan Rp0.

Bukti kode: `backend/tukin_annual.py:911` dan `backend/tukin_annual.py:969`.

Dampak:

- hasil pembayaran yang seharusnya immutable dapat berubah setelah dibayar;
- angka ekspor dan laporan tahunan dapat berbeda dari pembayaran yang telah dilakukan;
- jejak `tukin_payment_event` tidak otomatis dikoreksi, sehingga ledger dan posisi final dapat bertentangan.

Rekomendasi: gunakan satu guard periode di seluruh operasi mutasi; tolak perubahan pada `LOCKED`, `PAID`, dan `HISTORICAL_LOCKED`. Koreksi pascapembayaran harus melalui transaksi koreksi/reversal tersendiri, bukan update baris final.

### F-02 — Tinggi — Kasus `UNPAID` yang sah menghalangi penguncian periode

Validasi transisi status menghitung `HOLD` dan `UNPAID` sebagai data tidak lengkap. Akibatnya periode dengan keputusan sah “tidak dibayarkan” (misalnya bulan ketiga berturut-turut tidak membuat SKP) tidak dapat masuk ke `LOCKED` atau `PAID` melalui alur API normal.

Bukti kode: `backend/tukin_annual.py:1351`. Reproduksi menghasilkan `ValueError: Periode masih memiliki data ditahan/tidak lengkap`.

Rekomendasi: blokir hanya `HOLD`, nilai kalkulasi `NULL`, keputusan tanpa eviden/persetujuan, dan anomali lain. `UNPAID` yang tervalidasi harus dapat ikut dalam penutupan periode dengan nilai pembayaran nol.

### F-03 — Tinggi — Impor TUKIN tidak atomik dari data sampai registrasi batch

`import_rows()` menjalankan beberapa transaksi terpisah: membuat/memperbarui periode, mengganti final, meng-upsert SKP, menghitung streak, menyinkronkan kondisi, lalu baru mencatat `tukin_import_batch`. Jika langkah hilir gagal, data awal sudah committed tetapi batch tidak tercatat.

Bukti kode: `backend/tukin_annual.py:279`, `:317`, `:401`, dan `:417`. Simulasi kegagalan sinkronisasi meninggalkan satu baris `tukin_final_monthly` dengan nol baris `tukin_import_batch`.

Dampak: impor tampak gagal di UI tetapi sebagian data telah berubah; retry dapat menghasilkan hasil berbeda atau dianggap sebagai impor baru.

Rekomendasi: satukan seluruh perubahan dalam satu transaksi koneksi. Bila proses perlu bertahap, buat batch berstatus `PROCESSING`, simpan staging, lalu promote atomik menjadi `COMPLETED`; pada kegagalan tandai `FAILED` tanpa mengubah tabel final.

### F-04 — Tinggi — Klasifikasi laporan SKP berbeda antara modul SKP dan TUKIN

Urutan aturan `_report_status()` tidak sama. Untuk input yang mengandung status pengecualian sekaligus frasa tidak membuat SKP, modul penilaian mengembalikan `EXEMPT_OTHER`, sedangkan modul TUKIN mengembalikan `NOT_SUBMITTED_VERIFIED`.

Bukti kode: `backend/skp_assessment.py:154` dan `backend/tukin_annual.py:186`. Reproduksi dengan `CUTI TUGAS BELAJAR TIDAK MEMBUAT SKP` menghasilkan dua status berbeda.

Dampak: status, streak, potongan 35/70/100 persen, dan kelayakan pembayaran dapat bergantung pada jalur impor yang dipakai.

Rekomendasi: pindahkan normalisasi predikat/status ke satu modul domain bersama dan gunakan tabel keputusan eksplisit dengan prioritas aturan yang disetujui pemilik kebijakan.

### F-05 — Tinggi — Nominal tidak valid diam-diam menjadi nol

Parser `_number()` mengembalikan nilai default, umumnya `0.0`, ketika teks nominal tidak dapat diparse. Reproduksi menunjukkan `Rp ABC` diterima sebagai nol. Impor final tidak menandai baris tersebut sebagai ditolak.

Bukti kode: `backend/tukin_annual.py:132`.

Dampak: salah format sumber dapat mengubah TUKIN bruto, potongan, atau pembayaran menjadi nol tanpa peringatan.

Rekomendasi: bedakan nilai kosong yang memang opsional dari nilai terisi tetapi invalid. Nilai invalid harus menjadi error baris; tambahkan rekonsiliasi total sumber terhadap total hasil impor.

### F-06 — Sedang — Identitas pelaku berasal dari payload, bukan identitas terautentikasi

API dilindungi satu API key bersama, tetapi nama `approved_by`, `reviewed_by`, `actor`, dan `imported_by` diterima dari request. Pemegang key yang sama dapat mengisi nama siapa pun. Ini belum memenuhi non-repudiation dan segregation of duties.

Bukti kode: `backend/security.py:34-52`; contoh penggunaan actor terdapat pada `backend/skp_assessment.py:416`, `:1017`, `:1084`, dan `backend/tukin_annual.py:1332`.

Rekomendasi: gunakan akun pengguna/role yang terautentikasi; actor audit harus diturunkan dari sesi/token. Pisahkan minimal peran importir, reviewer SKP, approver, operator TUKIN, dan petugas pembayaran.

### F-07 — Sedang — Kondisi finansial manual tidak mewajibkan governance lengkap

`add_condition()` tidak mewajibkan alasan, eviden, atau approver walaupun efek dapat berupa `HOLD`, `UNPAID`, atau pengecualian potongan. Operasi juga tidak memiliki tabel riwayat perubahan khusus; ID yang sama dihapus lalu dimasukkan ulang.

Bukti kode: `backend/tukin_annual.py:911-967`.

Rekomendasi: wajibkan alasan, nomor dokumen/eviden, approver, timestamp, serta audit before/after. Larang overwrite; gunakan versioning atau status pembatalan.

### F-08 — Sedang — Event pembayaran tidak merekam pelaku sebenarnya

Saat periode ditandai `PAID`, `tukin_payment_event.created_by` diisi konstan `EVKIN`, bukan actor yang melakukan transisi.

Bukti kode: `backend/tukin_annual.py:1388`.

Rekomendasi: simpan actor terautentikasi dan referensi dokumen pembayaran/SP2D pada setiap event.

## Kontrol yang sudah baik

- Periode terkunci dicegah dari impor ulang pada jalur impor utama.
- Review SKP dan perubahan rekomendasi memiliki audit trail.
- Impor SKP memiliki preview, deteksi duplikasi NIP, serta penyimpanan baris sumber yang ditolak.
- Aturan bulan ketiga tidak membuat SKP menjadi `UNPAID` telah diuji.
- Snapshot Master SDM, data ahli waris, kondisi hukuman disiplin, dan ekspor tahunan telah memiliki pengujian khusus.
- API memiliki middleware API key, perbandingan token konstan-waktu, dan CORS allowlist.

## Hasil verifikasi

- Sebanyak **103 pengujian terarah lulus** untuk modul SKP, TUKIN tahunan, kombinasi SKP–absensi, readiness workflow, ownership, dan integritas final (9 peringatan deprecation, tanpa kegagalan).
- Reproduksi khusus dilakukan pada database sementara untuk F-01 sampai F-05 agar tidak mengubah database operasional.
- Pemeriksaan agregat database operasional tidak menemukan anomali karena tabel workflow baru masih kosong; ini bukan bukti bahwa kontrol efektif pada data nyata.

## Urutan perbaikan yang disarankan

1. Tutup F-01 dan tambahkan regression test bahwa seluruh mutasi ditolak setelah `LOCKED/PAID`.
2. Perbaiki F-02 agar `UNPAID` tervalidasi dapat ditutup, lalu uji periode campuran paid/unpaid.
3. Jadikan impor atomik dan berstatus jelas (F-03).
4. Satukan classifier SKP dan ubah parser nominal menjadi strict (F-04 dan F-05).
5. Perkuat identitas, role, approval, dan audit condition/payment event (F-06 sampai F-08).
6. Lakukan UAT dengan salinan satu periode nyata dan rekonsiliasi jumlah pegawai, bruto, potongan SKP, potongan absensi, nilai dibayar, nilai tidak dibayar, serta total per unit.

## Kriteria siap produksi

Sistem dapat dinyatakan siap setelah semua temuan kritis/tinggi ditutup, regression test lulus, satu periode UAT direkonsiliasi 100%, tidak ada perubahan setelah lock tanpa reversal resmi, dan setiap keputusan finansial dapat ditelusuri ke pengguna serta dokumen sumbernya.

# Audit proyek EVKIN — 9 Oktober 2026

Audit terhadap working tree saat ini, termasuk perubahan lokal dan modul belum di-track. Audit kode dan regresi; tidak melakukan perbaikan otomatis. Prioritas P1: integritas data/nominal; P2: fungsi atau verifikasi yang gagal pada kondisi tertentu.

## Temuan terkonfirmasi

### 1. P1 — GET dapat menghapus perlindungan sesi lama setelah reset
Lokasi: assets/js/export-backend.js:163–166; backend/runtime_guard.py:213–224.

Setiap respons sukses mengganti runtimeGeneration browser, termasuk GET /health, tanpa memastikan state/cache browser sudah dimuat ulang. Reproduksi dengan VM Node: generation awal 7, server merespons generation 8 pada GET /health, lalu POST /api/workflow/sync mengirim generation 8 dengan mutationBlocked=false. Data lokal lama dapat dikirim kembali dan melewati pagar generation server. syncWorkflow mengambil snapshot data browser sebelum mengantrekan penulisan; generation dipasang ketika fetch berjalan.

Perbaikan: ikat generation penulisan pada snapshot/hidrasi yang menghasilkan data, deteksi perubahan generation pada respons, blokir mutasi sampai hidrasi lengkap; respons GET umum tidak boleh mengadopsi generation baru secara diam-diam. Tambahkan regresi dua sesi: reset, GET, lalu upaya sync data lama.

### 2. P1 — Parser nominal pembulatan backend salah membaca format Indonesia
Lokasi: backend/pembulatan_backend.py:55–69; assets/js/pembulatan.js:22–29.

Python parse_numeric("1.000") menghasilkan 1.0; JavaScript menghasilkan 1000. build_result untuk satu baris amount="1.000" menghasilkan total/final 1 pada backend versus 1000 pada frontend. UI mengirim context.rows langsung ke backend ketika mode backend diaktifkan. Total rekonsiliasi tetap terlihat seimbang karena kedua sisi agregasi menggunakan nilai sumber yang sudah salah dibaca.

Perbaikan: samakan aturan pemisah ribuan/desimal frontend dan backend serta validasi nilai gagal parse. Uji angka numerik, 1.000, 1.000.000, Rp1.000, 1.000,50 dan nilai negatif.

### 3. P2 — Pembulatan nilai tengah berbeda antara frontend dan backend
Lokasi: backend/pembulatan_backend.py:72–78, 96–109; assets/js/pembulatan.js:15–19, 34–35.

Python round memakai ties-to-even, sedangkan Math.round JS membulatkan nilai tengah ke arah positif. round_value(2500,1000,"nearest") menghasilkan 2000 pada backend dan 3000 pada frontend. Ini mengubah pembulatan awal dan distribusi koreksi per pegawai meskipun agregat akhir direkonsiliasi. Perbedaan serupa dapat terjadi saat sumber memiliki setengah rupiah.

Perbaikan: tetapkan aturan bisnis pembulatan eksplisit dan gunakan implementasi identik. Uji nilai tengah, negatif, beberapa pegawai, dan koreksi residual.

### 4. P2 — CORS tidak mengizinkan header generation untuk operasi tulis
Lokasi: backend/security.py:105; backend/evkin_export_api.py:60–66; backend/main.py:30–36.

CORS_HEADERS hanya mengizinkan Accept, Content-Type, dan X-EVKIN-API-Key. Frontend menambahkan X-EVKIN-Generation, sementara middleware mewajibkannya pada mutasi. Reproduksi OPTIONS dengan origin lokal yang diizinkan dan requested headers content-type,x-evkin-api-key,x-evkin-generation menghasilkan HTTP 400 Disallowed CORS headers. Konfigurasi frontend/backend berbeda origin atau port gagal menulis. Respons generation juga belum dimasukkan expose_headers sehingga browser lintas origin tidak dapat membacanya. Akses origin yang sama tidak terkena preflight ini.

Perbaikan: allow dan expose X-EVKIN-Generation, serta uji preflight dan mutasi dari origin yang diizinkan. Backend pembulatan terpisah port 9101 juga menerima header tambahan dari pembungkus fetch global dan terdampak preflight.

### 5. P2 — Backend kalkulasi absensi diblokir baseline fingerprint lama
Lokasi: backend/absence_shadow_engine.py:21–33; backend/absence_cutover.py:104–115, 590–592.

readiness_gate() menghasilkan ready=false dan source_fingerprints_valid=false; golden_fingerprint_valid=true. Hash lima file berbeda: state.js, import.js, calc.js, harian.js, app.js. Tes test_feature_flag_default_remains_js_legacy gagal. submit() pada mode PYTHON_SHADOW/PYTHON_CANONICAL ditolak PARITY_GATE_FAILED. JS_LEGACY tetap merupakan default; temuan ini membatasi aktivasi backend Python, bukan bukti bahwa seluruh hasil legacy salah.

Perbaikan: audit perubahan rumus dan jalankan ulang parity baseline terkini sebelum menerbitkan baseline/version baru. Jangan sekadar mengganti hash atau mematikan gate.

### 6. P2 — CI menjalankan helper sebagai tes dan tidak mengagregasi exit code
Lokasi: .github/workflows/ci.yml:52–58; backend/tests/absence_import_real_world_runner.cjs:5–6; backend/tests/legacy_calculation_runner.cjs:4; backend/tests/generate_absence_js_golden.cjs:9.

CI memilih semua *.cjs tanpa argumen, termasuk runner yang membutuhkan file input. Reproduksi node backend/tests/absence_import_real_world_runner.cjs tanpa argumen gagal CSV source path required. Generator golden juga ikut dijalankan dan menulis ulang fixture saat verifikasi. Loop PowerShell tidak menghentikan proses atau mengumpulkan kegagalan node, sehingga exit code perintah terakhir dapat menutupi kegagalan sebelumnya. Pola yang sama berlaku untuk pemeriksaan sintaks.

Perbaikan: pilih test_*.cjs, jalankan helper hanya dengan argumen melalui tes pemilik, pisahkan generator fixture dari verifikasi, dan exit nonzero jika salah satu pemeriksaan gagal.

## Verifikasi

- 20 berkas test_*.cjs: lulus; syntax seluruh assets/js/*.js: lulus.
- 55 tes backend/tests/test_absence_import.py: lulus.
- Migration registry: 52 berkas valid.
- Python compileall backend: lulus.
- Pemeriksaan artefak repository: lulus, 234 berkas tracked diperiksa; bukan pemindaian isi rahasia atau seluruh berkas untracked.
- Regresi Python lengkap: berjalan saat laporan awal dibuat; hasil akhir akan diperbarui.
- Reproduksi terarah: generation setelah GET, parser nominal, ties pembulatan, preflight CORS, readiness fingerprint, helper CI.

## Batas cakupan

Tidak menjalankan UAT browser interaktif, benchmark data produksi, atau mencocokkan ketentuan nominal dengan regulasi eksternal. Tes lulus hanya mendukung kasus yang dicakup tes; bukan jaminan bebas bug. Ekspor window.open/tautan diperiksa kembali: pembungkus global sudah menambahkan autentikasi, sehingga bukan temuan. Perubahan kode lokal pengguna dipertahankan. Tidak ada kode aplikasi yang diperbaiki dalam audit ini.

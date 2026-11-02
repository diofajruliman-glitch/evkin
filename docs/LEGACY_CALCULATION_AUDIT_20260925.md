# Audit Kontrak Kalkulasi Legacy EVKIN

Tanggal audit: 25 September 2026  
Cakupan: Absensi -> Harian -> Rekap -> Final TUKIN  
Status: audit dan desain parity saja; belum ada migrasi kalkulasi

## Batasan audit

- Tidak membaca atau mengubah data produksi.
- Tidak mengubah formula, schema, import, reset, atau alur UI produksi.
- Fixture seluruhnya sintetis dan runner berjalan in-memory.
- `assets/js/final.js` hanya diinstrumentasi pada source string di VM Node; file produksi tidak dimodifikasi oleh runner.

## Kesimpulan utama

Sumber kebenaran Harian dan Rekap yang benar-benar aktif masih formula browser di
`assets/js/calc.js` dan `assets/js/rekap.js`. Dua fungsi backend yang namanya terlihat
seperti kandidat Harian (`compute_harian_core` dan `compute_harian_duckdb`) belum
setara dengan legacy dan tidak boleh dipromosikan menjadi canonical tanpa adapter dan
parity gate. Belum ditemukan implementasi Rekap backend yang dapat dibandingkan.

Final TUKIN memiliki lebih dari satu implementasi formula. Tampilan `final.js`
membulatkan nominal uang dengan `Math.round`, sedangkan payload sinkronisasi di
`app.js` menghitung SKP/combined dengan floating point tanpa pembulatan per nominal.
Kandidat Python memakai `Decimal`, pembulatan `ROUND_HALF_UP`, komponen rupiah
integer, dan workflow gate. Akibatnya kandidat Python lebih ketat, tetapi belum
merupakan pengganti exact dari seluruh perilaku legacy.

Fixture `rounding_rupiah` membuktikan konflik nyata tanpa data produksi: bruto
Rp1.000.002, absensi 50%, SKP 50%. Tampilan JS menghasilkan nominal bulat, tetapi
payload sinkron legacy menghasilkan `350000.69999999995` untuk nominal SKP. Kandidat
Python menghasilkan Rp350.001. Karena kontrak uang mewajibkan integer Rupiah dan
selisih Rp0, kondisi ini dinyatakan **belum parity**, bukan ditoleransi sebagai error
floating point.

## Peta dependency dan urutan kalkulasi

```text
raw row/file
  -> import.js / import-wali.js
     parse date/time, timezone minutes, crossDay, pairing, duplicate flag
  -> state.data (normalized attendance record)
  -> calc.js computeRecord
     config + holiday + master SDM + job method + leave/no-deduction
  -> state.daily
  -> Semi Final verification gate
  -> rekap.js process
     group month|NIP, reject duplicate dates, effective days, monthly sums/cap
  -> state.rekap
  -> final.js process
     lookup Tukin bruto Master SDM, attendance 30%, money rounding
  -> optional SKP/combined views
  -> app.js sync payload (formula duplicate yang tidak selalu sama dengan view)
```

Dependency konfigurasi:

- Kalender: `effectiveDaysMode`, `effectiveDaysManual`, `holidays`.
- Jadwal: `jamDasar.senKam`, `jamDasar.jumat`, dan konfigurasi flexi.
- Potongan: tier terlambat/pulang cepat, `minDurasiJam`, potongan tidak lengkap,
  dan potongan hari kurang.
- Klasifikasi: Master SDM per NIP, master jabatan/metode, jenis bebas potongan,
  dan periode cuti/sakit.
- Final: periode aktif, rekap per NIP, Tukin bruto Master SDM, sumber persentase
  absensi, persentase/status SKP, dan sumber kombinasi yang dipilih.

## Formula legacy: normalisasi input

Referensi utama: `assets/js/utils.js`, `assets/js/data.js`, `assets/js/import.js`,
dan `assets/js/import-wali.js`.

1. Tanggal disimpan sebagai `yyyy-mm-dd`, tampilan `dd/mm/yyyy`, dan month key
   `yyyy-mm`. Hari dalam minggu dihitung dengan `Date.UTC`.
2. Jam menjadi integer menit sejak tengah malam. Pecahan serial Excel dikalikan
   1440 lalu `Math.round`. Jam numerik bulat >= 1 dianggap tanggal tanpa jam dan
   menjadi `null`.
3. Normalisasi WIB adalah offset tetap: WIB 0, WITA -60, WIT -120 menit. Mode
   alternatif `wib-add` memakai +60/+120. Nilai import dibungkus modulo 1440.
   Tidak ada timezone database, DST, atau instant UTC.
4. `crossDay` aktif bila tanggal pulang lebih besar dari tanggal masuk atau jam
   pulang lebih kecil dari jam masuk.
5. Durasi adalah `pulang - masuk`; bila hasil `<= 0`, ditambah 1440. Karena itu
   jam masuk sama dengan jam pulang bermakna 24 jam, bukan 0 jam.
6. Import Wali memasangkan event masuk dengan event berikutnya bila selisih 0
   sampai 18 jam. Record hari kerja yang sama dapat digabung dengan masuk paling
   awal dan pulang paling akhir.
7. Duplicate standard ditandai dari kunci NIP+tanggal. Flag tidak menghapus atau
   menggabungkan record.

## Formula Harian

Referensi canonical aktif: `assets/js/calc.js:8-213`.

### Resolusi konteks

- Jabatan diambil dari Master SDM bila tersedia, kemudian fallback ke record.
- Hanya mapping metode yang persis `durasi` diperlakukan sebagai durasi; lainnya
  `reguler`.
- Hari libur adalah tanggal pada konfigurasi **atau** Sabtu/Minggu.
- Urutan early-return merupakan bagian formula dan tidak boleh ditukar:
  periode cuti/sakit -> jenis bebas potongan -> hari OFF/libur -> data tidak lengkap
  -> metode durasi/reguler.

### Reguler

- Senin-Kamis memakai jam dasar senKam; Jumat memakai jam dasar Jumat.
- `lateMin = max(0, masuk - masukStandar)`.
- `earlyMin = max(0, pulangStandar - pulang)`.
- Tier memilih entry pertama dengan `minutes <= max`. Default saat ini:
  <=30: 0,5%; <=60: 1%; <=90: 1,5%; <=120: 2%; selebihnya 3%.
- Flexi berlaku jika masuk berada pada rentang inklusif start..end. Kedua penalti
  menjadi 0 hanya bila pulang **lebih besar** (bukan sama dengan) jam kompensasi.
- Total harian = round2(terlambat + pulang cepat + durasi + tidak lengkap).

### Durasi/Wali

- Durasi aktual menggunakan aturan lintas tengah malam di atas.
- Dengan minimum 7,5 jam: >=7,5: 0%; >=7: 0,5%; >=6,5: 1%; >=6: 1,5%;
  >=5,5: 2%; di bawahnya 3%.
- `sourceFormat=wali` menentukan status Wali/lintas hari, tetapi metode potongan
  tetap ditentukan oleh jabatan canonical.

### Null, cuti, dan hari libur

- Satu atau kedua punch `null` pada hari kerja memberi hanya potongan tidak
  lengkap (default 3%); terlambat/pulang cepat tidak ditambahkan.
- Jika keduanya null, status memilih `LUPA_ABSEN_MASUK` karena pemeriksaan masuk
  dilakukan lebih dulu.
- Cuti/periode bebas dan jenis bebas potongan menghasilkan 0 dan `excused=true`.
- Hari libur/weekend menghasilkan 0 walaupun punch tidak lengkap. Statusnya
  `ABSEN_DI_LUAR_JADWAL`.

## Formula Rekap

Referensi canonical aktif: `assets/js/rekap.js:11-139`.

1. Wajib ada Harian dan `semifinalVerified=true`.
2. Group key adalah `month|NIP`.
3. Dua record pada NIP+tanggal yang sama menggagalkan **seluruh proses Rekap**;
   hasil lama tidak diganti.
4. Hari efektif manual memakai nilai konfigurasi; otomatis menghitung Senin-Jumat
   dikurangi tanggal libur konfigurasi.
5. Record hadir adalah record yang memiliki jam masuk atau jam pulang. Tanggal
   `excused`/cuti ditambahkan sebagai hadir.
6. Reguler menghitung hadir hanya pada set tanggal efektif. Metode durasi menghitung
   tanggal hadir seluruh Senin-Minggu pada bulan itu.
7. `hariKurang = max(0, hariEfektif - hariAdaAbsensi)` dan
   `potHariKurang = round2(hariKurang * potonganPerHari)`.
8. Setiap kolom penalti dijumlahkan lalu `round2`.
9. Total metode durasi = terlambat + pulang cepat + durasi + hari kurang hanya
   bila hari hadir < hari efektif. Potongan tidak lengkap tidak masuk total durasi.
10. Total reguler = terlambat + pulang cepat + tidak lengkap. Potongan durasi dan
    hari kurang tidak masuk total reguler.
11. `totalRaw` disimpan; `total = min(100, totalRaw)`. Tidak ada lower clamp.

## Formula Final TUKIN legacy

Referensi utama: `assets/js/final.js:140-231`, `assets/js/final.js:440-457`,
`assets/js/final.js:710-774`, dan duplikasi sinkron di `assets/js/app.js:75-111`.

### Final Absensi

- `tukin = Math.round(bruto)`.
- `komponenAbsensi = Math.round(tukin * 0.30)`.
- `potPct = min(100, Number(value) || 0)`; nilai negatif tidak di-clamp ke 0.
- `nominalAbsensi = Math.round(komponenAbsensi * potPct / 100)`.
- `dibayar = Math.round(tukin - nominalAbsensi)`.
- Bruto tidak tersedia menghasilkan nominal/dibayar `null`, sementara persentase
  tetap dinormalisasi.

### SKP dan combined pada tampilan

- Komponen SKP tampilan = `Math.round(tukin * 0.70)`.
- Nominal SKP tampilan = `Math.round(komponenSKP * min(100, skpPct) / 100)`.
- Combined menghitung komponen dari 30%/70%, membulatkan masing-masing nominal,
  lalu membulatkan total dan pembayaran.
- Persentase absensi combined adalah minimum sumber yang diaktifkan.
- Persentase weighted = round2(absensiPct*0,30 + skpPct*0,70).

### Perbedaan payload sinkron

`app.js` menghitung ulang payload, bukan memakai semua nominal yang sudah tampil:

- `nominalSKP = bruto * 0.70 * pct / 100` tanpa `Math.round`.
- nominal combined 30%/70% juga tidak dibulatkan sebelum disimpan.
- Status approval SKP tidak menjadi gate pada formula legacy manual/sync.

Akibatnya terdapat dua observasi legacy untuk input sama: nilai tampilan bulat dan
payload sinkron pecahan. Contract migrasi tidak boleh memilih salah satunya secara
diam-diam.

## Kandidat backend yang ditemukan

### Harian

`backend/evkin_export_api.py:1851` dan `:2244` bukan kandidat parity-ready.
Risiko yang teridentifikasi dari audit statis:

- durasi/core tidak mempertahankan seluruh semantik `<=0 + 1440` secara identik;
- cabang incomplete/holiday/durasi dan urutan early-return tidak identik;
- timezone/normalisasi, master jabatan canonical, duplicate, status, flags, dan
  provenance Wali tidak memiliki contract yang sama;
- jalur DuckDB membentuk SQL lain lagi, sehingga saat ini ada tiga implementasi
  yang berpotensi berbeda.

`backend/tests/compare_compute.py` hanya membandingkan dua kandidat backend pada
beberapa kolom dan memakai `fillna(0)`. Test itu tidak menjalankan JS canonical,
sehingga tidak membuktikan parity legacy.

### Rekap

Belum ada kandidat backend murni yang mengimplementasikan kontrak Rekap di atas.
Status parity harus `BLOCKED`, bukan dianggap lulus.

### Final

`backend/final_tukin.py:81` dan `backend/tukin_combined.py:47` memakai Decimal,
integer Rupiah, komponen 30% dibulatkan, komponen 70% komplementer, dan workflow
gate SKP. Ini dapat menjadi desain canonical baru, tetapi belum exact terhadap
payload legacy `app.js` dan status DRAFT legacy:

- fixture rounding menunjukkan payload legacy pecahan versus kandidat integer;
- SKP DRAFT tetap dihitung oleh legacy manual/sync, tetapi kandidat Python `HOLD`.

## Risiko parity prioritas tinggi

1. **Formula duplikat Final:** view, export, sync, dan Python tidak seluruhnya
   memakai fungsi yang sama.
2. **Rounding boundary:** JS `Math.round` pada setiap komponen/nominal tertentu,
   tetapi sync menghitung float langsung. Aggregate dapat berbeda dari sum per-row.
3. **Workflow semantics:** backend menahan SKP non-final; legacy menghitung nilai
   manual tanpa gate status.
4. **Urutan early-return Harian:** perubahan urutan akan membuat holiday/null/cuti
   menerima penalti yang berbeda.
5. **Cross-day:** jam sama berarti 24 jam; pairing Wali dibatasi 18 jam; record
   standard dapat cross-day hanya dari urutan jam.
6. **Timezone:** ini offset jam tetap dan modulo, bukan konversi timestamp modern.
7. **Duplicate:** Harian tetap menghasilkan row ber-flag, tetapi Rekap hard-fail.
8. **Kehadiran reguler vs durasi:** domain tanggal yang dihitung berbeda.
9. **Null versus 0:** kosong, null, string kosong, dan numeric 0 melalui coercion JS
   yang berbeda pada tanggal, jam, persentase, dan bruto.
10. **Aggregate combined view:** beberapa total tampilan memakai seluruh bruto tetapi
    deduction dari `pageData`; ini harus difreeze/test sebelum menjadi contract API.

## Canonical calculation contract yang disarankan

Contract ini adalah target desain; implementasi backend belum dilakukan.

### Input envelope

```json
{
  "contract_version": "legacy-js-audit-v1",
  "period": "YYYY-MM",
  "time_mode": "original|wib|wib-add",
  "config_snapshot": {},
  "master_snapshot_id": "...",
  "attendance_rows": [],
  "skp_snapshot": null
}
```

Setiap row wajib membawa stable row id, NIP string, work-date, optional out-date,
integer minute in/out, normalized minute variants, zone, kind, source format,
cross-day, duplicate flag, dan provenance. Backend tidak boleh parse ulang nilai
tampilan bila nilai canonical minute/date sudah tersedia.

### Output stages

- `daily[]`: satu output per input row dengan seluruh komponen potongan, durasi,
  status, flags, dan source row id.
- `rekap[]`: satu output per NIP+periode dengan component sums, attendance counts,
  `totalRaw`, capped `total`, duplicate/error status.
- `final[]`: satu output per NIP+periode dengan bruto, integer component Rupiah,
  percentage source/status, integer deductions, total deduction, dan paid amount.
- Setiap stage membawa `contract_version`, `config_hash`, `input_hash`, dan
  deterministic row key agar per-row diff dapat ditelusuri.

### Numeric rules

- Menit dan Rupiah adalah integer.
- Persentase dihitung sebagai decimal exact; round2 dilakukan hanya di titik yang
  tercantum pada formula legacy.
- Uang wajib integer Rupiah; comparison adalah equality, allowed delta Rp0.
- Tidak ada epsilon/tolerance float untuk output uang.
- Rekomendasi canonical Final: jadikan formula yang tampil di `final.js` sebagai
  baseline bisnis, dengan round-half-up untuk domain nonnegatif pada boundary yang
  sama. Payload sync pecahan harus diperlakukan sebagai defect compatibility yang
  memerlukan keputusan eksplisit, bukan formula canonical tersembunyi.

### Error/gate rules

- Duplicate NIP+tanggal: Daily boleh berstatus anomali, Rekap tidak diterbitkan.
- Missing config/master/attendance/SKP harus menghasilkan status deterministik,
  bukan coercion diam-diam.
- Keputusan apakah SKP DRAFT dihitung atau HOLD harus ditetapkan pemilik bisnis.
  Rekomendasi aman adalah workflow gate backend saat ini, tetapi perubahan dari
  legacy wajib melalui UAT/approval karena berdampak pada hasil.

## Fixture dan parity harness

Fixture: `backend/tests/fixtures/legacy_calculation_cases.json`.

Kasus yang tersedia: normal, terlambat 31 menit, tidak absen masuk, tidak absen
pulang, shift Wali lintas hari, shift Wali pendek, libur dengan punch kosong,
weekend Wali, null/kosong, flexi terkompensasi, WITA->WIB, jenis bebas potongan,
duplicate, boundary pembulatan Rupiah, SKP approved, dan SKP DRAFT.

Runner: `backend/tests/legacy_calculation_runner.cjs`.

- Memuat file JS produksi aktual dalam VM Node.
- Menghasilkan Harian dan Rekap aktual.
- Mengekspos fungsi private Final hanya di source string VM.
- Menangkap payload yang dibentuk `syncBackendWorkflow` tanpa HTTP/DB.

Comparator: `backend/tools/audit_calculation_parity.py`.

- Menjalankan runner legacy dan kandidat Python Final pada input sama.
- Membandingkan per NIP/per row dan aggregate.
- Menandai field uang non-integer.
- Menolak tolerance uang dan menyediakan `--require-parity` untuk gate masa depan.
- Harian ditandai `PARITY_BLOCKED_NO_PURE_BACKEND_CONTRACT`; Rekap ditandai
  `PARITY_BLOCKED_NO_BACKEND_IMPLEMENTATION`. Ini disengaja agar ketiadaan kandidat
  tidak berubah menjadi false-green.

Test regression: `backend/tests/test_calculation_parity_harness.py` memverifikasi
coverage, golden edge outputs legacy, exact-Rupiah policy, mismatch rounding, hard
failure duplicate, cross-day 480 menit, timezone, excused, dan SKP DRAFT HOLD.

## Strategi migrasi backend paling aman

1. Bekukan dokumen ini dan fixture sebagai contract review; putuskan lebih dulu
   authority Final (view integer direkomendasikan) dan policy SKP non-final.
2. Ekstrak adapter input murni dari record normalized. Jangan mulai dari query DB
   atau schema baru; fungsi kandidat harus menerima fixture JSON tanpa side effect.
3. Implementasikan Harian pure function di Python dengan urutan branch yang sama.
   Hubungkan ke comparator, tetapi jangan ke endpoint produksi.
4. Capai exact parity seluruh field Harian, bukan hanya `totalHarian`.
5. Implementasikan Rekap pure function setelah Harian parity, termasuk hard-fail
   duplicate dan perbedaan kalender reguler/durasi.
6. Capai exact per-row dan aggregate parity pada fixture yang diperluas dengan
   boundary tier (30/31, 60/61, dst.), tepat jam flexi, 18h/18h+1 pairing, akhir
   bulan/tahun, zero config, negative/invalid inputs, dan cap 100%.
7. Satukan formula Final view/sync/export lewat satu contract setelah keputusan
   rounding/status disetujui. Jalankan shadow calculation read-only.
8. Uji dataset sintetis besar dan snapshot anonim yang secara eksplisit disetujui;
   produksi tetap memakai JS hingga gate Rp0 dan seluruh workflow status lulus.
9. Cutover per stage di belakang feature flag dengan rollback ke legacy. Jangan
   menghapus JS sampai dua jalur memberi hasil identik dan audit trail tersimpan.

## Kriteria exit sebelum migrasi

- Harian: seluruh field dan status exact per source row.
- Rekap: exact per NIP+periode dan aggregate; duplicate menghasilkan error identik.
- Final: seluruh nominal integer dan delta Rp0 per NIP serta aggregate.
- Tidak ada field uang pecahan pada UI, export, sync, atau storage.
- Workflow/status decision telah disetujui dan fixture-nya eksplisit.
- Test existing tetap lulus; parity gate lulus tanpa menyentuh database produksi.


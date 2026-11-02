/* EVKIN ABSENSI v3.1 - export.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, S = EVKIN.state;
  const mod = EVKIN.exportMod = {};

  const FMT = { date: 'dd/mm/yyyy', pct: '0.##"%"', rp: '"Rp"#,##0' };
  function money(v) { const n = Number(v); return Number.isFinite(n) ? Math.round(n) : ''; }

  const EXPORT_WORKER_SOURCE = `
    const FMT = { date: 'dd/mm/yyyy', pct: '0.##"%"', rp: '"Rp"#,##0' };
    function num(v) { return (v === '' || v === null || v === undefined) ? '' : Number(v); }
    function money(v) { const n = Number(v); return Number.isFinite(n) ? Math.round(n) : ''; }
    function monthLabel(bulan) {
      const labels = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
      return labels[parseInt(bulan) || 0] || '';
    }
    self.onmessage = function (event) {
      const { action, payload } = event.data;
      if (action !== 'buildSheets') return;
      const { rawHeaders, raw, daily, rekap, finalData, finalSKP, finalKombinasi, finalCombinedSKP, skipRawData } = payload;
      const sheets = {};
      const indexByNip = (rows) => new Map((rows || []).map((row) => [String(row.nip), row]));
      const skpByNip = indexByNip(finalSKP.rows);
      const kombinasiByNip = indexByNip(finalKombinasi.rows);
      const combinedSkpByNip = indexByNip(finalCombinedSKP && finalCombinedSKP.skpRows);
      const eabsensiByNip = indexByNip(finalCombinedSKP && finalCombinedSKP.eabsensiRows);
      const esdmByNip = indexByNip(finalCombinedSKP && finalCombinedSKP.esdmRows);
      try {
        // Skip DATA SUMBER untuk dataset besar (smart mode)
        if (!skipRawData) {
          self.postMessage({ type: 'progress', percent: 10, message: 'Processing DATA SUMBER...' });
          const srcHead = rawHeaders.length ? rawHeaders : ['(kosong)'];
          const srcRows = raw.map(r => srcHead.map(h => r[h] !== undefined ? r[h] : ''));
          sheets['DATA SUMBER'] = { aoa: [srcHead, ...srcRows], formats: null, widths: srcHead.map(() => 16) };
        } else {
          self.postMessage({ type: 'progress', percent: 10, message: 'Skipping raw data (Smart Mode)...' });
        }

        self.postMessage({ type: 'progress', percent: 20, message: 'Processing HITUNG HARIAN...' });
        const dH = ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Hari', 'Jam Masuk', 'Jam Pulang', 'Durasi', 'Zona', 'Jam Normalisasi', 'Metode', 'Jenis Absensi', 'Status Hari Libur', 'Status Cuti', 'Status Duplikasi', 'Absensi Lengkap', 'Pot Keterlambatan', 'Pot Pulang Cepat', 'Pot Durasi', 'Pot Tidak Lengkap', 'Total Potongan Harian', 'Flag'];
        const dRows = daily.map(x => [x.nip, x.nama, x.jabatan, x.tanggal, x.hari, x.jamMasuk, x.jamPulang, x.durasi, x.zona, x.jamNormalisasi, x.metode, x.jenis, x.statusHariLibur, x.statusCuti, x.statusDuplikasi, x.statusLengkap, num(x.potKeterlambatan), num(x.potPulangCepat), num(x.potDurasi), num(x.potTidakLengkap), num(x.totalHarian), x.flags.join('; ')]);
        sheets['HITUNG HARIAN'] = { aoa: [dH, ...dRows], formats: { 16: FMT.pct, 17: FMT.pct, 18: FMT.pct, 19: FMT.pct, 20: FMT.pct }, widths: [18, 22, 22, 12, 8, 9, 9, 9, 7, 16, 10, 16, 18, 14, 12, 12, 12, 12, 10, 12, 14, 30] };

        self.postMessage({ type: 'progress', percent: 30, message: 'Processing SEMI FINAL...' });
        const sfH = ['NIP', 'Nama', 'Tanggal', 'Jam Masuk', 'Jam Pulang', 'Jenis', 'Status Duplikasi', 'Absensi Lengkap', 'Status Hari Libur', 'Status Cuti', 'Flag', 'Total Harian'];
        const sfRows = daily.filter(x => x.flags.length || x.statusDuplikasi || x.statusLengkap === 'Tidak Lengkap').map(x => [x.nip, x.nama, x.tanggal, x.jamMasuk, x.jamPulang, x.jenis, x.statusDuplikasi, x.statusLengkap, x.statusHariLibur, x.statusCuti, x.flags.join('; '), num(x.totalHarian)]);
        sheets['SEMI FINAL'] = { aoa: [sfH, ...(sfRows.length ? sfRows : [['-', 'Tidak ada anomali', '', '', '', '', '', '', '', '', '', '']])], formats: { 11: FMT.pct }, widths: [18, 22, 12, 9, 9, 14, 14, 14, 18, 14, 30, 12] };

        self.postMessage({ type: 'progress', percent: 40, message: 'Processing REKAP SDM...' });
        const rH = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Hari Efektif', 'Hari Ada Absensi', 'Hari Kurang', 'Jumlah Hari Absen', 'Pot Keterlambatan %', 'Pot Pulang Cepat %', 'Pot Durasi %', 'Pot Hari Kurang %', 'Pot Tidak Rekam %', 'Total Potongan Kehadiran %', 'Status'];
        const rRows = rekap.map(x => [x.nip, x.nama, x.jabatan, monthLabel(x.bulan), x.hariEfektif, x.hariAdaAbsensi, x.hariKurang, x.jumlahHariAbsen, num(x.potKeterlambatan), num(x.potPulangCepat), num(x.potDurasi), num(x.potHariKurang), num(x.potTidakLengkap), num(x.total), x.status]);
        sheets['REKAP SDM'] = { aoa: [rH, ...rRows], formats: { 8: FMT.pct, 9: FMT.pct, 10: FMT.pct, 11: FMT.pct, 12: FMT.pct, 13: FMT.pct }, widths: [18, 22, 22, 14, 11, 14, 11, 14, 12, 12, 10, 12, 12, 16, 14] };

        self.postMessage({ type: 'progress', percent: 50, message: 'Processing FINAL ABSENSI...' });
        const fH = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Total Potongan Kehadiran %', 'Nominal Potongan Kehadiran', 'FINAL TUKIN DIBAYAR'];
        const fRows = finalData.map(f => [f.nip, f.nama, f.jabatan, monthLabel(f.bulan), money(f.tukin), money(f.komponen), num(f.potPct), money(f.nominal), money(f.finalDibayar)]);
        const totB = finalData.reduce((a, f) => a + (Number(f.tukin) || 0), 0), totN = finalData.reduce((a, f) => a + (Number(f.nominal) || 0), 0), totP = finalData.reduce((a, f) => a + (Number(f.finalDibayar) || 0), 0);
        fRows.push(['', '', '', 'TOTAL', money(totB), '', '', money(totN), money(totP)]);
        sheets['FINAL ABSENSI'] = { aoa: [fH, ...fRows], formats: { 4: FMT.rp, 5: FMT.rp, 6: FMT.pct, 7: FMT.rp, 8: FMT.rp }, widths: [18, 22, 22, 14, 16, 20, 20, 22, 20] };

        self.postMessage({ type: 'progress', percent: 60, message: 'Processing ANOMALI...' });
        const aH = ['NIP', 'Nama', 'Tanggal', 'Jenis Anomali', 'Detail'];
        const aRows = [];
        daily.forEach(x => { if (x.statusDuplikasi) aRows.push([x.nip, x.nama, x.tanggal, 'Duplikasi NIP+Tanggal', x.jamMasuk + ' - ' + x.jamPulang]); });
        daily.forEach(x => { if (x.statusLengkap === 'Tidak Lengkap') aRows.push([x.nip, x.nama, x.tanggal, 'Absensi Tidak Lengkap', 'Masuk/Pulang kosong']); });
        sheets['ANOMALI'] = { aoa: [aH, ...(aRows.length ? aRows : [['-', 'Tidak ada anomali', '', '', '']])], formats: null, widths: [18, 22, 12, 22, 40] };

        self.postMessage({ type: 'progress', percent: 70, message: 'Processing optional sheets...' });
        if (finalSKP.enabled) {
          const skH = ['NIP', 'Nama', 'Tukin Bruto', 'Nominal Pot Kehadiran', 'Potongan SKP %', 'Nominal Pot SKP', 'FINAL TUKIN DIBAYAR'];
          const skRows = finalData.map(f => {
            const sr = skpByNip.get(String(f.nip)) || { skpPct: 0 };
            const nomSKP = Math.round((Number(f.tukin) || 0) * 0.70 * (Math.min(100, Number(sr.skpPct) || 0) / 100));
            return [f.nip, f.nama, money(f.tukin), money(f.nominal), num(sr.skpPct), money(nomSKP), money((Number(f.tukin) || 0) - (Number(f.nominal) || 0) - nomSKP)];
          });
          sheets['FINAL + SKP'] = { aoa: [skH, ...skRows], formats: { 2: FMT.rp, 3: FMT.rp, 4: FMT.pct, 5: FMT.rp, 6: FMT.rp }, widths: [18, 22, 16, 20, 14, 18, 20] };
        }

        if (finalKombinasi.enabled || finalKombinasi.useEabsensi || finalKombinasi.useEsdm) {
          const K = finalKombinasi;
          const kH = ['NIP', 'Nama', 'Aplikasi %', 'e-Absensi %', 'ESDM %', 'Potongan Terpilih %', 'Nominal Potongan', 'FINAL TUKIN DIBAYAR'];
          const kRows = finalData.map(f => {
            const kr = kombinasiByNip.get(String(f.nip)) || { eabs: 0, esdm: 0 };
            const cand = []; if (K.useApp) cand.push(f.potPct); if (K.useEabsensi) cand.push(kr.eabs); if (K.useEsdm) cand.push(kr.esdm);
            const minPct = cand.length ? Math.min(...cand) : 0;
            const nominal = Math.round((Number(f.komponen) || 0) * (Math.min(100, minPct) / 100));
            return [f.nip, f.nama, num(f.potPct), num(kr.eabs), num(kr.esdm), num(minPct), money(nominal), money((Number(f.tukin) || 0) - nominal)];
          });
          sheets['FINAL KOMBINASI'] = { aoa: [kH, ...kRows], formats: { 2: FMT.pct, 3: FMT.pct, 4: FMT.pct, 5: FMT.pct, 6: FMT.rp, 7: FMT.rp }, widths: [18, 22, 12, 12, 10, 16, 18, 20] };
        }

        self.postMessage({ type: 'progress', percent: 80, message: 'Processing FINAL + SKP KOMBINASI...' });
        if (finalCombinedSKP && finalCombinedSKP.absensiProcessed) {
          const FC = finalCombinedSKP;
          const fcH = ['NIP', 'Nama', 'Jabatan', 'Tukin Bruto', 'Potongan Absensi % (min)', 'Nominal Potongan Absensi (30%)', 'Potongan SKP %', 'Nominal Potongan SKP (70%)', 'Total Potongan %', 'Total Nominal Potongan', 'FINAL TUKIN DIBAYARKAN'];
          const fcRows = finalData.map(f => {
            const cr = combinedSkpByNip.get(String(f.nip)) || { skpPct: 0 };
            const komKehadiran = f.tukin * 0.30;
            const komKinerja = f.tukin * 0.70;
            const absensiCand = [];
            if (FC.useApp) absensiCand.push(f.potPct);
            if (FC.useEabsensi) {
              const eabsRow = eabsensiByNip.get(String(f.nip));
              if (eabsRow) absensiCand.push(eabsRow.pct);
            }
            if (FC.useEsdm) {
              const esdmRow = esdmByNip.get(String(f.nip));
              if (esdmRow) absensiCand.push(esdmRow.pct);
            }
            const finalAbsensiPct = absensiCand.length ? Math.min(...absensiCand) : f.potPct;
            const nomAbsensi = komKehadiran * (Math.min(100, finalAbsensiPct) / 100);
            const skpPct = cr.skpPct;
            const nomSKP = komKinerja * (Math.min(100, skpPct) / 100);
            const totalPotPct = Math.round((finalAbsensiPct * 0.30 + skpPct * 0.70) * 100) / 100;
            const totalNominal = nomAbsensi + nomSKP;
            const tukinDibayar = f.tukin - totalNominal;
            return [f.nip, f.nama, f.jabatan, num(f.tukin), num(finalAbsensiPct), num(nomAbsensi), num(skpPct), num(nomSKP), num(totalPotPct), num(totalNominal), num(tukinDibayar)];
          });
          const totBruto = finalData.reduce((a, f) => a + f.tukin, 0);
          const totAbsensiNom = fcRows.reduce((a, r) => a + (r[5] || 0), 0);
          const totSKPNom = fcRows.reduce((a, r) => a + (r[7] || 0), 0);
          const totNominal = fcRows.reduce((a, r) => a + (r[9] || 0), 0);
          const totFinal = fcRows.reduce((a, r) => a + (r[10] || 0), 0);
          fcRows.push(['', '', '', num(totBruto), '', num(totAbsensiNom), '', num(totSKPNom), '', num(totNominal), num(totFinal)]);
          sheets['FINAL + SKP KOMBINASI'] = { aoa: [fcH, ...fcRows], formats: { 3: FMT.rp, 4: FMT.pct, 5: FMT.rp, 6: FMT.pct, 7: FMT.rp, 8: FMT.pct, 9: FMT.rp, 10: FMT.rp }, widths: [18, 22, 22, 16, 20, 24, 14, 20, 16, 18, 22] };
        }

        self.postMessage({ type: 'progress', percent: 95, message: 'Finalizing sheets...' });
        self.postMessage({ type: 'complete', sheets, message: 'Sheets built successfully' });
      } catch (err) {
        self.postMessage({ type: 'error', message: 'Error building sheets: ' + err.message, error: err.toString() });
      }
    };
  `;

  function createWorker() {
    try {
      const blob = new Blob([EXPORT_WORKER_SOURCE], { type: 'application/javascript' });
      return new Worker(URL.createObjectURL(blob));
    } catch (e) {
      console.warn('Blob worker fallback failed, using direct worker attempt:', e);
      return null;
    }
  }

  // Buat worksheet dari Array-of-Arrays + opsi format kolom
  function makeSheet(aoa, colFormats, widths, totalRow) {
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    const range = XLSX.utils.decode_range(ws['!ref']);
    // format kolom (baris data mulai index 1)
    if (colFormats) {
      for (let r = 1; r <= range.e.r; r++) {
        Object.keys(colFormats).forEach(cStr => {
          const c = +cStr;
          const addr = XLSX.utils.encode_cell({ r, c });
          const cell = ws[addr];
          if (cell && (cell.t === 'n')) cell.z = colFormats[c];
          else if (cell && colFormats[c] === FMT.pct && cell.t === 's') { /* keep */ }
        });
      }
    }
    if (widths) ws['!cols'] = widths.map(w => ({ wch: w }));
    if (aoa.length > 1) ws['!autofilter'] = { ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: 0, c: range.e.c } }) };
    // freeze baris header (best-effort)
    ws['!freeze'] = { xSplit: 0, ySplit: 1, topLeftCell: 'A2', activePane: 'bottomLeft', state: 'frozen' };
    return ws;
  }

  function num(v) { return (v === '' || v === null || v === undefined) ? '' : Number(v); }

  let currentWorker = null;
  mod.progress = { active: false, percent: 0, message: 'Siap memproses export...' };

  function updateProgressUI(percent, message) {
    mod.progress = { active: true, percent, message };
    if (EVKIN.progress) EVKIN.progress({ title: 'Export Excel', percent, message: message || 'Memproses export...' });
    const bar = document.getElementById('exportProgressBar');
    const text = document.getElementById('exportProgressText');
    const pct = document.getElementById('exportProgressPct');
    const panel = document.getElementById('exportProgressPanel');
    if (panel) panel.style.display = 'block';
    if (bar) bar.style.width = percent + '%';
    if (text) text.textContent = message || 'Memproses export...';
    if (pct) pct.textContent = percent + '%';
  }

  function hideProgressUI() {
    const panel = document.getElementById('exportProgressPanel');
    if (panel) panel.style.display = 'none';
  }

  function saveWorkbookAsXlsx(wb, filename) {
    try {
      updateProgressUI(99, 'Menyimpan file Excel...');
      const wbData = XLSX.write(wb, { bookType: 'xlsx', type: 'array', compression: false, bookSST: false, cellFormula: false, cellDates: true });
      console.log('Export file size:', (wbData.length / 1024 / 1024).toFixed(2), 'MB');
      const blob = new Blob([wbData], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        URL.revokeObjectURL(url);
        a.remove();
      }, 1200);
      return true;
    } catch (err) {
      console.warn('Blob save failed, fallback to XLSX.writeFile:', err);
      try {
        XLSX.writeFile(wb, filename);
        return true;
      } catch (err2) {
        console.error('All export save strategies failed:', err2);
        return false;
      }
    }
  }

  // Helper: buat workbook dari sheets yang sudah diproses worker
  function buildWorkbookFromSheets(sheetsData) {
    const wb = XLSX.utils.book_new();
    Object.keys(sheetsData).forEach(sheetName => {
      const sheet = sheetsData[sheetName];
      if (sheet && sheet.aoa) {
        const ws = makeSheet(sheet.aoa, sheet.formats, sheet.widths);
        XLSX.utils.book_append_sheet(wb, ws, sheetName.slice(0, 31));
      }
    });
    return wb;
  }

  // Buat/proses sheets menggunakan Web Worker (non-blocking)
  mod.buildAsync = function (onProgress, onComplete, skipRawData, maxRows, dataSource) {
    // Terminate worker lama jika ada
    if (currentWorker) {
      currentWorker.terminate();
      currentWorker = null;
    }

    // Buat worker baru dengan pendekatan Blob URL agar aman di browser file:// / local
    currentWorker = createWorker();
    if (!currentWorker) {
      EVKIN.toast('Browser tidak mendukung worker export. Fallback ke proses sinkron.', 'warn');
      const wb = mod.build();
      onComplete && onComplete(wb);
      return;
    }

    // Handle progress updates dari worker
    currentWorker.onmessage = function (e) {
      const msg = e.data;
      if (msg.type === 'progress') {
        updateProgressUI(msg.percent, msg.message);
        if (onProgress) onProgress(msg.percent, msg.message);
      } else if (msg.type === 'complete') {
        updateProgressUI(100, 'Selesai');
        if (EVKIN.progress) EVKIN.progress({ title: 'Export Excel', percent: 100, message: 'File Excel selesai dibuat.', state: 'done' });
        const wb = buildWorkbookFromSheets(msg.sheets);
        currentWorker.terminate();
        currentWorker = null;
        setTimeout(hideProgressUI, 700);
        if (onComplete) onComplete(wb);
      } else if (msg.type === 'error') {
        console.error('Export worker error:', msg.error);
        updateProgressUI(0, 'Export gagal');
        if (EVKIN.progress) EVKIN.progress({ title: 'Export Excel gagal', message: msg.message, state: 'error' });
        EVKIN.toast('Error saat proses export: ' + msg.message, 'error');
        currentWorker.terminate();
        currentWorker = null;
        setTimeout(hideProgressUI, 1200);
        if (onComplete) onComplete(null);
      }
    };

    currentWorker.onerror = function (e) {
      console.error('Worker execution error:', e.message);
      EVKIN.toast('Kesalahan worker: ' + e.message, 'error');
      currentWorker.terminate();
      currentWorker = null;
      if (onComplete) onComplete(null);
    };

    // Kirim data ke worker untuk diproses
    const MAX_ROWS_DEFAULT = 0;
    const source = dataSource || {
      rawHeaders: S.rawHeaders,
      raw: S.raw,
      daily: S.daily,
      rekap: S.rekap,
      finalData: S.final,
      finalSKP: S.finalSKP,
      finalKombinasi: S.finalKombinasi,
      finalCombinedSKP: S.finalCombinedSKP
    };
    const payload = {
      rawHeaders: source.rawHeaders || [],
      raw: source.raw || [],
      daily: source.daily || [],
      rekap: source.rekap || [],
      finalData: source.finalData || [],
      finalSKP: source.finalSKP || { enabled: false, rows: [] },
      finalKombinasi: source.finalKombinasi || { enabled: false, rows: [] },
      finalCombinedSKP: source.finalCombinedSKP || { absensiProcessed: false },
      skipRawData: skipRawData || false,
      maxRows: maxRows || MAX_ROWS_DEFAULT
    };

    currentWorker.postMessage({
      action: 'buildSheets',
      payload: payload
    });
  };

  function resolveExportSource() {
    if (EVKIN.exportBackend && EVKIN.exportBackend.enabled) return 'backend';
    return 'browser';
  }

  mod.getAvailableSheets = function () {
    const sheets = ['DATA SUMBER', 'HITUNG HARIAN', 'REKAP SDM', 'FINAL ABSENSI'];
    if (S.finalSKP && S.finalSKP.enabled) sheets.push('FINAL + SKP');
    if (S.finalKombinasi && (S.finalKombinasi.enabled || S.finalKombinasi.useEabsensi || S.finalKombinasi.useEsdm)) sheets.push('FINAL KOMBINASI');
    if (S.finalCombinedSKP && S.finalCombinedSKP.absensiProcessed) sheets.push('FINAL + SKP KOMBINASI');
    return sheets;
  };

  // Fallback: build synchronous (jika worker tidak tersedia)
  mod.build = function () {
    const wb = XLSX.utils.book_new();

    // 1. DATA SUMBER (asli)
    const srcHead = S.rawHeaders.length ? S.rawHeaders : ['(kosong)'];
    const srcRows = S.raw.map(r => srcHead.map(h => r[h] !== undefined ? r[h] : ''));
    XLSX.utils.book_append_sheet(wb, makeSheet([srcHead, ...srcRows], null, srcHead.map(() => 16)), 'DATA SUMBER');

    // 2. HITUNG HARIAN
    const dH = ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Hari', 'Jam Masuk', 'Jam Pulang', 'Durasi', 'Zona', 'Jam Normalisasi', 'Metode', 'Jenis Absensi', 'Status Hari Libur', 'Status Cuti', 'Status Duplikasi', 'Absensi Lengkap', 'Pot Keterlambatan', 'Pot Pulang Cepat', 'Pot Durasi', 'Pot Tidak Lengkap', 'Total Potongan Harian', 'Flag', 'Tanggal/Jam Masuk', 'Tanggal/Jam Pulang', 'Status Jadwal', 'Status Absensi'];
    const dRows = S.daily.map(x => [x.nip, x.nama, x.jabatan, x.tanggal, x.hari, x.jamMasuk, x.jamPulang, x.durasi, x.zona, x.jamNormalisasi, x.metode, x.jenis, x.statusHariLibur, x.statusCuti, x.statusDuplikasi, x.statusLengkap, num(x.potKeterlambatan), num(x.potPulangCepat), num(x.potDurasi), num(x.potTidakLengkap), num(x.totalHarian), x.flags.join('; '), x.masukAt || '', x.pulangAt || '', x.statusJadwal || '', x.statusAbsensi || '']);
    XLSX.utils.book_append_sheet(wb, makeSheet([dH, ...dRows], { 16: FMT.pct, 17: FMT.pct, 18: FMT.pct, 19: FMT.pct, 20: FMT.pct }, [18, 22, 22, 12, 8, 9, 9, 9, 7, 16, 10, 16, 18, 14, 12, 12, 12, 12, 10, 12, 14, 30, 20, 20, 14, 20]), 'HITUNG HARIAN');

    // 3. REKAP SDM
    const rH = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Hari Efektif', 'Hari Ada Absensi', 'Hari Kurang', 'Jumlah Hari Absen', 'Pot Keterlambatan %', 'Pot Pulang Cepat %', 'Pot Durasi %', 'Pot Hari Kurang %', 'Pot Tidak Rekam %', 'Total Potongan Kehadiran %', 'Status'];
    const rRows = S.rekap.map(x => [x.nip, x.nama, x.jabatan, U.monthLabel(x.bulan), x.hariEfektif, x.hariAdaAbsensi, x.hariKurang, x.jumlahHariAbsen, num(x.potKeterlambatan), num(x.potPulangCepat), num(x.potDurasi), num(x.potHariKurang), num(x.potTidakLengkap), num(x.total), x.status]);
    XLSX.utils.book_append_sheet(wb, makeSheet([rH, ...rRows], { 8: FMT.pct, 9: FMT.pct, 10: FMT.pct, 11: FMT.pct, 12: FMT.pct, 13: FMT.pct }, [18, 22, 22, 14, 11, 14, 11, 14, 12, 12, 10, 12, 12, 16, 14]), 'REKAP SDM');

    // 4. SEMI FINAL (verifikasi anomali)
    const sfH = ['NIP', 'Nama', 'Tanggal', 'Jam Masuk', 'Jam Pulang', 'Jenis', 'Status Duplikasi', 'Absensi Lengkap', 'Status Hari Libur', 'Status Cuti', 'Flag', 'Total Harian'];
    const sfRows = S.daily.filter(x => x.flags.length || x.statusDuplikasi || x.statusLengkap === 'Tidak Lengkap').map(x => [x.nip, x.nama, x.tanggal, x.jamMasuk, x.jamPulang, x.jenis, x.statusDuplikasi, x.statusLengkap, x.statusHariLibur, x.statusCuti, x.flags.join('; '), num(x.totalHarian)]);
    XLSX.utils.book_append_sheet(wb, makeSheet([sfH, ...(sfRows.length ? sfRows : [['-', 'Tidak ada anomali', '', '', '', '', '', '', '', '', '', '']])], { 11: FMT.pct }, [18, 22, 12, 9, 9, 14, 14, 14, 18, 14, 30, 12]), 'SEMI FINAL');

    // 5. FINAL ABSENSI
    const fH = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Total Potongan Kehadiran %', 'Nominal Potongan Kehadiran', 'FINAL TUKIN DIBAYAR'];
    const fRows = S.final.map(f => [f.nip, f.nama, f.jabatan, U.monthLabel(f.bulan), num(f.tukin), num(f.komponen), num(f.potPct), num(f.nominal), num(f.finalDibayar)]);
    const totB = S.final.reduce((a, f) => a + f.tukin, 0), totN = S.final.reduce((a, f) => a + f.nominal, 0), totP = S.final.reduce((a, f) => a + f.finalDibayar, 0);
    fRows.push(['', '', '', 'TOTAL', num(totB), '', '', num(totN), num(totP)]);
    XLSX.utils.book_append_sheet(wb, makeSheet([fH, ...fRows], { 4: FMT.rp, 5: FMT.rp, 6: FMT.pct, 7: FMT.rp, 8: FMT.rp }, [18, 22, 22, 14, 16, 20, 20, 22, 20]), 'FINAL ABSENSI');

    // 6. ANOMALI
    const aH = ['NIP', 'Nama', 'Tanggal', 'Jenis Anomali', 'Detail'];
    const aRows = [];
    S.daily.forEach(x => { if (x.statusDuplikasi) aRows.push([x.nip, x.nama, x.tanggal, 'Duplikasi NIP+Tanggal', x.jamMasuk + ' - ' + x.jamPulang]); });
    S.daily.forEach(x => { if (x.statusLengkap === 'Tidak Lengkap') aRows.push([x.nip, x.nama, x.tanggal, 'Absensi Tidak Lengkap', 'Masuk/Pulang kosong']); });
    S.data.filter(d => d.issues && d.issues.length).forEach(d => aRows.push([d.nip, d.nama, d.tanggal.str, 'Validasi', d.issues.join('; ')]));
    XLSX.utils.book_append_sheet(wb, makeSheet([aH, ...(aRows.length ? aRows : [['-', 'Tidak ada anomali', '', '', '']])], null, [18, 22, 12, 22, 40]), 'ANOMALI');

    // 7. FINAL + SKP (opsional)
    if (S.finalSKP.enabled) {
      const skH = ['NIP', 'Nama', 'Tukin Bruto', 'Nominal Pot Kehadiran', 'Potongan SKP %', 'Nominal Pot SKP', 'FINAL TUKIN DIBAYAR'];
      const skRows = S.final.map(f => {
        const sr = S.finalSKP.rows.find(x => x.nip === f.nip) || { skpPct: 0 };
        const nomSKP = f.tukin * 0.70 * (Math.min(100, sr.skpPct) / 100);
        return [f.nip, f.nama, num(f.tukin), num(f.nominal), num(sr.skpPct), num(nomSKP), num(f.tukin - f.nominal - nomSKP)];
      });
      XLSX.utils.book_append_sheet(wb, makeSheet([skH, ...skRows], { 2: FMT.rp, 3: FMT.rp, 4: FMT.pct, 5: FMT.rp, 6: FMT.rp }, [18, 22, 16, 20, 14, 18, 20]), 'FINAL + SKP');
    }

    // 8. FINAL KOMBINASI (opsional)
    if (S.finalKombinasi.enabled || S.finalKombinasi.useEabsensi || S.finalKombinasi.useEsdm) {
      const K = S.finalKombinasi;
      const kH = ['NIP', 'Nama', 'Aplikasi %', 'e-Absensi %', 'ESDM %', 'Potongan Terpilih %', 'Nominal Potongan', 'FINAL TUKIN DIBAYAR'];
      const kRows = S.final.map(f => {
        const kr = K.rows.find(x => x.nip === f.nip) || { eabs: 0, esdm: 0 };
        const cand = []; if (K.useApp) cand.push(f.potPct); if (K.useEabsensi) cand.push(kr.eabs); if (K.useEsdm) cand.push(kr.esdm);
        const minPct = cand.length ? Math.min(...cand) : 0;
        const nominal = f.komponen * (Math.min(100, minPct) / 100);
        return [f.nip, f.nama, num(f.potPct), num(kr.eabs), num(kr.esdm), num(minPct), num(nominal), num(f.tukin - nominal)];
      });
      XLSX.utils.book_append_sheet(wb, makeSheet([kH, ...kRows], { 2: FMT.pct, 3: FMT.pct, 4: FMT.pct, 5: FMT.pct, 6: FMT.rp, 7: FMT.rp }, [18, 22, 12, 12, 10, 16, 18, 20]), 'FINAL KOMBINASI');
    }

    // 9. FINAL + SKP KOMBINASI (TAB BARU - UTAMA)
    if (S.finalCombinedSKP && S.finalCombinedSKP.absensiProcessed) {
      const FC = S.finalCombinedSKP;
      const fcH = ['NIP', 'Nama', 'Jabatan', 'Tukin Bruto', 'Potongan Absensi % (min)', 'Nominal Potongan Absensi (30%)', 'Potongan SKP %', 'Nominal Potongan SKP (70%)', 'Total Potongan %', 'Total Nominal Potongan', 'FINAL TUKIN DIBAYARKAN'];
      const fcRows = S.final.map(f => {
        const cr = S.finalCombinedSKP.combinedRows.find(x => x.nip === f.nip) || { skpPct: 0 };
        const komKehadiran = f.tukin * 0.30;
        const komKinerja = f.tukin * 0.70;

        // Potongan Absensi (30%)
        const absensiCand = [];
        if (FC.useApp) absensiCand.push(f.potPct);
        if (FC.useEabsensi) {
          const eabsRow = FC.eabsensiRows.find(x => x.nip === f.nip);
          if (eabsRow) absensiCand.push(eabsRow.pct);
        }
        if (FC.useEsdm) {
          const esdmRow = FC.esdmRows.find(x => x.nip === f.nip);
          if (esdmRow) absensiCand.push(esdmRow.pct);
        }

        const finalAbsensiPct = absensiCand.length ? Math.min(...absensiCand) : f.potPct;
        const nomAbsensi = komKehadiran * (Math.min(100, finalAbsensiPct) / 100);

        // Potongan SKP (70%)
        const skpPct = cr.skpPct;
        const nomSKP = komKinerja * (Math.min(100, skpPct) / 100);

        // Total
        const totalPotPct = Math.round((finalAbsensiPct * 0.30 + skpPct * 0.70) * 100) / 100;
        const totalNominal = nomAbsensi + nomSKP;
        const tukinDibayar = f.tukin - totalNominal;

        return [f.nip, f.nama, f.jabatan, num(f.tukin), num(finalAbsensiPct), num(nomAbsensi), num(skpPct), num(nomSKP), num(totalPotPct), num(totalNominal), num(tukinDibayar)];
      });

      const totBruto = S.final.reduce((a, f) => a + f.tukin, 0);
      const totAbsensi = S.final.reduce((a, f) => {
        const komKehadiran = f.tukin * 0.30;
        const absensiCand = [];
        if (FC.useApp) absensiCand.push(f.potPct);
        if (FC.useEabsensi) {
          const eabsRow = FC.eabsensiRows.find(x => x.nip === f.nip);
          if (eabsRow) absensiCand.push(eabsRow.pct);
        }
        if (FC.useEsdm) {
          const esdmRow = FC.esdmRows.find(x => x.nip === f.nip);
          if (esdmRow) absensiCand.push(esdmRow.pct);
        }
        const finalAbsensiPct = absensiCand.length ? Math.min(...absensiCand) : f.potPct;
        return a + (komKehadiran * (Math.min(100, finalAbsensiPct) / 100));
      }, 0);
      const totSKP = S.final.reduce((a, f) => {
        const cr = S.finalCombinedSKP.combinedRows.find(x => x.nip === f.nip) || { skpPct: 0 };
        return a + (f.tukin * 0.70 * (Math.min(100, cr.skpPct) / 100));
      }, 0);
      const totPotongan = totAbsensi + totSKP;
      const totDibayar = totBruto - totPotongan;

      fcRows.push(['', '', 'TOTAL', num(totBruto), '', num(totAbsensi), '', num(totSKP), '', num(totPotongan), num(totDibayar)]);
      XLSX.utils.book_append_sheet(wb, makeSheet([fcH, ...fcRows], { 4: FMT.pct, 5: FMT.rp, 6: FMT.pct, 7: FMT.rp, 8: FMT.pct, 9: FMT.rp, 10: FMT.rp }, [18, 22, 22, 16, 18, 22, 14, 18, 14, 20, 22]), 'FINAL + SKP KOMBINASI');
    }

    return wb;
  };

  function slug(name) { return name.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '').toUpperCase(); }

  // Satu workbook berisi sheet terpilih (dengan worker + progress)
  mod.downloadSelected = function (names, smartMode) {
    const fullNames = (names && names.length) ? names : ['DATA SUMBER', 'HITUNG HARIAN', 'REKAP SDM', 'FINAL ABSENSI'];
    const finalNames = smartMode && fullNames.includes('DATA SUMBER') ? fullNames.filter(n => n !== 'DATA SUMBER') : fullNames;
    // PRIORITAS: backend DuckDB master-only jika aktif; IndexedDB hanya menjadi fallback non-master
    const backendEnabled = !!(EVKIN.exportBackend && EVKIN.exportBackend.enabled);
    const hasDatabaseModule = !!(EVKIN.db && typeof EVKIN.db.getAll === 'function');

    if (backendEnabled) {
      EVKIN.toast('Export memakai master database DuckDB...', 'info');
      updateProgressUI(10, 'Mengambil data dari backend DuckDB...');
      EVKIN.exportBackend.downloadWorkbook({
        bulan: S.month || '2026-08',
        unit: null,
        sheetNames: finalNames,
        apiBase: EVKIN.exportBackend.apiBase
      }).then(() => updateProgressUI(100, 'Export backend selesai')).catch((err) => {
        updateProgressUI(100, 'Export backend gagal');
        EVKIN.toast('Export backend gagal: ' + (err.message || err), 'error');
      });
      return;
    }

    if (hasDatabaseModule) {
      if (EVKIN.db.isCacheStale && EVKIN.db.isCacheStale()) {
        EVKIN.toast('Cache browser berasal dari sebelum Reset Data Operasional dan belum berhasil dibersihkan. Offline export dinonaktifkan sampai cache dibersihkan.', 'warning');
        updateProgressUI(100, 'Cache browser lama harus dibersihkan sebelum export offline.');
        return;
      }
      const filename = 'EVKIN_ABSENSI_DB_' + (S.month || 'export') + (smartMode ? '_smart' : '') + '.xlsx';
      EVKIN.toast('Export dari database hasil hitung harian + rekap + final...', 'info');
      updateProgressUI(10, 'Membaca data dari IndexedDB...');

      const buildDatabaseWorkbook = async () => {
        try {
          // PRIORITAS 1: Baca dari database (lebih reliable untuk data besar)
          const readStore = (name, selected) => selected ? EVKIN.db.getAll(name).catch(() => []) : Promise.resolve([]);
          const [dailyRows, rekapRows, finalRows, rawRows, skpRows, kombinasiRows, combinedRows] = await Promise.all([
            readStore('daily_data', finalNames.includes('HITUNG HARIAN')),
            readStore('rekap_sdm', finalNames.includes('REKAP SDM')),
            readStore('final_absensi', finalNames.includes('FINAL ABSENSI')),
            readStore('raw_import', finalNames.includes('DATA SUMBER')),
            readStore('final_skp', finalNames.includes('FINAL + SKP')),
            readStore('final_kombinasi', finalNames.includes('FINAL KOMBINASI')),
            readStore('final_combined_skp', finalNames.includes('FINAL + SKP KOMBINASI'))
          ]);

          updateProgressUI(20, 'Data dari database dibaca...');

          // FALLBACK: Jika database kosong, gunakan state (data belum disimpan)
          const usedDaily = dailyRows.length > 0 ? dailyRows : (S.daily || []);
          const usedRekap = rekapRows.length > 0 ? rekapRows : (S.rekap || []);
          const usedFinal = finalRows.length > 0 ? finalRows : (S.final || []);
          const usedRaw = rawRows.length > 0 ? rawRows : (S.raw || []);
          const usedSKP = skpRows.length > 0 ? skpRows : (S.finalSKP && S.finalSKP.rows) || [];
          const usedKombinasi = kombinasiRows.length > 0 ? kombinasiRows : (S.finalKombinasi && S.finalKombinasi.rows) || [];
          const usedCombined = combinedRows.length > 0 ? combinedRows : (S.finalCombinedSKP && S.finalCombinedSKP.combinedRows) || [];

          if (!usedDaily.length && !usedRekap.length && !usedFinal.length) {
            throw new Error('Tidak ada data untuk diekspor. Pastikan: 1) Hitung Harian sudah dijalankan, 2) Rekap SDM sudah dijalankan, 3) Final sudah dijalankan');
          }

          updateProgressUI(30, 'Membangun workbook langsung dari IndexedDB...');

          const wb = XLSX.utils.book_new();
          const addSheet = (sheetName, headers, rows) => {
            if (!sheetName || !headers || !rows || !rows.length) return;
            const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
            XLSX.utils.book_append_sheet(wb, ws, sheetName.slice(0, 31));
          };

          // DATA SUMBER
          if (finalNames.includes('DATA SUMBER') && usedRaw.length) {
            const srcHead = S.rawHeaders && S.rawHeaders.length ? S.rawHeaders : Object.keys(usedRaw[0] || {});
            const srcRows = usedRaw.map(r => srcHead.map(h => r[h] !== undefined ? r[h] : ''));
            addSheet('DATA SUMBER', srcHead, srcRows);
          }

          // HITUNG HARIAN
          if (finalNames.includes('HITUNG HARIAN') && usedDaily.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Hari', 'Jam Masuk', 'Jam Pulang', 'Durasi', 'Zona', 'Jam Normalisasi', 'Metode', 'Jenis Absensi', 'Status Hari Libur', 'Status Cuti', 'Status Duplikasi', 'Absensi Lengkap', 'Pot Keterlambatan', 'Pot Pulang Cepat', 'Pot Durasi', 'Pot Tidak Lengkap', 'Total Potongan Harian', 'Flag'];
            const rows = usedDaily.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.tanggal || '', x.hari || '', x.jamMasuk || '', x.jamPulang || '', x.durasi ?? '', x.zona || '', x.jamNormalisasi || '', x.metode || '', x.jenis || '', x.statusHariLibur || '', x.statusCuti || '', x.statusDuplikasi ? 'Ya' : 'Tidak', x.statusLengkap || '', x.potKeterlambatan ?? '', x.potPulangCepat ?? '', x.potDurasi ?? '', x.potTidakLengkap ?? '', x.totalHarian ?? '', (x.flags || []).join('; ')]);
            addSheet('HITUNG HARIAN', headers, rows);
          }

          // REKAP SDM
          if (finalNames.includes('REKAP SDM') && usedRekap.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Hari Efektif', 'Hari Ada Absensi', 'Hari Kurang', 'Jumlah Hari Absen', 'Pot Keterlambatan', 'Pot Pulang Cepat', 'Pot Durasi', 'Pot Hari Kurang', 'Pot Tidak Rekam', 'Total Potongan Kehadiran %', 'Status'];
            const rows = usedRekap.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.bulan || '', x.hariEfektif ?? '', x.hariAdaAbsensi ?? '', x.hariKurang ?? '', x.jumlahHariAbsen ?? '', x.potKeterlambatan ?? '', x.potPulangCepat ?? '', x.potDurasi ?? '', x.potHariKurang ?? '', x.potTidakLengkap ?? '', x.total ?? '', x.status || '']);
            addSheet('REKAP SDM', headers, rows);
          }

          // FINAL ABSENSI
          if (finalNames.includes('FINAL ABSENSI') && usedFinal.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Total Potongan Kehadiran %', 'Nominal Potongan Kehadiran', 'FINAL TUKIN DIBAYAR'];
            const rows = usedFinal.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.bulan || '', x.tukin ?? '', x.komponen ?? '', x.potPct ?? '', x.nominal ?? '', x.finalDibayar ?? '']);
            addSheet('FINAL ABSENSI', headers, rows);
          }

          if (finalNames.includes('FINAL + SKP') && usedSKP.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Potongan Kehadiran %', 'Nominal Pot Kehadiran', 'Potongan SKP %', 'Nominal Pot SKP', 'FINAL TUKIN DIBAYAR'];
            const rows = usedSKP.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.bulan || '', x.tukin ?? '', x.komponen ?? '', x.potPct ?? '', x.nominal ?? '', x.skpPct ?? '', x.nominalSKP ?? '', x.finalDibayarSKP ?? '']);
            addSheet('FINAL + SKP', headers, rows);
          }

          if (finalNames.includes('FINAL KOMBINASI') && usedKombinasi.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Aplikasi %', 'e-Absensi %', 'ESDM %', 'Potongan Terpilih %', 'Nominal Potongan', 'FINAL TUKIN DIBAYAR'];
            const rows = usedKombinasi.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.bulan || '', x.tukin ?? '', x.komponen ?? '', x.aplikasiPct ?? '', x.eabsensiPct ?? '', x.esdmPct ?? '', x.potonganTerpilihPct ?? '', x.nominalKombinasi ?? '', x.finalDibayarKombinasi ?? '']);
            addSheet('FINAL KOMBINASI', headers, rows);
          }

          if (finalNames.includes('FINAL + SKP KOMBINASI') && usedCombined.length) {
            const headers = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Tukin Bruto', 'Komponen Kehadiran 30%', 'Potongan Absensi %', 'Nominal Potongan Absensi', 'Potongan SKP %', 'Nominal Potongan SKP', 'Total Potongan %', 'Total Nominal Potongan', 'FINAL TUKIN DIBAYAR'];
            const rows = usedCombined.map(x => [x.nip || '', x.nama || '', x.jabatan || '', x.bulan || '', x.tukin ?? '', x.komponen ?? '', x.potonganAbsensiPct ?? '', x.nominalPotonganAbsensi ?? '', x.potonganSKPPct ?? '', x.nominalPotonganSKP ?? '', x.totalPotonganPct ?? '', x.totalNominalPotongan ?? '', x.finalDibayarKombinasiSKP ?? '']);
            addSheet('FINAL + SKP KOMBINASI', headers, rows);
          }

          if (!wb.SheetNames.length) {
            throw new Error('Tidak ada sheet yang berhasil dibuat. Periksa data di setiap tahap.');
          }

          updateProgressUI(90, 'Menyimpan file Excel...');
          if (!saveWorkbookAsXlsx(wb, filename)) throw new Error('Browser gagal menyimpan file Excel');
          updateProgressUI(100, 'Export berhasil!');
          EVKIN.toast('✓ Export berhasil: ' + filename + ' (' + wb.SheetNames.length + ' sheet)', 'success');
        } catch (err) {
          console.error('Database export error:', err);
          throw err;
        }
      };

      buildDatabaseWorkbook().catch((err) => {
        console.error('Database export failed', err);
        EVKIN.toast('Data dari database belum siap, fallback ke browser export.', 'warn');
        mod.buildAsync(
          (percent, message) => { EVKIN.toast(`${message} (${percent}%)`, 'info'); },
          (wb) => {
            if (!wb) return;
            const filtered = XLSX.utils.book_new();
            finalNames.forEach(sheetName => {
              if (wb.Sheets[sheetName]) XLSX.utils.book_append_sheet(filtered, wb.Sheets[sheetName], sheetName.slice(0, 31));
            });
            const fallbackName = 'EVKIN_ABSENSI_' + (S.month || 'export') + (smartMode ? '_smart' : '') + '.xlsx';
            saveWorkbookAsXlsx(filtered, fallbackName);
          },
          smartMode,
          0
        );
      });
      return;
    }

    // Auto limit rows untuk dataset sangat besar
    const MAX_ROWS = 0;
    const rowsWarning = '';

    updateProgressUI(0, 'Menyiapkan export' + (smartMode ? ' (Smart Mode, skip raw data)...' : '...'));
    EVKIN.toast('Preparing export (0%)' + rowsWarning + '...', 'info');

    mod.buildAsync(
      (percent, message) => {
        EVKIN.toast(`${message} (${percent}%)`, 'info');
      },
      (wb) => {
        if (!wb) return;
        const filtered = XLSX.utils.book_new();
        finalNames.forEach(sheetName => {
          if (wb.Sheets[sheetName]) {
            XLSX.utils.book_append_sheet(filtered, wb.Sheets[sheetName], sheetName.slice(0, 31));
          }
        });
        const filename = 'EVKIN_ABSENSI_' + (S.month || 'export') + (smartMode ? '_smart' : '') + '.xlsx';
        setTimeout(() => {
          const ok = saveWorkbookAsXlsx(filtered, filename);
          if (ok) {
            EVKIN.toast('Export berhasil: ' + filename + ' (' + filtered.SheetNames.length + ' sheet)' + rowsWarning, 'success');
            EVKIN.auditMod.logDataExport(filename, finalNames.length);
          } else {
            EVKIN.toast('Export gagal disimpan. Coba kurangi jumlah sheet atau data yang dipilih.', 'error');
          }
        }, 120);
      },
      smartMode,
      MAX_ROWS
    );
  };

  // File .xlsx terpisah per sheet (dengan worker + progress)
  mod.downloadSeparate = function (names, smartMode) {
    const fullNames = (names && names.length) ? names : ['DATA SUMBER', 'HITUNG HARIAN', 'REKAP SDM', 'FINAL ABSENSI'];
    const finalNames = smartMode && fullNames.includes('DATA SUMBER') ? fullNames.filter(n => n !== 'DATA SUMBER') : fullNames;

    const MAX_ROWS = 0;
    const rowsWarning = '';

    updateProgressUI(0, 'Menyiapkan export terpisah' + (smartMode ? ' (Smart Mode)...' : '...'));
    EVKIN.toast('Preparing export (0%)' + rowsWarning + '...', 'info');

    mod.buildAsync(
      (percent, message) => {
        EVKIN.toast(`${message} (${percent}%)`, 'info');
      },
      (wb) => {
        if (!wb) return;
        let n = 0;
        finalNames.forEach(sheetName => {
          if (!wb.Sheets[sheetName]) return;
          const single = XLSX.utils.book_new();
          XLSX.utils.book_append_sheet(single, wb.Sheets[sheetName], sheetName.slice(0, 31));
          const filename = 'EVKIN_' + slug(sheetName) + '_' + (S.month || 'export') + (smartMode ? '_smart' : '') + '.xlsx';
          saveWorkbookAsXlsx(single, filename);
          n++;
        });
        EVKIN.toast(`Export berhasil: ${n} file terpisah` + rowsWarning, 'success');
        EVKIN.auditMod.logDataExport('EVKIN_[sheets]_' + (S.month || 'export') + (smartMode ? '_smart' : '') + '.xlsx', n);
      },
      smartMode,
      MAX_ROWS
    );
  };

  mod.download = function () { mod.downloadSelected(); };

  // PRIORITAS 1: Simpan hasil perhitungan ke IndexedDB
  mod.saveAllToDatabase = function () {
    if (!EVKIN.db) {
      EVKIN.toast('IndexedDB tidak tersedia', 'error');
      return;
    }

    updateProgressUI(5, 'Menyimpan raw data...');
    const promises = [];

    if (S.raw && S.raw.length) {
      promises.push(EVKIN.db.replaceStore('raw_import', S.raw)
        .then(() => updateProgressUI(20, 'Raw data tersimpan')));
    }

    if (S.daily && S.daily.length) {
      promises.push(EVKIN.db.replaceStore('daily_data', S.daily)
        .then(() => updateProgressUI(40, 'Daily data tersimpan')));
    }

    if (S.rekap && S.rekap.length) {
      promises.push(EVKIN.db.replaceStore('rekap_sdm', S.rekap)
        .then(() => updateProgressUI(60, 'Rekap SDM tersimpan')));
    }

    if (S.final && S.final.length) {
      promises.push(EVKIN.db.replaceStore('final_absensi', S.final)
        .then(() => updateProgressUI(80, 'Final absensi tersimpan')));
    }

    Promise.all(promises)
      .then(() => {
        updateProgressUI(100, 'Semua data berhasil disimpan ke IndexedDB');
        EVKIN.toast('✓ Semua hasil perhitungan disimpan ke IndexedDB (' + (S.daily.length + S.rekap.length + S.final.length) + ' records)', 'success');
        setTimeout(hideProgressUI, 1500);
      })
      .catch(err => {
        console.error('Save to database failed:', err);
        EVKIN.toast('Gagal menyimpan ke IndexedDB: ' + err.message, 'error');
        updateProgressUI(0, 'Error');
        setTimeout(hideProgressUI, 2000);
      });
  };

  // PRIORITAS 2: Pakai Web Worker untuk tugas berat
  mod.processWithWorker = function (sheetNames) {
    const chosen = sheetNames || mod.getAvailableSheets();
    updateProgressUI(5, 'Memulai Web Worker untuk pemrosesan...');

    mod.buildAsync(
      (percent, message) => {
        updateProgressUI(percent, message);
      },
      (wb) => {
        if (!wb) {
          EVKIN.toast('Pemrosesan Worker gagal', 'error');
          return;
        }
        const filename = 'EVKIN_ABSENSI_WebWorker_' + (S.month || 'export') + '.xlsx';
        const ok = saveWorkbookAsXlsx(wb, filename);
        if (ok) {
          EVKIN.toast('✓ Export via Web Worker berhasil: ' + filename, 'success');
          EVKIN.auditMod.logDataExport(filename, chosen.length);
        }
      },
      false,
      0
    );
  };

  // PRIORITAS 3: Export via Backend lokal
  mod.downloadViaBackend = function (sheetNames) {
    if (!EVKIN.exportBackend) {
      EVKIN.toast('Backend export module tidak tersedia. Pastikan export-backend.js sudah dimuat.', 'error');
      return;
    }

    const chosen = sheetNames || mod.getAvailableSheets();
    updateProgressUI(10, 'Membaca workbook dari database backend...');

    const month = S.month || 'export';
    const unit = (document.getElementById('exportUnitSelect') || {}).value || null;

    EVKIN.exportBackend.startExportJob({ bulan: month, sheetNames: chosen, unit: unit, format: 'xlsx' }, (p, msg) => {
      updateProgressUI(10 + Math.round(p * 90), msg || 'Export di server...');
    })
      .then(() => {
        updateProgressUI(100, 'Export backend selesai');
        EVKIN.toast('✓ Export via backend berhasil', 'success');
        EVKIN.auditMod.logDataExport('EVKIN_ABSENSI_' + month + '_backend.xlsx', chosen.length);
        setTimeout(hideProgressUI, 1500);
      })
      .catch(err => {
        console.error('Backend export failed:', err);
        EVKIN.toast('❌ Backend tidak tersedia atau error: ' + (err && err.message ? err.message : err), 'error');
        updateProgressUI(0, 'Backend error');
        setTimeout(hideProgressUI, 2500);
      });
  };

  // PRIORITAS 4: Smart Export - coba database dulu, fallback browser
  mod.smartExport = function (sheetNames) {
    const chosen = sheetNames || mod.getAvailableSheets();
    const isLargeDataset = (S.daily && S.daily.length > 20000) || (S.raw && S.raw.length > 20000);

    if (isLargeDataset && EVKIN.db && EVKIN.exportBackend) {
      EVKIN.toast('📊 Dataset besar - membaca dari backend...', 'info');
      updateProgressUI(5, 'Membaca workbook dari backend untuk sheet terpilih...');

      EVKIN.exportBackend.startExportJob({
        bulan: S.month || 'export',
        unit: null,
        sheetNames: chosen,
        format: 'xlsx',
        apiBase: EVKIN.exportBackend.apiBase
      }, (progress, message) => {
        updateProgressUI(5 + Math.round(Math.min(1, Number(progress) || 0) * 90), message);
      })
        .then(() => {
          updateProgressUI(100, 'Smart export via backend berhasil');
          EVKIN.toast('✓ Smart export berhasil via backend', 'success');
          EVKIN.auditMod.logDataExport('EVKIN_ABSENSI_' + (S.month || 'export') + '_smart.xlsx', chosen.length);
          setTimeout(hideProgressUI, 1500);
        })
        .catch(err => {
          console.warn('Backend error during smart export:', err);
          if (EVKIN.exportBackend && EVKIN.exportBackend.enabled) {
            EVKIN.toast('Backend aktif namun terjadi error: ' + (err && err.message ? err.message : err), 'error');
            updateProgressUI(0, 'Backend error');
            setTimeout(hideProgressUI, 2500);
            return; // Do not fallback when backend is active
          }
          updateProgressUI(10, 'Backend tidak tersedia, fallback ke Web Worker...');
          mod.buildAsync(
            (percent, message) => updateProgressUI(percent, message),
            (wb) => {
              if (!wb) return;
              const filename = 'EVKIN_ABSENSI_' + (S.month || 'export') + '_fallback.xlsx';
              saveWorkbookAsXlsx(wb, filename);
              EVKIN.toast('✓ Smart export via Web Worker (backend fallback)', 'success');
              EVKIN.auditMod.logDataExport(filename, chosen.length);
            },
            true,
            0
          );
        });
    } else {
      updateProgressUI(5, 'Dataset kecil - export via Web Worker');
      mod.buildAsync(
        (percent, message) => updateProgressUI(percent, message),
        (wb) => {
          if (!wb) return;
          const filename = 'EVKIN_ABSENSI_' + (S.month || 'export') + '.xlsx';
          saveWorkbookAsXlsx(wb, filename);
          EVKIN.toast('✓ Export berhasil', 'success');
          EVKIN.auditMod.logDataExport(filename, chosen.length);
        },
        false,
        8000
      );
    }
  };

  mod.render = function (el) {
    if (!EVKIN.stepUnlocked('export')) { el.innerHTML = `<div class="module-head"><h2>Export Rekap Absensi <span class="pill gray">🔒 Terkunci</span></h2></div><div class="alert warn">Selesaikan Rekap SDM terlebih dahulu.</div>`; return; }
    const sheets = mod.getAvailableSheets();
    el.innerHTML = `
      <div class="module-head"><div><h2>⑥ Export Rekap Absensi</h2><p class="muted">Pilih sheet yang akan dibuat menjadi satu workbook Excel. Data utama diambil dari backend DuckDB master.</p></div></div>

      <div class="card" style="background:linear-gradient(135deg, #f7fafc 0%, #eef4fb 100%); border:1px solid #d1e3f7;">
        <h3 style="color:#1a365d;">Sumber Data Export</h3>
        <div style="display:flex; gap:12px; align-items:center; margin-top:12px;">
          <div class="exp-modes">
            <div class="exp-mode" style="cursor:default; opacity:0.95;">
              <div><b>DuckDB Master Backend</b><span class="muted">Data export diambil langsung dari database master backend, tanpa pilihan sumber lokal IndexedDB.</span></div>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Sheet yang Disertakan</h3>
        <div class="sheet-select">
          ${sheets.map(s => `<label class="sheet-chk"><input type="checkbox" class="exp-sheet" value="${U.esc(s)}" checked data-testid="exp-sheet-${U.esc(slug(s))}"/> <span>${s}</span></label>`).join('')}
        </div>
        <p class="muted" style="margin-top:14px">Pilih satu atau beberapa sheet. Semua sheet terpilih akan masuk ke satu file Excel dengan struktur sesuai output master dari backend.</p>
        <div id="exportProgressPanel" class="card" style="display:none; margin-top:14px; border:1px solid #dfe7f5; background:#f7fafe;">
          <h4 style="margin:0 0 8px;">Proses Export</h4>
          <div style="width:100%; height:10px; background:#e5ecf8; border-radius:999px; overflow:hidden; margin-bottom:8px;">
            <div id="exportProgressBar" style="width:0%; height:100%; background:linear-gradient(90deg, #2d7ff9, #4ec3ff); transition:width 0.2s ease;"></div>
          </div>
          <div style="display:flex; justify-content:space-between; gap:10px; font-size:13px; color:#42526b;">
            <span id="exportProgressText">Menyiapkan export...</span>
            <strong id="exportProgressPct">0%</strong>
          </div>
        </div>
        <div class="actions-row"><button class="btn btn-success btn-lg" id="btnExport" data-testid="export-button"><i data-lucide="download"></i> Export ke Excel</button></div>
      </div>`;

    // Event handlers
    el.querySelector('#btnExport').onclick = () => {
      void (async function () {
        const chosen = Array.from(el.querySelectorAll('.exp-sheet:checked')).map(c => c.value);
        const backendReady = !!(EVKIN.exportBackend && EVKIN.exportBackend.enabled);
        if (!chosen.length) { EVKIN.toast('Pilih minimal satu sheet', 'error'); return; }
        const button = el.querySelector('#btnExport');
        if (button.disabled) return;
        button.disabled = true;
        try {
        let expectedFinal;

        if (backendReady) {
          const authority = await EVKIN.exportBackend.getAbsenceAuthority();
          const pythonCanonical = authority.authority === 'PYTHON_CANONICAL';
          if (pythonCanonical) {
            const latest = await EVKIN.exportBackend.getLatestAbsenceCalculation(S.month || '');
            if (latest.status !== 'COMPLETED' || !latest.canonical) {
              throw new Error('Final Absensi canonical backend belum siap untuk export.');
            }
            await EVKIN.exportBackend.hydrateCanonicalAbsence(S.month || '', latest.calculation_id);
          }
          // The user may enter Export from the sidebar, or change Final after
          // an earlier export. Persist just the current Final results first.
          if (!pythonCanonical && chosen.some(name => name.startsWith('FINAL')) && S.final.length) {
            if (S.final.some(row => row.tukin == null || row.tukin === '')) {
              EVKIN.toast('Final masih memiliki NIP tanpa Tukin bruto. Lengkapi Master SDM sebelum ekspor.', 'error');
              return;
            }
            updateProgressUI(5, 'Menyimpan seluruh hasil Final terbaru...');
            try {
              if (chosen.includes('FINAL ABSENSI')) expectedFinal = await EVKIN.exportBackend.finalManifest(S.final);
              await EVKIN.syncBackendWorkflow(undefined, { finalOnly: true });
            } catch (error) {
              updateProgressUI(0, 'Hasil Final gagal disimpan. Coba ekspor lagi.');
              EVKIN.toast('Export dibatalkan: ' + error.message, 'error');
              return;
            }
          }
          updateProgressUI(10, 'Menyiapkan export Excel di backend...');
          try {
            // Workflow stages already synchronize their results to DuckDB. Do not
            // resend the complete browser state here; large payloads made the UI
            // appear stuck at 10% before the export job even started.
            await EVKIN.exportBackend.startExportJob({
              bulan: S.month || 'export',
              unit: null,
              sheetNames: chosen,
              format: 'xlsx',
              expectedFinal,
              apiBase: EVKIN.exportBackend.apiBase
            }, (progress, message) => {
              const normalized = Math.max(0, Math.min(1, Number(progress) || 0));
              updateProgressUI(10 + Math.round(normalized * 85), message || 'Membuat file Excel di backend...');
            });
            updateProgressUI(100, expectedFinal
              ? `Validasi Excel lulus: ${expectedFinal.row_count} baris; bruto ${expectedFinal.gross}, potongan ${expectedFinal.deduction}, dibayar ${expectedFinal.paid}`
              : 'Export backend selesai');
            EVKIN.toast('✓ Export via backend berhasil', 'success');
            EVKIN.auditMod.logDataExport('EVKIN_ABSENSI_' + (S.month || 'export') + '_backend.xlsx', chosen.length);
            setTimeout(hideProgressUI, 1500);
            return;
          } catch (err) {
            updateProgressUI(0, 'Export gagal: ' + (err.message || err) + '. Periksa Final lalu coba lagi.');
            EVKIN.toast('Export gagal. File yang belum lolos validasi tidak diunduh.', 'error');
            return;
          }
        }

        if (EVKIN.exportBackend && EVKIN.exportBackend.calculationAuthority === 'PYTHON_CANONICAL') {
          throw new Error('Export canonical memerlukan backend; fallback browser/IndexedDB dinonaktifkan.');
        }

        updateProgressUI(10, 'Membaca data dari browser untuk fallback export...');
        mod.downloadSelected(chosen, false);
        EVKIN.auditMod.logDataExport('EVKIN_ABSENSI_' + (S.month || 'export') + '_browser.xlsx', chosen.length);
        } finally { button.disabled = false; }
      })();
    };
  };
})();

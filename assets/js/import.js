/* EVKIN ABSENSI v3.1 - import.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, M = EVKIN.master;
  const S = EVKIN.state;
  const mod = EVKIN.importMod = {};

  mod.LARGE_FILE_THRESHOLD = 500;
  mod.BATCH_SIZE = 500;

  const HEADER_MAP = [
    ['nip', ['nip', 'nomorpegawai', 'noindukpegawai']],
    ['nama', ['nama', 'namapegawai', 'namalengkap', 'nmpeg', 'name']],
    ['jabatan', ['jabatan', 'posisi', 'jabatanpegawai', 'nmjabatan']],
    ['tanggal', ['tanggal', 'tanggalmasuk', 'tglmasuk', 'tgl', 'tanggalkerja', 'tglfinger', 'date', 'tanggalabsen']],
    ['tanggalPulang', ['tanggalpulang', 'tglpulang', 'tanggalkeluar']],
    ['hari', ['hari', 'day']],
    ['jamMasuk', ['jammasuk', 'masuk', 'checkin', 'jamin', 'scanmasuk', 'waktumasuk', 'jamdatang', 'absenpagi', 'in']],
    ['jamPulang', ['jampulang', 'pulang', 'checkout', 'jamout', 'scanpulang', 'waktupulang', 'jamkeluar', 'absensore', 'out']],
    ['zona', ['zona', 'zonawaktu', 'timezone', 'tz']],
    ['provinsi', ['provinsi', 'prov', 'province']],
    ['kabupaten', ['kabupaten', 'kabkota', 'kota', 'kab', 'kabupatenkota']],
    ['jenis', ['jenis', 'jenisabsensi', 'keterangan', 'ket', 'statusabsen', 'jenisabsen', 'jnshadir', 'tipe']]
  ];

  // Koordinat tidak digunakan dalam perhitungan workflow absensi.
  // Abaikan saat menyimpan raw_import agar payload dan database lebih ringan.
  const IGNORED_IMPORT_HEADERS = new Set([
    'latitude',
    'longitude',
    'latitudesore',
    'longitudesore',
    'latitudepulang',
    'longitudepulang'
  ]);

  function norm(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

  mod.detectColumns = function detectColumns(headers) {
    const map = {};
    headers.forEach((h, i) => {
      const nh = norm(h);
      for (const [field, aliases] of HEADER_MAP) {
        if (map[field] !== undefined) continue;
        if (aliases.includes(nh)) { map[field] = i; return; }
      }
    });
    headers.forEach((h, i) => {
      const nh = norm(h);
      for (const [field, aliases] of HEADER_MAP) {
        if (map[field] !== undefined) continue;
        if (aliases.some(a => nh.includes(a))) map[field] = i;
      }
    });
    return map;
  };

  function validateColumnMap(colMap, headers) {
    const missing = ['nip', 'nama'].filter(f => colMap[f] === undefined);
    if (colMap.tanggal === undefined && colMap.tanggalPulang === undefined) missing.push('tanggal');
    if (colMap.jamMasuk === undefined && colMap.jamPulang === undefined) missing.push('jamMasuk/jamPulang');
    if (missing.length) {
      throw new Error('Kolom wajib tidak ditemukan: ' + missing.join(', ') +
        '. Kolom terbaca: ' + headers.join(', '));
    }
  }

  function hasMeaningfulValue(value) {
    return value !== null && value !== undefined && String(value).trim() !== '';
  }

  mod.processSingleRow = function processSingleRow(r, headers, colMap, options) {
    options = options || {};
    const get = (f) => (colMap[f] !== undefined ? r[colMap[f]] : '');
    const rawObj = {};
    headers.forEach((h, i) => {
      const key = h || ('Kolom' + (i + 1));
      const cellValue = r[i];
      if (!IGNORED_IMPORT_HEADERS.has(norm(key))) {
        if (["jammasuk", "jamdatang", "absenpagi", "in"].includes(norm(key))) rawObj[key] = U.normalizeExcelTime(cellValue);
        else if (["jampulang", "checkout", "jamout", "scanpulang", "waktupulang", "jamkeluar", "absensore", "out"].includes(norm(key))) rawObj[key] = U.normalizeExcelTime(cellValue);
        else rawObj[key] = cellValue;
      }
    });

    const nip = String(get('nip') || '').trim();
    const nama = String(get('nama') || '').trim();
    const jabatan = String(get('jabatan') || '').trim();
    const dObj = U.parseDate(get('tanggal')) || U.parseDate(get('tanggalPulang'));
    const dPulang = colMap.tanggalPulang !== undefined ? U.parseDate(get('tanggalPulang')) : null;
    const masukValue = U.normalizeExcelTime(get('jamMasuk'));
    const pulangValue = U.normalizeExcelTime(get('jamPulang'));
    const masukMin = U.parseTime(masukValue);
    const pulangMin = U.parseTime(pulangValue);
    const provinsi = String(get('provinsi') || '').trim();
    const kabupaten = String(get('kabupaten') || '').trim();
    const zona = M.detectZone(provinsi, get('zona'));
    const jenis = String(get('jenis') || '').trim();

    const issues = [];
    if (!nip) issues.push('NIP kosong');
    if (!dObj) issues.push('Tanggal tidak valid');
    if (hasMeaningfulValue(get('jamMasuk')) && masukMin === null) issues.push('Jam masuk tidak valid');
    if (hasMeaningfulValue(get('jamPulang')) && pulangMin === null) issues.push('Jam pulang tidak valid');
    if (!options.partial) {
      if (masukMin === null) issues.push('Jam masuk kosong');
      if (pulangMin === null) issues.push('Jam pulang kosong');
    }

    let crossDay = false;
    if (dPulang && dObj && dPulang.key > dObj.key) crossDay = true;
    if (!crossDay && masukMin !== null && pulangMin !== null && pulangMin < masukMin) crossDay = true;

    if (!dObj || !nip) {
      return { rawObj, dataObj: null, invalid: true };
    }

    const off = M.zoneOffsetMin[zona] || 0;
    const offAdd = M.zoneOffsetMinAdd[zona] || 0;
    const wrap = (m) => m === null ? null : (((m + off) % 1440) + 1440) % 1440;
    const wrapAdd = (m) => m === null ? null : (((m + offAdd) % 1440) + 1440) % 1440;

    return {
      rawObj,
      invalid: false,
      dataObj: {
        _id: U.uid(),
        nip, nama, jabatan,
        tanggal: dObj,
        tanggalPulang: dPulang ? dPulang.str : '',
        hari: String(get('hari') || '').trim() || U.dayName(dObj),
        masukRaw: masukValue,
        pulangRaw: pulangValue,
        masukMin, pulangMin,
        masukNormMin: wrap(masukMin), pulangNormMin: wrap(pulangMin),
        masukNormMinAdd: wrapAdd(masukMin), pulangNormMinAdd: wrapAdd(pulangMin),
        provinsi, kabupaten, zona, jenis,
        crossDay,
        issues,
        dupFlag: false,
        deleted: false
      }
    };
  };

  function isSplitAttendanceFormat(colMap, headers) {
      return colMap.tanggal !== undefined &&
        headers.some(h => ['absenpagi', 'absensore'].includes(norm(h)));
    }

  function isWaliShiftRecord(record) {
      const jenis = norm(record.jenis);
      const jabatan = norm(record.jabatan);
        return jenis.includes('waliasuh') || jenis.includes('waliasrama') || jabatan.includes('waliasuh') ||
          jabatan.includes('waliasrama');
    }

    function eventMinutes(event) {
        return Math.floor(Date.UTC(event.date.y, event.date.m - 1, event.date.d) / 60000) + event.time;
      }

  function refreshRecordIssues(record) {
      record.issues = record.issues.filter(x => x === 'NIP kosong' || x === 'Tanggal tidak valid');
      if (record.masukMin === null) record.issues.push('Jam masuk kosong');
      if (record.pulangMin === null) record.issues.push('Jam pulang kosong');
      record.crossDay = Boolean(
        record.tanggalPulang &&
        record.tanggalPulang !== record.tanggal.str
      ) || (record.masukMin !== null && record.pulangMin !== null && record.pulangMin < record.masukMin);
    }

  function mergeStandardSplitRecords(records) {
      const groups = {};
      const standalone = [];
      records.forEach(record => {
        if (!record.tanggal) {
          standalone.push(record);
          return;
        }
        const key = record.nip + '|' + record.tanggal.key;
        const target = groups[key] || (groups[key] = record);
        if (target !== record) {
          if (target.masukMin === null && record.masukMin !== null) target.masukMin = record.masukMin;
          if (target.pulangMin === null && record.pulangMin !== null) target.pulangMin = record.pulangMin;
          target.rawObj = Object.assign({}, target.rawObj, record.rawObj);
          refreshRecordIssues(target);
        }
      });
      const merged = Object.keys(groups).map(key => groups[key]).concat(standalone);
      merged.forEach(refreshRecordIssues);
      return merged;
    }

  function mergeWaliShiftRecords(records) {
      const byNip = {};
      const standard = [];
      records.forEach(record => {
        if (!isWaliShiftRecord(record)) {
          standard.push(record);
          return;
        }
        if (!record.tanggal) return;
        const list = byNip[record.nip] || (byNip[record.nip] = []);
        if (record.masukMin !== null) list.push({ date: record.tanggal, time: record.masukMin, record });
        if (record.pulangMin !== null) list.push({ date: record.tanggal, time: record.pulangMin, record });
      });

      const merged = [];
      Object.keys(byNip).forEach(nip => {
        const events = byNip[nip].sort((a, b) => {
          const dateCmp = a.date.key.localeCompare(b.date.key);
          return dateCmp || a.time - b.time;
        });
        for (let i = 0; i < events.length;) {
          const masuk = events[i];
          const candidate = events[i + 1];
          // A shift may cross midnight, but an event beyond 18 hours is
          // treated as a new shift rather than a late clock-out.
          const pulang = candidate && eventMinutes(candidate) - eventMinutes(masuk) <= 18 * 60
            ? candidate : null;
          const result = Object.assign({}, masuk.record);
          result.masukMin = masuk.time;
          result.pulangMin = pulang ? pulang.time : null;
          result.tanggal = masuk.date;
          result.tanggalPulang = pulang && pulang.date.key !== masuk.date.key ? pulang.date.str : '';
          result.shiftFlag = pulang
            ? (pulang.date.key !== masuk.date.key ? 'SHIFT LINTAS HARI' : 'SHIFT NORMAL')
            : 'ABSEN MASUK TANPA PULANG';
          if (pulang && pulang.date.key !== masuk.date.key) result.crossDay = true;
          refreshRecordIssues(result);
          merged.push(result);
          i += pulang ? 2 : 1;
        }
      });
      return mergeStandardSplitRecords(standard).concat(merged).sort((a, b) => {
        const ak = a.tanggal && a.tanggal.key ? a.tanggal.key : '';
        const bk = b.tanggal && b.tanggal.key ? b.tanggal.key : '';
        return ak.localeCompare(bk) || String(a.nip).localeCompare(String(b.nip));
      });
    }

  function processRows(rows, headers, colMap) {
      const raw = [];
      const data = [];
      rows.forEach(r => {
          const result = mod.processSingleRow(r, headers, colMap);
        raw.push(result.rawObj);
        if (result.dataObj) data.push(result.dataObj);
      });
      return { raw, data };
  }

  function finalizeRecords(raw, data, totalRaw) {
    const grp = {};
    data.forEach(d => { const k = d.nip + '|' + d.tanggal.key; (grp[k] = grp[k] || []).push(d); });
    let dupGroups = 0;
    Object.values(grp).forEach(g => { if (g.length > 1) { dupGroups++; g.forEach(d => d.dupFlag = true); } });

    const nipSet = new Set(data.map(d => d.nip));
    const dateKeys = data.map(d => d.tanggal.key).sort();
    const valid = data.filter(d => d.issues.length === 0).length;
    const invalidRows = data.filter(d => d.issues.length > 0).length;
    const skipped = totalRaw - data.length;

    return {
      totalRaw,
      totalRecord: data.length,
      rejectedRows: skipped,
      totalSDM: nipSet.size,
      rentang: dateKeys.length ? (dateKeys[0] + ' s/d ' + dateKeys[dateKeys.length - 1]) : '-',
      valid,
      bermasalah: skipped + invalidRows,
      dupGroups,
      crossDay: data.filter(d => d.crossDay).length,
      months: Array.from(new Set(data.filter(d => d.tanggal && d.tanggal.monthKey).map(d => d.tanggal.monthKey))).sort()
    };
  }

  mod.applyImportResult = async function (headers, raw, data, stats, importMode) {
    if (mod.saving) throw new Error('Penyimpanan impor masih berjalan.');
    mod.saving = true;
    try {
      const workflowId = `import-${Date.now().toString(36)}-${U.uid()}`;
      S.workflowMeta = {...(S.workflowMeta || {}), workflow_id:workflowId,
        periode:stats.months[0] || null, status:'IMPORTING'};
      S.rawHeaders = headers; S.raw = raw; S.data = data;
      S.workflowLoadError = '';
      S.importStats = {...stats, workflow_id:workflowId}; S.importMode = importMode || 'standard';
      if (!stats.months.includes(S.month)) S.month = stats.months[0] || '';
      EVKIN.invalidateFrom('import', {save:false});
      if (typeof EVKIN.syncImportedWorkflow === 'function') await EVKIN.syncImportedWorkflow();
      else await EVKIN.syncBackendWorkflow();
      S.workflowMeta.status = 'ACTIVE';
      EVKIN.auditMod.logDataImport(data.length, S.month);
      return stats;
    } catch (error) {
      if (S.workflowMeta) S.workflowMeta.status = 'ERROR';
      const message = error && error.message ? error.message :
        (error && typeof error === 'object' ? JSON.stringify(error) : String(error || 'Kesalahan import tidak diketahui.'));
      S.workflowLoadError = 'Impor belum tersimpan lengkap. Impor ulang file sumber: ' + message;
      throw error;
    } finally { mod.saving = false; }
  };

  mod.restoreRecords = function (raw) {
    if (!raw.length) return [];
    if (raw.every(row => row.tanggal && typeof row.tanggal === 'object' && row.tanggal.monthKey && 'masukMin' in row)) return raw;
    const headers = Object.keys(raw[0]);
    const colMap = mod.detectColumns(headers);
    validateColumnMap(colMap, headers);
    const parsed = processRows(raw.map(row => headers.map(key => row[key])), headers, colMap);
    const records = isSplitAttendanceFormat(colMap, headers) ? mergeWaliShiftRecords(parsed.data) : parsed.data;
    finalizeRecords(raw, records, raw.length);
    return records;
  };

  mod.process = function (aoa) {
    if (!aoa || aoa.length < 2) throw new Error('File kosong atau tidak memiliki data.');
    const headers = aoa[0].map(x => String(x).trim());
    const colMap = mod.detectColumns(headers);
    validateColumnMap(colMap, headers);

    const rows = aoa.slice(1).filter(r => r.some(c => String(c).trim() !== ''));
    const processed = processRows(rows, headers, colMap);
    const raw = processed.raw;
    const data = processed.data;

    const stats = finalizeRecords(raw, data, rows.length);
    return mod.applyImportResult(headers, raw, data, stats);
  };

  mod.processAsync = function (aoa, onProgress) {
    if (!aoa || aoa.length < 2) return Promise.reject(new Error('File kosong atau tidak memiliki data.'));
    const headers = aoa[0].map(x => String(x).trim());
    const colMap = mod.detectColumns(headers);
    try {
      validateColumnMap(colMap, headers);
    } catch (err) {
      return Promise.reject(err);
    }

    const rows = aoa.slice(1).filter(r => r.some(c => String(c).trim() !== ''));
    const raw = [];
    const data = [];
    const batchSize = mod.BATCH_SIZE;
    let index = 0;

    return new Promise((resolve, reject) => {
      function runBatch() {
        try {
          const end = Math.min(index + batchSize, rows.length);
          for (; index < end; index++) {
              const result = mod.processSingleRow(rows[index], headers, colMap);
            raw.push(result.rawObj);
            if (result.dataObj) data.push(result.dataObj);
          }

          const progress = rows.length ? Math.round(index / rows.length * 100) : 100;
          if (onProgress) {
            onProgress({
              progress,
              processedCount: index,
              totalRecords: rows.length,
              currentBatch: Math.ceil(index / batchSize),
              totalBatches: Math.ceil(rows.length / batchSize)
            });
          }

          if (index < rows.length) {
            setTimeout(runBatch, 0);
            return;
          }

          const stats = finalizeRecords(raw, data, rows.length);
          resolve(mod.applyImportResult(headers, raw, data, stats));
        } catch (err) {
          reject(err);
        }
      }
      runBatch();
    });
  };

  mod.handleFile = function (file, cb) {
    const reader = new FileReader();
    reader.onload = function (e) {
      // Yield to browser UI thread to allow progress bar / spinner to paint
      setTimeout(function () {
        try {
          const data = new Uint8Array(e.target.result);
          const wb = XLSX.read(data, {
            type: 'array',
            raw: true,
            cellDates: false,
            cellFormula: false,
            cellHTML: false,
            cellStyles: false,
            dense: true,
            sheets: [0]
          });
          const ws = wb.Sheets[wb.SheetNames[0]];
          const aoa = XLSX.utils.sheet_to_json(ws, { header: 1, raw: true, defval: '' });
          cb(null, aoa);
        } catch (err) { cb(err); }
      }, 20);
    };
    reader.onerror = () => cb(new Error('Gagal membaca file'));
    reader.readAsArrayBuffer(file);
  };

  mod.processFile = function (file, onProgress) {
    return new Promise((resolve, reject) => {
      mod.handleFile(file, (err, aoa) => {
        if (err) return reject(err);
        const rowCount = Math.max(0, (aoa ? aoa.length : 0) - 1);
        if (rowCount >= mod.LARGE_FILE_THRESHOLD) {
          mod.processAsync(aoa, onProgress).then(resolve).catch(reject);
        } else {
          try {
            resolve(mod.process(aoa));
          } catch (ex) {
            reject(ex);
          }
        }
      });
    });
  };

  mod.render = function (el) {
    const st = S.importStats;
    el.innerHTML = `
      <div class="module-head">
        <div><h2>① Import Data</h2><p class="muted">Import absensi dari Excel (.xlsx/.xls) atau CSV. Data sumber tidak diubah.</p></div>
      </div>
      <div class="card">
        <h3>Unggah File Absensi</h3>
        <div class="dropzone" id="dz">
          <i data-lucide="upload-cloud"></i>
          <p><strong>Klik atau tarik file ke sini</strong></p>
          <p class="muted">Format: .xlsx, .xls, .csv &nbsp;|&nbsp; Kolom minimal: NIP, Nama, Tanggal, Jam Masuk, Jam Pulang</p>
          <p class="muted">File &gt; ${mod.LARGE_FILE_THRESHOLD.toLocaleString('id-ID')} baris diproses batch (UI tetap responsif) dan disimpan ke ${EVKIN.exportBackend && EVKIN.exportBackend.enabled ? 'database backend' : 'IndexedDB'}.</p>
          <input type="file" id="fileInput" accept=".xlsx,.xls,.csv" data-testid="import-file-input" hidden />
          <button type="button" class="btn btn-primary" id="btnPick" data-testid="import-pick-button"><i data-lucide="folder-open"></i> Import Data</button>
          <button type="button" class="btn btn-secondary" id="btnFingerprintPick" data-testid="fingerprint-import-pick-button"><i data-lucide="file-up"></i> Import CSV Fingerprint</button>
          <button type="button" class="btn btn-secondary" id="btnWaliPick" data-testid="wali-import-pick-button"><i data-lucide="moon"></i> Import Absensi Wali</button>
          <input type="file" id="fingerprintFileInput" accept=".csv,text/csv" data-testid="fingerprint-import-file-input" hidden />
          <input type="file" id="waliFileInput" accept=".xlsx,.xls,.csv" data-testid="wali-import-file-input" hidden />
        </div>
        <div id="importProgress" class="import-progress" hidden>
          <div class="import-progress-head">
            <span id="importProgressLabel">Memproses...</span>
            <span id="importProgressPercent">0%</span>
          </div>
          <div class="import-progress-track"><div id="importProgressBar" class="import-progress-bar"></div></div>
          <p class="muted import-progress-meta" id="importProgressMeta">0 / 0 baris</p>
        </div>
        <div id="importMsg"></div>
      </div>
      ${st ? renderStats(st) : ''}
      ${st ? renderPreview() : ''}
    `;

    const fi = el.querySelector('#fileInput');
    const dz = el.querySelector('#dz');
    el.querySelector('#btnPick').onclick = (event) => { event.stopPropagation(); fi.click(); };
    dz.onclick = (e) => { if (e.target.closest('button')) return; fi.click(); };
    dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('drag'); };
    dz.ondragleave = () => dz.classList.remove('drag');
    dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove('drag'); if (e.dataTransfer.files[0]) doFile(e.dataTransfer.files[0]); };
    fi.onchange = () => { if (fi.files[0]) doFile(fi.files[0]); };
    const fingerprintInput = el.querySelector('#fingerprintFileInput');
    const fingerprintButton = el.querySelector('#btnFingerprintPick');
    if (fingerprintButton && fingerprintInput) {
      fingerprintButton.onclick = (event) => { event.stopPropagation(); fingerprintInput.click(); };
      fingerprintInput.onchange = () => {
        if (fingerprintInput.files[0]) doSpecialFile(fingerprintInput.files[0], 'Import CSV Fingerprint');
      };
    }
    const waliInput = el.querySelector('#waliFileInput');
    const waliButton = el.querySelector('#btnWaliPick');
    if (waliButton && waliInput) {
      waliButton.onclick = (event) => { event.stopPropagation(); waliInput.click(); };
      waliInput.onchange = () => {
        if (!waliInput.files[0] || !EVKIN.waliImportMod) return;
        doSpecialFile(waliInput.files[0], 'Import Absensi Wali');
      };
    }

    function setImportProgress(visible, info) {
      const box = el.querySelector('#importProgress');
      if (!box) return;
      box.hidden = !visible;
      if (!info) return;
      if (EVKIN.progress) EVKIN.progress({
        title: 'Import Data',
        percent: info.progress,
        message: `${info.processedCount.toLocaleString('id-ID')} dari ${info.totalRecords.toLocaleString('id-ID')} baris, batch ${info.currentBatch}/${info.totalBatches}...`
      });
      const bar = el.querySelector('#importProgressBar');
      const label = el.querySelector('#importProgressLabel');
      const pct = el.querySelector('#importProgressPercent');
      const meta = el.querySelector('#importProgressMeta');
      if (bar) bar.style.width = info.progress + '%';
      if (pct) pct.textContent = info.progress + '%';
      if (label) label.textContent = 'Memproses batch ' + info.currentBatch + '/' + info.totalBatches + '...';
      if (meta) meta.textContent = info.processedCount.toLocaleString('id-ID') + ' / ' + info.totalRecords.toLocaleString('id-ID') + ' baris';
    }

    function doFile(file) {
      const msg = el.querySelector('#importMsg');
      const pickBtn = el.querySelector('#btnPick');
      const sizeMb = file.size ? ' (' + (file.size / (1024 * 1024)).toFixed(1) + ' MB)' : '';
      msg.innerHTML = '<div class="alert info"><i data-lucide="loader-2" class="spin"></i> Membaca ' + U.esc(file.name) + sizeMb + ' ...</div>';
      if (window.lucide) lucide.createIcons();
      if (pickBtn) pickBtn.disabled = true;
      setImportProgress(true, { progress: 0, processedCount: 0, totalRecords: 0, currentBatch: 0, totalBatches: 1 });
      const label = el.querySelector('#importProgressLabel');
      if (label) label.textContent = 'Membaca struktur file Excel' + sizeMb + '...';

      mod.processFile(file, (progress) => setImportProgress(true, progress))
        .then((stats) => {
          setImportProgress(false);
          if (EVKIN.progress) EVKIN.progress({ title: 'Import Data', percent: 100, message: `${stats.totalRecord.toLocaleString('id-ID')} record berhasil diimpor.`, state: 'done' });
          if (pickBtn) pickBtn.disabled = false;
          const mode = stats.totalRaw >= mod.LARGE_FILE_THRESHOLD ? ' (mode batch)' : '';
          EVKIN.toast('Import berhasil: ' + stats.totalRecord.toLocaleString('id-ID') + ' record' + mode, 'success');
          EVKIN.go('import');
        })
        .catch((ex) => {
          const message = ex && ex.message ? ex.message :
            (ex && typeof ex === 'object' ? JSON.stringify(ex) : String(ex || 'Kesalahan import tidak diketahui.'));
          setImportProgress(false);
          if (EVKIN.progress) EVKIN.progress({ title: 'Import Data gagal', message, state: 'error' });
          if (pickBtn) pickBtn.disabled = false;
          msg.innerHTML = '<div class="alert error">' + U.esc(message) + '</div>';
        });
    }

    function doSpecialFile(file, label) {
      const msg = el.querySelector('#importMsg');
      if (label === 'Import CSV Fingerprint' && !/\.csv$/i.test(file.name)) {
        msg.innerHTML = '<div class="alert error">Import CSV Fingerprint hanya menerima file .csv.</div>';
        return;
      }
      if (!EVKIN.waliImportMod || typeof EVKIN.waliImportMod.processFile !== 'function') {
        msg.innerHTML = '<div class="alert error">Mesin import absensi wali belum tersedia.</div>';
        return;
      }
      msg.innerHTML = '<div class="alert info">' + label + ': membaca ' + U.esc(file.name) + ' ...</div>';
      EVKIN.waliImportMod.processFile(file, (progress) => setImportProgress(true, progress))
        .then((stats) => {
          setImportProgress(false);
          if (EVKIN.progress) EVKIN.progress({ title: label, percent: 100, message: `${stats.totalRecord.toLocaleString('id-ID')} record berhasil diimpor.`, state: 'done' });
          EVKIN.toast(label + ' berhasil: ' + stats.totalRecord.toLocaleString('id-ID') + ' record', 'success');
          EVKIN.go('import');
        })
        .catch((error) => {
          const message = error && error.message ? error.message :
            (error && typeof error === 'object' ? JSON.stringify(error) : String(error || 'Kesalahan import tidak diketahui.'));
          setImportProgress(false);
          if (EVKIN.progress) EVKIN.progress({ title: label + ' gagal', message, state: 'error' });
          msg.innerHTML = '<div class="alert error">' + U.esc(message) + '</div>';
        });
    }
  };

  function renderStats(st) {
    return `
      <div class="stat-grid" data-testid="import-stats">
        ${statCard('Jumlah Record', st.totalRecord, 'list', 'blue')}
        ${statCard('Jumlah SDM', st.totalSDM, 'users', 'blue')}
        ${statCard('Rentang Tanggal', st.rentang, 'calendar-range', 'gray', true)}
        ${statCard('Data Valid', st.valid, 'check-circle', 'green')}
        ${statCard('Data Bermasalah', st.bermasalah, 'alert-triangle', st.bermasalah ? 'yellow' : 'green')}
        ${statCard('Duplikasi (NIP+Tgl)', st.dupGroups, 'copy', st.dupGroups ? 'red' : 'green')}
        ${S.importMode === 'wali' ? statCard('Baris Excel Identik', st.duplicateSourceRows || 0, 'copy', st.duplicateSourceRows ? 'yellow' : 'green') : ''}
        ${statCard('Record Lintas Hari', st.crossDay, 'moon', 'gray')}
        ${S.importMode === 'wali' ? statCard('Mode Import', 'Absensi Wali', 'shield-check', 'blue', true) : ''}
      </div>
      <div class="alert ${st.bermasalah ? 'warn' : 'success'}">
        ${st.bermasalah ? '<i data-lucide="alert-triangle"></i> Terdapat data bermasalah. Data sumber tetap disimpan — perbaiki pada tahap Semi Final bila perlu.' : '<i data-lucide="check-circle"></i> Data siap diproses. Lanjutkan ke Konfigurasi.'}
      </div>
      <div class="actions-row">
        <button class="btn ${EVKIN.stepUnlocked('config') ? 'btn-primary' : 'btn-disabled'}" data-testid="import-next-button" ${EVKIN.stepUnlocked('config') ? '' : 'disabled'} onclick="EVKIN.go('config')">Lanjut: Konfigurasi <i data-lucide="arrow-right"></i></button>
      </div>`;
  }

  function statCard(label, val, icon, color, small) {
    return `<div class="stat-card ${color}"><div class="stat-icon"><i data-lucide="${icon}"></i></div>
      <div><div class="stat-val ${small ? 'small' : ''}">${U.esc(val)}</div><div class="stat-label">${label}</div></div></div>`;
  }

  function renderPreview() {
    const rows = S.data.filter(d => !d.deleted).slice(0, 100);
    const head = ['NIP', 'Nama', 'Tanggal', 'Hari', 'Jam Masuk', 'Jam Pulang', 'Zona', 'Provinsi', 'Jenis', 'Lintas Hari', 'Masalah'];
    const body = rows.map(d => `<tr class="${d.dupFlag ? 'row-dup' : ''} ${d.issues.length ? 'row-issue' : ''}">
      <td>${U.esc(d.nip)}</td><td>${U.esc(d.nama)}</td><td>${U.esc(d.tanggal.str)}</td><td>${U.esc(d.hari)}</td>
      <td>${U.esc(d.masukRaw || U.fmtTime(d.masukMin))}</td><td>${U.esc(d.pulangRaw || U.fmtTime(d.pulangMin))}</td><td>${U.esc(d.zona)}</td>
      <td>${U.esc(d.provinsi)}</td><td>${U.esc(d.jenis)}</td><td>${d.crossDay ? '✔' : ''}</td>
     <td class="err">${U.esc((d.issues || []).join('; '))}</td></tr>`).join('');
    const exampleRows = [
      ['0.288263888888889', '06:55:06'],
      ['0.827013888888889', '19:50:54'],
      ['0.5', '12:00:00']
    ].map(([raw, canonical]) => `<tr><td>${U.esc(raw)}</td><td>${U.esc(canonical)}</td></tr>`).join('');
    return `<div class="card"><h3>Preview Data <span class="muted">(maks 100 baris dari ${S.data.length})</span></h3>
      <div class="table-wrap"><table class="data-table" data-testid="import-preview-table"><thead><tr>${head.map(h => '<th>' + h + '</th>').join('')}</tr></thead><tbody>${body}</tbody></table></div></div>
      <div class="card"><h3>Preview Normalisasi Excel Time</h3>
        <div class="table-wrap"><table class="data-table" data-testid="excel-time-preview-table"><thead><tr><th>Input Excel</th><th>Canonical</th></tr></thead><tbody>${exampleRows}</tbody></table></div></div>`;
  }
})();

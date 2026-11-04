/* EVKIN Backend Import V1 controller.
   Loaded beside (not instead of) the frozen legacy import engine. */
(function () {
  const EVKIN = window.EVKIN;
  if (!EVKIN || !EVKIN.importMod) return;
  const backend = EVKIN.exportBackend || {};
  const mod = EVKIN.importBackend = EVKIN.importBackend || {};
  const legacyRender = EVKIN.importMod.render;
  const TERMINAL = new Set(['READY', 'COMMITTED', 'ALREADY_COMMITTED', 'RESET', 'FAILED', 'STALE_GENERATION', 'STALE_SOURCE', 'CANCELLED']);
  const IMPORT_MODES = Object.freeze({
    ROUTER:'ROUTER', LEGACY_EXCEL:'LEGACY_EXCEL', STANDARD_BACKEND:'STANDARD_BACKEND',
    CSV_EABSENSI_SPECIAL:'CSV_EABSENSI_SPECIAL', WALI_LEGACY:'WALI_LEGACY'
  });

  mod.authority = localStorage.getItem('evkin.importAuthority') || 'IMPORT_JS_LEGACY';
  mod.routes = {standard:mod.authority,wali:'IMPORT_JS_LEGACY',csv_eabsensi:'DISABLED'};
  mod.csvEabsensiEnabled = false;
  mod.currentBatch = null;
  mod.csvProcessing = false;
  mod.mode = IMPORT_MODES.ROUTER;
  mod.modes = IMPORT_MODES;

  mod.setMode = function (mode) {
    if (!Object.values(IMPORT_MODES).includes(mode)) throw new Error('Mode import tidak dikenal.');
    mod.mode = mode;
    return mode;
  };

  function apiUrl(path) {
    return new URL(path, backend.apiBase || window.location.origin).toString();
  }

  function csvPhase(status) {
    return ({
      UPLOADING:'Upload file', STAGED:'Membaca CSV', VALIDATING:'Validasi struktur/data',
      NORMALIZING:'Normalisasi event & pairing absensi', READY:'Menyiapkan canonical records',
      COMMITTING:'Menyimpan ke DuckDB', HYDRATING:'Memuat ulang workflow',
      COMMITTED:'Selesai', ALREADY_COMMITTED:'Hasil sebelumnya tersedia',
      FAILED:'Gagal', STALE_GENERATION:'Dibatalkan setelah reset', RESET:'Direset'
    })[status] || status || 'Menyiapkan proses';
  }

  function renderCsvProgress(el, detail) {
    const box = el && el.querySelector ? el.querySelector('#eabsensiCsvProgress') : null;
    if (!box) return;
    const status = detail.status || 'UPLOADING';
    const elapsed = Math.max(0, Math.floor((Date.now() - detail.startedAt) / 1000));
    const counters = Number(detail.staged_rows || 0) > 0
      ? `<p class="muted">${Number(detail.staged_rows).toLocaleString('id-ID')} record canonical disiapkan.</p>` : '';
    box.innerHTML = `<div class="card" data-testid="eabsensi-progress-panel">
      <div class="alert info"><strong>Sedang memproses CSV eAbsensi.</strong><br>Data diproses di backend. Jangan menutup atau memuat ulang halaman sampai proses selesai; progress batch belum dipulihkan otomatis setelah refresh.</div>
      <p><strong>File:</strong> ${EVKIN.util.esc(detail.filename || '-')}</p>
      <p><strong>Mode:</strong> ${detail.crossDay ? 'Lintas Hari' : 'Normal'} · <strong>Waktu:</strong> ${elapsed} detik</p>
      <p><strong>Tahap:</strong> ${EVKIN.util.esc(csvPhase(status))}</p>${counters}</div>`;
  }

  function showImportModal(options) {
    if (typeof document === 'undefined' || typeof document.createElement !== 'function' || !document.body) return null;
    let modal = document.getElementById('evkinImportModal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'evkinImportModal';
      modal.className = 'import-modal-overlay';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      modal.setAttribute('aria-labelledby', 'importModalTitle');
      document.body.appendChild(modal);
    }
    const filename = (options && options.filename) || 'File Import';
    const isEabsensi = options && options.isEabsensi;
    const title = isEabsensi ? 'Memproses CSV eAbsensi Backend' : 'Memproses Import Excel Backend';
    modal.innerHTML = `
      <div class="import-modal-dialog" data-testid="import-modal-dialog">
        <div class="import-modal-header">
          <div class="import-modal-badge-icon" id="importModalBadgeIcon">
            <div class="import-spinner"></div>
          </div>
          <div class="import-modal-title-group">
            <h3 id="importModalTitle">${EVKIN.util.esc(title)}</h3>
            <p class="import-modal-filename" title="${EVKIN.util.esc(filename)}">${EVKIN.util.esc(filename)}</p>
          </div>
        </div>
        <div class="import-modal-body">
          <div class="import-modal-phase-row">
            <span class="import-modal-phase-label" id="importModalPhase">Menyiapkan engine backend...</span>
            <span class="import-modal-percent" id="importModalPercent">0%</span>
          </div>
          <div class="import-modal-progress-track">
            <div class="import-modal-progress-bar" id="importModalBar" style="width: 0%"></div>
          </div>
          <div class="import-modal-stats-grid">
            <div class="import-stat-item">
              <span class="import-stat-lbl">Baris Diproses</span>
              <span class="import-stat-val" id="importModalRows">Memulai...</span>
            </div>
            <div class="import-stat-item">
              <span class="import-stat-lbl">Waktu Berjalan</span>
              <span class="import-stat-val" id="importModalElapsed">0s</span>
            </div>
            <div class="import-stat-item">
              <span class="import-stat-lbl">Engine</span>
              <span class="import-stat-val">Python + DuckDB</span>
            </div>
          </div>
          <div class="import-modal-stepper" id="importModalStepper">
            <div class="import-step active" id="importStepUpload">
              <span class="import-step-dot"></span>
              <span class="import-step-text">Upload File</span>
            </div>
            <div class="import-step" id="importStepNorm">
              <span class="import-step-dot"></span>
              <span class="import-step-text">Normalisasi Data</span>
            </div>
            <div class="import-step" id="importStepInteg">
              <span class="import-step-dot"></span>
              <span class="import-step-text">Validasi Integritas</span>
            </div>
          </div>
          <p class="import-modal-notice">
            ⚡ Data diproses streaming di backend tanpa membebani browser. Jangan menutup tab selama proses berlangsung.
          </p>
        </div>
        <div class="import-modal-footer" id="importModalFooter" style="display:none;">
          <button type="button" class="btn btn-secondary" id="importModalDismiss">Tutup</button>
        </div>
      </div>
    `;
    modal.classList.add('visible');
    const dismissBtn = modal.querySelector('#importModalDismiss');
    if (dismissBtn) {
      dismissBtn.onclick = () => closeImportModal();
    }
    return modal;
  }

  function updateImportModal(batch, startedAt) {
    if (typeof document === 'undefined' || typeof document.getElementById !== 'function') return;
    const modal = document.getElementById('evkinImportModal');
    if (!modal || !modal.classList.contains('visible')) return;
    const status = (batch && batch.status) || 'UPLOADING';
    const percentVal = Math.min(100, Math.max(0, Math.round((batch && batch.progress) || 0)));
    const elapsedSec = Math.max(0, Math.floor((Date.now() - (startedAt || Date.now())) / 1000));

    let phaseText = csvPhase(status);
    if (status === 'NORMALIZING') {
      phaseText = 'Normalisasi baris & pairing absensi...';
    } else if (status === 'VALIDATING') {
      phaseText = 'Validasi struktur file & skema kolom...';
    } else if (status === 'READY') {
      phaseText = 'Selesai! Menyiapkan pratinjau rekap...';
    } else if (status === 'FAILED') {
      phaseText = 'Import gagal: ' + ((batch && batch.error_message) || 'Terjadi kesalahan');
    }

    const phaseEl = modal.querySelector('#importModalPhase');
    if (phaseEl) phaseEl.textContent = phaseText;

    const percentEl = modal.querySelector('#importModalPercent');
    if (percentEl) percentEl.textContent = `${percentVal}%`;

    const barEl = modal.querySelector('#importModalBar');
    if (barEl) barEl.style.width = `${percentVal}%`;

    const rowsEl = modal.querySelector('#importModalRows');
    if (rowsEl) {
      const staged = Number((batch && batch.staged_rows) || 0);
      const total = Number((batch && batch.total_rows) || 0);
      if (total > 0 && staged > 0 && total > staged) {
        rowsEl.textContent = `${staged.toLocaleString('id-ID')} / ${total.toLocaleString('id-ID')}`;
      } else if (staged > 0) {
        rowsEl.textContent = `${staged.toLocaleString('id-ID')} baris`;
      } else {
        rowsEl.textContent = status === 'UPLOADING' ? 'Mengunggah...' : 'Memulai...';
      }
    }

    const elapsedEl = modal.querySelector('#importModalElapsed');
    if (elapsedEl) elapsedEl.textContent = `${elapsedSec}s`;

    const stepUpload = modal.querySelector('#importStepUpload');
    const stepNorm = modal.querySelector('#importStepNorm');
    const stepInteg = modal.querySelector('#importStepInteg');
    if (stepUpload && stepNorm && stepInteg) {
      if (status === 'UPLOADING') {
        stepUpload.className = 'import-step active';
        stepNorm.className = 'import-step';
        stepInteg.className = 'import-step';
      } else if (status === 'STAGED' || status === 'VALIDATING' || status === 'NORMALIZING') {
        stepUpload.className = 'import-step done';
        stepNorm.className = 'import-step active';
        stepInteg.className = 'import-step';
      } else if (status === 'READY' || status === 'COMMITTED' || status === 'ALREADY_COMMITTED') {
        stepUpload.className = 'import-step done';
        stepNorm.className = 'import-step done';
        stepInteg.className = 'import-step done';
      }
    }

    const badge = modal.querySelector('#importModalBadgeIcon');
    if (badge) {
      if (status === 'READY' || status === 'COMMITTED' || status === 'ALREADY_COMMITTED') {
        badge.innerHTML = '<span class="import-badge-success">✓</span>';
        if (barEl) barEl.classList.add('success');
      } else if (status === 'FAILED') {
        badge.innerHTML = '<span class="import-badge-error">✕</span>';
        if (barEl) barEl.classList.add('error');
        const footer = modal.querySelector('#importModalFooter');
        if (footer) footer.style.display = 'flex';
      }
    }
  }

  function closeImportModal(delayMs) {
    if (typeof document === 'undefined' || typeof document.getElementById !== 'function') return;
    const modal = document.getElementById('evkinImportModal');
    if (!modal) return;
    if (delayMs && delayMs > 0) {
      setTimeout(() => {
        if (modal) modal.classList.remove('visible');
      }, delayMs);
    } else {
      modal.classList.remove('visible');
    }
  }

  mod.showImportModal = showImportModal;
  mod.updateImportModal = updateImportModal;
  mod.closeImportModal = closeImportModal;

  async function request(path, options) {
    const response = await (backend.apiFetch || window.fetch)(apiUrl(path), options || {method:'GET'});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body && body.detail;
      const error = new Error((detail && detail.message) || detail || `HTTP ${response.status}`);
      error.code = detail && detail.code;
      throw error;
    }
    return body;
  }

  async function hydrateCommittedWorkflow(batch) {
    if (typeof EVKIN.load !== 'function' || !await EVKIN.load()) {
      throw new Error('Data sudah tersimpan, tetapi workflow canonical gagal dimuat.');
    }
    const state = EVKIN.state || {};
    const workflowId = 'backend-import-' + batch.import_id;
    if (!state.workflowMeta || state.workflowMeta.workflow_id !== workflowId) {
      throw new Error('Workflow aktif tidak cocok dengan import yang baru di-commit.');
    }
    if ((batch.source_format && state.importMode !== batch.source_format) || (batch.period && state.month !== batch.period)) {
      throw new Error('Mode atau periode workflow aktif tidak cocok dengan hasil commit.');
    }
    if (backend.calculationAuthority === 'PYTHON_CANONICAL') {
      const stored = Number(state.workflowMeta.row_counts?.raw_import || 0);
      if (!stored || (batch.valid_rows != null && stored !== Number(batch.valid_rows))) {
        throw new Error('Jumlah data tersimpan tidak cocok dengan hasil impor.');
      }
      return true;
    }
    if (typeof EVKIN.loadCalculationInput !== 'function') {
      throw new Error('Hydration input kalkulasi canonical tidak tersedia.');
    }
    const rows = await EVKIN.loadCalculationInput({silent:true});
    const expected = Number(state.workflowMeta.total_row_counts &&
      state.workflowMeta.total_row_counts.raw_import || 0);
    if (!expected || rows.length !== expected || state.raw.length !== expected || state.data.length !== expected) {
      throw new Error(`Hydration canonical tidak lengkap: ${state.data.length}/${expected} baris.`);
    }
    return true;
  }

  function hydrateCommittedEabsensiWorkflow(batch) {
    return hydrateCommittedWorkflow(batch);
  }

  mod.refreshAuthority = async function () {
    const info = await request('/api/absence-import/authority', {method:'GET', cache:'no-store'});
    if (!info || !['IMPORT_JS_LEGACY', 'IMPORT_BACKEND_V1'].includes(info.authority)) {
      throw new Error('Backend mengembalikan authority import yang tidak dikenali.');
    }
    mod.authority = info.authority;
    mod.routes = info.routes || {standard:mod.authority,wali:'IMPORT_JS_LEGACY',csv_eabsensi:'DISABLED'};
    mod.csvEabsensiEnabled = info.csv_eabsensi_enabled === true &&
      mod.routes.csv_eabsensi === 'CSV_EABSENSI_SPECIAL';
    localStorage.setItem('evkin.importAuthority', mod.authority);
    return info;
  };

  mod.upload = async function (file, options) {
    const sourceFormat = options.sourceFormat || 'standard';
    if (sourceFormat === 'wali') {
      const error = new Error('Absensi Wali menggunakan JavaScript Legacy; Backend Import V1 tidak dipanggil.');
      error.code = 'WALI_LEGACY_REQUIRED';
      throw error;
    }
    if (sourceFormat === 'csv_eabsensi') {
      if (!mod.csvEabsensiEnabled) {
        const error = new Error('CSV eAbsensi Special belum diaktifkan pada environment ini.');
        error.code = 'CSV_EABSENSI_SPECIAL_NOT_ENABLED';
        throw error;
      }
      if (!/\.csv$/i.test(file && file.name || '')) {
        const error = new Error('Mode CSV eAbsensi hanya menerima file .csv.');
        error.code = 'CSV_EABSENSI_REQUIRES_CSV';
        throw error;
      }
    } else if (sourceFormat !== 'standard') {
      const error = new Error('Mode import tidak dikenal.');
      error.code = 'INVALID_SOURCE_FORMAT';
      throw error;
    }
    const form = new FormData();
    form.append('file', file);
    form.append('operator', options.operator || 'operator');
    form.append('source_format', sourceFormat);
    if (sourceFormat === 'csv_eabsensi') {
      form.append('pair_cross_day', options.crossDay === true ? 'true' : 'false');
    }
    if (options.period) form.append('period', options.period);
    const config = {...(EVKIN.state.config || {})};
    delete config.masterPegawai;
    form.append('workflow_config_json', JSON.stringify(config));
    const batch = await request('/api/absence-import/upload', {method:'POST', body:form});
    mod.currentBatch = batch;
    return batch;
  };

  mod.status = async function (importId) {
    const batch = await request('/api/absence-import/' + encodeURIComponent(importId), {method:'GET', cache:'no-store'});
    mod.currentBatch = batch;
    return batch;
  };

  mod.wait = async function (importId, onProgress, timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 30 * 60 * 1000);
    while (Date.now() < deadline) {
      const batch = await mod.status(importId);
      if (onProgress) onProgress(batch);
      if (TERMINAL.has(batch.status)) return batch;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error('Import backend melewati batas waktu tunggu.');
  };

  mod.preview = function (importId, page, errorsOnly) {
    return request(`/api/absence-import/${encodeURIComponent(importId)}/${errorsOnly ? 'errors' : 'preview'}?page=${page || 1}&page_size=50`, {method:'GET', cache:'no-store'});
  };

  mod.commit = function (importId, period, acceptInvalid) {
    return request('/api/absence-import/' + encodeURIComponent(importId) + '/commit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({period:period || null, accept_invalid:!!acceptInvalid})
    });
  };

  mod.useCommitted = function (importId) {
    return request('/api/absence-import/' + encodeURIComponent(importId) + '/use-committed', {
      method:'POST'
    });
  };

  mod.previewResetPeriod = function (importId) {
    return request('/api/absence-import/' + encodeURIComponent(importId) + '/reset-period/preview', {
      method:'GET', cache:'no-store'
    });
  };

  mod.resetPeriod = function (importId) {
    return request('/api/absence-import/' + encodeURIComponent(importId) + '/reset-period', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirmation:'RESET'})
    });
  };

  mod.processCsvEabsensi = async function (file, options, onProgress) {
    options = options || {};
    const created = await mod.upload(file, {
      operator:options.operator || 'operator', sourceFormat:'csv_eabsensi',
      period:options.period || '', crossDay:options.crossDay === true
    });
    if (created.status === 'ALREADY_COMMITTED') return created;
    const ready = await mod.wait(created.import_id, onProgress);
    if (ready.status === 'ALREADY_COMMITTED') return ready;
    if (ready.status !== 'READY') {
      const error = new Error(ready.error_message || `Import CSV eAbsensi berhenti pada status ${ready.status}.`);
      error.code = ready.error_code || ready.status;
      throw error;
    }
    const periods = Array.isArray(ready.periods) ? ready.periods : [];
    const selectedPeriod = options.period || ready.period || (periods.length === 1 ? periods[0] : '');
    if (!selectedPeriod) {
      const error = new Error('CSV berisi lebih dari satu periode. Pilih periode secara eksplisit melalui mode Advanced.');
      error.code = 'MULTI_PERIOD_REQUIRES_SELECTION';
      error.batch = ready;
      throw error;
    }
    try {
      return await mod.commit(ready.import_id, selectedPeriod, false);
    } catch (error) {
      error.batch = ready;
      throw error;
    }
  };

  mod.importLegacyExcel = function (file, onProgress) {
    if (!/\.(xlsx|xlsm|xls)$/i.test(file && file.name || '')) {
      const error = new Error('Mode Excel Existing hanya menerima file .xlsx, .xlsm, atau .xls.');
      error.code = 'LEGACY_EXCEL_FILE_REQUIRED';
      return Promise.reject(error);
    }
    return EVKIN.importMod.processFile(file, onProgress);
  };

  function renderLegacyExcel(el) {
    mod.setMode(IMPORT_MODES.LEGACY_EXCEL);
    legacyRender(el);
    if (el.dataset) el.dataset.importMode = IMPORT_MODES.LEGACY_EXCEL;
    const heading = el.querySelector('.module-head');
    if (heading) heading.insertAdjacentHTML('beforebegin', `
      <div class="card" data-testid="legacy-excel-mode-panel">
        <div class="alert info">IMPORT ENGINE: JavaScript Legacy / Excel Existing.</div>
        <p class="muted">Workflow legacy lengkap aktif: preview, Import Absensi Wali/pairing lintas hari, hasil pairing, dan tombol lanjut.</p>
        <button type="button" class="btn btn-secondary" id="legacyExcelBack">Kembali ke Pilihan Import</button>
      </div>`);
    const input = el.querySelector('#fileInput');
    if (input) input.setAttribute('accept', '.xlsx,.xlsm,.xls');
    const pick = el.querySelector('#btnPick');
    if (pick) pick.innerHTML = '<i data-lucide="folder-open"></i> Import Excel Existing';
    const waliPick = el.querySelector('#btnWaliPick');
    const waliInput = el.querySelector('#waliFileInput');
    if (waliPick && waliInput) {
      // The legacy controls live inside one clickable dropzone. Capture the
      // dedicated Wali action so the parent never opens the ordinary Excel
      // input first; the unchanged legacy input.onchange still owns pairing.
      waliInput.addEventListener('click', event => event.stopPropagation(), true);
      waliPick.addEventListener('click', event => {
        event.preventDefault();
        event.stopImmediatePropagation();
        waliInput.click();
      }, true);
    }
    const back = el.querySelector('#legacyExcelBack');
    if (back) back.onclick = () => {
      mod.setMode(IMPORT_MODES.ROUTER);
      EVKIN.go('import');
    };
  }

  mod.returnToLegacyExcel = function () {
    mod.setMode(IMPORT_MODES.LEGACY_EXCEL);
    EVKIN.go('import');
  };

  function summaryCards(batch) {
    const cards = [
      ['TOTAL ROWS', batch.total_rows], ['VALID ROWS', batch.valid_rows], ['INVALID ROWS', batch.invalid_rows],
      ['DUPLICATE ROWS', batch.duplicate_rows], ['MISSING NIP', batch.missing_nip_rows],
      ['INVALID NIP', batch.invalid_nip_rows], ['INVALID DATE', batch.invalid_date_rows],
      ['INVALID TIME', batch.invalid_time_rows], ['CROSS-DAY', batch.cross_day_rows],
      ['WALI', batch.wali_rows], ['WIB', batch.wib_rows], ['WITA', batch.wita_rows], ['WIT', batch.wit_rows]
    ];
    return `<div class="stat-grid">${cards.map(([label,value]) => `<div class="stat-card blue"><div><div class="stat-val">${Number(value || 0).toLocaleString('id-ID')}</div><div class="stat-label">${label}</div></div></div>`).join('')}</div>`;
  }

  function eabsensiSummaryCards(batch) {
    const timing = batch.timings || {};
    const metrics = [
      ['Source Rows', timing.source_rows ?? batch.total_rows],
      ['Unique NIP', timing.unique_nip],
      ['Daily Output', batch.status === 'COMMITTED' ? batch.valid_rows : timing.canonical_rows],
      ['Complete Pair', timing.complete_pairs],
      ['IN Only', timing.check_in_only],
      ['OUT Only', timing.check_out_only],
      ['Duplicate', batch.duplicate_rows ?? (Number(timing.duplicate_source_rows || 0) + Number(timing.duplicate_event_rows || 0))],
      ['Invalid Timestamp', batch.invalid_time_rows],
      ['Cross-Day Pair', batch.cross_day_rows],
      ['Rejected', batch.invalid_rows]
    ];
    return `<div class="stat-grid" data-testid="eabsensi-summary">${metrics.map(([label,value]) => `
      <div class="stat-card blue"><div><div class="stat-val">${Number(value || 0).toLocaleString('id-ID')}</div>
      <div class="stat-label">${label}</div></div></div>`).join('')}</div>`;
  }

  function pairingStatus(row) {
    const errors = Array.isArray(row.error_codes) ? row.error_codes : [];
    if (errors.some(code => String(code).startsWith('INVALID_CHECK_'))) return 'Invalid timestamp';
    if (row.cross_day) return 'Cross-day pair';
    if (row.jam_masuk && row.jam_pulang) return 'Complete pair';
    if (row.jam_masuk) return 'IN only';
    if (row.jam_pulang) return 'OUT only';
    return 'No punch';
  }

  async function renderEabsensiResult(el, batch, options) {
    options = options || {};
    const alreadyCommitted = batch.status === 'ALREADY_COMMITTED';
    const authoritative = alreadyCommitted && batch.authoritative_import
      ? batch.authoritative_import : batch;
    let preview = {rows:[]};
    let previewError = '';
    try {
      preview = await mod.preview(authoritative.import_id, 1, false);
    } catch (error) {
      previewError = error.message;
    }
    const previewRows = (preview.rows || []).map(row => `<tr>
      <td>${EVKIN.util.esc(row.nip || '')}</td><td>${EVKIN.util.esc(row.nama || '')}</td>
      <td>${EVKIN.util.esc(row.tanggal || '')}</td><td>${EVKIN.util.esc(row.tanggal_pulang || '')}</td>
      <td>${EVKIN.util.esc(row.jam_masuk || '')}</td><td>${EVKIN.util.esc(row.jam_pulang || '')}</td>
      <td>${EVKIN.util.esc(row.jenis || '')}</td><td>${EVKIN.util.esc(pairingStatus(row))}</td></tr>`).join('');
    const canCommit = batch.status === 'READY';
    const periods = (batch.periods || []).map(value =>
      `<option value="${EVKIN.util.esc(value)}" ${value === batch.period ? 'selected' : ''}>${EVKIN.util.esc(value)}</option>`
    ).join('');
    const notice = options.notice ? `<div class="alert ${canCommit ? 'info' : 'error'}">${EVKIN.util.esc(options.notice)}</div>` : '';
    const workflowReady = batch.status === 'COMMITTED' && options.workflowLoaded;
    const invalidConfirmation = canCommit && Number(batch.invalid_rows || 0) > 0
      ? '<label><input type="checkbox" id="eabsensiAcceptInvalid"> Saya memahami row invalid tidak dipromosikan</label>' : '';
    const previousSummary = alreadyCommitted ? `<div class="alert info"><strong>File ini sudah pernah berhasil diimport.</strong><br>
      Periode: ${EVKIN.util.esc(authoritative.period || '-')} · Status: COMMITTED ·
      Pairing: ${authoritative.pair_cross_day ? 'Lintas Hari' : 'Normal'} · Import ID: ${EVKIN.util.esc(authoritative.import_id || '')}</div>` : '';
    el.querySelector('#backendImportResult').innerHTML = `
      <div class="card"><h3>Import CSV eAbsensi — Hasil</h3>
        ${previousSummary}
        ${notice}
        ${eabsensiSummaryCards(authoritative)}
        ${previewError ? `<div class="alert error">Preview gagal dimuat: ${EVKIN.util.esc(previewError)}</div>` : ''}
        <div class="card"><h4>Preview hasil (maks. 50 baris)</h4><div class="table-wrap"><table class="data-table">
          <thead><tr><th>NIP</th><th>Nama</th><th>Tanggal</th><th>Tanggal Pulang</th><th>Jam Masuk</th><th>Jam Pulang</th><th>Jenis</th><th>Status Pairing</th></tr></thead>
          <tbody>${previewRows}</tbody></table></div></div>
        ${canCommit ? `<div class="actions-row"><label>Periode <select id="eabsensiPeriod">${batch.period ? '' : '<option value="" selected disabled>Pilih periode...</option>'}${periods}</select></label>
          ${invalidConfirmation}<button class="btn btn-primary" id="eabsensiCommit">Proses CSV eAbsensi</button></div>` : ''}
        ${workflowReady ? '<div class="alert success"><strong>Import berhasil.</strong> Data tersimpan dan workflow canonical berhasil dimuat.</div><div class="actions-row"><button class="btn btn-primary" id="eabsensiContinue">Lanjut ke Konfigurasi</button></div>' : ''}
        ${batch.status === 'COMMITTED' && !workflowReady ? '<div class="alert error">Data sudah tersimpan, tetapi workflow belum berhasil dimuat. Muat ulang sebelum melanjutkan.</div>' : ''}
        ${alreadyCommitted ? '<div class="actions-row"><button class="btn btn-primary" id="eabsensiUsePrevious">Gunakan Hasil Import Sebelumnya</button><button class="btn btn-danger" id="eabsensiResetPeriod">Reset Import Periode</button></div>' : ''}
        ${workflowReady ? '<div class="actions-row"><button class="btn btn-danger" id="eabsensiResetPeriod">Reset Import Periode</button></div>' : ''}
      </div>`;
    const commit = el.querySelector('#eabsensiCommit');
    if (commit) commit.onclick = async () => {
      commit.disabled = true;
      try {
        const period = el.querySelector('#eabsensiPeriod').value;
        if (!period) throw new Error('Pilih periode sebelum memproses CSV eAbsensi.');
        const acceptInvalid = !!el.querySelector('#eabsensiAcceptInvalid')?.checked;
        const result = await mod.commit(batch.import_id, period, acceptInvalid);
        let loaded = false;
        let loadError = '';
        try {
          loaded = await hydrateCommittedEabsensiWorkflow(result);
        } catch (error) {
          loadError = error.message;
        }
        await renderEabsensiResult(el, result, {
          workflowLoaded:loaded,
          notice:loadError
        });
      } catch (error) {
        commit.disabled = false;
        if (EVKIN.toast) EVKIN.toast(error.message, 'error');
      }
    };
    const continueButton = el.querySelector('#eabsensiContinue');
    if (continueButton) continueButton.onclick = () => EVKIN.go('config');
    const usePrevious = el.querySelector('#eabsensiUsePrevious');
    if (usePrevious) usePrevious.onclick = async () => {
      usePrevious.disabled = true;
      try {
        const committed = await mod.useCommitted(batch.import_id);
        const loaded = await hydrateCommittedEabsensiWorkflow(committed);
        await renderEabsensiResult(el, committed, {workflowLoaded:loaded});
      } catch (error) {
        usePrevious.disabled = false;
        if (EVKIN.toast) EVKIN.toast(error.message, 'error');
      }
    };
    const resetButton = el.querySelector('#eabsensiResetPeriod');
    if (resetButton) resetButton.onclick = async () => {
      resetButton.disabled = true;
      try {
        const plan = await mod.previewResetPeriod(authoritative.import_id);
        const phrase = prompt(`Reset import absensi periode ${plan.period}?\nRaw Import: ${Number(plan.raw_import || 0).toLocaleString('id-ID')} record\n\nMaster SDM, Tukin, SKP, Pengembalian, dan periode lain tidak akan dihapus.\nKetik RESET untuk melanjutkan:`);
        if (String(phrase || '').trim().toUpperCase() !== 'RESET') {
          resetButton.disabled = false;
          return;
        }
        await mod.resetPeriod(authoritative.import_id);
        if (EVKIN.exportBackend && EVKIN.exportBackend.refreshGeneration) {
          await EVKIN.exportBackend.refreshGeneration();
        }
        if (typeof EVKIN.load === 'function') await EVKIN.load();
        mod.currentBatch = null;
        mod.setMode(IMPORT_MODES.ROUTER);
        if (EVKIN.toast) EVKIN.toast('Reset Import Periode berhasil.', 'success');
        EVKIN.go('import');
      } catch (error) {
        resetButton.disabled = false;
        if (EVKIN.toast) EVKIN.toast(error.message, 'error');
      }
    };
  }

  async function renderBatch(el, batch, notice) {
    let preview = {rows:[]};
    let errors = {rows:[], total:0};
    let previewError = '';
    let errorsError = '';
    try {
      preview = await mod.preview(batch.import_id, 1, false);
    } catch (error) {
      previewError = error.message;
    }
    try {
      errors = await mod.preview(batch.import_id, 1, true);
    } catch (error) {
      errorsError = error.message;
    }
    const periods = (batch.periods || []).map(value => `<option value="${EVKIN.util.esc(value)}" ${value === batch.period ? 'selected' : ''}>${EVKIN.util.esc(value)}</option>`).join('');
    const periodPlaceholder = batch.period ? '' : '<option value="" selected disabled>Pilih periode...</option>';
    const isEabsensi = batch.source_format === 'csv_eabsensi';
    const previewRows = (preview.rows || []).map(row => `<tr><td>${row.source_row}</td><td>${EVKIN.util.esc(row.nip || '')}</td><td>${EVKIN.util.esc(row.tanggal || '')}</td><td>${EVKIN.util.esc(row.jam_masuk || '')}</td><td>${EVKIN.util.esc(row.jam_pulang || '')}</td><td>${EVKIN.util.esc(row.zona || '')}</td><td>${EVKIN.util.esc((row.error_codes || []).join(', '))}</td></tr>`).join('');
    const errorRows = (errors.rows || []).map(row => `<tr><td>${row.source_row}</td><td>${EVKIN.util.esc(row.nip || '')}</td><td>${EVKIN.util.esc(row.tanggal || '')}</td><td>${EVKIN.util.esc(row.severity || '')}</td><td>${EVKIN.util.esc((row.error_codes || []).join(', '))}</td></tr>`).join('');
    el.querySelector('#backendImportResult').innerHTML = `
      ${notice ? `<div class="alert ${batch.status === 'READY' ? 'info' : 'error'}">${EVKIN.util.esc(notice)}</div>` : ''}
      <div class="alert ${batch.status === 'READY' ? 'success' : batch.status === 'COMMITTED' ? 'success' : 'error'}">Status: <strong>${EVKIN.util.esc(batch.status)}</strong>${batch.error_message ? ' — ' + EVKIN.util.esc(batch.error_message) : ''}</div>
      ${summaryCards(batch)}
      ${previewError ? `<div class="alert error">Preview gagal dimuat: ${EVKIN.util.esc(previewError)}</div>` : ''}
      <div class="card"><h3>Preview backend (maks. 50 baris)</h3><div class="table-wrap"><table class="data-table"><thead><tr><th>Row</th><th>NIP</th><th>Tanggal</th><th>Masuk</th><th>Pulang</th><th>Zona</th><th>Issue</th></tr></thead><tbody>${previewRows}</tbody></table></div></div>
      ${errorsError ? `<div class="alert error">Contoh error gagal dimuat: ${EVKIN.util.esc(errorsError)}</div>` : ''}
      ${errors.rows && errors.rows.length ? `<div class="card"><h3>Contoh warning/blocker (maks. 50 baris)</h3><p class="alert warn">${errors.total.toLocaleString('id-ID')} baris perlu ditinjau. ${Number(batch.invalid_rows || 0) > 0
        ? 'Konfirmasi hanya diperlukan untuk melanjutkan tanpa baris invalid.'
        : 'Semua baris dapat diproses tanpa konfirmasi invalid. Jam masuk/pulang kosong saja bukan blocker.'}</p><div class="table-wrap"><table class="data-table"><thead><tr><th>Row</th><th>NIP</th><th>Tanggal</th><th>Severity</th><th>Alasan</th></tr></thead><tbody>${errorRows}</tbody></table></div></div>` : ''}
      ${batch.status === 'READY' ? `<div class="actions-row"><label>Periode <select id="backendImportPeriod">${periodPlaceholder}${periods}</select></label>${Number(batch.invalid_rows || 0) > 0 ? '<label><input type="checkbox" id="backendImportAcceptInvalid"> Saya memahami baris invalid tidak ikut disimpan</label>' : ''}<button class="btn btn-primary" id="backendImportCommit">Simpan Impor</button></div>` : ''}
      ${batch.status === 'COMMITTED' ? (batch._workflowLoaded
        ? `<div class="alert success">Import ${isEabsensi ? 'CSV eAbsensi' : 'Excel/Standard'} tersimpan dan workflow berhasil dimuat.</div><div class="actions-row"><button class="btn btn-primary" id="backendImportContinue">Lanjut ke Konfigurasi</button></div>`
        : `<div class="alert success">Import ${isEabsensi ? 'CSV eAbsensi' : 'Excel/Standard'} sudah COMMITTED.</div><div class="actions-row"><button class="btn btn-primary" id="backendImportContinue">Lanjut ke Konfigurasi</button></div>`) : ''}`;
    const commit = el.querySelector('#backendImportCommit');
    if (commit) commit.onclick = async () => {
      commit.disabled = true;
      try {
        const selectedPeriod = el.querySelector('#backendImportPeriod').value;
        if (!selectedPeriod) throw new Error('Pilih periode secara eksplisit sebelum commit.');
        const result = await mod.commit(batch.import_id, selectedPeriod,
          !!el.querySelector('#backendImportAcceptInvalid')?.checked);
        if (EVKIN.toast) EVKIN.toast('Import backend berhasil di-commit.', 'success');
        if (result.source_format === 'csv_eabsensi') {
          let loaded = false;
          let loadNotice = '';
          try {
            loaded = await hydrateCommittedEabsensiWorkflow(result);
          } catch (error) {
            loadNotice = error.message;
          }
          await renderBatch(el, {...result, _workflowLoaded:loaded}, loadNotice);
        } else {
          let loaded = false;
          let loadNotice = '';
          try {
            loaded = await hydrateCommittedWorkflow(result);
          } catch (error) {
            loadNotice = error.message;
          }
          await renderBatch(el, {...result, _workflowLoaded:loaded}, loadNotice);
        }
      } catch (error) {
        commit.disabled = false;
        if (EVKIN.toast) EVKIN.toast(error.message, 'error');
      }
    };
    const continueButton = el.querySelector('#backendImportContinue');
    if (continueButton) continueButton.onclick = () => EVKIN.go('config');
  }

  function renderBackend(el) {
    el.innerHTML = `<div class="module-head"><div><h2>① Import Data</h2><p class="muted">Pilih jalur sumber secara eksplisit. Routing tidak menggunakan jabatan dan tidak memiliki fallback otomatis.</p></div></div>
      <div class="card"><h3>1. Import Excel Existing</h3>
        <div class="alert info">IMPORT ENGINE: Python Backend / Standard (Excel Existing).</div>
        <p class="muted">Proses cepat XLSX/XLSM/CSV via engine backend Python &amp; DuckDB tanpa freeze browser. Rekap &amp; hitung harian tetap 100% identik.</p>
        <label for="excelBackendFile">Pilih File Excel (.xlsx, .xlsm) atau CSV</label>
        <input type="file" id="excelBackendFile" accept=".xlsx,.xlsm,.csv">
        <div class="actions-row">
          <button class="btn btn-primary" id="excelBackendUpload">Proses Excel via Backend</button>
          <button class="btn btn-secondary" id="openLegacyExcel">Buka Versi Browser Legacy (JavaScript Legacy / Excel Existing)</button>
        </div>
        <div id="excelBackendProgress" class="muted"></div></div>
      <div class="card"><h3>2. Import Absensi Wali</h3>
        <div class="alert info">IMPORT ENGINE: JavaScript Legacy / cross-day.</div>
        <p class="muted">Import Wali tetap menggunakan parser dan pairing legacy.</p>
        <input type="file" id="legacyWaliFile" accept=".xlsx,.xls,.csv" hidden>
        <button class="btn btn-secondary" id="legacyWaliPick">Import Wali dengan JavaScript Legacy</button>
        <div id="legacyWaliProgress" class="muted"></div></div>
      <div class="card"><h3>3. Import CSV eAbsensi</h3>
        <div class="alert ${mod.csvEabsensiEnabled ? 'info' : 'warn'}">IMPORT ENGINE: Python Backend / CSV_EABSENSI_SPECIAL${mod.csvEabsensiEnabled ? '' : ' — belum diaktifkan'}.</div>
        <p class="muted">Jalur khusus untuk CSV eAbsensi; tidak menggunakan jabatan untuk routing.</p>
        <label for="eabsensiCsvFile">Pilih File CSV</label>
        <input type="file" id="eabsensiCsvFile" accept=".csv,text/csv" ${mod.csvEabsensiEnabled ? '' : 'disabled'}>
        <label class="checkbox-row"><input type="checkbox" id="eabsensiCrossDay" ${mod.csvEabsensiEnabled ? '' : 'disabled'}> Aktifkan pairing lintas hari / shift malam</label>
        <button class="btn btn-primary" id="eabsensiCsvUpload" ${mod.csvEabsensiEnabled ? '' : 'disabled'}>Proses CSV eAbsensi</button>
        <div id="eabsensiCsvProgress" class="muted"></div></div>
      <div id="backendImportResult"></div>`;
    const excelUploadBtn = el.querySelector('#excelBackendUpload');
    if (excelUploadBtn) {
      excelUploadBtn.onclick = async () => {
        mod.setMode(IMPORT_MODES.STANDARD_BACKEND);
        const file = el.querySelector('#excelBackendFile') ? el.querySelector('#excelBackendFile').files[0] : null;
        if (!file) return EVKIN.toast && EVKIN.toast('Pilih file Excel terlebih dahulu.', 'error');
        if (/\.xls$/i.test(file.name)) {
          if (EVKIN.toast) EVKIN.toast('File .xls format lama (Excel 97-2003) disarankan disimpan sebagai .xlsx untuk kecepatan maksimal, atau gunakan tombol browser legacy.', 'warn');
        }
        excelUploadBtn.disabled = true;
        const progressEl = el.querySelector('#excelBackendProgress');
        if (progressEl) progressEl.textContent = 'Mengunggah file ke backend...';
        const startedAt = Date.now();
        showImportModal({filename: file.name, isEabsensi: false});
        updateImportModal({status: 'UPLOADING', progress: 5, staged_rows: 0, total_rows: 0}, startedAt);
        try {
          const created = await mod.upload(file, {
            operator:'operator', sourceFormat:'standard'
          });
          updateImportModal(created, startedAt);
          const batch = await mod.wait(created.import_id, current => {
            if (progressEl) {
              progressEl.textContent = `${current.status}: ${Math.round(current.progress || 0)}% — ${Number(current.staged_rows || 0).toLocaleString('id-ID')} row staged`;
            }
            updateImportModal(current, startedAt);
          });
          updateImportModal(batch, startedAt);
          closeImportModal(600);
          await renderBatch(el, batch);
        } catch (error) {
          if (progressEl) {
            progressEl.innerHTML = `<span class="err">${EVKIN.util.esc(error.message)}</span>`;
          }
          updateImportModal({status: 'FAILED', error_message: error.message}, startedAt);
          if (EVKIN.toast) EVKIN.toast('Import backend gagal: ' + error.message, 'error');
        } finally {
          excelUploadBtn.disabled = false;
        }
      };
    }
    el.querySelector('#openLegacyExcel').onclick = () => renderLegacyExcel(el);
    const eabsensiButton = el.querySelector('#eabsensiCsvUpload');
    eabsensiButton.onclick = async () => {
      if (mod.csvProcessing) return;
      mod.setMode(IMPORT_MODES.CSV_EABSENSI_SPECIAL);
      const file = el.querySelector('#eabsensiCsvFile').files[0];
      if (!file) return EVKIN.toast && EVKIN.toast('Pilih CSV eAbsensi terlebih dahulu.', 'error');
      mod.csvProcessing = true;
      eabsensiButton.disabled = true;
      const crossDay = el.querySelector('#eabsensiCrossDay').checked;
      const startedAt = Date.now();
      let latestProgress = {status:'UPLOADING', filename:file.name, crossDay, startedAt};
      renderCsvProgress(el, latestProgress);
      showImportModal({filename: file.name, isEabsensi: true});
      updateImportModal(latestProgress, startedAt);
      const elapsedTimer = setInterval(() => {
        renderCsvProgress(el, latestProgress);
        updateImportModal(latestProgress, startedAt);
      }, 1000);
      try {
        const result = await mod.processCsvEabsensi(file, {
          operator:'operator', crossDay
        }, current => {
          latestProgress = {...current, filename:file.name, crossDay, startedAt};
          renderCsvProgress(el, latestProgress);
          updateImportModal(latestProgress, startedAt);
        });
        if (result.status === 'ALREADY_COMMITTED') {
          latestProgress = {...result, filename:file.name, crossDay, startedAt};
          renderCsvProgress(el, latestProgress);
          updateImportModal(latestProgress, startedAt);
          closeImportModal(600);
          await renderEabsensiResult(el, result);
          return;
        }
        let workflowLoaded = false;
        let workflowLoadError = '';
        try {
          latestProgress = {...result, status:'HYDRATING', filename:file.name, crossDay, startedAt};
          renderCsvProgress(el, latestProgress);
          updateImportModal(latestProgress, startedAt);
          workflowLoaded = await hydrateCommittedEabsensiWorkflow(result);
        } catch (error) {
          workflowLoadError = error.message;
        }
        latestProgress = {...result, status:'COMMITTED', filename:file.name, crossDay, startedAt};
        renderCsvProgress(el, latestProgress);
        updateImportModal(latestProgress, startedAt);
        closeImportModal(600);
        await renderEabsensiResult(el, result, {
          workflowLoaded,
          notice:workflowLoadError
        });
      } catch (error) {
        el.querySelector('#eabsensiCsvProgress').textContent = '';
        updateImportModal({status: 'FAILED', error_message: error.message}, startedAt);
        if (error.batch) {
          await renderEabsensiResult(el, error.batch, {notice:error.code === 'MULTI_PERIOD_REQUIRES_SELECTION'
            ? 'File mencakup beberapa periode. Pilih satu periode untuk diproses.'
            : error.message});
        } else {
          const stage = csvPhase(latestProgress.status);
          el.querySelector('#eabsensiCsvProgress').innerHTML = `<div class="alert error"><strong>Import gagal pada tahap: ${EVKIN.util.esc(stage)}</strong><br>${EVKIN.util.esc(error.message)}</div><button class="btn btn-secondary" id="eabsensiRetry">Coba Lagi</button>`;
          const retry = el.querySelector('#eabsensiRetry');
          if (retry) retry.onclick = () => eabsensiButton.click();
        }
      } finally {
        clearInterval(elapsedTimer);
        mod.csvProcessing = false;
        eabsensiButton.disabled = !mod.csvEabsensiEnabled;
      }
    };
    const waliInput = el.querySelector('#legacyWaliFile');
    el.querySelector('#legacyWaliPick').onclick = () => {
      mod.setMode(IMPORT_MODES.WALI_LEGACY);
      waliInput.click();
    };
    waliInput.onchange = async () => {
      const file = waliInput.files[0];
      if (!file || !EVKIN.waliImportMod) return;
      const status = el.querySelector('#legacyWaliProgress');
      status.textContent = 'JavaScript Legacy / Wali sedang memproses file...';
      try {
        const result = await EVKIN.waliImportMod.processFile(file, progress => {
          status.textContent = `JavaScript Legacy / Wali: ${Math.round(progress.progress || 0)}%`;
        });
        // processFile resolves only after the frozen Wali engine has completed
        // applyImportResult(), including syncImportedWorkflow().  Return to the
        // original Import renderer so the operator receives its complete legacy
        // workflow (stats, preview, pairing result, and next-step action), not
        // this router's one-line status.
        mod.returnToLegacyExcel();
      } catch (error) {
        status.textContent = 'JavaScript Legacy / Wali gagal: ' + error.message;
      }
    };
  }

  EVKIN.importMod.render = function (el) {
    if (mod.authority === 'IMPORT_BACKEND_V1' && mod.mode === IMPORT_MODES.LEGACY_EXCEL) return renderLegacyExcel(el);
    if (mod.authority === 'IMPORT_BACKEND_V1') return renderBackend(el);
    return legacyRender(el);
  };

  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
      mod.refreshAuthority().catch(error => {
        mod.authorityError = error.message;
        console.error('Import authority tidak dapat diverifikasi dari backend.', error);
        if (EVKIN.toast) EVKIN.toast('Authority import backend tidak dapat diverifikasi; pastikan status sebelum memulai import.', 'error');
      });
    });
  }
})();

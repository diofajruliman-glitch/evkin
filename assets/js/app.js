/* EVKIN ABSENSI v3.1 - app.js (router, layout, stepper) */
(function () {
  const EVKIN = window.EVKIN;
  let current = 'dashboard';

  const MODS = {
    dashboard: () => EVKIN.dashboardMod,
    pembulatan: () => EVKIN.pembulatanMod,
    import: () => EVKIN.importMod,
    config: () => EVKIN.configMod,
    'master-sdm': () => EVKIN.masterSdmMod,
    'skp-assessment': () => EVKIN.skpAssessmentMod,
  'tukin-annual': () => EVKIN.tukinAnnualMod,
  reconcile: () => EVKIN.reconcileMod,
  harian: () => EVKIN.harianMod,
  rekap: () => EVKIN.rekapMod,
  semifinal: () => EVKIN.semifinalMod,
  final: () => EVKIN.finalMod,
  'final-tukin': () => EVKIN.finalTukinMod,
  'final-tukin-results': () => EVKIN.finalTukinResultsMod,
  export: () => EVKIN.exportMod,
  'audit-viewer': () => EVKIN.auditViewerMod
  };

  EVKIN.toast = function (msg, type) {
    const normalizedType = {
      ok: 'ok',
      success: 'success',
      info: 'info',
      warn: 'warn',
      warning: 'warning',
      error: 'error'
    }[String(type || 'ok').toLowerCase()] || 'ok';

    const t = document.createElement('div');
    t.className = 'toast ' + normalizedType;
    t.textContent = msg;
    document.getElementById('toasts').appendChild(t);
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3200);
  };

  EVKIN.progress = function (options) {
    const opts = options || {};
    let panel = document.getElementById('evkinProcessProgress');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'evkinProcessProgress';
      panel.className = 'process-progress';
      panel.setAttribute('role', 'status');
      panel.setAttribute('aria-live', 'polite');
      document.body.appendChild(panel);
    }
    const percent = Math.max(0, Math.min(100, Number(opts.percent) || 0));
    const state = opts.state || 'active';
    panel.className = 'process-progress ' + state;
    panel.innerHTML = `<div class="process-progress-head"><strong>${EVKIN.util.esc(opts.title || 'Memproses data...')}</strong><span>${percent}%</span></div><div class="process-progress-track"><div class="process-progress-bar" style="width:${percent}%"></div></div><div class="process-progress-meta">${EVKIN.util.esc(opts.message || 'Mohon tunggu sampai proses selesai.')}</div>`;
    if (state === 'done' || state === 'error') setTimeout(() => { if (panel && panel.classList.contains(state)) panel.remove(); }, 3500);
  };

  EVKIN.requireBackend = function (message) {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) {
      EVKIN.toast(message || 'Backend DuckDB wajib aktif untuk menjalankan workflow master-only.', 'error');
      return false;
    }
    return true;
  };

  EVKIN.clearBrowserCache = async function (options = {}) {
    if (!EVKIN.db || typeof EVKIN.db.resetDatabase !== 'function') {
      return { ok: false, status: 'ERROR', error: 'Fitur cache browser tidak tersedia.' };
    }
    try {
      return await EVKIN.db.resetDatabase(options);
    } catch (error) {
      return { ok: false, status: 'ERROR', error: String(error && error.message || error) };
    }
  };

  EVKIN.syncBackendWorkflow = async function (onProgress, options = {}) {
    if (EVKIN.deletingAbsensi) throw new Error('Penghapusan workflow absensi sedang berlangsung.');
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return null;
    const S = EVKIN.state;
    const canonical = EVKIN.harianMod?.usesBackend?.();
    const optional = canonical && EVKIN.finalMod?.optionalRows;
    const skpRows = (S.finalSKP && S.finalSKP.rows) || [];
    const kombinasiRows = (S.finalKombinasi && S.finalKombinasi.rows) || [];
    const combined = S.finalCombinedSKP || {};
    const byNip = (rows) => new Map(rows.map((row) => [String(row.nip), row]));
    const skpByNip = byNip(skpRows);
    const kombinasiByNip = byNip(kombinasiRows);
    const combinedSkpByNip = byNip(combined.skpRows || []);
    const eabsensiByNip = byNip(combined.eabsensiRows || []);
    const esdmByNip = byNip(combined.esdmRows || []);
    const finalSkpData = optional ? optional('final_skp', S.final || []) : (S.final || []).map((item) => {
      const skp = skpByNip.get(String(item.nip)) || { skpPct: 0 };
      const skpNominal = Number(item.tukin || 0) * 0.70 * (Math.min(100, Number(skp.skpPct) || 0) / 100);
      return { ...item, skpPct: Number(skp.skpPct) || 0, nominalSKP: skpNominal, finalDibayarSKP: Number(item.tukin || 0) - Number(item.nominal || 0) - skpNominal };
    });
    const kombinasiData = optional ? optional('final_kombinasi', S.final || []) : (S.final || []).map((item) => {
      const source = kombinasiByNip.get(String(item.nip)) || { eabs: 0, esdm: 0 };
      const candidates = [];
      if (combined.useApp !== false) candidates.push(Number(item.potPct) || 0);
      if (combined.useEabsensi) candidates.push(Number(source.eabs) || 0);
      if (combined.useEsdm) candidates.push(Number(source.esdm) || 0);
      const selectedPct = candidates.length ? Math.min(...candidates) : 0;
      const nominal = Number(item.komponen || 0) * selectedPct / 100;
      return { ...item, aplikasiPct: Number(item.potPct) || 0, eabsensiPct: Number(source.eabs) || 0, esdmPct: Number(source.esdm) || 0, potonganTerpilihPct: selectedPct, nominalKombinasi: nominal, finalDibayarKombinasi: Number(item.tukin || 0) - nominal };
    });
    const combinedData = optional ? optional('final_combined_skp', S.final || []) : (S.final || []).map((item) => {
      const skp = combinedSkpByNip.get(String(item.nip)) || { pct: 0, skpPct: 0 };
      const skpPct = Number(skp.skpPct ?? skp.pct) || 0;
      const candidates = [];
      if (combined.useApp) candidates.push(Number(item.potPct) || 0);
      const eabsensi = eabsensiByNip.get(String(item.nip));
      const esdm = esdmByNip.get(String(item.nip));
      if (combined.useEabsensi && eabsensi) candidates.push(Number(eabsensi.pct) || 0);
      if (combined.useEsdm && esdm) candidates.push(Number(esdm.pct) || 0);
      const absensiPct = candidates.length ? Math.min(...candidates) : Number(item.potPct) || 0;
      const nominalAbsensi = Number(item.tukin || 0) * 0.30 * absensiPct / 100;
      const nominalSKP = Number(item.tukin || 0) * 0.70 * skpPct / 100;
      return { ...item, potonganAbsensiPct: absensiPct, nominalPotonganAbsensi: nominalAbsensi, potonganSKPPct: skpPct, nominalPotonganSKP: nominalSKP, totalPotonganPct: Math.round((absensiPct * 0.30 + skpPct * 0.70) * 100) / 100, totalNominalPotongan: nominalAbsensi + nominalSKP, finalDibayarKombinasiSKP: Number(item.tukin || 0) - nominalAbsensi - nominalSKP };
    });
    try {
      const reportProgress = (source, done, total) => {
        const safeTotal = Math.max(1, Number(total) || 1);
        const percent = Math.round(Math.max(0, Math.min(safeTotal, Number(done) || 0)) / safeTotal * 100);
        if (EVKIN.progress) EVKIN.progress({ title: 'Sinkronisasi data', percent, message: `${source || 'Workflow'}: ${done || 0} dari ${safeTotal} tahap...` });
        if (onProgress) onProgress(source, done, total);
      };
      const workflow = {
        config: {...Object.fromEntries(Object.entries(S.config || {}).filter(([key]) => key !== 'masterPegawai')),
          _workflow: {month:S.month, workflow_id:S.workflowMeta && S.workflowMeta.workflow_id || 'default',
            importStatus:S.workflowMeta && S.workflowMeta.status === 'ACTIVE' ? 'COMPLETE' : 'INCOMPLETE',
            timeMode:S.timeMode, importMode:S.importMode,
            importStats:S.importStats, semifinalVerified:S.semifinalVerified,
            finalSKP:S.finalSKP, finalKombinasi:S.finalKombinasi,
            finalCombinedSKP:S.finalCombinedSKP}},
        raw_import: S.raw || [],
        // raw_import is canonical. Never recreate the former per-row JSON copy.
        processed_data: [],
        daily_data: S.daily || [],
        rekap_sdm: S.rekap || [],
        semifinal: S.semifinal || S.daily || [],
        final_absensi: S.final || [],
        final_skp: finalSkpData,
        final_kombinasi: kombinasiData,
        final_combined_skp: combinedData
      };
      if (options.finalOnly) {
        for (const key of Object.keys(workflow)) {
          if (!['final_absensi', 'final_skp', 'final_kombinasi', 'final_combined_skp'].includes(key)) delete workflow[key];
        }
      }
      if (options.derivedOnly) {
        delete workflow.raw_import;
        delete workflow.processed_data;
      }
      if (Array.isArray(options.sources)) {
        const allowed = new Set(options.sources);
        for (const key of Object.keys(workflow)) if (!allowed.has(key)) delete workflow[key];
      }
      if (canonical) {
        for (const source of ['raw_import','processed_data','daily_data','rekap_sdm','final_absensi']) delete workflow[source];
      }
      const result = await EVKIN.exportBackend.syncWorkflow(workflow, undefined, reportProgress);
      if (EVKIN.progress) EVKIN.progress({ title: 'Sinkronisasi data', percent: 100, message: 'Data berhasil disinkronkan.', state: 'done' });
      if (EVKIN.auditMod) EVKIN.auditMod.logDatabaseSync('SQLite Python', 'SUCCESS', result.counts);
      return result;
    } catch (error) {
      if (EVKIN.progress) EVKIN.progress({ title: 'Sinkronisasi data gagal', message: error.message, state: 'error' });
      if (EVKIN.auditMod) EVKIN.auditMod.logDatabaseSync('SQLite Python', 'FAILED', { message: error.message });
      throw error;
    }
  };

  EVKIN.syncImportedWorkflow = async function (onProgress) {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return null;
    if (!EVKIN.exportBackend.syncTableChunks) {
      return EVKIN.syncBackendWorkflow(onProgress, {
        sources: ['config', 'raw_import', 'processed_data', 'daily_data', 'rekap_sdm',
          'semifinal', 'final_absensi', 'final_skp', 'final_kombinasi', 'final_combined_skp']
      });
    }
    const S = EVKIN.state;
    const workflowId = S.workflowMeta && S.workflowMeta.workflow_id || 'default';
    if (S.workflowMeta) S.workflowMeta.status = 'IMPORTING';
    const scope = {workflow_id:workflowId, periode:S.month || null};
    const config = {...Object.fromEntries(Object.entries(S.config || {}).filter(([key]) => key !== 'masterPegawai')),
      _workflow: {month:S.month, workflow_id:workflowId, timeMode:S.timeMode, importMode:S.importMode,
        importStatus:'COMPLETE', importStats:S.importStats, semifinalVerified:false,
        finalSKP:S.finalSKP, finalKombinasi:S.finalKombinasi,
        finalCombinedSKP:S.finalCombinedSKP}};
    const clearedSources = ['daily_data', 'rekap_sdm', 'semifinal', 'final_absensi',
      'final_skp', 'final_kombinasi', 'final_combined_skp'];
    // Legacy Excel/Wali still owns parsing and pairing in the browser. Persist
    // its already-normalized result through the canonical raw_import contract
    // so it can coexist with Backend Import V1 without changing either engine.
    const canonicalRawRows = (S.data || []).filter(row => row && !row.deleted).map((row, index) => ({
      workflow_id: workflowId,
      import_id: workflowId,
      source_row: index + 1,
      nip: String(row.nip || ''),
      nama: String(row.nama || ''),
      jabatan: String(row.jabatan || ''),
      tanggal: String(row.tanggal && row.tanggal.key || row.tanggalKey || row.tanggal || ''),
      tanggal_pulang: String(row.tanggalPulangObj && row.tanggalPulangObj.key || row.tanggalPulang || ''),
      jam_masuk: String(row.masukRaw || (row.masukMin == null ? '' : EVKIN.util.fmtTime(row.masukMin))),
      jam_pulang: String(row.pulangRaw || (row.pulangMin == null ? '' : EVKIN.util.fmtTime(row.pulangMin))),
      zona: String(row.zona || ''),
      provinsi: String(row.provinsi || ''),
      kabupaten: String(row.kabupaten || ''),
      jenis: String(row.jenis || ''),
      source_format: S.importMode === 'wali' ? 'wali' : 'legacy_excel',
      raw_json: JSON.stringify(row),
      file_sha256: ''
    }));
    const progress = (source, done, total, rowsDone) => {
      if (onProgress) onProgress(source, done, total, rowsDone);
      if (EVKIN.progress) {
        EVKIN.progress({title:'Sinkronisasi import', percent:Math.round(done / Math.max(1,total) * 100),
          message:`${source}: batch ${done}/${total}, ${Number(rowsDone || 0).toLocaleString('id-ID')} baris...`});
      }
    };
    // Old derived results belong to the previous import and are the only
    // additional tables intentionally cleared here.
    try {
      for (const source of clearedSources) await EVKIN.exportBackend.syncTableChunks(source, [], {onProgress:progress, ...scope});
      const rawResult = await EVKIN.exportBackend.syncTableChunks('raw_import', canonicalRawRows, {onProgress:progress, ...scope});
      const persistedRaw = Number(rawResult && rawResult.total_rows);
      if (!Number.isFinite(persistedRaw) || persistedRaw !== canonicalRawRows.length) {
        throw new Error(`Verifikasi penyimpanan import gagal: ${Number.isFinite(persistedRaw) ? persistedRaw : 0}/${canonicalRawRows.length} baris tersimpan.`);
      }
      // raw_import is the canonical persisted import.  The former workflow_records
      // JSON copy duplicated every source row and made the database/browser grow
      // linearly twice; calculation records are restored on demand from raw_import.
      await EVKIN.exportBackend.syncTableChunks('processed_data', [], {onProgress:progress, ...scope});
      // Publish configuration last so startup metadata never advertises a new
      // import before both source tables have completed.
      await EVKIN.exportBackend.syncTableChunks('config', [config], {onProgress:progress, ...scope});
    } catch (error) {
      if (S.workflowMeta) S.workflowMeta.status = 'ERROR';
      throw error;
    }
    if (S.workflowMeta) {
      S.workflowMeta.workflow_id = workflowId;
      S.workflowMeta.periode = S.month || null;
      S.workflowMeta.status = 'ACTIVE';
      S.workflowMeta.total_row_counts = {...(S.workflowMeta.total_row_counts || {}),
        raw_import:canonicalRawRows.length, processed_data:0,
        daily_data:0, rekap_sdm:0, semifinal:0, final_absensi:0,
        final_skp:0, final_kombinasi:0, final_combined_skp:0};
      S.workflowMeta.row_counts = {...S.workflowMeta.total_row_counts};
    }
    S.workflowLoaded = {};
    if (EVKIN.progress) EVKIN.progress({title:'Sinkronisasi import', percent:100, message:'Import tersimpan lengkap.', state:'done'});
    return {status:'ok', counts:{raw_import:canonicalRawRows.length, processed_data:0}};
  };

  EVKIN.saveWorkflowToIndexedDB = async function () {
    if (!EVKIN.db) return false;
    const S = EVKIN.state;
    await Promise.all([
      EVKIN.db.saveConfig ? EVKIN.db.saveConfig(S.config) : Promise.resolve(),
      EVKIN.db.replaceStore('raw_import', S.raw || []),
      EVKIN.db.replaceStore('daily_data', S.daily || []),
      EVKIN.db.replaceStore('rekap_sdm', S.rekap || []),
      EVKIN.db.replaceStore('final_absensi', S.final || []),
      EVKIN.db.replaceStore('final_skp', (S.finalSKP && S.finalSKP.rows) || []),
      EVKIN.db.replaceStore('final_kombinasi', (S.finalKombinasi && S.finalKombinasi.rows) || []),
      EVKIN.db.replaceStore('final_combined_skp', (S.finalCombinedSKP && S.finalCombinedSKP.combinedRows) || []),
      EVKIN.db.saveMasterData ? EVKIN.db.saveMasterData((S.config && S.config.masterPegawai) || []) : Promise.resolve()
    ]);
    return true;
  };

  EVKIN.syncMasterDatabase = async function (list) {
    const S = EVKIN.state;
    const rows = Array.isArray(list) ? list : (S.config && S.config.masterPegawai) || [];
    if (S.config) S.config.masterPegawai = rows;
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) {
      if (EVKIN.db && typeof EVKIN.db.saveMasterData === 'function') {
        await EVKIN.db.saveMasterData(rows).catch(() => {});
      }
      return false;
    }
    try {
      const masterUrl = new URL('/api/master-sdm', EVKIN.exportBackend.apiBase);
      masterUrl.searchParams.set('page', '1');
      masterUrl.searchParams.set('per_page', '1000');
      const response = await fetch(masterUrl.toString(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows })
      });
      if (response.ok) {
        const result = await response.json().catch(() => ({}));
        if (result && result.status === 'ok') {
          return true;
        }
      }
      throw new Error('Master SDM gagal disimpan. Gunakan modul Master SDM dan coba lagi.');
    } catch (error) {
      console.warn('Master SDM gagal disimpan:', error);
      throw error;
    }
  };

  EVKIN.saveMasterPegawaiToBackend = async function (list) {
    return EVKIN.syncMasterDatabase(list);
  };

  EVKIN.lookupWorkflowMaster = async function (nips) {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) throw new Error('Backend Master SDM belum aktif');
    const response = await fetch(new URL('/api/shared-master/lookup-tukin', EVKIN.exportBackend.apiBase), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nips: [...new Set(nips.map(nip => String(nip || '').trim()).filter(Boolean))] }),
      signal: AbortSignal.timeout(60000)
    });
    const result = await response.json();
    if (!response.ok || !Array.isArray(result.rows)) throw new Error(result.detail || 'Master SDM gagal dibaca');
    return new Map(result.rows.map(row => [String(row.nip).trim(), row]));
  };

  EVKIN.refreshMasterPegawaiFromBackend = async function () {
    const S = EVKIN.state;
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return false;
    try {
      const masterUrl = new URL('/api/master-sdm', EVKIN.exportBackend.apiBase);
      masterUrl.searchParams.set('page', '1');
      masterUrl.searchParams.set('per_page', '1000');
      const response = await fetch(masterUrl.toString(), {
        // Load the active master page-by-page. Production masters can be much
        // larger than the endpoint's 1000-row page limit.
        method: 'GET',
        cache: 'no-store'
      });
      if (!response.ok) {
        throw new Error('Master SDM gagal dibaca: ' + response.status);
      }
      const result = await response.json();
      let sourceRows = Array.isArray(result && result.rows) ? result.rows.slice() : [];
      const totalPages = Math.max(1, Number(result && (result.total_pages || result.pages)) || 1);
      for (let page = 2; page <= totalPages; page++) {
        masterUrl.searchParams.set('page', String(page));
        const nextResponse = await fetch(masterUrl.toString(), { method: 'GET', cache: 'no-store' });
        if (!nextResponse.ok) throw new Error(`Master SDM halaman ${page} gagal dimuat (${nextResponse.status})`);
        const nextResult = await nextResponse.json();
        sourceRows = sourceRows.concat(Array.isArray(nextResult && nextResult.rows) ? nextResult.rows : []);
      }
      const master = sourceRows.map((row) => ({
        ...row,
        tukin: Number(row.tukin ?? row.tukin_gross ?? 0) || 0
      }));
      S.config.masterPegawai = master;
      return master;
    } catch (error) {
      console.warn('Gagal refresh master SDM dari backend:', error);
      return false;
    }
  };

  EVKIN.go = async function (id) {
    // Non-workflow modules bypass lock check
    const bypassLock = ['dashboard', 'audit-viewer', 'pembulatan', 'master-sdm', 'reconcile'];

    if (!bypassLock.includes(id) && !EVKIN.requireBackend('Backend DuckDB wajib aktif sebelum membuka workflow master-only.')) {
      return;
    }

    if (!bypassLock.includes(id) && !EVKIN.stepUnlocked(id)) {
      EVKIN.toast('Tahap "' + labelOf(id) + '" masih terkunci. Selesaikan tahap sebelumnya.', 'error');
      return;
    }

    const resolveModule = function () {
      if (MODS[id]) return MODS[id]();
      if (EVKIN && EVKIN[id]) return EVKIN[id];
      const camel = id.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
      if (EVKIN && EVKIN[camel + 'Mod']) return EVKIN[camel + 'Mod'];
      if (EVKIN && EVKIN[camel]) return EVKIN[camel];
      return null;
    };

    const mod = resolveModule();
    if (!mod || typeof mod.render !== 'function') {
      EVKIN.toast('Tahap "' + labelOf(id) + '" tidak tersedia atau belum dimuat.', 'error');
      console.error('EVKIN.go called with unknown or unrenderable module:', id, mod);
      return;
    }

    current = id;
    document.body.dataset.page = id;
    if (EVKIN.auditMod) EVKIN.auditMod.logStage(id);
    const main = document.getElementById('main');
    main.innerHTML = '';
    try {
      if (typeof EVKIN.ensureWorkflowDataForModule === 'function') {
        main.innerHTML = '<div class="card"><div class="alert info">Memuat data modul secara bertahap...</div></div>';
        await EVKIN.ensureWorkflowDataForModule(id);
      }
      main.innerHTML = '';
      mod.render(main);
    } catch (e) {
      console.error(e);
      main.innerHTML = '<div class="alert error">Terjadi kesalahan: ' + EVKIN.util.esc(e.message) + '</div>';
    }
    renderSidebar();
    renderStepper();
    const pageTitle = document.getElementById('topbarPageTitle');
    const pageSubtitle = document.getElementById('topbarPageSubtitle');
    const periodLabel = document.getElementById('topbarPeriod');
    if (pageTitle) pageTitle.textContent = labelOf(id);
    if (pageSubtitle) pageSubtitle.textContent = id === 'dashboard'
      ? 'Ringkasan pengolahan absensi dan Tukin'
      : 'EVKIN Tools · ' + labelOf(id);
    if (periodLabel) periodLabel.textContent = EVKIN.state.month
      ? EVKIN.util.monthLabel(EVKIN.state.month)
      : 'Periode belum dipilih';
    renderTopbarNotification();
    if (window.lucide) lucide.createIcons();
    main.scrollTop = 0;
  };

  function labelOf(id) {
    if (id === 'audit-viewer') return 'Audit Log';
    const s = EVKIN.STEPS.find(x => x.id === id);
    return s ? s.label : id;
  }

  EVKIN.resetModuleData = async function (moduleName, label, stores, refresh) {
    if (!EVKIN.requireBackend('Aktifkan backend sebelum menghapus data modul.')) return null;
    const preview = await EVKIN.previewResetProfile(moduleName);
    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'max-width:560px;width:90%;padding:24px;border-radius:12px';
    dialog.innerHTML = `<h2>Hapus seluruh data ${EVKIN.util.esc(label)}?</h2><p>${preview.deleted_rows || 0} baris pada ${(preview.target_tables || []).length} tabel akan dikosongkan.</p><p>${EVKIN.util.esc(preview.preserved_note || 'Master penting tetap disimpan.')}</p><details><summary>Rincian target</summary><ul>${(preview.tables || []).map(item => `<li>${EVKIN.util.esc(item.table)}: ${item.rows || 0} baris</li>`).join('')}</ul></details><div class="actions-row"><button type="button" id="moduleResetCancel" class="btn btn-secondary">Batal</button><button type="button" id="moduleResetConfirm" class="btn btn-danger">Hapus</button></div>`;
    document.body.appendChild(dialog);
    const confirmed = await new Promise(resolve => {
      dialog.showModal();
      dialog.querySelector('#moduleResetCancel').onclick = () => { dialog.close(); resolve(false); };
      dialog.querySelector('#moduleResetConfirm').onclick = () => { dialog.close(); resolve(true); };
      dialog.oncancel = () => resolve(false);
    });
    dialog.remove();
    if (!confirmed) return null;
    const result = await EVKIN.executeResetProfile(moduleName, preview, {reason:`Reset modul ${label} dari UI`});
    if (EVKIN.db && EVKIN.db.replaceStore) await Promise.all((stores || []).map(store => EVKIN.db.replaceStore(store, [])));
    if (typeof refresh === 'function') await refresh(result);
    EVKIN.toast(`${label} berhasil dihapus (${result.deleted_rows || 0} baris).`, 'success');
    return result;
  };

  EVKIN.confirmResetWorkflow = async function (button) {
    if (EVKIN.deletingAbsensi) return;
    if (!EVKIN.requireBackend('Aktifkan backend sebelum mereset seluruh data workflow.')) return;
    if (prompt('Reset SELURUH data workflow untuk SEMUA periode? Data impor absensi, konfigurasi workflow, Harian, Semi Final, Rekap SDM, dan Final akan dihapus dari database dan cache browser. Master SDM, Penilaian SKP, arsip Tukin tetap disimpan. Tindakan ini tidak dapat dibatalkan. Ketik HAPUS ABSENSI untuk melanjutkan:') !== 'HAPUS ABSENSI') return;
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = 'Mereset data workflow...';
    try {
      const result = await EVKIN.deleteAbsensiWorkflow();
      EVKIN.go('import');
      EVKIN.toast(`Seluruh data workflow direset (${result.deleted_rows || 0} baris dihapus). Silakan impor data baru.`, 'success');
    } catch (error) {
      EVKIN.toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  };

  function renderSidebar() {
    const nav = document.getElementById('nav');

    const renderItem = (s, workflowItem) => {
      const done = EVKIN.stepDone(s.id), unlocked = s.id === 'audit-viewer' ? true : EVKIN.stepUnlocked(s.id);
      let cls = 'nav-item' + (workflowItem ? ' workflow-item' : '');
      if (s.id === current) cls += ' active';
      if (!unlocked) cls += ' locked';
      let status = '';
      if (done) status = '<span class="ni-status green">✓</span>';
      else if (s.id === 'semifinal' && unlocked) status = '<span class="ni-status yellow">!</span>';
      else if (!unlocked) status = '<span class="ni-status gray">🔒</span>';
      return `<div class="${cls}" data-nav="${s.id}" data-testid="nav-${s.id}"><i data-lucide="${s.icon}"></i><span class="ni-label">${s.label}</span>${status}</div>`;
    };

    const byId = id => EVKIN.STEPS.find(s => s.id === id);
    const item = (id, label) => ({...byId(id), label:label || byId(id)?.label});
    const auditItem = { id: 'audit-viewer', label: 'Audit Log', icon: 'activity' };
    const attendanceWorkflow = ['import', 'config', 'harian', 'semifinal', 'rekap', 'export']
      .map(byId).filter(Boolean);
    const tukinWorkflow = ['tukin-annual', 'final-tukin', 'final-tukin-results', 'final']
      .map(byId).filter(Boolean);

    nav.innerHTML = `
      <div class="nav-section"><div class="nav-section-label">Dashboard</div>${renderItem(item('dashboard','Dashboard'), false)}</div>
      <div class="nav-section"><div class="nav-section-label">Workflow Absensi</div>${attendanceWorkflow.map(step => renderItem(step, true)).join('')}<button type="button" class="btn btn-danger btn-sm workflow-reset" id="workflowResetAll" data-testid="workflow-reset-all" title="Hapus data impor dan hasil proses workflow semua periode"><i data-lucide="trash-2"></i><span>Reset Seluruh Data Workflow</span></button></div>
      <div class="nav-section"><div class="nav-section-label">Penilaian SKP</div>${renderItem(item('skp-assessment','Proses SKP'), false)}</div>
      <div class="nav-section"><div class="nav-section-label">Pengelolaan Tukin</div>${tukinWorkflow.map(step => renderItem(step, false)).join('')}</div>
      <div class="nav-section"><div class="nav-section-label">Data & Tools</div>${renderItem(item('reconcile','Rekonsiliasi'), false)}${renderItem(byId('pembulatan'), false)}</div>
      <div class="nav-section"><div class="nav-section-label">Admin</div>${renderItem(item('master-sdm','Master SDM'), false)}<div class="nav-item" data-nav="skp-assessment" data-skp-tab="configuration"><i data-lucide="settings-2"></i><span class="ni-label">Pengaturan SKP</span></div>${renderItem(auditItem, false)}</div>
      `;

    nav.querySelector('#workflowResetAll').onclick = (event) => EVKIN.confirmResetWorkflow(event.currentTarget);

    // Attach navigation handlers
    nav.querySelectorAll('[data-nav]').forEach(n => n.onclick = () => {
      if (n.dataset.skpTab && EVKIN.skpAssessmentMod?.selectTab) EVKIN.skpAssessmentMod.selectTab(n.dataset.skpTab);
      else if (n.dataset.nav === 'skp-assessment' && EVKIN.skpAssessmentMod?.selectTab) EVKIN.skpAssessmentMod.selectTab('overview');
      EVKIN.go(n.dataset.nav);
      if (window.matchMedia('(max-width: 860px)').matches) document.body.classList.remove('sidebar-collapsed');
    });

  }

  function renderStepper() {
    const attendanceIds = ['import', 'config', 'harian', 'semifinal', 'rekap', 'export'];
    const isAttendance = attendanceIds.includes(current);
    const wf = attendanceIds.map(byId).filter(Boolean);
    const bar = document.getElementById('stepper');
    bar.classList.toggle('is-dashboard', !isAttendance);
    if (!isAttendance) { bar.innerHTML = ''; return; }
    bar.innerHTML = wf.map((s, i) => {
      const done = EVKIN.stepDone(s.id), unlocked = EVKIN.stepUnlocked(s.id);
      let cls = 'step';
      if (done) cls += ' done'; else if (s.id === current) cls += ' current'; else if (!unlocked) cls += ' locked';
      const icon = done ? '✓' : (unlocked ? (i + 1) : '🔒');
      return `<div class="${cls}" data-step="${s.id}"><span class="step-dot">${icon}</span><span class="step-label">${s.label}</span></div>
        ${i < wf.length - 1 ? '<span class="step-line ' + (done ? 'done' : '') + '"></span>' : ''}`;
    }).join('');
    bar.querySelectorAll('[data-step]').forEach(n => n.onclick = () => EVKIN.go(n.dataset.step));
  }

  function renderTopbarNotification() {
    const button = document.getElementById('notificationButton');
    const dot = document.getElementById('notificationDot');
    if (!button || !dot) return;
    const daily = Array.isArray(EVKIN.state.daily) ? EVKIN.state.daily : [];
    const attentionCount = daily.filter(row => row.statusDuplikasi || row.statusLengkap === 'Tidak Lengkap').length;
    dot.classList.toggle('hidden', attentionCount === 0);
    button.setAttribute('aria-label', attentionCount
      ? `${attentionCount} data memerlukan perhatian`
      : 'Tidak ada notifikasi data');
    button.title = attentionCount ? `${attentionCount} data memerlukan perhatian` : 'Tidak ada notifikasi data';
  }

  document.addEventListener('DOMContentLoaded', function () {
    EVKIN.load();
    // Migrate: Ensure all records have proper tanggal objects with monthKey
    const S = EVKIN.state;
    const U = EVKIN.util;
    if (S.data && Array.isArray(S.data)) {
      S.data.forEach(rec => {
        if (rec.tanggal && typeof rec.tanggal === 'object') {
          // Record has tanggal object; ensure it has monthKey
          if (!rec.tanggal.monthKey && rec.tanggal.y && rec.tanggal.m && rec.tanggal.d) {
            // Rebuild full tanggal object with monthKey
            rec.tanggal = U.makeDate(rec.tanggal.y, rec.tanggal.m, rec.tanggal.d);
          }
        }
        // Don't auto-delete; let processing catch issues if they exist
      });
    }
    if (EVKIN.auditMod) EVKIN.auditMod.startSession();
    document.getElementById('sidebarToggle').onclick = () => document.body.classList.toggle('sidebar-collapsed');
    const sidebarBackdrop = document.getElementById('sidebarBackdrop');
    if (sidebarBackdrop) sidebarBackdrop.onclick = () => document.body.classList.remove('sidebar-collapsed');
    const notificationButton = document.getElementById('notificationButton');
    if (notificationButton) notificationButton.onclick = () => EVKIN.go('dashboard');
    const profileMenu = document.getElementById('profileMenu');
    if (profileMenu) {
      profileMenu.querySelectorAll('[data-topbar-go]').forEach(button => {
        button.onclick = () => {
          profileMenu.removeAttribute('open');
          EVKIN.go(button.dataset.topbarGo);
        };
      });
    }
    const topbarResetAll = document.getElementById('topbarResetAll');
    if (topbarResetAll) topbarResetAll.onclick = async () => {
      if (!confirm('Reset hasil workflow? Data sumber, konfigurasi, dan Master SDM tetap dipertahankan.')) return;
      if (profileMenu) profileMenu.removeAttribute('open');
      try { await EVKIN.resetAll(); }
      catch (error) { EVKIN.toast(error.message, 'error'); return; }
      EVKIN.go('dashboard');
      EVKIN.toast('Aplikasi direset');
    };
    const topbarDbReset = document.getElementById('topbarDbReset');
    const totalReset = document.getElementById('topbarTotalReset');
    if (totalReset) totalReset.onclick = async () => {
      if (!EVKIN.requireBackend('Aktifkan backend sebelum reset total.')) return;
      if (EVKIN.deletingAbsensi || EVKIN.harianMod?.processing) return EVKIN.toast('Tunggu proses workflow selesai.', 'error');
      EVKIN.deletingAbsensi = true;
      totalReset.disabled = true;
      let completed = false;
      try {
        await EVKIN.exportBackend.syncQueue;
        const request = async (path, body) => {
          const response = await fetch(new URL('/api/admin/reset-operational/' + path, EVKIN.exportBackend.apiBase), body ? {
            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
          } : {});
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || 'Reset gagal.');
          return result;
        };
        const plan = await request('preview');
        const dialog = document.createElement('dialog');
        dialog.style.cssText = 'max-width:760px;width:90%;max-height:85vh;overflow:auto;padding:24px;border-radius:12px';
        const esc = EVKIN.util.esc;
        dialog.innerHTML = `<h2>Reset Data Operasional</h2><p>Reset Data Operasional menghapus data workflow/transaksi dari server dan membersihkan cache browser. Master SDM, konfigurasi protected, dan audit tetap dipertahankan.</p><p>${esc(plan.preserved_note)}</p><p>Backup database wajib dibuat dan diverifikasi otomatis sebelum penghapusan. Pemulihan backup dilakukan secara administratif, bukan tombol Undo.</p><details><summary>Rincian ${plan.deleted_rows} baris yang akan dihapus</summary><ul>${plan.tables.filter(t => t.action === 'DELETE').map(t => `<li>${esc(t.table)}: ${t.rows} baris</li>`).join('')}</ul></details>${plan.blockers.length ? `<p class="alert error">Reset diblokir: ${plan.blockers.map(esc).join('; ')}</p>` : ''}<p><label>Nama petugas <input id="resetActor" autocomplete="off"></label></p><p><label>Alasan reset <input id="resetReason" autocomplete="off"></label></p><p><label>Ketik RESET TOTAL TANPA MASTER SDM <input id="resetPhrase" autocomplete="off"></label></p><div class="actions-row"><button id="resetCancel" class="btn btn-secondary">Batal</button><button id="resetConfirm" class="btn btn-danger" disabled>Backup &amp; Reset Data Operasional</button></div><p id="resetProgress"></p>`;
        document.body.appendChild(dialog);
        dialog.showModal();
        await new Promise(resolve => {
          const button = dialog.querySelector('#resetConfirm');
          const field = id => dialog.querySelector(id).value.trim();
          dialog.oninput = () => { button.disabled = !plan.can_reset || !field('#resetActor') || !field('#resetReason') || field('#resetPhrase') !== 'RESET TOTAL TANPA MASTER SDM'; };
          dialog.oncancel = () => resolve();
          dialog.querySelector('#resetCancel').onclick = () => { dialog.close(); resolve(); };
          button.onclick = async () => {
            button.disabled = true;
            dialog.querySelector('#resetCancel').disabled = true;
            dialog.oncancel = event => event.preventDefault();
            dialog.oninput = null;
            const progress = dialog.querySelector('#resetProgress');
            progress.textContent = 'Membuat dan memverifikasi backup, lalu mereset data. Jangan tutup halaman...';
            try {
              const result = await request('confirm', {preview_token:plan.preview_token, actor:field('#resetActor'), reason:field('#resetReason'), confirmation:field('#resetPhrase')});
              completed = true;
              if (EVKIN.exportBackend) EVKIN.exportBackend.mutationBlocked = true;
              if (EVKIN.db?.markCacheStale) EVKIN.db.markCacheStale();
              let cacheResult = {ok:false, status:'ERROR', error:'Fitur cache browser tidak tersedia.'};
              try {
                cacheResult = await EVKIN.clearBrowserCache();
                sessionStorage.removeItem('mp-final-filters');
              } catch (_) { /* clearBrowserCache already normalizes errors */ }
              const outcome = EVKIN.db?.resetOutcome
                ? EVKIN.db.resetOutcome(true, cacheResult)
                : {status:'PARTIAL_SUCCESS', message:'Reset data server berhasil. Cache browser belum berhasil dibersihkan.'};
              progress.textContent = `${outcome.message} ${result.deleted_rows} baris dihapus. Master SDM tetap disimpan. Backup: ${result.backup_path}`;
              button.textContent = 'Muat Ulang Aplikasi'; button.disabled = false;
              button.onclick = () => window.location.reload();
            } catch (error) {
              progress.textContent = `Reset belum terkonfirmasi berhasil: ${error.message}. Jika koneksi terputus, periksa status/audit sebelum mengulang.`;
              dialog.querySelector('#resetCancel').disabled = false;
              dialog.oncancel = () => resolve();
            }
          };
        });
        dialog.remove();
      } catch (error) { EVKIN.toast(error.message, 'error'); }
      finally { if (!completed) { EVKIN.deletingAbsensi = false; totalReset.disabled = false; } }
    };
    if (topbarDbReset) topbarDbReset.onclick = async () => {
      if (!confirm('Bersihkan cache browser di perangkat ini? Data server tidak akan diubah.')) return;
      if (profileMenu) profileMenu.removeAttribute('open');
      const result = await EVKIN.clearBrowserCache();
      if (result.status === 'SUCCESS') EVKIN.toast('Cache browser berhasil dibersihkan.', 'success');
      else if (result.status === 'BLOCKED') EVKIN.toast('Cache browser masih digunakan tab EVKIN lain. Tutup tab lain lalu coba lagi.', 'warning');
      else EVKIN.toast('Cache browser belum berhasil dibersihkan: ' + (result.error || result.status), 'warning');
    };
    const backendButton = document.getElementById('backendToggle');
    const backendStatus = document.getElementById('backendStatus');
    const setBackendActive = () => {
      EVKIN.exportBackend.enabled = true;
      backendStatus.className = 'pill green';
      backendStatus.textContent = 'Backend aktif';
      backendButton.innerHTML = '<i data-lucide="power-off"></i> Matikan Backend';
      window.dispatchEvent(new CustomEvent('evkin:backend-status', { detail: { enabled: true } }));
      if (window.lucide) lucide.createIcons();
    };
    const setBackendInactive = () => {
      EVKIN.exportBackend.enabled = false;
      backendStatus.className = 'pill gray';
      backendStatus.textContent = 'Backend mati';
      backendButton.innerHTML = '<i data-lucide="power"></i> Aktifkan Backend';
      window.dispatchEvent(new CustomEvent('evkin:backend-status', { detail: { enabled: false } }));
      if (window.lucide) lucide.createIcons();
    };
    backendButton.onclick = async () => {
      if (EVKIN.exportBackend && EVKIN.exportBackend.enabled) {
        setBackendInactive();
        EVKIN.toast('Backend dimatikan. Mode master sekarang berada di memory browser; aktifkan backend untuk sinkronisasi DuckDB.', 'info');
        return;
      }
      backendButton.disabled = true;
      backendButton.innerHTML = '<i data-lucide="loader-circle"></i> Menghubungkan...';
      if (window.lucide) lucide.createIcons();
      try {
        await EVKIN.exportBackend.activate();
        setBackendActive();
        EVKIN.toast('Backend berhasil diaktifkan', 'success');
        if (!await EVKIN.load()) throw new Error('State workflow gagal dimuat. Coba hubungkan kembali.');
        await EVKIN.refreshMasterPegawaiFromBackend();
        EVKIN.go('dashboard');
      } catch (error) {
        EVKIN.exportBackend.enabled = false;
        backendStatus.className = 'pill red';
        backendStatus.textContent = 'Backend tidak tersedia';
        backendButton.innerHTML = '<i data-lucide="power"></i> Aktifkan Backend';
        EVKIN.toast('Backend belum berjalan: ' + error.message, 'error');
      } finally {
        backendButton.disabled = false;
        if (window.lucide) lucide.createIcons();
      }
    };
    EVKIN.exportBackend.activate()
      .then(async () => {
        setBackendActive();
        // On startup DuckDB is authoritative.  Writing the still-empty browser
        // state back here caused ten schema-changing sync requests on every
        // reload, racing Master Pengembalian reads and occasionally taking the
        // DuckDB process down.  Hydrate the browser instead; writes happen only
        // after an actual workflow change.
        try {
          await EVKIN.load().catch(() => false);
        } catch (error) {
          console.warn('Pemuatan state awal dari backend gagal, namun backend tetap aktif:', error);
        }
        // Hydrating workflow config can contain an old/empty master cache.
        // Refresh the canonical master after hydration so it is not overwritten.
        await EVKIN.refreshMasterPegawaiFromBackend();
        EVKIN.go(EVKIN.stepUnlocked(current) ? current : 'dashboard');
      })
      .catch(() => { setBackendInactive(); });

    // Inisialisasi dropdown "Tools Absen" (akses cepat Import / Export)
    const toolsToggle = document.getElementById('toolsToggle');
    const toolsMenu = document.getElementById('toolsMenu');
    if (toolsToggle && toolsMenu) {
      toolsToggle.onclick = (e) => {
        e.stopPropagation();
        toolsMenu.classList.toggle('hidden');
        // update icons
        if (window.lucide) lucide.createIcons();
      };
      toolsMenu.querySelectorAll('[data-tool]').forEach(btn => {
        btn.onclick = () => {
          const id = btn.dataset.tool;
          toolsMenu.classList.add('hidden');
          if (id === 'import') EVKIN.go('import');
          else if (id === 'export') EVKIN.go('export');
          else EVKIN.go(id);
        };
      });
      // tutup saat klik di luar
      document.addEventListener('click', (e) => {
        if (!toolsMenu.classList.contains('hidden') && !document.getElementById('toolsDropdown').contains(e.target)) {
          toolsMenu.classList.add('hidden');
        }
      });
    }

    EVKIN.go('dashboard');
  });
})();

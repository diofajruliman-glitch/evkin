/* EVKIN ABSENSI v3.1 - state.js
   State global dan gating workflow. Penyimpanan master dilakukan di DuckDB backend. */
(function () {
  const EVKIN = window.EVKIN || (window.EVKIN = {});
  const M = EVKIN.master;

  EVKIN.STEPS = [
    { id: 'dashboard', label: 'Dashboard', icon: 'gauge', always: true },
    { id: 'pembulatan', label: 'Pembulatan Desimal', icon: 'repeat', always: true, workflow: false },
    { id: 'import', label: 'Import Data', icon: 'upload', workflow: true },
    { id: 'config', label: 'Konfigurasi', icon: 'sliders', workflow: true },
    { id: 'master-sdm', label: 'Master SDM', icon: 'users', workflow: false },
    { id: 'skp-assessment', label: 'Penilaian SKP (Opsional)', icon: 'target', workflow: false },
    { id: 'tukin-annual', label: 'Pengelolaan Tukin', icon: 'calendar-range', workflow: false },
    { id: 'reconcile', label: 'Rekonsiliasi', icon: 'clipboard-check', workflow: false },
    { id: 'harian', label: 'Hitung Harian', icon: 'calculator', workflow: true },
    { id: 'semifinal', label: 'Semi Final', icon: 'shield-check', workflow: true },
    { id: 'rekap', label: 'Rekap SDM', icon: 'users', workflow: true },
    { id: 'final', label: 'Preview Final Absensi (Legacy)', icon: 'badge-check', workflow: false },
    { id: 'final-tukin', label: 'Final Tukin', icon: 'badge-check', workflow: false, always: true },
    { id: 'final-tukin-results', label: 'Hasil Perhitungan', icon: 'table', workflow: false, always: true },
    { id: 'export', label: 'Export Rekap Absensi', icon: 'file-down', workflow: true }
  ];

  function defaultConfig() {
    const master = EVKIN.master || {};
    const holidaysDefault = Array.isArray(master.holidays2026) ? master.holidays2026 : [];
    const jabatanDefault = Array.isArray(master.jabatanDefault) ? master.jabatanDefault : [];
    const jenisDefault = Array.isArray(master.jenisNoPotonganDefault) ? master.jenisNoPotonganDefault : [];
    return {
      saved: false,
      effectiveDaysMode: 'auto',
      effectiveDaysManual: 22,
      holidays: holidaysDefault.map(h => ({ d: h && h.d, n: h && h.n ? h.n : 'Libur' })).filter(h => h && h.d),
      jabatanMaster: jabatanDefault.map(j => ({ nama: j && j.nama ? j.nama : '', metode: j && j.metode === 'durasi' ? 'durasi' : 'reguler' })).filter(j => j && j.nama),
      jamDasar: {
        senKam: { masuk: '07:30', pulang: '16:00' },
        jumat: { masuk: '07:30', pulang: '16:30' }
      },
      flexi: {
        enabled: true,
        masukStart: '07:31', masukEnd: '08:00',
        kompensasiSenKam: '16:30', kompensasiJumat: '17:00'
      },
      keterlambatanTiers: [
        { max: 30, pct: 0.5 }, { max: 60, pct: 1 }, { max: 90, pct: 1.50 }, { max: 120, pct: 2 }, { max: 9999, pct: 3 }
      ],
      pulangCepatTiers: [
        { max: 30, pct: 0.5 }, { max: 60, pct: 1 }, { max: 90, pct: 1.50 }, { max: 120, pct: 2 }, { max: 9999, pct: 3 }
      ],
      minDurasiJam: 7.5,
      potonganTidakLengkap: 3,
      potonganHariKurangPerHari: 3,
      jenisNoPotongan: jenisDefault.slice(),
      leavePeriods: [],
      masterPegawai: []
    };
  }

  function normalizeConfig(rawConfig) {
    const base = defaultConfig();
    const source = rawConfig && typeof rawConfig === 'object' ? rawConfig : {};
    const merged = { ...base, ...source };
    const jamDasar = source.jamDasar || {};
    const senKam = jamDasar.senKam || {};
    const jumat = jamDasar.jumat || {};
    const flexi = source.flexi || {};
    const mergedJam = {
      senKam: { masuk: senKam.masuk || base.jamDasar.senKam.masuk, pulang: senKam.pulang || base.jamDasar.senKam.pulang },
      jumat: { masuk: jumat.masuk || base.jamDasar.jumat.masuk, pulang: jumat.pulang || base.jamDasar.jumat.pulang }
    };
    const mergedFlexi = {
      enabled: typeof flexi.enabled === 'boolean' ? flexi.enabled : base.flexi.enabled,
      masukStart: flexi.masukStart || base.flexi.masukStart,
      masukEnd: flexi.masukEnd || base.flexi.masukEnd,
      kompensasiSenKam: flexi.kompensasiSenKam || base.flexi.kompensasiSenKam,
      kompensasiJumat: flexi.kompensasiJumat || base.flexi.kompensasiJumat
    };
    const normalizeTierList = (list, fallback) => {
      const arr = Array.isArray(list) ? list : fallback;
      return arr.map((tier) => ({ max: Number(tier && tier.max) || 0, pct: Number(tier && tier.pct) || 0 })).filter(tier => Number.isFinite(tier.max) && Number.isFinite(tier.pct));
    };
    return {
      ...merged,
      saved: !!source.saved,
      effectiveDaysMode: source.effectiveDaysMode === 'manual' ? 'manual' : 'auto',
      effectiveDaysManual: Number(source.effectiveDaysManual) || base.effectiveDaysManual,
      holidays: Array.isArray(source.holidays) ? source.holidays.map(h => ({ d: String(h && h.d || ''), n: String(h && h.n || 'Libur') })).filter(h => h.d) : base.holidays,
      jabatanMaster: Array.isArray(source.jabatanMaster) ? source.jabatanMaster.map(j => ({ nama: String(j && j.nama || ''), metode: j && j.metode === 'durasi' ? 'durasi' : 'reguler' })).filter(j => j.nama) : base.jabatanMaster,
      jamDasar: mergedJam,
      flexi: mergedFlexi,
      keterlambatanTiers: normalizeTierList(source.keterlambatanTiers, base.keterlambatanTiers),
      pulangCepatTiers: normalizeTierList(source.pulangCepatTiers, base.pulangCepatTiers),
      minDurasiJam: Number(source.minDurasiJam) || base.minDurasiJam,
      potonganTidakLengkap: source.potonganTidakLengkap != null && source.potonganTidakLengkap !== '' && Number.isFinite(Number(source.potonganTidakLengkap)) && Number(source.potonganTidakLengkap) >= 0 ? Number(source.potonganTidakLengkap) : base.potonganTidakLengkap,
      potonganHariKurangPerHari: source.potonganHariKurangPerHari != null && source.potonganHariKurangPerHari !== '' && Number.isFinite(Number(source.potonganHariKurangPerHari)) && Number(source.potonganHariKurangPerHari) >= 0 ? Number(source.potonganHariKurangPerHari) : base.potonganHariKurangPerHari,
      jenisNoPotongan: Array.isArray(source.jenisNoPotongan) ? source.jenisNoPotongan.map(String).filter(Boolean) : base.jenisNoPotongan,
      leavePeriods: Array.isArray(source.leavePeriods) ? source.leavePeriods : base.leavePeriods,
      masterPegawai: Array.isArray(source.masterPegawai) ? source.masterPegawai : base.masterPegawai
    };
  }

  const state = EVKIN.state = {
    rawHeaders: [],
    raw: [],
    data: [],
    importStats: null,
    importMode: 'standard',
    config: normalizeConfig(defaultConfig()),
    daily: [],
    harianPage: 0,
    rekap: [],
    rekapPage: 0,
    semifinalVerified: false,
    semifinal: [],
    semifinalPage: 0,
    final: [],
    finalPage: 0,
    finalSKP: { enabled: false, rows: [] },
    finalKombinasi: { enabled: false, useApp: true, useEabsensi: false, useEsdm: false, rows: [] },
    finalCombinedSKP: {
      useApp: true,
      useEabsensi: false,
      useEsdm: false,
      skpRows: [],
      eabsensiRows: [],
      esdmRows: [],
      combinedRows: [],
      absensiProcessed: false
    },
    reconciliationReviewed: false,
    auditLog: [],
    month: '',
    timeMode: 'original',
    workflowMeta: {workflow_id:'default', periode:null, row_counts:{}, total_row_counts:{}},
    workflowLoaded: {},
    workflowPages: {}
  };
  EVKIN.defaultConfig = defaultConfig;
  EVKIN.normalizeConfig = normalizeConfig;

  EVKIN.getMasterPegawaiList = function () {
    const rows = Array.isArray(state.config && state.config.masterPegawai) ? state.config.masterPegawai : [];
    return rows;
  };

  EVKIN.assertDailyCoverage = function () {
    if (state.workflowLoadError) throw new Error(state.workflowLoadError);
    const expected = state.data.filter(r => !r.deleted && r.tanggal && r.nip);
    // `_id` is generated again when canonical raw rows are hydrated after a
    // refresh.  It is therefore not a durable identity for comparing those
    // rows with persisted daily results.  Use the canonical business grain
    // instead; the multiset counter below still detects duplicate/missing
    // NIP-date rows correctly.
    const key = r => String(r.nip || '').trim() + '|' +
      String((r.tanggal && r.tanggal.key) || r.tanggalKey || '').trim();
    const remaining = new Map();
    expected.forEach(r => remaining.set(key(r), (remaining.get(key(r)) || 0) + 1));
    for (const row of state.daily) {
      const count = remaining.get(key(row)) || 0;
      if (!count) throw new Error('Hasil Harian tidak cocok dengan data impor. Hitung ulang seluruh data.');
      remaining.set(key(row), count - 1);
    }
    const missing = [...remaining.values()].reduce((a,b) => a+b,0);
    if (missing) throw new Error(`${missing} record impor belum dihitung Harian. Hitung ulang seluruh data sebelum verifikasi/Rekap.`);
    return {expected:expected.length, calculated:state.daily.length};
  };

  // ---- Persistensi (DuckDB master-only) ----
  EVKIN.save = async function () {
    try {
      if (EVKIN.exportBackend && EVKIN.exportBackend.enabled) {
        if (state.workflowMeta && ['IMPORTING', 'ERROR'].includes(state.workflowMeta.status)) {
          console.warn('Sinkronisasi turunan dilewati karena impor canonical belum berhasil.');
          return false;
        }
        // Saving workflow results must not re-import the entire canonical SDM
        // master. Master edits are committed explicitly in the Master SDM module.
          await EVKIN.syncBackendWorkflow(undefined, {derivedOnly:true}).catch((err) => {
          console.warn('Gagal sinkronisasi state ke DuckDB master:', err);
          throw err;
        });
        return true;
      }
      console.warn('DuckDB master-only mode: backend tidak aktif. State hanya ada di memory browser.' );
      return false;
    } catch (e) { console.warn('Gagal simpan state ke master database', e); return false; }
  };

  function finalRows(rows) {
    return (rows || []).map((row) => ({
      ...row,
      tukin: row.tukin ?? row.tukinbruto ?? row.tukin_gross,
      komponen: row.komponen ?? row.komponenKehadiran,
      potPct: row.potPct ?? row.potonganKehadiranPct,
      nominal: row.nominal ?? row.nominalPotonganKehadiran,
      finalDibayar: row.finalDibayar ?? row.final_tukin_dibayar
    }));
  }

  function refreshLoadedImportStats() {
    const loadedMonths = [...new Set(state.data.filter(row => row && row.tanggal && !row.deleted)
      .map(row => row.tanggal.monthKey).filter(Boolean))].sort();
    const totalRecord = Number(state.importStats && state.importStats.totalRecord ||
      state.workflowMeta.total_row_counts.processed_data || state.data.length ||
      state.workflowMeta.total_row_counts.raw_import || 0);
    const complete = state.data.length >= totalRecord;
    const savedMonths = Array.isArray(state.importStats && state.importStats.months) ? state.importStats.months : [];
    const months = complete ? loadedMonths : (savedMonths.length ? savedMonths : loadedMonths);
    if (!months.includes(state.month)) state.month = months[0] || state.month || '';
    state.importStats = {...(state.importStats || {}), months,
      totalRecord,
      totalRaw:Number(state.workflowMeta.total_row_counts.raw_import || state.raw.length || 0),
      totalSDM:complete ? new Set(state.data.map(row => row && row.nip).filter(Boolean)).size
        : Number(state.importStats && state.importStats.totalSDM || 0)};
  }

  function assignWorkflowRows(source, rows) {
    const list = Array.isArray(rows) ? rows : [];
    if (source === 'config') {
      const detail = list[0] || {};
      const workflow = detail._workflow || {};
      if (Number(state.workflowMeta.total_row_counts.final_absensi || 0) > 0) {
        if (workflow.finalSKP) state.finalSKP = {...state.finalSKP, ...workflow.finalSKP};
        if (workflow.finalKombinasi) state.finalKombinasi = {...state.finalKombinasi, ...workflow.finalKombinasi};
        if (workflow.finalCombinedSKP) state.finalCombinedSKP = {...state.finalCombinedSKP, ...workflow.finalCombinedSKP};
      }
    } else if (source === 'raw_import') {
      state.raw = list;
      state.rawHeaders = list.length ? Object.keys(list[0]) : [];
    } else if (source === 'processed_data') {
      state.data = list.map(row => typeof row.record === 'string' ? JSON.parse(row.record) : row.record);
      refreshLoadedImportStats();
    } else if (source === 'daily_data') state.daily = list;
    else if (source === 'rekap_sdm') state.rekap = list;
    else if (source === 'semifinal') state.semifinal = list;
    else if (source === 'final_absensi') state.final = finalRows(list);
    else if (source === 'final_skp') state.finalSKP.rows = list;
    else if (source === 'final_kombinasi') state.finalKombinasi.rows = list;
    else if (source === 'final_combined_skp') state.finalCombinedSKP.combinedRows = list;
  }

  EVKIN.loadWorkflowSource = async function (source, options = {}) {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return [];
    const all = options.all === true;
    const requestedPage = Math.max(1, Number(options.page) || 1);
    const search = String(options.search || '');
    const filter = String(options.filter || '');
    const sort = String(options.sort || '');
    const cacheKey = `${source}:${all ? 'all' : requestedPage}:${options.periode || ''}:${search}:${filter}:${sort}`;
    if (!options.force && state.workflowLoaded[cacheKey]) return state.workflowLoaded[cacheKey];
    const loadPromise = (async () => {
      const perPage = all ? 1000 : Math.min(100, Number(options.perPage) || 100);
      let page = all ? 1 : requestedPage, totalPages = 1;
      const rows = [];
      do {
        const url = new URL(`/api/workflow/data/${encodeURIComponent(source)}`, EVKIN.exportBackend.apiBase);
        url.searchParams.set('page', String(page));
        url.searchParams.set('per_page', String(perPage));
        url.searchParams.set('workflow_id', state.workflowMeta.workflow_id || 'default');
        if (all) url.searchParams.set('include_detail', 'true');
        if (search) url.searchParams.set('search', search);
        if (filter) url.searchParams.set('filter', filter);
        if (sort) url.searchParams.set('sort', sort);
        if (options.sortDir) url.searchParams.set('sort_dir', options.sortDir);
        if (options.periode) url.searchParams.set('periode', options.periode);
        const response = await fetch(url.toString(), {cache:'no-store'});
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || `Data ${source} gagal dimuat.`);
        rows.push(...(Array.isArray(result.rows) ? result.rows : []));
        state.workflowPages[source] = {
          page:Number(result.page || page), pageSize:Number(result.page_size || result.per_page || perPage),
          total:Number(result.total || 0), totalPages:Number(result.total_pages || 0),
          search, filter, sort
        };
        totalPages = all ? Number(result.total_pages || 0) : 1;
        page += 1;
      } while (all && page <= totalPages);
      assignWorkflowRows(source, rows);
      return rows;
    })();
    state.workflowLoaded[cacheKey] = loadPromise;
    try { return await loadPromise; }
    catch (error) { delete state.workflowLoaded[cacheKey]; throw error; }
  };

  function workflowErrorMessage(result, fallback) {
    const detail = result && result.detail;
    if (detail && detail.code === 'IMPORT_INCOMPLETE') {
      const expected = Number(detail.expected_source_rows || 0);
      const persisted = Number(detail.persisted_source_rows || 0);
      return `${detail.message || fallback} Tersimpan ${persisted}/${expected} baris. Impor ulang file sumber setelah backend diperbarui.`;
    }
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail.message === 'string') return detail.message;
    if (result && typeof result.message === 'string') return result.message;
    return fallback;
  }

  EVKIN.loadCalculationInput = async function (options = {}) {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return state.data;
    const workflowId = state.workflowMeta.workflow_id || 'default';
    const rows = [];
    let page = 1, totalPages = 1, manifest = null;
    try {
      do {
        const url = new URL('/api/workflow/calculation-input', EVKIN.exportBackend.apiBase);
        url.searchParams.set('workflow_id', workflowId);
        url.searchParams.set('page', String(page));
        url.searchParams.set('page_size', '500');
        const response = await fetch(url.toString(), {cache:'no-store'});
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(workflowErrorMessage(result,
          `Input kalkulasi gagal dimuat pada halaman ${page}.`));
        if (result.workflow_id !== workflowId) throw new Error('Workflow input kalkulasi tidak cocok dengan workflow aktif.');
        manifest = result;
        rows.push(...(Array.isArray(result.rows) ? result.rows : []));
        totalPages = Number(result.total_pages || 0);
        page += 1;
      } while (page <= totalPages);
      const persisted = Number(manifest && manifest.persisted_source_rows || 0);
      if (rows.length !== persisted) throw new Error(`Hydration input kalkulasi tidak lengkap: ${rows.length}/${persisted} row.`);
      const restored = state.importMode === 'wali' && EVKIN.waliImportMod && EVKIN.waliImportMod.restoreRecords
        ? EVKIN.waliImportMod.restoreRecords(rows) : EVKIN.importMod.restoreRecords(rows);
      const expectedCalculation = Number(manifest && manifest.expected_calculation_rows || restored.length);
      if (restored.length !== expectedCalculation) {
        throw new Error(`Normalisasi input kalkulasi tidak lengkap: ${restored.length}/${expectedCalculation} record.`);
      }
      state.raw = rows;
      state.rawHeaders = rows.length ? Object.keys(rows[0]) : [];
      state.data = restored;
      state.workflowMeta.total_row_counts = {...(state.workflowMeta.total_row_counts || {}),
        raw_import:persisted, processed_data:0};
      state.workflowMeta.row_counts = {...(state.workflowMeta.row_counts || {}),
        raw_import:persisted, processed_data:0};
      state.workflowMeta.status = 'ACTIVE';
      state.workflowLoadError = '';
      refreshLoadedImportStats();
      return restored;
    } catch (error) {
      const message = `Tahap CALCULATION_INPUT gagal: ${error.message}`;
      state.workflowLoadError = message;
      if (!options.silent) console.error(message, error);
      throw new Error(message);
    }
  };

  EVKIN.ensureWorkflowDataForModule = async function (moduleId) {
    const requirements = {
      import: [['raw_import', false]],
      harian: [['daily_data', false]],
      semifinal: [['daily_data', false], ['semifinal', false]],
      rekap: [['daily_data', false], ['rekap_sdm', false]],
      final: [['rekap_sdm', true], ['final_absensi', true], ['final_skp', true],
        ['final_kombinasi', true], ['final_combined_skp', true], ['config', true]],
      export: [['final_absensi', false], ['config', false]]
    };
    for (const [source, all] of requirements[moduleId] || []) {
      await EVKIN.loadWorkflowSource(source, {all});
    }
    if (moduleId === 'config' && state.importMode === 'csv_eabsensi') {
      await EVKIN.ensureWorkflowDataForComputation(['processed_data']);
    }
    // Full raw hydration is reserved for the calculation action. Merely opening
    // a module must stay bounded by the ordinary list pagination above. In
    // particular, raw_import list rows are a display projection and must never
    // be passed to restoreRecords() as calculation input.
    if (['harian', 'semifinal', 'rekap'].includes(moduleId) && state.daily.length) {
      try { EVKIN.assertDailyCoverage(); state.dailyComplete = true; }
      catch (_) { state.dailyComplete = false; state.semifinalVerified = false; }
    }
  };

  EVKIN.ensureWorkflowDataForComputation = async function (sources) {
    const currentRows = {processed_data:state.data, raw_import:state.raw, daily_data:state.daily,
      rekap_sdm:state.rekap, semifinal:state.semifinal, final_absensi:state.final};
    const totals = state.workflowMeta && state.workflowMeta.total_row_counts || {};
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled || !EVKIN.exportBackend.apiBase) return;
    const needsCalculationInput = (sources || []).includes('processed_data');
    const expectedCalculation = Number(state.config && state.config._workflow &&
      state.config._workflow.importStats && state.config._workflow.importStats.totalRecord ||
      totals.raw_import || 0);
    if (needsCalculationInput && (state.workflowLoadError || state.data.length < expectedCalculation)) {
      await EVKIN.loadCalculationInput();
    }
    for (const source of sources || []) {
      if (source === 'processed_data') continue;
      const expected = Number(totals[source] || 0);
      if (!expected || (currentRows[source] || []).length >= expected) continue;
      await EVKIN.loadWorkflowSource(source, {all:true, force:true});
    }
  };

  EVKIN.load = async function () {
    try {
      if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) {
        return false;
      }
      const response = await fetch(new URL('/api/workflow/state', EVKIN.exportBackend.apiBase).toString(), {
        method: 'GET',
        cache: 'no-store'
      });
      if (!response.ok) {
        console.warn('Tidak ada state tersimpan di DuckDB:', response.status);
        return false;
      }
      const result = await response.json();
      if (EVKIN.exportBackend && EVKIN.exportBackend.acceptGeneration) {
        if (!EVKIN.exportBackend.acceptGeneration(result.reset_generation)) {
          throw new Error('Backend tidak mengirim reset generation yang valid.');
        }
        EVKIN.exportBackend.staleNoticeShown = false;
      }
      const data = result && result.data ? result.data : {};
      const meta = result && result.meta ? result.meta : {};

      const masterCache = state.config && state.config.masterPegawai || [];
      if (data.config !== undefined && data.config !== null) {
        state.config = typeof data.config === 'string' ? JSON.parse(data.config) : data.config;
      } else { state.config = defaultConfig(); }
      state.config = normalizeConfig(state.config || defaultConfig());
      state.config.masterPegawai = masterCache;
      state.config = normalizeConfig(state.config);
      const workflow = state.config._workflow || {};
      const legacyCounts = {};
      for (const [source, rows] of Object.entries(data)) if (Array.isArray(rows)) legacyCounts[source] = rows.length;
      const metadataCounts = meta.total_row_counts && Object.keys(meta.total_row_counts).length
        ? meta.total_row_counts : (meta.row_counts && Object.keys(meta.row_counts).length ? meta.row_counts : legacyCounts);
      state.workflowMeta = {
        workflow_id: meta.workflow_id || 'default', periode: meta.periode || workflow.month || null,
        status: meta.status || 'EMPTY', row_counts: meta.row_counts || {},
        total_row_counts: metadataCounts
      };
      state.workflowLoaded = {};
      state.workflowPages = {};
      state.rawHeaders = []; state.raw = []; state.data = []; state.daily = [];
      state.rekap = []; state.semifinal = []; state.final = [];
      state.workflowLoadError = '';
      const months = Array.isArray(workflow.importStats && workflow.importStats.months)
        ? workflow.importStats.months.slice() : (workflow.month ? [workflow.month] : []);
      state.month = months.includes(workflow.month) ? workflow.month : (meta.periode || months[0] || '');
      state.timeMode = workflow.timeMode || 'original';
      state.importMode = workflow.importMode || 'standard';
      const totals = state.workflowMeta.total_row_counts;
      state.importStats = {...(workflow.importStats || {}), months,
        totalRecord:Number(workflow.importStats && workflow.importStats.totalRecord || totals.raw_import || 0),
        totalRaw:Number(workflow.importStats && workflow.importStats.totalRaw || totals.raw_import || 0)};
      state.semifinalVerified = workflow.semifinalVerified === true && Number(totals.daily_data || 0) > 0;
      if (EVKIN.exportBackend && EVKIN.exportBackend.getWorkflowFlags && state.month) {
        try {
          const flags = await EVKIN.exportBackend.getWorkflowFlags(state.month);
          state.semifinalVerified = !!(flags.flags && flags.flags['semifinal:' + state.month] && flags.flags['semifinal:' + state.month].verified);
        } catch (error) {
          console.warn('Status verifikasi server tidak dapat dimuat:', error);
          state.semifinalVerified = false;
        }
      }
      state.dailyComplete = Number(totals.daily_data || 0) > 0 &&
        Number(totals.daily_data || 0) === Number(state.importStats.totalRecord || 0);
      const hasFinal = Number(totals.final_absensi || 0) > 0;
      state.finalSKP = { enabled: false, rows: [], ...(hasFinal ? workflow.finalSKP : {}) };
      state.finalSKP.rows = [];
      state.finalKombinasi = { enabled: false, useApp: true, useEabsensi: false, useEsdm: false,
        rows: [], ...(hasFinal ? workflow.finalKombinasi : {}) };
      state.finalKombinasi.rows = [];
      state.finalCombinedSKP = { useApp: true, useEabsensi: false, useEsdm: false,
        skpRows: [], eabsensiRows: [], esdmRows: [], combinedRows: [],
        absensiProcessed: false, ...(hasFinal ? workflow.finalCombinedSKP : {}) };
      state.finalCombinedSKP.skpRows = []; state.finalCombinedSKP.eabsensiRows = [];
      state.finalCombinedSKP.esdmRows = []; state.finalCombinedSKP.combinedRows = [];
      // Rolling-upgrade compatibility: an already-running older backend can
      // still return the former full payload until it is restarted.
      for (const source of ['raw_import','processed_data','daily_data','rekap_sdm','semifinal',
        'final_absensi','final_skp','final_kombinasi','final_combined_skp']) {
        if (!Array.isArray(data[source])) continue;
        assignWorkflowRows(source, data[source]);
        const ready = Promise.resolve(data[source]);
        state.workflowLoaded[`${source}:all:`] = ready;
        state.workflowLoaded[`${source}:preview:`] = ready;
      }
      if (hasFinal) {
        if (workflow.finalSKP) state.finalSKP = {...state.finalSKP, ...workflow.finalSKP};
        if (workflow.finalKombinasi) state.finalKombinasi = {...state.finalKombinasi, ...workflow.finalKombinasi};
        if (workflow.finalCombinedSKP) state.finalCombinedSKP = {...state.finalCombinedSKP, ...workflow.finalCombinedSKP};
      }
      if (!state.data.length && state.raw.length) {
        state.data = state.importMode === 'wali' && EVKIN.waliImportMod
          ? EVKIN.waliImportMod.restoreRecords(state.raw) : EVKIN.importMod.restoreRecords(state.raw);
        refreshLoadedImportStats();
      }
      if (state.daily.length) {
        try { EVKIN.assertDailyCoverage(); state.dailyComplete = true; }
        catch (_) { state.dailyComplete = false; state.semifinalVerified = false; }
      }
      if (!state.config) state.config = defaultConfig();
      state.config = normalizeConfig(state.config);
      return true;
    } catch (e) { state.workflowLoadError = 'Data workflow gagal dimuat: ' + e.message; console.warn('Gagal baca state dari master database', e); return false; }
  };

  EVKIN.previewResetProfile = async function (profile) {
    const response = await fetch(new URL(`/api/admin/module-reset/${encodeURIComponent(profile)}/preview`, EVKIN.exportBackend.apiBase), {cache:'no-store'});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || 'Preview reset gagal.');
    return result;
  };

  EVKIN.executeResetProfile = async function (profile, preview, details = {}) {
    const response = await fetch(new URL(`/api/admin/module-reset/${encodeURIComponent(profile)}`, EVKIN.exportBackend.apiBase), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        preview_token:preview.preview_token, confirmation:preview.confirmation,
        actor:details.actor || 'EVKIN_LOCAL_UI', reason:details.reason || `Reset ${profile}`
      })
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || 'Reset gagal. Data aplikasi tetap dipertahankan.');
    return result;
  };

  EVKIN.requestResetProfile = async function (profile, details = {}) {
    const preview = await EVKIN.previewResetProfile(profile);
    return EVKIN.executeResetProfile(profile, preview, details);
  };

  EVKIN.resetAll = async function () {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) {
      throw new Error('Aktifkan backend sebelum mereset workflow.');
    }
    await EVKIN.requestResetProfile('workflow_results', {reason:'Reset hasil workflow dari UI'});
    if (!await EVKIN.load()) throw new Error('Reset selesai, tetapi pemuatan ulang gagal. Muat ulang halaman.');
    state.harianPage = state.rekapPage = state.semifinalPage = state.finalPage = 0;
  };

  EVKIN.deleteAbsensiWorkflow = async function () {
    if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) throw new Error('Aktifkan backend terlebih dahulu.');
    if (EVKIN.deletingAbsensi || (EVKIN.harianMod && EVKIN.harianMod.processing)) {
      throw new Error('Tunggu proses workflow selesai sebelum menghapus data.');
    }
    EVKIN.deletingAbsensi = true;
    try {
      // Finish previously queued writes so they cannot restore data after deletion.
      await EVKIN.exportBackend.syncQueue;
      const result = await EVKIN.requestResetProfile('workflow', {reason:'Reset seluruh workflow dari UI'});
      if (!await EVKIN.load()) throw new Error('Data backend sudah dihapus. Muat ulang halaman karena pemuatan state gagal.');
      state.harianPage = state.rekapPage = state.semifinalPage = state.finalPage = 0;
      state.reconciliationReviewed = false;
      if (EVKIN.db && EVKIN.db.replaceStore) {
        await Promise.all(['workflow_config','raw_import','daily_data','rekap_sdm','final_absensi',
          'final_skp','final_kombinasi','final_combined_skp'].map(name => EVKIN.db.replaceStore(name, [])))
          .catch(() => { throw new Error('Data backend sudah dihapus, tetapi cache browser gagal dibersihkan. Muat ulang halaman.'); });
      }
      return result;
    } finally { EVKIN.deletingAbsensi = false; }
  };

  // ---- Status penyelesaian tiap tahap ----
  function workflowCount(source) {
    const scoped = state.workflowMeta && state.workflowMeta.row_counts || {};
    if (Object.prototype.hasOwnProperty.call(scoped, source)) return Number(scoped[source] || 0);
    const totals = state.workflowMeta && state.workflowMeta.total_row_counts || {};
    return Number(totals[source] || 0);
  }

  EVKIN.stepDone = function (id) {
    switch (id) {
      case 'import': return state.data.length > 0 || workflowCount('processed_data') > 0 || workflowCount('raw_import') > 0;
      case 'config': return !!state.config.saved;
      case 'master-sdm': return Array.isArray(state.config.masterPegawai) && state.config.masterPegawai.length > 0;
      case 'reconcile': return !!state.reconciliationReviewed;
      case 'harian': return (state.daily.length > 0 || workflowCount('daily_data') > 0) && state.dailyComplete !== false;
      case 'rekap': return state.rekap.length > 0 || workflowCount('rekap_sdm') > 0;
      case 'semifinal': return !!state.semifinalVerified;
      case 'final': return state.final.length > 0 || workflowCount('final_absensi') > 0;
      case 'export': return state.final.length > 0 || workflowCount('final_absensi') > 0;
      default: return false;
    }
  };

  EVKIN.stepUnlocked = function (id) {
    switch (id) {
      case 'dashboard': return true;
      case 'pembulatan': return true;
      case 'import': return true;
      case 'config': return EVKIN.stepDone('import');
      case 'master-sdm': return true;
      case 'skp-assessment': return true;
      case 'final-tukin': return true; // Backend validates the selected period/mode and verified Rekap SDM.
      case 'final-tukin-results': return true; // Persisted snapshots remain accessible after restart.
      case 'tukin-annual': return true;
      case 'reconcile': return true;
      case 'harian': return EVKIN.stepDone('import') && EVKIN.stepDone('config');
      case 'semifinal': return EVKIN.stepDone('harian');
      // Navigation remains available for reviewing results and recovery guidance.
      // Calculation is guarded separately by rekapMod.process.
      case 'rekap': return true;
      case 'final': return EVKIN.stepDone('semifinal') && EVKIN.stepDone('rekap');
      case 'export': return (EVKIN.stepUnlocked('final') && EVKIN.stepDone('final')) || EVKIN.stepDone('rekap');
      default: return false;
    }
  };

  // Invalidasi hasil tahap-tahap setelah perubahan hulu
  EVKIN.invalidateFrom = function (id, options = {}) {
    const order = ['import', 'config', 'harian', 'semifinal', 'rekap', 'final'];
    const idx = order.indexOf(id);
    const clr = (from) => {
      if (from <= order.indexOf('harian')) { state.daily = []; state.dailyComplete = false; state.harianPage = 0; }
      if (from <= order.indexOf('semifinal')) { state.semifinalVerified = false; state.semifinal = []; state.semifinalPage = 0; }
      if (from <= order.indexOf('rekap')) { state.rekap = []; state.rekapPage = 0; }
      if (from <= order.indexOf('final')) {
        state.final = []; state.finalPage = 0;
        state.finalSKP = { enabled: false, rows: [] };
        state.finalKombinasi = { enabled: false, useApp: true, useEabsensi: false, useEsdm: false, rows: [] };
        state.finalCombinedSKP = { useApp: true, useEabsensi: false, useEsdm: false, skpRows: [], eabsensiRows: [], esdmRows: [], combinedRows: [], absensiProcessed: false };
      }
    };
    // saat suatu tahap berubah, hapus tahap setelahnya
    clr(idx + 1);
    if (options.save !== false) EVKIN.save();
  };

})();

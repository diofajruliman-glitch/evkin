const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class FakeFormData {
  constructor() { this.values = new Map(); }
  append(key, value) { this.values.set(key, value); }
}

class FakeElement {
  constructor() {
    this.innerHTML = '';
    this.textContent = '';
    this.files = [];
    this.checked = false;
    this.disabled = false;
    this.children = new Map();
  }
  querySelector(selector) {
    if (!this.children.has(selector)) this.children.set(selector, new FakeElement());
    return this.children.get(selector);
  }
  setAttribute() {}
  addEventListener() {}
}

async function main() {
  const storage = new Map();
  const calls = [];
  let poll = 0;
  let multiPeriodReady = false;
  let currentSourceFormat = 'standard';
  let duplicateUpload = false;
  const response = body => ({ok:true,status:200,json:async()=>body});
  const apiFetch = async (url, options={}) => {
    const path = new URL(url).pathname;
    calls.push({path, method:options.method || 'GET', body:options.body});
    if (path.endsWith('/authority')) return response({
      authority:'IMPORT_BACKEND_V1',
      routes:{standard:'IMPORT_BACKEND_V1',wali:'IMPORT_JS_LEGACY',csv_eabsensi:'CSV_EABSENSI_SPECIAL'},
      csv_eabsensi_enabled:true
    });
    if (path.endsWith('/upload')) {
      poll = 0;
      currentSourceFormat = options.body.values.get('source_format');
      multiPeriodReady = options.body.values.get('file').name === 'multi-period.csv';
      duplicateUpload = options.body.values.get('file').name === 'duplicate.csv';
      if (duplicateUpload) return response({
        import_id:'duplicate-attempt',status:'ALREADY_COMMITTED',duplicate_of_import_id:'imp-1',
        authoritative_import:{import_id:'imp-1',status:'COMMITTED',source_format:'csv_eabsensi',
          period:'2026-08',pair_cross_day:false,total_rows:2,valid_rows:2,invalid_rows:0,timings:{source_rows:2,unique_nip:1}}
      });
      return response({import_id:'imp-1',status:'STAGED'});
    }
    if (path.endsWith('/duplicate-attempt/use-committed')) return response({
      import_id:'imp-1',status:'COMMITTED',source_format:'csv_eabsensi',period:'2026-08',
      valid_rows:2,total_rows:2,invalid_rows:0,timings:{source_rows:2,unique_nip:1}
    });
    if (path.endsWith('/imp-1/commit')) return response({
      import_id:'imp-1',status:'COMMITTED',source_format:currentSourceFormat,period:'2026-08',
      valid_rows:2,total_rows:2,invalid_rows:0,timings:{source_rows:2,unique_nip:1,canonical_rows:1}
    });
    if (path.endsWith('/imp-1/preview')) return response({rows:[{
      nip:'NIP-1',nama:'Pegawai UAT',tanggal:'2026-08-01',jam_masuk:'07:00:00',
      jam_pulang:'16:00:00',error_codes:[]
    }]});
    if (path.endsWith('/imp-1/errors')) return response({rows:[],total:0});
    if (path.endsWith('/imp-1')) {
      poll += 1;
      return response(poll === 1 ? {import_id:'imp-1',status:'NORMALIZING',progress:50} : {
        import_id:'imp-1',status:'READY',progress:100,
        period:multiPeriodReady ? null : '2026-08',
        periods:multiPeriodReady ? ['2026-08','2026-09'] : ['2026-08'],
        source_format:currentSourceFormat
      });
    }
    throw new Error('Unexpected ' + path);
  };
  let legacyRenderCalls = 0;
  let legacyProcessCalls = 0;
  const EVKIN = {
    importMod:{
      render(){ legacyRenderCalls += 1; },
      processFile(file, progress) {
        legacyProcessCalls += 1;
        if (progress) progress({progress:100});
        return Promise.resolve({totalRecord:2, fileName:file.name});
      }
    },
    exportBackend:{apiBase:'http://localhost:8000',apiFetch},
    state:{month:'2026-08',config:{holidays:[],masterPegawai:[{nip:'private'}]}},
    util:{esc:value=>String(value == null ? '' : value)},
  };
  const sandbox = {
    window:{EVKIN,location:{origin:'http://localhost:8000'}},
    document:{addEventListener(){}},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    FormData:FakeFormData, URL, setTimeout, clearTimeout, setInterval, clearInterval,
    prompt:()=>'', console
  };
  vm.runInNewContext(fs.readFileSync('assets/js/import-backend.js','utf8'), sandbox);
  assert.equal(EVKIN.importBackend.authority, 'IMPORT_JS_LEGACY');
  EVKIN.importMod.render({});
  assert.equal(legacyRenderCalls, 1, 'legacy render remains default');

  await EVKIN.importBackend.refreshAuthority();
  assert.equal(EVKIN.importBackend.authority, 'IMPORT_BACKEND_V1');
  assert.equal(EVKIN.importBackend.routes.wali, 'IMPORT_JS_LEGACY');
  assert.equal(EVKIN.importBackend.csvEabsensiEnabled, true);
  assert.equal(storage.get('evkin.importAuthority'), 'IMPORT_BACKEND_V1');
  let legacyReturnTarget = '';
  EVKIN.go = target => { legacyReturnTarget = target; };
  EVKIN.importBackend.setMode(EVKIN.importBackend.modes.WALI_LEGACY);
  EVKIN.importBackend.returnToLegacyExcel();
  assert.equal(EVKIN.importBackend.mode, EVKIN.importBackend.modes.LEGACY_EXCEL,
    'Wali completion must select the complete legacy Import renderer');
  assert.equal(legacyReturnTarget, 'import',
    'Wali completion must navigate back to the full legacy Import screen');
  const fakeFile = {name:'absence.csv'};
  const created = await EVKIN.importBackend.upload(fakeFile, {operator:'uat',sourceFormat:'standard',period:'2026-08'});
  assert.equal(created.import_id, 'imp-1');
  const uploadCall = calls.find(item => item.path.endsWith('/upload'));
  assert.ok(uploadCall.body instanceof FakeFormData);
  assert.equal(uploadCall.body.values.get('file'), fakeFile);
  assert.ok(!uploadCall.body.values.get('workflow_config_json').includes('masterPegawai'));

  const ready = await EVKIN.importBackend.wait('imp-1', null, 2000);
  assert.equal(ready.status, 'READY');
  const committed = await EVKIN.importBackend.commit('imp-1','2026-08',false);
  assert.equal(committed.status, 'COMMITTED');
  assert.equal(calls.filter(item => item.path.endsWith('/upload')).length, 1);
  assert.equal(calls.filter(item => item.path.endsWith('/commit')).length, 1);
  await assert.rejects(
    () => EVKIN.importBackend.upload({name:'wali.xlsx'}, {operator:'uat',sourceFormat:'wali'}),
    error => error.code === 'WALI_LEGACY_REQUIRED'
  );
  assert.equal(calls.filter(item => item.path.endsWith('/upload')).length, 1, 'Wali must not reach backend upload');
  const apiCallsBeforeExcel = calls.length;
  const excelResult = await EVKIN.importBackend.importLegacyExcel({name:'absensi.xlsx'}, () => {});
  assert.equal(excelResult.totalRecord, 2);
  assert.equal(legacyProcessCalls, 1, 'Excel Existing must call the frozen legacy processFile');
  assert.equal(calls.length, apiCallsBeforeExcel, 'Excel Existing must not call any backend import endpoint');
  await assert.rejects(
    () => EVKIN.importBackend.importLegacyExcel({name:'absensi.csv'}),
    error => error.code === 'LEGACY_EXCEL_FILE_REQUIRED'
  );
  await assert.rejects(
    () => EVKIN.importBackend.upload({name:'not-eabsensi.xlsx'}, {operator:'uat',sourceFormat:'csv_eabsensi'}),
    error => error.code === 'CSV_EABSENSI_REQUIRES_CSV'
  );
  assert.equal(calls.filter(item => item.path.endsWith('/upload')).length, 1, 'Non-CSV eAbsensi upload must fail closed');
  const special = await EVKIN.importBackend.upload({name:'eabsensi.csv'}, {
    operator:'uat',sourceFormat:'csv_eabsensi'
  });
  assert.equal(special.import_id, 'imp-1');
  const specialCall = calls.filter(item => item.path.endsWith('/upload'))[1];
  assert.equal(specialCall.body.values.get('source_format'), 'csv_eabsensi');
  assert.equal(specialCall.body.values.get('pair_cross_day'), 'false');
  const autoCommitted = await EVKIN.importBackend.processCsvEabsensi(
    {name:'eabsensi-cross-day.csv'}, {operator:'uat',crossDay:true}, () => {}
  );
  assert.equal(autoCommitted.status, 'COMMITTED');
  const crossDayCall = calls.filter(item => item.path.endsWith('/upload'))[2];
  assert.equal(crossDayCall.body.values.get('pair_cross_day'), 'true');
  let multiPeriodBatch;
  await assert.rejects(
    () => EVKIN.importBackend.processCsvEabsensi(
      {name:'multi-period.csv'}, {operator:'uat'}, () => {}
    ),
    error => {
      assert.equal(error.code, 'MULTI_PERIOD_REQUIRES_SELECTION');
      multiPeriodBatch = error.batch;
      assert.deepEqual(Array.from(multiPeriodBatch.periods), ['2026-08','2026-09']);
      return true;
    }
  );
  const selectedPeriodCommit = await EVKIN.importBackend.commit(
    multiPeriodBatch.import_id, '2026-09', false
  );
  assert.equal(selectedPeriodCommit.status, 'COMMITTED');
  assert.equal(calls.filter(item => item.path.endsWith('/upload'))[3].body.values.get('source_format'), 'csv_eabsensi');

  currentSourceFormat = 'csv_eabsensi';
  multiPeriodReady = false;
  const canonicalRows = [{nip:'NIP-1'}, {nip:'NIP-1'}];
  let loadCalls = 0;
  let hydrationCalls = 0;
  let continueTarget = '';
  EVKIN.load = async () => {
    loadCalls += 1;
    EVKIN.state.workflowMeta = {
      workflow_id:'backend-import-imp-1',total_row_counts:{raw_import:canonicalRows.length}
    };
    EVKIN.state.importMode = currentSourceFormat;
    EVKIN.state.month = '2026-08';
    EVKIN.state.raw = [];
    EVKIN.state.data = [];
    return true;
  };
  EVKIN.loadCalculationInput = async () => {
    hydrationCalls += 1;
    EVKIN.state.raw = canonicalRows;
    EVKIN.state.data = canonicalRows;
    return canonicalRows;
  };
  EVKIN.go = target => { continueTarget = target; };
  EVKIN.importBackend.setMode(EVKIN.importBackend.modes.ROUTER);
  const importPanel = new FakeElement();
  EVKIN.importMod.render(importPanel);
  importPanel.querySelector('#eabsensiCsvFile').files = [{name:'eabsensi.csv'}];
  await importPanel.querySelector('#eabsensiCsvUpload').onclick();
  const resultHtml = importPanel.querySelector('#backendImportResult').innerHTML;
  assert.equal(loadCalls, 1, 'A committed CSV must reload its canonical workflow');
  assert.equal(hydrationCalls, 1,
    'A committed CSV must hydrate complete calculation input: ' +
      importPanel.querySelector('#eabsensiCsvProgress').textContent);
  assert.match(resultHtml, /Lanjut ke Konfigurasi/);
  await importPanel.querySelector('#eabsensiContinue').onclick();
  assert.equal(continueTarget, 'config', 'The CSV result must open Config before Harian');

  const duplicatePanel = new FakeElement();
  EVKIN.importMod.render(duplicatePanel);
  duplicatePanel.querySelector('#eabsensiCsvFile').files = [{name:'duplicate.csv'}];
  const uploadsBeforeDuplicate = calls.filter(item => item.path.endsWith('/upload')).length;
  await Promise.all([
    duplicatePanel.querySelector('#eabsensiCsvUpload').onclick(),
    duplicatePanel.querySelector('#eabsensiCsvUpload').onclick()
  ]);
  assert.equal(calls.filter(item => item.path.endsWith('/upload')).length, uploadsBeforeDuplicate + 1,
    'Double-click must create only one upload request');
  assert.match(duplicatePanel.querySelector('#backendImportResult').innerHTML, /sudah pernah berhasil diimport/);
  assert.match(duplicatePanel.querySelector('#backendImportResult').innerHTML, /Gunakan Hasil Import Sebelumnya/);
  assert.match(duplicatePanel.querySelector('#eabsensiCsvProgress').innerHTML, /Hasil sebelumnya tersedia/);
  await duplicatePanel.querySelector('#eabsensiUsePrevious').onclick();
  assert.ok(calls.some(item => item.path.endsWith('/duplicate-attempt/use-committed')),
    'Use Previous Result must verify the authoritative committed batch');

  const excelPanel = new FakeElement();
  EVKIN.importBackend.setMode(EVKIN.importBackend.modes.ROUTER);
  EVKIN.importMod.render(excelPanel);
  excelPanel.querySelector('#excelBackendFile').files = [{name:'absensi_sdm.xlsx'}];
  const uploadsBeforeExcel = calls.filter(item => item.path.endsWith('/upload')).length;
  await excelPanel.querySelector('#excelBackendUpload').onclick();
  assert.equal(calls.filter(item => item.path.endsWith('/upload')).length, uploadsBeforeExcel + 1,
    'Excel backend upload must invoke backend upload endpoint');
  const excelUpload = calls.filter(item => item.path.endsWith('/upload')).slice(-1)[0];
  assert.equal(excelUpload.body.values.get('source_format'), 'standard');
  assert.equal(excelUpload.body.values.get('file').name, 'absensi_sdm.xlsx');
  assert.match(excelPanel.querySelector('#backendImportResult').innerHTML, /Preview backend \(maks\. 50 baris\)/);

  excelPanel.querySelector('#backendImportPeriod').value = '2026-08';
  const loadCallsBeforeCommit = loadCalls;
  await excelPanel.querySelector('#backendImportCommit').onclick();
  assert.equal(loadCalls, loadCallsBeforeCommit + 1, 'Standard commit must reload canonical workflow');
  assert.match(excelPanel.querySelector('#backendImportResult').innerHTML, /Lanjut ke Konfigurasi/);

  const source = fs.readFileSync('assets/js/import-backend.js','utf8');
  assert.doesNotMatch(source, /XLSX\.read|FileReader|EVKIN\.state\.raw\s*=/);
  assert.match(source, /Preview backend \(maks\. 50 baris\)/);
  assert.doesNotMatch(source, /backendImportPeriodInput/);
  assert.doesNotMatch(source, /id="backendImportFormat"/);
  assert.match(source, /4\. Advanced \/ Experimental/);
  assert.match(source, /Import Excel Existing/);
  assert.match(source, /JavaScript Legacy \/ Excel Existing/);
  assert.match(source, /return EVKIN\.importMod\.processFile\(file/);
  assert.match(source, /Import CSV eAbsensi/);
  assert.match(source, /id="eabsensiCrossDay"/);
  assert.match(source, /Aktifkan pairing lintas hari \/ shift malam/);
  assert.match(source, /mod\.processCsvEabsensi/);
  assert.match(source, /UPLOADING:'Upload file'/);
  assert.match(source, /VALIDATING:'Validasi struktur\/data'/);
  assert.match(source, /NORMALIZING:'Normalisasi event & pairing absensi'/);
  assert.match(source, /COMMITTING:'Menyimpan ke DuckDB'/);
  assert.match(source, /HYDRATING:'Memuat ulang workflow'/);
  assert.match(source, /mod\.csvProcessing/);
  assert.match(source, /IMPORT ENGINE: Backend Import V1 \/ Standard/);
  assert.match(source, /accept="\.csv,\.parquet"/);
  assert.match(source, /setAttribute\('accept', '\.xlsx,\.xlsm,\.xls'\)/);
  assert.match(source, /function renderLegacyExcel\(el\)/);
  assert.match(source, /legacyRender\(el\)/);
  assert.match(source, /data-testid="legacy-excel-mode-panel"/);
  assert.match(source, /id="openLegacyExcel"/);
  assert.match(source, /Import Absensi Wali\/pairing lintas hari/);
  assert.match(source, /waliPick\.addEventListener\('click'/);
  assert.match(source, /waliInput\.addEventListener\('click'/);
  assert.match(source, /event\.stopImmediatePropagation\(\)/);
  assert.match(source, /mod\.mode === IMPORT_MODES\.LEGACY_EXCEL/);
  assert.match(source, /IMPORT ENGINE: Python Backend \/ CSV_EABSENSI_SPECIAL/);
  assert.match(source, /2\. Import Absensi Wali/);
  assert.match(source, /3\. Import CSV eAbsensi/);
  assert.match(source, /4\. Advanced \/ Experimental/);
  assert.match(source, /sourceFormat:'csv_eabsensi'/);
  assert.match(source, /sourceFormat:'standard'/);
  assert.match(source, /accept="\.csv,text\/csv"/);
  assert.match(source, /eabsensiSummaryCards/);
  for (const label of ['Source Rows', 'Unique NIP', 'Daily Output', 'Complete Pair',
    'IN Only', 'OUT Only', 'Duplicate', 'Invalid Timestamp', 'Cross-Day Pair', 'Rejected']) {
    assert.ok(source.includes(`['${label}'`), `eAbsensi summary should show ${label}`);
  }
  assert.match(source, /Tanggal Pulang/);
  assert.match(source, /Status Pairing/);
  assert.match(source, /renderEabsensiResult\(el, result/);
  assert.match(source, /function hydrateCommittedEabsensiWorkflow\(batch\)/);
  assert.match(source, /await EVKIN\.loadCalculationInput\(\{silent:true\}\)/,
    'A committed CSV workflow must hydrate all canonical calculation input before it is marked ready');
  assert.match(source, /id="eabsensiContinue">Lanjut ke Konfigurasi/);
  assert.match(source, /id="backendImportContinue">Lanjut ke Konfigurasi/);
  assert.equal((source.match(/continueButton\.onclick = \(\) => EVKIN\.go\('config'\)/g) || []).length, 2,
    'Both CSV result surfaces must continue to Config, not bypass it for Harian');
  assert.match(source, /Import Wali dengan JavaScript Legacy/);
  assert.match(source, /await EVKIN\.waliImportMod\.processFile\(file/);
  assert.match(source, /mod\.returnToLegacyExcel\(\);/,
    'Wali router must return to the full legacy Import renderer only after its persisted legacy process resolves');
  assert.match(source, /Pilih periode\.\.\./);
  assert.match(source, /Pilih periode secara eksplisit sebelum commit/);
  assert.match(source, /await renderEabsensiResult\(el, error\.batch/,
    'Multi-period eAbsensi must render its original ready batch for period selection and commit');
  assert.match(source, /result\.source_format === 'csv_eabsensi'/,
    'Manual eAbsensi commit must reload its canonical workflow');
  assert.match(source, /id="backendImportContinue"/);
  assert.match(source, /Preview gagal dimuat/);
  assert.match(source, /Contoh error gagal dimuat/);
  assert.match(source, /Authority import backend tidak dapat diverifikasi/);
  assert.match(source, /showImportModal/);
  assert.match(source, /updateImportModal/);
  assert.match(source, /closeImportModal/);
  assert.match(source, /import-modal-dialog/);
  assert.match(source, /import-modal-progress-bar/);
  assert.match(source, /import-modal-stepper/);

  const exportSource = fs.readFileSync('assets/js/export-backend.js','utf8');
  assert.match(exportSource, /function apiErrorInfo\(payload, fallback\)/);
  assert.match(exportSource, /error\.code = info\.code/);
  assert.doesNotMatch(exportSource, /new Error\(detail \|\| `Sinkronisasi tabel/);
  const launcherSource = fs.readFileSync('start_backend.ps1','utf8');
  assert.match(launcherSource, /\$env:EVKIN_CSV_EABSENSI_SPECIAL_ENABLED = 'true'/,
    'The standard Windows launcher must force-enable the eAbsensi CSV route');
  assert.match(launcherSource, /api\/absence-import\/authority/);
  assert.match(launcherSource, /Import CSV eAbsensi aktif/);
  const htmlSource = fs.readFileSync('index.html','utf8');
  assert.match(htmlSource, /state\.js\?v=20260930-csv-workflow-handoff-v1/,
    'The browser must fetch the CSV workflow hydration change after a refresh');
  assert.match(htmlSource, /import-backend\.js\?v=20260930-csv-workflow-handoff-v1/,
    'The browser must fetch the updated import UI instead of a cached version');
  console.log('Backend import frontend authority/upload/poll/commit: PASS');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

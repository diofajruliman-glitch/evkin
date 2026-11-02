const fs = require('node:fs');
const vm = require('node:vm');
const {performance} = require('node:perf_hooks');

const inputPath = process.argv[2];
const outputPath = process.argv[3];
if (!inputPath || !outputPath) throw new Error('Usage: node real_world_absence_runner.cjs <input.json> <output.json>');
const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'));

function load(path, sandbox) {
  vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox, {filename: path});
}

async function main() {
  const E = {};
  let synced = null;
  const sandbox = {
    window: {EVKIN: E}, console, Date, Math, URL, AbortSignal,
    setTimeout, clearTimeout, confirm: () => true, performance,
    document: {
      addEventListener() {}, getElementById() { return null; },
      body: {appendChild() {}, dataset: {}},
      createElement() { return {classList: {add() {}, remove() {}}, appendChild() {}, remove() {}}; }
    }
  };
  for (const name of ['utils', 'data', 'state', 'calc', 'config', 'import', 'rekap']) {
    load(`assets/js/${name}.js`, sandbox);
  }
  E.ensureWorkflowDataForComputation = async () => {};
  E.invalidateFrom = () => {};
  E.auditMod = {logStage() {}, logDatabaseSync() {}};
  E.db = {saveRekap: async () => {}};
  E.save = () => {};
  E.go = () => {};
  E.toast = () => {};
  E.progress = () => {};

  let finalSource = fs.readFileSync('assets/js/final.js', 'utf8');
  finalSource = finalSource.replace(/\}\)\(\);\s*$/, 'EVKIN.finalMod.__shadow = { recalc };\n})();');
  vm.runInNewContext(finalSource, sandbox, {filename: 'assets/js/final.js'});
  E.exportBackend = {
    enabled: true, apiBase: 'http://127.0.0.1:0',
    syncWorkflow: async payload => { synced = payload; return {counts: {}}; }
  };
  load('assets/js/app.js', sandbox);
  E.progress = () => {};
  E.toast = () => {};

  const timings = {};
  let started = performance.now();
  const records = E.importMod.restoreRecords(input.rows);
  records.forEach((record, index) => { record._id = `rw-${String(index).padStart(6, '0')}`; });
  timings.normalization_ms = performance.now() - started;

  const config = E.normalizeConfig(input.config);
  config.masterPegawai = input.master;
  E.state.config = config;
  E.state.timeMode = input.time_mode || 'original';
  const context = E.calc.buildContext();
  started = performance.now();
  const daily = records.map(record => E.calc.computeRecord(record, context, E.state.timeMode));
  timings.harian_ms = performance.now() - started;

  E.state.daily = daily;
  E.state.semifinalVerified = true;
  started = performance.now();
  const rekap = await E.rekapMod.process({sync: false});
  timings.rekap_ms = performance.now() - started;

  const master = new Map(input.master.map(row => [String(row.nip), row]));
  started = performance.now();
  const finalAbsensi = rekap.map(row => {
    const person = master.get(String(row.nip));
    const gross = person && person.tukin !== null && person.tukin !== undefined && person.tukin !== '' ? person.tukin : null;
    return E.finalMod.__shadow.recalc({
      nip: row.nip, nama: row.nama, jabatan: row.jabatan, bulan: row.bulan,
      tukin: gross, potPct: row.total
    });
  });
  timings.final_ms = performance.now() - started;

  E.state.final = finalAbsensi;
  E.state.finalSKP = {enabled: true, rows: finalAbsensi.map(row => ({nip: row.nip, skpPct: 0}))};
  E.state.finalKombinasi = {enabled: false, useApp: true, useEabsensi: false, useEsdm: false, rows: []};
  E.state.finalCombinedSKP = {
    useApp: true, useEabsensi: false, useEsdm: false,
    skpRows: finalAbsensi.map(row => ({nip: row.nip, skpPct: 0})),
    eabsensiRows: [], esdmRows: [], combinedRows: [], absensiProcessed: true
  };
  started = performance.now();
  await E.syncBackendWorkflow(null, {finalOnly: true});
  timings.legacy_sync_ms = performance.now() - started;

  const normalization = records.map(record => ({
    row_id: record._id, nip: record.nip, tanggal: record.tanggal && record.tanggal.key,
    masukRaw: record.masukRaw, pulangRaw: record.pulangRaw,
    masukMin: record.masukMin, pulangMin: record.pulangMin,
    masukNormMin: record.masukNormMin, pulangNormMin: record.pulangNormMin,
    masukNormMinAdd: record.masukNormMinAdd, pulangNormMinAdd: record.pulangNormMinAdd,
    zona: record.zona, jenis: record.jenis, jabatan: record.jabatan,
    crossDay: record.crossDay, issues: record.issues, dupFlag: record.dupFlag
  }));
  const total = Object.values(timings).reduce((sum, value) => sum + value, 0);
  const output = {
    baseline: input.baseline, input_fingerprint: input.input_fingerprint,
    normalization, daily, rekap, final_absensi: finalAbsensi,
    final_skp: synced.final_skp, final_combined: synced.final_combined_skp,
    timings: {...timings, total_ms: total},
    resource: {max_rss_kb: process.resourceUsage().maxRSS}
  };
  fs.writeFileSync(outputPath, JSON.stringify(output), 'utf8');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

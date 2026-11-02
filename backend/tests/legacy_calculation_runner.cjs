const fs = require('node:fs');
const vm = require('node:vm');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function load(path, sandbox) {
  vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox, {filename: path});
}

async function main() {
  const E = {};
  let synced = null;
  const sandbox = {
    window: {EVKIN: E}, console, Date, Math, URL, AbortSignal,
    setTimeout, clearTimeout, confirm: () => true,
    document: {
      addEventListener() {}, getElementById() { return null; },
      body: {appendChild() {}, dataset: {}},
      createElement() { return {classList: {add() {}, remove() {}}, appendChild() {}, remove() {}}; }
    }
  };
  for (const name of ['utils', 'data', 'state', 'calc', 'config', 'rekap', 'import']) {
    load(`assets/js/${name}.js`, sandbox);
  }
  E.ensureWorkflowDataForComputation = async () => {};
  E.invalidateFrom = () => {};
  E.syncBackendWorkflow = async () => {};
  E.auditMod = {logStage() {}, logDatabaseSync() {}};
  E.db = {saveRekap: async () => {}};
  E.save = () => {};
  E.go = () => {};
  E.toast = () => {};
  E.progress = () => {};

  let finalSource = fs.readFileSync('assets/js/final.js', 'utf8');
  finalSource = finalSource.replace(/\}\)\(\);\s*$/, 'EVKIN.finalMod.__parity = { recalc };\n})();');
  vm.runInNewContext(finalSource, sandbox, {filename: 'assets/js/final.js'});
  E.exportBackend = {
    enabled: true,
    apiBase: 'http://127.0.0.1:0',
    syncWorkflow: async payload => { synced = payload; return {counts: {}}; }
  };
  load('assets/js/app.js', sandbox);
  // The runner needs the production payload builder, not browser rendering.
  E.progress = () => {};
  E.toast = () => {};

  const output = [];
  for (const item of fixture.cases) {
    const config = E.normalizeConfig(JSON.parse(JSON.stringify(fixture.config)));
    config.masterPegawai = item.master_missing ? [] : [{nip: item.nip, nama: item.id, jabatan: item.master_job || item.job, tukin: item.gross}];
    E.state.config = config;
    E.state.month = item.date.slice(0, 7);
    E.state.timeMode = item.time_mode || 'original';
    const date = E.util.parseDate(item.date);
    const masuk = E.util.parseTime(item.in);
    const pulang = E.util.parseTime(item.out);
    const zone = item.zone || 'WIB';
    const offset = E.master.zoneOffsetMin[zone] || 0;
    const offsetAdd = E.master.zoneOffsetMinAdd[zone] || 0;
    const wrap = (value, adjustment) => value == null ? null : ((value + adjustment) % 1440 + 1440) % 1440;
    const base = {
      _id: item.id, nip: item.nip, nama: item.id, jabatan: item.job,
      tanggal: date, tanggalPulang: item.cross_day ? E.util.addDays(date, 1).str : '',
      tanggalMasukObj: date, tanggalPulangObj: item.cross_day ? E.util.addDays(date, 1) : date,
      masukMin: masuk, pulangMin: pulang,
      masukNormMin: wrap(masuk, offset), pulangNormMin: wrap(pulang, offset),
      masukNormMinAdd: wrap(masuk, offsetAdd), pulangNormMinAdd: wrap(pulang, offsetAdd),
      zona: zone, provinsi: '', kabupaten: '', jenis: item.kind || '',
      crossDay: !!item.cross_day, sourceFormat: item.source_format || 'standard',
      shiftFlag: item.cross_day ? 'SHIFT LINTAS HARI' : '', issues: item.issues || [], dupFlag: !!item.duplicate
    };
    const records = item.duplicate ? [base, {...base, _id: item.id + '-duplicate'}] : [base];
    const daily = records.map(record => E.calc.computeRecord(record, E.calc.buildContext(), E.state.timeMode));
    E.state.daily = daily;
    E.state.semifinalVerified = true;
    let rekap = null;
    let rekapError = null;
    try {
      rekap = (await E.rekapMod.process({sync: false}))[0] || null;
    } catch (error) {
      rekapError = error.message;
    }
    const attendanceRate = item.final_attendance_rate == null
      ? (rekap && rekap.total)
      : item.final_attendance_rate;
    const finalAbsensi = attendanceRate == null ? null : E.finalMod.__parity.recalc({
      nip: item.nip, nama: item.id, jabatan: item.job, bulan: E.state.month,
      tukin: item.gross, potPct: attendanceRate
    });
    let finalSkp = null;
    let finalCombined = null;
    if (finalAbsensi) {
      E.state.final = [finalAbsensi];
      E.state.finalSKP = {enabled: true, rows: [{nip: item.nip, skpPct: item.skp_rate || 0}]};
      E.state.finalKombinasi = {enabled: false, useApp: true, useEabsensi: false, useEsdm: false, rows: []};
      E.state.finalCombinedSKP = {
        useApp: true, useEabsensi: false, useEsdm: false,
        skpRows: [{nip: item.nip, skpPct: item.skp_rate || 0}],
        eabsensiRows: [], esdmRows: [], combinedRows: [], absensiProcessed: true
      };
      synced = null;
      await E.syncBackendWorkflow(null, {finalOnly: true});
      finalSkp = synced.final_skp[0];
      finalCombined = synced.final_combined_skp[0];
    }
    output.push({
      id: item.id, nip: item.nip,
      normalized_input: {
        date: date && date.key,
        check_in_minutes: masuk,
        check_out_minutes: pulang,
        check_in_effective: E.calc.timesForMode(base, E.state.timeMode).masuk,
        check_out_effective: E.calc.timesForMode(base, E.state.timeMode).pulang,
        zone, time_mode: E.state.timeMode, cross_day: !!item.cross_day
      },
      daily, rekap, rekap_error: rekapError,
      final_input: {gross: item.gross, attendance_rate: attendanceRate,
        skp_rate: item.skp_rate == null ? null : item.skp_rate,
        skp_status: item.skp_status || null},
      final_absensi: finalAbsensi, final_skp: finalSkp, final_combined: finalCombined
    });
  }
  const normalizationCases = (fixture.normalization_cases || []).map(item => {
    const headers = ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Tanggal Pulang', 'Jam Masuk', 'Jam Pulang', 'Zona', 'Provinsi', 'Jenis'];
    const row = [item.nip, item.name, item.job, item.date, item.out_date, item.in, item.out, item.zone, item.province, item.kind];
    const result = E.importMod.processSingleRow(row, headers, E.importMod.detectColumns(headers), {partial: !!item.partial});
    const data = result.dataObj ? {...result.dataObj} : null;
    if (data) delete data._id;
    return {id: item.id, invalid: result.invalid, raw: result.rawObj, normalized: data};
  });
  process.stdout.write(JSON.stringify({contract_version: fixture.contract_version, cases: output, normalization_cases: normalizationCases}));
}

main().catch(error => { console.error(error); process.exitCode = 1; });

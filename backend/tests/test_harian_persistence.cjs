const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const requests = [];
  let responseMode = 'success';
  let forbiddenSerializations = 0;
  const E = {};
  // Exercise the browser's string-size failure without allocating a 500 MB string.
  const guardedJSON = {
    parse: JSON.parse,
    stringify(value, ...args) {
      if ((value && !Array.isArray(value) && Object.hasOwn(value, 'daily_data')) ||
          (Array.isArray(value) && value.length > 1000)) {
        forbiddenSerializations++;
        throw new RangeError('Invalid string length');
      }
      return JSON.stringify(value, ...args);
    }
  };
  const sandbox = {
    window: {EVKIN: E}, console, Date, Math, URL, Headers, Blob, Response,
    AbortSignal, TextEncoder, JSON: guardedJSON, setTimeout, clearTimeout,
    document: {
      addEventListener() {}, getElementById() { return null; },
      body: {appendChild() {}, dataset: {}},
      createElement() {
        return {setAttribute() {}, remove() {},
          classList: {add() {}, remove() {}, contains() { return false; }}};
      }
    },
    fetch: async (url, options = {}) => {
      assert.equal(new URL(String(url)).pathname, '/api/workflow/sync');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers.get('Content-Type'), 'application/json');
      assert.equal(options.headers.get('X-EVKIN-Generation'), '7');
      assert.ok(options.body instanceof Blob, 'Workflow must use a bounded JSON Blob');
      const payload = JSON.parse(await options.body.text());
      requests.push({url: String(url), payload});
      if (responseMode === 'failure') {
        return new Response('Storage unavailable', {status: 500});
      }
      const counts = Object.fromEntries(Object.entries(payload).map(([source, value]) =>
        [source, source === 'config' ? (value ? 1 : 0) : value.length]));
      if (responseMode === 'mismatch') counts.daily_data--;
      if (responseMode === 'invalidated') E.invalidateFrom('config', {save: false});
      const result = responseMode === 'missing-counts' ? {status: 'ok'} : {status: 'ok', counts};
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: {'Content-Type': 'application/json', 'X-EVKIN-Generation': '7'}
      });
    }
  };
  for (const name of ['utils', 'data', 'state', 'import', 'calc', 'harian', 'app', 'export-backend']) {
    const path = `assets/js/${name}.js`;
    vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox, {filename: path});
  }
  E.progress = () => {};
  E.auditMod = {logStage() {}, logDatabaseSync() {}};
  E.ensureWorkflowDataForComputation = async () => {};
  E.exportBackend.enabled = true;
  E.exportBackend.acceptGeneration(7);

  const cases = [
    {id: 'normal', date: '03/08/2026', in: '07:30', out: '16:00', total: 0},
    {id: 'late-30', date: '03/08/2026', in: '08:00', out: '16:00', total: 0.5},
    {id: 'late-31', date: '03/08/2026', in: '08:01', out: '16:00', total: 1},
    {id: 'early-31', date: '03/08/2026', in: '07:30', out: '15:29', total: 1},
    {id: 'flexi', date: '03/08/2026', in: '07:45', out: '16:31', total: 0},
    {id: 'missing-out', date: '03/08/2026', in: '07:30', out: '', total: 3},
    {id: 'leave-period', date: '03/08/2026', in: '', out: '', total: 0, excused: true},
    {id: 'excused-kind', date: '03/08/2026', in: '09:00', out: '13:00',
      kind: 'Dinas Luar', total: 0, excused: true},
    {id: 'holiday', date: '17/08/2026', in: '', out: '', total: 0},
    {id: 'duration', date: '03/08/2026', in: '08:00', out: '15:00',
      method: 'durasi', total: 0.5},
    {id: 'friday', date: '07/08/2026', in: '07:30', out: '16:00', total: 0.5},
    {id: 'cross-day', date: '03/08/2026', in: '22:00', out: '04:00',
      method: 'durasi', total: 0}
  ];
  const headers = ['NIP', 'Nama', 'Jabatan', 'Tanggal', 'Jam Masuk', 'Jam Pulang', 'Zona', 'Jenis'];
  const columns = E.importMod.detectColumns(headers);
  const master = new Map();
  const normalized = Array.from({length: 1201}, (_, index) => {
    const item = cases[index] || {id: `normal-${index}`, date: index % 2 ? '03/09/2026' : '03/08/2026',
      in: '07:30', out: '16:00', total: 0};
    const nip = String(index + 1).padStart(18, '0');
    const jabatan = item.method === 'durasi' ? 'Petugas Durasi' : 'Petugas Reguler';
    const result = E.importMod.processSingleRow([
      nip, `Source ${item.id}`, jabatan, item.date, item.in, item.out, 'WIB', item.kind || ''
    ], headers, columns);
    assert.equal(result.invalid, false);
    master.set(nip, {nip, nama: `Master ${item.id}`,
      jabatan: item.method === 'durasi' ? 'Petugas Reguler' : 'Petugas Durasi'});
    return result;
  });
  E.lookupWorkflowMaster = async nips => {
    assert.equal(nips.length, 1201);
    return master;
  };
  const S = E.state;
  S.data = normalized.map(result => result.dataObj);
  // Deleted imports must be excluded without disturbing the coverage multiset.
  S.data.push({...S.data[0], _id: 'deleted', deleted: true});
  S.raw = normalized.map(result => result.rawObj);
  S.month = '2026-08';
  S.timeMode = 'original';
  S.importStats = {totalRecord: S.data.length, months: ['2026-08', '2026-09']};
  S.workflowMeta = {...S.workflowMeta, workflow_id: 'test-harian-save', status: 'ACTIVE'};
  S.config = E.normalizeConfig({
    saved: true,
    holidays: [{d: '2026-08-17', n: 'Kemerdekaan'}],
    jabatanMaster: [{nama: 'Petugas Reguler', metode: 'reguler'}, {nama: 'Petugas Durasi', metode: 'durasi'}],
    jenisNoPotongan: ['Dinas Luar'],
    leavePeriods: [{nip: S.data[6].nip, mulai: '2026-08-03', selesai: '2026-08-03', jenis: 'Cuti'}],
    masterPegawai: [{nip: 'MASTER-ONLY', nama: 'Do not sync canonical master'}]
  });
  const context = E.calc.buildContext();
  context.pegByNip = Object.fromEntries(master);
  const expected = JSON.parse(JSON.stringify(S.data.filter(row => !row.deleted)
    .map(row => E.calc.computeRecord(row, context, S.timeMode))));
  for (let index = 0; index < cases.length; index++) {
    assert.equal(expected[index].totalHarian, cases[index].total, cases[index].id);
    assert.equal(expected[index].excused === true, !!cases[index].excused, cases[index].id);
  }
  assert.equal(Object.hasOwn(expected[0], 'excused'), false);
  assert.equal(expected[6].excused, true);
  assert.equal(expected[0]._lateMin, 0);
  assert.equal(Object.hasOwn(expected[6], '_lateMin'), false);
  assert.equal(expected[11].durasiMin, 360);
  assert.equal(expected[11].metode, 'durasi');

  S.semifinalVerified = true;
  S.semifinal = [{old: true}];
  S.rekap = [{old: true}];
  S.final = [{nip: 'OLD', tukin: 100}];
  S.finalSKP = {enabled: true, rows: [{nip: 'OLD', skpPct: 10}]};
  const progress = [];
  await E.harianMod.process({onProgress: (done, total) => progress.push([done, total])});
  assert.equal(requests.length, 1, 'Daily results must commit in one atomic workflow request');
  assert.equal(forbiddenSerializations, 0, 'Neither workflow nor entire table arrays may be stringified');
  assert.equal(S.dailyComplete, true);
  assert.equal(E.stepDone('harian'), true);
  assert.equal(S.daily.length, 1201, 'Every active imported month must be calculated');
  assert.deepEqual(JSON.parse(JSON.stringify(S.daily)), expected);
  assert.deepEqual(progress.at(-1), [1201, 1201]);
  E.assertDailyCoverage();
  assert.equal(S.semifinalVerified, false);
  assert.equal(S.semifinal.length, 0);
  assert.equal(S.rekap.length, 0);
  assert.equal(S.final.length, 0);
  assert.equal(S.finalSKP.rows.length, 0);
  const payload = requests[0].payload;
  assert.deepEqual(payload.daily_data, expected, 'Persisted rows must match the established calculator exactly');
  for (let index = 0; index < normalized.length; index++) {
    assert.equal(payload.daily_data[index].jabatan, normalized[index].dataObj.jabatan,
      'Workbook jobs must prevail even when canonical SDM jobs differ');
  }
  assert.equal(payload.config.saved, true);
  assert.equal(payload.config._workflow.workflow_id, 'test-harian-save');
  assert.equal(payload.config._workflow.month, '2026-08');
  assert.equal(payload.config._workflow.semifinalVerified, false);
  assert.equal(Object.hasOwn(payload.config, 'masterPegawai'), false);
  for (const source of ['raw_import', 'processed_data', 'master_data']) {
    assert.equal(Object.hasOwn(payload, source), false, `${source} must be excluded from derived sync`);
  }
  for (const source of ['rekap_sdm', 'semifinal', 'final_absensi', 'final_skp', 'final_kombinasi', 'final_combined_skp']) {
    assert.deepEqual(payload[source], [], `${source} must be invalidated with new daily results`);
  }

  responseMode = 'failure';
  await assert.rejects(E.harianMod.process(), /gagal disimpan.*Storage unavailable/);
  assert.equal(requests.length, 2);
  assert.equal(S.daily.length, 1201, 'Computed results must remain available for retry');
  assert.deepEqual(JSON.parse(JSON.stringify(S.daily)), expected);
  assert.equal(S.dailyComplete, false, 'A failed save must not unlock daily verification');
  assert.equal(E.stepDone('harian'), false);
  assert.equal(E.harianMod.processing, false);

  responseMode = 'success';
  await E.harianMod.process();
  assert.equal(requests.length, 3, 'A failed queued write must allow a successful retry');
  assert.equal(S.dailyComplete, true);
  assert.deepEqual(requests.at(-1).payload.daily_data, expected);

  responseMode = 'mismatch';
  await assert.rejects(E.harianMod.process());
  assert.equal(requests.length, 4);
  assert.equal(S.dailyComplete, false, 'An incomplete persisted row count must not mark daily complete');
  assert.equal(E.stepDone('harian'), false);
  assert.equal(S.daily.length, 1201);
  assert.equal(E.harianMod.processing, false);

  responseMode = 'success';
  await E.harianMod.process();
  assert.equal(requests.length, 5);
  assert.equal(S.dailyComplete, true);
  assert.equal(forbiddenSerializations, 0);
  assert.deepEqual(requests.at(-1).payload.daily_data, expected);

  responseMode = 'missing-counts';
  await assert.rejects(E.harianMod.process(), /gagal disimpan.*mengonfirmasi jumlah/);
  assert.equal(requests.length, 6);
  assert.equal(S.dailyComplete, false, 'An active backend must confirm the saved row count');
  assert.equal(E.stepDone('harian'), false);
  assert.deepEqual(JSON.parse(JSON.stringify(S.daily)), expected);
  assert.equal(E.harianMod.processing, false);

  responseMode = 'success';
  E.exportBackend.enabled = false;
  await assert.rejects(E.harianMod.process(), /gagal disimpan.*Backend tidak aktif/);
  assert.equal(requests.length, 6, 'A disabled backend cannot perform a workflow POST');
  assert.equal(S.dailyComplete, false, 'A null save result cannot advertise persisted daily results');
  assert.equal(E.stepDone('harian'), false);
  assert.equal(S.daily.length, 1201);
  assert.equal(E.harianMod.processing, false);

  E.exportBackend.enabled = true;
  await E.harianMod.process();
  assert.equal(requests.length, 7);
  assert.equal(S.dailyComplete, true);
  assert.equal(forbiddenSerializations, 0);
  assert.deepEqual(requests.at(-1).payload.daily_data, expected);

  responseMode = 'invalidated';
  await assert.rejects(E.harianMod.process(), /berubah selama penyimpanan Harian/);
  assert.equal(requests.length, 8);
  assert.deepEqual(requests.at(-1).payload.daily_data, expected,
    'A pending POST must retain its immutable snapshot even if config invalidates current results');
  assert.equal(S.daily.length, 0, 'Completion must not revive results invalidated during the POST');
  assert.equal(S.dailyComplete, false);
  assert.equal(E.stepDone('harian'), false);
  assert.equal(E.harianMod.processing, false);

  responseMode = 'success';
  await E.harianMod.process();
  assert.equal(requests.length, 9);
  assert.equal(S.dailyComplete, true);
  assert.equal(forbiddenSerializations, 0);
  assert.deepEqual(requests.at(-1).payload.daily_data, expected);
  console.log('Harian: bounded atomic persistence, 1,201 exact legacy results, invalidation race and failure/count/backend retry gates: PASS');
}

main().catch(error => {console.error(error); process.exitCode = 1;});

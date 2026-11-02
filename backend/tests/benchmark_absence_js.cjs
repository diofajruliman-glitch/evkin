const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const E = {};
const sandbox = {window: {EVKIN: E}, console, Date, Math, URL, setTimeout, clearTimeout};
for (const name of ['utils', 'data', 'state', 'calc']) {
  vm.runInNewContext(fs.readFileSync(path.join(root, 'assets', 'js', `${name}.js`), 'utf8'), sandbox, {filename: name});
}
E.state.config = E.normalizeConfig(E.defaultConfig());
E.state.config.jabatanMaster = [{nama: 'Guru SR', metode: 'reguler'}, {nama: 'Wali Asuh', metode: 'durasi'}];

function record(index) {
  const wali = index % 5 === 0;
  const nip = String(index).padStart(8, '0');
  return {
    _id: `bench-${index}`, nip, nama: `Fixture ${index}`, jabatan: wali ? 'Wali Asuh' : 'Guru SR',
    tanggal: E.util.parseDate('2026-09-01'), tanggalMasukObj: E.util.parseDate('2026-09-01'),
    tanggalPulangObj: wali ? E.util.parseDate('2026-09-02') : E.util.parseDate('2026-09-01'),
    masukMin: wali ? 1200 : 451 + (index % 120), pulangMin: wali ? 240 : 960 - (index % 90),
    masukNormMin: wali ? 1200 : 451 + (index % 120), pulangNormMin: wali ? 240 : 960 - (index % 90),
    masukNormMinAdd: wali ? 1200 : 451 + (index % 120), pulangNormMinAdd: wali ? 240 : 960 - (index % 90),
    zona: 'WIB', jenis: '', crossDay: wali, sourceFormat: wali ? 'wali' : 'standard', issues: [], dupFlag: false
  };
}

function measure(size) {
  const rows = Array.from({length: size}, (_, index) => record(index));
  E.state.config.masterPegawai = rows.map(row => ({nip: row.nip, nama: row.nama, jabatan: row.jabatan}));
  const context = E.calc.buildContext();
  const heapBefore = process.memoryUsage().heapUsed;
  const started = process.hrtime.bigint();
  let checksum = 0;
  for (const row of rows) checksum += E.calc.computeRecord(row, context, 'original').totalHarian;
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
  const heapAfter = process.memoryUsage().heapUsed;
  return {records: size, elapsed_ms: Number(elapsedMs.toFixed(3)), records_per_second: Math.round(size / (elapsedMs / 1000)), heap_delta_bytes: heapAfter - heapBefore, checksum};
}

measure(1000);
const result = {baseline: 'ABSENCE_JS_BASELINE_2026_09', environment: {node: process.version, platform: process.platform, arch: process.arch}, synchronous_main_thread: true, samples: [measure(1000), measure(10000), measure(35000)]};
process.stdout.write(JSON.stringify(result, null, 2) + '\n');

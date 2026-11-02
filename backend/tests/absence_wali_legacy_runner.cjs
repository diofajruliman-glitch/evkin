const fs = require('node:fs');
const vm = require('node:vm');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const EVKIN = {
  state: {},
  master: {detectZone: () => 'WIB'},
  importMod: {
    applyImportResult: async (headers, raw, records, stats, mode) => {
      EVKIN.result = {headers, raw, records, stats, mode};
      return stats;
    }
  }
};
const sandbox = {
  window: {EVKIN}, console,
  crypto: {randomUUID: () => 'wali-golden-id'},
  setTimeout, clearTimeout
};

for (const file of ['assets/js/utils.js', 'assets/js/import-wali.js']) {
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, {filename:file});
}

async function main() {
  await EVKIN.waliImportMod.processAoa([fixture.headers, ...fixture.rows]);
  const result = EVKIN.result;
  const output = JSON.stringify({
    baseline: fixture.baseline,
    duplicate_source_rows: result.stats.duplicateSourceRows,
    records: result.records.map(item => ({
      nip: item.nip,
      nama: item.nama,
      jabatan: item.jabatan,
      date: item.tanggal.key,
      out_date: item.tanggalPulangObj ? item.tanggalPulangObj.key : '',
      check_in: item.masukMin,
      check_out: item.pulangMin,
      zone: item.zona,
      cross_day: item.crossDay,
      source_format: item.sourceFormat,
      issues: item.issues
    }))
  });
  if (process.argv[3]) fs.writeFileSync(process.argv[3], output + '\n');
  else process.stdout.write(output);
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

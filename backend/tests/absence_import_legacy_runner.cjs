const fs = require('node:fs');
const vm = require('node:vm');

const fixturePath = process.argv[2] || 'backend/tests/fixtures/absence_import_golden_cases_2026_09.json';
const outputPath = process.argv[3] || null;
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
let uid = 0;
const EVKIN = {state:{},master:null};
const sandbox = {
  window:{EVKIN}, console, crypto:{randomUUID:()=>`fixture-${++uid}`},
  setTimeout:fn=>fn(), clearTimeout
};
for (const file of ['assets/js/utils.js','assets/js/data.js','assets/js/import.js']) {
  vm.runInNewContext(fs.readFileSync(file,'utf8'), sandbox, {filename:file});
}
const headers = fixture.headers;
const mapping = EVKIN.importMod.detectColumns(headers);
const results = fixture.cases.map(item => {
  const result = EVKIN.importMod.processSingleRow(item.values, headers, mapping);
  const data = result.dataObj;
  return {
    id:item.id, invalid:result.invalid,
    normalized:data ? {
      nip:data.nip,nama:data.nama,jabatan:data.jabatan,date:data.tanggal.key,
      out_date:data.tanggalPulang,check_in:data.masukRaw,check_out:data.pulangRaw,
      check_in_minutes:data.masukMin,check_out_minutes:data.pulangMin,
      check_in_wib:data.masukNormMin,check_out_wib:data.pulangNormMin,
      check_in_add:data.masukNormMinAdd,check_out_add:data.pulangNormMinAdd,
      zone:data.zona,cross_day:data.crossDay,issues:data.issues
    } : null
  };
});
const duplicateKeys = new Map();
results.forEach(item => {
  if (!item.normalized) return;
  const key = item.normalized.nip + '|' + item.normalized.date;
  duplicateKeys.set(key, (duplicateKeys.get(key) || 0) + 1);
});
results.forEach(item => {
  item.duplicate = !!(item.normalized && duplicateKeys.get(item.normalized.nip + '|' + item.normalized.date) > 1);
});
const output = {baseline:fixture.baseline,cases:results};
if (outputPath) fs.writeFileSync(outputPath, JSON.stringify(output,null,2) + '\n');
else process.stdout.write(JSON.stringify(output));

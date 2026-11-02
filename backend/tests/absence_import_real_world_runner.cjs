const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');

const source = process.argv[2];
if (!source) throw new Error('CSV source path required');
let uid = 0;
const EVKIN = {state:{}};
const sandbox = {
  window:{EVKIN}, console, crypto:{randomUUID:()=>`rw-${++uid}`},
  setTimeout:fn=>fn(), clearTimeout, Buffer, Uint8Array, ArrayBuffer,
};
for (const file of ['assets/vendor/xlsx.full.min.js','assets/js/utils.js','assets/js/data.js','assets/js/import.js']) {
  vm.runInNewContext(fs.readFileSync(file,'utf8'), sandbox, {filename:file});
}
const workbook = sandbox.XLSX.read(fs.readFileSync(source), {type:'buffer',raw:true,cellDates:false});
const rows = sandbox.XLSX.utils.sheet_to_json(workbook.Sheets[workbook.SheetNames[0]], {header:1,raw:true,defval:''});
const headers = rows[0].map(value=>String(value).trim());
const mapping = EVKIN.importMod.detectColumns(headers);
const digest = crypto.createHash('sha256');
const keys = new Map();
let materialRows = 0, rejectedRows = 0, issueRows = 0, crossDay = 0;
for (const values of rows.slice(1)) {
  if (!values.some(value=>String(value).trim() !== '')) continue;
  const result = EVKIN.importMod.processSingleRow(values, headers, mapping);
  if (!result.dataObj) { rejectedRows++; continue; }
  const item = result.dataObj;
  const material = {
    nip:item.nip,nama:item.nama,jabatan:item.jabatan,date:item.tanggal.key,out_date:item.tanggalPulang,
    check_in:item.masukRaw,check_out:item.pulangRaw,check_in_minutes:item.masukMin,
    check_out_minutes:item.pulangMin,check_in_wib:item.masukNormMin,check_out_wib:item.pulangNormMin,
    check_in_add:item.masukNormMinAdd,check_out_add:item.pulangNormMinAdd,zone:item.zona,
    cross_day:item.crossDay,issues:item.issues
  };
  digest.update(JSON.stringify(material) + '\n');
  const key = item.nip + '|' + item.tanggal.key;
  keys.set(key,(keys.get(key)||0)+1);
  materialRows++; if (item.issues.length) issueRows++; if (item.crossDay) crossDay++;
}
const duplicateRows = [...keys.values()].filter(value=>value>1).reduce((total,value)=>total+value,0);
process.stdout.write(JSON.stringify({rows:materialRows,rejected_rows:rejectedRows,issue_rows:issueRows,
  cross_day:crossDay,duplicate_rows:duplicateRows,sha256:digest.digest('hex')}));

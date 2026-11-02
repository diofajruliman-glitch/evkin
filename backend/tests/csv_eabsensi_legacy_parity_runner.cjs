const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  let uid = 0;
  const EVKIN = {
    state:{month:'',data:[]}, master:null,
    invalidateFrom(){}, auditMod:{logDataImport(){}},
    syncImportedWorkflow:async()=>{}, syncBackendWorkflow:async()=>{},
    exportBackend:{enabled:false}
  };
  const sandbox = {
    window:{EVKIN}, console, Date, Math, setTimeout:fn=>fn(), clearTimeout,
    crypto:{randomUUID:()=>`parity-${++uid}`}
  };
  for (const file of ['assets/js/utils.js','assets/js/data.js','assets/js/import.js']) {
    vm.runInNewContext(fs.readFileSync(file,'utf8'), sandbox, {filename:file});
  }
  await EVKIN.importMod.process([fixture.headers, ...fixture.rows]);
  const rows = EVKIN.state.data.map(row => ({
    nip:String(row.nip || ''), nama:String(row.nama || ''), jabatan:String(row.jabatan || ''),
    tanggal:String(row.tanggal && row.tanggal.key || ''),
    tanggal_pulang:String(row.tanggalPulang || ''),
    jam_masuk:String(row.masukRaw || ''), jam_pulang:String(row.pulangRaw || ''),
    zona:String(row.zona || ''), provinsi:String(row.provinsi || ''),
    kabupaten:String(row.kabupaten || ''), jenis:String(row.jenis || ''),
    cross_day:!!row.crossDay
  })).sort((a,b)=>(a.nip+'|'+a.tanggal).localeCompare(b.nip+'|'+b.tanggal));
  process.stdout.write(JSON.stringify(rows));
}

main().catch(error => { console.error(error); process.exitCode = 1; });

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const E = {};
  const sandbox = {window:{EVKIN:E}, console, Date, Math, URL, setTimeout, clearTimeout};
  for (const name of ['utils','data','state','import','calc','harian']) {
    vm.runInNewContext(fs.readFileSync(`assets/js/${name}.js`,'utf8'),sandbox);
  }
  E.ensureWorkflowDataForComputation = async()=>{};
  E.auditMod = {logStage(){}};
  const master = new Map([
    ['001',{nip:'001',nama:'Nama Master 1',jabatan:'Wali Asuh'}],
    ['002',{nip:'002',nama:'Nama Master 2',jabatan:'Guru SR'}],
    ['003',{nip:'003',nama:'Nama Master 3',jabatan:'Wali Asuh'}]
  ]);
  E.lookupWorkflowMaster = async()=>master;
  E.exportBackend = {enabled:true};
  let saved;
  E.syncBackendWorkflow = async()=>{
    saved = JSON.parse(JSON.stringify(E.state.daily));
    return {counts:{daily_data:saved.length}};
  };
  const headers = ['NIP','Nama','Jabatan','Tanggal','Jam Masuk','Jam Pulang'];
  const columns = E.importMod.detectColumns(headers);
  function imported(nip, job) {
    const row = E.importMod.processSingleRow([nip,'Nama Workbook',job,'01/09/2026','08:00','16:00'],headers,columns);
    assert.equal(row.invalid,false);
    return row.dataObj;
  }
  E.state.month = '2026-09';
  E.state.config.saved = true;
  E.state.data = [imported('001',' Guru SR '),imported('002','Wali Asuh'),imported('003','  ')];
  await E.harianMod.process();
  assert.deepEqual(saved.map(row=>[row.nip,row.jabatan,row.metode,row.totalHarian]),[
    ['001','Guru SR','reguler',0.5],
    ['002','Wali Asuh','durasi',0],
    ['003','Belum Ditentukan','reguler',0.5]
  ]);
  assert.equal(saved[0].nama,'Nama Master 1','Identity policy must remain unchanged');
  // Reimport the same employee with a new workbook job; unchanged master must
  // not retain the old attendance job or method.
  E.state.data = [imported('001','Wali Asuh')];
  E.invalidateFrom('import',{save:false});
  await E.harianMod.process();
  assert.equal(saved[0].jabatan,'Wali Asuh');
  assert.equal(saved[0].metode,'durasi');
  assert.equal(saved[0].totalHarian,0);
  E.state.data = [imported('001','Guru SR')];
  E.invalidateFrom('import',{save:false});
  await E.harianMod.process();
  assert.equal(saved[0].jabatan,'Guru SR');
  assert.equal(saved[0].metode,'reguler');
  assert.equal(saved[0].totalHarian,0.5);
  assert.equal(master.get('001').jabatan,'Wali Asuh','Attendance import must not overwrite SDM master');
  console.log('Workbook job overrides differing master, blank stays undetermined, reimport changes method and persisted daily deduction: PASS');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

async function main() {
  const E = {};
  const raw = Array.from({length:1201}, (_, index) => ({
    workflow_id:'WORKFLOW-A', import_id:'IMPORT-A', source_row:index + 1,
    nip:String(index + 1).padStart(18, '0'), nama:`Pegawai ${index}`, jabatan:'Pegawai',
    tanggal:`2026-08-${String(index % 28 + 1).padStart(2, '0')}`, tanggal_pulang:'',
    jam_masuk:'07:30', jam_pulang:'16:00', zona:'WIB', provinsi:'', kabupaten:'', jenis:'',
    source_format:'csv_eabsensi'
  }));
  raw[0].tanggal='2026-08-03'; raw[0].jam_masuk='06:46:26'; raw[0].jam_pulang='17:41:22';
  raw[1].tanggal='2026-08-10'; raw[1].tanggal_pulang='2026-08-11';
  raw[1].jam_masuk='21:30:00'; raw[1].jam_pulang='06:45:00';
  let calls = 0;
  let expectedCalculationRows = raw.length;
  const workflowConfig={saved:true,_workflow:{workflow_id:'WORKFLOW-A',month:'2026-08',
    importMode:'csv_eabsensi',importStatus:'COMPLETE',
    importStats:{totalRaw:1201,totalRecord:1201,months:['2026-08']}}};
  const sandbox = {window:{EVKIN:E},console,Date,Math,URL,setTimeout,clearTimeout,
    fetch:async url => {
      const parsed = new URL(String(url));
      if(parsed.pathname==='/api/workflow/state') {
        return {ok:true,json:async()=>({data:{config:workflowConfig},meta:{
          workflow_id:'WORKFLOW-A',periode:'2026-08',status:'ACTIVE',
          row_counts:{raw_import:raw.length},total_row_counts:{raw_import:raw.length,processed_data:0}
        }})};
      }
      if(parsed.pathname==='/api/workflow/data/raw_import') {
        return {ok:true,json:async()=>({page:1,page_size:100,total:1201,total_pages:13,
          rows:[{_row_id:1,nip:raw[0].nip,nama:raw[0].nama,jabatan:raw[0].jabatan,
            tanggal:raw[0].tanggal,jenis:'',source_row:1}]})};
      }
      assert.equal(parsed.pathname, '/api/workflow/calculation-input');
      assert.equal(parsed.searchParams.get('workflow_id'), 'WORKFLOW-A');
      const page = Number(parsed.searchParams.get('page'));
      const start = (page - 1) * 500;
      calls++;
      return {ok:true,json:async()=>({status:'ok',workflow_id:'WORKFLOW-A',
        import_status:'COMPLETE',expected_source_rows:raw.length,persisted_source_rows:raw.length,
        expected_calculation_rows:expectedCalculationRows,page,page_size:500,total:raw.length,total_pages:Math.ceil(raw.length/500),
        rows:raw.slice(start,start + 500)})};
    }};
  for (const name of ['utils','data','state','import','calc','harian']) {
    vm.runInNewContext(fs.readFileSync(`assets/js/${name}.js`, 'utf8'), sandbox);
  }
  E.exportBackend={enabled:true,apiBase:'http://localhost'};
  E.lookupWorkflowMaster=async()=>new Map(); E.invalidateFrom=()=>{};
  E.auditMod={logStage(){}}; E.progress=()=>{};
  assert.equal(await E.load(),true,'Browser refresh restores canonical workflow metadata');
  assert.equal(E.state.importMode,'csv_eabsensi');
  assert.equal(E.state.raw.length,0,'The startup metadata response does not fabricate raw rows');
  await E.ensureWorkflowDataForModule('import');
  assert.deepEqual(Object.keys(E.state.raw[0]),
    ['_row_id','nip','nama','jabatan','tanggal','jenis','source_row']);
  assert.equal(E.state.data.length,0,
    'Bounded raw_import list projection must never be restored as calculation input');
  await E.ensureWorkflowDataForModule('config');
  assert.equal(E.stepUnlocked('config'),true);
  assert.equal(E.stepUnlocked('harian'),true,'Saved Config unlocks Harian for the active CSV workflow');
  assert.equal(E.state.raw.length,1201,'Opening Config hydrates every canonical row');
  assert.equal(E.state.data.length,1201,'Canonical CSV rows are restored into calculation state');
  assert.equal(E.state.data[0].tanggal.key,'2026-08-03');
  assert.equal(E.state.data[0].masukRaw,'06:46:26');
  assert.equal(E.state.data[0].pulangRaw,'17:41:22');
  assert.equal(E.state.data[1].tanggal.key,'2026-08-10');
  assert.equal(E.state.data[1].tanggalPulang,'11/08/2026');
  assert.equal(E.state.data[1].masukRaw,'21:30:00');
  assert.equal(E.state.data[1].pulangRaw,'06:45:00');
  assert.equal(E.state.importStats.totalRecord,1201);
  await E.harianMod.process({sync:false});
  assert.equal(calls,3,'Calculation hydration must walk all 500-row pages');
  assert.equal(E.state.data.length,1201);
  assert.equal(E.state.daily.length,1201,'Every canonical row must reach calc.js');
  assert.equal(E.state.workflowMeta.total_row_counts.processed_data,0);
  E.state.data=[];
  E.state.workflowMeta={row_counts:{raw_import:0},total_row_counts:{raw_import:1201}};
  assert.equal(E.stepUnlocked('config'),false,
    'Historical rows from another workflow must not unlock Config after active reset');

  raw.push({...raw[0],nip:'SOURCE-ONLY',tanggal:null,tanggal_pulang:'',jam_masuk:'',jam_pulang:''});
  workflowConfig._workflow.importStats.totalRaw = raw.length;
  assert.equal(await E.load(), true);
  await E.loadCalculationInput();
  assert.equal(E.state.raw.length, 1202);
  assert.equal(E.state.data.length, 1201, 'Source-only rows must not fail normalized-row completeness');
  raw.splice(0, raw.length - 1);
  workflowConfig._workflow.importStats.totalRaw = 1;
  workflowConfig._workflow.importStats.totalRecord = expectedCalculationRows = 0;
  assert.equal(await E.load(), true);
  await E.loadCalculationInput();
  assert.equal(E.state.raw.length, 1);
  assert.equal(E.state.data.length, 0);
  assert.equal(E.state.importStats.totalRecord, 0, 'Explicit zero dated records must not fall back to raw row count');
  assert.equal(E.state.month, '2026-08', 'The reporting month remains available without a dated attendance');
  raw[0] = {...raw[0],tanggal:'2026-08-03',jam_masuk:'07:30',jam_pulang:'16:00'};
  await assert.rejects(E.loadCalculationInput({silent:true}), /1\/0 record/,
    'A declared zero calculation count must still reject an unexpected dated row');
  console.log('CSV workflow refresh -> Config hydration -> 1,201-row Harian calculation PASS');
}

main().catch(error => { console.error(error); process.exitCode=1; });

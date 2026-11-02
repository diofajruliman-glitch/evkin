const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
async function main() {
  let payload, syncCalls = 0;
  const E = {util:{esc:value=>String(value??'')},state: {config: {masterPegawai: [{nip: 'MASTER'}]}, raw: [{source: true}],
    daily: [{daily: true}], final: [{nip: 'WORKFLOW', tukin: 2000000, komponen: 600000, potPct: 10, nominal: 60000}]},
    exportBackend: {enabled: true, syncWorkflow: async data => {syncCalls++;payload = data; return {counts: {}};}}};
  vm.runInNewContext(fs.readFileSync('assets/js/app.js', 'utf8'), {
    window: {EVKIN: E}, document: {addEventListener() {},getElementById(){return null},body:{appendChild(){}},
      createElement(){return {setAttribute(){},remove(){},classList:{add(){},remove(){},contains(){return false}}}}},
    console,setTimeout,clearTimeout
  });
  await E.syncBackendWorkflow(undefined, {finalOnly: true});
  assert.deepEqual(Object.keys(payload).sort(), ['final_absensi','final_combined_skp','final_kombinasi','final_skp']);
  assert.equal(payload.final_absensi[0].nip, 'WORKFLOW');
  await E.syncBackendWorkflow();
  assert.equal(payload.raw_import[0].source, true);
  assert.equal('master_data' in payload, false);
  assert.equal('masterPegawai' in payload.config, false);
  E.state.finalCombinedSKP = {useApp:true,skpRows:[{nip:'WORKFLOW',skpPct:20}]};
  await E.syncBackendWorkflow(undefined,{finalOnly:true});
  assert.equal(payload.final_combined_skp[0].totalPotonganPct,17);
  assert.equal(payload.final_combined_skp[0].totalNominalPotongan,340000);
  assert.equal(payload.final_combined_skp[0].finalDibayarKombinasiSKP,1660000);
  const tableWrites=[];const beforeImportSyncCalls=syncCalls;
  E.state.raw=Array.from({length:15001},(_,i)=>({nip:String(i),tanggal:'2026-08-01'}));
  E.state.data=E.state.raw.map(row=>({...row,tanggal:{monthKey:'2026-08',key:'2026-08-01'}}));
  E.state.workflowMeta={row_counts:{},total_row_counts:{}};E.state.workflowLoaded={};
  E.exportBackend.syncTableChunks=async(source,rows)=>{tableWrites.push([source,rows.length,rows[0]]);return {status:'ok',total_rows:rows.length};};
  await E.syncImportedWorkflow();
  assert.equal(syncCalls,beforeImportSyncCalls,'Import must not call full workflow sync');
  assert.deepEqual(tableWrites.filter(([source])=>['raw_import','processed_data'].includes(source)).map(row=>row.slice(0,2)),[
    ['raw_import',15001],['processed_data',0]
  ]);
  const persistedLegacy = tableWrites.find(([source])=>source==='raw_import')[2];
  assert.equal(persistedLegacy.workflow_id,E.state.workflowMeta.workflow_id);
  assert.equal(persistedLegacy.source_format,'legacy_excel');
  assert.equal(persistedLegacy.tanggal,'2026-08-01');
  assert.equal(Object.keys(persistedLegacy).length,17,'Legacy Excel persistence must use the canonical raw_import schema');
  assert.ok(tableWrites.some(([source,count])=>source==='daily_data'&&count===0));
  assert.ok(!tableWrites.some(([source])=>source==='master_data'));
  assert.equal(E.state.workflowMeta.status,'ACTIVE');
  assert.equal(tableWrites.at(-1)[0],'config','Import manifest must be published last');
  tableWrites.length=0;
  E.state.raw=Array.from({length:10},(_,i)=>({nip:String(i),tanggal:'2026-08-01'}));
  E.state.data=E.state.raw.map(row=>({...row,tanggal:{monthKey:'2026-08',key:'2026-08-01'}}));
  await E.syncImportedWorkflow();
  assert.equal(syncCalls,beforeImportSyncCalls,'Small import must also avoid the legacy JSON sync');
  assert.deepEqual(tableWrites.filter(([source])=>['raw_import','processed_data'].includes(source)).map(row=>row.slice(0,2)),[
    ['raw_import',10],['processed_data',0]
  ]);
  E.exportBackend.syncTableChunks=async(source,rows)=>{
    if(source==='raw_import') throw new Error('simulated chunk failure');
    return {status:'ok',total_rows:rows.length};
  };
  await assert.rejects(E.syncImportedWorkflow(),/simulated chunk failure/);
  assert.equal(E.state.workflowMeta.status,'ERROR','Failed imports must never be advertised as active');
  console.log('Final-only sync excludes raw, daily and master; ordinary sync preserved: PASS');
}
main().catch(error => {console.error(error); process.exitCode = 1;});

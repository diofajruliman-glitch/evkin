const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {webcrypto, createHash} = require('node:crypto');
async function main() {
  const requests = [];
  let fail = false;
  const E = {};
  vm.runInNewContext(fs.readFileSync('assets/js/export-backend.js', 'utf8'), {
    window: {EVKIN:E}, URL, AbortSignal, TextEncoder, Blob, crypto:webcrypto, console,
    fetch: async (url, options) => {
      requests.push({url:String(url), body:JSON.parse(options.body instanceof Blob ? await options.body.text() : options.body)});
      return {ok:!fail, json:async()=>({status:'ok'}), text:async()=> 'Database unavailable'};
    }
  });
  const backend = E.exportBackend;
  const rows = [{nip:'001234567890123456', tukin:100, nominal:10, finalDibayar:90}];
  const manifest = await backend.finalManifest(rows);
  assert.equal(manifest.sha256, createHash('sha256').update('["001234567890123456","100","10","90"]').digest('hex'));
  assert.equal(manifest.paid, '90');
  await assert.rejects(backend.finalManifest([{...rows[0],tukin:null}]));
  const data = {final_absensi:rows, final_skp:[], final_kombinasi:[], final_combined_skp:[]};
  await backend.syncWorkflow(data);
  assert.equal(requests.length, 1);
  assert.ok(requests[0].url.endsWith('/api/workflow/sync'));
  assert.deepEqual(requests[0].body, data);
  fail = true;
  await assert.rejects(backend.syncWorkflow(data));
  fail = false;
  await backend.syncWorkflow(data);
  await backend.createExportJob({bulan:'2026-09',expectedFinal:manifest});
  assert.deepEqual(requests.at(-1).body.expected_final, JSON.parse(JSON.stringify(manifest)));
  const full={processed_data:Array.from({length:35001},(_,i)=>({record:JSON.stringify({nip:String(i)})})),daily_data:[]};
  const requestCount=requests.length;
  await backend.syncWorkflow(full);
  assert.equal(requests.length,requestCount+1);
  assert.equal(requests.at(-1).body.processed_data.length,35001);
  assert.ok(requests.at(-1).url.endsWith('/api/workflow/sync'));
  // Mutating caller data after enqueue must not change an earlier snapshot.
  let release;backend.syncQueue=new Promise(resolve=>{release=resolve;});
  const input={raw_import:[{nip:'FIRST'}]};const write=backend.syncWorkflow(input);
  input.raw_import[0].nip='LATER';release();await write;
  assert.equal(requests.at(-1).body.raw_import[0].nip,'FIRST');
  const unusual={config:{note:'Cuti "pegawai"\\\n雪 😀',optional:undefined},
    daily_data:[{nip:'00001',flags:['Lintas hari'],optional:undefined},undefined,null,{totalHarian:NaN}],
    rekap_sdm:[],optional:undefined};
  await backend.syncWorkflow(unusual);
  assert.deepEqual(requests.at(-1).body,JSON.parse(JSON.stringify(unusual)),
    'Blob JSON must preserve escaped/Unicode values, nulls and omitted optional fields');
  const cyclic={};cyclic.self=cyclic;
  const beforeInvalid=requests.length;
  await assert.rejects(backend.syncWorkflow({daily_data:[cyclic]}),/circular/i);
  assert.equal(requests.length,beforeInvalid,'Invalid snapshots must never start a partial write');
  await backend.syncWorkflow(data);
  const chunkRows=Array.from({length:15001},(_,i)=>({nip:String(i),tanggal:'2026-08-01'}));
  const beforeChunks=requests.length;
  await backend.syncTableChunks('raw_import',chunkRows,{chunkSize:7500});
  const chunks=requests.slice(beforeChunks);
  assert.equal(chunks.length,3);
  assert.deepEqual(chunks.map(item=>item.body.rows.length),[7500,7500,1]);
  assert.deepEqual(chunks.map(item=>item.body.replace),[true,false,false]);
  assert.equal(new Set(chunks.flatMap(item=>item.body.rows.map(row=>row.nip))).size,15001);
  assert.ok(chunks.every(item=>item.url.endsWith('/api/workflow/sync-table')));
  const sparse=Array.from({length:5001},(_,i)=>i===5000?{nip:String(i),tanggal:'2026-08-01',Opsional:'X'}:{nip:String(i),tanggal:'2026-08-01'});
  const sparseStart=requests.length;
  await backend.syncTableChunks('raw_import',sparse,{chunkSize:5000});
  const sparseChunks=requests.slice(sparseStart);
  assert.equal(sparseChunks.length,2);
  assert.ok(sparseChunks.flatMap(item=>item.body.rows).every(row=>Object.hasOwn(row,'Opsional')),
    'Every raw chunk must preserve one stable column projection');
  assert.equal(sparseChunks[0].body.rows[0].Opsional,null);
  console.log('Manifest, atomic Final sync, failure/retry and export validation payload: PASS');
}
main().catch(e=>{console.error(e);process.exitCode=1;});

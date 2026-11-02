const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};
function root() {
  const nodes = new Map(['#btnHitung', '#hMonth', '#hMode', '#harianProcessStatus',
    '#harianStageBadge', '#harianResult'].map(id => [id, {innerHTML:'', textContent:'', disabled:false}]));
  return {innerHTML:'', querySelector: id => nodes.get(id) || null};
}
async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.fail('Expected process stage was not reached');
}
async function main() {
  const hydration = deferred(), master = deferred(), save = deferred();
  let failSave = false, writes = 0, toasts = 0;
  const row = {nip:'001', tanggal:{monthKey:'2026-09'}};
  const E = {
    state:{data:[row], daily:[], month:'2026-09', importStats:{months:['2026-09']}, timeMode:'original'},
    util:{esc:v=>String(v ?? '').replaceAll('<','&lt;'), monthLabel:v=>v, round2:v=>v},
    calc:{buildContext:()=>({}), computeRecord:()=>({nip:'001', flags:[],
      statusHariLibur:'Tidak', totalHarian:0, tanggal:'01/09/2026', monthKey:'2026-09'})},
    configMod:{effectiveDays:()=>22},
    ensureWorkflowDataForComputation:()=>hydration.promise,
    lookupWorkflowMaster:()=>master.promise,
    exportBackend:{enabled:true}, invalidateFrom(){}, assertDailyCoverage(){},
    auditMod:{logStage(){}}, progress(){},
    stepUnlocked:()=>true, stepDone:()=>E.state.dailyComplete === true,
    toast(){toasts++;}, go(){},
    syncBackendWorkflow:async()=>{
      writes++;
      await save.promise;
      if (failSave) throw new Error('Storage <unavailable>');
      return {counts:{daily_data:1}};
    }
  };
  vm.runInNewContext(fs.readFileSync('assets/js/harian.js','utf8'),
    {window:{EVKIN:E}, console, setTimeout, clearTimeout});
  const mod = E.harianMod;
  const first = root();
  mod.render(first);
  assert.match(first.innerHTML,/Siap menghitung/);
  const running = mod.process();
  assert.equal(first.querySelector('#btnHitung').disabled,true);
  assert.match(first.querySelector('#harianProcessStatus').innerHTML,/Memuat data impor/);
  hydration.resolve();
  await until(()=>mod.processStatus.stage === 1);
  assert.match(first.querySelector('#harianProcessStatus').innerHTML,/Membaca Master SDM/);
  // Navigating away and returning must retain busy controls and process state.
  const returned = root();
  mod.render(returned);
  assert.equal(returned.querySelector('#btnHitung').disabled,true);
  assert.equal(returned.querySelector('#hMode').disabled,true);
  assert.match(returned.innerHTML,/Sedang berjalan/);
  await returned.querySelector('#btnHitung').onclick();
  assert.equal(toasts,0,'Clicking during a running job must not show a misleading error');
  master.resolve(new Map());
  await until(()=>writes === 1);
  const saving = returned.querySelector('#harianProcessStatus').innerHTML;
  assert.match(saving,/Menyimpan hasil/);
  assert.match(saving,/menunggu konfirmasi/);
  assert.match(saving,/1<\/b> dari <b>1/);
  assert.match(saving,/<progress[^>]*max="100"[^>]*aria-label="Menyimpan hasil"/);
  assert.doesNotMatch(saving,/<progress[^>]*value=/,'Saving must show indeterminate progress');
  assert.equal(E.state.dailyComplete,false);
  save.resolve();
  await running;
  assert.equal(returned.querySelector('#btnHitung').disabled,false);
  assert.equal(returned.querySelector('#hMode').disabled,false);
  assert.match(returned.querySelector('#harianProcessStatus').innerHTML,/Jumlah hasil sudah dikonfirmasi/);
  assert.equal(mod.processStatus.state,'done');
  assert.equal(mod.processing,false);
  const finished = root();
  mod.render(finished);
  assert.match(finished.innerHTML,/Hitung Harian selesai/,'Completion must remain visible after returning');
  failSave = true;
  await assert.rejects(mod.process(),/Storage/);
  assert.equal(mod.processStatus.state,'error');
  assert.match(finished.querySelector('#harianProcessStatus').innerHTML,/Storage &lt;unavailable>/);
  assert.match(finished.querySelector('#harianProcessStatus').innerHTML,/role="alert"/);
  assert.equal(finished.querySelector('#btnHitung').disabled,false);
  assert.equal(E.state.dailyComplete,false);
  failSave = false;
  await mod.process();
  assert.equal(mod.processStatus.state,'done');
  console.log('Harian UI: visible stages/counts, indeterminate save, navigation busy state, persistent completion/error and retry PASS');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

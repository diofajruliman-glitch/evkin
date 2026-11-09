const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const local = new Map();
  let polls = 0;
  let legacyCalls = 0;
  const hydratedSources = [];
  const jsonResponse = body => ({
    ok: true, status: 200, headers: new Headers({'X-EVKIN-Generation':'12'}),
    json: async () => body
  });
  const fakeFetch = async input => {
    const url = new URL(String(input));
    if (url.pathname.endsWith('/authority')) return jsonResponse({authority:'PYTHON_CANONICAL'});
    if (url.pathname.endsWith('/calculate')) return jsonResponse({calculation_id:'batch-uat',status:'PENDING'});
    if (url.pathname.endsWith('/batch-uat')) {
      polls += 1;
      return jsonResponse(polls === 1
        ? {calculation_id:'batch-uat',status:'RUNNING'}
        : {calculation_id:'batch-uat',status:'COMPLETED',canonical:true,counts:{daily:100000,rekap:5000,final:5000}});
    }
    if (url.pathname.endsWith('/latest')) return jsonResponse({calculation_id:'batch-uat',status:'COMPLETED',canonical:true});
    throw new Error('Unexpected URL ' + url);
  };
  const EVKIN = {
    calc: () => { legacyCalls += 1; },
    rekapMod: { calculate: () => { legacyCalls += 1; } },
    finalMod: { calculate: () => { legacyCalls += 1; } },
    loadWorkflowSource: async source => {
      hydratedSources.push(source);
      return [{calculation_id:'batch-uat', nip:'anonymous'}];
    }
  };
  const sandbox = {
    window: {EVKIN, location:{origin:'http://127.0.0.1:8000',protocol:'http:',href:'http://127.0.0.1:8000/',pathname:'/',search:'',hash:''}, open(){}},
    location:{origin:'http://127.0.0.1:8000',protocol:'http:',href:'http://127.0.0.1:8000/',pathname:'/',search:'',hash:''},
    document:{addEventListener(){}}, history:{replaceState(){}}, sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    localStorage:{getItem(key){return local.get(key) || null;},setItem(key,value){local.set(key,value);}},
    fetch:fakeFetch, Headers, URL, URLSearchParams, AbortSignal, AbortController, setTimeout, clearTimeout, console
  };
  vm.runInNewContext(fs.readFileSync('assets/js/export-backend.js','utf8'), sandbox);
  const result = await EVKIN.exportBackend.runCanonicalAbsenceCalculation(
    '2026-08','uat','controlled canary',{intervalMs:1,timeoutMs:2000}
  );
  assert.equal(result.batch.status, 'COMPLETED');
  assert.deepEqual(hydratedSources, ['daily_data','rekap_sdm','final_absensi']);
  hydratedSources.length = 0;
  const bounded = await EVKIN.exportBackend.runCanonicalAbsenceCalculation(
    '2026-08','uat','bounded',{hydrate:false,intervalMs:1,timeoutMs:2000}
  );
  assert.equal(bounded.batch.status, 'COMPLETED');
  assert.equal(bounded.hydrated, null);
  assert.deepEqual(hydratedSources, [], 'Bounded workflow must not download entire result tables');
  assert.equal(legacyCalls, 0, 'canonical hydration must not invoke legacy calculations');
  assert.equal(local.get('evkin.absenceAuthority'), 'PYTHON_CANONICAL');

  EVKIN.state = {month:'2026-08', daily:[], workflowMeta:{row_counts:{}}, workflowPages:{}, harianPage:0};
  EVKIN.exportBackend.enabled = true;
  const pages = [];
  EVKIN.loadWorkflowSource = async (source, options) => {
    pages.push(options.page);
    assert.equal(source, 'daily_data');
    assert.equal(options.perPage, 10);
    assert.equal(options.periode, '2026-08');
    assert.equal(options.cache, false);
    assert.notEqual(options.all, true);
    EVKIN.state.daily = [{calculation_id:'batch-uat', nip:'001'}];
    EVKIN.state.workflowPages.daily_data = {total:100000};
    return EVKIN.state.daily;
  };
  vm.runInNewContext(fs.readFileSync('assets/js/harian.js','utf8'), sandbox);
  await EVKIN.harianMod.process();
  assert.equal(EVKIN.state.workflowMeta.row_counts.daily_data, 100000);
  assert.equal(EVKIN.harianMod.processStatus.total, 100000);
  EVKIN.state.harianPage = 1;
  await EVKIN.harianMod.loadPage();
  assert.deepEqual(pages, [1, 2]);

  const exportSource = fs.readFileSync('assets/js/export.js','utf8');
  const exportCalls = [], notices = [];
  let failExport = false, browserFallbackCalls = 0;
  EVKIN.util = {esc:String, monthLabel:String};
  EVKIN.stepUnlocked = () => true;
  EVKIN.toast = (message, kind) => notices.push({message, kind});
  EVKIN.auditMod = {logDataExport(){}};
  sandbox.document.getElementById = () => null;
  EVKIN.exportBackend.startExportJob = async options => {
    exportCalls.push(options);
    if (failExport) throw new Error('Backend export unavailable');
  };
  vm.runInNewContext(exportSource, sandbox);
  EVKIN.exportMod.downloadSelected = () => { browserFallbackCalls += 1; };
  const exportButton = {disabled:false};
  EVKIN.exportMod.render({
    innerHTML:'', querySelector:() => exportButton,
    querySelectorAll:() => [{value:'FINAL ABSENSI'}]
  });
  async function clickExport() {
    exportButton.onclick();
    for (let attempt = 0; exportButton.disabled && attempt < 20; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 1));
    }
    assert.equal(exportButton.disabled, false, 'Export must finish and restore the button');
  }
  await clickExport();
  assert.equal(exportCalls.length, 1);
  assert.equal(exportCalls[0].bulan, '2026-08');
  assert.equal(exportCalls[0].calculationId, 'batch-uat',
    'Backend export must identify the completed canonical calculation');
  assert.deepEqual(Array.from(exportCalls[0].sheetNames), ['FINAL ABSENSI']);
  assert.equal(browserFallbackCalls, 0);
  failExport = true;
  await clickExport();
  assert.equal(exportCalls.length, 2);
  assert.equal(browserFallbackCalls, 0, 'An active backend failure must never export browser cache');
  assert.ok(notices.some(item => item.kind === 'error' && item.message.includes('Backend export unavailable')));
  console.log('Frontend canonical poll/hydrate and export authority guard: PASS');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

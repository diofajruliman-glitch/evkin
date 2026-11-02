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
        : {calculation_id:'batch-uat',status:'COMPLETED',canonical:true});
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
  assert.equal(legacyCalls, 0, 'canonical hydration must not invoke legacy calculations');
  assert.equal(local.get('evkin.absenceAuthority'), 'PYTHON_CANONICAL');

  const exportSource = fs.readFileSync('assets/js/export.js','utf8');
  assert.match(exportSource, /if \(!pythonCanonical && chosen\.some/);
  assert.match(exportSource, /fallback browser\/IndexedDB dinonaktifkan/);
  console.log('Frontend canonical poll/hydrate and export authority guard: PASS');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

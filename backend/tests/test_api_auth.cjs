const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const calls = [];
  const store = new Map();
  let cleanedUrl = null;
  const fakeFetch = async (input, options = {}) => {
    calls.push({ url: String(input), headers: options.headers });
    return { ok: true, status: 200, headers: new Headers({'X-EVKIN-Generation':'7'}),
      json: async () => ({ status: 'ok' }) };
  };
  const location = {
    href: 'http://127.0.0.1:8000/#evkin-api-key=browser-test-key',
    origin: 'http://127.0.0.1:8000',
    protocol: 'http:',
    pathname: '/',
    search: '',
    hash: '#evkin-api-key=browser-test-key'
  };
  const sandbox = {
    window: { EVKIN: {}, location, open() {} },
    location,
    history: { replaceState(_state, _title, url) { cleanedUrl = url; } },
    sessionStorage: {
      getItem(key) { return store.get(key) || null; },
      setItem(key, value) { store.set(key, value); },
      removeItem(key) { store.delete(key); }
    },
    document: { addEventListener() {} },
    fetch: fakeFetch,
    Headers,
    URL,
    URLSearchParams,
    AbortSignal,
    AbortController,
    setTimeout,
    clearTimeout,
    console
  };
  vm.runInNewContext(fs.readFileSync('assets/js/export-backend.js', 'utf8'), sandbox);

  const structuredError = sandbox.window.EVKIN.exportBackend.apiErrorInfo({
    detail:{code:'RAW_IMPORT_SCHEMA_CONFLICT', message:'Schema raw import tidak cocok.'}
  }, 'Fallback');
  assert.equal(structuredError.code, 'RAW_IMPORT_SCHEMA_CONFLICT');
  assert.equal(structuredError.message, 'RAW_IMPORT_SCHEMA_CONFLICT: Schema raw import tidak cocok.');
  assert.doesNotMatch(structuredError.message, /\[object Object\]/);
  const validationError = sandbox.window.EVKIN.exportBackend.apiErrorInfo({
    detail:[{loc:['body','rows'], msg:'Field wajib tidak tersedia.'}]
  }, 'Fallback');
  assert.equal(validationError.message, 'body.rows: Field wajib tidak tersedia.');

  assert.equal(sandbox.window.EVKIN.exportBackend.getApiKey(), 'browser-test-key');
  assert.equal(cleanedUrl, '/');
  await sandbox.fetch('http://127.0.0.1:8000/api/workflow/state', { method: 'GET' });
  sandbox.window.EVKIN.exportBackend.acceptGeneration(7);
  await sandbox.fetch('http://127.0.0.1:8000/api/workflow/sync', { method: 'POST' });
  await sandbox.fetch('https://attacker.invalid/collect', { method: 'GET' });
  const authority = await sandbox.window.EVKIN.exportBackend.getAbsenceAuthority();
  assert.equal(authority.status, 'ok');
  await sandbox.window.EVKIN.exportBackend.startAbsenceCalculation('2026-08', 'tester', 'canary');
  await sandbox.window.EVKIN.exportBackend.getAbsenceCalculation('batch-1');
  assert.equal(calls[0].headers.get('X-EVKIN-API-Key'), 'browser-test-key');
  assert.equal(calls[1].headers.get('X-EVKIN-Generation'), '7');
  assert.equal(calls[2].headers, undefined);
  assert.equal(calls[4].headers.get('X-EVKIN-Generation'), '7');
  assert.match(calls[4].url, /\/api\/absence\/calculate$/);
  assert.match(calls[5].url, /\/api\/absence\/calculation\/batch-1$/);
  console.log('Frontend API token and reset generation injection: PASS');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fakeIndexedDB() {
  const state = { closeCalls: 0, openCalls: 0, deleteCalls: 0, deleteMode: 'success' };
  const database = {
    objectStoreNames: { contains() { return true; } },
    close() { state.closeCalls += 1; },
    transaction() {
      const tx = {
        error: null,
        objectStore() {
          return {
            put() { return requestSuccess(undefined); },
            clear() { return requestSuccess(undefined); },
            getAll() { return requestSuccess([{ id: 1 }]); },
          };
        },
      };
      setTimeout(() => { if (tx.oncomplete) tx.oncomplete(); }, 10);
      return tx;
    },
  };
  function requestSuccess(result) {
    const request = { result, error: null };
    setTimeout(() => { if (request.onsuccess) request.onsuccess(); }, 0);
    return request;
  }
  return {
    state,
    api: {
      open() {
        state.openCalls += 1;
        const request = { result: database, error: null };
        setTimeout(() => { if (request.onsuccess) request.onsuccess(); }, 0);
        return request;
      },
      deleteDatabase() {
        state.deleteCalls += 1;
        const request = { error: null };
        if (state.deleteMode !== 'timeout') setTimeout(() => {
          if (state.deleteMode === 'success' && request.onsuccess) request.onsuccess();
          if (state.deleteMode === 'blocked' && request.onblocked) request.onblocked();
          if (state.deleteMode === 'error' && request.onerror) {
            request.error = new Error('delete failed'); request.onerror();
          }
        }, 0);
        return request;
      },
    },
  };
}

async function main() {
  const fake = fakeIndexedDB();
  const persistentStorage = new Map();
  let backendCalls = 0;
  const loadTab = () => {
    const session = new Map();
    const sandbox = {
      window: { EVKIN: {} }, indexedDB: fake.api,
      localStorage: {
        getItem(key) { return persistentStorage.get(key) || null; },
        setItem(key, value) { persistentStorage.set(key, value); },
        removeItem(key) { persistentStorage.delete(key); },
      },
      sessionStorage: {
        getItem(key) { return session.get(key) || null; },
        setItem(key, value) { session.set(key, value); },
        removeItem(key) { session.delete(key); },
      },
      fetch() { backendCalls += 1; throw new Error('cache cleanup must not call backend'); },
      setTimeout, clearTimeout, console,
    };
    vm.runInNewContext(fs.readFileSync('assets/js/evkin-db.js', 'utf8'), sandbox);
    return sandbox.window.EVKIN.db;
  };
  let db = loadTab();

  assert.deepEqual(await db.getAll('raw_import'), [{ id: 1 }]);
  await db.putMany('raw_import', [{ id: 2 }]);
  await db.replaceStore('raw_import', [{ id: 3 }]);
  await db.clear('raw_import');
  assert.equal(fake.state.openCalls, 4);
  assert.equal(fake.state.closeCalls, 4, 'every completed operation must close its connection');

  // Backend success marks stale before cleanup. BLOCKED must preserve it.
  fake.state.deleteMode = 'blocked';
  db.markCacheStale();
  assert.equal(db.isCacheStale(), true);
  assert.equal((await db.resetDatabase({ timeoutMs: 30 })).status, 'BLOCKED');
  assert.equal(db.isCacheStale(), true);

  // Reload and a new tab/session see the same persistent localStorage marker.
  db = loadTab();
  assert.equal(db.isCacheStale(), true);
  const anotherTab = loadTab();
  assert.equal(anotherTab.isCacheStale(), true);

  fake.state.deleteMode = 'error';
  assert.equal((await db.resetDatabase({ timeoutMs: 30 })).status, 'ERROR');
  assert.equal(db.isCacheStale(), true);
  fake.state.deleteMode = 'blocked';
  assert.equal((await db.resetDatabase({ timeoutMs: 30 })).status, 'BLOCKED');
  assert.equal(db.isCacheStale(), true);
  fake.state.deleteMode = 'timeout';
  assert.equal((await db.resetDatabase({ timeoutMs: 5 })).status, 'TIMEOUT');
  assert.equal(db.isCacheStale(), true);

  fake.state.deleteMode = 'success';
  assert.equal((await db.resetDatabase({ timeoutMs: 30 })).status, 'SUCCESS');
  assert.equal(db.isCacheStale(), false);
  assert.equal(loadTab().isCacheStale(), false);
  assert.equal((await db.resetDatabase({ timeoutMs: 30 })).status, 'SUCCESS');
  assert.equal(backendCalls, 0);

  assert.equal(db.resetOutcome(true, { status: 'SUCCESS' }).status, 'SUCCESS');
  for (const status of ['BLOCKED', 'ERROR', 'TIMEOUT']) {
    const outcome = db.resetOutcome(true, { status });
    assert.equal(outcome.status, 'PARTIAL_SUCCESS');
    assert.match(outcome.message, /Reset data server berhasil/);
  }
  assert.equal(db.resetOutcome(false, null).status, 'RESET_FAILED');
  console.log('IndexedDB lifecycle, deterministic delete outcomes and reset partial-success UX: PASS');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

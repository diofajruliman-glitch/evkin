const assert = require('node:assert/strict');
const fs = require('node:fs');

const app = fs.readFileSync('assets/js/app.js', 'utf8');
const dashboard = fs.readFileSync('assets/js/dashboard.js', 'utf8');
const exportSource = fs.readFileSync('assets/js/export.js', 'utf8');
const databaseSource = fs.readFileSync('assets/js/evkin-db.js', 'utf8');
const html = fs.readFileSync('index.html', 'utf8');

const operational = app.slice(
  app.indexOf('if (totalReset) totalReset.onclick'),
  app.indexOf('const backendButton')
);
assert.match(operational, /request\('confirm'/);
assert.match(operational, /mutationBlocked = true/);
assert.match(operational, /markCacheStale/);
assert.match(operational, /clearBrowserCache/);
assert.match(operational, /resetOutcome\(true, cacheResult\)/);
assert.match(operational, /window\.location\.reload/);
assert.ok(operational.indexOf('mutationBlocked = true') > operational.indexOf("request('confirm'"));

const cacheOnly = app.slice(
  app.indexOf('if (topbarDbReset) topbarDbReset.onclick'),
  app.indexOf('const backendButton')
);
assert.match(cacheOnly, /clearBrowserCache/);
assert.doesNotMatch(cacheOnly, /resetAll|requestResetProfile|reset-operational|fetch\(/);

const dashboardCache = dashboard.slice(
  dashboard.indexOf("el.querySelector('#dbResetBtn').onclick"),
  dashboard.indexOf("el.querySelector('#backendWorkflowResetBtn').onclick")
);
assert.match(dashboardCache, /clearBrowserCache/);
assert.doesNotMatch(dashboardCache, /resetAll|requestResetProfile|fetch\(/);

assert.match(html, /> Bersihkan Cache Browser</);
assert.match(html, /> Reset Data Operasional</);
assert.doesNotMatch(html, /> Reset Cache &amp; Hasil Workflow</);
assert.match(exportSource, /isCacheStale\(\)/);
assert.match(exportSource, /Offline export dinonaktifkan sampai cache dibersihkan/);
assert.match(databaseSource, /typeof localStorage/);
assert.doesNotMatch(databaseSource.slice(
  databaseSource.indexOf('function cacheStorage'),
  databaseSource.indexOf('const EVKIN')
), /sessionStorage/);
assert.ok(exportSource.indexOf('if (backendEnabled)') < exportSource.indexOf('EVKIN.db.isCacheStale'),
  'Canonical backend export must remain allowed before the offline stale-cache guard');
assert.match(exportSource, /Cache browser lama harus dibersihkan sebelum export offline/);

console.log('Reset UX: backend/cache separation, mandatory reload and stale offline-export guard PASS');

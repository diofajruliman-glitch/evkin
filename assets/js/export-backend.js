/* EVKIN ABSENSI v3.1 - export-backend.js
   Browser-side helper untuk export melalui backend Python/FastAPI.
   Tidak menggantikan export worker, tapi menjadi fallback untuk dataset besar.
*/
(function () {
  const EVKIN = window.EVKIN || {};
  EVKIN.exportBackend = EVKIN.exportBackend || {};
  const backend = EVKIN.exportBackend;

  function normalizeApiBase(value) {
    const candidate = String(value || '').trim();
    try {
      const parsed = new URL(candidate || 'http://localhost:8000');
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('Unsupported protocol');
      return parsed.toString().replace(/\/$/, '');
    } catch (error) {
      console.warn('URL backend tidak valid, menggunakan localhost:8000:', candidate);
      return 'http://localhost:8000';
    }
  }

  function resolveApiBase() {
    if (backend.apiBase) return normalizeApiBase(backend.apiBase);
    if (typeof window !== 'undefined' && window.location && window.location.protocol.startsWith('http')) {
      return normalizeApiBase(window.location.origin);
    }
    return 'http://localhost:8000';
  }

  backend.apiBase = resolveApiBase();
  backend.enabled = false;
  backend.syncQueue = Promise.resolve();
  backend.runtimeGeneration = null;
  // Server authority is runtime metadata only. The production default remains
  // JS_LEGACY until an explicit backend feature-flag activation.
  const AUTHORITY_STORAGE = 'evkin.absenceAuthority';
  function storedAuthority() {
    try {
      const value = typeof localStorage !== 'undefined' ? localStorage.getItem(AUTHORITY_STORAGE) : '';
      return ['JS_LEGACY','PYTHON_SHADOW','PYTHON_CANONICAL'].includes(value) ? value : 'JS_LEGACY';
    } catch (_) { return 'JS_LEGACY'; }
  }
  function persistAuthority(value) {
    try { if (typeof localStorage !== 'undefined') localStorage.setItem(AUTHORITY_STORAGE, value); }
    catch (_) { /* Authority metadata is only a fail-safe hint; backend remains authoritative. */ }
  }
  backend.calculationAuthority = storedAuthority();
  backend.mutationBlocked = false;
  const API_KEY_STORAGE = 'evkin.localApiKey';
  const API_KEY_HEADER = 'X-EVKIN-API-Key';
  const nativeFetch = typeof globalThis.fetch === 'function' ? globalThis.fetch.bind(globalThis) : null;
  const authenticatedOrigins = new Set();

  function cookieGet() {
    try {
      if (typeof document === 'undefined' || !document.cookie) return '';
      const match = document.cookie.match(/(?:^|;\s*)evkin_local_api_key=([^;]+)/);
      return match ? decodeURIComponent(match[1]) : '';
    } catch (_) { return ''; }
  }

  function storageGet() {
    try {
      const sessionVal = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(API_KEY_STORAGE) || '' : '';
      if (sessionVal) return sessionVal;
      const localVal = typeof localStorage !== 'undefined' ? localStorage.getItem(API_KEY_STORAGE) || '' : '';
      if (localVal) return localVal;
      return cookieGet();
    }
    catch (_) { return cookieGet(); }
  }

  function storageSet(value) {
    try {
      if (typeof sessionStorage !== 'undefined') {
        if (value) sessionStorage.setItem(API_KEY_STORAGE, value);
        else sessionStorage.removeItem(API_KEY_STORAGE);
      }
      if (typeof localStorage !== 'undefined') {
        if (value) localStorage.setItem(API_KEY_STORAGE, value);
        else localStorage.removeItem(API_KEY_STORAGE);
      }
    } catch (_) { /* Storage may be disabled by browser policy. */ }
  }

  backend.setApiKey = function (value) {
    backend.apiKey = String(value || '').trim();
    storageSet(backend.apiKey);
  };
  backend.getApiKey = function () { return backend.apiKey || storageGet(); };
  backend.registerAuthenticatedOrigin = function (value) {
    try { authenticatedOrigins.add(new URL(normalizeApiBase(value)).origin); } catch (_) { /* Invalid URLs are rejected elsewhere. */ }
  };
  backend.registerAuthenticatedOrigin(backend.apiBase);

  (function captureApiKeyFromFragment() {
    if (typeof location === 'undefined' || typeof URLSearchParams === 'undefined') return;
    const fragment = new URLSearchParams(String(location.hash || '').replace(/^#/, ''));
    const supplied = fragment.get('evkin-api-key');
    if (!supplied) return;
    backend.setApiKey(supplied);
    fragment.delete('evkin-api-key');
    if (typeof history !== 'undefined' && history.replaceState) {
      const remaining = fragment.toString();
      history.replaceState(null, '', location.pathname + location.search + (remaining ? '#' + remaining : ''));
    }
  })();

  function requestUrl(input) {
    const value = input && typeof input === 'object' && input.url ? input.url : input;
    try {
      const base = typeof location !== 'undefined' && location.href ? location.href : backend.apiBase + '/';
      return new URL(String(value), base);
    } catch (_) { return null; }
  }

  backend.acceptGeneration = function (value) {
    const generation = Number(value);
    if (!Number.isSafeInteger(generation) || generation < 0) return false;
    backend.runtimeGeneration = generation;
    backend.mutationBlocked = false;
    return true;
  };

  function apiErrorInfo(payload, fallback) {
    const root = payload && typeof payload === 'object' ? payload : {};
    const detail = root.detail !== undefined ? root.detail :
      (root.error !== undefined ? root.error : (root.message !== undefined ? root.message : payload));
    const code = detail && typeof detail === 'object' && !Array.isArray(detail)
      ? (detail.code || detail.error_code || root.code || root.error_code || '')
      : (root.code || root.error_code || '');
    const describe = (value) => {
      if (value === null || value === undefined || value === '') return '';
      if (typeof value === 'string') return value;
      if (Array.isArray(value)) return value.map(describe).filter(Boolean).join('; ');
      if (typeof value === 'object') {
        const direct = value.message || value.error_message || value.reason || value.msg;
        if (direct) {
          const location = Array.isArray(value.loc) ? value.loc.join('.') : '';
          return (location ? location + ': ' : '') + describe(direct);
        }
        const reasons = value.blocking_reasons || value.errors;
        if (reasons) return describe(reasons);
        try { return JSON.stringify(value); } catch (_) { return ''; }
      }
      return String(value);
    };
    const detailMessage = describe(detail);
    const prefix = code && !detailMessage.includes(code) ? code + ': ' : '';
    return { code:String(code || ''), message:prefix + (detailMessage || fallback), detail };
  }

  backend.apiErrorInfo = apiErrorInfo;

  async function authenticatedFetch(input, options) {
    if (!nativeFetch) return Promise.reject(new Error('Fetch API tidak tersedia.'));
    const target = requestUrl(input);
    const key = backend.getApiKey();
    const method = String((options && options.method) || (input && input.method) || 'GET').toUpperCase();
    const mutating = !['GET', 'HEAD', 'OPTIONS'].includes(method);
    const authenticatedTarget = !!(target && authenticatedOrigins.has(target.origin));
    if (mutating && authenticatedTarget && backend.mutationBlocked) {
      return Promise.reject(new Error('Data operasional telah direset dari sesi lain. Muat ulang halaman sebelum melanjutkan.'));
    }
    const next = { ...(options || {}) };
    if (authenticatedTarget && typeof Headers !== 'undefined') {
      const inherited = input && typeof input === 'object' ? input.headers : undefined;
      const headers = new Headers(next.headers || inherited || {});
      if (key && !headers.has(API_KEY_HEADER)) headers.set(API_KEY_HEADER, key);
      if (mutating && Number.isSafeInteger(backend.runtimeGeneration) && !headers.has('X-EVKIN-Generation')) {
        headers.set('X-EVKIN-Generation', String(backend.runtimeGeneration));
      }
      next.headers = headers;
    } else if (authenticatedTarget) {
      next.headers = { ...((input && input.headers) || {}), ...(next.headers || {}) };
      if (key && !next.headers[API_KEY_HEADER]) next.headers[API_KEY_HEADER] = key;
      if (mutating && Number.isSafeInteger(backend.runtimeGeneration) && !next.headers['X-EVKIN-Generation']) {
        next.headers['X-EVKIN-Generation'] = String(backend.runtimeGeneration);
      }
    }
    const response = await nativeFetch(input, authenticatedTarget ? next : options);
    const responseGeneration = Number(response.headers && response.headers.get('X-EVKIN-Generation'));
    if (response.ok && Number.isSafeInteger(responseGeneration)) {
      backend.runtimeGeneration = responseGeneration;
    }
    if (mutating && authenticatedTarget && (response.status === 409 || response.status === 423)) {
      const body = await response.clone().json().catch(() => ({}));
      const detail = body && body.detail;
      const code = detail && typeof detail === 'object' ? detail.code : '';
      if (code === 'STALE_CLIENT_AFTER_RESET' || code === 'RESET_IN_PROGRESS' || code === 'GENERATION_REQUIRED') {
        backend.mutationBlocked = true;
        const message = code === 'RESET_IN_PROGRESS'
          ? 'Reset data sedang berlangsung. Tunggu dan muat ulang halaman.'
          : 'Data operasional telah direset dari sesi lain. Halaman harus dimuat ulang sebelum melanjutkan.';
        if (EVKIN.toast && !backend.staleNoticeShown) EVKIN.toast(message, 'error');
        backend.staleNoticeShown = true;
      }
    }
    return response;
  }

  backend.apiFetch = authenticatedFetch;
  if (nativeFetch) globalThis.fetch = authenticatedFetch;

  backend.getAbsenceAuthority = async function (apiBase = backend.apiBase) {
    const response = await fetch(new URL('/api/absence/authority', normalizeApiBase(apiBase)).toString(), {
      method:'GET', cache:'no-store'
    });
    if (!response.ok) throw new Error('Status authority kalkulasi tidak tersedia: HTTP ' + response.status);
    const result = await response.json();
    backend.calculationAuthority = result.authority || 'JS_LEGACY';
    persistAuthority(backend.calculationAuthority);
    return result;
  };

  backend.startAbsenceCalculation = async function (period, actor, reason, apiBase = backend.apiBase) {
    const response = await fetch(new URL('/api/absence/calculate', normalizeApiBase(apiBase)).toString(), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({period, actor, reason})
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = result && result.detail;
      const message = detail && typeof detail === 'object' ? detail.message : detail;
      throw new Error(message || 'Calculation backend gagal dimulai: HTTP ' + response.status);
    }
    return result;
  };

  backend.getAbsenceCalculation = async function (calculationId, apiBase = backend.apiBase) {
    const url = new URL('/api/absence/calculation/' + encodeURIComponent(calculationId), normalizeApiBase(apiBase));
    const response = await fetch(url.toString(), {method:'GET', cache:'no-store'});
    if (!response.ok) throw new Error('Status calculation tidak tersedia: HTTP ' + response.status);
    return response.json();
  };

  backend.getLatestAbsenceCalculation = async function (period, apiBase = backend.apiBase) {
    const url = new URL('/api/absence/calculation/latest', normalizeApiBase(apiBase));
    url.searchParams.set('period', period);
    const response = await fetch(url.toString(), {method:'GET', cache:'no-store'});
    if (!response.ok) throw new Error('Calculation terbaru tidak tersedia: HTTP ' + response.status);
    return response.json();
  };

  backend.waitAbsenceCalculation = async function (calculationId, options = {}) {
    const timeoutMs = Math.max(1000, Number(options.timeoutMs) || 300000);
    const intervalMs = Math.max(50, Number(options.intervalMs) || 500);
    const deadline = Date.now() + timeoutMs;
    const terminal = new Set(['COMPLETED','SHADOW_ONLY','FAILED','STALE_SOURCE','STALE_GENERATION']);
    while (Date.now() <= deadline) {
      const batch = await backend.getAbsenceCalculation(calculationId, options.apiBase || backend.apiBase);
      if (terminal.has(batch.status)) {
        if (batch.status === 'COMPLETED' || batch.status === 'SHADOW_ONLY') return batch;
        const error = new Error(batch.error_message || `Calculation berhenti dengan status ${batch.status}.`);
        error.code = batch.error_code || batch.status;
        error.batch = batch;
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
    const error = new Error('Calculation backend melewati batas waktu tunggu.');
    error.code = 'CALCULATION_POLL_TIMEOUT';
    throw error;
  };

  backend.hydrateCanonicalAbsence = async function (period, calculationId) {
    if (!EVKIN.loadWorkflowSource) throw new Error('Hydrator workflow backend tidak tersedia.');
    const daily = await EVKIN.loadWorkflowSource('daily_data', {all:true, force:true, periode:period});
    const rekap = await EVKIN.loadWorkflowSource('rekap_sdm', {all:true, force:true, periode:period});
    const finalRows = await EVKIN.loadWorkflowSource('final_absensi', {all:true, force:true, periode:period});
    const sets = [daily, rekap, finalRows];
    if (calculationId && sets.some(rows => rows.some(row => row.calculation_id !== calculationId))) {
      throw new Error('Hydration canonical memuat calculation_id yang tidak konsisten.');
    }
    return {daily, rekap, final_absensi:finalRows, calculation_id:calculationId || null};
  };

  backend.runCanonicalAbsenceCalculation = async function (period, actor, reason, options = {}) {
    const authority = await backend.getAbsenceAuthority(options.apiBase || backend.apiBase);
    if (authority.authority !== 'PYTHON_CANONICAL') {
      throw new Error('Authority aktif bukan PYTHON_CANONICAL.');
    }
    const pending = await backend.startAbsenceCalculation(period, actor, reason, options.apiBase || backend.apiBase);
    const completed = await backend.waitAbsenceCalculation(pending.calculation_id, options);
    if (completed.status !== 'COMPLETED' || !completed.canonical) {
      throw new Error('Calculation selesai tanpa hasil canonical.');
    }
    const hydrated = await backend.hydrateCanonicalAbsence(period, completed.calculation_id);
    return {batch:completed, hydrated};
  };

  backend.downloadAuthenticated = async function (value) {
    const response = await authenticatedFetch(value, { method: 'GET', cache: 'no-store' });
    if (!response.ok) throw new Error('Download gagal: HTTP ' + response.status);
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    const filename = match ? decodeURIComponent(match[1].replace(/\"$/, '')) : 'download';
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl; anchor.download = filename; anchor.hidden = true;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
  };

  backend.openAuthenticated = async function (value) {
    const response = await authenticatedFetch(value, { method: 'GET', cache: 'no-store' });
    if (!response.ok) throw new Error('Permintaan gagal: HTTP ' + response.status);
    const objectUrl = URL.createObjectURL(await response.blob());
    window.open(objectUrl, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  };

  if (typeof window !== 'undefined' && typeof window.open === 'function') {
    const nativeOpen = window.open.bind(window);
    window.open = function (value, target, features) {
      const destination = requestUrl(value);
      if (destination && authenticatedOrigins.has(destination.origin) && /^\/(api|analytics|pengembalian)(\/|$)/.test(destination.pathname)) {
        backend.openAuthenticated(destination.toString()).catch(error => {
          if (EVKIN.toast) EVKIN.toast(error.message, 'error');
        });
        return null;
      }
      return nativeOpen(value, target, features);
    };
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', event => {
      const anchor = event.target && event.target.closest ? event.target.closest('a[href]') : null;
      if (!anchor) return;
      const target = requestUrl(anchor.href);
      if (!target || !authenticatedOrigins.has(target.origin) || !/^\/(api|analytics|pengembalian)(\/|$)/.test(target.pathname)) return;
      event.preventDefault();
      backend.downloadAuthenticated(target.toString()).catch(error => {
        if (EVKIN.toast) EVKIN.toast(error.message, 'error');
      });
    }, true);
  }
  backend.isHttpServed = function () {
    return typeof window !== 'undefined' && window.location.protocol.startsWith('http');
  };

  backend.health = async function (apiBase = backend.apiBase) {
    const base = normalizeApiBase(apiBase);
    const response = await fetch(new URL('/health', base).toString(), {
      method: 'GET',
      cache: 'no-store'
    });
    if (!response.ok) throw new Error('Backend merespons dengan status ' + response.status);
    const result = await response.json();
    if (result.status !== 'ok') throw new Error('Status backend tidak valid');
    return result;
  };

  backend.activate = async function (apiBase = backend.apiBase) {
    const requestedBase = normalizeApiBase(apiBase);
    const candidates = [requestedBase];
    if (typeof window !== 'undefined' && window.location && requestedBase === window.location.origin.replace(/\/$/, '')) {
      const localBackend = 'http://127.0.0.1:8000';
      if (!candidates.includes(localBackend)) candidates.push(localBackend);
    }

    let lastError = null;
    for (const candidate of candidates) {
      try {
        await backend.health(candidate);
        backend.apiBase = candidate;
        backend.registerAuthenticatedOrigin(candidate);
        backend.analyticsBase = backend.apiBase.replace(/\/$/, '') + '/analytics';
        lastError = null;
        break;
      } catch (error) {
        lastError = error;
      }
    }
    if (lastError) throw lastError;

    let info = null;
    try {
      const response = await fetch(new URL('/api/info', backend.apiBase).toString(), {
        method: 'GET',
        cache: 'no-store'
      });
      if (response.ok) {
        info = await response.json().catch(() => null);
      }
    } catch (err) {
      console.warn('Backend info check failed, assuming non-export backend:', err);
    }

    const hasExportApi = !!(info && (info.service === 'evkin-export-api' || info.frontend_served !== undefined));
    if (!hasExportApi) {
      backend.enabled = false;
      throw new Error('Backend yang terhubung bukan EVKIN export API. Export job tidak tersedia.');
    }

    backend.enabled = true;
    return backend.apiBase;
  };

  function workflowJsonBody(workflow) {
    // A workflow can exceed the engine's maximum string length. Snapshot rows
    // separately into bounded Blob parts; never join the entire JSON document.
    // Keep one request so the backend still commits every table atomically.
    const parts = [];
    let pending = [], chars = 0;
    const flush = () => {
      if (!pending.length) return;
      parts.push(new Blob(pending));
      pending = [];
      chars = 0;
    };
    const append = text => {
      if (chars + text.length > 1024 * 1024 || pending.length >= 500) flush();
      pending.push(text);
      chars += text.length;
    };
    append('{');
    let first = true;
    for (const [source, value] of Object.entries(workflow || {})) {
      const isTable = Array.isArray(value);
      const json = isTable ? null : JSON.stringify(value);
      if (!isTable && json === undefined) continue;
      append((first ? '' : ',') + JSON.stringify(source) + ':');
      first = false;
      if (isTable) {
        append('[');
        for (let index = 0; index < value.length; index++) {
          if (index) append(',');
          append(JSON.stringify(value[index]) ?? 'null');
        }
        append(']');
      } else append(json);
    }
    append('}');
    flush();
    return new Blob(parts, {type: 'application/json'});
  }

  backend.syncWorkflow = async function (workflow, apiBase = backend.apiBase) {
    if (EVKIN.deletingAbsensi) throw new Error('Reset sedang berlangsung; sinkronisasi ditunda.');
    const onProgress = arguments.length > 2 && typeof arguments[2] === 'function' ? arguments[2] : null;
    // Capture one immutable snapshot and commit all tables in a single transaction.
    const body = workflowJsonBody(workflow);
    const write = async () => {
      if (onProgress) onProgress('Workflow', 0, 1);
      const response = await fetch(new URL('/api/workflow/sync', apiBase), {
        method:'POST', headers:{'Content-Type':'application/json'}, body,
        signal:AbortSignal.timeout(300000)
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      if (onProgress) onProgress('Workflow', 1, 1);
      return result;
    };
    const result = backend.syncQueue.then(write, write);
    backend.syncQueue = result.catch(() => undefined);
    return result;
  };

  async function writeTable(source, rows, replace, apiBase, scope) {
    const response = await fetch(new URL('/api/workflow/sync-table', normalizeApiBase(apiBase)).toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, rows, replace,
        workflow_id:(scope && scope.workflow_id) || 'default',
        periode:(scope && scope.periode) || null }),
      signal: AbortSignal.timeout(300000)
    });
    if (!response.ok) {
      const text = await response.text();
      let payload = text;
      try {
        payload = JSON.parse(text);
      } catch (_) { /* Keep the plain response body. */ }
      const info = apiErrorInfo(payload, `Sinkronisasi tabel ke backend gagal: ${source} (HTTP ${response.status})`);
      const error = new Error(info.message);
      error.code = info.code || `HTTP_${response.status}`;
      error.detail = info.detail;
      error.status = response.status;
      error.source = source;
      throw error;
    }
    return response.json();
  }

  // Serialize table writes with all other workflow writes. Import chunks must
  // arrive in order: first chunk initializes, following chunks append.
  backend.syncTable = function (source, rows, replace, apiBase = backend.apiBase, scope = {}) {
    const snapshot = Array.isArray(rows) ? rows.slice() : [];
    const write = () => writeTable(source, snapshot, replace, apiBase, scope);
    const result = backend.syncQueue.then(write, write);
    backend.syncQueue = result.catch(() => undefined);
    return result;
  };

  backend.syncTableChunks = async function (source, rows, options = {}) {
    let sourceRows = Array.isArray(rows) ? rows : [];
    // JSON.stringify omits undefined object properties.  Without a stable
    // projection, an otherwise valid raw-import column can disappear from one
    // chunk and make the following append fail schema validation.
    if (source === 'raw_import' && sourceRows.length) {
      const columns = [...new Set(sourceRows.flatMap(row => Object.keys(row || {})))];
      sourceRows = sourceRows.map(row => Object.fromEntries(
        columns.map(column => [column, row && row[column] !== undefined ? row[column] : null])
      ));
    }
    const chunkSize = Math.max(5000, Math.min(10000, Number(options.chunkSize) || 7500));
    const totalChunks = Math.max(1, Math.ceil(sourceRows.length / chunkSize));
    let result = null;
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
      const start = chunkIndex * chunkSize;
      const chunk = sourceRows.slice(start, start + chunkSize);
      result = await backend.syncTable(source, chunk, chunkIndex === 0, options.apiBase || backend.apiBase, {
        workflow_id:options.workflow_id || 'default', periode:options.periode || null
      });
      if (typeof options.onProgress === 'function') {
        options.onProgress(source, chunkIndex + 1, totalChunks, Math.min(start + chunk.length, sourceRows.length));
      }
    }
    return result;
  };

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      URL.revokeObjectURL(url);
      a.remove();
    }, 1200);
  }

  EVKIN.exportBackend.downloadFinalExcel = async function (opts = {}) {
    const {
      bulan = '2026-08',
      unit = null,
      smartMode = false,
      apiBase = backend.apiBase
    } = opts;

    const url = new URL('/api/export/final', apiBase);
    url.searchParams.set('bulan', bulan);
    if (unit) url.searchParams.set('unit', unit);
    url.searchParams.set('smart_mode', String(Boolean(smartMode)));

    const response = await fetch(url.toString(), {
      method: 'POST'
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || 'Export backend gagal');
    }

    const blob = await response.blob();
    const filename = `EVKIN_FINAL_${bulan}_${smartMode ? 'SMART' : 'FULL'}.xlsx`;
    triggerDownload(blob, filename);
    return { ok: true, filename };
  };

  EVKIN.exportBackend.downloadWorkbook = async function (opts = {}) {
    const {
      bulan = '2026-08',
      unit = null,
      sheetNames = ['HITUNG HARIAN', 'REKAP SDM', 'FINAL ABSENSI'],
      apiBase = backend.apiBase
    } = opts;

    const url = new URL('/api/export/workbook', apiBase);
    url.searchParams.set('bulan', bulan);
    if (unit) url.searchParams.set('unit', unit);
    if (sheetNames && sheetNames.length) {
      url.searchParams.set('sheet_names', sheetNames.join(','));
    }

    const response = await fetch(url.toString(), {
      method: 'POST'
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || 'Export workbook gagal');
    }

    const blob = await response.blob();
    triggerDownload(blob, `EVKIN_EXPORT_${bulan}.xlsx`);
    return { ok: true, filename: `EVKIN_EXPORT_${bulan}.xlsx` };
  };

  EVKIN.exportBackend.seedDemoData = async function (apiBase = backend.apiBase) {
    const response = await fetch(new URL('/api/export/seed-demo', apiBase).toString(), {
      method: 'POST'
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || 'Seed demo data gagal');
    }
    return response.json();
  };

  // Background export job APIs: create job, poll status, download result
  backend.finalManifest = async function (rows) {
    const values = rows.map(row => {
      const nip = String(row.nip || '').trim();
      if (!nip) throw new Error('NIP Final kosong');
      return [nip, ...['tukin', 'nominal', 'finalDibayar'].map(key => {
        if (row[key] == null || row[key] === '' || !Number.isFinite(Number(row[key]))) throw new Error('Nominal Final belum lengkap');
        const amount = Number(row[key]);
        return String(Math.sign(amount) * Math.round(Math.abs(amount)));
      })];
    });
    const input = values.map(row => JSON.stringify(row)).sort().join('\n');
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
    const sum = i => values.reduce((total, row) => total + BigInt(row[i]), 0n).toString();
    return {row_count: values.length, unique_nip: new Set(values.map(row => row[0])).size,
      gross: sum(1), deduction: sum(2), paid: sum(3),
      sha256: Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')};
  };

  EVKIN.exportBackend.createExportJob = async function (opts = {}) {
    const { bulan = '2026-08', unit = null, sheetNames = [], format = 'xlsx', apiBase = backend.apiBase } = opts;
    const url = new URL('/api/jobs/export', apiBase);
    const response = await fetch(url.toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bulan, unit, sheet_names: sheetNames, format, expected_final: opts.expectedFinal }),
      signal: AbortSignal.timeout(30000)
    });
    if (!response.ok) {
      const text = await response.text().catch(() => '<no body>');
      throw new Error('Create export job failed: ' + (text || response.status));
    }
    return response.json();
  };

  EVKIN.exportBackend.pollJob = async function (jobId, opts = {}) {
    const { apiBase = backend.apiBase, intervalMs = 2000, onProgress = null, timeoutMs = 60 * 60 * 1000 } = opts;
    const start = Date.now();
    const url = new URL('/api/jobs/' + jobId, apiBase).toString();
    while (true) {
      const resp = await fetch(url, { method: 'GET', cache: 'no-store', signal: AbortSignal.timeout(30000) });
      if (!resp.ok) {
        const text = await resp.text().catch(() => '<no body>');
        throw new Error('Polling job failed: ' + text);
      }
      const json = await resp.json();
      if (onProgress && typeof onProgress === 'function') {
        try { onProgress(json.progress || 0, json.message || json.status); } catch (e) { /* ignore */ }
      }
      if (json.status === 'finished') return json;
      if (json.status === 'failed' || json.status === 'cancelled') throw new Error('Job ' + json.status + ': ' + (json.message || ''));
      if (Date.now() - start > timeoutMs) throw new Error('Job polling timeout');
      await new Promise(r => setTimeout(r, intervalMs));
    }
  };

  EVKIN.exportBackend.downloadJobResult = async function (jobId, opts = {}) {
    const { apiBase = backend.apiBase } = opts;
    const url = new URL('/api/jobs/' + jobId + '/download', apiBase).toString();
    const resp = await fetch(url, { method: 'GET' });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '<no body>');
      throw new Error('Download job result failed: ' + text);
    }
    const blob = await resp.blob();
    const disp = resp.headers.get('content-disposition') || '';
    let filename = 'evkin_export_' + jobId;
    const m = /filename="?([^";]+)"?/.exec(disp);
    if (m) filename = m[1];
    triggerDownload(blob, filename);
    return { ok: true, filename };
  };

  EVKIN.exportBackend.startExportJob = async function (opts = {}, onProgress = null) {
    const create = await EVKIN.exportBackend.createExportJob(opts);
    const jobId = create.job_id || create.jobId || create.id;
    if (!jobId) throw new Error('Invalid job id from server');
    const pol = await EVKIN.exportBackend.pollJob(jobId, { apiBase: opts.apiBase || backend.apiBase, onProgress });
    // when finished, download
    return EVKIN.exportBackend.downloadJobResult(jobId, { apiBase: opts.apiBase || backend.apiBase });
  };


  // Lightweight mark: avoid resending the full workflow payload when only the semifinal verified flag changes.
  EVKIN.exportBackend.markSemifinalVerified = async function (opts = {}) {
    if (typeof opts === 'string') opts = { bulan: opts };
    const { bulan = '2026-08', verified = true, apiBase = backend.apiBase, timeoutMs = 60000 } = opts;
    const url = new URL('/api/workflow/mark-semifinal', apiBase);
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url.toString(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bulan, verified }),
        signal: controller.signal
      });
      clearTimeout(id);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const detail = data.detail || data;
        const reasons = detail.blocking_reasons || (detail.message ? [detail.message] : []);
        const error = new Error(reasons.join(' ') || 'Gagal menyimpan verifikasi Semi Final.');
        error.workflow = detail;
        throw error;
      }
      return response.json();
    } catch (err) {
      clearTimeout(id);
      // Normalize abort error for clearer messaging
      if (err && err.name === 'AbortError') {
        console.warn('markSemifinalVerified: request aborted after timeout', timeoutMs);
        throw new Error('Timeout saat menandai verifikasi semifinal ke backend');
      }
      console.error('markSemifinalVerified network/error:', err);
      throw err;
    }
  };

  // Analytics helpers (use analytics mounted under /analytics on same backend or standalone service)
  backend.analyticsBase = backend.apiBase + '/analytics';

  EVKIN.exportBackend.getWorkflowFlags = async function (bulan = null, apiBase = null) {
    // Try main API path first (/api/workflow/flags), then analytics sub-app if available.
    // Missing or partial backend responses should not surface as a red frontend "Server error"
    // because the workflow can still continue without the flag metadata being present.
    const mainBase = backend.apiBase;
    const analyticsBase = backend.analyticsBase || (backend.apiBase + '/analytics');
    const tryUrls = [];
    tryUrls.push(new URL('/api/workflow/flags', mainBase).toString());
    tryUrls.push(new URL('workflow/flags', analyticsBase.replace(/\/$/, '') + '/').toString());

    let lastErr = null;
    for (const u of tryUrls) {
      const url = new URL(u);
      if (bulan) url.searchParams.set('bulan', bulan);
      try {
        const response = await fetch(url.toString(), { method: 'GET' });
        if (!response.ok) {
          const text = await response.text().catch(() => '<no body>');
          console.warn('Failed to fetch workflow flags from', url.toString(), 'status', response.status, 'body:', text);
          lastErr = new Error('HTTP ' + response.status + ': ' + text);
          continue;
        }
        const data = await response.json().catch(async () => {
          const raw = await response.text().catch(() => '<no body>');
          console.warn('Invalid JSON from workflow flags endpoint', url.toString(), raw);
          return null;
        });
        if (!data) {
          lastErr = new Error('Invalid JSON in response from ' + url.toString());
          continue;
        }
        return data;
      } catch (err) {
        console.warn('Network/error fetching workflow flags from', url.toString(), err);
        lastErr = err;
        continue;
      }
    }

    const fallback = { status: 'ok', flags: {} };
    if (lastErr) console.warn('getWorkflowFlags: all attempts failed; returning empty flag set:', lastErr);
    return fallback;
  };

  EVKIN.exportBackend.getStatsCounts = async function (bulan = null, apiBase = backend.analyticsBase) {
    const url = new URL('stats/counts', apiBase.replace(/\/$/, '') + '/');
    if (bulan) url.searchParams.set('bulan', bulan);
    try {
      const response = await fetch(url.toString(), { method: 'GET' });
      if (!response.ok) {
        const text = await response.text().catch(() => '<no body>');
        console.error('getStatsCounts failed', url.toString(), 'status', response.status, 'body:', text);
        throw new Error('Gagal ambil stats: ' + response.status + ' - ' + text);
      }
      return response.json();
    } catch (err) {
      console.error('getStatsCounts network/error:', err);
      throw err;
    }
  };

  EVKIN.exportBackend.downloadAggregatesCSV = async function (tipe = 'per-unit', bulan = null, unit = null, apiBase = backend.analyticsBase) {
    const url = new URL('export/aggregates', apiBase.replace(/\/$/, '') + '/');
    url.searchParams.set('tipe', tipe);
    if (bulan) url.searchParams.set('bulan', bulan);
    if (unit) url.searchParams.set('unit', unit);
    const response = await fetch(url.toString(), { method: 'GET' });
    if (!response.ok) {
      const text = await response.text().catch(() => '<no body>');
      console.error('downloadAggregatesCSV failed', url.toString(), 'status', response.status, 'body:', text);
      throw new Error(text || 'Gagal download aggregates');
    }
    const blob = await response.blob();
    const filename = `evkin_aggregates_${tipe}_${(unit?unit+'_' : '')}${bulan || 'all'}.csv`;
    triggerDownload(blob, filename);
    return { ok: true, filename };
  };

  // Lightweight connectivity test endpoint used by UI to verify network/backend reachability.
  EVKIN.exportBackend.testConnectivity = async function (apiBase = backend.apiBase, timeoutMs = 5000) {
    const mainPing = new URL('/api/ping', backend.apiBase).toString();
    const analyticsBase = backend.analyticsBase || (backend.apiBase + '/analytics');
    const analyticsPing = new URL('ping', analyticsBase.replace(/\/$/, '') + '/').toString();
    const tryUrls = [mainPing, analyticsPing];
    let lastErr = null;
    for (const u of tryUrls) {
      try {
        const controller = new AbortController();
        const id = setTimeout(() => controller.abort(), timeoutMs);
        const resp = await fetch(u, { method: 'GET', cache: 'no-store', signal: controller.signal });
        clearTimeout(id);
        if (!resp.ok) {
          const text = await resp.text().catch(() => '<no body>');
          console.error('testConnectivity failed', u, 'status', resp.status, 'body:', text);
          lastErr = new Error('HTTP ' + resp.status + ': ' + text);
          continue;
        }
        const data = await resp.json().catch(async () => {
          const raw = await resp.text().catch(() => '<no body>');
          console.error('testConnectivity invalid json from', u, 'body:', raw);
          return null;
        });
        if (!data) {
          lastErr = new Error('Invalid JSON from ' + u);
          continue;
        }
        return { ok: true, url: u, data };
      } catch (err) {
        console.error('testConnectivity network/error for', u, err && err.name ? err.name : err);
        lastErr = err;
        continue;
      }
    }
    console.error('testConnectivity: all attempts failed', lastErr);
    throw lastErr || new Error('Tidak bisa menghubungi backend');
  };

})();

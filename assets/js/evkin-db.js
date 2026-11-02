/* EVKIN ABSENSI v3.1 - evkin-db.js
   IndexedDB wrapper for large dataset storage using Dexie-like patterns.
   This keeps the app compatible with local file:// environments.
*/
(function () {
  const dbName = 'EVKIN_ABSENSI_DB';
  const DB_VERSION = 4;
  const DEFAULT_TIMEOUT_MS = 5000;
  const STALE_CACHE_KEY = 'evkin.cacheStaleAfterReset';

  function openDB(timeoutMs = DEFAULT_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(dbName, DB_VERSION);
      let settled = false;
      const finish = (callback, value) => {
        if (settled) {
          if (callback === resolve && value && typeof value.close === 'function') value.close();
          return;
        }
        settled = true;
        clearTimeout(timer);
        callback(value);
      };
      const timer = setTimeout(() => finish(reject, Object.assign(
        new Error('Waktu membuka cache browser habis.'), { code: 'OPEN_TIMEOUT' }
      )), Math.max(1, Number(timeoutMs) || DEFAULT_TIMEOUT_MS));
      request.onupgradeneeded = (event) => {
        const db = event.target.result;
        // Define stores with keyPath that allows auto-indexing
        // Since data objects have no 'id', use autoIncrement only (no keyPath)
        const stores = [
          'master_data',
          'workflow_config',
          'raw_import',
          'daily_data',
          'rekap_sdm',
          'final_absensi',
          'final_skp',
          'final_kombinasi',
          'final_combined_skp'
        ];
        stores.forEach((storeName) => {
          if (!db.objectStoreNames.contains(storeName)) {
            // Remove keyPath requirement - let IndexedDB auto-generate keys
            db.createObjectStore(storeName, { autoIncrement: true });
          }
        });
      };
      request.onsuccess = () => {
        request.result.onversionchange = () => request.result.close();
        finish(resolve, request.result);
      };
      request.onerror = () => finish(reject, request.error || new Error('Gagal membuka IndexedDB'));
      request.onblocked = () => finish(reject, Object.assign(
        new Error('Cache browser sedang digunakan tab EVKIN lain.'), { code: 'OPEN_BLOCKED' }
      ));
    });
  }

  async function useStore(storeName, mode, operation) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      let result;
      let failure;
      let tx;
      const close = () => {
        try { db.close(); } catch (_) { /* already closed */ }
      };
      try {
        tx = db.transaction(storeName, mode);
        tx.oncomplete = () => { close(); resolve(result); };
        tx.onerror = () => { close(); reject(failure || tx.error || new Error('Transaksi cache browser gagal.')); };
        tx.onabort = () => { close(); reject(failure || tx.error || new Error('Transaksi cache browser dibatalkan.')); };
        operation(tx.objectStore(storeName), value => { result = value; }, error => { failure = error; });
      } catch (error) {
        close();
        reject(error);
      }
    });
  }

  function cacheStorage() {
    // This stores safety metadata only, never workflow data. Persistence is
    // required so stale IndexedDB cannot be trusted after all tabs are closed.
    try { return typeof localStorage !== 'undefined' ? localStorage : null; }
    catch (_) { return null; }
  }

  const EVKIN = window.EVKIN || {};
  EVKIN.db = {
    async putMany(storeName, rows) {
      if (!rows || !rows.length) return true;
      return useStore(storeName, 'readwrite', (store, setResult, setFailure) => {
        rows.forEach((row) => {
          const payload = typeof row === 'object' && row !== null ? { ...row } : row;
          const request = store.put(payload);
          request.onerror = () => setFailure(request.error || new Error('Put gagal'));
        });
        setResult(true);
      });
    },

    async replaceStore(storeName, rows) {
      return useStore(storeName, 'readwrite', (store, setResult, setFailure) => {
        const request = store.clear();
        request.onsuccess = () => {
          (rows || []).forEach((row) => {
            const payload = typeof row === 'object' && row !== null ? { ...row } : row;
            const putReq = store.put(payload);
            putReq.onerror = () => setFailure(putReq.error || new Error('Replace gagal'));
          });
          setResult(true);
        };
        request.onerror = () => setFailure(request.error || new Error('Clear gagal'));
      });
    },

    async getAll(storeName) {
      return useStore(storeName, 'readonly', (store, setResult, setFailure) => {
        const request = store.getAll();
        request.onsuccess = () => setResult(request.result || []);
        request.onerror = () => setFailure(request.error || new Error('GetAll gagal'));
      });
    },

    async clear(storeName) {
      return useStore(storeName, 'readwrite', (store, setResult, setFailure) => {
        const request = store.clear();
        request.onsuccess = () => setResult(true);
        request.onerror = () => setFailure(request.error || new Error('Clear gagal'));
      });
    },

    async saveMasterData(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('master_data', rows);
    },

    async saveConfig(config) {
      return this.replaceStore('workflow_config', config ? [config] : []);
    },

    async saveRaw(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('raw_import', rows);
    },

    async saveDaily(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('daily_data', rows);
    },

    async saveRekap(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('rekap_sdm', rows);
    },

    async saveFinal(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('final_absensi', rows);
    },

    async saveFinalSKP(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('final_skp', rows);
    },

    async saveFinalKombinasi(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('final_kombinasi', rows);
    },

    async saveFinalCombinedSKP(rows) {
      if (!rows || !rows.length) return true;
      return this.replaceStore('final_combined_skp', rows);
    },

    async getMasterData() {
      return this.getAll('master_data');
    },

    async getRekap() {
      return this.getAll('rekap_sdm');
    },

    async getFinal() {
      return this.getAll('final_absensi');
    },

    markCacheStale() {
      const storage = cacheStorage();
      if (storage) storage.setItem(STALE_CACHE_KEY, '1');
    },

    isCacheStale() {
      const storage = cacheStorage();
      return !!(storage && storage.getItem(STALE_CACHE_KEY) === '1');
    },

    resetOutcome(backendSucceeded, cacheResult) {
      if (!backendSucceeded) return {
        status: 'RESET_FAILED',
        message: 'Reset data server gagal. Data operasional belum dikonfirmasi terhapus.'
      };
      if (cacheResult && cacheResult.status === 'SUCCESS') return {
        status: 'SUCCESS',
        message: 'Reset Data Operasional berhasil.'
      };
      const cacheStatus = cacheResult && cacheResult.status || 'ERROR';
      return {
        status: 'PARTIAL_SUCCESS', cacheStatus,
        message: cacheStatus === 'BLOCKED'
          ? 'Reset data server berhasil, tetapi cache browser belum dapat dibersihkan karena masih digunakan oleh tab EVKIN lain. Tutup tab EVKIN lainnya lalu pilih Bersihkan Cache Browser.'
          : 'Reset data server berhasil. Cache browser belum berhasil dibersihkan. Pilih Bersihkan Cache Browser; jangan ulangi reset server.'
      };
    },

    async resetDatabase(options = {}) {
      const timeoutMs = Math.max(1, Number(options.timeoutMs) || DEFAULT_TIMEOUT_MS);
      return new Promise((resolve) => {
        const request = indexedDB.deleteDatabase(dbName);
        let settled = false;
        const finish = (status, error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (status === 'SUCCESS') {
            const storage = cacheStorage();
            if (storage) storage.removeItem(STALE_CACHE_KEY);
          }
          resolve({ ok: status === 'SUCCESS', status, error: error ? String(error.message || error) : null });
        };
        const timer = setTimeout(() => finish('TIMEOUT', new Error('Waktu pembersihan cache browser habis.')), timeoutMs);
        request.onsuccess = () => finish('SUCCESS');
        request.onerror = () => finish('ERROR', request.error || new Error('Gagal membersihkan cache browser.'));
        request.onblocked = () => finish('BLOCKED', new Error('Cache browser masih digunakan tab EVKIN lain.'));
      });
    }
  };
})();

/* EVKIN ABSENSI v3.1 - final.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, S = EVKIN.state, C = EVKIN.calc;
  const mod = EVKIN.finalMod = {};
  let tab = 'absensi';
  let nipSearch = '';

  // ---- HELPER: Import File Utils ----
  const IMPORT_HEADER_MAP = [
    ['nip', ['nip', 'nomorpegawai', 'noindukpegawai']],
    ['potongan', ['potongan', 'potong', 'pct', 'persentase', 'nilai', 'nilaipotongan', 'skp', 'skppct', 'eabsensi', 'esdm', 'persen']],
    ['eabsensi', ['eabsensi', 'eabs', 'absensi', 'nilai_eabsensi', 'potongan_eabs']],
    ['esdm', ['esdm', 'nilai_esdm', 'potongan_esdm', 'sdm']]
  ];

  function normHeader(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  function detectImportColumns(headers) {
    const map = {};
    headers.forEach((h, i) => {
      const nh = normHeader(h);
      for (const [field, aliases] of IMPORT_HEADER_MAP) {
        if (map[field] !== undefined) continue;
        if (aliases.includes(nh)) { map[field] = i; return; }
      }
    });
    // fallback: contains match
    headers.forEach((h, i) => {
      const nh = normHeader(h);
      for (const [field, aliases] of IMPORT_HEADER_MAP) {
        if (map[field] !== undefined) continue;
        if (aliases.some(a => nh.includes(a))) map[field] = i;
      }
    });
    return map;
  }

  function readFileAsArray(file, cb) {
    const reader = new FileReader();
    reader.onload = function (e) {
      try {
        const data = new Uint8Array(e.target.result);
        const wb = XLSX.read(data, { type: 'array', raw: true, cellDates: false });
        const ws = wb.Sheets[wb.SheetNames[0]];
        const aoa = XLSX.utils.sheet_to_json(ws, { header: 1, raw: true, defval: '' });
        cb(null, aoa);
      } catch (err) {
        cb(err);
      }
    };
    reader.onerror = () => cb(new Error('Gagal membaca file'));
    reader.readAsArrayBuffer(file);
  }

  // ---- Import SKP ----
  mod.importSkpFile = function (file, cb) {
    readFileAsArray(file, (err, aoa) => {
      if (err) return cb(err);
      if (!aoa || aoa.length < 2) return cb(new Error('File kosong'));

      const headers = aoa[0].map(x => String(x).trim());
      const colMap = detectImportColumns(headers);

      if (colMap.nip === undefined || colMap.potongan === undefined) {
        return cb(new Error('Kolom NIP dan Potongan/Nilai tidak ditemukan'));
      }

      let imported = 0, notfound = 0;
      aoa.slice(1).forEach(row => {
        const nip = String(row[colMap.nip] || '').trim();
        const val = parseFloat(row[colMap.potongan]) || 0;
        if (!nip) return;

        const existing = S.final.find(f => String(f.nip).trim() === nip);
        if (!existing) { notfound++; return; }

        const sr = S.finalSKP.rows.find(x => x.nip === nip);
        if (sr) {
          sr.skpPct = val;
        } else {
          S.finalSKP.rows.push({ nip, skpPct: val });
        }
        imported++;
      });

      EVKIN.syncBackendWorkflow().catch((syncError) => console.warn('Gagal sinkron import SKP ke backend:', syncError));
      if (EVKIN.db && EVKIN.db.saveFinalSKP) EVKIN.db.saveFinalSKP(S.finalSKP.rows).catch((syncError) => console.warn('Gagal simpan SKP ke IndexedDB:', syncError));
      cb(null, { imported, notfound });
    });
  };

  // ---- Import Kombinasi (e-Absensi & e-SDM) ----
  mod.importKombinasiFile = function (file, source, cb) {
    // source: 'eabsensi' atau 'esdm'
    readFileAsArray(file, (err, aoa) => {
      if (err) return cb(err);
      if (!aoa || aoa.length < 2) return cb(new Error('File kosong'));

      const headers = aoa[0].map(x => String(x).trim());
      const colMap = detectImportColumns(headers);

      if (colMap.nip === undefined) {
        return cb(new Error('Kolom NIP tidak ditemukan'));
      }

      const fieldToCheck = source === 'eabsensi' ? colMap.eabsensi : colMap.esdm;
      if (fieldToCheck === undefined && colMap.potongan === undefined) {
        return cb(new Error('Kolom potongan/nilai untuk ' + source + ' tidak ditemukan'));
      }

      let imported = 0, notfound = 0;
      const valCol = fieldToCheck !== undefined ? fieldToCheck : colMap.potongan;

      aoa.slice(1).forEach(row => {
        const nip = String(row[colMap.nip] || '').trim();
        const val = parseFloat(row[valCol]) || 0;
        if (!nip) return;

        const existing = S.final.find(f => String(f.nip).trim() === nip);
        if (!existing) { notfound++; return; }

        const kr = S.finalKombinasi.rows.find(x => x.nip === nip);
        if (kr) {
          kr[source === 'eabsensi' ? 'eabs' : 'esdm'] = val;
        } else {
          S.finalKombinasi.rows.push({ nip, eabs: source === 'eabsensi' ? val : 0, esdm: source === 'esdm' ? val : 0 });
        }
        imported++;
      });

      EVKIN.syncBackendWorkflow().catch((syncError) => console.warn('Gagal sinkron import kombinasi ke backend:', syncError));
      if (EVKIN.db && EVKIN.db.saveFinalKombinasi) EVKIN.db.saveFinalKombinasi(S.finalKombinasi.rows).catch((syncError) => console.warn('Gagal simpan kombinasi ke IndexedDB:', syncError));
      cb(null, { imported, notfound });
    });
  };

  const KEHADIRAN = 0.30, KINERJA = 0.70;

  function roundMoney(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.round(n) : 0;
  }

  function safeNumericInputValue(value) {
    if (value === null || value === undefined || value === '') return '';
    const number = Number(value);
    return Number.isFinite(number) ? String(number) : '';
  }

  function nextPaymentPeriod(period) {
    const [year, month] = String(period || '').split('-').map(Number);
    if (!year || !month) return '';
    return `${year + (month === 12 ? 1 : 0)}-${String(month === 12 ? 1 : month + 1).padStart(2, '0')}`;
  }

  function filteredFinal() {
    return nipSearch ? S.final.filter(x => String(x.nip || '').toLowerCase().includes(nipSearch)) : S.final;
  }

  let workflowMaster = null;
  function masterTukinOf(nip) {
    if (workflowMaster) {
      const key = String(nip || '').trim();
      if (!workflowMaster.has(key)) return { tukin: null, status: 'NIP TIDAK DITEMUKAN PADA MASTER SDM AKTIF' };
      const value = workflowMaster.get(key).tukin;
      if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) {
        return { tukin: null, status: 'TUKIN BRUTO BELUM TERSEDIA' };
      }
      return { tukin: Number(value), status: 'VALID' };
    }
    return { tukin: null, status: 'MASTER SDM BELUM DIBACA DARI SERVER' };
  }

  function finalNeedsMaster() {
    return !S.final.length || S.final.some(row => row.tukin === undefined || row.tukin === null || row.tukin === '');
  }

  let renderGeneration = 0;

  // Match only the participants in this workflow. Never replace the global
  // SDM master with a subset, and never add unrelated SDM to the final results.
  mod.pairWorkflowTukin = async function (rows) {
    const nips = [...new Set(rows.map(row => String(row.nip || '').trim()).filter(Boolean))];
    const response = await fetch(new URL('/api/shared-master/lookup-tukin', EVKIN.exportBackend.apiBase), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nips }), signal: AbortSignal.timeout(60000)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Gagal mencocokkan Tukin bruto dari Master SDM');
    if (!Array.isArray(result.rows)) throw new Error('Respons Master SDM tidak valid');
    const lookup = new Map(result.rows.map(row => [String(row.nip).trim(), row]));
    return lookup;
  };

  mod.process = function () {
    if (!S.rekap.length) throw new Error('Rekap belum tersedia.');
    S.final = S.rekap.filter(r => !r.bulan || r.bulan === S.month).map(r => {
      const master = masterTukinOf(r.nip);
      return recalc({ nip: r.nip, nama: r.nama, jabatan: r.jabatan, bulan: r.bulan,
        tukin: master.tukin, masterSdmStatus: master.status, potPct: r.total });
    });
    if (EVKIN.db && EVKIN.db.saveFinal) {
      EVKIN.db.saveFinal(S.final).catch((err) => console.warn('Gagal simpan final ke IndexedDB:', err));
    }
    EVKIN.syncBackendWorkflow().catch((err) => console.warn('Gagal sinkron final ke backend:', err));
    EVKIN.save();
    EVKIN.auditMod.logStage('final', 'PROCESSED:' + S.final.length);
    return S.final;
  };

  function recalc(f) {
    if (f.tukin === null || f.tukin === undefined || !Number.isFinite(Number(f.tukin))) {
      f.tukin = null;
      f.komponen = null;
      f.potPct = Math.min(100, Number(f.potPct) || 0);
      f.nominal = null;
      f.finalDibayar = null;
      return f;
    }
    const tukin = roundMoney(f.tukin);
    const komponen = roundMoney(tukin * KEHADIRAN);
    const potPct = Math.min(100, Number(f.potPct) || 0);
    const nominal = roundMoney(komponen * (potPct / 100));
    f.tukin = tukin;
    f.komponen = komponen;
    f.potPct = potPct;
    f.nominal = nominal;
    f.finalDibayar = roundMoney(tukin - nominal);
    return f;
  }

  mod.render = function (el, paired = false) {
    if (!EVKIN.stepUnlocked('final')) { el.innerHTML = lock('Verifikasi Semi Final terlebih dahulu.'); return; }
    if (!paired && EVKIN.exportBackend && EVKIN.exportBackend.enabled) {
      const generation = ++renderGeneration;
      const rows = (S.rekap.length ? S.rekap : S.final).filter(row => !row.bulan || row.bulan === S.month).map(row => ({ ...row }));
      const month = S.month;
      if (!rows.length) { el.innerHTML = lock('Rekap belum tersedia. Proses Rekap SDM terlebih dahulu.'); return; }
      el.innerHTML = `<div class="card"><div class="muted">Mencocokkan Tukin bruto untuk ${rows.length.toLocaleString('id-ID')} SDM workflow...</div></div>`;
      mod.pairWorkflowTukin(rows)
        .then((lookup) => {
          if (generation !== renderGeneration || S.month !== month ||
              (document.body.dataset.page && document.body.dataset.page !== 'final')) return;
          workflowMaster = lookup;
          S.final = rows.map(row => {
            const master = masterTukinOf(row.nip);
            const identity = lookup.get(String(row.nip || '').trim()) || {};
            return recalc({ ...row, nama: identity.nama || row.nama, jabatan: identity.jabatan || row.jabatan,
              tukin: master.tukin, masterSdmStatus: master.status,
              potPct: row.total ?? row.potPct });
          });
          mod.render(el, true);
        })
        .catch((error) => {
          if (generation !== renderGeneration || S.month !== month ||
              (document.body.dataset.page && document.body.dataset.page !== 'final')) return;
          EVKIN.toast('Master SDM gagal dimuat: ' + (error.message || error), 'error');
          el.innerHTML = lock(U.esc(error.message || 'Master SDM belum berhasil dimuat.')) +
            '<button class="btn btn-primary" id="retryFinalMaster">Coba lagi</button>';
          el.querySelector('#retryFinalMaster').onclick = () => mod.render(el);
        });
      return;
    }
    if (!S.final.length) mod.process();
    el.innerHTML = `
      <div class="module-head"><div><h2>⑥ Final Workflow Absensi</h2><p class="muted">Rekap workflow/preview legacy per SDM — <span id="finalMonthLabel"></span>. Bukan Final TUKIN canonical dan bukan dasar pembayaran.</p></div></div>
      <div class="alert warning"><i data-lucide="history"></i><span><strong>LEGACY / HISTORY.</strong> Gunakan Pengelolaan TUKIN untuk Calculation Snapshot dan menu Final TUKIN Canonical untuk approval/payment.</span></div>
      <div class="config-tabs">
        <button class="ctab ${tab === 'absensi' ? 'active' : ''}" data-t="absensi">Final Absensi</button>
        <button class="ctab ${tab === 'skp' ? 'active' : ''}" data-t="skp">Final + SKP (opsional)</button>
        <button class="ctab ${tab === 'kombinasi' ? 'active' : ''}" data-t="kombinasi">Final Kombinasi (opsional)</button>
        <button class="ctab ${tab === 'combined' ? 'active' : ''}" data-t="combined">FINAL + SKP Kombinasi</button>
      </div>
      <div class="filter-bar"><label>Periode Final <select id="finalMonth"></select></label><input type="search" id="finalNipSearch" placeholder="Cari NIP..." data-testid="final-nip-search" /></div>
      <div id="finalBody">${tab === 'absensi' ? absensiView() : tab === 'skp' ? skpView() : tab === 'kombinasi' ? kombinasiView() : combinedView()}</div>
      <div class="actions-row">
        <button class="btn btn-ghost" onclick="EVKIN.go('semifinal')"><i data-lucide="arrow-left"></i> Kembali</button>
        <button class="btn btn-success" id="saveFinalResult"><i data-lucide="database-zap"></i> Simpan ke Histori Legacy</button>
        <button class="btn btn-primary" id="exportPairedFinal">Lanjut: Export Excel <i data-lucide="arrow-right"></i></button>
        <button class="btn btn-primary" id="continueToSkp">Lanjut ke Penilaian SKP <i data-lucide="arrow-right"></i></button>
      </div><div id="finalExportStatus" role="status" aria-live="polite"></div>`;
    el.querySelectorAll('.ctab').forEach(b => b.onclick = () => { tab = b.dataset.t; EVKIN.go('final'); });
    el.querySelector('#finalMonthLabel').textContent = U.monthLabel(S.month);
    const finalMonth = el.querySelector('#finalMonth');
    [...new Set(S.rekap.map(r => r.bulan || S.month))].sort().forEach(month => {
      const option = document.createElement('option');
      option.value = String(month ?? '');
      option.textContent = U.monthLabel(month);
      option.selected = month === S.month;
      finalMonth.appendChild(option);
    });
    finalMonth.onchange = e => {S.month=e.target.value; EVKIN.invalidateFrom('rekap',{save:false}); EVKIN.go('final');};
    const finalNipSearch = el.querySelector('#finalNipSearch');
    finalNipSearch.value = nipSearch;
    finalNipSearch.oninput = (e) => { nipSearch = e.target.value.trim().toLowerCase(); S.finalPage = 0; EVKIN.go('final'); };
    const exportButton = el.querySelector('#exportPairedFinal');
    el.querySelector('#continueToSkp').onclick = async () => {
      try {
        if (EVKIN.exportBackend?.enabled) await EVKIN.syncBackendWorkflow(undefined, { finalOnly: true });
        if (EVKIN.skpAssessmentMod?.selectTab) EVKIN.skpAssessmentMod.selectTab('overview');
        EVKIN.go('skp-assessment');
      } catch (error) { EVKIN.toast(error.message || String(error), 'error'); }
    };
    exportButton.onclick = async () => {
      if (exportButton.disabled) return;
      const status = el.querySelector('#finalExportStatus');
      if (finalNeedsMaster()) {
        const missing = S.final.filter(row => row.tukin == null || row.tukin === '');
        status.textContent = `Belum bisa ekspor: ${missing.length} SDM belum memiliki Tukin bruto. NIP: ${missing.slice(0, 5).map(row => row.nip).join(', ')}. Lengkapi Master SDM lalu buka kembali Final.`;
        return;
      }
      const label = exportButton.innerHTML;
      exportButton.disabled = true;
      exportButton.textContent = 'Menyiapkan Export...';
      status.textContent = 'Menyimpan hasil Final. Menunggu antrean penyimpanan jika masih berjalan...';
      try {
        if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) throw new Error('Backend belum aktif. Aktifkan backend lalu coba lagi.');
        await EVKIN.syncBackendWorkflow((source, done, total) => {
          status.textContent = `Menyimpan ${source}: ${done.toLocaleString('id-ID')} / ${total.toLocaleString('id-ID')} baris`;
        }, { finalOnly: true });
        EVKIN.go('export');
      } catch (error) {
        status.textContent = 'Gagal menyiapkan Export: ' + (error.message || error) + '. Klik Lanjut: Export Excel untuk mencoba lagi.';
      } finally {
        exportButton.disabled = false;
        exportButton.innerHTML = label;
      }
    };
    const saveFinalResult = el.querySelector('#saveFinalResult');
    if (saveFinalResult) saveFinalResult.onclick = async () => {
      if (!S.final.length) return EVKIN.toast('Final Workflow belum berisi data.', 'error');
      if (finalNeedsMaster()) return EVKIN.toast('Lengkapi Master SDM untuk NIP yang belum memiliki Tukin bruto sebelum menyimpan.', 'error');
      if (!EVKIN.exportBackend || !EVKIN.exportBackend.enabled) return EVKIN.toast('Backend wajib aktif untuk menyimpan hasil final.', 'error');
      if (!confirm('Simpan hasil workflow ke HISTORI LEGACY? Data ini bukan Final TUKIN canonical dan bukan dasar pembayaran.')) return;
      saveFinalResult.disabled = true;
      try {
        await EVKIN.syncBackendWorkflow(undefined, { finalOnly: true });
        const response = await fetch(new URL('/api/tukin/periods/capture-live', EVKIN.exportBackend.apiBase), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assessment_period: S.month, payment_period: nextPaymentPeriod(S.month) })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
          const detail = result.detail || result;
          const reasons = detail.blocking_reasons || (detail.message ? [detail.message] : []);
          const error = new Error(reasons.length ? reasons.join(' ') : 'Gagal menyimpan hasil final');
          error.workflow = detail;
          throw error;
        }
        EVKIN.toast(`${result.captured || 0} hasil Final berhasil disimpan ke database.`, 'success');
      } catch (error) {
        const reasons = error.workflow && error.workflow.blocking_reasons;
        if (reasons && reasons.length) {
          const checks = error.workflow.checks || {};
          const label = {
            harian_available: 'Hitung Harian tersedia',
            semifinal_available: 'Semi Final tersedia',
            semifinal_verified: 'Verifikasi Semi Final tercatat',
            rekap_available: 'Rekap SDM tersedia',
            rekap_consistent: 'Rekap SDM konsisten'
          };
          const status = Object.entries(label).map(([key, text]) =>
            `${checks[key] ? '✓' : '✗'} ${text}`).join(' · ');
          EVKIN.toast(reasons.join(' '), 'error');
          const statusNode = el.querySelector('#finalExportStatus');
          if (statusNode) statusNode.textContent = `Final Tukin belum dapat dibuat. ${status}. Perbaiki item bertanda ✗ lalu coba lagi.`;
        } else {
          EVKIN.toast(error.message || 'Gagal menyimpan hasil final.', 'error');
        }
      } finally {
        saveFinalResult.disabled = false;
      }
    };
    bindAbsensi(el); bindSkp(el); bindKomb(el); bindCombined(el);
    if (window.lucide) lucide.createIcons();
  };

  // ---- FINAL ABSENSI ----
  function absensiView() {
    const ITEMS_PER_PAGE = 10;
    const data = filteredFinal();
    const totalPages = Math.ceil(data.length / ITEMS_PER_PAGE);
    let currentPage = Math.max(0, Math.min(S.finalPage, totalPages - 1));
    S.finalPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = data.slice(startIdx, endIdx);

    const rows = pageData.map((f, rowIndex) => `<tr>
      <td>${U.esc(f.nip)}</td><td>${U.esc(f.nama)}</td><td>${U.esc(f.jabatan)}</td>
      <td><input type="number" class="cell-num" data-final-tukin-row="${startIdx + rowIndex}" data-testid="final-tukin-input"/></td>
      <td>${f.komponen == null ? '<span class="pill red">Belum tersedia</span>' : U.fmtRupiah(f.komponen)}</td><td><b>${f.potPct}%</b></td><td>${f.nominal == null ? '<span class="pill red">Belum tersedia</span>' : U.fmtRupiah(f.nominal)}</td>
      <td class="pay"><b>${f.finalDibayar == null ? '<span class="pill red">' + U.esc(f.masterSdmStatus || 'TUKIN BRUTO BELUM TERSEDIA') + '</span>' : U.fmtRupiah(f.finalDibayar)}</b></td></tr>`).join('');
    const totBruto = data.reduce((a, f) => a + (Number(f.tukin) || 0), 0);
    const totNom = data.reduce((a, f) => a + (Number(f.nominal) || 0), 0);
    const totPay = data.reduce((a, f) => a + (Number(f.finalDibayar) || 0), 0);

    const paginationInfo = `<div style="text-align: center; margin: 12px 0; font-size: 13px; color: #666;">
      <strong>Data ${startIdx + 1}–${Math.min(endIdx, data.length)} dari ${data.length}</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>`;

    const paginationButtons = `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="absePrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="abseNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>`;

    return `<div class="card"><h3>Final Potongan Kehadiran</h3>
      <div class="table-wrap"><table class="data-table" data-testid="final-table"><thead><tr>
      <th>NIP</th><th>Nama</th><th>Jabatan</th><th>Tukin Bruto</th><th>Komponen Kehadiran 30%</th><th>Total Potongan %</th><th>Nominal Potongan</th><th>Final Tukin Dibayar</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td colspan="3">TOTAL</td><td>${U.fmtRupiah(totBruto)}</td><td>-</td><td>-</td><td>${U.fmtRupiah(totNom)}</td><td><b>${U.fmtRupiah(totPay)}</b></td></tr></tfoot>
      </table></div>
      ${paginationInfo}
      ${paginationButtons}
      <p class="muted">Edit Tukin Bruto pada tabel untuk memperbarui perhitungan. Nominal = Komponen 30% × Total Potongan %.</p></div>`;
  }
  function bindAbsensi(el) {
    el.querySelectorAll('[data-final-tukin-row]').forEach(inp => {
      const f = filteredFinal()[Number(inp.dataset.finalTukinRow)];
      if (!f) return;
      inp.dataset.tukin = String(f.nip ?? '');
      inp.value = safeNumericInputValue(f.tukin);
      inp.onchange = () => {
        const current = S.final.find(x => x.nip === inp.dataset.tukin);
        if (!current) return;
        current.tukin = parseFloat(inp.value) || 0; recalc(current); EVKIN.save(); EVKIN.go('final');
      };
    });

    const ITEMS_PER_PAGE = 10;
    const totalPages = Math.ceil(filteredFinal().length / ITEMS_PER_PAGE);
    const btnPrev = el.querySelector('#absePrev');
    const btnNext = el.querySelector('#abseNext');
    if (btnPrev) btnPrev.onclick = () => { S.finalPage = Math.max(0, S.finalPage - 1); EVKIN.save(); EVKIN.go('final'); };
    if (btnNext) btnNext.onclick = () => { S.finalPage = Math.min(totalPages - 1, S.finalPage + 1); EVKIN.save(); EVKIN.go('final'); };
  }

  // ---- FINAL + SKP ----
  function skpRow(nip) { let r = S.finalSKP.rows.find(x => x.nip === nip); if (!r) { r = { nip, skpPct: 0 }; S.finalSKP.rows.push(r); } return r; }
  function skpView() {
    const ITEMS_PER_PAGE = 10;
    const data = filteredFinal();
    const totalPages = Math.ceil(data.length / ITEMS_PER_PAGE);
    let currentPage = Math.max(0, Math.min(S.finalPage, totalPages - 1));
    S.finalPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = data.slice(startIdx, endIdx);

    const rows = pageData.map((f, rowIndex) => {
      const sr = skpRow(f.nip);
      const komKinerja = roundMoney(f.tukin * KINERJA);
      const nomSKP = roundMoney(komKinerja * (Math.min(100, sr.skpPct) / 100));
      const finalTotal = roundMoney(f.tukin - f.nominal - nomSKP);
      return `<tr><td>${U.esc(f.nip)}</td><td>${U.esc(f.nama)}</td><td>${U.fmtRupiah(f.tukin)}</td>
        <td>${U.fmtRupiah(f.nominal)}</td>
        <td><input type="number" step="0.1" class="cell-num" data-final-skp-row="${startIdx + rowIndex}" ${S.finalSKP.enabled ? '' : 'disabled'} data-testid="skp-pct-input"/></td>
        <td>${U.fmtRupiah(nomSKP)}</td><td class="pay"><b>${U.fmtRupiah(finalTotal)}</b></td></tr>`;
    }).join('');

    const paginationInfo = `<div style="text-align: center; margin: 12px 0; font-size: 13px; color: #666;">
      <strong>Data ${startIdx + 1}–${Math.min(endIdx, data.length)} dari ${data.length}</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>`;

    const paginationButtons = `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="skpPrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="skpNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>`;

    return `<div class="card"><h3>Final + SKP</h3>
      <label><input type="checkbox" id="skpEnabled" ${S.finalSKP.enabled ? 'checked' : ''} data-testid="skp-enabled"/> Gunakan potongan SKP (sumber terpisah, tidak dicampur otomatis)</label>
      <div style="margin-top:12px; padding: 12px; background: #f5f5f5; border-radius: 4px;">
        <p style="margin: 0 0 10px 0; font-size: 13px; color: #666;">
          <strong>📥 Import Nilai Potongan SKP dari Excel</strong><br>
          Format: Kolom <strong>NIP</strong> dan <strong>Potongan/Nilai</strong> (dalam %)
        </p>
        <input type="file" id="skpFileInput" accept=".xlsx,.xls,.csv" style="margin-bottom: 8px;" />
        <button id="skpImportBtn" class="btn btn-sm" style="margin-right: 6px;">🔄 Import SKP</button>
        <span id="skpStatus" style="font-size: 12px; color: #666;"></span>
      </div>
      <div class="table-wrap" style="margin-top:12px"><table class="data-table" data-testid="skp-table"><thead><tr>
      <th>NIP</th><th>Nama</th><th>Tukin Bruto</th><th>Nominal Pot. Kehadiran</th><th>Potongan SKP %</th><th>Nominal Pot. SKP (dari 70%)</th><th>Final Tukin Dibayar</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${paginationInfo}
      ${paginationButtons}
    </div>`;
  }
  function bindSkp(el) {
    const cb = el.querySelector('#skpEnabled');
    if (cb) cb.onchange = () => { S.finalSKP.enabled = cb.checked; EVKIN.save(); EVKIN.go('final'); };

    el.querySelectorAll('[data-final-skp-row]').forEach(inp => {
      const f = filteredFinal()[Number(inp.dataset.finalSkpRow)];
      if (!f) return;
      const row = skpRow(f.nip);
      inp.dataset.skp = String(f.nip ?? '');
      inp.value = safeNumericInputValue(row.skpPct);
      inp.onchange = () => {
        skpRow(inp.dataset.skp).skpPct = parseFloat(inp.value) || 0; EVKIN.save(); EVKIN.go('final');
      };
    });

    // File import SKP
    const importBtn = el.querySelector('#skpImportBtn');
    const fileInput = el.querySelector('#skpFileInput');
    const statusSpan = el.querySelector('#skpStatus');

    if (importBtn && fileInput) {
      importBtn.onclick = () => {
        if (!fileInput.files.length) {
          statusSpan.textContent = '⚠️ Pilih file terlebih dahulu';
          return;
        }
        const file = fileInput.files[0];
        statusSpan.textContent = '⏳ Sedang import...';

        mod.importSkpFile(file, (err, result) => {
          if (err) {
            statusSpan.textContent = '❌ ' + err.message;
            statusSpan.style.color = '#d32f2f';
            return;
          }
          statusSpan.textContent = `✅ Berhasil: ${result.imported} data di-import${result.notfound ? ', ' + result.notfound + ' NIP tidak ditemukan' : ''}`;
          statusSpan.style.color = '#388e3c';
          EVKIN.save();
          setTimeout(() => EVKIN.go('final'), 500);
        });
      };
    }

    // Pagination SKP
    const ITEMS_PER_PAGE = 10;
    const totalPages = Math.ceil(filteredFinal().length / ITEMS_PER_PAGE);
    const btnSkpPrev = el.querySelector('#skpPrev');
    const btnSkpNext = el.querySelector('#skpNext');
    if (btnSkpPrev) btnSkpPrev.onclick = () => { S.finalPage = Math.max(0, S.finalPage - 1); EVKIN.save(); EVKIN.go('final'); };
    if (btnSkpNext) btnSkpNext.onclick = () => { S.finalPage = Math.min(totalPages - 1, S.finalPage + 1); EVKIN.save(); EVKIN.go('final'); };
  }

  // ---- FINAL KOMBINASI ----
  function kombRow(nip) { let r = S.finalKombinasi.rows.find(x => x.nip === nip); if (!r) { r = { nip, eabs: 0, esdm: 0 }; S.finalKombinasi.rows.push(r); } return r; }
  function kombinasiView() {
    const ITEMS_PER_PAGE = 10;
    const K = S.finalKombinasi;
    const data = filteredFinal();
    const totalPages = Math.ceil(data.length / ITEMS_PER_PAGE);
    let currentPage = Math.max(0, Math.min(S.finalPage, totalPages - 1));
    S.finalPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = data.slice(startIdx, endIdx);

    const rows = pageData.map((f, rowIndex) => {
      const kr = kombRow(f.nip);
      const cand = [];
      if (K.useApp) cand.push(f.potPct);
      if (K.useEabsensi) cand.push(kr.eabs);
      if (K.useEsdm) cand.push(kr.esdm);
      const minPct = cand.length ? Math.min(...cand) : 0;
      const nominal = roundMoney(f.komponen * (Math.min(100, minPct) / 100));
      const finalPay = roundMoney(f.tukin - nominal);
      return `<tr><td>${U.esc(f.nip)}</td><td>${U.esc(f.nama)}</td><td>${f.potPct}%</td>
        <td><input type="number" step="0.1" class="cell-num" data-kf="eabs" data-final-komb-row="${startIdx + rowIndex}" ${K.useEabsensi ? '' : 'disabled'}/></td>
        <td><input type="number" step="0.1" class="cell-num" data-kf="esdm" data-final-komb-row="${startIdx + rowIndex}" ${K.useEsdm ? '' : 'disabled'}/></td>
        <td><b>${minPct}%</b></td><td>${U.fmtRupiah(nominal)}</td><td class="pay"><b>${U.fmtRupiah(finalPay)}</b></td></tr>`;
    }).join('');

    const paginationInfo = `<div style="text-align: center; margin: 12px 0; font-size: 13px; color: #666;">
      <strong>Data ${startIdx + 1}–${Math.min(endIdx, data.length)} dari ${data.length}</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>`;

    const paginationButtons = `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="kombPrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="kombNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>`;

    return `<div class="card"><h3>Final Kombinasi</h3>
      <p class="muted">Jika lebih dari satu sumber dipilih, diambil <b>potongan terkecil</b>. e-Absensi dan ESDM adalah sumber terpisah.</p>
      <div class="filter-bar">
        <label><input type="checkbox" id="kApp" ${K.useApp ? 'checked' : ''}/> Aplikasi</label>
        <label><input type="checkbox" id="kEabs" ${K.useEabsensi ? 'checked' : ''}/> e-Absensi</label>
        <label><input type="checkbox" id="kEsdm" ${K.useEsdm ? 'checked' : ''}/> ESDM</label>
      </div>
      <div style="margin-top:12px; padding: 12px; background: #f5f5f5; border-radius: 4px;">
        <p style="margin: 0 0 10px 0; font-size: 13px; color: #666;">
          <strong>📥 Import Nilai Potongan dari Excel</strong><br>
          Format: Kolom <strong>NIP</strong> dan <strong>Potongan/Nilai</strong> (dalam %)
        </p>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
          <div>
            <label style="display: block; font-size: 12px; color: #333; margin-bottom: 4px;"><strong>e-Absensi</strong></label>
            <input type="file" id="kombEabsInput" accept=".xlsx,.xls,.csv" />
            <button id="kombEabsBtn" class="btn btn-sm" style="margin-top: 4px; width: 100%;">Import e-Absensi</button>
            <span id="kombEabsStatus" style="font-size: 11px; display: block; margin-top: 4px; color: #666;"></span>
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #333; margin-bottom: 4px;"><strong>ESDM</strong></label>
            <input type="file" id="kombEsdmInput" accept=".xlsx,.xls,.csv" />
            <button id="kombEsdmBtn" class="btn btn-sm" style="margin-top: 4px; width: 100%;">Import ESDM</button>
            <span id="kombEsdmStatus" style="font-size: 11px; display: block; margin-top: 4px; color: #666;"></span>
          </div>
        </div>
      </div>
      <div class="table-wrap" style="margin-top:12px"><table class="data-table" data-testid="komb-table"><thead><tr>
      <th>NIP</th><th>Nama</th><th>Aplikasi %</th><th>e-Absensi %</th><th>ESDM %</th><th>Potongan Terpilih (min)</th><th>Nominal Potongan</th><th>Final Tukin Dibayar</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${paginationInfo}
      ${paginationButtons}
    </div>`;
  }
  function bindKomb(el) {
    const K = S.finalKombinasi;
    const b = (id, key) => { const c = el.querySelector(id); if (c) c.onchange = () => { K[key] = c.checked; EVKIN.save(); EVKIN.go('final'); }; };
    b('#kApp', 'useApp'); b('#kEabs', 'useEabsensi'); b('#kEsdm', 'useEsdm');

    el.querySelectorAll('[data-kf]').forEach(inp => {
      const f = filteredFinal()[Number(inp.dataset.finalKombRow)];
      if (!f) return;
      const row = kombRow(f.nip);
      inp.dataset.nip = String(f.nip ?? '');
      inp.value = safeNumericInputValue(row[inp.dataset.kf]);
      inp.onchange = () => {
        kombRow(inp.dataset.nip)[inp.dataset.kf] = parseFloat(inp.value) || 0; EVKIN.save(); EVKIN.go('final');
      };
    });

    // File import Kombinasi (e-Absensi & e-SDM)
    const importFile = (source) => {
      const inputId = '#komb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Input';
      const btnId = '#komb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Btn';
      const statusId = '#komb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Status';

      const fileInput = el.querySelector(inputId);
      const importBtn = el.querySelector(btnId);
      const statusSpan = el.querySelector(statusId);

      if (importBtn && fileInput) {
        importBtn.onclick = () => {
          if (!fileInput.files.length) {
            statusSpan.textContent = '⚠️ Pilih file terlebih dahulu';
            statusSpan.style.color = '#f57c00';
            return;
          }
          const file = fileInput.files[0];
          statusSpan.textContent = '⏳ Sedang import...';
          statusSpan.style.color = '#1976d2';

          mod.importKombinasiFile(file, source, (err, result) => {
            if (err) {
              statusSpan.textContent = '❌ ' + err.message;
              statusSpan.style.color = '#d32f2f';
              return;
            }
            statusSpan.textContent = `✅ ${result.imported} data${result.notfound ? ', ' + result.notfound + ' NIP tidak ditemukan' : ''}`;
            statusSpan.style.color = '#388e3c';
            EVKIN.save();
            setTimeout(() => EVKIN.go('final'), 500);
          });
        };
      }
    };

    importFile('eabsensi');
    importFile('esdm');

    // Pagination Kombinasi
    const ITEMS_PER_PAGE = 10;
    const totalPages = Math.ceil(filteredFinal().length / ITEMS_PER_PAGE);
    const btnKombPrev = el.querySelector('#kombPrev');
    const btnKombNext = el.querySelector('#kombNext');
    if (btnKombPrev) btnKombPrev.onclick = () => { S.finalPage = Math.max(0, S.finalPage - 1); EVKIN.save(); EVKIN.go('final'); };
    if (btnKombNext) btnKombNext.onclick = () => { S.finalPage = Math.min(totalPages - 1, S.finalPage + 1); EVKIN.save(); EVKIN.go('final'); };
  }

  function lock(m) { return `<div class="module-head"><h2>⑥ Final <span class="pill gray">🔒 Terkunci</span></h2></div><div class="alert warn">${m}</div>`; }

  // ---- FINAL + SKP KOMBINASI (TAB BARU) ----
  function combinedRow(nip) {
    let r = S.finalCombinedSKP.combinedRows.find(x => x.nip === nip);
    if (!r) {
      r = { nip, skpPct: 0, absensiPct: 0 };
      S.finalCombinedSKP.combinedRows.push(r);
    }
    return r;
  }

  function combinedView() {
    const ITEMS_PER_PAGE = 10;
    const FC = S.finalCombinedSKP;
    const data = filteredFinal();
    const totalPages = Math.ceil(data.length / ITEMS_PER_PAGE);
    let currentPage = Math.max(0, Math.min(S.finalPage, totalPages - 1));
    S.finalPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = data.slice(startIdx, endIdx);

    const rows = pageData.map(f => {
      const cr = combinedRow(f.nip);
      const komKehadiran = f.tukin * KEHADIRAN;
      const komKinerja = f.tukin * KINERJA;

      // Potongan Absensi (30%) - ambil terkecil dari sumber yang dipilih
      const absensiCand = [];
      if (FC.useApp) absensiCand.push(f.potPct);
      if (FC.useEabsensi) {
        const eabsRow = S.finalCombinedSKP.eabsensiRows.find(x => x.nip === f.nip);
        if (eabsRow) absensiCand.push(eabsRow.pct);
      }
      if (FC.useEsdm) {
        const esdmRow = S.finalCombinedSKP.esdmRows.find(x => x.nip === f.nip);
        if (esdmRow) absensiCand.push(esdmRow.pct);
      }

      const finalAbsensiPct = absensiCand.length ? Math.min(...absensiCand) : f.potPct;
      const nomAbsensi = roundMoney(komKehadiran * (Math.min(100, finalAbsensiPct) / 100));

      // Potongan SKP (70%)
      const skpPct = cr.skpPct;
      const nomSKP = roundMoney(komKinerja * (Math.min(100, skpPct) / 100));

      // Total Potongan
      const totalPotPct = Math.round((finalAbsensiPct * 0.30 + skpPct * 0.70) * 100) / 100;
      const totalNominal = roundMoney(nomAbsensi + nomSKP);
      const tukinDibayar = roundMoney(f.tukin - totalNominal);

      return `<tr>
        <td>${U.esc(f.nip)}</td><td>${U.esc(f.nama)}</td>
        <td>${U.fmtRupiah(f.tukin)}</td>
        <td><b>${finalAbsensiPct}%</b></td><td>${U.fmtRupiah(nomAbsensi)}</td>
        <td><b>${skpPct}%</b></td><td>${U.fmtRupiah(nomSKP)}</td>
        <td><b>${totalPotPct}%</b></td><td>${U.fmtRupiah(totalNominal)}</td>
        <td class="pay"><b>${U.fmtRupiah(tukinDibayar)}</b></td>
      </tr>`;
    }).join('');

    const totBruto = data.reduce((a, f) => a + f.tukin, 0);
    const totAbsensi = roundMoney(pageData.reduce((a, f) => {
      const cr = combinedRow(f.nip);
      const komKehadiran = roundMoney(f.tukin * KEHADIRAN);
      const absensiCand = [];
      if (FC.useApp) absensiCand.push(f.potPct);
      if (FC.useEabsensi) {
        const eabsRow = S.finalCombinedSKP.eabsensiRows.find(x => x.nip === f.nip);
        if (eabsRow) absensiCand.push(eabsRow.pct);
      }
      if (FC.useEsdm) {
        const esdmRow = S.finalCombinedSKP.esdmRows.find(x => x.nip === f.nip);
        if (esdmRow) absensiCand.push(esdmRow.pct);
      }
      const finalAbsensiPct = absensiCand.length ? Math.min(...absensiCand) : f.potPct;
      return a + (komKehadiran * (Math.min(100, finalAbsensiPct) / 100));
    }, 0));
    const totSKP = roundMoney(pageData.reduce((a, f) => {
      const cr = combinedRow(f.nip);
      return a + (f.tukin * KINERJA * (Math.min(100, cr.skpPct) / 100));
    }, 0));
    const totPotongan = roundMoney(totAbsensi + totSKP);
    const totDibayar = roundMoney(totBruto - totPotongan);

    const paginationInfo = `<div style="text-align: center; margin: 12px 0; font-size: 13px; color: #666;">
      <strong>Data ${startIdx + 1}–${Math.min(endIdx, data.length)} dari ${data.length}</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>`;

    const paginationButtons = `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="combinedPrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="combinedNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>`;

    const isDataPaired = S.finalCombinedSKP.eabsensiRows.length > 0 || S.finalCombinedSKP.esdmRows.length > 0;

    return `<div class="card"><h3>FINAL + SKP Kombinasi (Aplikasi, E-Absensi, E-SDM)</h3>
      <p class="muted">Potongan Absensi (30%) diambil dari <b>potongan terkecil</b> sumber yang dipilih. Potongan SKP (70%) dari import. Total = Absensi + SKP.</p>

      <div style="margin-top: 12px; padding: 12px; background: #f9f9f9; border: 1px solid #ddd; border-radius: 4px;">
        <p style="margin: 0 0 10px 0; font-weight: 500; font-size: 13px; color: #333;">
          <strong>🎯 Pilih Sumber Potongan Absensi (30%)</strong>
        </p>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;">
          <label style="display: flex; align-items: center; gap: 6px;">
            <input type="checkbox" id="combApp" ${FC.useApp ? 'checked' : ''}/>
            <span>Aplikasi</span>
          </label>
          <label style="display: flex; align-items: center; gap: 6px;">
            <input type="checkbox" id="combEabs" ${FC.useEabsensi ? 'checked' : ''}/>
            <span>E-Absensi</span>
          </label>
          <label style="display: flex; align-items: center; gap: 6px;">
            <input type="checkbox" id="combEsdm" ${FC.useEsdm ? 'checked' : ''}/>
            <span>E-SDM</span>
          </label>
        </div>
      </div>

      <div style="margin-top: 12px; padding: 12px; background: #f5f5f5; border-radius: 4px;">
        <p style="margin: 0 0 10px 0; font-size: 13px; color: #666;">
          <strong>📥 Import Data Potongan dari Excel</strong><br>
          Format: Kolom <strong>NIP</strong> dan <strong>Potongan/Nilai</strong> (dalam %)
        </p>
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px;">
          <div>
            <label style="display: block; font-size: 12px; color: #333; margin-bottom: 4px;"><strong>SKP (70% Kinerja)</strong></label>
            <input type="file" id="combSkpInput" accept=".xlsx,.xls,.csv" />
            <button id="combSkpBtn" class="btn btn-sm" style="margin-top: 4px; width: 100%;">Import SKP</button>
            <span id="combSkpStatus" style="font-size: 11px; display: block; margin-top: 4px; color: #666;"></span>
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #333; margin-bottom: 4px;"><strong>E-Absensi</strong></label>
            <input type="file" id="combEabsInput" accept=".xlsx,.xls,.csv" />
            <button id="combEabsBtn" class="btn btn-sm" style="margin-top: 4px; width: 100%;">Import E-Abs</button>
            <span id="combEabsStatus" style="font-size: 11px; display: block; margin-top: 4px; color: #666;"></span>
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #333; margin-bottom: 4px;"><strong>E-SDM</strong></label>
            <input type="file" id="combEsdmInput" accept=".xlsx,.xls,.csv" />
            <button id="combEsdmBtn" class="btn btn-sm" style="margin-top: 4px; width: 100%;">Import E-SDM</button>
            <span id="combEsdmStatus" style="font-size: 11px; display: block; margin-top: 4px; color: #666;"></span>
          </div>
        </div>
      </div>

      <div style="margin-top: 12px;">
        <button class="btn btn-primary" id="combProcessBtn" ${!isDataPaired ? 'disabled' : ''} title="${!isDataPaired ? 'Import e-Absensi atau e-SDM terlebih dahulu' : ''}">
          <i data-lucide="zap"></i> Proses Potongan Absensi
        </button>
        ${!isDataPaired ? '<span style="margin-left: 10px; font-size: 13px; color: #f57c00;">⚠️ Import e-Absensi atau e-SDM untuk mengaktifkan proses</span>' : '<span style="margin-left: 10px; font-size: 13px; color: #388e3c;">✓ Data siap diproses</span>'}
      </div>

      <div class="table-wrap" style="margin-top:12px"><table class="data-table" data-testid="combined-table"><thead><tr>
      <th>NIP</th><th>Nama</th><th>Tukin Bruto</th>
      <th>Pot. Absensi % (min)</th><th>Nominal Absensi (30%)</th>
      <th>Pot. SKP %</th><th>Nominal SKP (70%)</th>
      <th>Total Pot. %</th><th>Total Nominal</th>
      <th>Tukin Dibayarkan</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr>
        <td colspan="2">TOTAL</td>
        <td>${U.fmtRupiah(totBruto)}</td>
        <td>-</td><td>${U.fmtRupiah(totAbsensi)}</td>
        <td>-</td><td>${U.fmtRupiah(totSKP)}</td>
        <td>-</td><td>${U.fmtRupiah(totPotongan)}</td>
        <td><b>${U.fmtRupiah(totDibayar)}</b></td>
      </tr></tfoot>
      </table></div>
      ${paginationInfo}
      ${paginationButtons}
    </div>`;
  }

  function bindCombined(el) {
    const FC = S.finalCombinedSKP;

    // Handle checkbox changes
    const chk = (id, key) => {
      const c = el.querySelector(id);
      if (c) c.onchange = () => { FC[key] = c.checked; EVKIN.save(); EVKIN.go('final'); };
    };
    chk('#combApp', 'useApp');
    chk('#combEabs', 'useEabsensi');
    chk('#combEsdm', 'useEsdm');

    // Import SKP
    const importSkpCombined = () => {
      const fileInput = el.querySelector('#combSkpInput');
      const importBtn = el.querySelector('#combSkpBtn');
      const statusSpan = el.querySelector('#combSkpStatus');

      if (importBtn && fileInput) {
        importBtn.onclick = () => {
          if (!fileInput.files.length) {
            statusSpan.textContent = '⚠️ Pilih file terlebih dahulu';
            statusSpan.style.color = '#f57c00';
            return;
          }
          const file = fileInput.files[0];
          statusSpan.textContent = '⏳ Sedang import...';
          statusSpan.style.color = '#1976d2';

          readFileAsArray(file, (err, aoa) => {
            if (err) {
              statusSpan.textContent = '❌ ' + err.message;
              statusSpan.style.color = '#d32f2f';
              return;
            }
            if (!aoa || aoa.length < 2) {
              statusSpan.textContent = '❌ File kosong';
              statusSpan.style.color = '#d32f2f';
              return;
            }

            const headers = aoa[0].map(x => String(x).trim());
            const colMap = detectImportColumns(headers);

            if (colMap.nip === undefined || colMap.potongan === undefined) {
              statusSpan.textContent = '❌ Kolom NIP & Potongan tidak ditemukan';
              statusSpan.style.color = '#d32f2f';
              return;
            }

            let imported = 0, notfound = 0;
            aoa.slice(1).forEach(row => {
              const nip = String(row[colMap.nip] || '').trim();
              const val = parseFloat(row[colMap.potongan]) || 0;
              if (!nip) return;

              const existing = S.finalCombinedSKP.skpRows.find(x => x.nip === nip);
              if (existing) {
                existing.pct = val;
              } else {
                S.finalCombinedSKP.skpRows.push({ nip, pct: val });
              }
              imported++;
            });

            statusSpan.textContent = `✅ ${imported} data di-import${notfound ? ', ' + notfound + ' NIP tidak ditemukan' : ''}`;
            statusSpan.style.color = '#388e3c';
            EVKIN.auditMod.logSkpImport(imported, notfound, 'combined');
            EVKIN.save();
            setTimeout(() => EVKIN.go('final'), 500);
          });
        };
      }
    };
    importSkpCombined();

    // Import E-Absensi & E-SDM
    const importCombinedSource = (source) => {
      const inputId = '#comb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Input';
      const btnId = '#comb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Btn';
      const statusId = '#comb' + (source === 'eabsensi' ? 'Eabs' : 'Esdm') + 'Status';
      const storeKey = source === 'eabsensi' ? 'eabsensiRows' : 'esdmRows';

      const fileInput = el.querySelector(inputId);
      const importBtn = el.querySelector(btnId);
      const statusSpan = el.querySelector(statusId);

      if (importBtn && fileInput) {
        importBtn.onclick = () => {
          if (!fileInput.files.length) {
            statusSpan.textContent = '⚠️ Pilih file terlebih dahulu';
            statusSpan.style.color = '#f57c00';
            return;
          }
          const file = fileInput.files[0];
          statusSpan.textContent = '⏳ Sedang import...';
          statusSpan.style.color = '#1976d2';

          readFileAsArray(file, (err, aoa) => {
            if (err) {
              statusSpan.textContent = '❌ ' + err.message;
              statusSpan.style.color = '#d32f2f';
              return;
            }
            if (!aoa || aoa.length < 2) {
              statusSpan.textContent = '❌ File kosong';
              statusSpan.style.color = '#d32f2f';
              return;
            }

            const headers = aoa[0].map(x => String(x).trim());
            const colMap = detectImportColumns(headers);

            if (colMap.nip === undefined) {
              statusSpan.textContent = '❌ Kolom NIP tidak ditemukan';
              statusSpan.style.color = '#d32f2f';
              return;
            }

            const fieldToCheck = source === 'eabsensi' ? colMap.eabsensi : colMap.esdm;
            if (fieldToCheck === undefined && colMap.potongan === undefined) {
              statusSpan.textContent = '❌ Kolom potongan tidak ditemukan';
              statusSpan.style.color = '#d32f2f';
              return;
            }

            let imported = 0, notfound = 0;
            const valCol = fieldToCheck !== undefined ? fieldToCheck : colMap.potongan;

            aoa.slice(1).forEach(row => {
              const nip = String(row[colMap.nip] || '').trim();
              const val = parseFloat(row[valCol]) || 0;
              if (!nip) return;

              const existing = S.finalCombinedSKP[storeKey].find(x => x.nip === nip);
              if (existing) {
                existing.pct = val;
              } else {
                S.finalCombinedSKP[storeKey].push({ nip, pct: val });
              }
              imported++;
            });

            statusSpan.textContent = `✅ ${imported} data di-import${notfound ? ', ' + notfound + ' NIP tidak ditemukan' : ''}`;
            statusSpan.style.color = '#388e3c';
            if (source === 'eabsensi') {
              EVKIN.auditMod.logEabsensiImport(imported, notfound);
            } else {
              EVKIN.auditMod.logEsdmImport(imported, notfound);
            }
            EVKIN.save();
            setTimeout(() => EVKIN.go('final'), 500);
          });
        };
      }
    };
    importCombinedSource('eabsensi');
    importCombinedSource('esdm');

    // Process Potongan Absensi button
    const processBtn = el.querySelector('#combProcessBtn');
    if (processBtn) {
      processBtn.onclick = () => {
        FC.absensiProcessed = true;
        EVKIN.auditMod.logAbsensiProcess(S.final.length, {
          app: FC.useApp,
          eabsensi: FC.useEabsensi,
          esdm: FC.useEsdm
        });
        EVKIN.toast('✓ Potongan absensi telah diproses', 'success');
        EVKIN.save();
        EVKIN.go('final');
      };
    }

    // Pagination
    const ITEMS_PER_PAGE = 10;
    const totalPages = Math.ceil(filteredFinal().length / ITEMS_PER_PAGE);
    const btnPrev = el.querySelector('#combinedPrev');
    const btnNext = el.querySelector('#combinedNext');
    if (btnPrev) btnPrev.onclick = () => { S.finalPage = Math.max(0, S.finalPage - 1); EVKIN.save(); EVKIN.go('final'); };
    if (btnNext) btnNext.onclick = () => { S.finalPage = Math.min(totalPages - 1, S.finalPage + 1); EVKIN.save(); EVKIN.go('final'); };
  }
})();

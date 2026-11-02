/* EVKIN ABSENSI v3.1 - semifinal.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, S = EVKIN.state;
  let filter = 'all';

  const mod = EVKIN.semifinalMod = {};
  let nipSearch = '';

  function recomputeDup() {
    const grp = {};
    S.data.filter(d => !d.deleted).forEach(d => { const k = d.nip + '|' + d.tanggal.key; (grp[k] = grp[k] || []).push(d); });
    S.data.forEach(d => d.dupFlag = false);
    Object.values(grp).forEach(g => { if (g.length > 1) g.forEach(d => d.dupFlag = true); });
  }

  mod.recalcChain = async function () {
    if (EVKIN.ensureWorkflowDataForComputation) await EVKIN.ensureWorkflowDataForComputation(['processed_data', 'daily_data']);
    if (EVKIN.progress) EVKIN.progress({title: 'Hitung Ulang Semi Final', percent: 0, message: 'Menyiapkan perhitungan ulang...'});
    recomputeDup();
    await EVKIN.harianMod.process({sync:false});
    if (EVKIN.progress) EVKIN.progress({title: 'Hitung Ulang Semi Final', percent: 70, message: 'Menyimpan hasil hitungan...'});
    if (EVKIN.db) {
      await Promise.all([
        EVKIN.db.replaceStore('daily_data', S.daily || []),
        EVKIN.db.replaceStore('rekap_sdm', S.rekap || [])
      ]);
    }
    await EVKIN.syncBackendWorkflow(undefined, {derivedOnly:true});
    if (EVKIN.progress) EVKIN.progress({title: 'Hitung Ulang Semi Final', percent: 100, message: 'Hitung ulang selesai.', state: 'done'});
  };

  function counts() {
    const d = S.daily;
    return {
      dup: d.filter(x => x.statusDuplikasi).length,
      tl: d.filter(x => x.statusLengkap === 'Tidak Lengkap').length,
      libur: d.filter(x => x.statusHariLibur.startsWith('Ya')).length,
      cuti: d.filter(x => x.statusCuti).length,
      total: d.length
    };
  }

  mod.render = function (el) {
    if (!EVKIN.stepUnlocked('semifinal')) { el.innerHTML = lock('Selesaikan Hitung Harian terlebih dahulu.'); return; }
    const c = counts();

    // ---- PAGINATION ----
    const ITEMS_PER_PAGE = 10;
    let d = S.daily.slice();
    if (filter === 'dup') d = d.filter(x => x.statusDuplikasi);
    else if (filter === 'tl') d = d.filter(x => x.statusLengkap === 'Tidak Lengkap');
    else if (filter === 'libur') d = d.filter(x => x.statusHariLibur.startsWith('Ya'));
    else if (filter === 'cuti') d = d.filter(x => x.statusCuti);
    if (nipSearch) d = d.filter(x => String(x.nip || '').toLowerCase().includes(nipSearch));

    const totalPages = Math.ceil(d.length / ITEMS_PER_PAGE);
    let currentPage = Math.max(0, Math.min(S.semifinalPage, totalPages - 1));
    S.semifinalPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = d.slice(startIdx, endIdx);

    const rowsHtml = !pageData.length ? '<tr><td colspan="9" class="muted">Tidak ada data untuk filter ini.</td></tr>'
      : pageData.map(x => `<tr class="${x.flags.length ? 'row-flag' : ''}">
        <td>${U.esc(x.nip)}</td><td>${U.esc(x.nama)}</td><td>${U.esc(x.tanggal)}</td><td>${U.esc(x.jamMasuk)}</td><td>${U.esc(x.jamPulang)}</td>
        <td>${U.esc(x.jenis || x.statusCuti)}</td><td class="err small">${U.esc(x.flags.join(', '))}</td><td><b>${x.totalHarian}%</b></td>
        <td><button class="link-edit" data-edit="${x._id}">Edit</button> · <button class="link-del" data-del="${x._id}">Hapus</button></td></tr>`).join('');

    const paginationInfo = d.length > 0 ? `<div style="text-align: center; margin: 12px 0; font-size: 13px; color: #666;">
      <strong>Data ${startIdx + 1}–${Math.min(endIdx, d.length)} dari ${d.length}</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>` : '';

    const paginationButtons = d.length > 0 ? `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 12px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="btnPrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="btnNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>` : '';

    el.innerHTML = `
      <div class="module-head"><div><h2>④ Semi Final</h2><p class="muted">Verifikasi & pemeriksaan anomali (${U.monthLabel(S.month)}).</p></div>
        <div>
          ${S.semifinalVerified ? '<span class="pill green">✓ Terverifikasi</span>' : '<span class="pill yellow">⚠ Perlu Verifikasi</span>'}
          <span id="serverSemifinalIndicator" style="margin-left:8px"></span>
        </div></div>
      <div class="stat-grid">
        ${sc('Anomali Duplikasi', c.dup, 'copy', c.dup ? 'red' : 'green')}
        ${sc('Absensi Tidak Lengkap', c.tl, 'file-warning', c.tl ? 'yellow' : 'green')}
        ${sc('Absensi Hari Libur', c.libur, 'moon', 'gray')}
        ${sc('Status Cuti/Bebas', c.cuti, 'plane', 'blue')}
        ${sc('Total Record', c.total, 'list', 'blue')}
      </div>
      <div class="card">
        <div class="filter-bar">
          <input type="search" id="semifinalNipSearch" placeholder="Cari NIP..." value="${U.esc(nipSearch)}" data-testid="semifinal-nip-search" />
          ${fbtn('all', 'Semua')} ${fbtn('dup', 'Duplikasi')} ${fbtn('tl', 'Tidak Lengkap')} ${fbtn('libur', 'Hari Libur')} ${fbtn('cuti', 'Cuti')}
        </div>
        <div class="table-wrap"><table class="data-table sm" data-testid="semifinal-table"><thead><tr>
          <th>NIP</th><th>Nama</th><th>Tgl</th><th>Masuk</th><th>Pulang</th><th>Jenis</th><th>Flag</th><th>Total</th><th>Aksi</th></tr></thead>
          <tbody>${rowsHtml}</tbody></table></div>
        ${paginationInfo}
        ${paginationButtons}
      </div>
      <div class="actions-row">
        <button class="btn btn-ghost" onclick="EVKIN.go('harian')"><i data-lucide="arrow-left"></i> Kembali</button>
        <button class="btn btn-secondary" id="recalc" data-testid="semifinal-recalc"><i data-lucide="refresh-cw"></i> Hitung Ulang</button>
        <button class="btn btn-success" id="verify" data-testid="semifinal-verify"><i data-lucide="shield-check"></i> VERIFIKASI SEMI FINAL</button>
      </div>`;

    el.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { filter = b.dataset.filter; S.semifinalPage = 0; EVKIN.go('semifinal'); });
    el.querySelector('#semifinalNipSearch').oninput = (event) => { nipSearch = event.target.value.trim().toLowerCase(); S.semifinalPage = 0; EVKIN.go('semifinal'); };
    el.querySelector('#recalc').onclick = async () => {
      const button = el.querySelector('#recalc');
      button.disabled = true;
      try {
        await mod.recalcChain();
        EVKIN.auditMod.logStage('semifinal', 'RECALCULATED');
        EVKIN.toast('Hitung ulang selesai dan database diperbarui');
        EVKIN.go('semifinal');
      } catch (error) {
        button.disabled = false;
        if (EVKIN.progress) EVKIN.progress({title: 'Hitung Ulang Semi Final gagal', message: error.message, state: 'error'});
        EVKIN.toast('Gagal memperbarui hasil hitungan: ' + error.message, 'error');
      }
    };
    el.querySelector('#verify').onclick = async () => {
      const button = el.querySelector('#verify');
      if (button.disabled) return;
      button.disabled = true;
      if (EVKIN.progress) EVKIN.progress({title: 'Verifikasi Semi Final', percent: 0, message: 'Memeriksa kelengkapan dan duplikasi data...'});
      const previous = S.semifinalVerified;
      const previousRows = S.semifinal;
      try {
        if (EVKIN.ensureWorkflowDataForComputation) await EVKIN.ensureWorkflowDataForComputation(['processed_data', 'daily_data']);
        EVKIN.assertDailyCoverage();
        const seen = new Set();
        for (const row of S.daily) {
          const key = row.nip + '|' + row.tanggalKey;
          if (seen.has(key)) throw new Error('Masih ada duplikat NIP/tanggal. Koreksi baris duplikat sebelum verifikasi.');
          seen.add(key);
        }
        S.semifinal = S.daily.map(row => ({...row}));
        if (EVKIN.progress) EVKIN.progress({title: 'Verifikasi Semi Final', percent: 70, message: 'Menyimpan status verifikasi...'});
        if (EVKIN.exportBackend && EVKIN.exportBackend.markSemifinalVerified) {
          // The canonical daily result is already persisted by Hitung Harian.
          // Verification is a lightweight, idempotent backend operation; do
          // not upload every derived workflow table again just to set a flag.
          await EVKIN.exportBackend.markSemifinalVerified({bulan:S.month, timeoutMs:60000});
        }
        S.semifinalVerified = true;
        if (S.workflowMeta && S.workflowMeta.total_row_counts) {
          S.workflowMeta.total_row_counts.semifinal = S.semifinal.length;
        }
        if (EVKIN.progress) EVKIN.progress({title: 'Verifikasi Semi Final', percent: 100, message: 'Semi Final berhasil diverifikasi.', state: 'done'});
        EVKIN.auditMod.logStage('semifinal', 'VERIFIED');
        EVKIN.toast('Semi Final terverifikasi dan tersimpan — Rekap SDM terbuka');
        EVKIN.go('rekap');
      } catch (error) {
        S.semifinalVerified = previous;
        S.semifinal = previousRows;
        if (EVKIN.progress) EVKIN.progress({title: 'Verifikasi Semi Final gagal', message: error.message, state: 'error'});
        EVKIN.toast('Verifikasi gagal disimpan: ' + error.message, 'error');
      } finally { button.disabled = false; }
    };
    el.querySelectorAll('[data-del]').forEach(b => b.onclick = () => delRec(b.dataset.del));
    el.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => editRec(b.dataset.edit));

    const btnPrev = el.querySelector('#btnPrev');
    const btnNext = el.querySelector('#btnNext');
    // Pagination is view-only. Saving here used to resend tens of thousands
    // of rows and could keep the database busy when Verify was clicked.
    if (btnPrev) btnPrev.onclick = () => { S.semifinalPage = Math.max(0, S.semifinalPage - 1); EVKIN.go('semifinal'); };
    if (btnNext) btnNext.onclick = () => { S.semifinalPage = Math.min(totalPages - 1, S.semifinalPage + 1); EVKIN.go('semifinal'); };

    if (window.lucide) lucide.createIcons();

    // Populate small server-side semifinal indicator if backend analytics available
    (async function populateServerSemifinal() {
      const indicator = el.querySelector('#serverSemifinalIndicator');
      if (!indicator) return;
      if (EVKIN.exportBackend && EVKIN.exportBackend.enabled && typeof EVKIN.exportBackend.getWorkflowFlags === 'function') {
        indicator.innerHTML = '<span class="pill gray">Memuat...</span>';
        try {
          const res = await EVKIN.exportBackend.getWorkflowFlags(S.month);
          const key = 'semifinal:' + (S.month || '');
          const flags = (res && res.flags) ? res.flags : (res && res.flags === undefined && res) ? res : {};
          const f = flags[key] || null;
          if (f && f.verified) {
            indicator.innerHTML = '<span class="pill green" title="Diverifikasi di server">Server ✓</span>';
          } else if (f) {
            indicator.innerHTML = `<a class="link" href="${(EVKIN.exportBackend && EVKIN.exportBackend.analyticsBase) ? EVKIN.exportBackend.analyticsBase + '/workflow/flags?bulan=' + encodeURIComponent(S.month) : '#'}" target="_blank">Server: Belum</a>`;
          } else {
            indicator.innerHTML = '<span class="pill gray">Server: -</span>';
          }
          indicator.onclick = () => {
            if (f) EVKIN.toast('Server semifinal: ' + (f.verified ? 'terverifikasi' : 'belum') + (f.updated_at ? ' pada ' + f.updated_at : ''), 'info');
            else if (EVKIN.exportBackend && EVKIN.exportBackend.analyticsBase) EVKIN.exportBackend.openAuthenticated(EVKIN.exportBackend.analyticsBase + '/workflow/flags?bulan=' + encodeURIComponent(S.month));
          };
        } catch (err) {
          indicator.innerHTML = '<span class="pill gray">Server: -</span>';
          console.warn('Gagal ambil server semifinal flag:', err);
        }
      } else {
        // No backend
        indicator.innerHTML = '';
      }
    })();
  };

  function rows() {
    let d = S.daily.slice();
    if (filter === 'dup') d = d.filter(x => x.statusDuplikasi);
    else if (filter === 'tl') d = d.filter(x => x.statusLengkap === 'Tidak Lengkap');
    else if (filter === 'libur') d = d.filter(x => x.statusHariLibur.startsWith('Ya'));
    else if (filter === 'cuti') d = d.filter(x => x.statusCuti);
    if (!d.length) return '<tr><td colspan="9" class="muted">Tidak ada data untuk filter ini.</td></tr>';
    return d.slice(0, 300).map(x => `<tr class="${x.flags.length ? 'row-flag' : ''}">
      <td>${U.esc(x.nip)}</td><td>${U.esc(x.nama)}</td><td>${U.esc(x.tanggal)}</td><td>${U.esc(x.jamMasuk)}</td><td>${U.esc(x.jamPulang)}</td>
      <td>${U.esc(x.jenis || x.statusCuti)}</td><td class="err small">${U.esc(x.flags.join(', '))}</td><td><b>${x.totalHarian}%</b></td>
      <td><button class="link-edit" data-edit="${x._id}">Edit</button> · <button class="link-del" data-del="${x._id}">Hapus</button></td></tr>`).join('');
  }

  async function delRec(id) {
    if (!confirm('Hapus record ini? Data sumber tetap tersimpan (ditandai dihapus).')) return;
    const rec = S.data.find(d => d._id === id); if (rec) rec.deleted = true;
    try {
      await mod.recalcChain();
      EVKIN.auditMod.log('DATA_DELETE', { nip: rec ? rec.nip : null, id });
      EVKIN.toast('Record dihapus, hitungan dan database diperbarui');
      EVKIN.go('semifinal');
    } catch (error) {
      EVKIN.toast('Gagal memperbarui database setelah hapus: ' + error.message, 'error');
    }
  }
  async function editRec(id) {
    const rec = S.data.find(d => d._id === id); if (!rec) return;
    const m = prompt('Jam Masuk (HH:MM), kosongkan bila tidak ada:', U.fmtTime(rec.masukMin));
    if (m === null) return;
    const p = prompt('Jam Pulang (HH:MM), kosongkan bila tidak ada:', U.fmtTime(rec.pulangMin));
    if (p === null) return;
    const off = EVKIN.master.zoneOffsetMin[rec.zona] || 0;
    const wrap = (x) => x === null ? null : (((x + off) % 1440) + 1440) % 1440;
    rec.masukMin = U.parseTime(m); rec.pulangMin = U.parseTime(p);
    rec.masukNormMin = wrap(rec.masukMin); rec.pulangNormMin = wrap(rec.pulangMin);
    const addOff = EVKIN.master.zoneOffsetMinAdd[rec.zona] || 0;
    const wrapAdd = x => x === null ? null : ((x + addOff) % 1440 + 1440) % 1440;
    rec.masukNormMinAdd = wrapAdd(rec.masukMin); rec.pulangNormMinAdd = wrapAdd(rec.pulangMin);
    try {
      await mod.recalcChain();
      EVKIN.auditMod.logDataEdit(rec.nip, 'jamMasuk/jamPulang', m, p);
      EVKIN.toast('Record diperbarui, hitungan dan database diperbarui');
      EVKIN.go('semifinal');
    } catch (error) {
      EVKIN.toast('Gagal memperbarui database setelah edit: ' + error.message, 'error');
    }
  }

  function fbtn(f, label) { return `<button class="chip ${filter === f ? 'active' : ''}" data-filter="${f}">${label}</button>`; }
  function sc(l, v, i, c) { return `<div class="stat-card ${c}"><div class="stat-icon"><i data-lucide="${i}"></i></div><div><div class="stat-val">${v}</div><div class="stat-label">${l}</div></div></div>`; }
  function lock(m) { return `<div class="module-head"><h2>④ Semi Final <span class="pill gray">🔒 Terkunci</span></h2></div><div class="alert warn">${m}</div>`; }
})();

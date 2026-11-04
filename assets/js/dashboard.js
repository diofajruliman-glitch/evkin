/* EVKIN ABSENSI v3.1 - dashboard.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, S = EVKIN.state;
  const mod = EVKIN.dashboardMod = {};

  mod.render = function (el) {
    if (S.config && EVKIN.normalizeConfig) S.config = EVKIN.normalizeConfig(S.config);
    const st = S.importStats;
    const rekap = Array.isArray(S.rekap) ? S.rekap : [];
    const daily = Array.isArray(S.daily) ? S.daily : [];
    const final = Array.isArray(S.final) ? S.final : [];
    const totalSDM = st ? st.totalSDM : 0;
    const totalRec = st ? st.totalRecord : 0;
    const effDays = EVKIN.configMod.effectiveDays(S.month);
    const totalHariAbsen = rekap.reduce((a, r) => a + (Number(r.jumlahHariAbsen) || 0), 0);
    const anomali = daily.filter(x => x.statusDuplikasi).length;
    const tidakLengkap = daily.filter(x => x.statusLengkap === 'Tidak Lengkap').length;
    const avgPot = rekap.length ? U.round2(rekap.reduce((a, r) => a + (Number(r.total) || 0), 0) / rekap.length) : 0;
    const totalNominal = final.reduce((a, f) => a + (Number(f.nominal) || 0), 0);
    const totalPay = final.reduce((a, f) => a + (Number(f.finalDibayar) || 0), 0);
    const dataValid = Math.max(0, totalRec - anomali - tidakLengkap);
    const quality = totalRec ? Math.max(0, Math.min(100, dataValid / totalRec * 100)) : 0;
    const workflowSteps = EVKIN.STEPS.filter(s => s.workflow === true);
    const doneCount = workflowSteps.filter(s => EVKIN.stepDone(s.id)).length;
    const workflowPct = workflowSteps.length ? Math.round(doneCount / workflowSteps.length * 100) : 0;
    const nextStep = workflowSteps.find(s => !EVKIN.stepDone(s.id) && EVKIN.stepUnlocked(s.id)) || { id: 'dashboard', label: 'Tinjau Dashboard' };
    const hasData = totalRec > 0 || totalSDM > 0;
    const monthParts = String(S.month || '').split('-').map(Number);
    const calendarDays = monthParts.length === 2 && monthParts.every(Number.isFinite)
      ? new Date(monthParts[0], monthParts[1], 0).getDate()
      : 0;
    const workflowNodes = workflowSteps.map((step, index) => {
      const done = EVKIN.stepDone(step.id);
      const unlocked = EVKIN.stepUnlocked(step.id);
      const currentStep = step.id === nextStep.id;
      const stateClass = done ? 'done' : (currentStep ? 'current' : (unlocked ? 'ready' : 'locked'));
      return `<button class="workflow-node ${stateClass}" type="button" data-workflow-node="${step.id}" ${unlocked ? '' : 'disabled'} aria-label="${U.esc(step.label)}">
        <span>${done ? '<i data-lucide="check"></i>' : index + 1}</span>
      </button>${index < workflowSteps.length - 1 ? `<span class="workflow-connector ${done ? 'done' : ''}"></span>` : ''}`;
    }).join('');

    el.innerHTML = `
      <section class="liquid-dashboard">
      <section class="dashboard-overview">
        <div class="dashboard-overview-main">
          <div class="dashboard-heading">
            <div>
              <span class="dashboard-eyebrow">Ruang kerja absensi</span>
              <h2>Pengolahan Tukin ${S.month ? U.monthLabel(S.month) : ''}</h2>
              <p class="dashboard-status ${hasData && (anomali + tidakLengkap) ? 'warning' : ''}">
                <i data-lucide="${hasData && (anomali + tidakLengkap) ? 'triangle-alert' : 'sparkles'}"></i>
                ${hasData ? `${anomali + tidakLengkap} data memerlukan perhatian sebelum proses dilanjutkan.` : 'Siapkan data periode kerja untuk memulai proses EVKIN.'}
              </p>
            </div>
            <div class="dashboard-tools">
              <button class="btn btn-primary btn-sm" id="gotoTukinBtn" title="Buka Pengelolaan Tukin (70/30)"><i data-lucide="calendar-range"></i><span>Kelola Tukin</span></button>
              <button class="btn btn-ghost btn-sm" id="reconcileBtn" data-testid="reconcile-button" title="Rekonsiliasi"><i data-lucide="clipboard-check"></i><span>Rekonsiliasi</span></button>
              <button class="btn btn-ghost btn-sm" id="auditBtn" data-testid="audit-button" title="Audit Log"><i data-lucide="activity"></i><span>Audit Log</span></button>
              <details class="danger-menu"><summary class="btn btn-ghost btn-sm" title="Tindakan lainnya"><i data-lucide="ellipsis"></i></summary><div class="danger-menu-popover">
              <button class="btn btn-danger btn-sm" id="deleteAbsensiBackendBtn" data-testid="delete-absensi-backend"><i data-lucide="trash-2"></i> Hapus Data Workflow</button>
                <button class="btn btn-ghost btn-sm" id="dbResetBtn" data-testid="db-reset-button" title="Hanya membersihkan cache lokal; data server tidak berubah"><i data-lucide="database"></i> Bersihkan Cache Browser</button>
                <button class="btn btn-danger btn-sm" id="backendWorkflowResetBtn" data-testid="backend-workflow-reset-button"><i data-lucide="server-cog"></i> Reset Database Workflow Backend</button>
                <button class="btn btn-danger btn-sm" id="workflowResultsResetBtn"><i data-lucide="calculator"></i> Hapus Hasil Hitungan Workflow</button>
                <button class="btn btn-danger btn-sm" id="resetBtn" data-testid="reset-button"><i data-lucide="trash-2"></i> Reset Semua</button>
              </div></details>
            </div>
          </div>

          <div class="workflow-precision">
            <div class="workflow-precision-head"><span>Tahap ${Math.min(doneCount + 1, workflowSteps.length)} dari ${workflowSteps.length}</span><strong>${workflowPct}% selesai</strong></div>
            <div class="workflow-precision-track">${workflowNodes}</div>
          </div>
        </div>
        <article class="next-action-card">
          <div class="next-action-copy"><span>Langkah berikutnya</span><h3>${nextStep.label}</h3><p>${hasData ? 'Lanjutkan workflow berdasarkan data yang sudah tersimpan.' : 'Impor data absensi untuk memulai proses perhitungan.'}</p></div>
          <div class="next-action-icon"><i data-lucide="file-search"></i></div>
          <button class="btn btn-primary" id="nextActionBtn">${hasData ? 'Periksa Sekarang' : 'Import Data'} <i data-lucide="chevron-right"></i></button>
        </article>
      </section>

      <div class="dashboard-kpis">
        ${sc('Total SDM', totalSDM, 'users', 'blue', false, 'Orang')}
        ${sc('Total Record', totalRec, 'database', 'green', false, 'Record')}
        ${sc('Kualitas Data', U.round2(quality) + '%', 'shield-check', quality >= 95 ? 'green' : 'blue', false, quality >= 95 ? 'Sangat baik' : 'Perlu validasi')}
        ${sc('Tukin Dibayar', U.fmtRupiah(totalPay), 'wallet', 'blue', true, 'Total estimasi')}
      </div>

      <div class="dashboard-lower">
        <section class="attention-card">
          <div class="panel-title"><div><h3>Perlu perhatian</h3><p>Validasi sebelum melanjutkan</p></div><span class="attention-total">${anomali + tidakLengkap}</span></div>
          <button class="attention-row" id="attentionIncomplete"><span class="attention-icon warn"><i data-lucide="calendar-x"></i></span><span><strong>${tidakLengkap}</strong> absensi tidak lengkap</span><span class="attention-link">Lihat detail</span><i data-lucide="chevron-right"></i></button>
          <button class="attention-row" id="attentionDuplicate"><span class="attention-icon warn"><i data-lucide="layers-3"></i></span><span><strong>${anomali}</strong> data duplikat</span><span class="attention-link">Lihat detail</span><i data-lucide="chevron-right"></i></button>
          <div class="attention-row static"><span class="attention-icon blue"><i data-lucide="clock-3"></i></span><span><strong>${effDays}</strong> hari efektif</span><span class="attention-link">Periode aktif</span><i data-lucide="check"></i></div>
        </section>
        <section class="activity-card">
          <div class="panel-title"><div><h3>Ringkasan periode</h3><p>${S.month ? U.monthLabel(S.month) : 'Belum ada periode aktif'}</p></div><span class="period-summary-icon"><i data-lucide="list"></i></span></div>
          <div class="period-table-wrap"><table class="period-summary-table"><thead><tr><th>Komponen</th><th>Nilai</th><th>Status</th></tr></thead><tbody>
            <tr><td>Total hari kalender</td><td>${calendarDays || '—'}</td><td>—</td></tr>
            <tr><td>Hari efektif</td><td>${effDays}</td><td class="positive">Aktif</td></tr>
            <tr><td>Total hari absen</td><td>${totalHariAbsen}</td><td>—</td></tr>
            <tr><td>Rata-rata potongan</td><td>${avgPot}%</td><td>—</td></tr>
            <tr><td>Nominal potongan</td><td>${U.fmtRupiah(totalNominal)}</td><td>—</td></tr>
          </tbody></table></div>
        </section>
      </div>

      <div id="serverStatsContainer"></div>
      </section>`;
    el.querySelector('#deleteAbsensiBackendBtn').onclick = (event) => EVKIN.confirmResetWorkflow(event.currentTarget);
    el.querySelector('#workflowResultsResetBtn').onclick = async () => {
      try {
        await EVKIN.resetModuleData('workflow_results', 'Hasil Hitungan Workflow', ['daily_data', 'rekap_sdm', 'final_absensi'], async () => {
          await EVKIN.load(); EVKIN.go('dashboard');
        });
      } catch (error) { EVKIN.toast(error.message, 'error'); }
    };
    el.querySelector('#resetBtn').onclick = async () => {
      if (!confirm('Reset hasil workflow? Data sumber, konfigurasi, dan Master SDM tetap dipertahankan.')) return;
      try { await EVKIN.resetAll(); EVKIN.go('dashboard'); EVKIN.toast('Aplikasi direset'); }
      catch (error) { EVKIN.toast(error.message, 'error'); }
    };
    el.querySelector('#dbResetBtn').onclick = async () => {
      if (!confirm('Bersihkan cache browser di perangkat ini? Data server tidak akan diubah.')) return;
      const result = await EVKIN.clearBrowserCache();
      if (result.status === 'SUCCESS') EVKIN.toast('Cache browser berhasil dibersihkan.', 'success');
      else if (result.status === 'BLOCKED') EVKIN.toast('Cache browser masih digunakan tab EVKIN lain. Tutup tab lain lalu coba lagi.', 'warning');
      else EVKIN.toast('Cache browser belum berhasil dibersihkan: ' + (result.error || result.status), 'warning');
    };
    el.querySelector('#backendWorkflowResetBtn').onclick = async () => {
      const button = el.querySelector('#backendWorkflowResetBtn');
      button.disabled = true;
      try {
        await EVKIN.resetModuleData('workflow_results', 'Hasil Workflow Backend', ['daily_data','rekap_sdm','final_absensi'], async result => {
          if (!await EVKIN.load()) throw new Error('Reset selesai, tetapi pemuatan ulang gagal. Muat ulang halaman.');
          EVKIN.go('dashboard');
          return result;
        });
      } catch (error) {
        console.error('Reset database workflow backend gagal:', error);
        EVKIN.toast(error.message || 'Reset database workflow backend gagal.', 'error');
      } finally {
        button.disabled = false;
      }
    };
    const gotoTukin = el.querySelector('#gotoTukinBtn');
    if (gotoTukin) gotoTukin.onclick = () => { EVKIN.go('tukin-annual'); };
    el.querySelector('#reconcileBtn').onclick = () => { EVKIN.go('reconcile'); };
    el.querySelector('#auditBtn').onclick = () => { EVKIN.go('audit-viewer'); };
    el.querySelector('#nextActionBtn').onclick = () => { EVKIN.go(nextStep.id === 'dashboard' ? 'import' : nextStep.id); };
    el.querySelector('#attentionIncomplete').onclick = () => { EVKIN.go(EVKIN.stepUnlocked('harian') ? 'harian' : 'import'); };
    el.querySelector('#attentionDuplicate').onclick = () => { EVKIN.go(EVKIN.stepUnlocked('harian') ? 'harian' : 'import'); };
    el.querySelectorAll('[data-workflow-node]').forEach(node => {
      node.onclick = () => EVKIN.go(node.dataset.workflowNode);
    });


    // Fetch server-side stats/flags if backend analytics available
    (async function () {
      if (EVKIN.exportBackend && EVKIN.exportBackend.enabled) {
        try {
          const bulan = S.month || null;
          const stats = await EVKIN.exportBackend.getStatsCounts(bulan);
          const flags = await EVKIN.exportBackend.getWorkflowFlags(bulan);
          const container = el.querySelector('#serverStatsContainer');
          const counts = stats && stats.counts ? stats.counts : {};
          const safeCount = key => {
            const value = Number(counts[key]);
            return Number.isFinite(value) ? value : 0;
          };
          const flagMap = flags && flags.flags ? flags.flags : {};
          const semifinalKey = 'semifinal:' + (bulan || '') ;
          const semifinalFlag = flagMap[semifinalKey] || null;
          const html = `
            <details class="card server-analytics">
              <summary><span><strong>Analitik workflow server</strong><small>Data proses dan verifikasi backend</small></span><i data-lucide="chevron-down"></i></summary>
              <div class="server-analytics-body">
              <div style="display:flex; gap:10px; flex-wrap:wrap;">
                <div class="stat-card blue"><div class="stat-icon"><i data-lucide="database"></i></div><div><div class="stat-val">${safeCount('raw_import')}</div><div class="stat-label">Raw Import</div></div></div>
                <div class="stat-card blue"><div class="stat-icon"><i data-lucide="calendar"></i></div><div><div class="stat-val">${safeCount('hitung_harian')}</div><div class="stat-label">Hitung Harian</div></div></div>
                <div class="stat-card blue"><div class="stat-icon"><i data-lucide="users"></i></div><div><div class="stat-val">${safeCount('rekap_sdm')}</div><div class="stat-label">Rekap SDM</div></div></div>
                <div class="stat-card green"><div class="stat-icon"><i data-lucide="file-spreadsheet"></i></div><div><div class="stat-val">${safeCount('final_absensi')}</div><div class="stat-label">Final Absensi</div></div></div>
                <div style="flex:1"></div>
                <div class="stat-card ${semifinalFlag && semifinalFlag.verified ? 'green' : 'yellow'}"><div class="stat-icon"><i data-lucide="shield-check"></i></div><div><div class="stat-val">${semifinalFlag && semifinalFlag.verified ? 'Terverifikasi' : 'Belum'}</div><div class="stat-label">Server Semifinal</div></div></div>
              </div>
              <div style="margin-top:8px; display:flex; gap:8px;">
                <button class="btn btn-ghost btn-sm" id="downloadPerUnit">CSV per-Unit</button>
                <button class="btn btn-ghost btn-sm" id="downloadPerJabatan">CSV per-Jabatan</button>
                <button class="btn btn-ghost btn-sm" id="downloadPerStatus">CSV per-Status</button>
              </div>
              </div>
            </details>`;
          container.innerHTML = html;
          if (window.lucide) lucide.createIcons();
          container.querySelector('#downloadPerUnit').onclick = () => { EVKIN.exportBackend.downloadAggregatesCSV('per-unit', bulan).catch(err => EVKIN.toast('Download gagal: ' + err.message, 'error')); };
          container.querySelector('#downloadPerJabatan').onclick = () => { EVKIN.exportBackend.downloadAggregatesCSV('per-jabatan', bulan).catch(err => EVKIN.toast('Download gagal: ' + err.message, 'error')); };
          container.querySelector('#downloadPerStatus').onclick = () => { EVKIN.exportBackend.downloadAggregatesCSV('per-status', bulan).catch(err => EVKIN.toast('Download gagal: ' + err.message, 'error')); };
        } catch (err) {
          console.warn('Gagal ambil server-side stats:', err);
        }
      }
    })();
  };

  function progress() {
    return EVKIN.STEPS.filter(s => s.id !== 'dashboard' && s.workflow !== false).map(s => {
      const done = EVKIN.stepDone(s.id), unlocked = EVKIN.stepUnlocked(s.id);
      const state = done ? 'done' : (unlocked ? 'ready' : 'locked');
      const icon = done ? '✓' : (unlocked ? '●' : '🔒');
      return `<div class="prog ${state}" onclick="${unlocked ? `EVKIN.go('${s.id}')` : ''}"><span class="pdot">${icon}</span> ${s.label}</div>`;
    }).join('');
  }
  function sc(l, v, i, c, small, meta) { return `<div class="stat-card ${c}"><div class="stat-icon"><i data-lucide="${i}"></i></div><div class="stat-copy"><div class="stat-label">${l}</div><div class="stat-val ${small ? 'small' : ''}">${v}</div>${meta ? `<div class="stat-meta">${meta}</div>` : ''}</div></div>`; }
})();

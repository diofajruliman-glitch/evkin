/* EVKIN ABSENSI v3.1 - rekap.js */
(function () {
  const EVKIN = window.EVKIN;
  const U = EVKIN.util, S = EVKIN.state, C = EVKIN.calc;
  const mod = EVKIN.rekapMod = {};
  let nipSearch = '';
  let pageSize = 50, monthFilter = '';
  function coverageView() {
    const count = S.rekap.reduce((sum,row) => sum + Number(row.jumlahRecord || 0),0);
    return `<div class="alert info">Harian seluruh bulan: <b>${S.daily.length}</b> record · Direkap: <b>${count}</b> record → <b>${S.rekap.length}</b> baris NIP/bulan · <b>${new Set(S.rekap.map(r => r.nip)).size}</b> SDM unik.</div>`;
  }

  mod.process = async function (options = {}) {
    if (EVKIN.ensureWorkflowDataForComputation) await EVKIN.ensureWorkflowDataForComputation(['daily_data']);
    if (!S.daily.length) throw new Error('Belum ada hasil Hitung Harian.');
    if (!S.semifinalVerified) throw new Error('Verifikasi Semi Final sebelum memproses Rekap SDM.');
    const ctx = C.buildContext();
    if (S.data && S.data.length && EVKIN.assertDailyCoverage) EVKIN.assertDailyCoverage();
    const byNip = {};
    S.daily.forEach(d => {
      const month = d.monthKey || d.bulan || String(d.tanggalKey || '').slice(0,7);
      const key = month + '|' + d.nip;
      (byNip[key] = byNip[key] || []).push(d);
    });
    if (!Object.keys(byNip).length) throw new Error('Hasil Harian tidak sesuai bulan aktif. Hitung ulang terlebih dahulu.');
    for (const records of Object.values(byNip)) {
      const dates = new Set();
      for (const record of records) {
        if (dates.has(record.tanggalKey)) throw new Error('Ada duplikat NIP/tanggal. Periksa dan koreksi di Semi Final sebelum Rekap.');
        dates.add(record.tanggalKey);
      }
    }

    const rekap = [];
    const calendarCache = new Map();
    const leavesByNip = new Map();
    const groups = Object.keys(byNip);
    if (EVKIN.progress) EVKIN.progress({title: 'Rekap SDM', percent: 0, message: `Menyiapkan ${groups.length} grup NIP/bulan...`});
    ctx.cfg.leavePeriods.forEach(l => {
      const key = String(l.nip).trim();
      if (!leavesByNip.has(key)) leavesByNip.set(key, []);
      leavesByNip.get(key).push(l);
    });
    for (let groupIndex = 0; groupIndex < groups.length; groupIndex++) {
      const group = groups[groupIndex];
      const recs = byNip[group];
      const nip = recs[0].nip;
      const month = recs[0].monthKey || recs[0].bulan || String(recs[0].tanggalKey || '').slice(0,7);
      if (!calendarCache.has(month)) {
        const [yy, mm] = month.split('-').map(Number);
        const dates = new Set();
        for (let dd = 1; dd <= new Date(Date.UTC(yy, mm, 0)).getUTCDate(); dd++) {
          const key = `${yy}-${U.pad(mm)}-${U.pad(dd)}`;
          const weekday = new Date(Date.UTC(yy, mm - 1, dd)).getUTCDay();
          if (weekday !== 0 && weekday !== 6 && !ctx.holidaySet.has(key)) dates.add(key);
        }
        calendarCache.set(month, {dates, days:EVKIN.configMod.effectiveDays(month)});
      }
      const {dates:effDateKeys, days:effDays} = calendarCache.get(month);
      const metode = recs[0].metode;
      const jabatan = recs[0].jabatan;
      const nama = recs[0].nama;

      const attendanceRecs = recs.filter(r => r.jamMasuk || r.jamPulang);
      const dateKeys = new Set(attendanceRecs.map(r => r.tanggalKey));
      const jumlahHariAbsen = dateKeys.size;

      // duplikasi tanggal
      const perDate = {};
      attendanceRecs.forEach(r => { (perDate[r.tanggalKey] = perDate[r.tanggalKey] || []).push(r); });
      let dupDates = 0;
      Object.values(perDate).forEach(g => { if (g.length > 1) dupDates++; });

      // tanggal hadir/dikecualikan (record ada + cuti periode)
      const presentAll = new Set(dateKeys);
      recs.filter(r => r.excused || r.statusCuti).forEach(r => presentAll.add(r.tanggalKey));
      (leavesByNip.get(String(nip).trim()) || []).forEach(l => {
        let cur = U.parseDate(l.mulai); const end = U.parseDate(l.selesai);
        let guard = 0;
        while (cur && end && cur.key <= end.key && guard < 400) {
          if (cur.monthKey === month) presentAll.add(cur.key);
          cur = U.addDays(cur, 1); guard++;
        }
      });

      let hariAdaAbsensi, hariKurang;
      if (metode === 'reguler') {
        let attended = 0; effDateKeys.forEach(k => { if (presentAll.has(k)) attended++; });
        hariAdaAbsensi = attended;
      } else {
        // Durasi Aktual: kehadiran dihitung untuk seluruh Senin-Minggu.
        let attended = 0; presentAll.forEach(k => { if (k.startsWith(month)) attended++; });
        hariAdaAbsensi = attended;
      }
      hariKurang = Math.max(0, effDays - hariAdaAbsensi);

      // Semua potongan harian dijumlahkan menjadi total potongan bulanan.
      const sumKet = C.round2(recs.reduce((a, r) => a + (Number(r.potKeterlambatan) || 0), 0));
      const sumPsw = C.round2(recs.reduce((a, r) => a + (Number(r.potPulangCepat) || 0), 0));
      const sumDur = C.round2(recs.reduce((a, r) => a + (Number(r.potDurasi) || 0), 0));
      const sumTL = C.round2(recs.reduce((a, r) => a + (Number(r.potTidakLengkap) || 0), 0));
      const potHK = C.round2(hariKurang * ctx.cfg.potonganHariKurangPerHari);

      let totalRaw;
      if (metode === 'durasi') {
        // Durasi Aktual: Tdk Rekam tidak masuk total. Hari Kurang hanya masuk
        // ketika jumlah hari absen belum memenuhi hari efektif.
        const totalHariKurang = hariAdaAbsensi < effDays ? potHK : 0;
        totalRaw = C.round2(sumKet + sumPsw + sumDur + totalHariKurang);
      } else {
        // Reguler: Durasi dan Hari Kurang tidak masuk total; Tdk Rekam tetap masuk.
        totalRaw = C.round2(sumKet + sumPsw + sumTL);
      }
      const total = Math.min(100, totalRaw);

      let status = 'Bersih';
      if (dupDates > 0) status = 'Perlu Verifikasi';
      else if (total > 0) status = 'Ada Potongan';
      if (totalRaw > 100) status = 'Dibatasi 100%';

      rekap.push({
        nip, nama, jabatan, metode, bulan: month, jumlahRecord: recs.length,
        hariEfektif: effDays, hariAdaAbsensi, hariKurang, jumlahHariAbsen,
        potKeterlambatan: sumKet, potPulangCepat: sumPsw, potDurasi: sumDur,
        potHariKurang: potHK, potTidakLengkap: sumTL,
        totalRaw, total, dupDates, status
      });
      if (EVKIN.progress) EVKIN.progress({title: 'Rekap SDM', percent: Math.round((groupIndex + 1) / groups.length * 100), message: `Memproses ${groupIndex + 1} dari ${groups.length} grup NIP/bulan...`});
      if (groupIndex + 1 < groups.length) await new Promise(resolve => setTimeout(resolve, 0));
    }

    rekap.sort((a, b) => String(a.nama || '').localeCompare(String(b.nama || '')));
    S.rekap = rekap;
    if (EVKIN.db && EVKIN.db.saveRekap) {
      EVKIN.db.saveRekap(rekap).catch((err) => console.warn('Gagal simpan rekap ke IndexedDB:', err));
    }
    EVKIN.invalidateFrom('rekap', {save:false});
    if (options.sync !== false) await EVKIN.syncBackendWorkflow(undefined, {derivedOnly:true});
    EVKIN.auditMod.logStage('rekap', 'PROCESSED:' + rekap.length);
    if (EVKIN.progress) EVKIN.progress({title: 'Rekap SDM', percent: 100, message: `${rekap.length} baris rekap selesai dan tersimpan.`, state: 'done'});
    return rekap;
  };

  mod.render = function (el) {
    if (!EVKIN.stepUnlocked('rekap')) { el.innerHTML = lock('Verifikasi Semi Final terlebih dahulu.', '⑤ Rekap SDM'); return; }
    const canProcess = S.semifinalVerified && S.daily.length > 0 && S.dailyComplete !== false;
    const expected = (S.data || []).filter(row => !row.deleted && row.tanggal && row.nip).length;
    const guidance = canProcess ? '' : `<div class="alert warn" role="status">Rekap SDM tetap dapat dibuka untuk ditinjau. ${S.dailyComplete === false || S.daily.length !== expected
      ? `Harian belum lengkap: ${S.daily.length} dari ${expected} record impor aktif. Jalankan Hitung Semua Data Impor, lalu verifikasi Semi Final.`
      : 'Verifikasi Semi Final sebelum menghitung ulang Rekap.'} Hasil Final lama tidak menggantikan tahap verifikasi.</div>
      <div class="actions-row"><button class="btn btn-secondary" id="rekapOpenHarian" ${EVKIN.stepUnlocked('harian') ? '' : 'disabled'}>Buka Hitung Harian</button><button class="btn btn-secondary" id="rekapOpenSemifinal" ${EVKIN.stepUnlocked('semifinal') ? '' : 'disabled'}>Buka Semi Final</button></div>`;
    const bindGuidance = () => {
      const daily = el.querySelector('#rekapOpenHarian');
      const verify = el.querySelector('#rekapOpenSemifinal');
      if (daily) daily.onclick = () => EVKIN.go('harian');
      if (verify) verify.onclick = () => EVKIN.go('semifinal');
    };
    if (!S.rekap.length) {
      el.innerHTML = `<div class="module-head"><h2>⑤ Rekap SDM</h2></div>
        ${guidance}
        <div class="card"><div class="alert info">${canProcess ? S.daily.length + ' record Harian terverifikasi siap direkap untuk seluruh bulan.' : 'Belum ada hasil Rekap SDM yang valid.'} Setiap NIP menghasilkan satu baris per bulan.</div>
        <button class="btn btn-primary" id="btnRekap" data-testid="rekap-run" ${canProcess ? '' : 'disabled'}><i data-lucide="users"></i> Proses Rekap SDM</button></div>`;
      bindGuidance();
      el.querySelector('#btnRekap').onclick = async () => {
        const button = el.querySelector('#btnRekap');
        button.disabled = true;
        try { await mod.process(); EVKIN.toast('Rekap selesai dan tersimpan'); EVKIN.go('rekap'); }
        catch (e) { if (EVKIN.progress) EVKIN.progress({title: 'Rekap SDM gagal', message: e.message, state: 'error'}); EVKIN.toast('Rekap gagal disimpan: ' + e.message, 'error'); }
        finally { button.disabled = false; }
      };
      return;
    }

    // ---- PAGINATION ----
    const ITEMS_PER_PAGE = pageSize;
    const r = S.rekap.filter(x => (!monthFilter || x.bulan === monthFilter) && (!nipSearch || [x.nip,x.nama,x.jabatan].some(v => String(v || '').toLowerCase().includes(nipSearch))));
    const totalPages = Math.max(1, Math.ceil(r.length / ITEMS_PER_PAGE));
    let currentPage = Math.max(0, Math.min(S.rekapPage, totalPages - 1));
    S.rekapPage = currentPage;

    const startIdx = currentPage * ITEMS_PER_PAGE;
    const endIdx = startIdx + ITEMS_PER_PAGE;
    const pageData = r.slice(startIdx, endIdx);

    const head = ['NIP', 'Nama', 'Jabatan', 'Bulan', 'Record Sumber', 'Hari Efektif', 'Hari Absensi', 'Hari Kurang', 'Jml Hari Absen', 'Terlambat %', 'Plg Cepat %', 'Durasi %', 'Hari Kurang %', 'Tdk Rekam %', 'Total Potongan %', 'Status'];
    const rows = pageData.map(x => `<tr class="${x.dupDates ? 'row-flag' : ''}">
      <td>${U.esc(x.nip)}</td><td>${U.esc(x.nama)}</td><td>${U.esc(x.jabatan)}</td><td>${U.monthLabel(x.bulan)}</td>
      <td>${x.jumlahRecord ?? '—'}</td><td>${x.hariEfektif}</td><td>${x.hariAdaAbsensi}</td><td>${x.hariKurang}</td><td>${x.jumlahHariAbsen}${x.dupDates ? ' <span class="pill red">dup</span>' : ''}</td>
      <td>${x.potKeterlambatan}%</td><td>${x.potPulangCepat}%</td><td>${x.potDurasi}%</td><td>${x.potHariKurang}%</td><td>${x.potTidakLengkap}%</td>
      <td><b>${x.total}%</b>${x.totalRaw > 100 ? ' <span class="muted">(' + x.totalRaw + '%)</span>' : ''}</td>
      <td><span class="pill ${statusColor(x.status)}">${U.esc(x.status)}</span></td></tr>`).join('') || '<tr><td colspan="16" class="muted">Tidak ada SDM yang sesuai pencarian/filter.</td></tr>';

    const paginationInfo = `<div style="text-align: center; margin: 16px 0; font-size: 13px; color: #666;">
      <strong>Data ${r.length ? startIdx + 1 : 0}–${Math.min(endIdx, r.length)} dari ${r.length} SDM</strong> |
      <strong>Halaman ${currentPage + 1} dari ${totalPages}</strong>
    </div>`;

    const paginationButtons = `<div style="display: flex; gap: 8px; justify-content: center; margin-top: 16px; flex-wrap: wrap;">
      <button class="btn btn-secondary" id="btnPrev" ${currentPage === 0 ? 'disabled' : ''}>
        <i data-lucide="chevron-left"></i> Sebelumnya
      </button>
      <button class="btn btn-secondary" id="btnNext" ${currentPage >= totalPages - 1 ? 'disabled' : ''}>
        Berikutnya <i data-lucide="chevron-right"></i>
      </button>
    </div>`;

    el.innerHTML = `<div class="module-head"><div><h2>⑤ Rekap SDM</h2><p class="muted">Seluruh bulan impor; 1 NIP = 1 baris per bulan.</p></div>
      <button class="btn btn-secondary" id="btnRekap2" data-testid="rekap-rerun" ${canProcess ? '' : 'disabled'}><i data-lucide="refresh-cw"></i> Hitung Ulang</button></div>
      ${guidance}
      ${coverageView()}
      <div class="card"><div class="filter-bar"><select id="rekapMonthFilter"><option value="">Semua bulan</option>${[...new Set(S.rekap.map(r => r.bulan))].sort().map(m => `<option value="${m}" ${monthFilter === m ? 'selected' : ''}>${U.monthLabel(m)}</option>`).join('')}</select><select id="rekapPageSize">${[25,50,100,250].map(n => `<option value="${n}" ${pageSize === n ? 'selected' : ''}>${n} baris</option>`).join('')}</select><input type="search" id="rekapNipSearch" placeholder="Cari NIP, nama, jabatan..." value="${U.esc(nipSearch)}" data-testid="rekap-nip-search" /></div><div class="table-wrap"><table class="data-table sm" data-testid="rekap-table"><thead><tr>${head.map(h => '<th>' + h + '</th>').join('')}</tr></thead><tbody>${rows}</tbody></table></div>
      ${paginationInfo}
      ${paginationButtons}
      <div class="actions-row"><button class="btn btn-ghost" onclick="EVKIN.go('semifinal')"><i data-lucide="arrow-left"></i> Kembali</button>
      <button class="btn btn-secondary" id="rekapToExport"><i data-lucide="file-down"></i> Export Rekap Absensi</button>
      <button class="btn btn-primary" id="rekapToTukin">Lanjut: Pengelolaan Tukin (70/30) <i data-lucide="arrow-right"></i></button></div></div>`;

    bindGuidance();
    el.querySelector('#btnRekap2').onclick = async () => {
      const button = el.querySelector('#btnRekap2');
      button.disabled = true;
      try { await mod.process(); EVKIN.toast('Rekap diperbarui dan tersimpan'); EVKIN.go('rekap'); }
      catch (error) { if (EVKIN.progress) EVKIN.progress({title: 'Rekap SDM gagal', message: error.message, state: 'error'}); EVKIN.toast('Rekap gagal: ' + error.message, 'error'); }
      finally { button.disabled = false; }
    };
    const btnExport = el.querySelector('#rekapToExport');
    if (btnExport) btnExport.onclick = () => { if (monthFilter) S.month = monthFilter; EVKIN.go('export'); };
    const btnTukin = el.querySelector('#rekapToTukin');
    if (btnTukin) btnTukin.onclick = () => { if (monthFilter) S.month = monthFilter; EVKIN.go('tukin-annual'); };
    const btnFinal = el.querySelector('#rekapToFinal');
    if (btnFinal) btnFinal.onclick = () => { if (monthFilter) S.month = monthFilter; EVKIN.go('final'); };
    el.querySelector('#rekapMonthFilter').onchange = e => {monthFilter=e.target.value; S.rekapPage=0; EVKIN.go('rekap');};
    el.querySelector('#rekapPageSize').onchange = e => {pageSize=Number(e.target.value); S.rekapPage=0; EVKIN.go('rekap');};
    el.querySelector('#rekapNipSearch').oninput = (e) => {
      nipSearch = e.target.value.toLowerCase(); S.rekapPage = 0;
      mod.render(el);
      const input = el.querySelector('#rekapNipSearch');
      input.focus(); input.setSelectionRange(input.value.length, input.value.length);
    };

    const btnPrev = el.querySelector('#btnPrev');
    const btnNext = el.querySelector('#btnNext');
    if (btnPrev) btnPrev.onclick = () => { S.rekapPage = Math.max(0, S.rekapPage - 1); EVKIN.go('rekap'); };
    if (btnNext) btnNext.onclick = () => { S.rekapPage = Math.min(totalPages - 1, S.rekapPage + 1); EVKIN.go('rekap'); };

    if (window.lucide) lucide.createIcons();
  };

  function statusColor(s) { return s === 'Bersih' ? 'green' : (s === 'Perlu Verifikasi' ? 'yellow' : (s === 'Dibatasi 100%' ? 'red' : 'blue')); }
  function lock(m, t) { return `<div class="module-head"><h2>${t} <span class="pill gray">🔒 Terkunci</span></h2></div><div class="alert warn">${m}</div>`; }
})();

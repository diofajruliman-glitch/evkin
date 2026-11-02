/* Canonical Final Tukin: decision -> immutable calculation snapshot. */
(function () {
  'use strict';
  const E = window.EVKIN;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const money = value => value == null ? '—' : new Intl.NumberFormat('id-ID').format(value);
  const url = path => new URL('/api/final-tukin' + path, E.exportBackend?.apiBase || location.origin).toString();
  let root, period = '', calculations = [], decisions = [], calculation = null, page = 0;

  async function api(path, options) {
    const response = await fetch(url(path), {cache:'no-store', ...(options || {})});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || response.statusText);
    return body;
  }
  const post = (path, payload) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  async function action(callback) {
    const buttons = [...root.querySelectorAll('button')];
    buttons.forEach(button => { button.disabled = true; });
    try { await callback(); } catch (error) { E.toast(error.message || String(error), 'error'); }
    finally { buttons.forEach(button => { if (button.isConnected) button.disabled = false; }); }
  }
  function options(rows, selected, emptyLabel) {
    const empty = emptyLabel ? `<option value="">${esc(emptyLabel)}</option>` : '';
    return empty + rows.map(row => `<option value="${esc(row.value)}" ${row.value === selected ? 'selected' : ''}>${esc(row.label)}</option>`).join('');
  }
  const badge = (value, risk) => `<span class="pill ${risk ? 'red' : 'green'}">${esc(value)}</span>`;
  const decisionForCalculation = calculationId => decisions.find(row => row.calculation_id === calculationId);

  function rowTable(rows) {
    const start = page * 100;
    const visible = rows.slice(start, start + 100);
    const columns = [
      ['nip','NIP'],['nama','Nama'],['periode','Periode'],['version','Calculation Version'],
      ['tukin_bruto','TUKIN Bruto'],['komponen_absensi','Komponen Absensi'],
      ['potongan_absensi','Potongan Absensi'],['komponen_skp','Komponen SKP'],
      ['potongan_skp','Potongan SKP'],['adjustment','Adjustment'],
      ['total_potongan','Total Potongan'],['tukin_dibayar','TUKIN Dibayar'],
      ['snapshot_status','Status Calculation'],['calculated_at','Calculated At']
    ];
    const amounts = new Set(['tukin_bruto','komponen_absensi','potongan_absensi','komponen_skp','potongan_skp','adjustment','total_potongan','tukin_dibayar']);
    return `<p>${money(rows.length)} baris · ${rows.length ? start + 1 : 0}–${Math.min(start + 100, rows.length)}</p>
      <div style="overflow:auto;max-height:560px"><table class="data-table"><thead><tr>${columns.map(([, label]) => `<th>${esc(label)}</th>`).join('')}<th>Traceability</th></tr></thead>
      <tbody>${visible.map(row => `<tr>${columns.map(([key]) => `<td>${esc(amounts.has(key) ? money(row[key]) : (row[key] ?? '—'))}</td>`).join('')}
        <td><details><summary>Sumber</summary><small>Master v${esc(row.master_version ?? '—')}<br>Absensi: ${esc(row.attendance_source_id || '—')} / batch ${esc(row.attendance_batch_id || '—')}<br>SKP: ${esc(row.skp_source_id || '—')} / version ${esc(row.skp_source_version || '—')} / batch ${esc(row.skp_batch_id || '—')}</small></details></td></tr>`).join('')}</tbody></table></div>
      <p><button id="ftPrev" ${page === 0 ? 'disabled' : ''}>Sebelumnya</button> <button id="ftNext" ${start + 100 >= rows.length ? 'disabled' : ''}>Berikutnya</button></p>`;
  }

  function drawSnapshot() {
    const target = root.querySelector('#ftSnapshot');
    if (!target) return;
    if (!calculation) { target.innerHTML = '<p>Pilih Calculation vX untuk mereview snapshot immutable.</p>'; return; }
    const batch = calculation.batch;
    const decision = decisionForCalculation(batch.calculation_id);
    const stale = batch.source_status === 'STALE';
    target.innerHTML = `<div class="card" style="padding:16px;margin-top:16px"><h3>Calculation v${esc(batch.version)}</h3>
      <p>${badge(stale ? 'SOURCE UPDATED / SNAPSHOT STALE' : 'SOURCE CURRENT', stale)} ${badge(batch.status, batch.status !== 'CALCULATED')} ${decision ? badge(decision.display_status || decision.status, false) : badge('BELUM FINAL', true)}</p>
      <p>Calculation ID: <code>${esc(batch.calculation_id)}</code> · Mesin: ${esc(batch.calculation_version)} · Dihitung ${esc(batch.calculated_at || '—')} oleh ${esc(batch.calculated_by || '—')}</p>
      ${decision ? `<p>Final Decision: <code>${esc(decision.id)}</code> · disetujui ${esc(decision.approved_at || '—')} oleh ${esc(decision.approved_by || '—')} · dibayar ${esc(decision.paid_at || '—')}</p>
        <p><a class="btn" href="${esc(url('/decisions/' + decision.id + '/export'))}" download>Export Final Canonical</a> ${decision.status === 'FINAL' && decision.is_current_decision ? '<button id="ftPaid">Tandai PAID</button>' : ''}</p>`
        : `<div class="ft-mapping"><label>Approver <input id="ftApprover" maxlength="150"></label><label>Alasan approval <input id="ftReason" maxlength="2000"></label></div>
        <button class="btn btn-primary" id="ftApprove" ${stale || batch.status !== 'CALCULATED' ? 'disabled' : ''}>Approve Calculation v${esc(batch.version)} sebagai FINAL</button>
        ${stale ? '<p role="alert">Approval diblokir. Buat calculation version baru dari Pengelolaan TUKIN; Final lama tidak diubah.</p>' : ''}`}
      <div id="ftRows">${rowTable(calculation.rows)}</div></div>`;
    const prev = target.querySelector('#ftPrev'), next = target.querySelector('#ftNext');
    if (prev) prev.onclick = () => { page = Math.max(0, page - 1); drawSnapshot(); };
    if (next) next.onclick = () => { page += 1; drawSnapshot(); };
    const approve = target.querySelector('#ftApprove');
    if (approve) approve.onclick = () => action(async () => {
      const approvedBy = target.querySelector('#ftApprover').value.trim();
      const reason = target.querySelector('#ftReason').value.trim();
      if (!approvedBy || !reason) throw new Error('Approver dan alasan approval wajib diisi.');
      await post('/approve-calculation', {calculation_id:batch.calculation_id, approved_by:approvedBy, reason});
      E.toast(`Calculation v${batch.version} dikunci sebagai Final Tukin.`, 'success');
      await loadPeriod(batch.calculation_id);
    });
    const paid = target.querySelector('#ftPaid');
    if (paid) paid.onclick = () => action(async () => {
      const paidBy = prompt('Petugas pembayaran:'); if (!paidBy) return;
      const paymentReference = prompt('Referensi pembayaran / SP2D:'); if (!paymentReference) return;
      await post(`/decisions/${decision.id}/paid`, {paid_by:paidBy, payment_reference:paymentReference});
      E.toast('Final Tukin ditandai PAID dan terkunci.', 'success');
      await loadPeriod(batch.calculation_id);
    });
  }

  function drawShell(resultsOnly) {
    root.innerHTML = `<div class="card ft-screen" style="padding:20px"><h2>${resultsOnly ? 'Hasil Final TUKIN Canonical' : 'Final TUKIN Canonical'}</h2>
      <p>Final Decision → Calculation Snapshot → Absensi Final + SKP Approved → Master SDM. Halaman ini tidak menghitung ulang nominal.</p>
      <label>Periode <input id="ftPeriod" type="month" value="${esc(period)}"></label> <button id="ftRefresh">Refresh</button>
      <div id="ftStatus"></div><p><label>Calculation Snapshot <select id="ftCalculation"></select></label></p>
      <div id="ftDecisions"></div><div id="ftSnapshot"></div></div>`;
    root.querySelector('#ftPeriod').onchange = event => { period = event.target.value; action(() => loadPeriod()); };
    root.querySelector('#ftRefresh').onclick = () => action(() => loadPeriod(calculation?.batch?.calculation_id));
  }

  function drawLists(selectedId) {
    const select = root.querySelector('#ftCalculation');
    select.innerHTML = options(calculations.map(row => ({value:row.calculation_id, label:`${row.assessment_period} · Calculation v${row.version} · ${row.status} · ${row.source_status}`})), selectedId || '', 'Pilih calculation snapshot');
    root.querySelector('#ftStatus').innerHTML = calculations.length ? `<p>${money(calculations.length)} calculation snapshot · ${money(decisions.length)} Final Decision.</p>` : '<p>Belum ada calculation snapshot. Hitung dari Pengelolaan TUKIN terlebih dahulu.</p>';
    root.querySelector('#ftDecisions').innerHTML = decisions.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Final Decision</th><th>Periode</th><th>Calculation</th><th>Status Final</th><th>Status Source</th><th>Approved At</th><th>Paid At</th></tr></thead><tbody>
      ${decisions.map(row => `<tr><td><button data-decision="${esc(row.id)}">${esc(row.id)}</button></td><td>${esc(row.assessment_period)}</td><td>v${esc(row.calculation_version)}<br><small>${esc(row.calculation_id)}</small></td><td>${esc(row.display_status || row.status)}</td><td>${esc(row.source_status)}</td><td>${esc(row.approved_at || '—')}</td><td>${esc(row.paid_at || '—')}</td></tr>`).join('')}</tbody></table></div>` : '';
    select.onchange = () => action(async () => { page = 0; calculation = select.value ? await api('/calculation/' + encodeURIComponent(select.value)) : null; drawSnapshot(); });
    root.querySelectorAll('[data-decision]').forEach(button => button.onclick = () => action(async () => {
      const detail = await api('/decisions/' + encodeURIComponent(button.dataset.decision));
      calculation = {batch:detail.batch, rows:detail.rows}; page = 0; select.value = detail.batch.calculation_id; drawSnapshot();
    }));
  }

  async function loadPeriod(selectedId) {
    const query = '?assessment_period=' + encodeURIComponent(period);
    const [calculationResponse, decisionResponse] = await Promise.all([api('/calculations' + query), api('/decisions' + query)]);
    calculations = calculationResponse.rows || []; decisions = decisionResponse.rows || [];
    const wanted = selectedId || calculation?.batch?.calculation_id || calculations[0]?.calculation_id || '';
    calculation = wanted && calculations.some(row => row.calculation_id === wanted) ? await api('/calculation/' + encodeURIComponent(wanted)) : null;
    page = 0; drawLists(wanted); drawSnapshot();
  }

  function render(element, resultsOnly) {
    root = element; period = period || E.state.month || new Date().toISOString().slice(0, 7); calculation = null;
    drawShell(resultsOnly); action(() => loadPeriod());
  }
  E.finalTukinMod = {render(element) { render(element, false); }};
  E.finalTukinResultsMod = {render(element) { render(element, true); }};
})();

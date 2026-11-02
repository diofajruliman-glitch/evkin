const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

async function main() {
  const E = {};
  const sandbox = {window:{EVKIN:E},console,Date,Math,URL,setTimeout,clearTimeout};
  for (const name of ['utils','data','state','calc','config','rekap','semifinal']) {
    vm.runInNewContext(fs.readFileSync(`assets/js/${name}.js`,'utf8'),sandbox);
  }
  E.auditMod = {logStage(){}};
  E.state.month = '2026-08';
  E.state.config.effectiveDaysMode = 'manual';
  E.state.config.effectiveDaysManual = 2;
  function record(date, extra={}) {
    return {nip:'001',nama:'Tes',jabatan:'Guru SR',tanggal:E.util.parseDate(date),
      masukMin:450,pulangMin:960,zona:'WIB',...extra};
  }
  function calc(date, extra={}) {return E.calc.computeRecord(record(date,extra),E.calc.buildContext(),'original');}
  // An incomplete record on an OFF day must not create a workday penalty.
  assert.equal(calc('2026-08-02',{masukMin:null,pulangMin:null}).totalHarian,0);
  assert.equal(calc('2026-08-17',{pulangMin:null}).totalHarian,0);
  assert.equal(calc('2026-08-03',{pulangMin:null}).totalHarian,3);
  // Cross-day checkout is evaluated on the next-day timeline, so an early
  // morning checkout is not mistaken for leaving before the same-day schedule.
  const crossDay = calc('2026-08-03',{masukMin:1200,pulangMin:240,crossDay:true});
  assert.equal(crossDay.potPulangCepat,0);
  assert.equal(crossDay.potKeterlambatan,0);
  assert.equal(crossDay.totalHarian,0);
  assert.equal(crossDay.statusLengkap,'Lengkap');
  assert.equal(crossDay.statusAbsensi,'LINTAS_HARI');
  assert.equal(crossDay.durasiMin,480);
  assert.ok(crossDay.flags.includes('PULANG HARI BERIKUTNYA'));
  const shortShift = calc('2026-08-03',{jabatan:'Wali Asuh',masukMin:1380,pulangMin:60,crossDay:true});
  assert.equal(shortShift.durasiMin,120);
  assert.equal(shortShift.totalHarian,0);
  assert.equal(shortShift.potDurasi,0);
  assert.equal(calc('2026-08-03',{crossDay:true,pulangMin:null}).potTidakLengkap,3);
  assert.equal(calc('2026-08-03',{crossDay:true,masukMin:null,pulangMin:240}).potTidakLengkap,3);
  assert.equal(calc('2026-08-03',{masukMin:1200,pulangMin:1320}).potKeterlambatan,3);
  assert.equal(calc('2026-08-03',{jabatan:'Wali Asuh',masukMin:480,pulangMin:600}).potDurasi,3);
  E.state.semifinalVerified=true;
  E.state.daily=[calc('2026-08-03',{jabatan:'Wali Asuh'}),
    calc('2026-08-04',{jabatan:'Wali Asuh',masukMin:null,pulangMin:null,jenis:'Cuti Tahunan'})];
  await E.rekapMod.process({sync:false});
  assert.equal(E.state.rekap[0].hariKurang,0);
  assert.equal(E.state.rekap[0].total,0);
  // Numeric strings restored from older snapshots must sum arithmetically.
  E.state.daily=[calc('2026-08-03'),calc('2026-08-04')].map(r=>({...r,potPulangCepat:'0.5',potTidakLengkap:'0'}));
  await E.rekapMod.process({sync:false});
  assert.equal(E.state.rekap[0].total,1);
  const previous = E.state.rekap;
  E.state.daily.push({...E.state.daily[0]});
  await assert.rejects(E.rekapMod.process({sync:false}),/duplikat/i);
  assert.equal(E.state.rekap,previous);
  E.state.daily.pop();
  E.state.semifinalVerified=false;
  await assert.rejects(E.rekapMod.process({sync:false}),/verifikasi/i);
  // Recalculating Semi Final must invalidate downstream results, not build Rekap before approval.
  let rekapCalled=false;
  E.harianMod={process:async()=>{E.invalidateFrom('harian',{save:false});}};
  E.rekapMod.process=async()=>{rekapCalled=true;};
  E.syncBackendWorkflow=async()=>{};
  await E.semifinalMod.recalcChain();
  assert.equal(rekapCalled,false);
  assert.equal(E.state.rekap.length,0);
  assert.equal(E.state.semifinalVerified,false);
  const zero=E.normalizeConfig({potonganTidakLengkap:0,potonganHariKurangPerHari:0});
  assert.equal(zero.potonganTidakLengkap,0);
  assert.equal(zero.potonganHariKurangPerHari,0);
  console.log('Attendance OFF days, leave credits, numeric sums, duplicates and workflow ordering: PASS');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

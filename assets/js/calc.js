/* EVKIN ABSENSI v3.1 - calc.js
   Mesin perhitungan: durasi, keterlambatan, pulang cepat, flexi, durasi jabatan. */
(function () {
  const EVKIN = window.EVKIN || (window.EVKIN = {});
  const U = EVKIN.util;
  const M = EVKIN.master;
  const C = EVKIN.calc = {};

  // Peta bantu dari konfigurasi
  C.buildContext = function () {
    const cfg = EVKIN.state.config = EVKIN.normalizeConfig ? EVKIN.normalizeConfig(EVKIN.state.config) : (EVKIN.state.config || EVKIN.defaultConfig());
    const holidays = Array.isArray(cfg.holidays) ? cfg.holidays : [];
    const holidaySet = new Set(holidays.map(h => h.d));
    const holidayName = {};
    holidays.forEach(h => holidayName[h.d] = h.n);

    const jabMethod = {};
    const jabatanMaster = Array.isArray(cfg.jabatanMaster) ? cfg.jabatanMaster : [];
    jabatanMaster.forEach(j => jabMethod[String(j.nama || '').trim().toLowerCase()] = j.metode);

    const pegByNip = {};
    const masterPegawai = Array.isArray(cfg.masterPegawai) ? cfg.masterPegawai : [];
    masterPegawai.forEach(p => pegByNip[String(p.nip).trim()] = p);

    const jenisNo = new Set((Array.isArray(cfg.jenisNoPotongan) ? cfg.jenisNoPotongan : []).map(x => String(x).trim().toLowerCase()));

    return { cfg, holidaySet, holidayName, jabMethod, pegByNip, jenisNo };
  };

  C.resolveJabatan = function (rec, ctx) {
    // Attendance job belongs to the imported workbook, not the SDM master.
    const jab = String(rec.jabatan || '').trim();
    return jab || 'Belum Ditentukan';
  };

  C.methodOf = function (jabatan, ctx) {
    const m = ctx.jabMethod[String(jabatan).trim().toLowerCase()];
    return m === 'durasi' ? 'durasi' : 'reguler';
  };

  C.inLeavePeriod = function (nip, dateKey, ctx) {
    const nipS = String(nip).trim();
    for (const lp of ctx.cfg.leavePeriods) {
      if (String(lp.nip).trim() !== nipS) continue;
      if (dateKey >= lp.mulai && dateKey <= lp.selesai) return lp;
    }
    return null;
  };

  function tierPct(tiers, minutes) {
    for (const t of tiers) { if (minutes <= t.max) return t.pct; }
    return tiers.length ? tiers[tiers.length - 1].pct : 0;
  }

  C.durasiTierPct = function (hours, cfg) {
    const minJ = cfg.minDurasiJam; // 7.5
    if (hours >= minJ) return 0;
    if (hours >= minJ - 0.5) return 0.5;
    if (hours >= minJ - 1.0) return 1;
    if (hours >= minJ - 1.5) return 1.5;
    if (hours >= minJ - 2.0) return 2;
    return 3;
  };

  // Jam yang dipakai berdasar mode waktu
  C.timesForMode = function (rec, mode) {
    if (mode === 'wib') {
      // Normalisasi WIB - pengurangan (default)
      return { masuk: rec.masukNormMin, pulang: rec.pulangNormMin };
    }
    if (mode === 'wib-add') {
      // Normalisasi WIB - penambahan
      return { masuk: rec.masukNormMinAdd, pulang: rec.pulangNormMinAdd };
    }
    // Original
    return { masuk: rec.masukMin, pulang: rec.pulangMin };
  };

  // Hitung satu record harian -> objek hasil
  C.computeRecord = function (rec, ctx, mode) {
    const cfg = ctx.cfg;
    const jabatan = C.resolveJabatan(rec, ctx);
    const metode = C.methodOf(jabatan, ctx);
    const t = C.timesForMode(rec, mode);
    const isHoliday = ctx.holidaySet.has(rec.tanggal.key) || U.isWeekend(rec.tanggal);
    const isWali = rec.sourceFormat === 'wali';
    const isOffSchedule = isHoliday;
    const holidayLabel = ctx.holidaySet.has(rec.tanggal.key) ? (ctx.holidayName[rec.tanggal.key] || 'Libur Nasional')
      : (U.isWeekend(rec.tanggal) ? 'Akhir Pekan' : '');

    const res = {
      _id: rec._id,
      nip: rec.nip, nama: (ctx.pegByNip[String(rec.nip).trim()] || {}).nama || rec.nama, jabatan, metode,
      tanggal: rec.tanggal.str, tanggalKey: rec.tanggal.key, monthKey: rec.tanggal.monthKey,
      hari: U.dayName(rec.tanggal),
      jamMasuk: U.fmtTime(t.masuk), jamPulang: U.fmtTime(t.pulang),
      tanggalMasuk: rec.tanggalMasukObj ? rec.tanggalMasukObj.str : rec.tanggal.str,
      tanggalPulang: rec.tanggalPulangObj ? rec.tanggalPulangObj.str : (rec.tanggalPulang || rec.tanggal.str),
      masukAt: rec.masukAt || '',
      pulangAt: rec.pulangAt || '',
      jamMasukAsli: U.fmtTime(rec.masukMin), jamPulangAsli: U.fmtTime(rec.pulangMin),
      jamNormalisasi: (function(){ if (mode === 'original') return 'Original'; if (rec.zona === 'WIB') return ''; if (mode === 'wib') return U.fmtTime(rec.masukNormMin) + ' - ' + U.fmtTime(rec.pulangNormMin); if (mode === 'wib-add') return U.fmtTime(rec.masukNormMinAdd) + ' - ' + U.fmtTime(rec.pulangNormMinAdd); return ''; })(),
      zona: rec.zona,
      durasiMin: null, durasi: '',
      jenis: rec.jenis || '',
      statusHariLibur: isHoliday ? ('Ya (' + holidayLabel + ')') : 'Tidak',
      statusCuti: '',
      statusDuplikasi: rec.dupFlag ? 'Duplikasi' : '',
      statusLengkap: '',
      potKeterlambatan: 0, potPulangCepat: 0, potDurasi: 0, potHariKurang: 0, potTidakLengkap: 0,
      totalHarian: 0,
      flags: []
    };
    res.statusJadwal = isOffSchedule ? 'OFF' : 'ADA_JADWAL';
    res.statusAbsensi = 'PERLU_VALIDASI';
    if (rec.dupFlag) res.flags.push('ANOMALI DUPLIKASI TANGGAL');
    if (rec.issues && rec.issues.length) {
      res.statusAbsensi = 'PERLU_VALIDASI';
      res.flags.push('PERLU_VALIDASI');
    }
    if (isHoliday) res.flags.push('ABSEN HARI LIBUR');
    if (rec.shiftFlag) res.flags.push(rec.shiftFlag);
    if (rec.crossDay) res.flags.push('PULANG HARI BERIKUTNYA');
    if (isWali && isOffSchedule) {
      res.statusAbsensi = 'ABSEN_DI_LUAR_JADWAL';
      res.flags.push('ABSEN_DI_LUAR_JADWAL');
    }

    // For cross-day punches, compare the actual elapsed clock times. A
    // next-day checkout such as 04:00 is 28 hours on the attendance timeline,
    // not 04:00 on the same day as the 07:30 check-in.
    const actualPulang = rec.crossDay && t.pulang !== null ? t.pulang + 1440 : t.pulang;
    const dur = rec.crossDay && t.masuk !== null && actualPulang !== null
      ? Math.max(0, actualPulang - t.masuk)
      : U.durationMin(t.masuk, t.pulang);
    res.durasiMin = dur;
    res.durasi = dur === null ? '' : U.fmtDur(dur);

    // 1) Cuti/sakit periode -> 0%
    const lp = C.inLeavePeriod(rec.nip, rec.tanggal.key, ctx);
    if (lp) {
      res.statusCuti = lp.jenis;
      res.flags.push('PERIODE CUTI/SAKIT');
      res.totalHarian = 0;
      res.excused = true;
      return res;
    }

    // 2) Jenis absensi bebas potongan
    if (rec.jenis && ctx.jenisNo.has(String(rec.jenis).trim().toLowerCase())) {
      res.statusCuti = rec.jenis;
      res.flags.push('JENIS BEBAS POTONGAN');
      res.totalHarian = 0;
      res.excused = true;
      return res;
    }

    // OFF days are not subject to attendance penalties, including missing punches.
    if (isOffSchedule) {
      res.statusLengkap = t.masuk == null || t.pulang == null ? 'Tidak Lengkap' : 'Lengkap';
      res.statusAbsensi = 'ABSEN_DI_LUAR_JADWAL';
      res.flags.push('HARI LIBUR - TANPA POTONGAN');
      return res;
    }

    // 3) Absensi tidak lengkap -> 3% (tanpa penalti lain)
    if (t.masuk === null || t.pulang === null) {
      res.statusLengkap = 'Tidak Lengkap';
      res.statusAbsensi = t.masuk === null ? 'LUPA_ABSEN_MASUK' : 'LUPA_ABSEN_PULANG';
      if (metode === 'durasi' && jabatan.toLowerCase() === 'wali asuh') {
        res.statusAbsensi = 'PERLU_VALIDASI';
        res.flags.push('REKAMAN SHIFT TIDAK LENGKAP - CEK REKAP HARI KERJA');
        return res;
      }
      res.potTidakLengkap = cfg.potonganTidakLengkap;
      res.totalHarian = res.potTidakLengkap;
      res.flags.push('ABSENSI TIDAK LENGKAP');
      return res;
    }
    res.statusLengkap = 'Lengkap';
    if (isWali) res.statusAbsensi = rec.crossDay ? 'LINTAS_HARI' : 'NORMAL';

    // Complete next-day attendance is accepted without a daily deduction.
    // Check completeness first: a next-day date alone cannot excuse a missing punch.
    if (rec.crossDay) {
      res.statusAbsensi = 'LINTAS_HARI';
      if (metode === 'reguler') { res._lateMin = 0; res._earlyMin = 0; }
      return res;
    }

    if (metode === 'durasi') {
      // METODE DURASI AKTUAL (shift hanya flag)
      res.potDurasi = isOffSchedule ? 0 : C.durasiTierPct(dur / 60, cfg);
    } else {
      // METODE REGULER — hanya hari kerja (Sen-Jum non-libur)
      if (!isHoliday) {
        const dow = U.dow(rec.tanggal);
        const isJumat = dow === 5;
        const jd = isJumat ? cfg.jamDasar.jumat : cfg.jamDasar.senKam;
        const masukStd = U.parseTime(jd.masuk);
        const pulangStd = U.parseTime(jd.pulang);

        let lateMin = Math.max(0, t.masuk - masukStd);
        let earlyMin = rec.crossDay
          ? Math.max(0, pulangStd - actualPulang)
          : Math.max(0, pulangStd - t.pulang);

        // FLEXI TIME
        const fx = cfg.flexi;
        if (fx.enabled) {
          const fStart = U.parseTime(fx.masukStart), fEnd = U.parseTime(fx.masukEnd);
          if (t.masuk >= fStart && t.masuk <= fEnd) {
            const komp = isJumat ? U.parseTime(fx.kompensasiJumat) : U.parseTime(fx.kompensasiSenKam);
            if (t.pulang > komp) {
              lateMin = 0; earlyMin = 0;
              res.flags.push('FLEXI TIME (kompensasi)');
            }
          }
        }
        if (lateMin > 0) res.potKeterlambatan = tierPct(cfg.keterlambatanTiers, lateMin);
        if (earlyMin > 0) res.potPulangCepat = tierPct(cfg.pulangCepatTiers, earlyMin);
        res._lateMin = lateMin; res._earlyMin = earlyMin;
      } else {
        res.flags.push('HARI LIBUR - TANPA POTONGAN REGULER');
      }
    }

    res.totalHarian = round2(res.potKeterlambatan + res.potPulangCepat + res.potDurasi + res.potTidakLengkap);
    return res;
  };

  function round2(n) { return Math.round(n * 100) / 100; }
  C.round2 = round2;
})();

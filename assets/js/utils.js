/* EVKIN ABSENSI v3.1 - utils.js
   Fungsi bantu: parsing tanggal/jam, format, zona waktu. */
(function () {
  const EVKIN = window.EVKIN || (window.EVKIN = {});
  const U = EVKIN.util = {};

  const MONTHS_ID = ['Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember'];
  const DAYS_ID = ['Minggu','Senin','Selasa','Rabu','Kamis','Jumat','Sabtu'];
  U.MONTHS_ID = MONTHS_ID;
  U.DAYS_ID = DAYS_ID;

  U.pad = (n) => String(n).padStart(2, '0');

  // Excel serial number -> {y,m,d,minutes}
  U.excelSerialToDate = function (serial) {
    const utcDays = Math.floor(serial - 25569);
    const utcValue = utcDays * 86400;
    const d = new Date(utcValue * 1000);
    return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() };
  };

  // Parse tanggal ke {y,m,d, key:'yyyy-mm-dd', str:'dd/mm/yyyy'} atau null
  U.parseDate = function (v) {
    if (v === null || v === undefined || v === '') return null;
    let y, m, d;
    if (v instanceof Date) {
      y = v.getFullYear(); m = v.getMonth() + 1; d = v.getDate();
    } else if (typeof v === 'number') {
      if (v < 60) return null;
      const r = U.excelSerialToDate(v); y = r.y; m = r.m; d = r.d;
    } else {
      const s = String(v).trim();
      let mt;
      if (/^\d+(?:\.\d+)?$/.test(s) && Number(s) >= 60) {
        const r = U.excelSerialToDate(Number(s)); y = r.y; m = r.m; d = r.d;
      } else if ((mt = s.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/))) {
        y = +mt[1]; m = +mt[2]; d = +mt[3];
      } else if ((mt = s.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/))) {
        d = +mt[1]; m = +mt[2]; y = +mt[3];
      } else if ((mt = s.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{2})$/))) {
        d = +mt[1]; m = +mt[2]; y = 2000 + (+mt[3]);
      } else {
        const dt = new Date(s);
        if (!isNaN(dt.getTime())) { y = dt.getFullYear(); m = dt.getMonth() + 1; d = dt.getDate(); }
        else return null;
      }
    }
    if (!y || !m || !d || m < 1 || m > 12 || d < 1 || d > 31) return null;
    return U.makeDate(y, m, d);
  };

  U.makeDate = function (y, m, d) {
    return {
      y, m, d,
      key: `${y}-${U.pad(m)}-${U.pad(d)}`,
      str: `${U.pad(d)}/${U.pad(m)}/${y}`,
      monthKey: `${y}-${U.pad(m)}`
    };
  };

  // day of week 0=Minggu ... 6=Sabtu
  U.dow = function (dObj) {
    const dt = new Date(Date.UTC(dObj.y, dObj.m - 1, dObj.d));
    return dt.getUTCDay();
  };
  U.dayName = function (dObj) { return DAYS_ID[U.dow(dObj)]; };
  U.isWeekend = function (dObj) { const w = U.dow(dObj); return w === 0 || w === 6; };

  U.addDays = function (dObj, n) {
    const dt = new Date(Date.UTC(dObj.y, dObj.m - 1, dObj.d + n));
    return U.makeDate(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());
  };

  function formatCanonicalSeconds(totalSeconds) {
    const normalized = ((totalSeconds % 86400) + 86400) % 86400;
    const hours = Math.floor(normalized / 3600);
    const minutes = Math.floor((normalized % 3600) / 60);
    const seconds = normalized % 60;
    return `${U.pad(hours)}:${U.pad(minutes)}:${U.pad(seconds)}`;
  }

  U.normalizeExcelTime = function normalizeExcelTime(value) {
    if (value === null || value === undefined || value === '') return '';

    if (typeof value === 'string') {
      const s = String(value).trim();
      if (s === '') return '';
      const dec = s.replace(/\s+/g, '').replace(',', '.');
      if (/^-?\d+(?:\.\d+)?$/.test(dec)) {
        return U.normalizeExcelTime(Number(dec));
      }
      const mt = s.match(/^(\d{1,2})[:.](\d{2})(?::(\d{2}))?$/);
      if (mt) {
        const h = Number(mt[1]);
        const mi = Number(mt[2]);
        const sec = mt[3] === undefined ? 0 : Number(mt[3]);
        if (h >= 0 && h <= 23 && mi >= 0 && mi <= 59 && sec >= 0 && sec <= 59) {
          return `${U.pad(h)}:${U.pad(mi)}:${U.pad(sec)}`;
        }
      }
      return '';
    }

    if (typeof value === 'number' && Number.isFinite(value)) {
      if (value < 0) return '';
      if (value >= 1) {
        const frac = value - Math.floor(value);
        if (frac === 0) return '';
        const seconds = Math.round(frac * 86400) % 86400;
        return formatCanonicalSeconds(seconds);
      }
      const seconds = Math.round(value * 86400) % 86400;
      return formatCanonicalSeconds(seconds);
    }

    return '';
  };

  // Parse jam -> menit sejak tengah malam, atau null
  U.parseTime = function (v) {
    const canonical = U.normalizeExcelTime(v);
    if (canonical === '') return null;
    const parts = canonical.split(':').map(Number);
    if (parts.length !== 3 || parts.some(p => Number.isNaN(p))) return null;
    const [h, mi, sec] = parts;
    if (h > 23 || mi > 59 || sec > 59) return null;
    return h * 60 + mi + (sec >= 30 ? 1 : 0);
  };

  U.fmtTime = function (mins) {
    if (mins === null || mins === undefined || mins === '') return '';
    let m = ((mins % 1440) + 1440) % 1440;
    return `${U.pad(Math.floor(m / 60))}:${U.pad(m % 60)}`;
  };

  // durasi menit (dukung lintas tengah malam)
  U.durationMin = function (masuk, pulang) {
    if (masuk === null || pulang === null) return null;
    let d = pulang - masuk;
    if (d <= 0) d += 1440;
    return d;
  };

  U.fmtDur = function (mins) {
    if (mins === null || mins === undefined) return '';
    const h = Math.floor(mins / 60), m = mins % 60;
    return `${h}j ${U.pad(m)}m`;
  };
  U.hours = (mins) => (mins === null ? null : mins / 60);

  U.fmtRupiah = function (n) {
    n = Math.round(+n || 0);
    return 'Rp' + n.toLocaleString('id-ID');
  };
  U.fmtPct = function (n) { return (Math.round((+n || 0) * 100) / 100) + '%'; };
  U.round2 = function (n) { return Math.round((+n || 0) * 100) / 100; };

  U.monthLabel = function (monthKey) {
    if (!monthKey) return '';
    const [y, m] = monthKey.split('-');
    return `${MONTHS_ID[(+m) - 1]} ${y}`;
  };

  U.esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  };

  U.uid = () => 'id' + Math.random().toString(36).slice(2, 9);
})();

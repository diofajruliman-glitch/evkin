const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const XLSX = require('../../assets/vendor/xlsx.full.min.js');

class FakeFileReader {
  readAsArrayBuffer(file) {
    const bytes = file.bytes;
    const value = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    queueMicrotask(() => this.onload({target:{result:value}}));
  }
}

async function main() {
  const materialRows = rows => JSON.parse(JSON.stringify(rows)).map(({_id, ...row}) => row);
  const E = {
    state: { month: '2026-08', data: [], daily: [] },
    util: {},
    master: {
      detectZone: () => 'WIB',
      zoneOffsetMin: { WIB: 0 },
      zoneOffsetMinAdd: { WIB: 0 }
    },
    invalidateFrom() {},
    auditMod: { logDataImport() {} },
    syncImportedWorkflow: async () => {},
    syncBackendWorkflow: async () => {},
    exportBackend: { enabled: false }
  };

  const sandbox = {
    window: { EVKIN: E }, console, Date, Math, setTimeout, clearTimeout,
    queueMicrotask, FileReader:FakeFileReader, XLSX
  };
  vm.runInNewContext(fs.readFileSync('assets/js/utils.js', 'utf8'), sandbox);
  vm.runInNewContext(fs.readFileSync('assets/js/import.js', 'utf8'), sandbox);
  vm.runInNewContext(fs.readFileSync('assets/js/import-wali.js', 'utf8'), sandbox);

  const U = E.util;
  const mod = E.importMod;

  assert.equal(U.normalizeExcelTime(0.288263888888889), '06:55:06');
  assert.equal(U.normalizeExcelTime(0.827013888888889), '19:50:54');
  assert.equal(U.normalizeExcelTime(0), '00:00:00');
  assert.equal(U.normalizeExcelTime(0.5), '12:00:00');
  assert.equal(U.normalizeExcelTime(0.99999), '23:59:59');
  assert.equal(U.normalizeExcelTime('07:30'), '07:30:00');
  assert.equal(U.normalizeExcelTime('07:30:00'), '07:30:00');
  assert.equal(U.normalizeExcelTime(''), '');
  assert.equal(U.normalizeExcelTime(null), '');
  assert.equal(U.normalizeExcelTime('29'), '');
  assert.equal(U.parseTime(0.288263888888889), 415);
  assert.equal(U.parseTime(0.827013888888889), 1191);
  assert.equal(U.parseTime(0), 0);
  assert.equal(U.parseTime('07:30'), 450);
  assert.deepEqual(
    JSON.parse(JSON.stringify(U.parseDate('46234'))),
    JSON.parse(JSON.stringify(U.parseDate(46234))),
    'Excel serial dates persisted as canonical text must restore identically'
  );

  const headers = ['NIP', 'Nama', 'Tanggal', 'Jam Datang', 'Jam Pulang'];
  const colMap = mod.detectColumns(headers);
  const validRow = ['202401230000000001', 'Pegawai 001', '03/08/2026', 0.288263888888889, 0.827013888888889];
  const validResult = mod.processSingleRow(validRow, headers, colMap, { partial: true });
  assert.equal(validResult.dataObj.masukRaw, '06:55:06');
  assert.equal(validResult.dataObj.pulangRaw, '19:50:54');
  assert.equal(validResult.dataObj.masukMin, 415);
  assert.equal(validResult.dataObj.pulangMin, 1191);

  const persistedTextRow = ['202401230000000005', 'Pegawai 005', '46234', '0.288263888888889', '0.827013888888889'];
  const persistedTextResult = mod.processSingleRow(persistedTextRow, headers, colMap, { partial: true });
  assert.equal(persistedTextResult.dataObj.tanggal.key, U.parseDate(46234).key);
  assert.equal(persistedTextResult.dataObj.masukMin, 415);
  assert.equal(persistedTextResult.dataObj.pulangMin, 1191);

  const invalidRow = ['202401230000000002', 'Pegawai 002', '03/08/2026', '29', ''];
  const invalidResult = mod.processSingleRow(invalidRow, headers, colMap, { partial: true });
  assert.equal(invalidResult.dataObj.masukRaw, '');
  assert.match(invalidResult.dataObj.issues.join('; '), /Jam masuk tidak valid/);

  const ambil = ['Jam Datang kosong tetapi Jam Pulang ada', 'Jam Pulang kosong tetapi Jam Datang ada'];
  const rowMasukKosong = ['202401230000000003', 'Pegawai 003', '03/08/2026', '', '19:50:54'];
  const rowPulangKosong = ['202401230000000004', 'Pegawai 004', '03/08/2026', '06:55:06', ''];
  const masukKosong = mod.processSingleRow(rowMasukKosong, headers, colMap);
  const pulangKosong = mod.processSingleRow(rowPulangKosong, headers, colMap);
  assert.ok(masukKosong.dataObj.issues.some(i => i.includes('Jam masuk')));
  assert.ok(pulangKosong.dataObj.issues.some(i => i.includes('Jam pulang')));

  const fixturePath = 'backend/tests/fixtures/absensi_excel_time_fixture_600.csv';
  const fixtureRows = fs.readFileSync(fixturePath, 'utf8').trim().split(/\r?\n/).map(line => line.split(','));
  const fixtureStats = await mod.process(fixtureRows);
  assert.equal(fixtureRows.length - 1, 600);
  assert.equal(fixtureStats.totalRecord, 600);
  assert.equal(fixtureStats.totalRaw, 600);

  const representativeRows = fixtureRows.slice(0, 7);
  await mod.process(representativeRows);
  const directResult = materialRows(E.state.data);
  const worksheet = XLSX.utils.aoa_to_sheet(representativeRows);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'Absensi');
  const excelBytes = XLSX.write(workbook, {bookType:'xlsx', type:'buffer'});
  const fileStats = await mod.processFile({name:'absensi-representative.xlsx', bytes:excelBytes});
  const fileResult = materialRows(E.state.data);
  assert.equal(fileStats.totalRecord, representativeRows.length - 1);
  assert.deepEqual(fileResult, directResult, 'Legacy processFile XLSX result must equal the frozen legacy row engine');

  const waliFixture = JSON.parse(fs.readFileSync(
    'backend/tests/fixtures/absence_wali_golden_cases_2026_09.json', 'utf8'
  ));
  const waliAoa = [waliFixture.headers, ...waliFixture.rows];
  await E.waliImportMod.processAoa(waliAoa);
  const directWali = materialRows(E.state.data);
  const waliSheet = XLSX.utils.aoa_to_sheet(waliAoa);
  const waliWorkbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(waliWorkbook, waliSheet, 'Absensi Wali');
  const waliBytes = XLSX.write(waliWorkbook, {bookType:'xlsx', type:'buffer'});
  const waliStats = await E.waliImportMod.processFile({name:'absensi-wali-representative.xlsx', bytes:waliBytes});
  const fileWali = materialRows(E.state.data);
  assert.deepEqual(fileWali, directWali, 'Legacy Wali XLSX result must equal the frozen pairing engine');
  assert.equal(E.state.importMode, 'wali');
  assert.equal(waliStats.crossDay, fileWali.filter(row => row.crossDay).length);
  assert.ok(fileWali.some(row => row.nip === 'N2' && row.tanggal.key === '2026-08-04' &&
    row.masukMin === 1200 && row.pulangMin === 360 && row.crossDay));
  assert.ok(fileWali.some(row => row.nip === 'N9' && row.tanggal.key === '2026-12-31' &&
    row.tanggalPulangObj.key === '2027-01-01' && row.crossDay));

  console.log('Excel normalization, full XLSX processFile, and frozen Wali/cross-day pairing: PASS');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

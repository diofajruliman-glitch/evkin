const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');

const root = path.resolve(__dirname, '..', '..');
const fixturePath = path.join(__dirname, 'fixtures', 'absence_js_golden_cases_2026_09.json');
const outputPath = path.join(__dirname, 'fixtures', 'absence_cross_day_complete_output_2026_10.json');
const runnerPath = path.join(__dirname, 'legacy_calculation_runner.cjs');

function run() {
  return JSON.parse(execFileSync(process.execPath, [runnerPath, fixturePath], {cwd: root, encoding: 'utf8'}));
}

const expected = JSON.parse(fs.readFileSync(outputPath, 'utf8'));
const first = run();
const second = run();
assert.deepEqual(first, expected, 'legacy engine output changed from the frozen golden snapshot');
assert.deepEqual(second, first, 'legacy engine is not repeatable for identical input');
assert.equal(first.contract_version, 'ABSENCE_JS_BASELINE_2026_09');
assert.equal(first.cases.length, 76);
assert.equal(first.normalization_cases.length, 15);

const byId = new Map(first.cases.map(row => [row.id, row]));
const daily = id => byId.get(id).daily[0];
assert.equal(daily('ABS-IN-004')._lateMin, 30);
assert.equal(daily('ABS-IN-005')._lateMin, 31);
assert.equal(daily('ABS-IN-004').potKeterlambatan, 0.5);
assert.equal(daily('ABS-IN-005').potKeterlambatan, 1);
assert.equal(daily('ABS-OUT-004')._earlyMin, 30);
assert.equal(daily('ABS-OUT-005')._earlyMin, 31);
assert.ok(daily('ABS-FLEX-002').flags.includes('FLEXI TIME (kompensasi)'));
assert.equal(daily('ABS-FLEX-005').potKeterlambatan, 0.5);
assert.equal(daily('ABS-DUR-002').durasiMin, 450);
assert.equal(daily('ABS-DUR-003').durasiMin, 449);
assert.equal(daily('ABS-DUR-002').potDurasi, 0);
assert.equal(daily('ABS-DUR-003').potDurasi, 0, 'Complete overnight attendance is exempt');
assert.equal(daily('ABS-SHIFT-004').durasiMin, 1440);
assert.equal(daily('ABS-WALI-004').jabatan, 'Guru SR', 'Workbook job must prevail over master job');
assert.equal(daily('ABS-WALI-004').metode, 'reguler');
assert.equal(daily('ABS-MISS-003').statusAbsensi, 'LUPA_ABSEN_MASUK');
assert.equal(byId.get('ABS-DQ-003').rekap, null);
assert.match(byId.get('ABS-DQ-003').rekap_error, /duplikat/i);
assert.equal(daily('ABS-EXC-002').excused, true);
assert.equal(byId.get('ABS-FINAL-001').final_absensi.nominal, 150001);
assert.equal(byId.get('ABS-FINAL-002').final_absensi.nominal, null);
assert.equal(byId.get('ABS-FINAL-003').final_absensi.potPct, 100);

const normalized = new Map(first.normalization_cases.map(row => [row.id, row]));
assert.equal(normalized.get('ABS-NORM-002').normalized.masukMin, 450);
assert.equal(normalized.get('ABS-NORM-003').normalized.masukMin, 451);
assert.equal(normalized.get('ABS-NORM-009').invalid, true);
assert.equal(normalized.get('ABS-NORM-010').invalid, true);
assert.equal(normalized.get('ABS-NORM-011').normalized.crossDay, true);
assert.equal(normalized.get('ABS-NORM-014').normalized.zona, 'WITA');
assert.equal(normalized.get('ABS-NORM-015').normalized.zona, 'WIT');

console.log('Absence JS golden baseline: 76 calculation + 15 normalization cases, repeatability PASS');

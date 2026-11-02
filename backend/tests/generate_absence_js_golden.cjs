const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..', '..');
const fixture = path.join(__dirname, 'fixtures', 'absence_js_golden_cases_2026_09.json');
const output = path.join(__dirname, 'fixtures', 'absence_cross_day_complete_output_2026_10.json');
const historical = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'absence_js_golden_output_2026_09.json'), 'utf8'));
const runner = path.join(__dirname, 'legacy_calculation_runner.cjs');
const sourceCases = new Map(JSON.parse(fs.readFileSync(fixture, 'utf8')).cases.map(row => [row.id, row]));
const raw = execFileSync(process.execPath, [runner, fixture], {cwd: root, encoding: 'utf8'});
const current = JSON.parse(raw);
assert.deepEqual(current.normalization_cases, historical.normalization_cases);
for (const row of current.cases) {
  const source = sourceCases.get(row.id);
  for (const daily of row.daily) assert.equal(daily.jabatan, String(source.job || '').trim() || 'Belum Ditentukan', row.id);
  const previous = historical.cases.find(item => item.id === row.id);
  if (!row.normalized_input.cross_day) assert.deepEqual(row, previous, `Unrelated calculation changed: ${row.id}`);
  else if (row.normalized_input.check_in_effective != null && row.normalized_input.check_out_effective != null) {
    for (const daily of row.daily) assert.equal(daily.totalHarian, 0, row.id);
  }
}
fs.writeFileSync(output, JSON.stringify(current, null, 2) + '\n', 'utf8');
console.log(`Golden output written: ${output}`);

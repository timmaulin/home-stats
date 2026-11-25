const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('docs/usage.html', 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].at(-1)[1];

function days(start, end) {
  const rows = [];
  for (let d = new Date(start + 'T00:00:00Z'); d.toISOString().slice(0, 10) <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    const date = d.toISOString().slice(0, 10);
    rows.push({date, electricity_kwh: date.startsWith('2026') ? 4 : 2,
      gas_kwh: 1, temperature_c: date.startsWith('2026') ? 1 : 0});
  }
  return rows;
}

async function loadPage(rows) {
  const elements = {};
  const defaults = {range: '60', mode: 'daily', comparison: 'previous', fuel: 'gas'};
  const element = id => elements[id] ||= {
    id, value: defaults[id], textContent: '', hidden: false,
    addEventListener(event, callback) { this[event] = callback; }
  };
  const charts = {};
  class Chart {
    static defaults = {font: {}};
    constructor(canvas, config) { charts[canvas.id] = config; }
    destroy() {}
  }
  const context = {
    document: {getElementById: element, documentElement: {}, body: {}},
    getComputedStyle: () => ({getPropertyValue: () => '#123456', fontFamily: 'sans-serif'}),
    Chart, window: {matchMedia: () => ({addEventListener() {}})},
    fetch: async () => ({ok: true, json: async () => ({updated: 'now', days: rows})}),
  };
  vm.runInNewContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(element('status').textContent, '');
  return {element, charts};
}

test('14 months uses calendar dates and retains year labels', async () => {
  const page = await loadPage(days('2023-01-01', '2026-03-31'));
  page.element('range').value = '14m';
  page.element('range').change();
  const labels = page.charts['usage-chart'].data.labels;
  assert.equal(labels[0], '1 Feb 2025');
  assert.equal(labels.at(-1), '31 Mar 2026');
  assert.equal(labels.length, 424);
});

test('year-on-year compares matching dates and permits zero temperature', async () => {
  const page = await loadPage(days('2024-01-01', '2026-03-31'));
  page.element('range').value = '30';
  page.element('comparison').value = 'year';
  page.element('comparison').change();
  assert.match(page.element('electricity-delta').textContent, /100% vs same dates last year/);
  assert.match(page.element('temperature-delta').textContent, /1 °C vs same dates last year/);
  assert.equal(page.element('comparison-note').hidden, false);
  page.element('comparison').value = 'previous';
  page.element('comparison').change();
  assert.match(page.element('electricity-delta').textContent, /0% vs previous period/);
});

test('year-on-year does not compare incomplete or unmatched readings', async () => {
  const rows = days('2023-01-01', '2024-03-03');
  const page = await loadPage(rows);
  page.element('range').value = '14';
  page.element('comparison').value = 'year';
  page.element('comparison').change();
  assert.equal(page.element('electricity-delta').textContent, '');

  const otherRows = days('2024-01-01', '2026-03-31');
  otherRows.find(r => r.date === '2025-03-02').electricity_kwh = null;
  const otherPage = await loadPage(otherRows);
  otherPage.element('range').value = '30';
  otherPage.element('comparison').value = 'year';
  otherPage.element('comparison').change();
  assert.equal(otherPage.element('electricity-delta').textContent, '');
  assert.equal(otherPage.element('energy-delta').textContent, '');
  assert.match(otherPage.element('gas-delta').textContent, /0% vs same dates last year/);
});

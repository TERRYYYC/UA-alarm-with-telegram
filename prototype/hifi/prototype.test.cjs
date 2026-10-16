const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'situation-monitor.html'), 'utf8');
const code = html.match(/<script id="monitor-model">([\s\S]*?)<\/script>/)[1];
const sandbox = {}; vm.createContext(sandbox); vm.runInContext(code, sandbox);
const model = sandbox.MonitorModel;
test('preserves all 11 original demonstrative events', () => {
  assert.equal(model.events.length, 11);
  assert.equal(model.events.find(e=>e.id==='kherson').reports, 3);
  assert.equal(model.events.find(e=>e.id==='odesa').minutes, 95);
});
test('one time query drives map and list without future history events', () => {
  const state = model.createState();
  assert.equal(model.query(state).length, 11);
  state.windowMinutes = 60;
  assert.equal(model.query(state).length, 8);
  state.cursorMinutes = 30;
  const result = model.query(state);
  assert.equal(result.length, 6);
  assert.ok(result.every(e => e.minutes >= 30 && e.minutes <= 90));
});
test('type and text filters compose, including honest empty state', () => {
  const state = model.createState();
  state.types = ['shelling'];
  assert.equal(model.query(state).length, 4);
  state.search = '赫尔松';
  assert.equal(model.query(state)[0].id, 'kherson');
  state.search = 'qa-unseen-7941';
  assert.equal(model.query(state).length, 0);
});
test('freshness and statistics use the selected historical cursor', () => {
  const state = model.createState();
  assert.equal(model.stats(state).fresh, 2);
  assert.equal(model.stats(state).hour, 8);
  state.cursorMinutes = 30;
  assert.equal(model.stats(state).fresh, 2);
});
test('hiding events is view-only and cannot disable sources or delete data', () => {
  const state = model.createState();
  const next = model.setLayer(state, 'events', false);
  assert.equal(next.layers.events, false);
  assert.equal(state.layers.events, true);
  assert.equal(JSON.stringify(next.sources), JSON.stringify(state.sources));
  assert.equal(model.query(next).length, 11);
});
test('prototype contains no external runtime dependency or real Telegram link', () => {
  assert.doesNotMatch(html, /<script[^>]+src=|<link[^>]+href=["']https:|https:\/\/t\.me\//i);
});
test('export CSS makes hidden-layer selectors work without the page wrapper', () => {
  const style=html.match(/<style>([\s\S]*?)<\/style>/)[1];
  const transformation=html.match(/style\.textContent=document\.querySelector\('style'\)\.textContent([^;]+);/)[1];
  const scope={style};vm.createContext(scope);
  const exported=vm.runInContext('style'+transformation,scope);
  for(const name of ['events','cities','labels','borders']){
    assert.ok(exported.includes('svg.hide-'+name),'missing standalone selector: '+name);
  }
  assert.doesNotMatch(exported,/\.map-surface svg/);
});

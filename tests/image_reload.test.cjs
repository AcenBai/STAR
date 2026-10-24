// Run the actual browser script with deterministic image events and no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function page() {
  const requests = [];
  const apiRequests = [];
  class Element {
    constructor(tag = 'div') {
      this.tag = tag; this.alt = 'Tumor overlay'; this.disabled = false;
      this.textContent = ''; this.style = {}; this.listeners = {};
      this.classList = { toggle() {}, add() {}, remove() {} };
      this.children = [];
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    emit(type) { for (const fn of this.listeners[type] || []) fn(); }
    replaceWith(node) { this.replacedBy = node; }
    removeAttribute(name) { if (name === 'src') this._src = ''; }
    set src(value) { this._src = value; requests.push(value); }
    get src() { return this._src || ''; }
    querySelectorAll() { return this.children; }
    querySelector() { return this.children.find(node => node.checked) || null; }
    appendChild(node) { this.children.push(node); }
  }
  const elements = new Map();
  const context = vm.createContext({
    URL, Date, console,
    window: { location: { href: 'http://star.test/' } },
    localStorage: { getItem() { return ''; }, setItem() {} },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, new Element());
        return elements.get(selector);
      },
      createElement(tag) { return new Element(tag); },
    },
    fetch(url) { apiRequests.push(url); throw new Error('Unexpected task API request'); },
  });
  const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, '\n'), context);
  const api = vm.runInContext('({ state, el, loadTaskImage, reloadCurrentImage, renderTask })', context);
  const task = { image_id: 'image_a', image_url: '/image/image_a', stage: 'q2',
    answers: { q1_region: 'Chest', q_lymph_node: 'No' }, completed: 12, total: 500,
    task_revision: 8, can_go_back: true, done: false };
  api.state.task = task; api.state.imageId = task.image_id;
  const selected = new Element('input'); selected.value = 'Lung'; selected.checked = true;
  api.el.choiceList.children = [selected];
  return { ...api, requests, apiRequests, selected, task };
}

for (const stage of ['q_lymph_node', 'q2', 'q4']) {
  test(`reload keeps ${stage} selections, progress, stage and submit state`, () => {
    const p = page(); p.task.stage = stage;
    const before = JSON.stringify(p.task);
    const choices = p.el.choiceList;
    p.reloadCurrentImage();
    assert.equal(JSON.stringify(p.state.task), before);
    assert.equal(p.el.choiceList, choices);
    assert.equal(p.selected.checked, true);
    assert.equal(p.el.submitBtn.disabled, false);
    assert.equal(p.el.imageStatus.textContent, 'Reloading…');
    assert.equal(new URL(p.requests[0]).pathname, '/image/image_a');
    assert.ok(new URL(p.requests[0]).searchParams.get('reload'));
    assert.equal(p.apiRequests.length, 0);
  });
}

test('unlimited clicks create fresh image requests and no task requests', () => {
  const p = page();
  for (let i = 0; i < 25; i++) p.reloadCurrentImage();
  assert.equal(p.requests.length, 25);
  assert.equal(new Set(p.requests).size, 25);
  assert.equal(p.el.reloadImageBtn.disabled, false);
  assert.equal(p.apiRequests.length, 0);
});

test('failed request allows manual retry without automatic requests or losing selections', () => {
  const p = page(); p.reloadCurrentImage();
  p.el.taskImage.emit('error');
  assert.equal(p.el.imageStatus.textContent, 'Load failed. Retry.');
  assert.equal(p.requests.length, 1);
  p.reloadCurrentImage(); p.el.taskImage.emit('load');
  assert.equal(p.el.imageStatus.textContent, 'Loaded');
  assert.equal(p.selected.checked, true);
  assert.equal(p.el.submitBtn.disabled, false);
});

test('late events from an older retry cannot replace the latest status', () => {
  const p = page(); p.reloadCurrentImage();
  const oldImage = p.el.taskImage;
  p.reloadCurrentImage(); p.el.taskImage.emit('load');
  oldImage.emit('error'); oldImage.emit('load');
  assert.equal(p.el.imageStatus.textContent, 'Loaded');
  assert.notEqual(p.el.taskImage, oldImage);
});

test('late image events cannot overwrite the next image', () => {
  const p = page(); p.reloadCurrentImage();
  const oldImage = p.el.taskImage;
  p.state.task = { ...p.task, image_id: 'image_b', image_url: '/image/image_b' };
  p.state.imageId = 'image_b'; p.loadTaskImage(p.state.task);
  const newImage = p.el.taskImage;
  oldImage.emit('load'); oldImage.emit('error');
  assert.equal(p.el.taskImage, newImage);
  assert.equal(p.el.imageStatus.textContent, 'Loading…');
  assert.equal(new URL(newImage.src).pathname, '/image/image_b');
  assert.equal(new URL(newImage.src).search, '');
});

test('finishing the study invalidates old requests and disables reload', () => {
  const p = page(); p.reloadCurrentImage();
  const oldImage = p.el.taskImage;
  p.renderTask({ done: true, total: 500, completed: 500, message: 'Complete', show_accuracy: false });
  oldImage.emit('load'); oldImage.emit('error');
  p.reloadCurrentImage();
  assert.equal(p.requests.length, 1);
  assert.equal(p.el.imageStatus.textContent, '');
  assert.equal(p.el.reloadImageBtn.disabled, true);
});

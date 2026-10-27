const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page({ userId = '', beacon = true } = {}) {
  const requests = [], apiRequests = [], metrics = [], microtasks = [], resources = [];
  const timers = new Map(); let clock = 1000, timerId = 0;
  const flush = () => { while (microtasks.length) microtasks.shift()(); };
  class Element {
    constructor(tag = 'div') {
      this.tag = tag; this.alt = 'Tumor overlay'; this.disabled = false;
      this.textContent = ''; this.style = {}; this.listeners = {}; this.children = [];
      this.classList = { toggle() {}, add() {}, remove() {} };
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    emit(type) { for (const fn of this.listeners[type] || []) fn(); flush(); }
    replaceWith(node) { this.replacedBy = node; }
    removeAttribute(name) { if (name === 'src') this._src = ''; }
    setAttribute(name, value) { this[name] = value; }
    set src(value) { this._src = value; requests.push(value); }
    get src() { return this._src || ''; }
    querySelectorAll() { return this.children; }
    querySelector() { return this.children.find(node => node.checked) || null; }
    appendChild(node) { this.children.push(node); }
  }
  const elements = new Map();
  const context = vm.createContext({
    URL, Date, console,
    Blob: class { constructor(parts) { this.body = parts.join(''); } },
    window: { location: { href: 'http://star.test/' } },
    performance: { now: () => clock, getEntriesByName: url => resources.filter(r => r.name === url) },
    setTimeout(fn, delay) { timers.set(++timerId, { fn, at: clock + delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    queueMicrotask(fn) { microtasks.push(fn); },
    navigator: { sendBeacon(url, blob) {
      if (beacon) metrics.push(JSON.parse(blob.body));
      return beacon;
    } },
    localStorage: { getItem() { return ''; }, setItem() {} },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, new Element());
        return elements.get(selector);
      },
      createElement(tag) { return new Element(tag); },
    },
    fetch(url, options) {
      apiRequests.push(url);
      if (url === '/api/image-metrics') {
        metrics.push(JSON.parse(options.body));
        return Promise.resolve({ status: 204 });
      }
      throw new Error('Unexpected task API request');
    },
  });
  for (const file of ['image_delivery.js', 'app.js']) {
    const source = fs.readFileSync(path.join(__dirname, '../web/static/', file), 'utf8');
    vm.runInContext(source.replace(/\ninit\(\);\s*$/, '\n'), context);
  }
  const api = vm.runInContext('({ state, el, imageDelivery, loadTaskImage, reloadCurrentImage, renderTask })', context);
  const task = { image_id: 'image_a', image_url: '/image/image_a', stage: 'q2',
    answers: { q1_region: 'Chest', q_lymph_node: 'No' }, completed: 12, total: 500,
    task_revision: 8, can_go_back: true, done: false, show_accuracy: false,
    regions: ['Chest', 'Abdomen', 'Pelvis', 'Others'] };
  api.state.task = task; api.state.imageId = task.image_id; api.state.userId = userId;
  const selected = new Element('input'); selected.value = 'Lung'; selected.checked = true;
  api.el.choiceList.children = [selected];
  function advance(ms) {
    const target = clock + ms;
    while (true) {
      const entry = [...timers.entries()].filter(([, t]) => t.at <= target)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry) break;
      clock = entry[1].at; timers.delete(entry[0]); entry[1].fn(); flush();
    }
    clock = target; flush();
  }
  return { ...api, requests, apiRequests, metrics, resources, advance, flush,
    context, selected, task, Element };
}

module.exports = { page };

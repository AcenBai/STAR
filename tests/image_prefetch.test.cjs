const assert = require('node:assert/strict');
const { test } = require('node:test');
const { page } = require('./image_test_page.cjs');

function start(options) {
  const p = page({ userId: 'synthetic_reader', ...options });
  p.task.next_image = { image_id: 'image_b', image_url: '/image/image_b' };
  p.loadTaskImage(p.task);
  return p;
}
function showNext(p) {
  p.state.task = { ...p.task, image_id: 'image_b', image_url: '/image/image_b',
    next_image: { image_id: 'image_c', image_url: '/image/image_c' } };
  p.state.imageId = 'image_b'; p.loadTaskImage(p.state.task);
}

test('waits for the displayed image, then prefetches only one image at low priority', () => {
  const p = start(); const before = JSON.stringify(p.task);
  p.advance(1000); assert.equal(p.requests.length, 1);
  p.el.taskImage.emit('load'); p.advance(249); assert.equal(p.requests.length, 1);
  p.advance(1); assert.equal(p.requests.length, 2);
  assert.equal(p.imageDelivery.prefetch.image.fetchPriority, 'low');
  p.imageDelivery.prefetch.image.emit('load'); p.advance(1000);
  assert.equal(p.requests.length, 2);
  assert.equal(JSON.stringify(p.state.task), before);
  assert.equal(p.selected.checked, true);
  assert.equal(p.apiRequests.length, 0);
});

for (const complete of [true, false]) {
  test(`adopts ${complete ? 'finished' : 'in-flight'} prefetch without a second download`, () => {
    const p = start(); p.el.taskImage.emit('load'); p.advance(250);
    const prefetched = p.imageDelivery.prefetch.image;
    if (complete) prefetched.emit('load');
    showNext(p);
    assert.equal(p.el.taskImage, prefetched);
    assert.equal(p.requests.length, 2);
    assert.equal(prefetched.fetchPriority, 'high');
    if (!complete) prefetched.emit('load');
    assert.equal(p.el.imageStatus.textContent, 'Loaded');
    p.flush();
    assert.ok(p.metrics.some(m => m.image_id === 'image_b' && m.kind === 'display'
      && m.outcome === 'loaded' && m.prefetch_used));
    p.advance(250);
    assert.equal(p.requests.length, 3);
    assert.equal(new URL(p.requests[2]).pathname, '/image/image_c');
  });
}

test('backtracking within one image does not initiate more image downloads', () => {
  const p = start(); p.el.taskImage.emit('load'); p.advance(250);
  for (const stage of ['q4', 'q2', 'q_lymph_node', 'q1']) {
    p.renderTask({ ...p.task, stage });
  }
  p.advance(1000);
  assert.equal(p.requests.length, 2);
  assert.equal(p.apiRequests.length, 0);
});

test('failed prefetch falls back to a normal current-image request without retries', () => {
  const p = start(); p.el.taskImage.emit('load'); p.advance(250);
  p.imageDelivery.prefetch.image.emit('error'); p.advance(20000);
  assert.equal(p.requests.length, 2);
  showNext(p);
  assert.equal(p.requests.length, 3);
  assert.equal(new URL(p.requests[2]).pathname, '/image/image_b');
  assert.equal(p.el.submitBtn.disabled, false);
});

test('manual reload cancels lookahead and keeps answers, revision and selected input', () => {
  const p = start(); p.el.taskImage.emit('load'); p.advance(250);
  const pending = p.imageDelivery.prefetch.image; const before = JSON.stringify(p.task);
  p.reloadCurrentImage(); p.advance(1000);
  pending.emit('load'); pending.emit('error');
  assert.equal(p.el.imageStatus.textContent, 'Reloading…');
  assert.equal(p.requests.length, 3);
  assert.equal(pending.src, '');
  assert.equal(p.selected.checked, true);
  assert.equal(JSON.stringify(p.state.task), before);
});

test('prefetch from a different account is cancelled even when the image ID matches', () => {
  const p = start(); p.el.taskImage.emit('load'); p.advance(250);
  const pending = p.imageDelivery.prefetch.image;
  p.state.userId = 'other_reader'; showNext(p);
  assert.notEqual(p.el.taskImage, pending);
  assert.equal(pending.src, '');
  assert.equal(p.requests.length, 3);
});

test('completion cancels current and pending work; late events cannot restart prefetch', () => {
  const p = start(); p.el.taskImage.emit('load'); p.advance(250);
  const old = p.el.taskImage, pending = p.imageDelivery.prefetch.image;
  p.renderTask({ done: true, completed: 500, total: 500, show_accuracy: false });
  old.emit('load'); pending.emit('load'); p.advance(20000);
  assert.equal(p.requests.length, 2);
  assert.equal(p.el.imageStatus.textContent, '');
  assert.equal(p.imageDelivery.prefetch, null);
  assert.equal(p.imageDelivery.current, null);
});

test('a slow image emits one diagnostic while the image and answer form remain active', () => {
  const p = start(); p.advance(15000);
  assert.equal(p.metrics.filter(m => m.outcome === 'slow').length, 1);
  p.advance(15000);
  assert.equal(p.metrics.filter(m => m.outcome === 'slow').length, 1);
  assert.equal(p.requests.length, 1);
  assert.equal(p.el.submitBtn.disabled, false);
  assert.equal(p.selected.checked, true);
  p.el.taskImage.emit('load');
  assert.equal(p.metrics.find(m => m.outcome === 'loaded').elapsed_ms, 30000);
});

test('resource timing diagnostics omit answers, raw URLs and task fields', () => {
  const p = start(); p.advance(1000);
  p.resources.push({ name: p.requests[0], entryType: 'resource', startTime: 1000,
    duration: 1000, requestStart: 1010, responseStart: 1210, responseEnd: 2000,
    transferSize: 20500, encodedBodySize: 20000, decodedBodySize: 20000, nextHopProtocol: 'http/1.1' });
  p.el.taskImage.emit('load');
  const metric = p.metrics[0];
  assert.equal(metric.elapsed_ms, 1000);
  assert.equal(metric.resource.ttfb_ms, 200);
  assert.equal(metric.resource.download_ms, 790);
  assert.equal(metric.resource.encoded_bytes, 20000);
  assert.equal(metric.user_id, 'synthetic_reader');
  for (const key of ['answers', 'stage', 'task_revision', 'url', 'gt_site']) assert.equal(metric[key], undefined);
});

test('beacon fallback posts diagnostics only and does not block image display', () => {
  const p = start({ beacon: false }); p.el.taskImage.emit('load');
  assert.equal(p.el.imageStatus.textContent, 'Loaded');
  assert.deepEqual(p.apiRequests, ['/api/image-metrics']);
  assert.equal(p.el.submitBtn.disabled, false);
});

test('diagnostic transport failure is ignored without losing selections', () => {
  const p = start(); p.context.navigator.sendBeacon = () => { throw new Error('offline'); };
  p.el.taskImage.emit('load');
  assert.equal(p.el.imageStatus.textContent, 'Loaded');
  assert.equal(p.selected.checked, true);
  assert.equal(p.el.submitBtn.disabled, false);
});

for (const image_url of ['http://elsewhere.test/image/image_b', '/api/current?user_id=someone', '/image/image_b?extra=1']) {
  test(`does not prefetch invalid next-image URL ${image_url}`, () => {
    const p = start(); p.task.next_image.image_url = image_url;
    p.el.taskImage.emit('load'); p.advance(1000);
    assert.equal(p.requests.length, 1);
  });
}

test('failed current image never initiates a prefetch or automatic reload', () => {
  const p = start(); p.el.taskImage.emit('error'); p.advance(30000);
  assert.equal(p.requests.length, 1);
  assert.equal(p.el.imageStatus.textContent, 'Load failed. Retry.');
  assert.equal(p.el.submitBtn.disabled, false);
});

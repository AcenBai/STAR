// Run the actual browser script with deterministic image events and no network.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { page } = require('./image_test_page.cjs');

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

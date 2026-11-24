"""Exercise shipped dashboard write handling with a lost POST response."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_lost_post_refreshes_without_repeating_and_shared_controls_restore():
    source = (Path(__file__).parent / 'static/app.js').read_text()
    functions = ''.join(re.search(r'\n  function %s\(.*?\n  \}\n' % name, source, re.S).group(0)
                        for name in ('handled', 'apiPost', 'saveControls', 'showInlineError', 'masterRow', 'addLoopControl'))
    script = functions + r'''
const assert = require('node:assert/strict');
let posts = 0, refreshes = 0, messages = [], retired = false;
const state = {csrf: 'fixture', renderSeq: 1};
let textarea, saveButton, replaced, addButton, activeEditor;
const main = {querySelector() { return activeEditor; }};
function el(tag, cls, text) {
  return {tag, cls, text, children: [], appendChild(child) { this.children.push(child); },
          replaceWith(node) { replaced = node; }, replaceChildren(...nodes) { this.children = nodes; },
          setAttribute() {}, focus() {}};
}
const document = {createElement(tag) { const node = el(tag); node.value = ''; if (tag === 'textarea') textarea = node; return node; }};
function append(parent, ...nodes) { nodes.forEach(node => parent.appendChild(node)); }
function button(cls, label, action) {
  const node = {disabled: false, action};
  if (label === 'save') saveButton = node;
  if (label === 'Add') addButton = node;
  return node;
}
function ensureCsrf() { return Promise.resolve(state.csrf); }
function fetch() { posts++; return Promise.reject(new Error('response lost')); }
function skeletonView() { return 'loading'; }
function setView(view) { assert.equal(view, 'loading'); retired = true; }
function reroute() { assert(retired, 'stale controls must be removed before readback'); refreshes++; }
function toast(message) { messages.push(message); }
(async () => {
  const controls = [{disabled: false}, {disabled: false}];
  const error = {hidden: false, textContent: ''};
  const pending = saveControls(controls, error, () => apiPost('/api/loops', {op: 'resolve'}));
  assert(controls.every(c => c.disabled));
  await pending;
  assert.equal(posts, 1);
  assert.equal(refreshes, 1);
  assert(messages[0].includes('Could not confirm'));
  assert(!messages[0].includes('nothing was changed'));
  assert(controls.every(c => !c.disabled));
  assert.equal(error.hidden, true);
  const before = refreshes;
  const row = masterRow(1, {name: 'Context'}, 'saved text');
  row.children[1].children[0].action();
  textarea.value = 'unsaved standing context';
  saveButton.action();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshes, before, 'the standing-file editor must survive a lost response');
  assert.equal(replaced, textarea, 'the editor stays in place');
  assert.equal(textarea.value, 'unsaved standing context');
  assert.equal(saveButton.disabled, false, 'the retained draft stays editable');
  assert.equal(posts, 2, 'the standing save must not replay');
  const addLoop = addLoopControl();
  addLoop.children[0].action();
  textarea.value = 'send Sarah the revised deck';
  const loopDraft = textarea;
  addButton.action();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshes, before, 'add-loop text must survive a lost POST on the shared endpoint');
  assert.equal(loopDraft.value, 'send Sarah the revised deck');
  assert.equal(addButton.disabled, false);
  assert.equal(posts, 3, 'add loop must not replay');
  activeEditor = loopDraft;
  await saveControls(controls, error, () => apiPost('/api/loops', {op: 'resolve', anchor_key: 'sibling'}));
  assert.equal(refreshes, before, 'a sibling row save must preserve the open add-loop editor');
  assert.equal(loopDraft.value, 'send Sarah the revised deck');
  assert(controls.every(c => c.disabled), 'uncertain sibling controls must stay fenced');
  assert.equal(error.hidden, false);
  assert(error.textContent.includes('refresh'));
  assert.equal(posts, 4, 'the sibling operation must not replay');
  await saveControls(controls, error, () => { throw new Error('unhandled'); });
  assert.equal(error.hidden, false);
  assert(error.textContent.includes('Could not confirm'));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_now_tasks_failed_read_then_retry_empty():
    source = (Path(__file__).parent / 'static/app.js').read_text()
    functions = ''.join(re.search(r'\n  function %s\(.*?\n  \}\n' % name, source, re.S).group(0)
                        for name in ('viewNow', 'buildLoopsSection', 'errorView', 'emptyState', 'updateLoopCount'))
    script = functions + r'''
const assert = require('node:assert/strict');
let tries = 0, page;
const state = {renderSeq: 0, entered: {}};
function el(tag, cls, textContent = '') {
  return {cls, textContent, children: [], appendChild(node) { this.children.push(node); },
          querySelector(selector) { return this.children.find(child => child.cls === selector.slice(1)); }};
}
const document = {createDocumentFragment: el};
const main = {appendChild(frag) { page = frag; }};
function append(parent, node) { if (node) parent.appendChild(node); }
function api(path) {
  if (path === '/api/loops' && ++tries === 1) return Promise.reject(new Error('503'));
  return Promise.resolve(path === '/api/loops' ? {loops: [], parked: []} : {});
}
function button(cls, label, click) { return {textContent: label, click, children: []}; }
function ledgerCap(label, right) { const node = el('h2', '', label); append(node, right); return node; }
function capCount(text) { return el('span', '', text); }
function enterOnce(view, node) { return node; }
function buildDocket() { return el('p', '', 'Calendar still loaded'); }
function setView() {} function skeletonView() {} function buildParkedSection() {}
function buildUpdateNote() {} function addLoopControl() { return el('button'); }
function buildThesis() { return el('p'); } function friendlyDate() { return 'Thursday'; }
function nodes(node) { return [node, ...node.children.flatMap(nodes)]; }
(async () => {
  viewNow();
  await new Promise(setImmediate);
  const first = nodes(page);
  assert(first.some(n => n.textContent === 'Calendar still loaded'));
  assert(!first.some(n => n.textContent === '0 open' || n.textContent === 'Nothing is waiting on you'));
  assert(first.some(n => n.textContent === "Tasks couldn't load."));
  first.find(n => n.textContent === 'Try again').click();
  await new Promise(setImmediate);
  const second = nodes(page);
  assert.equal(tries, 2);
  assert(second.some(n => n.textContent === 'Nothing is waiting on you'));
  assert(second.some(n => n.textContent === '0 open'));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], check=True, text=True)

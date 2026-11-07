import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { LIMITS, readArticle } from '../src/index.js';

const html = await readFile(new URL('./fixtures/article.html', import.meta.url), 'utf8');
const sourceUrl = 'https://example.com/news/library';
const runCli = input => new Promise((resolve, reject) => {
  const child = spawn(process.execPath, [new URL('../src/cli.js', import.meta.url).pathname]);
  let stdout = '', stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.on('error', reject);
  child.stdin.on('error', () => {});
  child.on('close', code => resolve({ code, stdout, stderr, result: JSON.parse(stdout) }));
  child.stdin.end(input);
});

test('extracts an article and removes surrounding navigation', async () => {
  const result = await readArticle({ html, sourceUrl });
  assert.equal(result.ok, true);
  assert.equal(result.article.title, 'A library opens its doors');
  assert.equal(result.article.sourceUrl, sourceUrl);
  assert.match(result.article.text, /neighborhood library opened/);
  assert.doesNotMatch(result.article.text, /Subscribe|Copyright/);
  assert.equal(result.coverage.truncated, false);
  assert.equal(result.coverage.returnedTextChars, Array.from(result.article.text).length);
});

test('malformed article markup is still readable', async () => {
  const result = await readArticle({ html: html.replaceAll('</p>', '').replace('</article>', ''), sourceUrl });
  assert.equal(result.ok, true);
  assert.match(result.article.text, /repair workshop/);
});

test('minified paragraph boundaries remain separated', async () => {
  const result = await readArticle({ html: html.replace(/>\s+</g, '><'), sourceUrl });
  assert.equal(result.ok, true);
  assert.match(result.article.text, /accessible entrance\. Library staff/);
});

test('does not execute script or request external resources', async t => {
  let requests = 0;
  const server = createServer((_request, response) => { requests++; response.end('unexpected'); });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const endpoint = `http://127.0.0.1:${server.address().port}/trap`;
  const malicious = `<script>document.body.innerHTML='EXECUTED_MARKER'; fetch('${endpoint}')</script>
    <script src="${endpoint}"></script><link rel="stylesheet" href="${endpoint}">
    <img src="${endpoint}" onerror="fetch('${endpoint}')"><iframe src="${endpoint}"></iframe>
    <style>@import url('${endpoint}'); body { background: url('${endpoint}'); }</style>`;
  const result = await readArticle({ html: html.replace('</body>', malicious + '</body>'), sourceUrl });
  assert.equal(result.ok, true);
  assert.match(result.article.text, /neighborhood library/);
  assert.doesNotMatch(result.article.text, /EXECUTED_MARKER|fetch\(/);
  assert.equal(requests, 0);
});

test('reports truncation explicitly using Unicode code points', async () => {
  const paragraph = `<p>${'A neighborhood library welcomes everyone, including readers of every age. 📚 '.repeat(80)}</p>`;
  const result = await readArticle({ html: `<html><head><title>Long article</title></head><body><article>${paragraph.repeat(8)}</article></body></html>`, sourceUrl });
  assert.equal(result.ok, true);
  assert.equal(result.coverage.truncated, true);
  assert.equal(Array.from(result.article.text).length, LIMITS.textChars);
  assert.ok(result.coverage.originalTextChars > LIMITS.textChars);
  assert.equal(result.coverage.returnedTextChars, LIMITS.textChars);
  assert.ok(!/[\uD800-\uDBFF]$/.test(result.article.text));
});

test('rejects nonarticles, invalid jobs and oversized HTML with typed errors', async () => {
  assert.equal((await readArticle({ html: '<html><body><nav>Home Login Contact</nav></body></html>', sourceUrl })).error.code, 'NO_ARTICLE');
  assert.equal((await readArticle({ html: ' ', sourceUrl })).error.code, 'INVALID_INPUT');
  assert.equal((await readArticle({ html, sourceUrl, extra: true })).error.code, 'INVALID_INPUT');
  assert.equal((await readArticle({ html: 'x'.repeat(LIMITS.htmlBytes + 1), sourceUrl })).error.code, 'HTML_TOO_LARGE');
  assert.equal((await readArticle({ html: '<div></div>'.repeat(LIMITS.domElements + 1), sourceUrl })).error.code, 'DOM_TOO_LARGE');
});

test('URL is validated metadata; local, credential-bearing and non-HTTP URLs are rejected', async () => {
  for (const url of ['file:///etc/passwd', 'ftp://example.com/x', 'http://localhost/x', 'http://127.0.0.1/x',
    'http://2130706433/x', 'http://[::1]/x', 'https://host.local/x', 'https://a.internal/x',
    'https://u:p@example.com', 'https://bad..example.com', 'https://-bad.example.com']) {
    assert.equal((await readArticle({ html, sourceUrl: url })).error.code, 'INVALID_SOURCE_URL', url);
  }
});

test('CLI returns exactly one JSON result with status and no HTML logging', async () => {
  const result = await runCli(JSON.stringify({ html, sourceUrl }));
  assert.equal(result.code, 0);
  assert.equal(result.result.ok, true);
  assert.equal(result.stdout.split('\n').length, 2);
  assert.equal(result.stderr, '');
  const invalid = await runCli('{broken');
  assert.equal(invalid.code, 1);
  assert.equal(invalid.result.error.code, 'INVALID_INPUT');
  const oversized = await runCli('x'.repeat(LIMITS.inputBytes + 1));
  assert.equal(oversized.code, 1);
  assert.equal(oversized.result.error.code, 'INPUT_TOO_LARGE');
});

test('CLI terminates an unfinished input stream', { timeout: 15000 }, async () => {
  const child = spawn(process.execPath, [new URL('../src/cli.js', import.meta.url).pathname]);
  let stdout = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stdin.write('{');
  const [code] = await once(child, 'close');
  assert.equal(code, 1);
  assert.equal(JSON.parse(stdout).error.code, 'INPUT_TIMEOUT');
});

import { spawn } from 'node:child_process';
import { isIP } from 'node:net';
import { fileURLToPath } from 'node:url';
import { LIMITS, failure } from './contracts.js';

export { LIMITS, failure };

// This validates metadata only. A future fetcher needs DNS, redirect and egress checks.
function sourceUrl(value) {
  if (typeof value !== 'string' || value.length > LIMITS.sourceUrlChars) return null;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password ||
        !host.includes('.') || isIP(host.replace(/^\[|\]$/g, '')) ||
        !/^[a-z0-9.-]+$/.test(host) || host.split('.').some(label =>
          !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) ||
        /(?:^|\.)(?:localhost|local|internal|lan|home|invalid)$/.test(host)) return null;
    return url.href;
  } catch {
    return null;
  }
}

/** Run one job in a disposable parser process. Never fetches the source URL. */
export async function readArticle(job) {
  if (!job || typeof job !== 'object' || Array.isArray(job) ||
      Object.keys(job).some(key => !['html', 'sourceUrl'].includes(key)) ||
      typeof job.html !== 'string' || !job.html.trim()) {
    return failure('INVALID_INPUT', 'Expected an object containing nonempty html and sourceUrl.');
  }
  if (Buffer.byteLength(job.html) > LIMITS.htmlBytes) {
    return failure('HTML_TOO_LARGE', 'HTML exceeds the 1 MiB limit.');
  }
  const url = sourceUrl(job.sourceUrl);
  if (!url) return failure('INVALID_SOURCE_URL', 'sourceUrl must be a public HTTP(S) hostname URL without credentials.');

  return new Promise(resolve => {
    const child = spawn(process.execPath, ['--max-old-space-size=128',
      fileURLToPath(new URL('./worker.js', import.meta.url))], {
      stdio: ['pipe', 'pipe', 'ignore'], env: {},
    });
    const chunks = [];
    let bytes = 0;
    let settled = false;
    const finish = result => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.kill('SIGKILL');
      resolve(result);
    };
    const timer = setTimeout(() => finish(failure('EXTRACTION_TIMEOUT', 'Extraction exceeded its time limit.')), LIMITS.timeoutMs);
    child.on('error', () => finish(failure('WORKER_FAILED', 'Could not start the extraction worker.')));
    child.stdin.on('error', () => {}); // Exit handling below reports early worker closure.
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > LIMITS.workerOutputBytes) {
        finish(failure('WORKER_FAILED', 'Extraction exceeded its output limit.'));
      } else chunks.push(chunk);
    });
    child.on('close', code => {
      if (settled) return;
      if (code !== 0) return finish(failure('WORKER_FAILED', 'Extraction worker exited unexpectedly.'));
      try { finish(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { finish(failure('WORKER_FAILED', 'Extraction worker returned an invalid result.')); }
    });
    child.stdin.end(JSON.stringify({ html: job.html, sourceUrl: url }));
  });
}

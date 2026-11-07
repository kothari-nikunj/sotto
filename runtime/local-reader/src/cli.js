import { LIMITS, failure } from './contracts.js';
import { readArticle } from './index.js';

// Exactly one JSON value per invocation. No URL or article data is logged.
let done = false;
const reply = result => {
  if (done) return;
  done = true;
  clearTimeout(timer);
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = result.ok ? 0 : 1;
  process.stdin.destroy();
};
const timer = setTimeout(() => reply(failure('INPUT_TIMEOUT', 'Input was not completed within 10 seconds.')), LIMITS.timeoutMs);
let size = 0;
const chunks = [];
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > LIMITS.inputBytes) reply(failure('INPUT_TOO_LARGE', 'JSON input exceeds the 2 MiB limit.'));
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply(failure('INVALID_INPUT', 'Could not read JSON input.')));
process.stdin.on('end', async () => {
  if (done) return;
  clearTimeout(timer);
  let job;
  try { job = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { reply(failure('INVALID_INPUT', 'Expected one JSON object on standard input.')); return; }
  reply(await readArticle(job));
});

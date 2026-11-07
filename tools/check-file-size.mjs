#!/usr/bin/env node
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const baseline = JSON.parse(readFileSync(join(root, 'tools/file-size-baseline.json'), 'utf8'));
const extensions = ['.bash', '.cjs', '.js', '.jsx', '.mjs', '.py', '.rs', '.sh', '.swift', '.ts', '.tsx'];
if (baseline.version !== 1 || baseline.maxBytes !== 32000 || baseline.maxLines !== 800 ||
    JSON.stringify(baseline.extensions) !== JSON.stringify(extensions)) {
  throw new Error('file-size: invalid committed baseline schema or source scope');
}
const pruned = new Set(['.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', 'target']);
const failures = [];
let measured = 0;

function scan(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const path = join(directory, entry.name);
    const name = relative(root, path).split('\\').join('/');
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      if (!pruned.has(entry.name) && name !== 'runtime/sotto-chat/dist') scan(path);
      continue;
    }
    if (!entry.isFile() || !extensions.includes(extname(entry.name))) continue;
    const data = readFileSync(path);
    const lines = data.length === 0 ? 0 : data.toString('utf8').split('\n').length - (data.at(-1) === 10 ? 1 : 0);
    const cap = baseline.files[name] ?? { bytes: 32000, lines: 800 };
    if (!Number.isSafeInteger(cap.bytes) || !Number.isSafeInteger(cap.lines) || cap.bytes < 0 || cap.lines < 0) {
      throw new Error(`file-size: invalid cap for ${name}`);
    }
    measured++;
    if (data.length > cap.bytes || lines > cap.lines) {
      failures.push(`${name}: ${data.length}/${cap.bytes} bytes, ${lines}/${cap.lines} lines`);
    }
  }
}

scan(root);
for (const failure of failures) console.error(`file-size: ${failure}`);
if (failures.length) {
  console.error('file-size: split the source or reduce it; do not raise committed caps to pass.');
  process.exitCode = 1;
} else {
  console.log(`file-size: ${measured} source files fit committed byte and line caps`);
}

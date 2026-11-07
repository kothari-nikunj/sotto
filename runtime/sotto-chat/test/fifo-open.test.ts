import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmod, lstat, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { ConversationSourceGuard, ownerOnlyAuthority } from '../src/agent/source-authority.js';

const root = path.resolve('.');
const child = path.join(root, 'test/fifo-child.mjs');
function childRun(kind: string, target: string, timeout = 20_000): string {
  return execFileSync(process.execPath, ['--import', 'tsx', child, kind, target], {
    cwd: root, timeout, encoding: 'utf8', env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: tmpdir() },
  }).trim();
}
async function fifo(directory: string, name: string): Promise<string> {
  const target = path.join(directory, name);
  execFileSync('mkfifo', [target]);
  return target;
}
function validConfig(directory: string) {
  return {
    version: 1, channel: 'telegram', stateDirectory: path.join(directory, 'state'), mediaRoot: path.join(directory, 'media'),
    bearerToken: 'test-only-bearer-token-is-at-least-32-characters-long', port: 0,
    owner: { accountId: 'telegram-owner', ownerId: '123' }, lineKey: 'primary', target: 'owner-target',
    providerThreadId: 'telegram:123',
    credentials: { telegramToken: 'fixture-token', telegramLinkFile: { bot_token: 'fixture-token', allowed_user: 123 } },
    instructions: 'Offline test.',
    modelProxy: { origin: 'https://proxy.example', bearer: 'fixture-proxy-token', chatModelId: 'chat', nativeMediaModelId: 'media' },
    allowedSkills: [],
  };
}

test('config rejects a FIFO promptly instead of blocking in open before fstat', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'sotto-config-fifo-'));
  try {
    const target = await fifo(directory, 'config.fifo');
    const output = childRun('config', target);
    assert.match(output, /^OPENING:config\nCONFIG_REJECTED:Config must be a regular non-symlink file$/);
    assert.equal((await lstat(target)).isFIFO(), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('invalid FIFO transcript baseline is purged and atomically replaced with a regular proof', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'sotto-baseline-fifo-'));
  try {
    const target = await fifo(directory, 'baseline.fifo');
    const output = JSON.parse(childRun('baseline', target).split('\n').at(-1)!);
    assert.equal(output.purges, 1);
    assert.equal(output.regular, true);
    assert.equal(output.mode, 0o600);
    assert.equal(output.revision, ownerOnlyAuthority().revision);
    assert.equal((await lstat(target)).isFile(), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('regular private files retain their current config and baseline behavior', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'sotto-private-files-'));
  try {
    const configPath = path.join(directory, 'runtime.json');
    await writeFile(configPath, JSON.stringify(validConfig(directory)), { mode: 0o600 });
    await chmod(configPath, 0o600);
    assert.equal((await loadRuntimeConfig(configPath)).owner.ownerId, '123');

    const baselinePath = path.join(directory, 'baseline.json');
    const authority = ownerOnlyAuthority();
    await writeFile(baselinePath, JSON.stringify({ version: 1, revision: authority.revision }), { mode: 0o600 });
    let purges = 0;
    await new ConversationSourceGuard({ baselinePath, reader: { read: async () => authority }, purge: async () => { purges++; } }).begin();
    assert.equal(purges, 0);
    assert.equal(JSON.parse(await readFile(baselinePath, 'utf8')).revision, authority.revision);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

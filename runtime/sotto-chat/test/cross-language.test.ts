import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { OperationalJournal } from '../src/journal/index.js';
import { ReceiptedDelivery } from '../src/delivery.js';
import { createHttpServices } from '../src/services.js';
import { createChatHttpServer, listenLoopback } from '../src/http.js';

test('Python receiver client and Node service agree on send identity, receipt and unattended contracts', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-contract-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  let sends = 0;
  const runIds: string[] = [];
  const delivery = new ReceiptedDelivery(journal, new Map([['photon:owner', {
    threadId: 'owner-thread', transport: { send: async () => {
      sends++; return { acceptance: 'accepted' as const, providerMessageIds: ['provider-1'] };
    } },
  }]]), { channel: 'photon', tenantId: '', projectFingerprint: 'a'.repeat(64),
    ownerId: 'owner', accountId: 'account', lineKey: 'line' });
  const services = createHttpServices({
    delivery, allowedSkills: new Set(['morning-brief']),
    agent: { generateUnattended: async input => { runIds.push(input.runId); return { text: 'prepared result', usage: { inputTokens: 7, outputTokens: 3 } }; } },
    sessions: { execute: async () => ({ ok: true }) }, health: async () => ({ connected: false }),
  });
  const bearerToken = 'cross-language-test-bearer-00000000000';
  const server = createChatHttpServer(services, { bearerToken, allowedTargets: new Set(['photon:owner']), mediaRoot: directory });
  const port = await listenLoopback(server);
  const clientPath = resolve(dirname(fileURLToPath(import.meta.url)), '../../../adapters/sotto/client.py');
  const script = `
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('client', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules['client'] = module
spec.loader.exec_module(module)
client = module.ChatClient(sys.argv[2], sys.argv[3])
first = client.send_receipt('fixture reply', 'photon:owner', 'cross-language-1')
second = client.send_receipt('fixture reply', 'photon:owner', 'cross-language-1')
receipt = client.receipt('cross-language-1')
run = client.run('prepare', 'job-1', skill='morning-brief')
print(json.dumps({'first':first, 'second':second, 'receipt':receipt, 'run':run}))
`;
  try {
    const result = await promisify(execFile)('python3', ['-I', '-c', script, clientPath, `http://127.0.0.1:${port}`, bearerToken], { timeout: 20_000, maxBuffer: 64_000 });
    const parsed = JSON.parse(result.stdout);
    assert.equal(parsed.first[0], true);
    assert.equal(parsed.second[0], true);
    assert.equal(parsed.receipt.message_id, 'provider-1');
    assert.deepEqual(parsed.run, { text: 'prepared result', usage: { inputTokens: 7, outputTokens: 3 } });
    assert.equal(sends, 1);
    assert.deepEqual(runIds, ['job-1']);
    const usagePath = join(directory, 'usage.json');
    const cli = await promisify(execFile)('python3', ['-I', join(dirname(clientPath), 'run.py'), '--usage-file', usagePath, 'prepare via receiver subprocess'], {
      timeout: 20_000, maxBuffer: 64_000,
      env: { PATH: process.env.PATH, SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'job-2',
        SOTTO_CHAT_RUNTIME_URL: `http://127.0.0.1:${port}`, SOTTO_CHAT_RUNTIME_TOKEN: bearerToken },
    });
    assert.equal(cli.stdout, 'prepared result\n');
    assert.deepEqual(JSON.parse(readFileSync(usagePath, 'utf8')), { input_tokens: 7, output_tokens: 3 });
    assert.deepEqual(runIds, ['job-1', 'job-2']);
    assert.equal(sends, 1, 'unattended execution never posts its result');
  } finally {
    server.closeAllConnections();
    await new Promise<void>((done, reject) => server.close(error => error ? reject(error) : done()));
    journal.close(); rmSync(directory, { recursive: true, force: true });
  }
});

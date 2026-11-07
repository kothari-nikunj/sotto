import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OperationalJournal } from '../src/journal/index.js';
import { ReceiptedDelivery } from '../src/delivery.js';
import { createHttpServices } from '../src/services.js';

test('unattended receiver run passes identity and cancellation but never posts', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-services-'));
  const journal = new OperationalJournal(join(dir, 'journal.sqlite'));
  let sends = 0;
  const delivery = new ReceiptedDelivery(journal, new Map([['owner', { threadId: 'owner', transport: { send: async () => {
    sends++; return { acceptance: 'accepted' as const, providerMessageIds: ['p1'] };
  } } }]]), { channel: 'photon', tenantId: '', projectFingerprint: 'a'.repeat(64),
    ownerId: 'owner', accountId: 'account', lineKey: 'line' });
  const controller = new AbortController();
  let generated: unknown;
  const services = createHttpServices({
    delivery,
    agent: { generateUnattended: async input => { generated = input; return { text: 'saved result' }; } },
    sessions: { execute: async request => ({ action: request.action }) },
    allowedSkills: new Set(['morning-brief']),
    health: async () => ({ connected: false }),
  });
  try {
    const result = await services.run({ prompt: 'prepare', skill: 'morning-brief', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'run-1' } }, controller.signal);
    assert.deepEqual(result, { text: 'saved result' });
    assert.deepEqual(generated, { text: 'Run the morning-brief skill for this task.\n\nprepare', runId: 'run-1', signal: controller.signal });
    await services.run({prompt: 'Answer supplied evidence', answer_only: true, env: {SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'answer-1'}}, controller.signal);
    assert.deepEqual(generated, {text: 'Answer supplied evidence', answerOnly: true, runId: 'answer-1', signal: controller.signal});
    assert.equal(sends, 0);
    await assert.rejects(() => services.run({ prompt: 'x', skill: 'unknown', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'run-2' } }, controller.signal), /Unrecognized skill/);
    assert.equal(sends, 0);
    assert.deepEqual(await services.receipt('unknown'), { found: false });
  } finally { journal.close(); rmSync(dir, { recursive: true, force: true }); }
});

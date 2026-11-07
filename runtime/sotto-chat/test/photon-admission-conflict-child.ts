// Drives the pinned Photon adapter's non-awaiting routeInbound path with real journal admission.
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPhotonSdkAdapter } from '../src/channels/sdk.js';
import { DurableAdmissionChat } from '../src/channels/chat.js';
import { SqliteChatStateAdapter } from '../src/channels/state.js';
import { OperationalJournal } from '../src/journal/index.js';

const folder = mkdtempSync(join(tmpdir(), 'sotto-photon-conflict-'));
const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
const adapter = createPhotonSdkAdapter({ photonProjectId: 'project', photonProjectSecret: 'secret' } as never) as never as {
  chat: unknown; routeInbound(space: unknown, message: unknown, options: unknown): Promise<unknown> };
let admissions = 0;
const chat = new DurableAdmissionChat({ userName: 'sotto', adapters: { imessage: adapter as never },
  state: new SqliteChatStateAdapter(journal.db), concurrency: 'concurrent', logger: 'silent' } as never,
  ({ message, threadId }) => {
    admissions++;
    // Same synchronous shape as the gateway: application.receive -> coordinator -> journal.admitInbound.
    journal.admitInbound({ eventId: message.id, accountKey: 'account', lineKey: 'line', threadKey: threadId,
      channel: 'photon', sender: 'owner', payload: JSON.stringify({ text: message.text }) });
    return { admitted: true, handoff: 'coordinator' };
  });
adapter.chat = chat;
const space = { id: 'any;-;+15551234567', phone: '+15557654321' };
const message = (text: string) => ({ id: 'provider-message', content: { type: 'text', text }, direction: 'inbound',
  sender: { id: '+15551234567' }, timestamp: new Date() });
await adapter.routeInbound(space, message('original'), {});
await adapter.routeInbound(space, message('redelivered with different content'), {});
await new Promise(resolve => setTimeout(resolve, 200));
process.stdout.write(`SURVIVED admissions=${admissions} pending=${journal.listPendingInbound().length}\n`);
journal.close(); rmSync(folder, { recursive: true, force: true });

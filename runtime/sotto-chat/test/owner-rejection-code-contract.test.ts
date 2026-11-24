import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import type { z } from 'zod';
import { calendarRejectionSchema } from '../src/tools/owner-calendar-contract.js';
import { contactsRejectionSchema } from '../src/tools/owner-contacts-contract.js';
import { gmailDraftRejectionSchema } from '../src/tools/owner-gmail-draft-contract.js';
import { bridgeSendRejectionSchema } from '../src/tools/owner-bridge-send-contract.js';
import { ownerBriefRequestRejectionSchema } from '../src/tools/owner-brief-request-client.js';
import { ownerReminderRejectionSchema } from '../src/tools/owner-reminder-tool-contract.js';
import { OperationalJournal } from '../src/journal/index.js';

// Every rejection code a Python owner tool can return must parse on the Node side; otherwise a
// write that had no effect is recorded "unknown" forever (a saved rejection replays verbatim).
const receiver = new URL('../../trigger-receiver/', import.meta.url);
const shared = ['owner_google_effect.py'];
const producers: [string[], z.ZodEnum][] = [
  [['runtime_owner_calendar_tool.py', ...shared], calendarRejectionSchema as never],
  [['runtime_owner_contacts_tool.py', ...shared], contactsRejectionSchema as never],
  [['runtime_owner_gmail_draft_tool.py', ...shared], gmailDraftRejectionSchema as never],
  [['runtime_owner_bridge_send_tool.py'], bridgeSendRejectionSchema as never],
];
// Every expression that can become a rejection code: a RefusalError argument (not a class base), a `code` value in a
// response, or a `refusal=` keyword. Each `A if C else B` contributes both branches.
const expression = /(?<!class \w*)RefusalError\(\s*([^)]*?)\s*\)|['"]code['"]\s*:\s*(outcome\.get\('status', '[a-z_]+'\)|[^,}\n]+)|\brefusal\s*=\s*([^)\n]+)/g;
// Reviewed non-literal forms. Each only passes on a code that a literal above already produced
// (a raised RefusalError's `.code`, a saved rejection, a pre-contact refusal, the `refusal` argument).
const PASSTHROUGH = new Set(['error.code', "saved['code']", "result['precontact_refusal']", 'refusal']);

function producedCodes(files: string[], extra: Record<string, string[]> = {}): {codes: Set<string>; unreviewed: string[]} {
  const codes = new Set<string>(), unreviewed: string[] = [];
  for (const file of files)
    for (const match of readFileSync(new URL(file, receiver), 'utf8').matchAll(expression)) {
      const value = (match[1] ?? match[2] ?? match[3])!.trim();
      const conditional = /^(.*?) if .* else (.*)$/.exec(value);
      for (const part of conditional ? [conditional[1]!.trim(), conditional[2]!.trim()] : [value]) {
        const quoted = /^['"]([a-z_]+)['"]$/.exec(part);
        if (quoted) codes.add(quoted[1]!);
        else if (Object.hasOwn(extra, part)) for (const code of extra[part]!) codes.add(code);
        else if (!PASSTHROUGH.has(part)) unreviewed.push(`${file}: ${part}`);
      }
    }
  return {codes, unreviewed};
}

for (const [files, schema] of producers)
  test(`every rejection code ${files[0]} produces parses in its Node contract`, () => {
    const {codes, unreviewed} = producedCodes(files);
    assert.ok(codes.size > 0, 'the extractor found no codes; the Python shape changed');
    assert.deepEqual([...codes].filter(code => !schema.safeParse(code).success), []);
    assert.deepEqual(unreviewed, [], 'a non-literal rejection code needs review (and a PASSTHROUGH entry)');
  });

function pythonFunction(file: URL, name: string): string {
  const functions = readFileSync(file, 'utf8').split(/^def /m);
  const source = functions.find(part => part.startsWith(`${name}(`));
  assert.ok(source, `Python producer ${name} is missing`);
  return source;
}

test('reminder receiver and canonical writer rejection codes match the Node contract', () => {
  const writer = new URL('../../sotto-chief-of-staff/_shared/scripts/schedule_wakeup.py', receiver);
  const writerOutcomes = ['_create_owner', 'cancel_admitted'].flatMap(name =>
    pythonFunction(writer, name).split('\n').filter(line => line.trimStart().startsWith('return ')).flatMap(line =>
      Array.from(line.matchAll(/['"]([a-z_]+)['"], (?:row|None|changed)/g), match => match[1]!)));
  const {codes, unreviewed} = producedCodes(['runtime_owner_reminder_tool.py'],
    {outcome: writerOutcomes.filter(code => code !== 'committed')});
  assert.deepEqual(unreviewed, []);
  assert.deepEqual([...codes].sort(), [...ownerReminderRejectionSchema.options].sort());
});

test('brief receiver and admission rejection codes match the Node contract', () => {
  const admission = pythonFunction(new URL('receiver.py', receiver), 'request_owner_brief');
  const statuses = Array.from(admission.matchAll(/\breturn \d+, \{'status': '([a-z_]+)'/g), match => match[1]!)
    .filter(status => status !== 'known_request' && status !== 'queued');
  const {codes, unreviewed} = producedCodes(['runtime_owner_brief_request.py'],
    {"outcome.get('status', 'unavailable')": [...statuses, 'unavailable']});
  assert.deepEqual(unreviewed, []);
  assert.deepEqual([...codes].sort(), [...ownerBriefRequestRejectionSchema.options].sort());
});

test('canonical reminder and brief rejections survive journal settlement and intent adoption', () => {
  const journal = new OperationalJournal(':memory:');
  try {
    const owner = {accountId:'account', ownerId:'owner', lineKey:'line', channel:'photon' as const, scopeHash:'a'.repeat(64)};
    const context = {threadId:'thread', destination:'owner', lineKey:'line'};
    const admit = (eventId: string, text: string) => {
      const source = journal.admitInbound({eventId, accountKey:owner.accountId, lineKey:owner.lineKey,
        threadKey:context.threadId, channel:owner.channel, sender:owner.ownerId, payload:JSON.stringify({
          inbound:{eventId,accountId:owner.accountId,senderId:owner.ownerId,channel:owner.channel,text},context,
        })}).event.id as string;
      const authority = {...owner, runId:eventId};
      journal.startRun({runId:eventId,eventId:source});
      return {source,authority};
    };
    for (const code of ownerReminderRejectionSchema.options) {
      const {source,authority} = admit(`reminder-${code}`, 'Remind me to call Dana.');
      const args = {due:'2026-10-09T08:15:00Z',action:'call Dana'};
      const request = journal.prepareOwnerReminderToolAction(authority, source, args);
      journal.beginAction(request.actionId);
      journal.settleOwnerReminderToolAction(request.actionId, authority, code);
      assert.equal(journal.readOwnerReminderToolAction(request.actionId, authority).rejection, code);
      assert.equal(journal.prepareOwnerReminderToolAction(authority, source, args).actionId, request.actionId);
    }
    for (const code of ownerBriefRequestRejectionSchema.options) {
      const providerMessageId = `brief-${code}`;
      const {source,authority} = admit(providerMessageId, 'Give me my evening brief');
      const args = {kind:'evening' as const, refresh:false, remainder:null, provider_message_id:providerMessageId};
      const request = journal.prepareOwnerBriefRequestAction(authority, source, args);
      journal.beginAction(request.actionId);
      journal.settleOwnerBriefRequestAction(request.actionId, authority, code);
      assert.equal(journal.readOwnerBriefRequestAction(request.actionId, authority).rejection, code);
      assert.equal(journal.prepareOwnerBriefRequestAction(authority, source, args).actionId, request.actionId);
    }
  } finally { journal.close(); }
});

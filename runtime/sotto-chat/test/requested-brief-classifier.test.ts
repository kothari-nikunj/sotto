import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { classifyRequestedBrief, isReadChatBriefRemainder, ownerBriefIntent } from '../src/tools/requested-brief-classifier.js';
import { normalizeInbound } from '../src/channels/normalize.js';
const inbound = {channel: 'photon' as const, eventId: 'original-provider-id', accountId: 'account', senderId: 'owner', isDirect: true};
test('explicit brief grammar preserves independent remainder', () => {
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'Send me my evening brief, and what time is my dentist?'}),
    {kind: 'evening', refresh: false, remainder: 'what time is my dentist?'});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'Give me my evening brief'}), {kind: 'evening', refresh: false, remainder: null});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'Update my brief!'}), {kind: 'auto', refresh: true, remainder: null});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: "I'd like my morning brief."}), {kind: 'morning', refresh: false, remainder: null});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'Give me my evening brief and tell me what is on my calendar'}), {kind: 'evening', refresh: false, remainder: 'tell me what is on my calendar'});
});
test('ambiguous and bound input remains ordinary chat with original identity', () => {
  for (const text of ['Do not send my brief', '"Give me my brief"', 'Give me my brief and send it to Alex', 'Give me my brief and a summary', 'Give me my brief and show my brief', 'I mentioned my brief']) assert.equal(classifyRequestedBrief({...inbound, text}), null);
  const bound = normalizeInbound({...inbound, text: 'Give me my brief', replyBound: true});
  assert.equal(bound.replyBound, true);
  assert.equal(classifyRequestedBrief(bound), null);
  assert.equal(bound.eventId, inbound.eventId);
});

test('modal mutation remainder gets a separate-message prompt', () => {
  const request = classifyRequestedBrief({...inbound, text: 'Give me my brief and can you remind me tomorrow'});
  assert.equal(request?.remainder, 'can you remind me tomorrow');
  assert.equal(isReadChatBriefRemainder(request!.remainder!), false);
  assert.equal(isReadChatBriefRemainder('could you tell me what is on my calendar'), true);
});


test('explicit fresh brief uses existing refresh lane without broad context matching',()=>{
 for(const text of ['Please send me a fresh evening brief.','Could you give me my fresh evening brief?']) assert.deepEqual(classifyRequestedBrief({...inbound,text}),{kind:'evening',refresh:true,remainder:null});
 assert.deepEqual(classifyRequestedBrief({...inbound,text:'Send me a fresh morning brief.'}),{kind:'morning',refresh:true,remainder:null});
 for(const text of ['Do not send me a fresh evening brief.','"Please send me a fresh evening brief."','She said please send me a fresh evening brief.','Please send me a fresh evening brief for Alex.']) assert.equal(classifyRequestedBrief({...inbound,text}),null);
});

test('explicit current brief uses the fresh lane while targeted briefs remain ordinary chat',()=>{
 for(const [text,kind] of [['Send me my current evening brief.','evening'],['Could you give me a current morning brief?','morning'],['Please show me the current brief.','auto']] as const)
  assert.deepEqual(classifyRequestedBrief({...inbound,text}),{kind,refresh:true,remainder:null});
 assert.deepEqual(classifyRequestedBrief({...inbound,text:'Send me my current evening brief and tell me what is on my calendar'}),{kind:'evening',refresh:true,remainder:'tell me what is on my calendar'});
 for(const text of ['Do not send me my current evening brief.','"Send me my current evening brief."','She said send me my current evening brief.','Send me my current evening brief for Alex.','Send me my current company brief.','Send me my current work-prep brief.','Send me my current evening brief for my meeting.','Send me my current evening brief and send it to Alex'])
  assert.equal(classifyRequestedBrief({...inbound,text}),null);
});


test('standalone owner brief fragments and shorthand use the existing admission grammar', () => {
  for (const [text, kind, refresh] of [
    ['morning brief pls', 'morning', false], ['evening brief plz!', 'evening', false],
    ['my brief please', 'auto', false], ['daily brief pls', 'auto', false],
    ['brief please', 'auto', false], ['please morning brief', 'morning', false],
    ['fresh morning brief pls', 'morning', true], ['current evening brief', 'evening', true],
    ['send me my morning brief pls', 'morning', false], ['pls refresh my brief', 'auto', true],
  ] as const) assert.deepEqual(classifyRequestedBrief({...inbound, text}), {kind, refresh, remainder: null});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'morning brief pls and tell me what is on my calendar'}),
    {kind: 'morning', refresh: false, remainder: 'tell me what is on my calendar'});
});

test('brief fragments never turn mentions, negation, quoted requests or targeted copy into admission', () => {
  for (const text of ['not my morning brief pls', 'no morning brief please', "don't send a morning brief pls",
    'I mentioned morning brief pls', 'she asked morning brief pls', 'what is a morning brief',
    'morning brief for Alex pls', 'morning brief for my meeting', 'company brief pls',
    '"morning brief pls"', 'morning brief pls and send it to Alex', 'morning brief pls instead of a report'])
    assert.equal(classifyRequestedBrief({...inbound, text}), null);
  assert.equal(classifyRequestedBrief({...inbound, text: 'morning brief pls', replyBound: true}), null);
  assert.equal(classifyRequestedBrief({...inbound, text: 'morning brief pls', media: [{mimeType: 'image/png'}]}), null);
});

test('one shared intent table binds TS, Python and the narrower fast path', () => {
  const vectors = JSON.parse(readFileSync(new URL('./fixtures/owner-brief-intent-vectors.json', import.meta.url), 'utf8')) as
    Array<{text: string; intent: {kind: string; refresh: boolean} | null}>;
  const python = resolve('../../.venv/bin/python');
  const receiver = fileURLToPath(new URL('../../trigger-receiver/', import.meta.url));
  const py = JSON.parse(execFileSync(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), ['-c',
    'import json,sys\nimport runtime_owner_brief_request as b\nprint(json.dumps([b.owner_brief_intent(t) for t in json.load(sys.stdin)]))'],
    {cwd: receiver, input: JSON.stringify(vectors.map(v => v.text)), env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1'}}).toString());
  for (const [i, {text, intent}] of vectors.entries()) {
    assert.deepEqual(ownerBriefIntent(text), intent, `TS: ${text}`);
    assert.deepEqual(py[i], intent, `Python: ${text}`);
    const fast = classifyRequestedBrief({...inbound, text});
    if (fast) assert.ok(intent && intent.kind === fast.kind && (!fast.refresh || intent.refresh), `fast path exceeds intent: ${text}`);
  }
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'send my brief and tell me which tasks are not done'}),
    {kind: 'auto', refresh: false, remainder: 'tell me which tasks are not done'});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'give me my brief and check if Dana can stop by'}),
    {kind: 'auto', refresh: false, remainder: 'check if Dana can stop by'});
  assert.equal(classifyRequestedBrief({...inbound, text: 'send my brief and do not include email'}), null);
});


test('normal articles use the same direct and mixed brief admission', () => {
  for (const [text, kind, refresh] of [
    ['send me an updated brief', 'auto', true],
    ['send me a fresh brief', 'auto', true],
    ['send me an evening brief', 'evening', false],
  ] as const) assert.deepEqual(classifyRequestedBrief({...inbound, text}), {kind, refresh, remainder: null});
  assert.deepEqual(classifyRequestedBrief({...inbound, text: 'send me an updated brief and tell me which tasks are not done'}),
    {kind: 'auto', refresh: true, remainder: 'tell me which tasks are not done'});
  for (const text of ['do not send me an updated brief', '\"send me an updated brief\"',
    'She said send me an updated brief', 'send me an updated brief for Alex',
    'send me an updated brief and then stop', "send me an updated brief. Actually, don't.",
    'send me an updated brief and do not include email', 'send me an updated brief and send it to Alex'])
    assert.equal(classifyRequestedBrief({...inbound, text}), null, text);
  // Multiple sentences still belong to chat; direct admission must not swallow their remainder.
  assert.equal(classifyRequestedBrief({...inbound, text: 'Thanks. Send me an updated brief.'}), null);
});


test('punctuated brief modifiers stay with the model while shared admission accepts the request', () => {
  for (const text of [
    'Give me a fresh evening brief, including anything worth preparing for over the next few days. Keep the help offer focused on meeting prep this time.',
    'Please prepare my morning brief, focusing on tomorrow’s meetings.',
    'Refresh my brief; prioritize deadlines and calendar changes.',
    'Give me my evening brief — keep it concise.',
  ]) {
    assert.ok(ownerBriefIntent(text));
    assert.equal(classifyRequestedBrief({...inbound, text}), null, text);
  }
});

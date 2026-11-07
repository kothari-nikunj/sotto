import assert from 'node:assert/strict';
import test from 'node:test';
import {skillPackRootForConfig} from '../src/bootstrap/main.js';
import { parsePreferenceCommand, createPreferenceCommand } from '../src/tools/preference-command.js';

test('shared preference command catalog maps exact argv and refuses unsupported or malformed commands', () => {
  for (const [add, remove, field] of [
    ['mute-sender', 'unmute-sender', 'mute_senders'], ['mute-person', 'unmute-person', 'mute_people'],
    ['mute-section', 'unmute-section', 'mute_sections'], ['tone', 'remove-tone', 'tone_notes'], ['vip', 'unvip', 'vip_people'],
  ]) {
    assert.deepEqual(parsePreferenceCommand([add!, 'Exact owner words']), {operation: 'add', field, value: 'Exact owner words'});
    assert.deepEqual(parsePreferenceCommand([remove!, 'Exact owner words']), {operation: 'remove', field, value: 'Exact owner words'});
  }
  assert.equal(parsePreferenceCommand(['show']), null);
  assert.deepEqual(parsePreferenceCommand(['clear-tone']), {operation: 'clear-tone'});
  for (const direction of ['fewer', 'no', 'more']) assert.deepEqual(parsePreferenceCommand(['nudge-budget', direction]), {operation: 'nudge-budget', direction});
  for (const args of [['audio', 'morning'], ['snooze', 'tomorrow'], ['show', 'extra'], ['mute-person', ' untrimmed'], ['tone', 'line\nline'], ['nudge-budget', 'many']]) assert.throws(() => parsePreferenceCommand(args));
});

test('command facade exposes actual backend results and refuses unenabled script paths before execution', async () => {
  const calls: unknown[] = [];
  const execution = {read: async () => ({coverage: 'explicit_only' as const, preferences: {}, nudge_budget: 3, truncated_fields: []}), change: async (args: unknown) => {calls.push(args); return {status: 'rejected', code: 'target_unavailable'};}};
  const command = createPreferenceCommand({execution: execution as never});
  const result = await command({id: 'preferences', args: ['unmute-person', 'Missing']}, {});
  assert.deepEqual(result, {backend: 'owner-preference-receiver', coverage: 'explicit_only', result: {status: 'rejected', code: 'target_unavailable'}});
  await assert.rejects(command({id: '/other/preferences.py', args: ['show']}, {}), /not enabled/);
  for (const id of ['$HOME/.hermes/skills/sotto/_shared/scripts/preferences.py', '~/.hermes/skills/sotto/_shared/scripts/preferences.py'])
    await assert.rejects(command({id, args: ['show']}, {}), /not enabled/);
  assert.equal(calls.length, 1);
});


test('master-file command accepts only the no-selector offer command and returns actual execution result', async () => {
  let calls = 0;
  const context = {mode: 'chat', ownerRequestKey: 'trusted'};
  const command = createPreferenceCommand({execution: {acceptOffer: async (received: unknown) => {
    assert.equal(received, context); calls++; return {status: 'no_match'};
  }} as never});
  assert.deepEqual(await command({id: 'master-file', args: ['accept-offer']}, context), {
    backend: 'owner-preference-receiver', coverage: 'accepted_procedure_offer', result: {status: 'no_match'},
  });
  for (const args of [[], ['append', 'Procedures', 'model rule'], ['accept-offer', 'model selector']])
    await assert.rejects(command({id: 'master-file', args}, context));
  assert.equal(calls, 1);
});


test('installed omitted-root selection enables exact packaged preference alias without enabling unrelated paths', async () => {
  const skillRoot = skillPackRootForConfig({configPath: '/data/config.json'},
    'file:///app/runtime/sotto-chat/dist/bootstrap/main.js');
  assert.equal(skillRoot, '/app/sotto-skills');
  let reads = 0;
  const command = createPreferenceCommand({skillRoot, execution: {read: async () => {reads++; return {coverage: 'explicit_only'};}} as never});
  assert.deepEqual(await command({id: '/app/sotto-skills/_shared/scripts/preferences.py', args: ['show']}, {}),
    {backend: 'owner-preference-receiver', coverage: 'explicit_only', result: {coverage: 'explicit_only'}});
  await assert.rejects(command({id: '/other/_shared/scripts/preferences.py', args: ['show']}, {}));
  assert.equal(reads, 1);
});

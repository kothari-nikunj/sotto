import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { readSkillIndex } from '../src/tools/skills.js';

/*
 * The shared SKILL.md text reaches the model unchanged, so every command or tool it names must be
 * one this runtime actually exposes. Sotto exposes `run_sotto_script` command IDs and native tools,
 * never script paths or a host shell. Each table below is also checked against the routing source,
 * so the table cannot claim a command, verb or flag the runtime does not parse.
 */
const PACK = path.resolve('../../sotto-chief-of-staff');
const SRC = path.resolve('src');

// run_sotto_script command ID → the argv flags its parser accepts.
const COMMANDS: Record<string, readonly string[]> = {
  'knowledge-query': ['--person', '--person-coverage', '--editable-person', '--topic', '--relevant-days', '--company'],
  'loops-query': ['--parked'], 'retune-scan': [], 'triage-queue': [], 'relationship-attention': [],
  'relationship-importance': ['--person'], 'granola-history': ['--days', '--meeting-id'], 'style-apply': [],
  'knowledge-edit': ['--op', '--anchor', '--loop-version', '--loop-identity', '--correction', '--to', '--direction', '--text',
    '--contact', '--identifier', '--slug', '--fact-id', '--fact-version', '--other-slug', '--type', '--date'],
  'retune-apply': ['--anchor', '--loop-version', '--loop-identity', '--correction', '--days'],
  'master-file': ['--section', '--text'], 'preferences': [], 'usefulness-feedback': ['--reference', '--excerpt', '--reason'],
  'meeting-prep': ['--focus'], 'ask-research': ['--person', '--company', '--website'], 'followup': ['--backlog'],
  'relationship-pulse': [], 'proactive': [],
  'web-read': ['--url'], 'docsend-fetch': ['--url', '--passcode', '--fresh'], 'confirm-docsend': [],
  'google-action': ['--to', '--subject', '--body', '--query', '--resource-name', '--fields-json', '--event-id', '--calendar', '--response'],
};
// Commands whose first argv word (or --op value) selects an operation.
const VERBS: Record<string, readonly string[]> = {
  'google-action': ['capabilities', 'contacts-search', 'contacts-get', 'contacts-create', 'contacts-update', 'contacts-delete',
    'confirm-contacts', 'gmail-send', 'gmail-send-confirm', 'calendar-update', 'calendar-update-confirm',
    'calendar-move-update-confirm', 'calendar-move-confirm', 'calendar-rsvp', 'calendar-rsvp-confirm'],
  'preferences': ['show', 'mute-sender', 'unmute-sender', 'mute-person', 'unmute-person', 'mute-section', 'unmute-section',
    'tone', 'remove-tone', 'clear-tone', 'vip', 'unvip', 'nudge-budget', 'snooze-nudges', 'unsnooze-nudges'],
  'master-file': ['get', 'accept-offer', 'append', 'set', 'prioritize', 'confirm-procedure', 'confirm-priorities', 'confirm-context'],
  'retune-apply': ['list', 'cleanup', 'keep', 'snooze'],
  'usefulness-feedback': ['list', 'useful', 'not_useful'],
};
const KNOWLEDGE_EDIT_OPS = ['loop', 'loop-retarget', 'loop-add', 'correct', 'relation-add', 'relation-remove',
  'add-identifier', 'confirm-identifier', 'confirm-relation'];
// Backticked names in these namespaces are native tools (or the capability flags `capabilities` returns).
const TOOL_LIKE = /^(?:(?:owner|gmail|calendar|contacts|accepted|staged)_[a-z_]+|capabilities|health|read_local|read_history|get_messages|get_contacts)$/;
// Names in the same namespaces that are not tools: a tool input field, and the receiver's event
// class for a calendar diff. Each must exist where it is defined.
const NON_TOOL_NAMES: Record<string, string> = {
  calendar_id: path.join('src', 'tools', 'owner-calendar-contract.ts'),
  calendar_change: path.join(PACK, 'event-triage', 'scripts', 'triage_event.py'),
};
// Host-agent instructions that cannot run here: script paths, shells, other agents' CLIs.
const FORBIDDEN: readonly [RegExp, string][] = [
  [/hermes/i, 'a Hermes reference'], [/openclaw/i, 'an OpenClaw reference'], [/execute_code/, 'execute_code'],
  [/`terminal`/, 'the terminal tool'], [/\bpython3?\b/, 'a python invocation'], [/\$HOME\/|~\//, 'a home-directory path'],
  [/\/tmp\//, 'a temp-file path'], [/\b(?:install|start)\.sh\b/, 'an install script'],
  [/\b[A-Za-z0-9_-]+\.py\b/, 'a script name (Sotto exposes command IDs, not scripts)'],
];

async function sources(): Promise<string> {
  const files = [path.join(SRC, 'bootstrap', 'main.ts'), ...(await readdir(path.join(SRC, 'tools'))).map(name => path.join(SRC, 'tools', name)),
    ...(await readdir(path.join(SRC, 'agent'))).map(name => path.join(SRC, 'agent', name))];
  return (await Promise.all(files.filter(file => file.endsWith('.ts')).map(file => readFile(file, 'utf8')))).join('\n');
}

async function skills(): Promise<{name: string; text: string}[]> {
  const indexed = await readSkillIndex(PACK);
  const all = (await readdir(PACK, {withFileTypes: true})).filter(entry => entry.isDirectory() && !entry.name.startsWith('_'));
  const files = new Set(indexed.map(entry => entry.path));
  for (const entry of all) {
    try { await readFile(path.join(PACK, entry.name, 'SKILL.md')); files.add(path.join(PACK, entry.name, 'SKILL.md')); } catch { /* not a skill */ }
  }
  assert.ok(files.size >= 17, 'the pack carries its skills');
  return Promise.all([...files].map(async file => ({name: path.relative(PACK, file), text: await readFile(file, 'utf8')})));
}

function quoted(source: string, literal: string): boolean {
  const key = new RegExp(`(?:^|[\\s{,])${literal.replace(/[-]/g, '\\-')}:`, 'm'); // an object-key verb table
  return source.includes(`'${literal}'`) || source.includes(`"${literal}"`) || key.test(source);
}

/** Inline code spans, and each line of a fenced block, as separate bodies. */
function codeBodies(text: string): string[] {
  const bodies: string[] = [];
  const unfenced = text.replace(/```[^\n]*\n([\s\S]*?)```/g, (_all, block: string) => {
    bodies.push(...block.split('\n'));
    return '';
  });
  for (const span of unfenced.matchAll(/`([^`]+)`/g)) bodies.push(span[1]!);
  for (const line of unfenced.split('\n')) if (/^\s{4,}run_sotto_script\s/.test(line)) bodies.push(line); // indented block
  return bodies.map(body => body.trim()).filter(Boolean);
}

test('every command, verb and flag in the drift tables is routed by the runtime', async () => {
  const src = await sources();
  for (const [id, flags] of Object.entries(COMMANDS)) {
    assert.ok(quoted(src, id), `command ${id} is not routed`);
    for (const flag of flags) assert.ok(quoted(src, flag), `${id} flag ${flag} is not parsed`);
  }
  for (const [id, verbs] of Object.entries(VERBS)) for (const verb of verbs) assert.ok(quoted(src, verb), `${id} ${verb} is not routed`);
  for (const op of KNOWLEDGE_EDIT_OPS) assert.ok(quoted(src, op), `knowledge-edit --op ${op} is not routed`);
  for (const [name, file] of Object.entries(NON_TOOL_NAMES))
    assert.ok((await readFile(path.resolve(file), 'utf8')).includes(name), `${name} does not exist in ${file}`);
});

test('SKILL.md names only commands and tools the Sotto runtime exposes', async () => {
  const src = await sources();
  const tools = new Set<string>([...src.matchAll(/\bid:\s*'([a-z_]+)'/g)].map(match => match[1]!));
  const google = await readFile(path.join(SRC, 'agent', 'google-read.ts'), 'utf8');
  for (const match of google.matchAll(/operation: z\.enum\(\[([^\]]+)\]\)/g))
    for (const name of match[1]!.matchAll(/'([a-z_]+)'/g)) tools.add(name[1]!);
  for (const match of google.matchAll(/([a-z_]+):z\.boolean\(\)/g)) tools.add(match[1]!); // capability flags
  const bridge = await readFile(path.join(SRC, 'agent', 'source-tools.ts'), 'utf8');
  for (const match of bridge.matchAll(/name === '([a-z_]+)'/g)) tools.add(match[1]!);
  for (const name of Object.keys(NON_TOOL_NAMES)) tools.add(name);
  for (const required of ['run_sotto_script', 'accepted_brief_detail', 'gmail_search', 'capabilities', 'read_history', 'owner_routine_propose'])
    assert.ok(tools.has(required), `tool discovery found ${required}`);

  const problems: string[] = [];
  let commands = 0;
  for (const {name, text} of await skills()) {
    for (const [pattern, what] of FORBIDDEN) {
      const hit = pattern.exec(text);
      if (hit) problems.push(`${name}: ${what} (${JSON.stringify(hit[0])})`);
    }
    for (const body of codeBodies(text)) {
      const head = /^([a-z_]+)(?:\s*[({]|$)/.exec(body)?.[1];
      if (head && TOOL_LIKE.test(head) && !tools.has(head)) problems.push(`${name}: unknown tool ${head}`);
      const call = /^run_sotto_script\s+([a-z][a-z0-9-]*)([\s\S]*)$/.exec(body);
      if (!call) continue;
      commands++;
      const [, id, rest] = call as unknown as [string, string, string];
      if (!Object.hasOwn(COMMANDS, id)) { problems.push(`${name}: unknown command ${id}`); continue; }
      for (const flag of rest.match(/(?<![\w-])--[a-z][a-z-]*/g) ?? [])
        if (!COMMANDS[id]!.includes(flag)) problems.push(`${name}: ${id} has no flag ${flag}`);
      const first = /^\s*([a-z][a-z_-]*)/.exec(rest)?.[1];
      if (VERBS[id] && first && !VERBS[id]!.includes(first)) problems.push(`${name}: ${id} has no verb ${first}`);
      const op = /--op\s+([a-z-]+)/.exec(rest)?.[1];
      if (id === 'knowledge-edit' && op && !KNOWLEDGE_EDIT_OPS.includes(op)) problems.push(`${name}: knowledge-edit has no --op ${op}`);
    }
  }
  assert.ok(commands > 20, 'the skills name their commands as run_sotto_script calls');
  assert.deepEqual(problems, []);
});

test('the drift check catches a retired script path, an unknown command and an unknown tool', () => {
  const sample = 'Run `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"` then `run_sotto_script loops-sweep`.';
  assert.ok(FORBIDDEN.some(([pattern]) => pattern.test(sample)));
  assert.ok(!Object.hasOwn(COMMANDS, 'loops-sweep'));
  assert.ok(TOOL_LIKE.test('owner_routine_delete'));
});

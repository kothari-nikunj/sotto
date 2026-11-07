import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { resolveAgentSkills, validateSkillContent } from '@mastra/core/skills';
import { loadAgentSkills } from '../src/agent/skill-loader.js';
import { readSkillIndex } from '../src/tools/skills.js';

test('installed Mastra lists and loads every canonical indexed skill despite differing folder names', async () => {
  const entries = await readSkillIndex(path.resolve('../../sotto-chief-of-staff'));
  assert.equal(entries.length, 17);
  const skills = resolveAgentSkills(await loadAgentSkills(entries.map(entry => entry.path)));
  assert.deepEqual((await skills.list()).map(skill => skill.name).sort(), entries.map(entry => entry.name).sort());
  for (const entry of entries) {
    const parsed = validateSkillContent({ content: await readFile(entry.path, 'utf8') });
    const loaded = await skills.get(entry.name);
    assert.equal(loaded?.instructions, parsed.instructions);
    assert.equal(loaded?.description, parsed.metadata?.description);
    assert.deepEqual(loaded?.metadata, parsed.metadata?.metadata ?? undefined);
    assert.equal(loaded?.license, parsed.metadata?.license);
  }
  const approval = await skills.getReference('sotto-feedback', 'references/_shared/references/approval-tiers.md');
  assert.match(approval ?? '', /A Gmail draft is not a send/);
  const focused = await skills.getReference('sotto-meeting-prep', 'references/meeting-prep-prompt.md');
  assert.match(focused ?? '', /FOCUSED PREP/);
});

async function fixture(body: string, run: (root: string, file: string) => Promise<void>) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'sotto-skill-loader-'));
  const dir = path.join(root, 'feedback');
  await mkdir(dir);
  const file = path.join(dir, 'SKILL.md');
  await writeFile(file, body);
  try { await run(root, file); } finally { await rm(root, { recursive: true, force: true }); }
}
const valid = '---\nname: sotto-feedback\ndescription: Owner feedback\n---\nRead references/rules.md.';

test('malformed skill and missing bundled reference fail rather than silently disappearing', async () => {
  await fixture('---\nname: bad_NAME\n---\nBody', async (_, file) => {
    await assert.rejects(loadAgentSkills([file]), /content is invalid/);
  });
  await fixture(valid, async (_, file) => {
    await assert.rejects(loadAgentSkills([file]), /document is missing/);
  });
});

test('references cannot escape the canonical pack through a symlink', async () => {
  await fixture(valid, async (root, file) => {
    const outside = await mkdtemp(path.join(os.tmpdir(), 'outside-skill-'));
    try {
      await writeFile(path.join(outside, 'rules.md'), 'Outside content');
      await symlink(outside, path.join(root, 'feedback', 'references'));
      await assert.rejects(loadAgentSkills([file]), /escapes pack/);
    } finally { await rm(outside, { recursive: true, force: true }); }
  });
});

test('oversized bundled documents and duplicate canonical names fail closed', async () => {
  await fixture(valid, async (root, file) => {
    await mkdir(path.join(root, 'feedback', 'references'));
    const reference = path.join(root, 'feedback', 'references', 'rules.md');
    await writeFile(reference, 'x'.repeat(128 * 1024 + 1));
    await assert.rejects(loadAgentSkills([file]), /exceed bounds/);
    await writeFile(reference, 'Contained rules');
    await assert.rejects(loadAgentSkills([file, file]), /name is repeated/);
  });
});

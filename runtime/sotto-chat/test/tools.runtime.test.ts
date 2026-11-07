import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { readSkillIndex } from '../src/tools/skills.js';

test('packaged chief-of-staff index resolves representative skill files', async () => {
  const root = resolve('../../sotto-chief-of-staff');
  const skills = await readSkillIndex(root);
  assert.ok(skills.length >= 10);
  assert.ok(skills.some(skill => skill.name === 'sotto-ask' && skill.path.endsWith('/ask/SKILL.md')));
  assert.ok(skills.some(skill => skill.name === 'sotto-morning-brief'));
});

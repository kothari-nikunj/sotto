import { readFile, readdir, realpath } from 'node:fs/promises';
import path from 'node:path';
import { createSkill, validateSkillContent, type InlineSkill } from '@mastra/core/skills';

const MAX_FILE = 128 * 1024;
const MAX_REFERENCES = 256;
const MAX_REFERENCE_BYTES = 2 * 1024 * 1024;

/** Adapt canonical frontmatter names to Mastra without renaming the packaged skill directories. */
export async function loadAgentSkills(skillPaths: readonly string[]): Promise<InlineSkill[]> {
  const names = new Set<string>();
  const skills: InlineSkill[] = [];
  for (const skillPath of skillPaths) {
    const file = await realpath(skillPath);
    const directory = path.dirname(file);
    const root = await realpath(path.dirname(directory));
    if (path.basename(file) !== 'SKILL.md' || path.dirname(directory) !== root) throw new Error('skill path is invalid');
    const content = await readFile(file, 'utf8');
    if (Buffer.byteLength(content) > MAX_FILE) throw new Error('skill is too large');
    const parsed = validateSkillContent({ content });
    if (!parsed.valid || !parsed.metadata || typeof parsed.metadata.name !== 'string' ||
        typeof parsed.metadata.description !== 'string' || !parsed.instructions) throw new Error('skill content is invalid');
    if (names.has(parsed.metadata.name)) throw new Error('skill name is repeated');
    names.add(parsed.metadata.name);
    const references: Record<string, string> = {};
    let total = 0;
    async function bundle(relative: string): Promise<void> {
      const requested = path.join(directory, relative);
      let entries;
      try {
        const resolved = await realpath(requested);
        if (!resolved.startsWith(root + path.sep)) throw new Error('skill reference escapes pack');
        entries = await readdir(resolved, { withFileTypes: true });
      }
      catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
      for (const entry of entries) {
        const key = `${relative}/${entry.name}`;
        const target = await realpath(path.join(directory, key));
        if (!target.startsWith(root + path.sep)) throw new Error('skill reference escapes pack');
        if (entry.isDirectory()) { await bundle(key); continue; }
        if (!entry.name.endsWith('.md')) continue;
        const value = await readFile(target, 'utf8');
        total += Buffer.byteLength(value);
        if (Buffer.byteLength(value) > MAX_FILE || total > MAX_REFERENCE_BYTES || Object.keys(references).length >= MAX_REFERENCES)
          throw new Error('skill references exceed bounds');
        references[key.slice('references/'.length)] = value;
      }
    }
    await bundle('references');
    // Canonical instructions address shared references from the pack root.
    const shared = path.join(root, '_shared', 'references');
    try {
      const resolvedShared = await realpath(shared);
      if (!resolvedShared.startsWith(root + path.sep)) throw new Error('shared reference directory escapes pack');
      for (const entry of await readdir(resolvedShared, { withFileTypes: true })) {
        if (!entry.name.endsWith('.md')) continue;
        const target = await realpath(path.join(shared, entry.name));
        if (!target.startsWith(root + path.sep) || !entry.isFile()) throw new Error('shared reference is invalid');
        const value = await readFile(target, 'utf8');
        total += Buffer.byteLength(value);
        if (Buffer.byteLength(value) > MAX_FILE || total > MAX_REFERENCE_BYTES || Object.keys(references).length >= MAX_REFERENCES)
          throw new Error('skill references exceed bounds');
        references[`_shared/references/${entry.name}`] = value;
      }
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    for (const match of parsed.instructions.matchAll(/(?<![\w-])((?:_shared\/)?references\/[A-Za-z0-9_./-]+\.md)/g)) {
      const key = match[1]!.startsWith('references/') ? match[1]!.slice('references/'.length) : match[1]!;
      if (!Object.hasOwn(references, key)) throw new Error('referenced skill document is missing');
    }
    skills.push(createSkill({ name: parsed.metadata.name, description: parsed.metadata.description,
      instructions: parsed.instructions, references,
      ...(typeof parsed.metadata.license === 'string' ? { license: parsed.metadata.license } : {}),
      ...(parsed.metadata.compatibility !== undefined ? { compatibility: parsed.metadata.compatibility } : {}),
      ...(typeof parsed.metadata['user-invocable'] === 'boolean' ? { 'user-invocable': parsed.metadata['user-invocable'] } : {}),
      ...(parsed.metadata.metadata && typeof parsed.metadata.metadata === 'object' && !Array.isArray(parsed.metadata.metadata)
        ? { metadata: parsed.metadata.metadata as Record<string, unknown> } : {}) }));
  }
  return skills;
}

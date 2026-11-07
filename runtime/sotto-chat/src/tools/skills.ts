import { readFile, readdir, realpath } from 'node:fs/promises';
import path from 'node:path';

export interface SkillIndexEntry { readonly name: string; readonly path: string; }

/** Resolve the packaged skills.sh.json by declared frontmatter name, never by model text. */
export async function readSkillIndex(packRoot: string): Promise<SkillIndexEntry[]> {
  const root = await realpath(packRoot);
  const raw = await readFile(path.join(root, 'skills.sh.json'), 'utf8');
  if (Buffer.byteLength(raw) > 64 * 1024) throw new Error('skill index is too large');
  const index: unknown = JSON.parse(raw);
  if (!index || typeof index !== 'object' || !('groupings' in index) || !Array.isArray(index.groupings)) {
    throw new Error('skill index has invalid groupings');
  }
  const listed = new Set<string>();
  for (const group of index.groupings) {
    if (!group || typeof group !== 'object' || !('skills' in group) || !Array.isArray(group.skills)) throw new Error('skill group is invalid');
    for (const name of group.skills) {
      if (typeof name !== 'string' || !/^sotto-[a-z0-9-]{1,80}$/.test(name) || listed.has(name)) throw new Error('skill name is invalid or repeated');
      listed.add(name);
    }
  }
  const found = new Map<string, string>();
  for (const dir of await readdir(root, { withFileTypes: true })) {
    if (!dir.isDirectory() || dir.name.startsWith('_')) continue;
    const skillFile = path.join(root, dir.name, 'SKILL.md');
    let file: string;
    try { file = await realpath(skillFile); } catch { continue; }
    if (path.dirname(file) !== path.join(root, dir.name)) throw new Error('skill file escapes its directory');
    const content = await readFile(file, 'utf8');
    if (Buffer.byteLength(content) > 128 * 1024) throw new Error('skill file is too large');
    const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(content)?.[1];
    const name = frontmatter && /^name:\s*(sotto-[a-z0-9-]+)\s*$/m.exec(frontmatter)?.[1];
    if (!name) throw new Error(`skill frontmatter name is missing: ${dir.name}`);
    if (found.has(name)) throw new Error(`skill name is repeated: ${name}`);
    found.set(name, file);
  }
  return [...listed].map(name => {
    const file = found.get(name);
    if (!file) throw new Error(`indexed skill is missing: ${name}`);
    return { name, path: file };
  });
}

import fs from 'node:fs';
import { lstat, readFile } from 'node:fs/promises';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { ConversationSourceGuard, ownerOnlyAuthority } from '../src/agent/source-authority.js';
const [kind, target] = process.argv.slice(2);
if (kind === 'config') {
  fs.writeSync(1, 'OPENING:config\n');
  try { await loadRuntimeConfig(target); console.log('CONFIG_ACCEPTED'); }
  catch (error) { console.log(`CONFIG_REJECTED:${error.message}`); }
} else {
  let purges = 0;
  const guard = new ConversationSourceGuard({ baselinePath: target,
    reader: { read: async () => ownerOnlyAuthority() }, purge: async () => { purges++; } });
  fs.writeSync(1, 'OPENING:baseline\n');
  await guard.begin();
  const stat = await lstat(target);
  const row = JSON.parse(await readFile(target, 'utf8'));
  console.log(JSON.stringify({ purges, regular: stat.isFile(), mode: stat.mode & 0o777, revision: row.revision }));
}

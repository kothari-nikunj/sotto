/** Synthetic local benchmark; never opens a pilot journal or calls a provider.
 * Run: node --import tsx scripts/benchmark-hot-paths.ts <baseline-git-ref>
 */
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {performance} from 'node:perf_hooks';
import {OperationalJournal} from '../src/journal/index.js';
const baseline = process.argv[2];
if (!baseline || baseline.startsWith('-')) throw new Error('Supply the baseline Git ref');
const file = 'sotto-agent/runtime/sotto-chat/src/journal/index.ts';
const before = execFileSync('git', ['show', `${baseline}:${file}`], {encoding:'utf8'});
const after = readFileSync(new URL('../src/journal/index.ts', import.meta.url), 'utf8');
function query(source:string) {
  const route = source.match(/const sameRoute = "([^"]+)";/)?.[1];
  const sql = source.match(/const rows = this\.db\.prepare\(`(SELECT d\.[^`]+)`\)\.all\(routeDigest, threadKey\)/)?.[1];
  if (!route || !sql) throw new Error('Continuation query changed; update the benchmark extractor');
  return sql.replace('${sameRoute}', route);
}
const directory = mkdtempSync(join(tmpdir(), 'sotto-hot-path-bench-'));
const journal = new OperationalJournal(join(directory,'journal.sqlite'));
try {
  const insert = journal.db.prepare(`INSERT INTO dispatches
    (id,kind,destination,payload_hash,payload,route_digest,status,current_attempt,max_attempts,
     provider_message_id,provider_message_ids,provider_sent_at_ms,created_at,updated_at)
    VALUES (?,'scheduled_text','synthetic-owner','synthetic-hash',?,?,'accepted',1,1,?,'[]',?,0,?)`);
  const route = 'a'.repeat(64), payload = 'x'.repeat(8192);
  journal.db.exec('BEGIN');
  for (let i=0;i<5000;i++) insert.run(`synthetic-${i}`,payload,route,`provider-${i}`,i,i);
  journal.db.exec('COMMIT');
  const oldStatement = journal.db.prepare(query(before)), newStatement = journal.db.prepare(query(after));
  const oldRows = oldStatement.all(route,'synthetic-thread'), newRows = newStatement.all(route,'synthetic-thread');
  assert.equal(newRows.length,2);
  for (let i=0;i<2;i++) for (const key of Object.keys(newRows[i]!)) assert.deepEqual(newRows[i]![key],oldRows[i]![key]);
  journal.db.exec("UPDATE dispatches SET updated_at=4999 WHERE id='synthetic-4998'");
  const tied = newStatement.all(route,'synthetic-thread');
  assert.equal(tied[0]!.updated_at,tied[1]!.updated_at);
  function measure(statement:typeof oldStatement) {
    for(let i=0;i<3;i++) statement.all(route,'synthetic-thread');
    const times:number[]=[];
    for(let i=0;i<15;i++){const start=performance.now();statement.all(route,'synthetic-thread');times.push(performance.now()-start);}
    return times.sort((a,b)=>a-b)[7];
  }
  console.log(JSON.stringify({fixture:{rows:5000,payloadBytes:8192},baseline,
    before:{returnedRows:oldRows.length,returnedJsonBytes:Buffer.byteLength(JSON.stringify(oldRows)),medianMs:measure(oldStatement)},
    after:{returnedRows:newRows.length,returnedJsonBytes:Buffer.byteLength(JSON.stringify(newRows)),medianMs:measure(newStatement)},
    sameLatestFields:true,tiePreserved:true},null,2));
} finally {journal.close();rmSync(directory,{recursive:true,force:true});}

import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { OperationalJournal } from '../src/journal/index.js';
import { freezeCleanupRepairSelection } from '../src/journal/owner-task-selection.js';

// The cleanup "which ones?" repair is a review like any other: a yes counts only while the repair
// question is still the latest message Sotto sent to that destination.
for (const later of [false, true])
  test(`a cleanup repair question ${later ? 'followed by a later send is not' : 'that is still the latest send is'} confirmable`, () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-cleanup-repair-')), journal = new OperationalJournal(join(folder, 'operations.sqlite'));
    try {
      const sql = (text: string, ...values: (string | number)[]) => journal.db.prepare(text).run(...values);
      sql("INSERT INTO inbound_events(id,source_id,account_key,line_key,thread_key,channel,sender,payload_hash,payload,received_at,outcome) VALUES('repair','repair','a','l','t','photon','owner',?, '',0,'resolved')", 'h'.repeat(64));
      sql("INSERT INTO runs(id,event_id,thread_key,generation,status,started_at,ended_at) VALUES('run','repair','t',1,'completed',0,0)");
      sql("INSERT INTO dispatches(id,kind,destination,payload_hash,payload,status,max_attempts,created_at,updated_at) VALUES('d-repair','interactive','owner','h','{}','accepted',1,10,10)");
      sql("INSERT INTO prepared_replies(id,event_id,run_id,dispatch_id,payload,covered_event_ids,prepared_at) VALUES('p','repair','run','d-repair','which?','[]',10)");
      if (later) sql("INSERT INTO dispatches(id,kind,destination,payload_hash,payload,status,max_attempts,created_at,updated_at) VALUES('d-nudge','scheduled_text','owner','h','{}','accepted',3,20,20)");
      const metadata = { args: { operation: 'propose_cleanup' }, cleanup_repair_event_id: 'repair' };
      sql("INSERT INTO actions(id,run_id,kind,payload_hash,status,result_json,created_at,updated_at) VALUES('a','run','owner_loop_tool','h','succeeded',?,0,0)", JSON.stringify(metadata));
      freezeCleanupRepairSelection(journal, journal.getAction('a')!, 'yes');
      assert.equal(JSON.parse(journal.getAction('a')!.result_json as string).repair_next_accepted, !later);
    } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
  });

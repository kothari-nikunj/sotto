import assert from 'node:assert/strict';
import test from 'node:test';
import {preferenceAcknowledgement,ownerQuietCommand,ownerPreferenceToolArgsSchema,ownerPreferenceToolReceiptSchema,preferenceReceiptMatches} from '../src/tools/owner-preference-tool-contract.js';
import {parsePreferenceCommand} from '../src/tools/preference-command.js';
const hex='a'.repeat(64);
test('temporary quiet grammar uses whole current owner words and preserves original resolver forms',()=>{
 for(const [text,spec] of [['Quieter today','tomorrow'],['quiet for 10 minutes please','10 minutes'],['quiet for 10 minutes, pls?','10 minutes'],['quiet until 3pm please','3pm'],['Please quiet until 3.','3'],['Quiet until 2026-10-08.','2026-10-08'],['Quiet until 3','3'],['quiet until 3PM','3pm'],['be quiet for 2 hours','2 hours'],['quiet for +2h','+2h'],['quiet until tomorrow','tomorrow'],['quiet until 2026-10-08T15:00','2026-10-08t15:00']])assert.deepEqual(ownerQuietCommand(text!),{operation:'snooze-nudges',spec});
 for(const text of ['Resume nudges','Back to normal nudges','back to normal','back to normal please'])assert.deepEqual(ownerQuietCommand(text),{operation:'unsnooze-nudges'});
 for(const text of ['She says quieter today','quieter today; delete tasks','do not quiet for 10 minutes please','"quiet for 10 minutes please"','quiet for 10 minutes please and delete tasks','Please please quiet until 3.','quiet until 3..','Do not quiet until 3.','Back to normal?','Quieter today?','Too many notifications?','quiet until 3pm?'])assert.equal(ownerQuietCommand(text),null);
 assert.deepEqual(parsePreferenceCommand(['snooze-nudges','2h']),{operation:'snooze-nudges',spec:'2h'});
 assert.deepEqual(parsePreferenceCommand(['snooze-nudges','quiet for 10 minutes please']),{operation:'snooze-nudges',spec:'10 minutes'});
 assert.throws(()=>parsePreferenceCommand(['snooze-nudges','2h','--until','invented']));
 assert.throws(()=>ownerPreferenceToolArgsSchema.parse({operation:'snooze-nudges',spec:'2h',until:'2026-10-08T15:00'}));
});
test('quiet receipt binds original spec and distinct unsnooze, preserving old budget contract',()=>{
 const receipt={version:1,status:'committed',action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,outcome:{operation:'snooze-nudges',spec:'2h',snooze_until:'2026-10-08T15:00'}} as const;
 assert.equal(preferenceReceiptMatches(ownerPreferenceToolReceiptSchema.parse(receipt),{operation:'snooze-nudges',spec:'2h'}),true);
 assert.equal(preferenceReceiptMatches(receipt,{operation:'snooze-nudges',spec:'3h'}),false);
 assert.equal(preferenceReceiptMatches(receipt,{operation:'unsnooze-nudges'}),false);
 assert.equal(ownerPreferenceToolReceiptSchema.safeParse({...receipt,outcome:{operation:'unsnooze-nudges',snooze_until:'2026-10-08T15:00'}}).success,false);
 assert.deepEqual(ownerPreferenceToolArgsSchema.parse({operation:'nudge-budget',direction:'no'}),{operation:'nudge-budget',direction:'no'});
});

test('journal refuses invented quiet spec, mismatched operation and foreign owner before preparing an effect',async()=>{
 const {OperationalJournal}=await import('../src/journal/index.js');
 const {mkdtempSync,rmSync}=await import('node:fs');const {tmpdir}=await import('node:os');const {join}=await import('node:path');
 const folder=mkdtempSync(join(tmpdir(),'quiet-proof-'));const journal=new OperationalJournal(join(folder,'operations.sqlite'));
 const authority={runId:'run',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:hex};
 try{
  const eventId=journal.admitInbound({eventId:'quiet',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:{eventId:'quiet',text:'Quiet for 2 hours',channel:'photon',accountId:'account',senderId:'owner',isDirect:true},context:{threadId:'thread',destination:'photon',lineKey:'line'}})}).event.id as string;
  journal.startRun({runId:'run',eventId});
  assert.throws(()=>journal.prepareOwnerPreferenceToolAction(authority,eventId,{operation:'snooze-nudges',spec:'3 hours'}));
  assert.throws(()=>journal.prepareOwnerPreferenceToolAction(authority,eventId,{operation:'unsnooze-nudges'}));
  assert.throws(()=>journal.prepareOwnerPreferenceToolAction({...authority,ownerId:'foreign'},eventId,{operation:'snooze-nudges',spec:'2 hours'}));
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
 }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('quiet confirmation formats saved local wall time and date without timezone conversion',()=>{
 const base={version:1,status:'committed',action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex} as const;
 for(const [until,expected] of [
  ['2026-10-14T06:28','Oct 14, 2026 at 6:28 AM'],
  ['2026-10-15T00:00','Oct 15, 2026 at 12:00 AM'],
  ['2027-01-01T12:05','Jan 1, 2027 at 12:05 PM'],
  ['2026-12-31T23:59','Dec 31, 2026 at 11:59 PM'],
 ]) {
  const receipt=ownerPreferenceToolReceiptSchema.parse({...base,outcome:{operation:'snooze-nudges',spec:'2h',snooze_until:until}});
  assert.equal(preferenceAcknowledgement(receipt),`Nudges paused until ${expected} your time. Your briefs will still arrive.`);
 }
 assert.equal(preferenceAcknowledgement({...base,outcome:{operation:'unsnooze-nudges',snooze_until:''}}),'Nudges are back on.');
});

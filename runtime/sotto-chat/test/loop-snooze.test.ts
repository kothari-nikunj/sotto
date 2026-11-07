import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {parseTaskSnoozeCommand} from '../src/tools/task-command.js';
import {ownerTaskToolArgsSchema,ownerTaskToolReceiptSchema,taskReceiptMatches} from '../src/tools/owner-task-tool-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';

const revision='a'.repeat(64),version='b'.repeat(64),identityDigest='c'.repeat(64);
const args={operation:'snooze' as const,anchor:'source-loop',loop_version:version,loop_identity:identityDigest,source_revision:revision,correction:'Set it aside for 400 days',days:400};
const argv=['snooze','--anchor',args.anchor,'--loop-version',version,'--loop-identity',identityDigest,'--correction',args.correction,'--days','400'];
test('snooze grammar preserves >365 calendar days, trusted revision and exact closed flags',()=>{
 assert.deepEqual(parseTaskSnoozeCommand(argv,revision),args);
 for(const value of ['0','-1','01','1.5','1e3','9007199254740992','3652059'])assert.throws(()=>parseTaskSnoozeCommand([...argv.slice(0,-1),value],revision));
 for(const extra of [['--file','/tmp/loops'],['--source-revision',revision],['--days','2']])assert.throws(()=>parseTaskSnoozeCommand([...argv,...extra],revision));
 assert.throws(()=>parseTaskSnoozeCommand(argv,undefined));
 assert.deepEqual(ownerTaskToolArgsSchema.parse({operation:'close',anchor:'original'}),{operation:'close',anchor:'original'});
});
test('snooze receipt requires exact anchor, active status and real local date',()=>{
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'snooze' as const,anchor_key:args.anchor,post_digest:revision,loop_status:'waiting' as const,snoozed_until:'2028-02-29'};
 assert.equal(taskReceiptMatches(ownerTaskToolReceiptSchema.parse(receipt),args),true);
 assert.equal(taskReceiptMatches({...receipt,anchor_key:'other'},args),false);
 for(const date of ['2027-02-29','0000-01-01','2028-13-01','2028-02-30'])assert.equal(ownerTaskToolReceiptSchema.safeParse({...receipt,snoozed_until:date}).success,false);
 assert.equal(ownerTaskToolReceiptSchema.safeParse({...receipt,loop_status:'resolved'}).success,false);
 assert.equal(ownerTaskToolReceiptSchema.safeParse({...receipt,unexpected:true}).success,false);
});
test('new snooze refuses stale/ambiguous/absent target, owner wording, source and grouped authority before action/contact',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-snooze-proof-'));const journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let contacts=0,currentRevision=revision;
 const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async()=>{contacts++;throw new Error('must not contact');}});
 const row={anchor_key:args.anchor,loop_version:version,loop_identity:identityDigest};let rows=[row];
 client.readLoops=async()=>({coverage:'currently_permitted_canonical_loops',you_owe:rows,waiting_on_them:[],counts:{you_owe:rows.length,waiting_on_them:0}} as never);
 const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision:currentRevision,bridge_sources:[],google_sources:['gmail' as const]})}});
 const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
 const event=journal.admitInbound({eventId:'snooze',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:{eventId:'snooze',text:args.correction,channel:'photon',accountId:'account',senderId:'owner',isDirect:true},context:{threadId:'thread',destination:'photon',lineKey:'line'}})}).event.id as string;
 journal.startRun({runId:'run',eventId:event});
 const context={mode:'chat',runId:'run',ownerRequestKey:event,allowOwnerTask:true,sourceRevision:revision};
 try{
  await assert.rejects(execution.execute(args,{...context,allowOwnerTask:false}));
  await assert.rejects(execution.execute({...args,correction:'Invented owner words'},context));
  rows=[];await assert.rejects(execution.execute(args,context));
  rows=[row,row];await assert.rejects(execution.execute(args,context));
  rows=[{...row,loop_version:'d'.repeat(64)}];await assert.rejects(execution.execute(args,context));
  rows=[row];currentRevision='e'.repeat(64);await assert.rejects(execution.execute(args,context));
  assert.equal(contacts,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
 }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {parseTaskCorrectionCommand,parseTaskCleanupItems} from '../src/tools/task-command.js';
import {ownerTaskToolArgsSchema,taskReceiptMatches} from '../src/tools/owner-task-tool-contract.js';
const revision='a'.repeat(64);
const common=['--anchor','source','--loop-version','b'.repeat(64),'--loop-identity','c'.repeat(64),'--correction','I handled it'];
test('common correction parser binds trusted revision and closes only explicit grammar',()=>{
 assert.deepEqual(parseTaskCorrectionCommand(['--op','loop',...common,'--to','resolved'],revision),{operation:'correct',anchor:'source',loop_version:'b'.repeat(64),loop_identity:'c'.repeat(64),correction:'I handled it',source_revision:revision,to:'resolved'});
 assert.equal(parseTaskCorrectionCommand(['--op','loop-retarget',...common,'--direction','them'],revision).operation,'retarget');
 for(const extra of [['--source-revision','d'.repeat(64)],['--to','resolved'],['--file','/tmp/input']])assert.throws(()=>parseTaskCorrectionCommand(['--op','loop',...common,'--to','dismissed',...extra],revision));
 assert.throws(()=>parseTaskCorrectionCommand(['--op','loop',...common,'--to','resolved'],undefined));
 assert.deepEqual(ownerTaskToolArgsSchema.parse({operation:'close',anchor:'source'}),{operation:'close',anchor:'source'});
});
test('correction receipt must match exact old anchor, operation and requested terminal state',()=>{
 const args=ownerTaskToolArgsSchema.parse({operation:'correct',anchor:'source',loop_version:revision,loop_identity:revision,source_revision:revision,correction:'done',to:'dismissed'});
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'correct' as const,anchor_key:'source',post_digest:revision,loop_status:'dismissed' as const};
 assert.equal(taskReceiptMatches(receipt,args),true);
 assert.equal(taskReceiptMatches({...receipt,loop_status:'resolved'},args),false);
 assert.equal(taskReceiptMatches({...receipt,anchor_key:'other'},args),false);
});


test('person fact command binds exact target version and trusted revision without expanding other edits', () => {
 const argv=['--op','correct','--slug','c_123abc','--fact-id','fact_1','--fact-version','b'.repeat(64),'--text','Dana is the COO.'];
 const args=parseTaskCorrectionCommand(argv,revision);
 assert.deepEqual(args,{operation:'fact_correct',canonical_id:'c_123abc',fact_id:'fact_1',fact_version:'b'.repeat(64),text:'Dana is the COO.',source_revision:revision});
 for(const extra of [['--source-revision',revision],['--file','/tmp/person.md'],['--person','Dana'],['--fact-id','other']])assert.throws(()=>parseTaskCorrectionCommand([...argv,...extra],revision));
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'fact_correct' as const,canonical_id:'c_123abc',fact_id:'fact_1',replacement_fact_id:'fact_2',post_digest:revision};
 assert.equal(taskReceiptMatches(receipt,args),true);
 assert.equal(taskReceiptMatches({...receipt,fact_id:'other'},args),false);
 assert.equal(taskReceiptMatches({...receipt,canonical_id:'c_456def'},args),false);
});

test('identifier command pins raw owner identifier and receipt canonical target without widening grammar',()=>{
 const argv=['--op','add-identifier','--slug','c_123abc','--identifier','Dana.New@Example.com'];
 const args=parseTaskCorrectionCommand(argv,revision);
 assert.deepEqual(args,{operation:'propose_identifier',canonical_id:'c_123abc',identifier:'Dana.New@Example.com',source_revision:revision});
 for(const extra of [['--text','invented'],['--source-revision',revision],['--file','/tmp/person']])assert.throws(()=>parseTaskCorrectionCommand([...argv,...extra],revision));
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'add_identifier' as const,canonical_id:'c_123abc',identifier:'Dana.New@Example.com',added:true,post_digest:revision};
 const legacy={operation:'add_identifier' as const,canonical_id:'c_123abc',identifier:'Dana.New@Example.com',source_revision:revision};assert.equal(taskReceiptMatches(receipt,legacy),true);assert.equal(taskReceiptMatches({...receipt,identifier:'dana.new@example.com'},legacy),false);assert.equal(taskReceiptMatches({...receipt,canonical_id:'c_456def'},legacy),false);
 for(const separator of ['\n','\u0085','\u2028','\u2029'])assert.throws(()=>parseTaskCorrectionCommand([...argv.slice(0,-1),'bad'+separator+'address'],revision));
});

 test('owner-only directed add keeps legacy serialization and binds optional direction receipt',()=>{
 const legacy={operation:'add' as const,text:'Send Cedar packet'};
 assert.deepEqual(parseTaskCorrectionCommand(['--op','loop-add','--text',legacy.text],undefined),legacy);
 const args=parseTaskCorrectionCommand(['--op','loop-add','--text',legacy.text,'--direction','them','--contact','Dana','--identifier','dana@example.com'],undefined);
 assert.deepEqual(args,{...legacy,direction:'them',contact:'Dana',identifier:'dana@example.com'});
 assert.throws(()=>parseTaskCorrectionCommand(['--op','loop-add','--text',legacy.text,'--contact','Dana'],undefined));
 for(const field of ['contact','identifier'])for(const separator of ['\u0085','\u2028','\u2029'])assert.equal(ownerTaskToolArgsSchema.safeParse({...legacy,direction:'them',[field]:'Dana'+separator+'x'}).success,false);
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'add' as const,anchor_key:'task',post_digest:revision,loop_status:'open' as const};
 assert.equal(taskReceiptMatches(receipt,legacy),true);assert.equal(taskReceiptMatches({...receipt,direction:'them'},legacy),false);
 assert.equal(taskReceiptMatches({...receipt,direction:'them'},args),true);assert.equal(taskReceiptMatches(receipt,args),false);assert.equal(taskReceiptMatches({...receipt,direction:'you'},args),false);
 });

test('cleanup command accepts only bounded unique numbered intents, never caller target proofs',()=>{
 const items=[{index:1,action:'dismiss',correction:'Dismiss the first'},{index:2,action:'snooze',correction:'snooze the second for seven days',days:7}];
 assert.deepEqual(parseTaskCleanupItems(['cleanup',JSON.stringify({items})]),items);
 for(const value of [{items:[items[0],items[0]]},{items:[{...items[0],days:7}]},{items:[{index:2,action:'snooze',correction:'snooze second'}]},{items:[{...items[0],anchor:'invented'}]},{items:[],list:{}},{items:[{...items[0],index:51}]}])assert.throws(()=>parseTaskCleanupItems(['cleanup',JSON.stringify(value)]));
 assert.throws(()=>parseTaskCleanupItems(['cleanup','{}','extra']));
});


test('person correction accepts only explicit scalar typed profile fields',()=>{
 const revision='a'.repeat(64);
 const argv=['--op','correct','--slug','c_123abc','--fact-id','fact_1','--fact-version',revision,'--text','Dana is COO at Cedar.'];
 assert.deepEqual(parseTaskCorrectionCommand([...argv,'--title','COO','--company','Cedar'],revision),{operation:'fact_correct',canonical_id:'c_123abc',fact_id:'fact_1',fact_version:revision,text:'Dana is COO at Cedar.',title:'COO',company:'Cedar',source_revision:revision});
 for(const field of ['--title','--company'])for(const value of ['', ' padded ', 'bad\nvalue', 'bad\u0085value', 'bad\u2028value', 'x'.repeat(201)])assert.throws(()=>parseTaskCorrectionCommand([...argv,field,value],revision));
 assert.throws(()=>parseTaskCorrectionCommand(['--op','add','--slug','c_123abc','--text','A fact','--title','COO'],revision));
});

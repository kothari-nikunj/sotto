import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {ownerTaskToolArgsSchema, resolveManyOwnerTextMatches, ownerTaskToolReceiptSchema, resolveManyAcknowledgement, taskReceiptMatches, cleanupSelectionsMatch} from '../src/tools/owner-task-tool-contract.js';

const hex='a'.repeat(64);
const item=(name:string,anchor_key:string)=>({name,anchor_key,loop_version:hex,loop_identity:hex,correction:name});

test('numbered completion shares bounded reference and ambiguity cases with the receiver',()=>{
 const vectors=JSON.parse(readFileSync(new URL('./fixtures/numbered-completion-vectors.json',import.meta.url),'utf8')) as Array<{text:string;indices:number[];corrections?:string[];expected:boolean}>;
 for(const vector of vectors){
  const items=vector.indices.map((index,i)=>({index,action:'resolve' as const,correction:vector.corrections?.[i]??String(index)}));
  assert.equal(cleanupSelectionsMatch(items,vector.text),vector.expected,vector.text);
 }
});

test('one explicit owner clause binds each selected current person or exact owner task',()=>{
  const people=ownerTaskToolArgsSchema.parse({operation:'resolve_many',source_revision:hex,selection_mode:'named_people',items:[item('Tina','t'),item('shimolee','s'),item('Hari','h')]});
  assert.equal(people.operation,'resolve_many');
  if(people.operation!=='resolve_many')return;
  assert.equal(resolveManyOwnerTextMatches(people.items,'Tina shimolee Hari loops are closed. What else is open?',people.selection_mode),true);
  assert.equal(resolveManyOwnerTextMatches(people.items,'Tina, shimolee, and Hari are sorted. What else is open?',people.selection_mode),true);
  for(const text of ["Don't close Tina shimolee Hari loops",'Should Tina shimolee Hari loops be closed?','Tina shimolee loops are closed. What else is open?'])
    assert.equal(resolveManyOwnerTextMatches(people.items,text,people.selection_mode),false);
  const tasks=ownerTaskToolArgsSchema.parse({operation:'resolve_many',source_revision:hex,selection_mode:'owner_tasks',items:[item('Sotto acceptance 11A','a'),item('Sotto acceptance 11B','b')]});
  assert.equal(tasks.operation,'resolve_many');
  if(tasks.operation!=='resolve_many')return;
  assert.equal(resolveManyOwnerTextMatches(tasks.items,'I finished Sotto acceptance 11A and Sotto acceptance 11B; leave Sotto acceptance 11C open.',tasks.selection_mode),true);
  assert.equal(resolveManyOwnerTextMatches(tasks.items,'Sotto acceptance 11A and Sotto acceptance 11B done, Sotto acceptance 11C still open. What’s still open?',tasks.selection_mode),true);
  assert.equal(resolveManyOwnerTextMatches(tasks.items,'I finished Sotto acceptance 11A and Sotto acceptance 11B; leave Sotto acceptance 11A open.',tasks.selection_mode),false);
  const pair=[item('Tina','t'),item('Hari','h')];
  for(const question of ["what’s left?",'what is left?','what remains?'])
    assert.equal(resolveManyOwnerTextMatches(pair,`Tina and Hari done. ${question}`,'named_people'),true,question);
  assert.equal(resolveManyOwnerTextMatches(pair,'Tina and Hari done — what’s left?','named_people'),true);
  assert.equal(resolveManyOwnerTextMatches(pair,"Tina done. Hari is still open. what’s left?",'named_people'),false);
  assert.equal(resolveManyOwnerTextMatches(pair,"Tina done; Hari is still open — what’s left?",'named_people'),false);
  for(const text of ['Tina is done. Hari is pending.','Tina is done; remind me to call Hari.','Tina is sorted, but I need to call Hari.','Tina and Hari are open, Other is done.','Finished Tina and calling Hari tomorrow','finished Tina and Hari is still pending','Tina needs a call; Hari is done'])
    assert.equal(resolveManyOwnerTextMatches(pair,text,'named_people'),false,text);
  assert.equal(resolveManyOwnerTextMatches(tasks.items,'I might finish Sotto acceptance 11A and Sotto acceptance 11B.',tasks.selection_mode),false);
});

test('numbered completion and direct completion retain distinct receipt shapes',()=>{
  const base={version:1,status:'committed',action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,review_hash:hex,outcome:'complete',items:[{index:1,action:'resolve',anchor_key:'a',status:'applied',post_digest:hex,loop_status:'resolved'}]};
  assert.equal(ownerTaskToolReceiptSchema.safeParse({...base,operation:'cleanup'}).success,true);
  assert.equal(ownerTaskToolReceiptSchema.safeParse({...base,operation:'resolve_many',items:[...base.items,{...base.items[0],index:2,anchor_key:'b'}]}).success,false);
  const {review_hash:_,...direct}=base;
  assert.equal(ownerTaskToolReceiptSchema.safeParse({...direct,operation:'resolve_many',items:[...base.items,{...base.items[0],index:2,anchor_key:'b'}]}).success,true);
});

test('multi-loop refusals explain each held reason without exposing internal codes',()=>{
  const args=ownerTaskToolArgsSchema.parse({operation:'resolve_many',source_revision:hex,selection_mode:'named_people',
    items:[item('Tina','t'),item('Hari','h'),item('Alex','a'),item('Dana','d')]});
  const receipt=ownerTaskToolReceiptSchema.parse({version:1,status:'committed',operation:'resolve_many',
    action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,outcome:'partial',items:[
      {index:1,action:'resolve',anchor_key:'t',status:'refused',reason:'target_changed'},
      {index:2,action:'resolve',anchor_key:'h',status:'refused',reason:'target_unavailable'},
      {index:3,action:'resolve',anchor_key:'a',status:'refused',reason:'source_unavailable'},
      {index:4,action:'resolve',anchor_key:'d',status:'refused',reason:'correction_unavailable'},
    ]});
  assert.equal(args.operation,'resolve_many');assert.equal(receipt.operation,'resolve_many');
  if(args.operation!=='resolve_many'||receipt.operation!=='resolve_many')return;
  assert.equal(resolveManyAcknowledgement(receipt,args),[
    'Tina: This one changed since I checked.',
    'Hari: I couldn’t tell which one you meant.',
    'Alex: I can’t check this right now.',
    'Dana: I couldn’t confirm what you wanted changed.',
  ].join('\n'));
});


test('single-task selection accounts for the whole completion clause and preserves explicitly open peers',()=>{
  const one=[{name:'alpha',correction:'alpha'}];
  for(const text of ['alpha done','I finished alpha.','alpha done, beta still open','alpha done beta still open'])
    assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),true,text);
  for(const text of ['alpha and beta done, gamma still open','alpha and beta done','alpha done but alpha still open','alpha done beta open alpha still open','alpha might be done'])
    assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),false,text);
});


test('conversational completion suffixes retain exact selection and negation guards',()=>{
  const one=[{name:'gamma',correction:'gamma'}];
  for(const text of ['gamma is done too','gamma is done now'])
    assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),true,text);
  for(const text of ['gamma is not done too','gamma is not done now','gamma is done too and beta','gamma is done too?'])
    assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),false,text);
  assert.equal(ownerTaskToolArgsSchema.safeParse({operation:'close',anchor:'owner:legacy'}).success,true,'legacy receipt arguments remain readable');
});


test('a hedged or reversed carve-out withdraws the whole completion claim',()=>{
  const one=[{name:'alpha',correction:'alpha'}],pair=[...one,{name:'beta',correction:'beta'}];
  for(const text of ['alpha and beta are done, actually leave them open',"alpha and beta are done, but I'm not sure, so keep both open",'alpha and beta are done; never mind, everything stays open','alpha and beta are done, just kidding, leave all open'])
    assert.equal(resolveManyOwnerTextMatches(pair,text,'owner_tasks'),false,text);
  for(const text of ["I finished alpha, actually don't, keep it open",'alpha done; wait, leave it open'])
    assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),false,text);
  assert.equal(resolveManyOwnerTextMatches(pair,'alpha and beta are done; leave gamma open',"owner_tasks"),true);
});

test('explicit human completion binds the whole single or multi selection',()=>{
 const one=[{name:'alpha',correction:'alpha'}],pair=[...one,{name:'beta',correction:'beta'}];
 for(const text of ["please close alpha", "close alpha", "mark alpha done", "please mark alpha as complete", "alpha is complete", "I have completed alpha", "please close alpha, beta still open", "mark alpha done; leave beta open", "alpha is complete, beta still open", "I have completed alpha; leave beta open"]) assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),true,text);
 for(const text of ["don't close alpha", "please do not close alpha", "mark alpha not done", "alpha is not complete", "I have not completed alpha", "please close alpha?", "please close alpha but alpha still open", "please close alpha and beta still open", "mark alpha done and call beta", "I have completed alpha if beta agrees", "please close alpha and beta", "mark alpha and beta done", "alpha and beta are complete", "I have completed alpha and beta"]) assert.equal(resolveManyOwnerTextMatches(one,text,'owner_tasks'),false,text);
 for(const text of ["please close alpha and beta", "mark alpha and beta done", "alpha and beta are complete", "I have completed alpha and beta"]) assert.equal(resolveManyOwnerTextMatches(pair,text,'owner_tasks'),true,text);
});


test('natural remaining-task inquiries do not change the completion clause',()=>{
 const pair=[{name:'beta',correction:'beta'},{name:'gamma',correction:'gamma'}];
 for(const suffix of ['any test tasks left?','anything else open?','what else?']){
  assert.equal(resolveManyOwnerTextMatches(pair,`beta and gamma are done. ${suffix}`,'owner_tasks'),true);
  for(const clause of ['beta and gamma are not done','beta is done; gamma still open','beta and gamma might be done'])
   assert.equal(resolveManyOwnerTextMatches(pair,`${clause}. ${suffix}`,'owner_tasks'),false);
 }
});


test('numbered completion binds all numbers, including 22, without substring matches',()=>{
 const numbers=[1,4,6,7,8,11,14,13,17,19,20,22];
 const items=numbers.map(index=>({index,action:'resolve' as const,correction:String(index)}));
 assert.equal(cleanupSelectionsMatch(items,'1,4,6,7,8,11,14,13,17,19,20,22 are done. You can close them'),true);
 for(const text of ['11 and 22 are done','1 and 22 are not done','1 and 22 are done. nvm','1 and 22 are done. scratch that','1 and 22 are done?'])
  assert.equal(cleanupSelectionsMatch([items[0]!,items.at(-1)!],text),false,text);
});

test('an unresolved name cannot claim an applied effect; refusal can follow an uncertain effect',()=>{
 const args=ownerTaskToolArgsSchema.parse({operation:'resolve_many',allow_partial:true,source_revision:hex,selection_mode:'named_people',items:[item('Flash','f'),{name:'Annabel',correction:'Annabel'}]});
 const receipt=ownerTaskToolReceiptSchema.parse({version:1,status:'committed',operation:'resolve_many',action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,outcome:'partial',items:[{index:1,action:'resolve',anchor_key:'f',status:'unknown'},{index:2,action:'resolve',anchor_key:'',status:'refused',reason:'target_unavailable'}]});
 assert.equal(taskReceiptMatches(receipt,args),true);
 assert.equal(receipt.operation,'resolve_many');if(receipt.operation!=='resolve_many')return;
 assert.equal(ownerTaskToolReceiptSchema.safeParse({...receipt,items:[receipt.items[0],{index:2,action:'resolve',anchor_key:'a',status:'applied',post_digest:hex,loop_status:'resolved'}]}).success,false);
 const applied=ownerTaskToolReceiptSchema.parse({...receipt,items:[{index:1,action:'resolve',anchor_key:'f',status:'applied',post_digest:hex,loop_status:'resolved'},{index:2,action:'resolve',anchor_key:'a',status:'applied',post_digest:hex,loop_status:'resolved'}],outcome:'complete'});
 assert.equal(taskReceiptMatches(applied,args),false);
});

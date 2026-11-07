import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {cleanupSelectionItems,cleanupListHash,resolveManyOwnerTextMatches,cleanupListSchema,taskPreviewMatches,ownerTaskToolArgsSchema,type CleanupList} from '../src/tools/owner-task-tool-contract.js';
const fullList:CleanupList={operation:'cleanup_list',scope:'open',source_revision:'a'.repeat(64),total:45,remaining:0,review_hash:'',rows:Array.from({length:45},(_,i)=>({index:i+1,anchor_key:'task-'+(i+1),name:'Owner',what:'task '+(i+1),direction:'you_owe',age_days:0,times_surfaced:0,surface_count_provenance:'accepted_delivery',overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss',loop_version:'b'.repeat(64),loop_identity:'c'.repeat(64)}))};
fullList.review_hash=cleanupListHash(fullList);
const vectors=JSON.parse(readFileSync(new URL('./fixtures/numbered-selection-vectors.json',import.meta.url),'utf8')) as Array<{label:string;selection:{correction:string};indices:number[]|null;review?:Record<string,unknown>}>;
for(const vector of vectors)test(`typed numbered selection: ${vector.label}`,()=>{
 const review={...fullList,...vector.review} as CleanupList;
 if(vector.indices===null)assert.throws(()=>cleanupSelectionItems(review,vector.selection,vector.selection.correction));
 else{const selected=cleanupSelectionItems(review,vector.selection,vector.selection.correction);assert.deepEqual(selected.map(row=>row.index),vector.indices);assert.ok(selected.every(row=>row.action==='resolve'&&row.correction===vector.selection.correction));}
});

for(const vector of vectors.filter(vector=>vector.label.startsWith('review multi-sentence')))test(`strict completion recursion: ${vector.label}`,()=>{
 const selection=vector.selection as unknown as {correction:string;clauses:{references:{correction:string}[]}[]};
 const items=selection.clauses[0]!.references.map(ref=>({name:ref.correction,correction:ref.correction}));
 assert.equal(resolveManyOwnerTextMatches(items,selection.correction,'owner_tasks',true),false);
});

test('subset anchor keys are schema checked, hashed and bound to original list request',()=>{
 const keyed={...fullList,anchor_keys:fullList.rows.map(row=>row.anchor_key)};keyed.review_hash=cleanupListHash(keyed);
 assert.notEqual(keyed.review_hash,fullList.review_hash);
 assert.equal(cleanupListSchema.safeParse({...fullList,anchor_keys:null}).success,false);
 const args=ownerTaskToolArgsSchema.parse({operation:'propose_cleanup',scope:'open',source_revision:fullList.source_revision});
 assert.equal(taskPreviewMatches({version:1,status:'preview',event_id:fullList.source_revision,payload_hash:fullList.source_revision,review:keyed},args),false,'rehashing altered anchor keys cannot impersonate the unkeyed accepted request');
});
const legacy=JSON.parse(readFileSync(new URL('./fixtures/legacy-named-completion-vectors.json',import.meta.url),'utf8')) as Array<{text:string;names:string[];expected:boolean}>;
for(const vector of legacy)test(`legacy named punctuation parity: ${JSON.stringify(vector.text)}`,()=>{
 assert.equal(resolveManyOwnerTextMatches(vector.names.map(name=>({name,correction:name})),vector.text,'owner_tasks'),vector.expected);
});

test('whole owner completion matcher returns booleans for singleton evidence',()=>{
 const items=[{name:'That one',correction:'That one'}];
 const accepted:boolean=resolveManyOwnerTextMatches(items,'That one is done.','owner_tasks',true);
 const refused:boolean=resolveManyOwnerTextMatches(items,'That one is not done.','owner_tasks',true);
 assert.equal(typeof accepted,'boolean');assert.equal(accepted,true);
 assert.equal(typeof refused,'boolean');assert.equal(refused,false);
});

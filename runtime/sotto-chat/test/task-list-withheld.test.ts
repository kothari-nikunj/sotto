import assert from 'node:assert/strict';
import test from 'node:test';
import {cleanupListText,type CleanupList} from '../src/tools/owner-task-tool-contract.js';
for(const scope of ['open',undefined] as const)test(`all withheld ${scope??'stale'} rows never imply an empty owner list`,()=>{
 const list:CleanupList={operation:'cleanup_list',source_revision:'a'.repeat(64),rows:[],total:4,remaining:4,review_hash:'b'.repeat(64),...(scope?{scope}:{})};
 const text=cleanupListText(list);
 assert.match(text,/can be safely numbered/);assert.match(text,/4 more/);
 assert.doesNotMatch(text,/No open tasks|No stale loops were found/);
 const empty=cleanupListText({...list,total:0,remaining:0});
 assert.match(empty,scope?/No open tasks/:/No stale loops were found/);
});

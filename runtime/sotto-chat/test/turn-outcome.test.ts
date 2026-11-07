import test from 'node:test';
import assert from 'node:assert/strict';


test('turn outcome preserves independent receipts and clarification precedence', async()=>{
 const {ownerTurnOutcome}=await import('../src/agent/task-receipt-guard.js');
 const facts=new Map<string,unknown>();
 const context={get:(key:string)=>facts.get(key)};
 assert.deepEqual(ownerTurnOutcome(context),{kind:'continue'});
 facts.set('ownerTaskSettled',true);
 facts.set('ownerSkillQueued',true);
 assert.deepEqual(ownerTurnOutcome(context),{kind:'handoff'});
 facts.set('ownerTaskClarification','Choose one task.');
 assert.deepEqual(ownerTurnOutcome(context),{kind:'clarification',text:'Choose one task.'});
 assert.equal(facts.get('ownerTaskSettled'),true);
});

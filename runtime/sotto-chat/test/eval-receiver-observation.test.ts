import test from 'node:test';
import assert from 'node:assert/strict';
import {observeReceiverJson,receiverObservationErrors} from '../evals/receiver-observation.js';

test('invalid receiver JSON remains a sticky turn error without becoming evidence',async()=>{
  const response=new Response('{invalid',{headers:{'content-type':'application/json'}});
  const observation=observeReceiverJson(await response.clone().text());
  // The client can handle the response and continue; the evaluator must still fail the turn.
  assert.equal(await response.text(),'{invalid');
  assert.equal(observation.returnedEvidence,undefined);
  assert.equal(observation.responseBytes,8);
  const requests=[observation,observeReceiverJson('{"ok":true}')];
  assert.deepEqual(receiverObservationErrors(requests),['receiver_invalid_json']);
  assert.deepEqual(requests.filter(request=>request.returnedEvidence!==undefined)
    .map(request=>request.returnedEvidence),[{ok:true}]);
});

test('valid receiver JSON provides evidence without contract errors',()=>{
  const observation=observeReceiverJson('{"rows":[]}');
  assert.deepEqual(observation.returnedEvidence,{rows:[]});
  assert.deepEqual(receiverObservationErrors([observation]),[]);
});

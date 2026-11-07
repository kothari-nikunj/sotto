import test from 'node:test';
import assert from 'node:assert/strict';
import {measureTurn,summarizeMeasurements} from '../evals/measurements.js';

test('latency uses final acceptance including worker delivery, not acknowledgement or chat timing',()=>{
  const result=measureTurn({startedAtMs:1000,replies:[{at:2000},{at:32000}],
    health:{chatTimingCompletedAtMs:2500,chatTiming:{totalMs:1500},
      modelMetrics:{completedAtMs:2500,stepCount:2,tools:[{tool:'knowledge-query',durationMs:9}],
        steps:[{inputTokens:100,outputTokens:10,modelMs:50},{inputTokens:150,outputTokens:20,modelMs:75}]}},
    modelRequests:4,sourceReads:1,latencyClass:'evidence',errors:[]});
  assert.equal(result.requestToFinalAcceptanceMs,31000);
  assert.equal(result.targetMet,false);
  assert.equal(result.chatModelSteps,2);
  assert.equal(result.modelRequests,4);
  assert.equal(result.chatInputTokens,250);
  assert.equal(result.chatModelMs,125);
  assert.equal(result.costUsd,null);
});

test('synthetic provider timings report source/model/tool/output separately without scoring model targets',()=>{
  const measurements=measureTurn({startedAtMs:1000,replies:[{at:1300}],health:{},
    modelRequests:3,sourceReads:1,latencyClass:'ordinary',errors:[],scriptedProvider:true,
    outputCharacters:80,requests:[{kind:'model',path:'/chat/completions',ms:3,responseBytes:90},
      {kind:'receiver',path:'/runtime/knowledge-read',ms:25,responseBytes:800}]});
  assert.equal(measurements.targetMet,null);
  assert.equal(measurements.sourceGatheringMs,25);
  assert.equal(measurements.providerMs,3);
  assert.equal(measurements.outputCharacters,80);
  assert.equal(measurements.responseBytes,890);
  const summary=summarizeMeasurements([{errors:[],measurements}]);
  assert.equal(summary.sampleSize,1);assert.equal(summary.targetSamples,0);
  assert.equal(summary.targetMisses,0);assert.equal(summary.modelRequests,3);
  assert.match(summary.costStatus,/Synthetic/);
});

test('a failed turn cannot borrow timing or tokens from the preceding successful turn',()=>{
  const result=measureTurn({startedAtMs:3000,replies:[],
    health:{chatTimingCompletedAtMs:2000,chatTiming:{totalMs:1},
      modelMetrics:{completedAtMs:2000,stepCount:8,steps:[{inputTokens:999,outputTokens:999}]}},
    modelRequests:0,sourceReads:0,latencyClass:'ordinary',errors:['missing_final_reply']});
  assert.equal(result.requestToFinalAcceptanceMs,null);
  assert.equal(result.targetStatus,'missing');
  assert.equal(result.targetMet,null);
  assert.equal(result.chatTiming,null);
  assert.equal(result.chatModelSteps,null);
  assert.equal(result.chatInputTokens,null);
});

test('a fast acknowledgement with failed background work is not final acceptance',()=>{
  const result=measureTurn({startedAtMs:1000,replies:[{at:2000}],health:{},
    modelRequests:1,sourceReads:0,latencyClass:'brief',
    errors:['background_work_incomplete','queued_is_not_completion']});
  assert.equal(result.requestToFinalAcceptanceMs,null);
  assert.equal(result.targetStatus,'missing');
  assert.equal(result.targetMet,null);
  assert.equal(result.requestToLastAcceptanceMs,1000);
});

test('a targeted turn with no acceptance has no measurement',()=>{
  const result=measureTurn({startedAtMs:1000,replies:[],health:{},modelRequests:0,
    sourceReads:0,latencyClass:'ordinary',errors:[]});
  assert.equal(result.targetStatus,'missing');
  assert.equal(result.targetMet,null);
  const summary=summarizeMeasurements([{errors:[],measurements:result}]);
  assert.equal(summary.targetMissing,1);
  assert.equal(summary.targetsMet,0);
});

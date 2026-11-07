/** Project existing runtime receipts; never substitute chat generation time for delivery. */
export type LatencyClass = 'ordinary' | 'evidence' | 'brief';
export const LATENCY_TARGET_MS: Record<LatencyClass, number> = {ordinary:15_000,evidence:30_000,brief:90_000};

export function measureTurn(input: {
  startedAtMs:number;
  replies:Array<{at:number}>;
  health:any;
  modelRequests:number;
  sourceReads:number;
  errors:readonly string[];
  latencyClass?:LatencyClass;
  requests?:Array<{kind:'model'|'receiver';path:string;ms:number;responseBytes?:number}>;
  outputCharacters?:number;
  scriptedProvider?:boolean;
}) {
  const {startedAtMs,replies,health}=input;
  const metrics=health?.modelMetrics?.completedAtMs>=startedAtMs?health.modelMetrics:undefined;
  const timing=health?.chatTimingCompletedAtMs>=startedAtMs?health.chatTiming:undefined;
  const acceptedAt=replies.length?Math.max(...replies.map(reply=>reply.at)):undefined;
  const requestToLastAcceptanceMs=acceptedAt===undefined?null:Math.max(0,acceptedAt-startedAtMs);
  // A queued acknowledgement is still an accepted transport message when the worker fails.
  // Retain that timing for diagnosis, but never score a failed turn as a completed result.
  const requestToFinalAcceptanceMs=input.errors.length?null:requestToLastAcceptanceMs;
  const targetMs=input.latencyClass?LATENCY_TARGET_MS[input.latencyClass]:null;
  return {
    requestToFinalAcceptanceMs,
    requestToLastAcceptanceMs,
    boundary:'Synthetic transport acceptance, not device observation; inspect replies for usefulness.',
    latencyClass:input.latencyClass??null,targetMs,
    targetStatus:input.scriptedProvider?'unmeasured_actual_model':targetMs===null?'untargeted':requestToFinalAcceptanceMs===null?'missing':'measured',
    targetMet:input.scriptedProvider||targetMs===null||requestToFinalAcceptanceMs===null?null:requestToFinalAcceptanceMs<=targetMs,
    modelRequests:input.modelRequests,sourceReads:input.sourceReads,
    chatTiming:timing??null,
    chatModelSteps:metrics?.stepCount??null,
    chatTools:metrics?.tools??null,
    chatInputTokens:metrics?metrics.steps.reduce((n:number,s:any)=>n+s.inputTokens,0):null,
    chatOutputTokens:metrics?metrics.steps.reduce((n:number,s:any)=>n+s.outputTokens,0):null,
    chatModelMs:metrics?metrics.steps.reduce((n:number,s:any)=>n+s.modelMs,0):null,
    toolMs:metrics?.tools?metrics.tools.reduce((n:number,tool:any)=>n+tool.durationMs,0):null,
    sourceGatheringMs:input.requests?input.requests.filter(request=>request.kind==='receiver'&&/\/runtime\/(?:knowledge-read|google-read|owner-loops|owner-retune|owner-web-read|brief-detail)$/.test(request.path)).reduce((n,request)=>n+request.ms,0):null,
    providerMs:input.requests?input.requests.filter(request=>request.kind==='model').reduce((n,request)=>n+request.ms,0):null,
    outputCharacters:input.outputCharacters??null,
    responseBytes:input.requests?input.requests.reduce((n,request)=>n+(request.responseBytes??0),0):null,
    timingAggregation:'Source/provider/tool durations sum individual calls; overlapping calls are not wall-clock components.',
    measurementMode:input.scriptedProvider?'synthetic-scripted-provider':'actual-model',
    costUsd:null,
    costStatus:input.scriptedProvider?'No paid model calls. Synthetic token fields and provider durations do not estimate actual-model cost or latency.':'Not reported by the proxy; token counts cover the latest chat generation only, not worker or formatting calls.',
  };
}

/** Finite sample accounting; failed or untargeted turns never count as a met target. */
export function summarizeMeasurements(turns:Array<{errors:string[];measurements:ReturnType<typeof measureTurn>;journeyMeasurements?:{modelCalls:number;sourceReads:number;totalMs:number}}>,options:{expectedSamples?:number;scriptedProvider?:boolean}={}) {
  return {sampleSize:turns.length,expectedSamples:options.expectedSamples??turns.length,
    incompleteSamples:Math.max(0,(options.expectedSamples??turns.length)-turns.length),
    correct:turns.filter(turn=>turn.errors.length===0).length,
    failed:turns.filter(turn=>turn.errors.length>0).length,
    targetSamples:turns.filter(turn=>turn.measurements.targetMet!==null).length,
    targetMissing:turns.filter(turn=>turn.measurements.targetStatus==='missing').length,
    targetMisses:turns.filter(turn=>turn.measurements.targetMet===false).length,
    targetsMet:turns.filter(turn=>turn.measurements.targetMet===true).length,
    modelRequests:turns.reduce((n,turn)=>n+turn.measurements.modelRequests,0),
    sourceReads:turns.reduce((n,turn)=>n+turn.measurements.sourceReads,0),
    journeyModelCalls:turns.reduce((n,turn)=>n+(turn.journeyMeasurements?.modelCalls??0),0),
    journeySourceReads:turns.reduce((n,turn)=>n+(turn.journeyMeasurements?.sourceReads??0),0),
    journeyProcessingMs:turns.reduce((n,turn)=>n+(turn.journeyMeasurements?.totalMs??0),0),
    costUsd:null,
    costStatus:options.scriptedProvider||turns.some(turn=>turn.measurements.measurementMode==='synthetic-scripted-provider')
      ?'Synthetic contract timings only; no actual-model target or cost acceptance.':'Proxy billing unavailable; not zero cost.'};
}

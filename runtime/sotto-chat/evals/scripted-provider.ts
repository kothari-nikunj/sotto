/** Deterministic external model boundary for local integration proof, never model acceptance. */
import {z} from 'zod';

const stepSchema=z.object({
  tool:z.object({name:z.string().min(1),arguments:z.record(z.string(),z.unknown())}).strict().optional(),
  text:z.string().min(1).optional(),
  // Inspect tool-role messages only: an expected fact in user text/history is not retrieval.
  toolResultContains:z.array(z.string().min(1)).optional(),
  toolResultExcludes:z.array(z.string().min(1)).optional(),
}).strict().refine(step=>Boolean(step.tool)!==Boolean(step.text),'Exactly one tool or text response required');
export const scriptedProviderSchema=z.object({instructions:z.string().min(1),steps:z.array(stepSchema).min(1),
  // Explicit synthetic provider boundary; never an approved model configuration.
  nativeResponses:z.array(z.object({response:z.record(z.string(),z.unknown()),requestContains:z.array(z.string().min(1)).min(1)}).strict()).max(2).optional()}).strict();
export type ScriptedProvider=z.infer<typeof scriptedProviderSchema>;

export function assertScriptedEndpoint(request:Request, native=false):void {
  const url=new URL(request.url);
  if(request.method!=='POST'||url.pathname!==(native?'/native/v1beta/models/gemini-3.8-flash:generateContent':'/openai/v1/chat/completions')||url.search||url.hash)
    throw new Error('Unexpected scripted model endpoint');
}

export function createScriptedProvider(script:ScriptedProvider) {
  let count=0;
  let nativeCount=0;
  let latestToolCallId:string|undefined;
  return {
    get consumed(){return count;},
    get nativeConsumed(){return nativeCount;},
    respondNative(body:unknown):Response {
      const step=script.nativeResponses?.[nativeCount++];
      if(!step)throw new Error('Unexpected native model request: scripted provider exhausted');
      const request=JSON.stringify(body);
      for(const expected of step.requestContains)if(!request.includes(expected))throw new Error(`Required native composition input missing: ${expected}`);
      return Response.json({candidates:[{content:{parts:[{text:JSON.stringify(step.response)}]},finishReason:'STOP'}],usageMetadata:{promptTokenCount:0,candidatesTokenCount:0}});
    },
    respond(body:any):Response {
      const step=script.steps[count++];
      if(!step)throw new Error('Unexpected model request: scripted provider exhausted');
      const results=latestToolCallId?(body.messages??[]).filter((message:any)=>message.role==='tool'&&message.tool_call_id===latestToolCallId):[];
      const returned=JSON.stringify(results);
      for(const expected of step.toolResultContains??[])if(!returned.includes(expected))
        throw new Error(`Required returned tool evidence missing: ${expected}`);
      for(const forbidden of step.toolResultExcludes??[])if(returned.includes(forbidden))
        throw new Error(`Forbidden returned tool evidence: ${forbidden}`);
      if(step.tool&&!(body.tools??[]).some((tool:any)=>tool.function?.name===step.tool!.name))
        throw new Error(`Script selected unavailable tool: ${step.tool.name}`);
      const message=step.tool?{role:'assistant',content:null,tool_calls:[{id:`scripted-${count}`,type:'function',
        function:{name:step.tool.name,arguments:JSON.stringify(step.tool.arguments)}}]}:{role:'assistant',content:step.text};
      latestToolCallId=step.tool?`scripted-${count}`:undefined;
      return Response.json({id:`scripted-${count}`,created:1,model:'synthetic-scripted-provider',
        choices:[{index:0,message,finish_reason:step.tool?'tool_calls':'stop'}],
        // Synthetic protocol fields, deliberately zero; these are not measured billing tokens.
        usage:{prompt_tokens:0,completion_tokens:0,total_tokens:0}});
    },
  };
}

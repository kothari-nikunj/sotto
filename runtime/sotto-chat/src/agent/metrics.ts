import {ManagedProxyAdmissionBusy,ManagedProxyBudgetExhausted,ManagedProxyRequestTooLarge,ManagedProxyUpstreamUnavailable} from './models.js';
import {SourceAuthorityChanged} from './source-authority.js';
import {z} from 'zod';
import type {InputProcessor, ProcessInputStepArgs} from '@mastra/core/processors';
import type {ToolHooks} from '@mastra/core/tools';
import type {LLMStepResult} from '@mastra/core/stream';

const count = z.number().int().min(0).max(1_000_000_000);
const duration = z.number().int().min(0).max(300_000);
const finish = z.enum(['stop','length','tool-calls','content-filter','error','other','unknown']);
const stepSchema = z.object({durationMs:duration,modelMs:duration,inputTokens:count,outputTokens:count,
  finishReason:finish,processorRetries:count}).strict();
const failureSchema=z.object({category:z.enum(['provider_api','provider_unavailable','budget_exhausted',
  'admission_busy','request_too_large','aborted','invalid_output','invalid_tool_call','tool_error','source_authority','unknown']),
  httpStatus:z.number().int().min(100).max(599).optional()}).strict();

/** Inspect only fixed exception identities/statuses; never retain provider text, bodies or URLs. */
export function classifyModelFailure(error:unknown,signal?:AbortSignal):z.infer<typeof failureSchema> {
  if(signal?.aborted)return {category:'aborted'};
  let current=error;
  let streamFailure=false;
  for(let depth=0;depth<5;depth++){
    try{
      if(current instanceof SourceAuthorityChanged)return {category:'source_authority'};
      if(current instanceof ManagedProxyAdmissionBusy)return {category:'admission_busy'};
      if(current instanceof ManagedProxyBudgetExhausted)return {category:'budget_exhausted'};
      if(current instanceof ManagedProxyRequestTooLarge)return {category:'request_too_large'};
      if(current instanceof ManagedProxyUpstreamUnavailable)return {category:'provider_unavailable',httpStatus:current.status};
      if(!current||typeof current!=='object')break;
      const value=current as {name?:unknown;statusCode?:unknown;cause?:unknown;id?:unknown;code?:unknown;domain?:unknown;details?:unknown};
      // Mastra's synthetic terminal-output errors inherit the generic Error name.
      // Serialization uses code instead of id. Inspect only these fixed identities;
      // arbitrary provider finish reasons and messages never enter diagnostics.
      if(value.domain==='AGENT'&&(value.id==='AGENT_STREAM_ERROR'||value.code==='AGENT_STREAM_ERROR')){
        const details=value.details;
        const malformed=details&&typeof details==='object'&&
          (details as {rawFinishReason?:unknown}).rawFinishReason==='MALFORMED_FUNCTION_CALL';
        if(malformed)return {category:'invalid_tool_call'};
        streamFailure=true; // A recognized underlying cause is more specific than the wrapper.
      }
      // DOMException.name is inherited. Do not require an own-property descriptor.
      const name=value.name;
      if(name==='AbortError'||name==='TimeoutError')return {category:'aborted'};
      if(name==='AI_APICallError')return {category:'provider_api',
        ...(Number.isInteger(value.statusCode)&&Number(value.statusCode)>=100&&Number(value.statusCode)<=599?{httpStatus:value.statusCode as number}:{})};
      if(['AI_NoOutputGeneratedError','AI_NoObjectGeneratedError','AI_TypeValidationError',
          'AI_JSONParseError','AI_InvalidResponseDataError','AI_EmptyResponseBodyError'].includes(String(name)))return {category:'invalid_output'};
      if(['AI_ToolCallRepairError','AI_InvalidToolInputError','AI_NoSuchToolError'].includes(String(name)))return {category:'invalid_tool_call'};
      if(name==='AI_ToolExecutionError'||name==='ToolExecutionError')return {category:'tool_error'};
      current=value.cause;
    }catch{break;}
  }
  return {category:streamFailure?'invalid_output':'unknown'};
}

export const modelMetricsSchema = z.object({version:z.literal(1),mode:z.enum(['chat','unattended']),
  failure:failureSchema.optional(),outcome:z.enum(['success','failed']),completedAtMs:z.number().int().nonnegative(),
  rememberedHistory:z.object({users:count,assistants:count}).strict().optional(),
  lastMemoryPurge:z.object({reason:z.enum(['authority_recovered','authority_unavailable','baseline_unavailable','revision_changed','authority_changed_during_turn']),atMs:z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)}).strict().optional(),
  standingChars:count,promptBytes:count,toolCount:count,toolSchemaBytes:count,schemaUnavailable:count,
  stepCount:count,steps:z.array(stepSchema).max(60),
  tools:z.array(z.object({tool:z.string().regex(/^[A-Za-z0-9_-]{1,80}$/),durationMs:duration,
    outcome:z.enum(['success','failed'])}).strict()).max(256),
}).strict();
export type ModelMetrics = z.infer<typeof modelMetricsSchema>;

export function projectModelMetrics(value:unknown):ModelMetrics|undefined {
  const parsed=modelMetricsSchema.safeParse(value);
  return parsed.success?parsed.data:undefined;
}
const bounded = (value:unknown, max=1_000_000_000):number =>
  typeof value==='number'&&Number.isFinite(value)?Math.min(max,Math.max(0,Math.round(value))):0;

/** Numbers and mounted tool names only; never retains prompts, arguments, outputs or call IDs. */
export class RunMetrics {
  private readonly started = new Map<number,{at:number;modelAt?:number;modelMs?:number;retries:number}>();
  private active = 0;
  private readonly completed = new Set<number>();
  private readonly toolStarts = new WeakMap<object,number>();
  private readonly toolNames = new Set<string>();
  readonly value:ModelMetrics;

  constructor(mode:ModelMetrics['mode']) {
    this.value={version:1,mode,outcome:'failed',completedAtMs:0,standingChars:0,promptBytes:0,
      toolCount:0,toolSchemaBytes:0,schemaUnavailable:0,stepCount:0,steps:[],tools:[]};
  }

  input(args:ProcessInputStepArgs):void {
    this.active=args.stepNumber;
    if(this.value.mode==='chat' && !this.value.rememberedHistory) {
      const excluded=new Set(args.requestContext?.get('historyInputIds') as string[]|undefined);
      const remembered=args.messageList.get.remembered.db().filter(message=>!excluded.has(message.id));
      this.value.rememberedHistory={users:remembered.filter(message=>message.role==='user').length,
        assistants:remembered.filter(message=>message.role==='assistant').length};
    }
    if(!this.started.has(args.stepNumber)&&this.started.size<60)
      this.started.set(args.stepNumber,{at:performance.now(),retries:0});
    const timing=this.started.get(args.stepNumber);
    if(timing)timing.retries=Math.max(timing.retries,bounded(args.retryCount));
    const tools=Object.entries(args.tools??{}).filter(([name])=>!args.activeTools||args.activeTools.includes(name));
    this.value.toolCount=Math.max(this.value.toolCount,tools.length);
    let bytes=0,unavailable=0;
    for(const [name,raw] of tools){
      if(/^[A-Za-z0-9_-]{1,80}$/.test(name))this.toolNames.add(name);
      try{
        const tool=raw as {inputSchema?:unknown;parameters?:unknown};
        const schema=tool.inputSchema??tool.parameters;
        const json=schema&&typeof schema==='object'&&'jsonSchema' in schema?schema.jsonSchema:
          schema&&typeof schema==='object'&&'_zod' in schema?z.toJSONSchema(schema as z.ZodType):schema;
        if(!json||typeof json!=='object'){unavailable++;continue;}
        bytes+=Buffer.byteLength(JSON.stringify(json));
      }catch{unavailable++;}
    }
    this.value.toolSchemaBytes=Math.max(this.value.toolSchemaBytes,bounded(bytes));
    this.value.schemaUnavailable=Math.max(this.value.schemaUnavailable,unavailable);
  }

  request(step:number,prompt:unknown):void {
    const timing=this.started.get(step);
    if(timing)timing.modelAt=performance.now();
    this.value.promptBytes=Math.max(this.value.promptBytes,bounded(Buffer.byteLength(JSON.stringify(prompt))));
  }
  response(step:number):void {
    const timing=this.started.get(step);
    if(timing?.modelAt!==undefined)timing.modelMs=performance.now()-timing.modelAt;
  }
  step(result:LLMStepResult<unknown>):void {
    this.completed.add(this.active);
    this.value.stepCount++;
    if(this.value.steps.length>=60)return;
    const timing=this.started.get(this.active);
    this.value.steps.push({durationMs:bounded(timing?performance.now()-timing.at:0,300_000),
      modelMs:bounded(timing?.modelMs??(timing?.modelAt===undefined?0:performance.now()-timing.modelAt),300_000),
      inputTokens:bounded(result.usage.inputTokens),outputTokens:bounded(result.usage.outputTokens),
      finishReason:finish.safeParse(result.finishReason).success?result.finishReason as z.infer<typeof finish>:'unknown',
      processorRetries:timing?.retries??0});
  }
  readonly hooks:ToolHooks = {
    beforeToolCall:({context})=>{if(context&&typeof context==='object')this.toolStarts.set(context,performance.now());},
    afterToolCall:({context,toolName,error})=>{
      if(!context||typeof context!=='object'||!this.toolNames.has(toolName)||this.value.tools.length>=256)return;
      const at=this.toolStarts.get(context);this.toolStarts.delete(context);
      if(at!==undefined)this.value.tools.push({tool:toolName,durationMs:bounded(performance.now()-at,300_000),outcome:error===undefined?'success':'failed'});
    },
  };
  finish(outcome:ModelMetrics['outcome']):ModelMetrics {
    const pending=this.started.get(this.active);
    if(outcome==='failed'&&pending&&!this.completed.has(this.active)&&this.value.steps.length<60){
      this.value.stepCount++;
      this.value.steps.push({durationMs:bounded(performance.now()-pending.at,300_000),
        modelMs:bounded(pending.modelMs??(pending.modelAt===undefined?0:performance.now()-pending.modelAt),300_000),
        inputTokens:0,outputTokens:0,finishReason:'error',processorRetries:pending.retries});
    }
    this.value.outcome=outcome;this.value.completedAtMs=Date.now();
    return modelMetricsSchema.parse(this.value);
  }
}

export function metricsProcessor(runs:WeakMap<object,RunMetrics>):InputProcessor {
  return {id:'sotto-content-free-metrics',
    processInputStep:args=>{try{if(args.requestContext)runs.get(args.requestContext)?.input(args);}catch{/* Best effort. */}},
    processLLMRequest:args=>{try{if(args.requestContext)runs.get(args.requestContext)?.request(args.stepNumber,args.prompt);}catch{/* Best effort. */}},
    processLLMResponse:args=>{try{if(args.requestContext)runs.get(args.requestContext)?.response(args.stepNumber);}catch{/* Best effort. */}},
  };
}

import {CleanupDiagnosticError,cleanupDiagnostic,type CleanupDiagnostic} from './cleanup-diagnostic.js';
import {ownerLoopEntrySchema,readOwnerLoops} from '../agent/owner-loops.js';
import {readAuthority,sameConsent,type SourceAuthorityReader} from '../agent/source-authority.js';
import {readOwnerContinuityJson} from '../agent/owner-continuity-client.js';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import { createOwnerToolTransport, OwnerToolTransportError } from './owner-tool-transport.js';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { stableActionIdentity } from './action-gate.js';
import { ownerTaskToolArgsSchema, ownerTaskToolKind, ownerTaskToolReceiptSchema, ownerTaskToolRejectionSchema, taskReceiptMatches,
  taskPreviewSchema,taskPreviewMatches,type OwnerTaskToolArgs } from './owner-task-tool-contract.js';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const schema = z.union([ownerTaskToolReceiptSchema,taskPreviewSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('unknown')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: ownerTaskToolRejectionSchema}).strict(),
]);
export interface OwnerTaskToolRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerTaskToolArgs;
}
export type OwnerTaskToolReply = z.infer<typeof schema>;
const hash = (value: string) => createHash('sha256').update(value, 'utf8').digest('hex');
export function createOwnerTaskToolClient(options: OwnerToolClientOptions & {readBearer?:string;sourceAuthority?:SourceAuthorityReader}) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-loop-tool');
  const body=(operation:'preview'|'apply'|'inspect',input:OwnerTaskToolRequest)=>({version:1,operation,action_id:input.actionId,event_id:input.eventId,payload_hash:input.payloadHash,text_hash:hash(input.text),text:input.text,admitted_at_ms:input.admittedAtMs,provider_thread_id:input.providerThreadId,args:input.args});
  const preflight=(input:OwnerTaskToolRequest)=>{if(Buffer.byteLength(JSON.stringify({...body('apply',input),identity:transport.identity}))>65536)throw new Error('Cleanup request exceeds transport capacity');};
  async function call(operation: 'preview' | 'apply' | 'inspect', input: OwnerTaskToolRequest,
                      signal?: AbortSignal): Promise<OwnerTaskToolReply> {
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        !Number.isSafeInteger(input.admittedAtMs) || !input.providerThreadId || !input.text || input.text.length > 16000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
        !ownerTaskToolArgsSchema.safeParse(input.args).success)
      throw new Error('Owner task request is invalid');
    const binding = stableActionIdentity(input.eventId, ownerTaskToolKind, input.args);
    if (binding.actionId !== input.actionId) throw new Error('Owner task request is invalid');
    // A 300-row / 12-item local batch takes about seven seconds even without Bridge latency.
    const batchItems=operation==='apply'&&(input.args.operation==='cleanup'||input.args.operation==='resolve_many')?input.args.items.length:0;
    const deadlineMs=batchItems>1?Math.min(60_000,5_000+2_000*batchItems):5_000;
    let response;
    try {
      response = await transport.request(body(operation,input), signal, {responseLimit: ['propose_relation','propose_identifier','propose_fact','fact_add','propose_cleanup','cleanup','resolve_many'].includes(input.args.operation)?65536:8192, deadlineMs: deadlineMs, errorBodyStatuses: operation === 'apply' && input.args.operation === 'cleanup' ? [503] : []});
    } catch (error) {
      if (operation === 'apply' && input.args.operation === 'cleanup' && error instanceof OwnerToolTransportError) {
        let diagnostic: CleanupDiagnostic = {cleanup_stage: error.stage, cleanup_category: error.category,
          ...(error.httpStatus === undefined ? {} : {cleanup_http_status: error.httpStatus})};
        if (error.httpStatus === 503 && error.category === 'http') {
          const detail = error.detail(cleanupDiagnostic);
          if (detail && ['receiver_perform','receiver_serialize'].includes(detail.cleanup_stage) &&
              ['io','type','import','unavailable','other'].includes(detail.cleanup_category) && detail.cleanup_http_status === 503)
            diagnostic = detail;
        }
        throw new CleanupDiagnosticError(diagnostic);
      }
      throw error;
    }
    if(operation==='apply'&&input.args.operation==='cleanup'&&response.value&&typeof response.value==='object'){
      const {version,status,...fields}=response.value as Record<string,unknown>;
      const diagnostic=cleanupDiagnostic(fields);
      if(version===1&&status==='unknown'&&diagnostic?.cleanup_stage==='writer'&&['io','edit','unavailable'].includes(diagnostic.cleanup_category)&&diagnostic.cleanup_http_status===undefined)
        throw new CleanupDiagnosticError({...diagnostic,cleanup_http_status:response.status});
    }
    let value:OwnerTaskToolReply;
    try{value=schema.parse(response.value);}catch(error){
      if(operation==='apply'&&input.args.operation==='cleanup')throw new CleanupDiagnosticError({cleanup_stage:'response',cleanup_category:'invalid_response',cleanup_http_status:response.status});
      throw error;
    }
    if(operation==='apply'&&input.args.operation==='cleanup'&&value.status==='unknown')throw new CleanupDiagnosticError({cleanup_stage:'response',cleanup_category:'unavailable',cleanup_http_status:response.status});
    if ((response.status === 409) !== (value.status === 'rejected') ||
          (operation === 'preview' && !['preview','rejected','unknown'].includes(value.status)) ||
          (operation === 'apply' && !['committed', 'rejected', 'unknown'].includes(value.status)) ||
          (operation === 'inspect' && !['committed', 'not_found', 'rejected', 'unknown', 'preview'].includes(value.status)) ||
          (value.status==='preview'&&(value.event_id!==input.eventId||value.payload_hash!==input.payloadHash||!taskPreviewMatches(value,input.args))) ||
          (value.status === 'committed' && (value.action_id !== input.actionId || value.event_id !== input.eventId ||
            value.payload_hash !== input.payloadHash || value.args_hash !== binding.payloadHash ||
            !taskReceiptMatches(value, input.args)))) {
      if(operation==='apply'&&input.args.operation==='cleanup')throw new CleanupDiagnosticError({cleanup_stage:'response',cleanup_category:'invalid_response',cleanup_http_status:response.status});
      throw new Error();
    }
    return value;
  }
  async function readLoops(sourceRevision:string,signal?:AbortSignal,includeParked=false) {
    if(!options.readBearer || !options.sourceAuthority)throw new Error('Correction readback unavailable');
    const before=await readAuthority(options.sourceAuthority,signal);
    if(!before.complete || before.revision!==sourceRevision || signal?.aborted)throw new Error('Correction readback unavailable');
    const result=await readOwnerContinuityJson({...options,readBearer:options.readBearer},'knowledge-read',
      {operation:'loops_query',arguments:{argv:includeParked?['--parked']:[]},source_revision:sourceRevision},signal);
    const after=await readAuthority(options.sourceAuthority,signal);
    const row=ownerLoopEntrySchema;
    const parsed=z.object({coverage:z.literal('currently_permitted_canonical_loops'),you_owe:z.array(row).max(1024),waiting_on_them:z.array(row).max(1024),
      parked:z.array(row).max(1024).optional(),counts:z.object({you_owe:z.number().int().nonnegative(),waiting_on_them:z.number().int().nonnegative(),parked:z.number().int().nonnegative().optional()}).strict()}).strict().parse(result);
    if(!sameConsent(before,after) || signal?.aborted || parsed.counts.you_owe!==parsed.you_owe.length || parsed.counts.waiting_on_them!==parsed.waiting_on_them.length || (includeParked ? !parsed.parked || parsed.counts.parked!==parsed.parked.length : parsed.parked!==undefined || parsed.counts.parked!==undefined))throw new Error('Correction readback unavailable');
    return parsed;
  }
  async function readback(args:OwnerTaskToolArgs,receipt:import('./owner-task-tool-contract.js').OwnerTaskToolReceipt,signal?:AbortSignal) {
    if(args.operation!=='cleanup' && args.operation!=='resolve_many' && args.operation!=='add' && args.operation!=='correct' && args.operation!=='retarget' && args.operation!=='snooze' && args.operation!=='keep')throw new Error('Correction readback unavailable');
    if(!options.sourceAuthority)throw new Error('Correction readback unavailable');
    const authority=await readAuthority(options.sourceAuthority,signal);
    if(!authority.complete || !authority.revision)throw new Error('Correction readback unavailable');
    const result=await readLoops(authority.revision,signal,args.operation==='keep'||args.operation==='cleanup');
    const all=[...result.you_owe,...result.waiting_on_them];
    if(args.operation==='cleanup'){if(receipt.operation!=='cleanup')throw new Error('Cleanup readback unavailable');for(const item of receipt.items){if(item.status!=='applied')continue;const matches=[...all,...(result.parked??[])].filter(row=>row.anchor_key===item.anchor_key);if(item.action==='keep'){if(matches.length!==1||matches[0]!.loop_identity!==args.list.rows[item.index-1]!.loop_identity)throw new Error('Cleanup readback unavailable');}else if(all.some(row=>row.anchor_key===item.anchor_key))throw new Error('Cleanup readback unavailable');}}
    else if(args.operation==='resolve_many'){if(receipt.operation!=='resolve_many')throw new Error('Resolve readback unavailable');for(const item of receipt.items)if(item.status==='applied'&&all.some(row=>row.anchor_key===item.anchor_key))throw new Error('Resolve readback unavailable');}
    else if(args.operation==='add'){if(receipt.operation!=='add'||args.direction===undefined)throw new Error('Task readback unavailable');const lane=args.direction==='you'?result.you_owe:result.waiting_on_them;if(all.filter(row=>row.anchor_key===receipt.anchor_key).length!==1||!lane.some(row=>row.anchor_key===receipt.anchor_key))throw new Error('Task readback unavailable');}
    else if(args.operation==='keep'){const matches=[...all,...(result.parked??[])].filter(row=>row.anchor_key===args.anchor);if(matches.length!==1||matches[0]!.loop_identity!==args.loop_identity)throw new Error('Correction readback unavailable');}
    else if(all.some(row=>row.anchor_key===args.anchor))throw new Error('Correction readback unavailable');
    if(args.operation==='retarget') {
      if(receipt.operation!=='retarget')throw new Error('Correction readback unavailable');
      const lane=args.direction==='you'?result.you_owe:result.waiting_on_them;
      if(all.filter(row=>row.anchor_key===receipt.anchor_key).length!==1 || !lane.some(row=>row.anchor_key===receipt.anchor_key))throw new Error('Correction readback unavailable');
    }
    return {...result.counts,source_revision:authority.revision,...(args.operation==='resolve_many'?{remaining:all}: {})};
  }
  const readOwnerTasks=(signal?:AbortSignal)=>{if(!options.readBearer)throw new Error('Owner task selection unavailable');return readOwnerLoops({...options,readBearer:options.readBearer},{include_parked:false,signal});};
  return {identity: transport.identity, scopeHash: transport.scopeHash, call,readLoops,readOwnerTasks,readback,preflight};
}
export type OwnerTaskToolClient = ReturnType<typeof createOwnerTaskToolClient>;

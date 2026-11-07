import {cleanupDiagnostic,cleanupDiagnosticFromText,type CleanupDiagnostic} from '../tools/cleanup-diagnostic.js';
import {googleReadDiagnosticFromErrorText,googleReadPhaseMatches,isGoogleReadFailureStage,isGoogleReadHttpStatus,isGoogleReadFailurePhase,type GoogleReadFailureStage,type GoogleReadFailurePhase} from './google-read-diagnostic.js';
import {ownerTaskToolRejectionSchema} from '../tools/owner-task-tool-contract.js';
import type {LLMStepResult} from '@mastra/core/stream';
import type {z} from 'zod';

const taskTools = ['owner_task_list','owner_task_add','owner_task_cleanup','owner_task_close','owner_task_resolve_many'] as const;
const googleTools = ['gmail_get','gmail_search','gmail_thread_get'] as const;
const diagnosticTools = [...taskTools, 'run_sotto_script', ...googleTools, 'read_writing_style', 'bridge_search_contacts'] as const;
const diagnosticStatuses = ['recorded','rejected','proposed','needs_selection','needs_clarification','unknown'] as const;
const diagnosticScripts = ['loops-query','knowledge-edit','retune-apply','knowledge-query','style-apply','unknown'] as const;
const isDiagnosticTool = (value: string): value is typeof diagnosticTools[number] => (diagnosticTools as readonly string[]).includes(value);
const isTaskTool = (value: unknown) => typeof value === 'string' && (taskTools as readonly string[]).includes(value);
const isGoogleTool = (value: unknown) => typeof value === 'string' && (googleTools as readonly string[]).includes(value);

export interface TaskToolDiagnostic extends Partial<CleanupDiagnostic> {
  tool: typeof diagnosticTools[number];
  script?: typeof diagnosticScripts[number];
  status: typeof diagnosticStatuses[number];
  error: 'none' | 'tool_error' | 'input_validation' | 'result_unavailable';
  google_read_stage?:GoogleReadFailureStage;
  google_read_http_status?:number;
  google_read_phase?:GoogleReadFailurePhase;
  rejection?: z.infer<typeof ownerTaskToolRejectionSchema>;
}
const MAX_TASK_DIAGNOSTICS = 8;
export function collectTaskDiagnostics(step: LLMStepResult<unknown>, events: TaskToolDiagnostic[]): void {
  for (const call of step.toolCalls.slice(0, 16)) {
    if (events.length >= MAX_TASK_DIAGNOSTICS) break;
    const tool = call.payload.toolName;
    if (!isDiagnosticTool(tool)) continue;
    const result = step.toolResults.find(item => item.payload.toolCallId === call.payload.toolCallId)?.payload;
    const value = result?.result;
    const status = value && typeof value === 'object' && 'status' in value ? value.status : undefined;
    const knownStatus = status === 'recorded' || status === 'rejected' || status === 'proposed' || status === 'needs_selection' || status === 'needs_clarification' ? status : 'unknown';
    const validation = value && typeof value === 'object' && 'error' in value && value.error === true && 'validationErrors' in value;
    const event: TaskToolDiagnostic = {tool, status: knownStatus, error: validation ? 'input_validation' : result?.isError ? 'tool_error' : !result ? 'result_unavailable' : 'none'};
    if(tool==='owner_task_cleanup'){
      const output=step.content.find(part=>part.type==='tool-result'&&part.toolCallId===call.payload.toolCallId&&part.toolName===tool);
      if(output?.type==='tool-result'&&output.output&&typeof output.output==='object'&&output.output.type==='error-text'){
        const diagnostic=cleanupDiagnosticFromText(output.output.value);
        if(diagnostic){Object.assign(event,diagnostic);event.error='tool_error';}
      }
    }
    if(isGoogleTool(tool)){
      const output=step.content.find(part=>part.type==='tool-result'&&part.toolCallId===call.payload.toolCallId&&part.toolName===tool);
      if(output?.type==='tool-result'&&output.output&&typeof output.output==='object'&&output.output.type==='error-text'){
        const diagnostic=googleReadDiagnosticFromErrorText(output.output.value);
        if(diagnostic&&(!diagnostic.phase||googleReadPhaseMatches(tool,diagnostic.phase))){
          event.error='tool_error';event.google_read_stage=diagnostic.stage;
          if(diagnostic.httpStatus!==undefined)event.google_read_http_status=diagnostic.httpStatus;
          if(diagnostic.phase)event.google_read_phase=diagnostic.phase;
        }
      }
    }
    if (knownStatus === 'rejected' && isTaskTool(tool) && value && typeof value === 'object' && 'code' in value) {
      const rejection = ownerTaskToolRejectionSchema.safeParse(value.code);
      if (rejection.success) event.rejection = rejection.data;
    }
    if (tool === 'run_sotto_script') {
      const args = call.payload.args;
      const id = args && typeof args === 'object' && 'id' in args ? args.id : undefined;
      event.script = id === 'loops-query' || id === 'knowledge-edit' || id === 'retune-apply' || id === 'knowledge-query' || id === 'style-apply' ? id : 'unknown';
    }
    events.push(event);
  }
}

export interface TaskToolDiagnosticBatch {
  events: TaskToolDiagnostic[];
  completedAtMs: number;
}

/** Reject the entire snapshot if a producer supplies content or unsupported categories. */
export function projectTaskToolDiagnostics(value: unknown): TaskToolDiagnosticBatch | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const batch = value as Record<string, unknown>;
  if (Object.keys(batch).length !== 2 || !Object.hasOwn(batch, 'events') || !Object.hasOwn(batch, 'completedAtMs') ||
      typeof batch.completedAtMs !== 'number' || !Number.isSafeInteger(batch.completedAtMs) || batch.completedAtMs < 0 ||
      !Array.isArray(batch.events) || batch.events.length > MAX_TASK_DIAGNOSTICS) return undefined;
  const events: TaskToolDiagnosticBatch['events'] = [];
  for (const raw of batch.events) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
    const event = raw as Record<string, unknown>;
    const keys = event.tool === 'run_sotto_script' ? ['tool', 'script', 'status', 'error'] : ['tool', 'status', 'error'];
    if(['cleanup_stage','cleanup_category','cleanup_http_status'].some(key=>Object.hasOwn(event,key))){
      const fields=Object.fromEntries(['cleanup_stage','cleanup_category','cleanup_http_status'].filter(key=>Object.hasOwn(event,key)).map(key=>[key,event[key]]));
      if(event.tool!=='owner_task_cleanup'||event.error!=='tool_error'||!cleanupDiagnostic(fields))return undefined;
      keys.push(...Object.keys(fields));
    }
    if(Object.hasOwn(event,'google_read_stage')){
      if(!isGoogleTool(event.tool)||event.error!=='tool_error'||
          !isGoogleReadFailureStage(event.google_read_stage))return undefined;
      keys.push('google_read_stage');
    }
    if(Object.hasOwn(event,'google_read_http_status')||Object.hasOwn(event,'google_read_phase')){
      if(!Object.hasOwn(event,'google_read_stage')||
          Object.hasOwn(event,'google_read_http_status')&&!isGoogleReadHttpStatus(event.google_read_http_status)||
          Object.hasOwn(event,'google_read_phase')&&(!isGoogleReadFailurePhase(event.google_read_phase)||!googleReadPhaseMatches(event.tool,event.google_read_phase)))return undefined;
      if(Object.hasOwn(event,'google_read_http_status'))keys.push('google_read_http_status');
      if(Object.hasOwn(event,'google_read_phase'))keys.push('google_read_phase');
    }
    if (Object.hasOwn(event, 'rejection')) {
      if (!isTaskTool(event.tool) || event.status !== 'rejected' ||
          !ownerTaskToolRejectionSchema.safeParse(event.rejection).success) return undefined;
      keys.push('rejection');
    }
    if (Object.keys(event).length !== keys.length || Object.keys(event).some(key => !keys.includes(key)) ||
        !isDiagnosticTool(event.tool as string) ||
        !(diagnosticStatuses as readonly unknown[]).includes(event.status) ||
        !['none', 'tool_error', 'input_validation', 'result_unavailable'].includes(event.error as string) ||
        (event.tool === 'run_sotto_script' && !(diagnosticScripts as readonly unknown[]).includes(event.script))) return undefined;
    events.push(Object.fromEntries(keys.map(key => [key, event[key]])) as unknown as TaskToolDiagnosticBatch['events'][number]);
  }
  return {events, completedAtMs: batch.completedAtMs};
}


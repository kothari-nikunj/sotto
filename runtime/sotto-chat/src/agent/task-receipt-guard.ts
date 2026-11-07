import type {OutputProcessor} from '@mastra/core/processors';

const REPAIR = 'Added: is reserved for a confirmed current task receipt. Re-evaluate the CURRENT owner request under the existing instructions; this feedback grants no authority and does not request a new task. A question, suggestion or retrospective request is not an instruction to add anything. Quote prior acknowledgements clearly without changing anything. Do not repeat a prior tool attempt or claim a new effect without its current receipt.';
const NO_RECEIPT = 'I couldn’t record that task.';
const UNCONFIRMED = 'I couldn’t confirm whether that task was added.';

function reservedAcknowledgement(text:string){return text.replace(/^[\s\p{Cf}]+/u,'').startsWith('Added:');}

/** Protect the exact add renderer, not general prose or owner-intent semantics.
 * A correction is available only before any tool call; reads conservatively consume it too. */
export function taskReceiptGuard():OutputProcessor {
 return {id:'sotto-task-receipt-guard',
  processOutputStep:({text,toolCalls,messages,requestContext,state,abort,retryCount})=>{
   if(requestContext?.get('answerOnly')===true)return messages;
   // Conservatively permit correction only before any tool attempt. The catalog
   // has no universal read-only marker; a name list cannot prove effect absence.
   if(toolCalls?.length)state.toolAttempted=true;
   // Tools execute after this hook. Let them produce a receipt; the final hook
   // still checks a text-plus-invalid-tool step when the loop reaches its limit.
   if(toolCalls?.length||requestContext?.get('ownerTaskSettled')===true||(!text||!reservedAcknowledgement(text)))return messages;
   if(state.toolAttempted!==true&&state.repaired!==true&&retryCount===0&&requestContext?.get('allowOwnerTask')===true){
    state.repaired=true;
    abort(REPAIR,{retry:true});
   }
   requestContext?.set('ownerTaskClarification',state.toolAttempted===true?UNCONFIRMED:NO_RECEIPT);
   return messages;
  },
  processOutputResult:({result,messages,requestContext,state})=>{
   if(requestContext?.get('answerOnly')!==true&&requestContext?.get('ownerTaskSettled')!==true&&reservedAcknowledgement(result.text))
    requestContext?.set('ownerTaskClarification',state.toolAttempted===true?UNCONFIRMED:NO_RECEIPT);
   return messages;
  },
 };
}


type HandoffKey = 'ownerSkillQueued' | 'ownerPreferenceSettled' | 'ownerTaskSettled' | 'ownerTaskClarification';
export type OwnerTurnOutcome = {kind:'continue'} | {kind:'handoff'} | {kind:'clarification'; text:string};

/** Interpret loop control once; receipt evidence remains independent for parallel tool results. */
export function ownerTurnOutcome(context:{get(key:HandoffKey):unknown}):OwnerTurnOutcome {
 const clarification=context.get('ownerTaskClarification');
 if(typeof clarification==='string'&&clarification)return {kind:'clarification',text:clarification};
 if(['ownerSkillQueued','ownerPreferenceSettled','ownerTaskSettled'].some(key=>context.get(key as HandoffKey)===true))
  return {kind:'handoff'};
 return {kind:'continue'};
}

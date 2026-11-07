import {ownerRetuneRowSchema,reminderHandoff} from '../agent/owner-retune.js';
import {stableActionIdentity} from './action-gate.js';
import {createHash} from 'node:crypto';
import { z } from 'zod';
export const ownerTaskToolKind = 'owner_loop_tool';
const text = z.string().min(1).refine(value => value.trim() === value && Array.from(value).length <= 500 &&
  !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const anchor = z.string().min(1).refine(value => value.trim() === value && Array.from(value).length <= 256 &&
  !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const hex = z.string().regex(/^[a-f0-9]{64}$/);
export const ownerTaskAnchorKeysSchema=z.array(anchor).min(1).max(50).refine(keys=>new Set(keys).size===keys.length&&keys.every(key=>!/[\u0080-\u009f]/u.test(key)));
export {OwnerTaskListUnavailable} from './owner-task-scalars.js';
export const OWNER_TASK_SELECTION_CLARIFICATION='I couldn’t safely match those choices to a current task list. I can show you a fresh numbered list.';
const canonicalId = z.string().regex(/^c_[a-f0-9]{6,64}$/);
const factId = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/);
const factText = z.string().min(1).max(1000).refine(value => value.trim() === value && Array.from(value).length <= 500 &&
  !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(value));
const correction = z.string().min(1).max(16000).refine(value => value.trim() === value && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(value));
const binding = {anchor, loop_version:hex, loop_identity:hex, source_revision:hex, correction};
export const cleanupItemSchema=z.object({index:z.number().int().min(1).max(50),action:z.enum(['dismiss','snooze','keep','resolve']),correction,days:z.number().int().positive().max(3652058).optional()}).strict().refine(value=>(value.action==='snooze')===(value.days!==undefined));
export const cleanupItemsSchema=z.array(cleanupItemSchema).min(1).max(50).refine(items=>new Set(items.map(item=>item.index)).size===items.length);
// The model selects owner spans; indices are expanded only against the saved delivered list.
const numberedReferenceSchema=z.object({first:z.number().int().min(1).max(50),last:z.number().int().min(1).max(50),correction}).strict();
const numberedReferencesSchema=z.array(numberedReferenceSchema).min(1).max(50);
export const cleanupSelectionSchema=z.discriminatedUnion('mode',[
 z.object({mode:z.literal('all'),correction,reference:correction}).strict(),
 z.object({mode:z.literal('singleton'),correction,reference:correction}).strict(),
 z.object({mode:z.literal('all_except'),correction,excluded:numberedReferencesSchema}).strict(),
 z.object({mode:z.literal('explicit'),correction,clauses:z.array(z.object({action:z.enum(['resolve','leave_open']),correction,references:numberedReferencesSchema}).strict()).min(1).max(50)}).strict(),
]);
export const cleanupInputSchema=z.object({items:cleanupItemsSchema.optional(),selection:cleanupSelectionSchema.optional()}).strict().refine(value=>(value.items===undefined)!==(value.selection===undefined));
const resolveItemSchema=z.object({name:scalarName(),anchor_key:anchor.optional(),loop_version:hex.optional(),loop_identity:hex.optional(),correction}).strict().refine(v=>[v.anchor_key,v.loop_version,v.loop_identity].every(x=>x===undefined)||[v.anchor_key,v.loop_version,v.loop_identity].every(x=>x!==undefined));
import {scalarName,explicitProfileFields,numberedReferenceIndices,OwnerTaskListUnavailable} from './owner-task-scalars.js';
export const resolveManyItemsSchema=z.array(resolveItemSchema).min(1).max(20).refine(items=>
  new Set(items.filter(item=>item.anchor_key).map(item=>item.anchor_key)).size===items.filter(item=>item.anchor_key).length && new Set(items.map(item=>item.name.toLocaleLowerCase('en-US'))).size===items.length);
const withheldReversal=/\b(?:not|never|neither|don't|don’t|do not|actually|kidding|unsure|wait|undo|cancel|scratch|them|both|it|those|these|all|everything|anything)\b/i;
export function ownerTaskReadback(text:string){
  const value=text.trim();
  const match=/(?:[.!]\s*|\s+[—–-]\s*)(?:what(?:(?:'s|’s| is)? left|(?:'s|’s)?(?: else| still)?(?: is)? open| remains)|any((?: [A-Za-z0-9_-]+){0,6}) tasks? left|anything else open|what else)\?$/i.exec(value);
  return match?{claim:value.slice(0,match.index).trim(),scope:match[1]?.trim()??''}:undefined;
}
const connectors='(?:\\s|,|&|\\band\\b)*';
const completionPatterns = [
  new RegExp(`^(?:I|we|I've|we've|I have|we have)?\\s*(?:finished|completed|closed|resolved|sorted)(?:\\s+with)?\\s*${connectors}$`,'i'),
  new RegExp(`^${connectors}(?:loops?\\s+)?(?:(?:are|were|is|have been)\\s+)?(?:both\\s+)?(?:closed|done|handled|finished|complete|completed|resolved|sorted)(?:\\s+(?:too|now))?$`,'i'),
  new RegExp(`^(?:please\\s+)?(?:close|resolve|complete)\\s*${connectors}$`,'i'),
  new RegExp(`^(?:please\\s+)?mark\\s+${connectors}(?:as\\s+)?(?:done|complete|completed|closed|resolved)$`,'i')
];
export function resolveManyOwnerTextMatches(items:ReadonlyArray<Pick<z.infer<typeof resolveManyItemsSchema>[number],'name'|'correction'>>,ownerText:string,mode:'named_people'|'owner_tasks',exactClause=false):boolean{
  void mode;
  let claim=exactClause?ownerText.trim():ownerTaskReadback(ownerText)?.claim??ownerText.trim();
  claim=exactClause?claim:claim.replace(/[.!]\s*(?:you can|please) close (?:them|those|these)[.!]?$/i,'').trim();
  // A counted restatement is redundant only when it names this same complete set.
  const restatement=exactClause?null:/[.!]\s*(?:please\s+)?(?:close|resolve|mark)\s+(?:those|these|the)\s+(two|three|[2-9]|1[0-9]|20)(?:\s+(?:done|closed))?[.!]?$/i.exec(claim);
  if(restatement){const count=restatement[1]!.toLowerCase()==='two'?2:restatement[1]!.toLowerCase()==='three'?3:Number(restatement[1]);if(count!==items.length)return false;claim=claim.slice(0,restatement.index).trim();}
  const withheld=exactClause?null:/(?:[;,]\s*|\bbut\s+|(?<=\bdone)\s+)(?:leave\s+)?(.+?)\s+(?:still\s+)?open[.!]?$/i.exec(claim);
  // A carve-out names a specific remaining item; a hedge, reversal or blanket pronoun withdraws the claim.
  if(withheld&&withheldReversal.test(withheld[1]!))return false;
  if(withheld)claim=claim.slice(0,withheld.index).trim();
  claim=claim.replace(/[.!]$/,'').trim();
  if(!claim||/[?]/.test(claim))return false;
  // Independent explicit completion clauses remain independently selectable.
  const clauses=claim.split(/[.;]\s+/);
  if(clauses.length>1){
    const groups=clauses.map(clause=>items.filter(item=>clause.includes(item.correction)));
    return groups.flat().length===items.length&&new Set(groups.flat()).size===items.length&&groups.every((group,i)=>group.length>0&&resolveManyOwnerTextMatches(group,clauses[i]!,mode,exactClause));
  }

  const spans:{start:number;end:number}[]=[];
  for(const item of items){
    const name=item.name.toLocaleLowerCase('en-US');
    const escaped=item.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const found=[...claim.matchAll(new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`,'giu'))];
    const index=found[0]?.index??-1;
    if(found.length!==1||item.correction!==found[0]![0]||
      (withheld?.[1]??'').toLocaleLowerCase('en-US').includes(name))return false;
    spans.push({start:index,end:index+found[0]![0].length});
  }
  if(spans.some((span,i)=>spans.some((other,j)=>i!==j&&span.start<other.end&&other.start<span.end)))return false;
  let remainder=claim;
  for(const span of spans.sort((a,b)=>b.start-a.start))remainder=remainder.slice(0,span.start)+' '+remainder.slice(span.end);
  if(/\b(?:not|never|neither|don't|do not|isn't|aren't|might|maybe|perhaps|if|unless|should|could|would|open|pending|waiting|unfinished)\b/i.test(remainder))return false;
  if(/\bboth\b/i.test(remainder)&&items.length!==2)return false;
  return completionPatterns.some(pattern => pattern.test(remainder.trim()));
}

/** Normalize labels only; uniqueness is still checked over every active peer. */
export function loopContactName(value:string){return value.toLocaleLowerCase('en-US').replace(/\s+(?:(?:text|chat)\s+)?thread$/u,'').match(/[\p{L}\p{N}]+/gu)?.join(' ')??'';}

/** An explicit thread label must match its normalized full label, never another person's first name. */
export function explicitLoopThreadSelection(phrase:string){
 const person=/^(.+?)[’']s\s+(.+)$/.exec(phrase)?.[1]??phrase;
 return /\s+(?:(?:text|chat)\s+)?thread$/iu.test(person);
}

/** Possessive descriptions select one task among exact matching contact names; never merge identities. */
export function namedLoopSelection(phrase:string): {person:string;words:string[]} {
  const match=/^(.+?)[’']s\s+(.+)$/.exec(phrase);
  const words=(match?.[2].toLocaleLowerCase('en-US').match(/[\p{L}\p{N}]+/gu)??[])
    .map(word=>word.length>3&&word.endsWith('s')?word.slice(0,-1):word).filter(word=>!['the','a','an','plan','task','loop'].includes(word));
  return {person:loopContactName(match?.[1]??phrase),words:match?words:[]};
}
export function namedLoopDescriptionMatches(phrase:string,what:string,ask=''):boolean {
  const selection=namedLoopSelection(phrase);
  if(!/[’']s\s/.test(phrase))return true;
  const evidence=new Set(((what+' '+ask).toLocaleLowerCase('en-US').match(/[\p{L}\p{N}]+/gu)??[])
    .map(word=>word.length>3&&word.endsWith('s')?word.slice(0,-1):word));
  return selection.words.length>0&&selection.words.every(word=>evidence.has(word));
}

export function ownerTaskTitleMatches(title:string,phrase:string){
  const text=title.toLocaleLowerCase('en-US'),name=phrase.toLocaleLowerCase('en-US'),at=text.indexOf(name);
  return at>=0&&!/[\p{L}\p{N}]/u.test(text[at-1]??'')&&!/[\p{L}\p{N}]/u.test(text[at+name.length]??'');
}

const ordinals=['first','second','third','fourth','fifth','sixth','seventh','eighth','ninth','tenth','eleventh','twelfth','thirteenth','fourteenth','fifteenth','sixteenth','seventeenth','eighteenth','nineteenth','twentieth'];
export function cleanupIntentMatches(item:z.infer<typeof cleanupItemSchema>,ownerText:string){
 if(item.action==='resolve')return /^#?[1-9][0-9]*$/.test(item.correction)&&Number(item.correction.replace('#',''))===item.index&&ownerText.includes(item.correction);
 if(!ownerText.includes(item.correction))return false;
 const match=/^(dismiss|keep|snooze)[ \t\r\n]+(?:the[ \t\r\n]+)?(?:item[ \t\r\n]+)?(#[1-9][0-9]*|[1-9][0-9]*|[a-z]+)(?:[ \t\r\n]+(?:for[ \t\r\n]+)?([1-9][0-9]*)[ \t\r\n]+days?)?\.?$/i.exec(item.correction);
 if(!match)return false;const index=/^#?[0-9]+$/.test(match[2]!)?Number(match[2]!.replace('#','')):ordinals.indexOf(match[2]!.toLowerCase())+1;
 return match[1]!.toLowerCase()===item.action&&index===item.index&&(item.action==='snooze'?Number(match[3])===item.days:match[3]===undefined);
}
function numericCompletionMatches(items:z.infer<typeof cleanupItemsSchema>,ownerText:string){
 // Numeric tokens must be exact references, not fragments of dates, versions or titles.
 const refs=[...ownerText.matchAll(/(?<![\p{L}\p{N}_#:/.-])#?[1-9][0-9]*(?![\p{L}\p{N}_:/-]|\.[0-9])/gu)];
 if(refs.length!==items.length||refs.some(ref=>ref[0].replace('#','').length>2)||new Set(items.map(item=>item.index)).size!==items.length)return false;
 if(refs.some(ref=>!items.some(item=>item.correction===ref[0]&&item.index===Number(ref[0].replace('#','')))))return false;
 // Only this existing redundant close restatement is normalized; validate the whole remaining request.
 const claim=ownerText.replace(/[.!]\s*(?:(?:you can|please)\s+)?close (?:them|those|these)[.!]?$/i,'').trim();
 return resolveManyOwnerTextMatches(items.map(item=>({name:item.correction,correction:item.correction})),claim,'owner_tasks');
}
export function cleanupSelectionsMatch(items:z.infer<typeof cleanupItemsSchema>,ownerText:string,allowStoredMixedResolve=false){
 if(!items.every(item=>cleanupIntentMatches(item,ownerText)))return false;
 if(items.every(item=>item.action==='resolve'))return items.length>0&&(numericCompletionMatches(items,ownerText)||
  (allowStoredMixedResolve&&resolveManyOwnerTextMatches(items.map(item=>({name:item.correction,correction:item.correction})),ownerText,'owner_tasks')));
 // Legacy resolve wording and mixed actions remain readable only during saved-action inspection.
 if(!allowStoredMixedResolve&&items.some(item=>item.action==='resolve'))return false;

 const spans=items.map(item=>({start:ownerText.indexOf(item.correction),end:ownerText.indexOf(item.correction)+item.correction.length})).sort((a,b)=>a.start-b.start);
 if(items.some(item=>ownerText.indexOf(item.correction)!==ownerText.lastIndexOf(item.correction)))return false;
 if(!/^[ \t\r\n]*$/.test(ownerText.slice(0,spans[0]!.start)))return false;
 const separators=/^(?:[ \t\r\n,;]|\band\b)*$/i;
 for(let i=1;i<spans.length;i++){if(spans[i]!.start<spans[i-1]!.end||!separators.test(ownerText.slice(spans[i-1]!.end,spans[i]!.start)))return false;}
 return separators.test(ownerText.slice(spans.at(-1)!.end).replace(/\.[ \t\r\n]*$/,''));
}
/** Bounded set selection, not a general natural-language authority parser. */
export function cleanupSelectionItems(list:CleanupList,raw:unknown,ownerText?:string):z.infer<typeof cleanupItemsSchema>{
 const selection=cleanupSelectionSchema.parse(raw);
 if(/[\u0085\ufeff]/u.test(selection.correction))throw new OwnerTaskListUnavailable('Ambiguous selection whitespace');
 if(ownerText!==undefined&&selection.correction!==ownerText.trim())throw new OwnerTaskListUnavailable('Exact whole owner selection required');
 let selected:number[];
 if(selection.mode==='all'){
  const displayedReference=/^all of (?:them|this|that|these|those)$/iu.test(selection.reference);
  const displayedScope=list.scope==='open'&&(list.anchor_keys
   ?JSON.stringify(list.anchor_keys)===JSON.stringify(list.rows.map(row=>row.anchor_key))
   :list.remaining===0||list.rows.length>0&&displayedReference);
  if(!displayedScope)throw new OwnerTaskListUnavailable('Complete displayed open list required');
  if((!/^all$/iu.test(selection.reference)&&!displayedReference)||!resolveManyOwnerTextMatches([{name:selection.reference,correction:selection.reference}],selection.correction,'owner_tasks',true))throw new OwnerTaskListUnavailable('Explicit all completion required');
  selected=list.rows.map(row=>row.index);
 }else if(selection.mode==='singleton'){
  if(list.scope!=='open'||list.rows.length!==1||(list.anchor_keys?JSON.stringify(list.anchor_keys)!==JSON.stringify(list.rows.map(row=>row.anchor_key)):list.remaining!==0))throw new OwnerTaskListUnavailable('One complete displayed task required');
  if(!/^[\x00-\x7f]+$/u.test(selection.reference)||!/^(?:it|(?:this|that)(?: one| task)?|the (?:remaining|last) (?:one|task))$/iu.test(selection.reference)||!resolveManyOwnerTextMatches([{name:selection.reference,correction:selection.reference}],selection.correction,'owner_tasks',true))throw new OwnerTaskListUnavailable('Explicit singleton completion required');
  selected=[list.rows[0]!.index];
 }else if(selection.mode==='all_except'){
  if(list.scope!=='open'||(list.anchor_keys?JSON.stringify(list.anchor_keys)!==JSON.stringify(list.rows.map(row=>row.anchor_key)):list.remaining!==0))throw new OwnerTaskListUnavailable('Complete displayed open list required');
  const parts=selection.correction.split(/(?<![\p{L}\p{N}_])except(?![\p{L}\p{N}_])/iu);
  if(parts.length!==2)throw new OwnerTaskListUnavailable('Explicit exception required');
  let claim=parts[0]!.trim().replace(/[.!;]$/u,'').trim();
  const excluded=numberedReferenceIndices(list.rows.length,selection.excluded,parts[1]!.trim());
  if(!/^(?:\s|,|&|\band\b)*[.!]?$/iu.test(excluded.residual)){
   // The exact exception references may precede the predicate. Remove only that
   // contiguous numeric list, then validate the remaining whole completion claim.
   if(!/^all$/iu.test(claim)||!/^(?:\s|,|&|\band\b)*$/iu.test(excluded.referenceResidual))throw new OwnerTaskListUnavailable('Exception contains other intent');
   if(/[0-9]/u.test(excluded.tail))throw new OwnerTaskListUnavailable('Unreferenced numeric choice');
   claim+=' '+excluded.tail.trim();
  }
  const all=claim.match(/(?<![\p{L}\p{N}])all(?![\p{L}\p{N}])/giu);
  if(!all||all.length!==1||!resolveManyOwnerTextMatches([{name:all[0]!,correction:all[0]!}],claim,'owner_tasks',true))throw new OwnerTaskListUnavailable('Whole completion claim unavailable');
  selected=list.rows.map(row=>row.index).filter(index=>!excluded.indices.includes(index));
 }else{
  let offset=0;const mentioned=new Set<number>();selected=[];
  for(const clause of selection.clauses){
   const start=selection.correction.indexOf(clause.correction,offset);
   if(start<0||selection.correction.indexOf(clause.correction)!==selection.correction.lastIndexOf(clause.correction)||!(/^[\s.!;]*$/u).test(selection.correction.slice(offset,start))||(offset===0&&selection.correction.slice(0,start).trim()))throw new OwnerTaskListUnavailable('Whole numbered request unavailable');
   offset=start+clause.correction.length;
   const refs=numberedReferenceIndices(list.rows.length,clause.references,clause.correction);
   for(const index of refs.indices){if(mentioned.has(index))throw new OwnerTaskListUnavailable('Conflicting numbered choices');mentioned.add(index);}
   if(clause.action==='resolve'){
    if(/(?<![\p{L}\p{N}_#:/.-])#?[1-9][0-9]*(?![\p{L}\p{N}_:/-]|\.[0-9])/u.test(refs.residual))throw new OwnerTaskListUnavailable('Unreferenced numeric choice');
    const complete=list.anchor_keys?JSON.stringify(list.anchor_keys)===JSON.stringify(list.rows.map(row=>row.anchor_key)):list.remaining===0;
    const shortAnswer=list.scope==='open'&&selection.clauses.length===1&&clause.correction===selection.correction&&(
     /^(?:\s|,|&|\band\b)*[.!]?$/iu.test(refs.residual)||complete&&refs.indices.length===list.rows.length&&/^all\s*\?(?:\s|,|&|\band\b)*[.!?]?$/iu.test(refs.residual.trim()));
    if(!shortAnswer&&!resolveManyOwnerTextMatches(clause.references.map(ref=>({name:ref.correction,correction:ref.correction})),clause.correction,'owner_tasks',true))throw new OwnerTaskListUnavailable('Whole completion clause unavailable');
    selected.push(...refs.indices);
   }else if(!/^(?:keep|leave)\s*(?:\s|,|&|\band\b)*open[.!]?$/iu.test(refs.residual.trim()))throw new OwnerTaskListUnavailable('Explicit leave-open clause required');
  }
  if(!/^[\s.!;]*$/u.test(selection.correction.slice(offset)))throw new OwnerTaskListUnavailable('Unselected owner text');
 }
 if(!selected.length)throw new OwnerTaskListUnavailable('No numbered completions selected');
 return selected.sort((a,b)=>a-b).map(index=>({index,action:'resolve' as const,correction:selection.correction}));
}
export function cleanupRequestMatches(args:{list:CleanupList;items:z.infer<typeof cleanupItemsSchema>;selection?:z.infer<typeof cleanupSelectionSchema>},ownerText:string,stored=false){
 if(!args.selection)return cleanupSelectionsMatch(args.items,ownerText,stored);
 try{return JSON.stringify(cleanupSelectionItems(args.list,args.selection,ownerText))===JSON.stringify(args.items);}catch{return false;}
}
const snoozeDate=z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(value=>{const stamp=new Date(value+'T00:00:00Z');return value.slice(0,4)!=='0000'&&Number.isFinite(stamp.getTime())&&stamp.toISOString().slice(0,10)===value;});
export const cleanupListSchema=z.object({operation:z.literal('cleanup_list'),source_revision:hex,scope:z.literal('open').optional(),anchor_keys:ownerTaskAnchorKeysSchema.optional(),rows:z.array(ownerRetuneRowSchema.extend({index:z.number().int().min(1).max(50),anchor_key:anchor,loop_version:hex,loop_identity:hex})).max(50),total:z.number().int().nonnegative().max(1024),remaining:z.number().int().nonnegative().max(1024),review_hash:hex}).strict().refine(v=>v.remaining===v.total-v.rows.length&&v.rows.every((r,i)=>r.index===i+1)&&new Set(v.rows.map(r=>r.anchor_key)).size===v.rows.length&&(!v.anchor_keys||(v.scope==='open'&&JSON.stringify(v.anchor_keys)===JSON.stringify(v.rows.map(r=>r.anchor_key)))));
export type CleanupList=z.infer<typeof cleanupListSchema>;
export function cleanupListHash(list:CleanupList){const {review_hash:_,...fields}=list;return stableActionIdentity('cleanup-list',ownerTaskToolKind,fields).payloadHash;}
const visible=(value:string)=>value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/gu,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
export function cleanupListText(list:CleanupList){if(list.scope==='open')return ['Open tasks:', '', ...list.rows.map(row=>`${row.index}. ${visible(row.name)} — ${visible(row.what)}${row.direction==='waiting_on_them'?' (waiting on them)':''}`),...(list.rows.length?[]:[list.remaining?'No tasks can be safely numbered in this list.':'No open tasks in the sources I can currently read.']),...(list.remaining?['',`${list.remaining} more tasks are not shown.`]:[]),...(list.rows.length?['','Tell me which ones are done. You can use the numbers.']:[])].join('\n');const lines=['Stale loops in your currently permitted sources:',''];for(const row of list.rows){const reasons=[];if(row.overdue)reasons.push('overdue');if(row.age_days!==null)reasons.push(`${row.age_days} days old`);if(row.times_surfaced)reasons.push(`surfaced ${row.times_surfaced} times`);if(row.chased_out)reasons.push(row.suggestion===reminderHandoff?'reminded you twice; automatic reminders have stopped':'chased twice; automatic nudging has stopped');lines.push(`${row.index}. ${visible(row.name)} — ${visible(row.what)}`,`   ${row.direction==='you_owe'?'You owe':'Waiting on them'} · ${reasons.join('; ')||'needs your decision'}`,`   ${visible(row.suggestion)}`);}if(!list.rows.length)lines.push(list.remaining?'No stale loops can be safely numbered in this list.':'No stale loops were found in this permitted view.');if(list.remaining)lines.push('',`${list.remaining} more stale loops are not shown in this list.`);if(list.rows.length)lines.push('','Tell me which numbers to dismiss, snooze, or keep. For example: dismiss 1; snooze 2 for 7 days; keep 3. Nothing has changed yet.');return lines.join('\n');}
export const cleanupPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:cleanupListSchema}).strict();
const cleanupOutcomeBase={index:z.number().int().min(1).max(50),action:z.enum(['dismiss','snooze','keep','resolve']),anchor_key:anchor};
export const cleanupOutcomeSchema=z.discriminatedUnion('status',[
 z.object({...cleanupOutcomeBase,status:z.literal('applied'),post_digest:hex,loop_status:z.enum(['open','waiting','failed','blocked','parked','dismissed','resolved']),snoozed_until:snoozeDate.optional()}).strict().refine(v=>(v.action==='snooze')===(v.snoozed_until!==undefined)&&(v.action!=='dismiss'||v.loop_status==='dismissed')&&(v.action!=='resolve'||v.loop_status==='resolved')&&(v.action!=='snooze'||v.loop_status!=='parked'&&v.loop_status!=='dismissed'&&v.loop_status!=='resolved')&&(v.action!=='keep'||v.loop_status!=='dismissed'&&v.loop_status!=='resolved')),
 z.object({...cleanupOutcomeBase,anchor_key:z.union([anchor,z.literal('')]),status:z.literal('refused'),reason:z.enum(['target_changed','target_unavailable','source_unavailable','correction_unavailable'])}).strict(),
 z.object({...cleanupOutcomeBase,status:z.literal('unknown')}).strict(),z.object({...cleanupOutcomeBase,status:z.literal('skipped')}).strict(),
]);
const scalar = (max:number) => z.string().max(max * 2).refine(value => value.trim() === value && Array.from(value).length <= max && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const relationType = z.enum(['introduced_by','introduced','works_with','family_of','partner_of','met_through','connected']);
const relationDate = z.string().refine(value=>value==='' || (/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)&&value.slice(0,4)!=='0000'&&Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value));
const relationFields={mode:z.enum(['add','remove']),canonical_id:canonicalId,other_canonical_id:canonicalId,relation_type:relationType,date:relationDate,source_revision:hex};
export const relationReviewSchema=z.object({operation:z.literal('relation'),...relationFields,name:scalar(500).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),other_name:scalar(500).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),present:z.boolean(),relation_version:hex,review_hash:hex}).strict().refine(v=>v.canonical_id!==v.other_canonical_id&&(v.mode==='add'||v.date===''));
const relationProof=z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:hex}).strict();
export const relationPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:relationReviewSchema}).strict();
export type RelationPreview=z.infer<typeof relationPreviewSchema>;
export function relationReviewHash(review:z.infer<typeof relationReviewSchema>){const {review_hash:_,...fields}=review;return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a<b?-1:a>b?1:0)))).digest('hex');}
export function validRelationReview(review:z.infer<typeof relationReviewSchema>){return relationReviewHash(review)===review.review_hash;}
export function relationReviewText(review:z.infer<typeof relationReviewSchema>){const sentences={introduced_by:'Introduced to you by',introduced:'Introduced',works_with:'Works with',family_of:'Family of',partner_of:'Partner of',met_through:'Met through',connected:'Connected you with'};const sentence=sentences[review.relation_type]+' '+review.other_name+(review.relation_type==='introduced'?' to you':'');const lines=[(review.mode==='add'?'Add':'Remove')+' relationship:','',review.name+': '+sentence];if(review.date)lines.push('Date: '+review.date);if(review.mode==='add'&&review.present)lines.push('Already recorded; no change is needed.');if(review.mode==='remove'&&!review.present)lines.push('Already absent; no change is needed.');lines.push('',(review.mode==='add'?'Save':'Remove')+' this relationship?');return lines.join('\n');}
const identifierFields={canonical_id:canonicalId,identifier:scalar(200).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),source_revision:hex};
export const identifierReviewSchema=z.object({operation:z.literal('identifier'),...identifierFields,name:scalar(500).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),normalized_identifier:scalar(200).min(1),present:z.boolean(),review_hash:hex}).strict();
export const identifierPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:identifierReviewSchema}).strict();
const factFields={canonical_id:canonicalId,text:factText,source_revision:hex};
export const factReviewSchema=z.object({operation:z.literal('fact_add'),...factFields,name:scalar(500).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),identity_hint:scalar(200).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)),person_version:hex,present:z.boolean(),review_hash:hex}).strict();
export const factPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:factReviewSchema}).strict();
export function factReviewHash(review:z.infer<typeof factReviewSchema>){const {review_hash:_,...fields}=review;return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a<b?-1:a>b?1:0)))).digest('hex');}
export const taskPreviewSchema=z.union([relationPreviewSchema,identifierPreviewSchema,factPreviewSchema,cleanupPreviewSchema]);
export type TaskPreview=z.infer<typeof taskPreviewSchema>;
export function identifierReviewHash(review:z.infer<typeof identifierReviewSchema>){const {review_hash:_,...fields}=review;return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a<b?-1:a>b?1:0)))).digest('hex');}
export function taskReviewText(review:TaskPreview['review']){if(review.operation==='cleanup_list')return cleanupListText(review);if(review.operation==='relation')return relationReviewText(review);if(review.operation==='fact_add')return ['Save person fact:','',review.name+' ('+review.identity_hint+'): '+review.text,...(review.present?['Already recorded; no change is needed.']:[]),'','Save this fact?'].join('\n');return ['Save identifier:','',review.name+': '+review.identifier,...(review.present?['Already recorded; no change is needed.']:[]),'','Save this association?'].join('\n');}
export function taskPreviewMatches(preview:TaskPreview,args:OwnerTaskToolArgs){const review=preview.review;if(review.operation==='cleanup_list')return args.operation==='propose_cleanup'&&review.source_revision===args.source_revision&&review.scope===args.scope&&JSON.stringify(review.anchor_keys)===JSON.stringify(args.anchor_keys)&&cleanupListHash(review)===review.review_hash&&cleanupListText(review).length<=16000;if(review.operation==='relation')return args.operation==='propose_relation'&&validRelationReview(review)&&(['mode','canonical_id','other_canonical_id','relation_type','date','source_revision'] as const).every(k=>args[k]===review[k]);if(review.operation==='fact_add')return args.operation==='propose_fact'&&factReviewHash(review)===review.review_hash&&(['canonical_id','text','source_revision'] as const).every(k=>args[k]===review[k]);return args.operation==='propose_identifier'&&identifierReviewHash(review)===review.review_hash&&(['canonical_id','identifier','source_revision'] as const).every(k=>args[k]===review[k]);}
export const ownerTaskToolArgsSchema = z.discriminatedUnion('operation', [
  z.object({operation: z.literal('add'), text, direction:z.enum(['you','them']).optional(), contact:scalar(120).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)).optional(), identifier:scalar(200).min(1).refine(v=>!/[\u007f-\u009f\u2028\u2029]/u.test(v)).optional()}).strict().refine(v=>v.direction!==undefined||(v.contact===undefined&&v.identifier===undefined)),
  z.object({operation: z.literal('close'), anchor, correction:scalarName().optional()}).strict(),
  z.object({operation:z.literal('keep'),...binding}).strict(),
  z.object({operation:z.literal('snooze'),...binding,days:z.number().int().positive().max(3652058)}).strict(),
  z.object({operation:z.literal('correct'),...binding,to:z.enum(['resolved','dismissed'])}).strict(),
  z.object({operation:z.literal('retarget'),...binding,direction:z.enum(['you','them']),text:text.optional(),contact:scalar(120).optional(),identifier:scalar(200).optional()}).strict(),
  z.object({operation: z.literal('fact_correct'), canonical_id: canonicalId, fact_id: factId, fact_version: hex, text: factText, ...explicitProfileFields, source_revision: hex}).strict(),
  z.object({operation:z.literal('add_identifier'),canonical_id:canonicalId,identifier:scalar(200).min(1).refine(value=>!/[\u0085\u2028\u2029]/u.test(value)),source_revision:hex}).strict(),
  z.object({operation:z.literal('propose_relation'),...relationFields}).strict().refine(v=>v.canonical_id!==v.other_canonical_id&&(v.mode==='add'||v.date==='')),
  z.object({operation:z.literal('relation'),review:relationReviewSchema,proposal:relationProof}).strict(),
  z.object({operation:z.literal('propose_fact'),...factFields}).strict(),
  z.object({operation:z.literal('fact_add'),review:factReviewSchema,proposal:relationProof}).strict(),
  z.object({operation:z.literal('propose_identifier'),...identifierFields}).strict(),
  z.object({operation:z.literal('identifier'),review:identifierReviewSchema,proposal:relationProof}).strict(),
  z.object({operation:z.literal('propose_cleanup'),source_revision:hex,scope:z.literal('open').optional(),anchor_keys:ownerTaskAnchorKeysSchema.optional()}).strict().refine(v=>!v.anchor_keys||v.scope==='open'),
  z.object({operation:z.literal('cleanup'),list:cleanupListSchema,proposal:relationProof,items:cleanupItemsSchema,selection:cleanupSelectionSchema.optional()}).strict().refine(v=>v.items.every((item,i)=>item.index<=v.list.rows.length&&(i===0||v.items[i-1]!.index<item.index))),
  z.object({operation:z.literal('resolve_many'),source_revision:hex,selection_mode:z.enum(['named_people','owner_tasks']),allow_partial:z.literal(true).optional(),items:resolveManyItemsSchema}).strict(),
]);
export type OwnerTaskToolArgs = z.infer<typeof ownerTaskToolArgsSchema>;
export const ownerTaskToolRejectionSchema = z.enum(['event_conflict', 'target_unavailable', 'owner_span_unavailable', 'owner_anchor_collision','target_changed','source_unavailable','correction_unavailable','anchor_collision','identifier_collision','similar_fact_exists']);
/** Owner-visible wording for a fact proposal refused because it would fold into an existing fact; never claims a save. */
export const SIMILAR_FACT_TEXT='That fact was not saved: it overlaps a fact already recorded for this person. To change that fact, ask me to correct it.';
export type OwnerTaskToolRejection = z.infer<typeof ownerTaskToolRejectionSchema>;
const receiptBinding = {version:z.literal(1),status:z.literal('committed'),action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex};
export const ownerTaskToolReceiptSchema = z.discriminatedUnion('operation', [
  z.object({...receiptBinding,operation:z.literal('cleanup'),review_hash:hex,outcome:z.enum(['complete','partial']),items:z.array(cleanupOutcomeSchema).min(1).max(50)}).strict().refine(v=>(v.outcome==='complete')===v.items.every(item=>item.status==='applied')&&v.items.every((item,i)=>item.status!=='unknown'||v.items.slice(i+1).every(later=>later.status==='skipped'||later.status==='refused'))),
  z.object({...receiptBinding,operation:z.literal('resolve_many'),outcome:z.enum(['complete','partial']),items:z.array(cleanupOutcomeSchema).min(1).max(20)}).strict().refine(v=>v.items.every(item=>item.action==='resolve')&&(v.outcome==='complete')===v.items.every(item=>item.status==='applied')&&v.items.every((item,i)=>item.status!=='unknown'||v.items.slice(i+1).every(later=>later.status==='skipped'||later.status==='refused'))),
  z.object({...receiptBinding,operation:z.literal('fact_add'),canonical_id:canonicalId,fact_id:factId,added:z.boolean(),review_hash:hex,post_digest:hex}).strict(),
  z.object({...receiptBinding,operation:z.literal('identifier'),canonical_id:canonicalId,identifier:identifierFields.identifier,added:z.boolean(),review_hash:hex,post_digest:hex}).strict(),
  z.object({...receiptBinding,operation:z.literal('relation'),mode:z.enum(['add','remove']),canonical_id:canonicalId,other_canonical_id:canonicalId,relation_type:relationType,date:relationDate,review_hash:hex,changed:z.boolean(),post_digest:hex}).strict(),
  z.object({...receiptBinding,operation:z.literal('add_identifier'),canonical_id:canonicalId,identifier:scalar(200).min(1).refine(value=>!/[\u0085\u2028\u2029]/u.test(value)),added:z.boolean(),post_digest:hex}).strict(),
  z.object({...receiptBinding, operation:z.literal('fact_correct'), canonical_id:canonicalId, fact_id:factId, replacement_fact_id:factId, post_digest:hex}).strict(),
  z.object({...receiptBinding,operation:z.literal('add'),direction:z.enum(['you','them']).optional(),anchor_key:anchor,post_digest:hex,loop_status:z.enum(['open','resolved'])}).strict(),
  z.object({...receiptBinding,operation:z.literal('close'),anchor_key:anchor,post_digest:hex,loop_status:z.literal('resolved')}).strict(),
  z.object({...receiptBinding,operation:z.literal('keep'),anchor_key:anchor,post_digest:hex,loop_status:z.enum(['open','waiting','failed','blocked','parked'])}).strict(),
  z.object({...receiptBinding,operation:z.literal('snooze'),anchor_key:anchor,post_digest:hex,loop_status:z.enum(['open','waiting','failed','blocked']),snoozed_until:z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(value=>{const stamp=new Date(value+'T00:00:00Z');return value.slice(0,4)!=='0000' && Number.isFinite(stamp.getTime()) && stamp.toISOString().slice(0,10)===value;})}).strict(),
  z.object({...receiptBinding,operation:z.literal('correct'),anchor_key:anchor,post_digest:hex,loop_status:z.enum(['resolved','dismissed'])}).strict(),
  z.object({...receiptBinding,operation:z.literal('retarget'),old_anchor_key:anchor,anchor_key:anchor,old_post_digest:hex,post_digest:hex,loop_status:z.literal('open'),direction:z.enum(['you','them'])}).strict(),
]);
export type OwnerTaskToolReceipt = z.infer<typeof ownerTaskToolReceiptSchema>;
export function taskReceiptMatches(receipt: OwnerTaskToolReceipt, args: OwnerTaskToolArgs): boolean {
  if(receipt.operation !== args.operation)return false;
  if(args.operation==='cleanup')return receipt.operation==='cleanup'&&receipt.review_hash===args.list.review_hash&&receipt.items.length===args.items.length&&receipt.items.every((item,i)=>item.index===args.items[i]!.index&&item.action===args.items[i]!.action&&item.anchor_key===args.list.rows[item.index-1]!.anchor_key);
  if(args.operation==='resolve_many')return receipt.operation==='resolve_many'&&receipt.items.length===args.items.length&&receipt.items.every((item,i)=>item.index===i+1&&item.action==='resolve'&&item.anchor_key===(args.items[i]!.anchor_key??'')&& (args.items[i]!.anchor_key!==undefined||item.status==='refused'));
  if(args.operation==='fact_add')return receipt.operation==='fact_add'&&receipt.canonical_id===args.review.canonical_id&&receipt.review_hash===args.review.review_hash&&receipt.added===!args.review.present;
  if(args.operation==='identifier')return receipt.operation==='identifier'&&receipt.canonical_id===args.review.canonical_id&&receipt.identifier===args.review.identifier&&receipt.review_hash===args.review.review_hash&&receipt.added===!args.review.present;
  if(args.operation==='relation')return receipt.operation==='relation'&&receipt.changed===(args.review.present!==(args.review.mode==='add'))&&(['mode','canonical_id','other_canonical_id','relation_type','date','review_hash'] as const).every(key=>receipt[key]===args.review[key]);
  if(args.operation==='add_identifier')return receipt.operation==='add_identifier'&&receipt.canonical_id===args.canonical_id&&receipt.identifier===args.identifier;
  if(args.operation==='fact_correct')return receipt.operation==='fact_correct' && receipt.canonical_id===args.canonical_id && receipt.fact_id===args.fact_id;
  if(args.operation==='add')return receipt.operation==='add' && receipt.direction===args.direction;
  if(args.operation==='close')return receipt.operation==='close' && receipt.anchor_key===args.anchor;
  if(args.operation==='keep')return receipt.operation==='keep' && receipt.anchor_key===args.anchor;
  if(args.operation==='snooze')return receipt.operation==='snooze' && receipt.anchor_key===args.anchor;
  if(args.operation==='correct')return receipt.operation==='correct' && receipt.anchor_key===args.anchor && receipt.loop_status===args.to;
  if(args.operation==='retarget')return receipt.operation==='retarget' && receipt.old_anchor_key===args.anchor && receipt.direction===args.direction;
  return true;
}

export function cleanupAcknowledgement(receipt:Extract<OwnerTaskToolReceipt,{operation:'cleanup'}>){if(receipt.outcome==='complete'&&receipt.items.length>5&&receipt.items.every(item=>item.action==='resolve'&&item.status==='applied'))return `Marked ${receipt.items.length} tasks done.`;return receipt.items.map(item=>`${item.index}. `+(item.status==='applied'?(item.action==='resolve'?'Marked done.':item.action==='dismiss'?'Dismissed.':item.action==='snooze'?`Snoozed until ${item.snoozed_until}.`:'Kept.'):item.status==='unknown'?'Outcome unconfirmed; this item may have changed.':item.status==='skipped'?'Not attempted.':`Not changed: ${item.reason.replaceAll('_',' ')}.`)).join('\n');}
export function resolveManyAcknowledgement(receipt:Extract<OwnerTaskToolReceipt,{operation:'resolve_many'}>,args:Extract<OwnerTaskToolArgs,{operation:'resolve_many'}>){const refused={target_changed:'This one changed since I checked.',target_unavailable:'I couldn’t tell which one you meant.',source_unavailable:'I can’t check this right now.',correction_unavailable:'I couldn’t confirm what you wanted changed.'};return receipt.items.map((item,i)=>`${args.items[i]!.name}: `+(item.status==='applied'?'Marked done.':item.status==='unknown'?'I couldn’t confirm whether this was marked done.':item.status==='skipped'?'I haven’t changed this one.':refused[item.reason])).join('\n');}

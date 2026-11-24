import {z} from 'zod';
export function scalarName(){return z.string().min(1).max(500).refine(value=>value===value.trim()&&!/[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(value));}
const profileValue=scalarName().max(200).optional();
export const explicitProfileFields={title:profileValue,company:profileValue};

export class OwnerTaskListUnavailable extends Error {}
export function numberedReferenceIndices(rowCount:number,references:readonly {first:number;last:number;correction:string}[],claim:string){
  const result:number[]=[],spans:{start:number;end:number}[]=[];
  for(const ref of references){
   const token=/^#?([1-9][0-9]*)(?:[-–]#?([1-9][0-9]*))?$/u.exec(ref.correction);
   if(!token||Number(token[1])!==ref.first||Number(token[2]??token[1])!==ref.last||ref.first>ref.last||ref.last>rowCount)throw new OwnerTaskListUnavailable('Numbered reference unavailable');
   const escaped=ref.correction.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
   const found=[...claim.matchAll(new RegExp(`(?<![\\p{L}\\p{N}_#:/.-])${escaped}(?![\\p{L}\\p{N}_:/-]|\\.[0-9])`,'gu'))];
   if(found.length!==1)throw new OwnerTaskListUnavailable('Exact numbered span unavailable');
   spans.push({start:found[0]!.index!,end:found[0]!.index!+found[0]![0].length});
   for(let n=ref.first;n<=ref.last;n++){if(result.includes(n))throw new OwnerTaskListUnavailable('Overlapping numbered choices');result.push(n);}
  }
  if(!spans.length)throw new OwnerTaskListUnavailable('Numbered reference unavailable');
  if(spans.some((a,i)=>spans.some((b,j)=>i!==j&&a.start<b.end&&b.start<a.end)))throw new OwnerTaskListUnavailable('Overlapping numbered spans');
  let residual=claim;for(const span of spans.sort((a,b)=>b.start-a.start))residual=residual.slice(0,span.start)+' '+residual.slice(span.end);
  const tail=claim.slice(Math.max(...spans.map(span=>span.end)));
  return {indices:result,residual,tail,referenceResidual:residual.slice(0,residual.length-tail.length)};
 }

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
  new RegExp(`^(?:please\\s+)?(?:close|resolve|complete)\\s*${connectors}(?:as well|too)?$`,'i'),
  new RegExp(`^(?:please\\s+)?mark\\s+${connectors}(?:as\\s+)?(?:done|complete|completed|closed|resolved)$`,'i')
];
export function resolveManyOwnerTextMatches(items:ReadonlyArray<{name:string;correction:string}>,ownerText:string,mode:'named_people'|'owner_tasks'|'delivered',exactClause=false):boolean{
  if(mode==='delivered'){
    if(/^done[.!]?$/i.test(ownerText.trim()))return items.length===1&&items[0]!.name===items[0]!.correction&&items[0]!.name===ownerText.trim().replace(/[.!]$/,'');
    const sent=/^(?:I\s+)?sent\s+(?:(?:a|an|the)\s+)?([\p{L}\p{N}_-]+)[.!]?$/iu.exec(ownerText.trim());
    if(sent)return items.length===1&&items[0]!.name===sent[1]&&items[0]!.correction===sent[1];
    if(items.some(item=>/\b(?:not|never|all|both|everything|done|close|leave|maybe|except|instead|still|open|if)\b/i.test(item.name)))return false;
  }
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

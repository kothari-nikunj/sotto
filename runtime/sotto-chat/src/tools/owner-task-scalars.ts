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

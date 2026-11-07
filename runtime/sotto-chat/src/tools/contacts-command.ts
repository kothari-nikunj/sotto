import {contactsArgsSchema} from './owner-contacts-contract.js';
import type {createOwnerContactsTools} from './owner-contacts-tool.js';
import {readOwnerGoogle,type GoogleReadOptions} from '../agent/google-read.js';
export function parseContactsCommand(argv:readonly string[]) {
 const [verb,...rest]=argv;const flags=new Map<string,string>();
 for(let i=0;i<rest.length;i+=2){const k=rest[i]!,v=rest[i+1];if(!['--query','--resource-name','--fields-json'].includes(k)||v===undefined||flags.has(k))throw new Error('Contacts command arguments are invalid');flags.set(k,v);}
 const exact=(keys:string[])=>flags.size===keys.length&&keys.every(k=>flags.has(k));
 if(verb==='capabilities'&&rest.length===0)return {kind:'read' as const,operation:'capabilities' as const,args:{}};
 if(verb==='confirm-contacts'&&rest.length===0)return {kind:'confirm' as const};
 if(verb==='contacts-search'&&exact(['--query']))return {kind:'read' as const,operation:'contacts_search' as const,args:{query:flags.get('--query')!,max:30}};
 if(verb==='contacts-get'&&exact(['--resource-name']))return {kind:'read' as const,operation:'contacts_get' as const,args:{resource_name:flags.get('--resource-name')!}};
 if(verb==='contacts-create'&&exact(['--fields-json']))return {kind:'write' as const,args:contactsArgsSchema.parse({operation:'propose_create',fields:JSON.parse(flags.get('--fields-json')!)})};
 if(verb==='contacts-update'&&exact(['--resource-name','--fields-json']))return {kind:'write' as const,args:contactsArgsSchema.parse({operation:'propose_update',resource_name:flags.get('--resource-name'),fields:JSON.parse(flags.get('--fields-json')!)})};
 if(verb==='contacts-delete'&&exact(['--resource-name']))return {kind:'write' as const,args:contactsArgsSchema.parse({operation:'propose_delete',resource_name:flags.get('--resource-name')})};
 throw new Error('Contacts command arguments are invalid');
}
export function createContactsCommand(tools:ReturnType<typeof createOwnerContactsTools>,reads:GoogleReadOptions){
 return async(request:{id:string;args:readonly string[]},context:{mode?:unknown;runId?:unknown;ownerRequestKey?:unknown;allowOwnerSkill?:unknown;sourceRevision?:unknown;signal?:AbortSignal})=>{
  if(request.id!=='google-action'||context.mode!=='chat'||typeof context.runId!=='string'||typeof context.ownerRequestKey!=='string'||context.allowOwnerSkill!==true)throw new Error('Contacts require one current owner message');
  const p=parseContactsCommand(request.args);
  if(p.kind==='read'){if(typeof context.sourceRevision!=='string')throw new Error('Contacts source proof unavailable');const result=await readOwnerGoogle(reads,p.operation,p.args,context.sourceRevision,context.signal);return {backend:'owner-google-read',coverage:'current_google_contacts',result};}
  const ctx={requestContext:{get:(k:string)=>(context as Record<string,unknown>)[k]},abortSignal:context.signal};
  let result:unknown;
  if(p.kind==='confirm')result=await tools.contacts_confirm.execute!({},ctx as never);
  else if(p.args.operation==='propose_create')result=await tools.contacts_create_review.execute!({fields:p.args.fields},ctx as never);
  else if(p.args.operation==='propose_update')result=await tools.contacts_update_review.execute!({resource_name:p.args.resource_name,fields:p.args.fields},ctx as never);
  else if(p.args.operation==='propose_delete')result=await tools.contacts_delete_review.execute!({resource_name:p.args.resource_name},ctx as never);
  else throw new Error('Contacts command unavailable');
  return {backend:'owner-contacts-receiver',coverage:'reviewed_google_contacts',result};
 };
}

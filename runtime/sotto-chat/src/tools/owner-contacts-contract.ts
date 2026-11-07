import {createHash} from 'node:crypto';
import {z} from 'zod';
export const ownerContactsKind='owner_contacts_tool';
const hex=z.string().regex(/^[a-f0-9]{64}$/);
export const contactResourceSchema=z.string().max(256).regex(/^people\/c[A-Za-z0-9_-]+$/);
export const contactFieldNames=['names','emailAddresses','phoneNumbers','organizations','birthdays','addresses','biographies'] as const;
export function contactsCanonical(value:unknown):string {
 if(Array.isArray(value))return '['+value.map(contactsCanonical).join(',')+']';
 if(value!==null&&typeof value==='object')return '{'+Object.keys(value).sort((a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)!),y=Array.from(b,c=>c.codePointAt(0)!);for(let i=0;i<Math.min(x.length,y.length);i++){if(x[i]!==y[i])return x[i]!-y[i]!;}return x.length-y.length;}).map(k=>JSON.stringify(k)+':'+contactsCanonical((value as Record<string,unknown>)[k])).join(',')+'}';
 return JSON.stringify(value);
}
const validJson=(v:unknown,depth=0):boolean=>depth<=8&&(v===null||typeof v==='boolean'||typeof v==='number'&&Number.isSafeInteger(v)||typeof v==='string'&&!/[\ud800-\udfff]/u.test(v)||Array.isArray(v)&&v.every(x=>validJson(x,depth+1))||typeof v==='object'&&v!==null&&Object.entries(v).every(([k,x])=>!/[\ud800-\udfff]/u.test(k)&&validJson(x,depth+1)));
const bytes=(n:number)=>(v:unknown)=>Buffer.byteLength(contactsCanonical(v),'utf8')<=n;
const bounded=(n:number)=>(v:unknown)=>validJson(v)&&bytes(n)(v);
const fieldShape=Object.fromEntries(contactFieldNames.map(k=>[k,z.array(z.unknown()).optional()])) as Record<typeof contactFieldNames[number],z.ZodOptional<z.ZodArray<z.ZodUnknown>>>;
export const contactFieldsSchema=z.object(fieldShape).strict().refine(bounded(12288));
export const contactWriteFieldsSchema=contactFieldsSchema.refine(v=>Object.keys(v).length>0);
const metadataText=z.string().min(1).max(2048).refine(v=>Array.from(v).length<=1024&&!/[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(v));
export const contactSnapshotSchema=z.object({resource_name:contactResourceSchema,etag:metadataText,fields:contactFieldsSchema.refine(v=>contactFieldNames.every(k=>Array.isArray(v[k]))),contact_sources:z.array(z.object({type:z.literal('CONTACT'),etag:metadataText,id:metadataText.optional()}).strict()).min(1)}).strict().refine(bytes(16384));
export const contactsReviewSchema=z.object({operation:z.enum(['create','update','delete']),resource_name:contactResourceSchema.nullable(),before:contactSnapshotSchema.nullable(),fields:contactFieldsSchema,source_revision:hex,review_hash:hex}).strict().refine(v=>bytes(32768)(v)&& (v.operation==='create'?v.resource_name===null&&v.before===null&&Object.keys(v.fields).length>0:v.before!==null&&v.before.resource_name===v.resource_name&&(v.operation==='delete'?Object.keys(v.fields).length===0:Object.keys(v.fields).length>0)));
const proof=z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:hex}).strict();
export const contactsArgsSchema=z.union([
 z.object({operation:z.literal('propose_create'),fields:contactWriteFieldsSchema}).strict(),
 z.object({operation:z.literal('propose_update'),resource_name:contactResourceSchema,fields:contactWriteFieldsSchema}).strict(),
 z.object({operation:z.literal('propose_delete'),resource_name:contactResourceSchema}).strict(),
 z.object({operation:z.enum(['create','update','delete']),review:contactsReviewSchema,proposal:proof}).strict().refine(v=>v.operation===v.review.operation&&v.proposal.source_revision===v.review.source_revision)
]);
export type ContactsArgs=z.infer<typeof contactsArgsSchema>;
export const contactsPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:contactsReviewSchema}).strict();
export type ContactsPreview=z.infer<typeof contactsPreviewSchema>;
export const contactsReceiptSchema=z.object({version:z.literal(1),status:z.literal('committed'),action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,outcome:z.object({operation:z.enum(['create','update','delete']),status:z.enum(['created','updated','deleted']),resource_name:contactResourceSchema,review_hash:hex}).strict()}).strict().refine(r=>r.outcome.status===({create:'created',update:'updated',delete:'deleted'} as const)[r.outcome.operation]);
export type ContactsReceipt=z.infer<typeof contactsReceiptSchema>;
export const contactsRejectionSchema=z.enum(['authority_changed','contacts_permission_unavailable','invalid_target','target_changed','review_unavailable','event_conflict']);
export type ContactsRejection=z.infer<typeof contactsRejectionSchema>;
export function contactsReviewHash(review:z.infer<typeof contactsReviewSchema>):string {const {review_hash,...rest}=review;return createHash('sha256').update(contactsCanonical(rest)).digest('hex');}
export function validContactsReview(p:ContactsPreview):boolean{return contactsReviewHash(p.review)===p.review.review_hash;}
export function contactsPreviewMatches(p:ContactsPreview,a:ContactsArgs):boolean {if(!validContactsReview(p)||!a.operation.startsWith('propose_'))return false;if(p.review.operation!==a.operation.slice(8))return false;if('resource_name'in a&&p.review.resource_name!==a.resource_name)return false;return 'fields'in a?contactsCanonical(p.review.fields)===contactsCanonical(a.fields):contactsCanonical(p.review.fields)==='{}';}
export function contactsReceiptMatches(r:ContactsReceipt,a:ContactsArgs):boolean{return 'review'in a&&r.outcome.operation===a.operation&&r.outcome.review_hash===a.review.review_hash&&(a.operation==='create'||r.outcome.resource_name===a.review.resource_name);}
const keyOrder=(a:string,b:string)=>{const x=Array.from(a,c=>c.codePointAt(0)!),y=Array.from(b,c=>c.codePointAt(0)!);for(let i=0;i<Math.min(x.length,y.length);i++){if(x[i]!==y[i])return x[i]!-y[i]!;}return x.length-y.length;};
const contactLabels:Record<string,string>={names:'Name',emailAddresses:'Email addresses',phoneNumbers:'Phone numbers',organizations:'Organizations',birthdays:'Birthdays',addresses:'Addresses',biographies:'Notes',givenName:'First name',familyName:'Last name',displayName:'Full name',value:'Value',type:'Label',formattedValue:'Full address',metadata:'Additional details',etag:'Version'};
const label=(k:string)=>contactLabels[k]??(k.replace(/([a-z0-9])([A-Z])/g,'$1 $2').replaceAll('_',' ').replace(/^./u,c=>c.toUpperCase()));
const scalarText=(v:unknown):string=>typeof v==='string'?(v?v.replaceAll('\\','\\\\').replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g,c=>({'\n':'\\n','\r':'\\r','\t':'\\t','\b':'\\b','\f':'\\f'} as Record<string,string>)[c]??'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0')):'(empty)'):v===null?'(not set)':String(v);
function detailLines(value:unknown,depth:number):string[]{
 const pad=' '.repeat(depth);
 const entry=(name:string,v:unknown):string[]=>{if(Array.isArray(v))return v.length?[pad+name+':',...detailLines(v,depth+2)]:[pad+name+': None'];if(v!==null&&typeof v==='object')return Object.keys(v).length?[pad+name+':',...detailLines(v,depth+2)]:[pad+name+': No details'];return [pad+name+': '+scalarText(v)];};
 if(Array.isArray(value))return value.flatMap((v,i)=>entry('Entry '+(i+1),v));
 return Object.keys(value as object).sort(keyOrder).flatMap(k=>entry(scalarText(label(k)),(value as Record<string,unknown>)[k]));
}
function fieldsText(fields:z.infer<typeof contactFieldsSchema>):string{return contactFieldNames.filter(k=>Object.hasOwn(fields,k)).flatMap(k=>fields[k]!.length?[label(k)+':',...detailLines(fields[k],2)]:[label(k)+': None']).join('\n');}
export function contactsReviewText(p:ContactsPreview):string {const r=p.review;return `${r.operation==='create'?'Add':r.operation==='update'?'Update':'Delete'} Google contact`+(r.resource_name?`\nContact ID: ${r.resource_name}`:'')+(r.before?`\nCurrent details:\n${fieldsText(r.before.fields)}`:'')+(r.operation!=='delete'?`\n${r.operation==='create'?'New details':'Requested changes'}:\n${fieldsText(r.fields)}`:'')+(r.operation==='update'?'\nThe listed details will replace the current details in those categories. Other details stay unchanged.':'')+`\n\nShould I ${r.operation==='create'?'add':r.operation} this contact?`;}
export function contactsActionIdentity(eventId:string,_kind:string,args:unknown):{actionId:string;payloadHash:string}{const encoded=contactsCanonical(args);if(Buffer.byteLength(encoded)>128*1024)throw new Error('Contacts arguments exceed bound');return {payloadHash:createHash('sha256').update(encoded).digest('hex'),actionId:createHash('sha256').update(JSON.stringify([eventId,ownerContactsKind,encoded])).digest('hex')};}

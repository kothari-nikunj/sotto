import {Message} from 'chat';
import {createHash} from 'node:crypto';
import {mkdir,writeFile} from 'node:fs/promises';
import {join} from 'node:path';

export async function captureDeliveryMedia(output:string,id:string,items:ReadonlyArray<{data?:Uint8Array;mimeType?:string}>|undefined,enabled:boolean) {
  if(!enabled||!items?.length)return {};
  const folder=join(output,'delivered-media');
  await mkdir(folder,{recursive:true,mode:0o700});
  const mediaSha256s:string[]=[],mediaPaths:string[]=[];
  for(const [index,media] of items.entries()){
    if(!media.data||media.mimeType!=='image/png'||media.data.length>5_000_000)throw new Error('Unknown gallery media contract');
    const bytes=Buffer.from(media.data),path=join(folder,`${id}-${index+1}.png`);
    await writeFile(path,bytes,{mode:0o600});
    mediaPaths.push(path);
    mediaSha256s.push(createHash('sha256').update(bytes).digest('hex'));
  }
  return {mediaSha256s,mediaPaths};
}

export async function acceptGalleryFixture(input:{command:(op:string,args:object)=>Promise<any>;output:string;step:number;text:string;
  sends:Array<{id:string;text:string;media:number;at:number;mediaSha256s?:string[]}>;initialize:()=>Promise<unknown>}) {
  const before=input.sends.length;
  await input.initialize();
  const deadline=Date.now()+5000;
  while(input.sends.length===before&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,10));
  if(input.sends.length!==before+1||input.sends[before]?.text!=='I started a new conversation.')
    throw new Error('Gallery fixture could not initialize a fresh owner session');
  const proof=await input.command('deliver_gallery',{step:input.step,text:input.text});
  const captured=input.sends.find(send=>send.id===proof.message_id);
  if(!captured||captured.media!==4||captured.text!==proof.summary||
      JSON.stringify(captured.mediaSha256s)!==JSON.stringify(proof.image_sha256s))
    throw new Error('Receiver gallery proof did not match captured transport acceptance');
  await writeFile(join(input.output,`background-gallery-${input.step}.json`),JSON.stringify({proof,captured},null,2),{mode:0o600});
  if(Date.now()<=captured.at)await new Promise(resolve=>setTimeout(resolve,2));
  return {id:captured.id,text:captured.text};
}


export async function acceptBackgroundFixture(input:{fetch:typeof fetch;port:number;bearer:string;step:number;text:string;sends:Array<{id:string;text:string}>}) {
  const before=input.sends.length;
  const response=await input.fetch(`http://127.0.0.1:${input.port}/v1/send`,{method:'POST',
    headers:{authorization:`Bearer ${input.bearer}`,'content-type':'application/json'},
    body:JSON.stringify({dispatch_id:`eval-background-${input.step}`,target:'photon',text:input.text,source_revision:'a'.repeat(64)})});
  if(!response.ok||(await response.json()).acceptance!=='accepted')throw new Error('Background fixture was not accepted');
  const accepted=input.sends.slice(before);
  await new Promise(resolve=>setTimeout(resolve,10));
  return accepted.length===1?{id:accepted[0]!.id,text:accepted[0]!.text}:undefined;
}

export function conversationInbound(step:number,thread:string,text:string,ownerId:string,quotedProviderId?:string,eventPrefix='eval-in') {
  return new Message({id:`${eventPrefix}-${step}`,threadId:thread,text,
    formatted:{type:'root',children:[]},raw:{id:`${eventPrefix}-${step}`,direction:'inbound',timestamp:new Date(),
      ...(quotedProviderId?{content:{type:'reply',target:{id:quotedProviderId}}}:{})},attachments:[],
    author:{userId:ownerId,userName:'owner',fullName:'Owner',isBot:false,isMe:false},
    metadata:{dateSent:new Date(),edited:false}});
}

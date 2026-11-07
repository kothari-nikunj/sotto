import {Message} from 'chat';

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

export function conversationInbound(step:number,thread:string,text:string,ownerId:string,quotedProviderId?:string) {
  return new Message({id:`eval-in-${step}`,threadId:thread,text,
    formatted:{type:'root',children:[]},raw:{id:`eval-in-${step}`,direction:'inbound',timestamp:new Date(),
      ...(quotedProviderId?{content:{type:'reply',target:{id:quotedProviderId}}}:{})},attachments:[],
    author:{userId:ownerId,userName:'owner',fullName:'Owner',isBot:false,isMe:false},
    metadata:{dateSent:new Date(),edited:false}});
}

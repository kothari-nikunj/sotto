import {createHash} from 'node:crypto';
/** Conservative smoke assertion, not a semantic judge of the answer. */
export function claimsQueuedWork(text: string): boolean {
  return text.split(/[.!?\n]+/u).some(sentence => {
    if (!/\bqueued\b|arrive separately/i.test(sentence)) return false;
    // A truthful refusal or statement that no *additional* job was queued is
    // not a completion promise. Keep positive claims in other sentences visible.
    return !/\bno(?: additional)? (?:research|work|job) (?:is|was|has been) queued\b|\bnothing (?:is|was|has been) queued\b|\b(?:is|was|have|has|did|could|can) not (?:been )?queued?\b|\b(?:haven['’]t|hasn['’]t|didn['’]t|couldn['’]t|can['’]t) queued?\b/i.test(sentence);
  });
}


/** Content of this exact accepted send; gallery text requires both receipt and byte binding. */
export function deliveredArtifactText(delivery:any, send:any): string | undefined {
  if(!delivery||delivery.status!=='delivered'||delivery.acceptance!=='accepted'||!send||delivery.provider_id!==send.id)return undefined;
  if(!send.media)return delivery.gallery==null?send.text:undefined;
  const gallery=delivery.gallery;
  if(gallery?.status!=='verified'||send.media!==4||send.text!==gallery.summary||
     !Array.isArray(send.mediaSha256s)||send.mediaSha256s.length!==4||
     JSON.stringify(send.mediaSha256s)!==JSON.stringify(gallery.image_sha256s))return undefined;
  return send.text+'\n'+gallery.card_text;
}

/** Seeded offers must execute the receipt-bound offer, never the direct-request path. */
export function prepExecutionMatches(execution:any,event:any,offered:boolean,proof?:any):boolean {
  if(!execution||!event)return false;
  let meeting;
  if(offered){
    const offer=proof?.offer,receipt=proof?.receipt;
    if(execution.kind!=='focused-meeting-prep'||!offer?.offer_id||execution.offerId!==offer.offer_id||
       offer.kind!=='meeting_prep'||!offer.message_id||offer.message_id!==receipt?.message_id||
       offer.target!=='photon'||receipt?.target!=='photon'||!offer.chat_id||offer.chat_id!==receipt?.chat_id||
       typeof offer.accepted_at!=='number'||!Number.isFinite(offer.accepted_at)||offer.accepted_at<=0||
       offer.accepted_at!==receipt?.accepted_at||!(Date.parse(offer.expires_at)>offer.accepted_at*1000))return false;
    meeting=execution.offerMeeting;
    if(meeting?.event_id!==offer.meeting?.event_id||Date.parse(meeting?.start)!==Date.parse(offer.meeting?.start))return false;
  }else{
    if(execution.kind!=='shared-meeting-prep')return false;
    meeting=execution.selection?.meeting;
  }
  return meeting?.event_id===event.id&&Number.isFinite(Date.parse(meeting?.start))&&
    Date.parse(meeting.start)===Date.parse(event.start?.dateTime);
}


/** Real typed chat offer must bind source occurrence, receiver receipt and exact accepted send. */
export function chatPrepOfferMatches(event:any,preview:any,recorded:any,proof:any,sends:any[]):boolean {
  const review=preview?.review,offer=proof?.offer;
  if(!event||preview?.status!=='preview'||recorded?.status!=='recorded'||!review||!offer)return false;
  const scoped='gcal_'+createHash('sha256').update(JSON.stringify(['owner@inside.test',event.id])).digest('hex');
  if(review.calendar_id!=='owner@inside.test'||review.provider_event_id!==event.id||review.meeting?.event_id!==scoped||
     offer.meeting?.event_id!==scoped||Date.parse(review.start)!==Date.parse(event.start?.dateTime)||
     Date.parse(offer.meeting?.start)!==Date.parse(event.start?.dateTime)||review.title!==event.summary||
     !event.attendees?.some((row:any)=>!row.self&&row.email===review.attendee_email)||offer.person!==review.attendee_email||
     review.offer_id!==offer.offer_id||recorded.offer_id!==offer.offer_id||recorded.review_hash!==review.review_hash||
     ['action_id','event_id','payload_hash','args_hash'].some(key=>!preview[key]||recorded[key]!==preview[key])||
     recorded.provider_message_id!==offer.message_id||Math.round(offer.accepted_at*1000)!==recorded.accepted_at_ms||
     offer.question!==review.question||!sends.some(send=>send.id===recorded.provider_message_id&&send.text===review.question))return false;
  return prepExecutionMatches({kind:'focused-meeting-prep',offerId:offer.offer_id,offerMeeting:offer.meeting},
    {...event,id:scoped},true,proof);
}

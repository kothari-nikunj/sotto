import type {OperationalJournal,ExplicitOwnerLoopAuthority} from '../journal/index.js';
import type {OwnerSkillRequestArgs} from './owner-skill-request-client.js';
import {ownerSkillRequestKind} from './owner-skill-request-client.js';
import {ownerReadRequest,researchTargetAnswer,validResearchSubject,ownerSubjectMention,contextualReadRequest,explicitReadReference,prepResearchRequest} from './read-admission.js';
import {createHash} from 'node:crypto';
const hash=(value:string)=>createHash('sha256').update(value).digest('hex');
type Context={eventId:string;admittedAt:number;dispatchId:string;text:string};
export function resolveResearchSelection(journal:OperationalJournal,authority:ExplicitOwnerLoopAuthority,eventId:string,
  input:OwnerSkillRequestArgs,sourceRevision:unknown,acceptedReadContexts:(eventId:string,revision:unknown)=>Context[]):OwnerSkillRequestArgs {
  const one=(sql:string,...args:(string|number)[])=>journal.db.prepare(sql).get(...args);
  if (input.skill !== 'ask-research') return input;
  const selector = input.selection.mode === 'company' ? input.selection.company : input.selection.person;
  const admitted = journal.ownerReminderToolAdmission(authority, eventId, true);
  const saved = one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerSkillRequestKind);
  if (saved && saved.status !== 'pending') {
    const request = journal.readOwnerSkillRequestAction(saved.id as string, authority).request;
    if (request.args.skill !== 'ask-research' || JSON.stringify(request.args.selection) !== JSON.stringify(input.selection))
      throw new Error('Research selection conflicts with saved request');
    return request.args;
  }
  if (!validResearchSubject(selector,input.selection.mode) ||
      !(ownerReadRequest(admitted.text) || researchTargetAnswer(admitted.text,selector,input.selection.mode)))
    throw new Error('Research requires a current owner request and a lookup identity');
  // Preserve direct public person research when optional private sources are unavailable.
  if (input.selection.mode === 'person' && ownerSubjectMention(admitted.text,selector) && sourceRevision === undefined)
    return {skill: 'ask-research', selection: input.selection};
  if (typeof sourceRevision !== 'string' || !/^[a-f0-9]{64}$/.test(sourceRevision)) throw new Error('Research source authority unavailable');
  if (saved) {
    const request = journal.readOwnerSkillRequestAction(saved.id as string, authority).request;
    // Preserve pre-upgrade pending request identity; it still receives both current-source checks.
    if (request.args.skill === 'ask-research' && request.args.source_revision === undefined &&
        !request.args.prior_selection && JSON.stringify(request.args.selection) === JSON.stringify(input.selection) &&
        ownerSubjectMention(admitted.text,selector)) return request.args;
  }
  const args = {...input, source_revision: sourceRevision};
  delete args.prior_selection;
  delete args.prior_request;
  delete args.prior_prep;
  if (!ownerReadRequest(admitted.text)) {
    const prior=acceptedReadContexts(eventId,sourceRevision)[0];
    const immediate=one('SELECT id FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if (!prior || immediate?.id!==prior.eventId || prior.text.length>1000 || !ownerReadRequest(prior.text) ||
        !researchTargetAnswer(admitted.text,selector,input.selection.mode)) throw new Error('Accepted research request unavailable');
    return {...args,prior_request:{event_id:prior.eventId,text:prior.text,admitted_at_ms:prior.admittedAt,
      accepted_dispatch_id:prior.dispatchId,source_revision:sourceRevision,thread_digest:hash(admitted.providerThreadId)}};
  }
  if (ownerSubjectMention(admitted.text,selector)) return args;
  for (const context of acceptedReadContexts(eventId, sourceRevision)) {
    if (!contextualReadRequest(admitted.text) ||
        !ownerSubjectMention(context.text,selector)) continue;
    // An elliptical question can continue the immediately preceding request.
    // Without a reference, unrelated older mentions are not a lookup grant.
    if (!explicitReadReference(admitted.text)) {
      const immediate=one('SELECT id FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
      if (immediate?.id!==context.eventId || !ownerReadRequest(context.text)) continue;
    }
    // Preserve the complete evidence, including surrounding negation; do not manufacture a target-only excerpt.
    if (context.text.length>1000) continue;
    return {...args, prior_selection: {event_id: context.eventId, text:context.text,
      admitted_at_ms: context.admittedAt, accepted_dispatch_id: context.dispatchId,
      source_revision: sourceRevision, thread_digest: hash(admitted.providerThreadId)}};
  }
  const prep = acceptedPrepResearch(journal,eventId,admitted.providerThreadId,admitted.text,input,sourceRevision);
  if(prep)return {...args,prior_prep:prep};
  throw new Error('Accepted research subject unavailable');
}

/** A frozen delivery selects the prep; its composed text never selects the company. */
function acceptedPrepResearch(journal:OperationalJournal,eventId:string,providerThreadId:string,text:string,
    input:OwnerSkillRequestArgs,sourceRevision:string) {
  if(input.skill!=='ask-research'||input.selection.mode!=='company'||input.selection.operation||!prepResearchRequest(text))return;
  const sessions=journal.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_sessions'").get();
  const session=sessions?journal.db.prepare('SELECT created_at FROM memory_sessions WHERE provider_thread=?').get(providerThreadId):undefined;
  const background=session?journal.acceptedBackgroundMessage(eventId,Number(session.created_at)):undefined;
  if(!background || journal.db.prepare(`SELECT 1 FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?)
    AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) AND received_at>=? LIMIT 1`).get(eventId,eventId,background.acceptedAt))return;
  if(background.sourceRevision===sourceRevision)return {provider_message_id:background.providerMessageId,
    source_revision:sourceRevision,route_digest:background.routeDigest};
}

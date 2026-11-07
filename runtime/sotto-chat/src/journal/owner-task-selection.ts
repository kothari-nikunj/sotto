import type {OperationalJournal} from './index.js';
import {OWNER_TASK_SELECTION_CLARIFICATION} from '../tools/owner-task-tool-contract.js';

export function ownerCleanupProposalCurrent(journal:OperationalJournal,action:Record<string,unknown>,eventId:string):boolean {
  const metadata=JSON.parse(action.result_json as string);
  if(metadata.next_event_id===eventId)return metadata.next_event_accepted===true;
  if(metadata.cleanup_repair_event_id!==metadata.next_event_id||metadata.next_event_accepted!==true||metadata.repair_next_event_id!==eventId||metadata.repair_next_accepted!==true)return false;
  const prior=journal.db.prepare('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1').get(eventId,eventId);
  const repair=journal.getReplyForEvent(metadata.cleanup_repair_event_id);
  if(prior?.id!==metadata.cleanup_repair_event_id||prior?.outcome!=='resolved'||repair?.dispatch_status!=='accepted'||repair?.payload!==OWNER_TASK_SELECTION_CLARIFICATION||repair.source_revision!==metadata.args?.source_revision)return false;
  const original=JSON.parse(journal.getInbound(action.source_event_id as string)!.payload as string),clarification=JSON.parse(journal.getInbound(prior.id as string)!.payload as string);
  return original.context.destination===clarification.context.destination&&journal.getDispatch(repair.dispatch_id as string)?.destination===original.context.destination;
}

export function freezeCleanupRepairSelection(journal:OperationalJournal,action:Record<string,unknown>,id:string){
  const metadata=JSON.parse(action.result_json as string);
  if(metadata.args?.operation==='propose_cleanup'&&metadata.cleanup_repair_event_id!==undefined&&metadata.repair_next_event_id===undefined){
    const repairReply=journal.getReplyForEvent(metadata.cleanup_repair_event_id);
    journal.db.prepare('UPDATE actions SET result_json=? WHERE id=?').run(JSON.stringify({...metadata,
      repair_next_event_id:id,repair_next_accepted:repairReply?.dispatch_status==='accepted'}),action.id as string);
  }
}

export function markCleanupSelectionRepair(journal:OperationalJournal,action:Record<string,unknown>,eventId:string){
  const metadata=JSON.parse(action.result_json as string);
  if(metadata.next_event_id===eventId)journal.db.prepare('UPDATE actions SET result_json=?,proposal_checked=NULL WHERE id=?').run(
    JSON.stringify({...metadata,cleanup_repair_event_id:eventId}),action.id as string);
}

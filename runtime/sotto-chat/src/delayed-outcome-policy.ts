/** Existing owner-action receipt paths; policy never authorizes an effect or weakens its proof. */
export const DELAYED_OUTCOME_POLICY = {
  owner_calendar_tool: {subject:'the calendar change',settled:['committed','rejected'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_bridge_send_tool: {subject:'the message from your Mac',settled:['committed','rejected'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_contacts_tool: {subject:'the contact change',settled:['committed','rejected'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_gmail_draft_tool: {subject:'the Gmail action',settled:['committed','preview','rejected'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_loop_tool: {subject:'the task change',settled:['committed','preview'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_reminder_tool: {subject:'the reminder change',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_preference_tool: {subject:'the preference change',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_routine_tool: {subject:'the routine change',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_skill_request: {subject:'the skill request',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_web_read: {subject:'the URL read',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_brief_request: {subject:'the brief request',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_meeting_prep_request: {subject:'the meeting prep request',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_meeting_prep_offer: {subject:'the meeting preparation offer',settled:['preview','recorded','rejected'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_offer_reply: {subject:'the offer reply',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_loop_add_admitted: {subject:'the task addition',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_loop_close_admitted: {subject:'the task closure',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_reminder_admitted: {subject:'the reminder command',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
  owner_quiet_admitted: {subject:'the quiet setting',settled:['committed'],inspectIntervalMs:60_000,maxNoticeAttempts:2},
} satisfies Record<string, {subject:string;settled:readonly string[];inspectIntervalMs:number;maxNoticeAttempts:number}>;
export type DelayedActionKind = keyof typeof DELAYED_OUTCOME_POLICY;
export const DELAYED_ACTION_KINDS = Object.keys(DELAYED_OUTCOME_POLICY);
export function delayedPolicy(kind: unknown) {
  return typeof kind === 'string' && Object.hasOwn(DELAYED_OUTCOME_POLICY,kind)
    ? DELAYED_OUTCOME_POLICY[kind as DelayedActionKind] : undefined;
}
export function delayedProofSettles(kind: DelayedActionKind, status: string): boolean {
  return (DELAYED_OUTCOME_POLICY[kind].settled as readonly string[]).includes(status);
}

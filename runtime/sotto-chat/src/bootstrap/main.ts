import {projectTaskToolDiagnostics, type TaskToolDiagnosticBatch} from '../agent/tool-diagnostics.js';
import {OWNER_TASK_READBACK_GUIDE} from '../tools/owner-task-readback.js';
import {createOwnerBridgeSendClient} from '../tools/owner-bridge-send-client.js';
import {createOwnerBridgeSendTools} from '../tools/owner-bridge-send-tool.js';
import {createOwnerBriefTools} from '../tools/owner-brief-tool.js';
import {projectModelMetrics, type ModelMetrics} from '../agent/metrics.js';
import {createPhotonPrepContinuation} from '../channels/photon-continuation.js';
import {createGmailSendCommand} from '../tools/gmail-send-command.js';
import {createOwnerContactsClient} from '../tools/owner-contacts-client.js';
import {createOwnerContactsTools,CONTACTS_EXECUTION_GUIDE} from '../tools/owner-contacts-tool.js';
import {createContactsCommand} from '../tools/contacts-command.js';
import {createOwnerCalendarClient} from '../tools/owner-calendar-client.js';
import {createOwnerCalendarTools,createCalendarCommand,CALENDAR_EXECUTION_GUIDE} from '../tools/owner-calendar-tool.js';
import {createOwnerTaskExecution} from '../tools/owner-task-execution.js';
import {createTaskCommand,TASK_CORRECTION_EXECUTION_GUIDE} from '../tools/task-command.js';
import {createOwnerWebReadClient,createWebReadCommand,WEB_READ_EXECUTION_GUIDE} from '../tools/owner-web-read.js';
import {createKnowledgeCommand, KNOWLEDGE_EXECUTION_GUIDE, MASTER_CONTEXT_EXECUTION_GUIDE, LOOPS_EXECUTION_GUIDE, RETUNE_EXECUTION_GUIDE, GRANOLA_HISTORY_EXECUTION_GUIDE, RELATIONSHIP_IMPORTANCE_EXECUTION_GUIDE, RELATIONSHIP_ATTENTION_EXECUTION_GUIDE, TRIAGE_EXECUTION_GUIDE, STYLE_APPLY_EXECUTION_GUIDE, isStyleApplyCommand} from '../tools/knowledge-command.js';
import {createPhotonTextFormatter} from '../channels/photon-text-formatter.js';
import {createOwnerSkillRequestClient} from '../tools/owner-skill-request-client.js';
import {createOwnerSkillExecution} from '../tools/owner-skill-execution.js';
import {createSkillCommand, SHARED_SKILL_EXECUTION_GUIDE} from '../tools/skill-command.js';
import {createRunScriptTool} from '../tools/run-script-tool.js';
import {createPreferenceCommand, SKILL_COMMAND_EXECUTION_GUIDE, PROCEDURE_EXECUTION_GUIDE,USEFULNESS_EXECUTION_GUIDE} from '../tools/preference-command.js';
import {createOwnerPreferenceExecution} from '../tools/owner-preference-execution.js';
import {createOwnerOfferReplyClient} from '../tools/owner-offer-reply-client.js';
import {createOwnerMeetingPrepClient} from '../tools/owner-meeting-prep-client.js';
import {createOwnerMeetingPrepOfferClient} from '../tools/owner-meeting-prep-offer-client.js';
import {createOwnerMeetingPrepOfferTool,MEETING_PREP_OFFER_GUIDE} from '../tools/owner-meeting-prep-offer-tool.js';
import {createOwnerBriefRequestClient} from '../tools/owner-brief-request-client.js';
import { createOwnerGmailDraftClient } from '../tools/owner-gmail-draft-client.js';
import { createOwnerGmailDraftTools, GMAIL_DRAFT_EXECUTION_GUIDE } from '../tools/owner-gmail-draft-tool.js';
import { createOwnerRoutineToolClient } from '../tools/owner-routine-tool-client.js';
import { createOwnerRoutineTools } from '../tools/owner-routine-tool.js';
import { RequestContext } from '@mastra/core/request-context';
import { createOwnerPreferenceToolClient } from '../tools/owner-preference-tool-client.js';
import { createOwnerPreferenceTools } from '../tools/owner-preference-tool.js';
import { createOwnerTaskToolClient } from '../tools/owner-task-tool-client.js';
import { createOwnerTaskTools } from '../tools/owner-task-tool.js';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createBridgeMcpClients } from '../agent/bridge.js';
import { createStyleReadTool } from '../tools/style-read-tool.js';
import { createOwnerLoopsTool } from '../agent/owner-loops.js';
import { createOwnerRetuneTool } from '../agent/owner-retune.js';
import { createGoogleReadTools } from '../agent/google-read.js';
import { createBriefDetailTool, BRIEF_DETAIL_EXECUTION_GUIDE } from '../agent/brief-detail.js';
import type { Tool } from '@mastra/core/tools';
import { createSottoAgentRuntime, type SottoAgentRuntime } from '../agent/runtime.js';
import { createManagedProxyModels } from '../agent/models.js';
import { createSelfHostGoogleModels } from '../agent/selfhost-models.js';
import { createSourceAuthorityReader, readAuthority, type SourceAuthority, type SourceAuthorityReader } from '../agent/source-authority.js';
import { startApplication, projectChatTiming, type ApplicationAgent, type ChatTiming } from '../application.js';
import type { createChatHttpServer } from '../http.js';
import { createChannelSdkAdapter } from '../channels/sdk.js';
import { createInboundMediaHydrator } from '../channels/media-hydration.js';
import { PhotonOwnerFeedback, photonFeedbackProvider, TurnPresence, photonPresenceProvider, telegramPresenceProvider } from '../channels/presence.js';
import { createPhotonTransport, createTelegramTransport, type ChannelTransport } from '../channels/transport.js';
import type { CredentialSource } from '../channels/types.js';
import { GatewayStartupError, startOwnerGateway, type GatewayAdapter } from '../channels/gateway.js';
import { claimRuntimeDirectory } from '../ownership.js';
import { activationDiscoverySeedSchema, activationIdentitySchema, photonProjectFingerprint,
  type ActivationDiscoverySeed, type ActivationIdentity } from '../activation.js';
import { readSkillIndex } from '../tools/skills.js';
import { createOwnerLoopClient } from '../tools/owner-loop-client.js';
import { createOwnerReminderClient } from '../tools/owner-reminder-client.js';
import type { OperationalJournal } from '../journal/index.js';
import { createOwnerReminderToolClient } from '../tools/owner-reminder-tool-client.js';
import { createOwnerReminderTools } from '../tools/owner-reminder-tool.js';
import { createOwnerQuietClient } from '../tools/owner-quiet-client.js';
import { ensurePrivateStateDirectory, loadRuntimeConfig, type RuntimeConfig } from './config.js';
import { configuredChannelIdentityForConfig } from '../configured-identity.js';

export interface RuntimeAgent extends ApplicationAgent { close(): Promise<void> }
interface BridgeTools { chat(sourceRevision?: string, listingAuthority?: SourceAuthority): Promise<Record<string, Tool>>; unattended(sourceRevision?: string): Promise<Record<string, Tool>> }
type AgentConfig = Omit<RuntimeConfig, 'ownerLoop'>;
type RuntimeModels = ReturnType<typeof createManagedProxyModels> | ReturnType<typeof createSelfHostGoogleModels>;
export interface RuntimeHandle {
  readonly port: number;
  readonly gateway: Awaited<ReturnType<typeof startOwnerGateway>>;
  rotateSessions(): Promise<{ rotated: true; count: number }>;
  close(): Promise<void>;
}
export interface BootstrapDependencies {
  /** Test seam. The default factories remain local and do not connect until gateway start. */
  createAdapter?(channel: RuntimeConfig['channel'], credentials: CredentialSource): GatewayAdapter;
  createTransport?(channel: RuntimeConfig['channel'], adapter: GatewayAdapter, options?: {sharedProjectThread?: () => string | undefined; formatText?: (text: string) => string | Promise<string>}): ChannelTransport;
  createAgent?(config: AgentConfig, model: RuntimeModels['chatModel'], mediaInterpreter: RuntimeModels['generateNativeMedia'], skillPaths: readonly string[], bridgeTools?: BridgeTools, sourceAuthority?: SourceAuthorityReader): RuntimeAgent;
  fetch?: typeof fetch;
  openStateDatabase?(path: string): DatabaseSync;
  listenApplication?(server: ReturnType<typeof createChatHttpServer>, port: number | undefined): Promise<number>;
  shutdownTimeoutMs?: number;
}

/** Omitted local packs stay optional; installed images use their canonical pack. */
export function skillPackRootForConfig(config: Pick<RuntimeConfig, 'configPath' | 'skillPackRoot'>,
                                     moduleUrl = import.meta.url): string | undefined {
  if (config.skillPackRoot) return path.resolve(path.dirname(config.configPath), config.skillPackRoot);
  return fileURLToPath(moduleUrl) === '/app/runtime/sotto-chat/dist/bootstrap/main.js'
    ? '/app/sotto-skills' : undefined;
}

/** Only Spectrum Cloud exposes a project identity compatible with receiver activation. */
export function activationIdentityForConfig(config: RuntimeConfig): ActivationIdentity | undefined {
  if (config.channel !== 'photon' || config.credentials.mode !== 'cloud' || !config.providerThreadId) return undefined;
  return activationIdentitySchema.parse({
    tenant_id: config.tenantId ?? '',
    project_fingerprint: photonProjectFingerprint(config.credentials.photonProjectId),
    owner_id: config.owner.ownerId,
    account_id: config.owner.accountId,
    line_key: config.lineKey,
    provider_thread_id: config.providerThreadId,
  });
}

export function activationDiscoverySeedForConfig(config: RuntimeConfig): ActivationDiscoverySeed | undefined {
  if (config.channel !== 'photon' || config.credentials.mode !== 'cloud' || !config.firstContact) return undefined;
  return activationDiscoverySeedSchema.parse({
    tenant_id: config.tenantId ?? '',
    project_fingerprint: photonProjectFingerprint(config.credentials.photonProjectId),
    owner_id: config.owner.ownerId,
    account_id: config.owner.accountId,
    line_key: config.lineKey,
    ...(config.firstContact.mode === 'owner_dm_shared_project' ? { provider_route_mode: 'shared_project' } : { expected_provider_line: config.firstContact.expectedProviderLine }),
  });
}

// Keep a failed-stop owner's lock and associated resources reachable until process exit.
// This is deliberately fail-closed; no timer or PID guess can release ownership.
const retainedRuntimeStates = new Set<Record<string, unknown>>();

function defaultTransport(channel: RuntimeConfig['channel'], adapter: GatewayAdapter, sharedProjectThread?: () => string | undefined, formatText?: (text: string) => string | Promise<string>): ChannelTransport {
  return channel === 'telegram'
    ? createTelegramTransport(adapter as Parameters<typeof createTelegramTransport>[0])
    : createPhotonTransport(adapter as unknown as Parameters<typeof createPhotonTransport>[0], { sharedProjectThread, formatText });
}

// Older rendered startup files contain a fixed capability appendix. Keep the
// persisted file intact, but correct its now-stale denials using capabilities
// this bootstrap actually mounted. Source-backed tools still need a current
// per-turn grant; these instructions cannot supply one.
function effectiveCapabilityInstructions(instructions: string, mounted: {
  task: boolean; conditionalReminder: boolean; routine: boolean; preference: boolean;
  gmail: boolean; calendar: boolean; bridgeSend: boolean;
}): string {
  const heading = /\n\n(?:Managed Photon|Self-host Photon|Self-host Telegram) capabilities:/g;
  const matches = [...instructions.matchAll(heading)];
  const start = matches.at(-1)?.index;
  if (start === undefined) return instructions;
  const appendix = instructions.slice(start);
  if (!appendix.endsWith('Claim an effect only after a confirmed receipt; otherwise say it is unavailable or uncertain.')) return instructions;
  let current = appendix;
  if (mounted.task) current = current.replace(
    'An owner must send the literal `/loop add <text>` command to add an open loop. To close one, read `owner_open_loops` for its actual `anchor_key` and ask the owner to send `/loop close <anchor_key>`; never invent an anchor or claim a plain-language request changed a loop.',
    'For an explicit owner task request, use offered owner_task_add, owner_task_close or owner_task_resolve_many. For two or more completions in one message, read current rows and use owner_task_resolve_many once for every requested completion, leaving still-open tasks out. Include unresolved names without target proofs; they must not block independent clear selections. Use owner_task_list for displayed numbered tasks and owner_task_cleanup for numbered done choices. Single close requires its exact owner selection phrase and cannot complete only the first of several. Never invent an anchor or claim a change without each receipt.');
  if (mounted.conditionalReminder) current = current.replace(
    'These tools do not authorize conditional checks, recurring jobs or external actions.',
    'A conditional reminder requires the separately offered owner_reminder_conditional tool and its exact current loop proof; reminder tools do not authorize recurring jobs or external actions.');
  const oldDenial = 'You cannot create or change personal routines or other schedules, change other preferences, write Gmail or Calendar, or send through Bridge. You may draft text in chat, but cannot create an external draft.';
  const effective = [
    mounted.routine ? 'Offered owner_routine_propose, owner_routine_create, owner_routine_adjust_propose, owner_routine_adjust_confirm and owner_routine_cancel tools may change personal routines under their own review and proof rules.' : 'Personal routine changes are unavailable.',
    mounted.preference ? 'Offered owner_preference_change and the admitted run_sotto_script preferences command may change owner preferences.' : 'Preference changes are unavailable.',
    mounted.gmail ? 'Gmail draft and send review tools require a current Gmail source grant and their separate confirmation rules; an offered draft-save tool can create an external draft but never sends it.' : 'Gmail writes and external drafts are unavailable.',
    mounted.calendar ? 'Calendar write review tools require a current Calendar source grant and their separate confirmation rules.' : 'Calendar writes are unavailable.',
    mounted.bridgeSend ? 'For an owner-requested iMessage or SMS, use bridge_send_review with the exact recipient phone/email and full body. If only a name was given, use Contacts to resolve it and clarify ambiguity. Review checks sending permission; the Mac checks its live switch again at dispatch. Wait for a distinct owner yes, then use bridge_send_confirm with no arguments. Never call raw MCP send_message. A connection alone is not send permission. Never claim sending before the committed receipt.' : 'Mac message sending is unavailable in this runtime.',
  ].join(' ');
  current = current.replace(oldDenial, effective);
  return instructions.slice(0, start) + current;
}

// Base chat can answer without loading a skill. Mirror the short-chat and inline-wording
// and evidence/company-claim rules in _shared/references/voice.md here so direct replies get the same voice.
const GENERAL_CHAT_VOICE_GUIDE = 'Explain evidence metadata in ordinary words, preserving who said or confirmed what and when. Omit internal field names, status labels and opaque record or message IDs unless the owner asks for them; keep useful names, dates, quotations and links. A stated intention, a change in participants or an acknowledgment of a request does not establish a later action the message itself does not report; do not narrate an unseen continuation as fact. Describe missing evidence as a limit of the read (for example, “I didn’t find a later reply in this thread”), never as proof that activity stopped or continued elsewhere. Describe relevant source, conversation and time limits in plain words, using a conversation’s subject or participants instead of raw queries or IDs, even when it was read in full. For a narrow owner question, answer in one or two plain sentences without an unasked diagnostic list. For a request for just the wording, give one ready-to-use version, not several options unless asked. A follow-up such as “shorter pls” revises that wording; it does not save or send anything. If a follow-up could refer equally to multiple people or companies, ask which one before reading; never silently pick one. A question about why a previous read failed uses that result, not a fresh retry unless requested. When the owner chooses one of several people or records, carry that choice into later references; other candidates are not aliases or additional details of the chosen person. Health and capabilities show connection and enabled permissions, not proof that every source was read. A partial or dated coverage report does not prove that other records are absent, unlearned or missing from memory; say what the report actually measured. Describe sources as enabled unless a current content read succeeded; preserve partial or unavailable coverage. An inline draft is wording only, not evidence that the owner sent it or promised it in a meeting. For a meeting-promise question, use the loaded followup skill and its current meeting notes; if those notes are unavailable, say you cannot verify the promises, even if chat history contains an inline draft. Attribute company security, privacy, data-retention, performance and compliance claims to their source (for example, “the company says”). Saved notes, decks and company websites are evidence of a claim, not independent verification. Do not turn a stated design or benefit into an absolute guarantee that data cannot leak or that compliance is solved; preserve any supported scope and limits. Keep this attribution brief, including in requested blurbs.';

function defaultAgent(
  config: AgentConfig,
  model: RuntimeModels['chatModel'],
  mediaInterpreter: RuntimeModels['generateNativeMedia'],
  skillPaths: readonly string[],
  bridgeTools?: BridgeTools,
  sourceAuthority?: SourceAuthorityReader,
  readMasterContext?: Parameters<typeof createSottoAgentRuntime>[0]['readMasterContext'],
  recordChatTiming?: (timing: ChatTiming, completedAtMs: number, outcome: 'success' | 'failed') => void,
  recordTaskToolDiagnostics?: (batch: TaskToolDiagnosticBatch | undefined) => void,
  recordModelMetrics?: (metrics:ModelMetrics) => void,
  answerOnlyInstructions?: string,
): SottoAgentRuntime {
  return createSottoAgentRuntime({
    model,
    storagePath: path.join(config.stateDirectory, 'agent-memory.db'),
    instructions: config.instructions,
    answerOnlyInstructions,
    skillPaths,
    mediaInterpreter,
    sourceAuthority,
    readMasterContext,
    onModelMetrics: value => {
      const metrics = projectModelMetrics(value);
      if (metrics?.mode === 'chat') recordModelMetrics?.(metrics);
    },
    onTaskToolDiagnostics: events => {
      recordTaskToolDiagnostics?.(projectTaskToolDiagnostics({events, completedAtMs: Date.now()}));
    },
    onChatTiming: (timing, outcome) => {
      console.info('[sotto-chat-timing]', JSON.stringify(timing));
      const bounded = projectChatTiming(timing);
      if (bounded) recordChatTiming?.(bounded, Date.now(), outcome ?? 'success');
    },
    chatTools: bridgeTools ? authority => bridgeTools.chat(authority.sourceRevision, authority.listingAuthority) : undefined,
    unattendedTools: bridgeTools ? authority => bridgeTools.unattended(authority.sourceRevision) : undefined,
  }) as SottoAgentRuntime;
}

export async function startConfiguredRuntime(configFile: string, dependencies: BootstrapDependencies = {}): Promise<RuntimeHandle> {
  const config = await loadRuntimeConfig(configFile);
  await ensurePrivateStateDirectory(config.stateDirectory);
  const rawRelease = await claimRuntimeDirectory(config.stateDirectory);
  let lockReleased = false;
  const release = async () => {
    if (lockReleased) return;
    await rawRelease();
    lockReleased = true;
  };

  let agent: RuntimeAgent | undefined;
  let application: Awaited<ReturnType<typeof startApplication>> | undefined;
  let gateway: RuntimeHandle['gateway'] | undefined;
  let chatDatabase: DatabaseSync | undefined;
  let bridge: ReturnType<typeof createBridgeMcpClients> | undefined;
  let gatewayStopped = false;
  let presence: TurnPresence | undefined;
  let feedback: PhotonOwnerFeedback | undefined;
  const retainedState: Record<string, unknown> = { release };
  try {
    const proxy = config.modelDirect ? createSelfHostGoogleModels({
      ...config.modelDirect, fetch: dependencies.fetch,
    }) : createManagedProxyModels({
      origin: config.modelProxy!.origin,
      allowedOrigin: new URL(config.modelProxy!.origin).origin,
      bearer: config.modelProxy!.bearer,
      chatModelId: config.modelProxy!.chatModelId,
      nativeMediaModelId: config.modelProxy!.nativeMediaModelId,
      fetch: dependencies.fetch,
    });
    const skillPackRoot = skillPackRootForConfig(config);
    const skillPaths = skillPackRoot ? (await readSkillIndex(skillPackRoot)).map(entry => entry.path) : [];
    const credentials = config.credentials as CredentialSource;
    const adapter = dependencies.createAdapter?.(config.channel, credentials) ?? createChannelSdkAdapter(config.channel, credentials) as GatewayAdapter;
    retainedState.adapter = adapter;
    let reminderJournal: OperationalJournal | undefined;
    const sharedProjectThread = config.channel === 'photon' && config.firstContact?.mode === 'owner_dm_shared_project'
      ? () => reminderJournal?.getActivationIdentity()?.provider_thread_id : undefined;
    const formatText = config.channel === 'photon' ? createPhotonTextFormatter(config.skillPackRoot
      ? path.resolve(path.dirname(config.configPath), config.skillPackRoot, '_shared/lib')
      : '/app/sotto-skills/_shared/lib') : undefined;
    const transport = dependencies.createTransport?.(config.channel, adapter, {sharedProjectThread, formatText}) ??
      defaultTransport(config.channel, adapter, sharedProjectThread, formatText);
    presence = config.turnPresence ? new TurnPresence({ provider: config.channel === 'photon'
      ? photonPresenceProvider(adapter as Parameters<typeof photonPresenceProvider>[0])
      : telegramPresenceProvider(adapter as Parameters<typeof telegramPresenceProvider>[0]) }) : undefined;
    feedback = config.channel === 'photon' ? new PhotonOwnerFeedback(photonFeedbackProvider(
      adapter as unknown as Parameters<typeof photonFeedbackProvider>[0])) : undefined;
    retainedState.transport = transport;

    // The explicit no-source-tools configuration also has a baseline. Removing a
    // Bridge configuration must invalidate history written while it was connected.
    const sourceAuthority: SourceAuthorityReader = config.bridge
      ? createSourceAuthorityReader({ bridgeUrl: config.bridge.url, bearer: config.bridge.readBearer, fetch: dependencies.fetch })
      : { read: async () => ({ version: 2, complete: true, bridge_sources: [], google_sources: [],
        revision: createHash('sha256').update('sotto-no-source-tools-v2').digest('hex') }) };
    if (config.bridge) {
      bridge = createBridgeMcpClients({ url: config.bridge.url, readBearer: config.bridge.readBearer, sourceAuthority, fetch: dependencies.fetch });
      retainedState.bridge = bridge;
    }
    // MCP tools are refreshed per turn and read-only; this bootstrap never creates
    // a Bridge send capability or script runner.
    const ownerLoopsTool = config.bridge ? createOwnerLoopsTool({ bridgeUrl: config.bridge.url,
      readBearer: config.bridge.readBearer, fetch: dependencies.fetch }) : undefined;
    const ownerRetuneTool = config.bridge ? createOwnerRetuneTool({ bridgeUrl: config.bridge.url,
      readBearer: config.bridge.readBearer, fetch: dependencies.fetch }) : undefined;
    const googleReadTools = config.bridge ? createGoogleReadTools({ bridgeUrl: config.bridge.url,
      readBearer: config.bridge.readBearer, sourceAuthority, fetch: dependencies.fetch }) : {};
    const briefDetailTool = config.bridge
      ? createBriefDetailTool({ bridgeUrl: config.bridge.url,
        readBearer: config.bridge.readBearer, sourceAuthority, fetch: dependencies.fetch }) : undefined;
    // This static private capability is used only by the coordinator's exact
    // admitted-command path. It is never a Mastra or MCP tool.
    const ownerLoop = config.ownerLoop && config.bridge
      ? createOwnerLoopClient({ bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch }) : undefined;
    const ownerReminder = config.ownerLoop && config.bridge
      ? createOwnerReminderClient({ bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch }) : undefined;
    const ownerQuiet = config.ownerLoop && config.bridge
      ? createOwnerQuietClient({ bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch }) : undefined;
    const ownerReminderTool = config.ownerLoop && config.bridge
      ? createOwnerReminderToolClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerTaskTool = config.ownerLoop && config.bridge
      ? createOwnerTaskToolClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,readBearer:config.bridge.readBearer,sourceAuthority,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerPreferenceTool = config.ownerLoop && config.bridge
      ? createOwnerPreferenceToolClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerRoutineTool = config.ownerLoop && config.bridge
      ? createOwnerRoutineToolClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerMeetingPrep = config.ownerLoop?.identity.channel === 'photon' && config.bridge
      ? createOwnerMeetingPrepClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerMeetingPrepOffer = config.ownerLoop?.identity.channel === 'photon' && config.bridge
      ? createOwnerMeetingPrepOfferClient({bridgeUrl:config.bridge.url,bearer:config.ownerLoop.bearer,
        identity:config.ownerLoop.identity,fetch:dependencies.fetch}) : undefined;
    const ownerMeetingPrepOfferTools=ownerMeetingPrepOffer?createOwnerMeetingPrepOfferTool({client:ownerMeetingPrepOffer,
      journal:()=>reminderJournal,sourceAuthority}):{};
    const ownerOfferReply = config.ownerLoop?.identity.channel === 'photon' && config.bridge
      ? createOwnerOfferReplyClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerBriefRequest = config.ownerLoop && config.bridge
      ? createOwnerBriefRequestClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const briefTools = ownerBriefRequest ? createOwnerBriefTools({client: ownerBriefRequest, journal: () => reminderJournal, sourceAuthority}) : {};
    const ownerGmailDraft = config.ownerLoop && config.bridge
      ? createOwnerGmailDraftClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer,
        identity: config.ownerLoop.identity, fetch: dependencies.fetch}) : undefined;
    const ownerCalendar=config.ownerLoop&&config.bridge?createOwnerCalendarClient({bridgeUrl:config.bridge.url,bearer:config.ownerLoop.bearer,identity:config.ownerLoop.identity,fetch:dependencies.fetch}):undefined;
    const calendarTools=ownerCalendar?createOwnerCalendarTools({client:ownerCalendar,journal:()=>reminderJournal,sourceAuthority}):{};
    const calendarCommand=ownerCalendar?createCalendarCommand({client:ownerCalendar,journal:()=>reminderJournal,sourceAuthority}):undefined;
    const ownerContacts=config.ownerLoop&&config.bridge?createOwnerContactsClient({bridgeUrl:config.bridge.url,bearer:config.ownerLoop.bearer,identity:config.ownerLoop.identity,fetch:dependencies.fetch}):undefined;
    const contactsTools=ownerContacts?createOwnerContactsTools({client:ownerContacts,journal:()=>reminderJournal,sourceAuthority}):undefined;
    const ownerBridgeSend=config.ownerLoop&&config.bridge?createOwnerBridgeSendClient({bridgeUrl:config.bridge.url,bearer:config.ownerLoop.bearer,identity:config.ownerLoop.identity,fetch:dependencies.fetch}):undefined;
    const bridgeSendTools=ownerBridgeSend?createOwnerBridgeSendTools({client:ownerBridgeSend,journal:()=>reminderJournal,sourceAuthority}):undefined;
    const contactsCommand=contactsTools&&config.bridge?createContactsCommand(contactsTools,{bridgeUrl:config.bridge.url,readBearer:config.bridge.readBearer,fetch:dependencies.fetch,sourceAuthority}):undefined;
    const reminderTools = ownerReminderTool ? createOwnerReminderTools({client: ownerReminderTool,
      journal: () => reminderJournal, sourceAuthority, readLoops: ownerTaskTool?.readLoops}) : {};
    const taskCommand=ownerTaskTool?createTaskCommand(createOwnerTaskExecution({client:ownerTaskTool,journal:()=>reminderJournal,sourceAuthority})):undefined;
    const taskTools = ownerTaskTool ? createOwnerTaskTools({client: ownerTaskTool,
      journal: () => reminderJournal, sourceAuthority}) : {};
    const preferenceTools = ownerPreferenceTool ? createOwnerPreferenceTools({client: ownerPreferenceTool,
      journal: () => reminderJournal, sourceAuthority}) : {};
    const ownerSkillRequest = config.channel === 'photon' && config.ownerLoop && config.bridge
      ? createOwnerSkillRequestClient({bridgeUrl: config.bridge.url, bearer: config.ownerLoop.bearer, identity: config.ownerLoop.identity}) : undefined;
    const skillCommand = ownerSkillRequest ? createSkillCommand(createOwnerSkillExecution({client: ownerSkillRequest,
      journal: () => reminderJournal, sourceAuthority})) : undefined;
    const preferenceCommand = ownerPreferenceTool ? createPreferenceCommand({skillRoot: skillPackRoot,
      execution: createOwnerPreferenceExecution({client: ownerPreferenceTool, journal: () => reminderJournal, sourceAuthority})}) : undefined;
    const ownerWebRead = config.channel === 'photon' && config.ownerLoop && config.bridge
      ? createOwnerWebReadClient({bridgeUrl:config.bridge.url,bearer:config.ownerLoop.bearer,identity:config.ownerLoop.identity,fetch:dependencies.fetch}) : undefined;
    const webCommand = ownerWebRead ? createWebReadCommand({client:ownerWebRead,journal:()=>reminderJournal,sourceAuthority}) : undefined;
    const knowledgeCommand = config.bridge ? createKnowledgeCommand({bridgeUrl: config.bridge.url, readBearer: config.bridge.readBearer,
      fetch: dependencies.fetch, skillRoot: skillPackRoot, sourceAuthority, isCurrent: (runId, eventId) => Boolean(reminderJournal?.isRunCurrent(runId) &&
        reminderJournal.getRun(runId)?.event_id === eventId)}) : undefined;
    const unattendedKnowledge = config.bridge ? createKnowledgeCommand({mode:'unattended',
      bridgeUrl:config.bridge.url,readBearer:config.bridge.readBearer,fetch:dependencies.fetch,sourceAuthority}) : undefined;
    const styleReadTool = knowledgeCommand ? createStyleReadTool(knowledgeCommand) : undefined;
    const unattendedCommand = unattendedKnowledge ? createRunScriptTool({mode:'unattended',
      commandGuide:'Only loops-query with [] or ["--parked"] is enabled: current consent-filtered canonical obligations. No mutations, jobs, research or other commands.',
      commandContext:(context,signal)=>({mode:context?.get('mode'),runId:context?.get('runId'),sourceRevision:context?.get('sourceRevision'),signal}),
      executeCommand:unattendedKnowledge}) : undefined;
    const unattendedGoogle = config.bridge ? createGoogleReadTools({bridgeUrl:config.bridge.url,
      readBearer:config.bridge.readBearer,fetch:dependencies.fetch,sourceAuthority},'unattended') : {};
    const gmailDraftTools = ownerGmailDraft ? createOwnerGmailDraftTools({client: ownerGmailDraft,
      journal: () => reminderJournal, sourceAuthority}) : {};
    const gmailSendCommand=ownerGmailDraft?createGmailSendCommand(gmailDraftTools as ReturnType<typeof createOwnerGmailDraftTools>):undefined;
    const contactsCommandIds=['google-action',...(skillPackRoot?[path.join(skillPackRoot,'_shared/scripts/google_action.py')]:[])];
    const sharedCommands = (ownerPreferenceTool || ownerSkillRequest || knowledgeCommand) ? (granolaGranted: boolean) => createRunScriptTool({mode: 'chat',contactsCommandIds,
      commandGuide: [
        ...(gmailSendCommand?['google-action: gmail-send --to EMAIL [--subject SUBJECT] --body BODY presents the full new-email send review; gmail-send-confirm with no flags sends only after a distinct owner yes. No saved draft or invented reply thread.']:[]),
        ...(contactsCommand?['google-action: capabilities [] (original local capability flags); contacts-search --query TEXT; contacts-get --resource-name people/cID; contacts-create --fields-json JSON; contacts-update --resource-name people/cID --fields-json JSON; contacts-delete --resource-name people/cID. All writes review first; confirm-contacts empty after distinct yes.']:[]),
        ...(calendarCommand?['google-action: calendar-update --event-id ID [--calendar EXACT_KNOWN_ID] --fields-json JSON reviews title (summary), location and agenda (description) edits to an existing event; calendar-update-confirm with no flags after a distinct yes. For combined time and detail changes, include both start/end objects with dateTime and selected details in fields JSON; this reviews one change, then calendar-move-update-confirm with no flags after a distinct yes. Original calendar-rsvp --event-id ID --response accepted|declined|tentative [--calendar EXACT_KNOWN_ID] reviews an invitation response; calendar-rsvp-confirm takes no flags after a distinct yes. --comment is unsupported. Time-only start/end fields JSON uses the existing move review, then empty calendar-move-confirm after a distinct yes; typed calendar_move_review/confirm also remains available. Never split mixed time and detail changes into separate writes.']:[]),
        ...(knowledgeCommand ? ['knowledge-query: --topic SUBJECT_KEYWORDS alone reads bounded permitted recorded obligations/outcomes including closed items without a people cohort; explicit --relevant-days N selects the people/cohort read, with --topic ranking its facts; [] discovers saved people, or --person NAME with optional --person-coverage, --editable-person, --topic TEXT, --relevant-days 1..365; --company NAME_OR_DOMAIN reads saved company details, with optional --company-page NEXT_CURSOR; master-file: ["get"]; loops-query: [] or ["--parked"]; retune-scan: [] (original stale-loop scan over currently permitted sources; no automatic cleanup); relationship-importance: [] or ["--person", NAME]; relationship-attention: [] (saved suggestions, freshness unknown); triage-queue: []; style-apply: [JSON request with recipient, channel email|imessage|whatsapp, optional canonical_id and context work|personal|unknown].'] : []),
        ...(knowledgeCommand && granolaGranted ? ['granola-history: [] or --days 1..90 and optional --meeting-id ID. Current permission allows this read; connector availability and coverage still come from its result.'] : []),
        ...(preferenceCommand ? ['usefulness-feedback: list or useful/not_useful --reference REF [--excerpt TEXT] [--reason TEXT]; preferences: show, mute-sender/unmute-sender, mute-person/unmute-person, mute-section/unmute-section, tone/remove-tone, vip/unvip VALUE, clear-tone, nudge-budget fewer/no/more, snooze-nudges EXACT_OWNER_TIME_SPEC, unsnooze-nudges (explicit resume nudges only); master-file: ["accept-offer"], ["append","--section","Procedures","--text",EXACT_OWNER_RULE] (review only), ["confirm-procedure"] (distinct later yes); ["set","--section","Priorities","--text",EXACT_COMPLETE_SET] (one to three lines, review only), ["confirm-priorities"] (distinct later yes); ["append","--section","About" or "People","--text",EXACT_OWNER_LINE] (review only), ["confirm-context","--section",SAME_SECTION] (distinct later yes); ["prioritize","--text","Staying in touch with "+EXACT_NAME] (only exact current owner prioritize command, no second yes).'] : []),
        ...(skillCommand ? ['meeting-prep: [] or ["--focus", RESOLVED_TARGET]; for one specific currently read calendar occurrence also supply optional typed meeting {event_id,start,calendar_id?} on run_sotto_script; preserve calendar_id from the current Calendar read when supplied; ask-research: ["--person", OWNER_WORDS] or ["--company", RESOLVED_COMPANY_OR_BARE_DOMAIN] with optional "--website" for homepage retrieval; followup: [] or ["--backlog"]; proactive: [] (original urgent check, silent successful admission); relationship-pulse: [] (queues the complete original deterministic pulse; stop after queued receipt).'] : []),
        ...(taskCommand ? ['knowledge-edit: loop-add --text OWNER_SPAN [--direction you|them] [--contact OWNER_SPAN] [--identifier OWNER_SPAN] (explicit private task, no second yes); add-identifier --slug CID --identifier OWNER_SPAN makes review, then ["--op","confirm-identifier"] on distinct yes; exact correction argv from the correction guide, relation-add/remove --slug CID --other-slug OTHER --type TYPE [--date DATE add only] makes review, then ["--op","confirm-relation"] on distinct yes; retune-apply: ["list"] makes the canonical numbered list; ["cleanup", JSON({items})] applies explicit numbered owner clauses from the immediately prior accepted list; keep --anchor A --loop-version V --loop-identity I --correction OWNER_WORDS; snooze --anchor A --loop-version V --loop-identity I --correction OWNER_WORDS --days POSITIVE_DECIMAL.'] : []),
        ...(webCommand ? ['web-read: ["--url", EXACT_URL] from the current owner message, or the single link in its available explicit Sotto reply target when there is no current link; docsend-fetch: ["--url", EXACT_DECK_URL] with optional ["--passcode", OWNER_VALUE] and --fresh (trusted review first); confirm-docsend: [] after distinct yes.'] : []),
      ].join(' '),
      commandContext: (context, signal) => ({mode: context?.get('mode'), runId: context?.get('runId'),
        ownerRequestKey: context?.get('ownerRequestKey'), allowOwnerPreference: context?.get('allowOwnerPreference'),allowOwnerTask:context?.get('allowOwnerTask'), allowOwnerSkill: context?.get('allowOwnerSkill'), allowOwnerGmailDraft: context?.get('allowOwnerGmailDraft'),
        sourceRevision: context?.get('sourceRevision'), usefulnessList:context?.get('usefulnessList'),setUsefulnessList:(value:unknown)=>context?.set('usefulnessList',value), standingContext: context?.get('standingContext'), markQueued: () => context?.set('ownerSkillQueued', true), markPreferenceSettled: () => context?.set('ownerPreferenceSettled', true), signal}),
      executeCommand: async (invocation, context) => {
        if(contactsCommandIds.includes(invocation.id)){
          if(['gmail-send','gmail-send-confirm'].includes(invocation.args[0]??'')){
            if(!gmailSendCommand)throw new Error('Gmail send unavailable');
            return gmailSendCommand({...invocation,id:'google-action'},context);
          }
          if(['calendar-update','calendar-update-confirm','calendar-move-update-confirm','calendar-rsvp','calendar-rsvp-confirm','calendar-move-confirm'].includes(invocation.args[0]??'')){
            if(!calendarCommand)throw new Error('Calendar unavailable');
            return calendarCommand({...invocation,id:'google-action'},context);
          }
          if(!contactsCommand)throw new Error('Contacts unavailable');return contactsCommand({...invocation,id:'google-action'},context);
        }
        if(invocation.id==='knowledge-edit'||invocation.id==='retune-apply'){if(!taskCommand)throw new Error('Correction unavailable');return taskCommand(invocation,context);}
        if(['web-read','docsend-fetch','confirm-docsend'].includes(invocation.id)||skillPackRoot&&invocation.id===path.join(skillPackRoot,'_shared/scripts/docsend_fetch.py')){if(!webCommand)throw new Error('URL read is unavailable');return webCommand({...invocation,id:invocation.id==='web-read'||invocation.id==='confirm-docsend'?invocation.id:'docsend-fetch'},context);}
        if (isStyleApplyCommand(invocation.id, skillPackRoot) || invocation.id === 'triage-queue' || invocation.id === 'knowledge-query' || invocation.id === 'loops-query' || invocation.id === 'retune-scan' || invocation.id === 'granola-history' || invocation.id === 'relationship-importance' || invocation.id === 'relationship-attention' || (invocation.id === 'master-file' && invocation.args.length === 1 && invocation.args[0] === 'get')) {
          if (!knowledgeCommand) throw new Error('Knowledge read is unavailable');
          return knowledgeCommand(invocation, context);
        }
        if ((invocation.id === 'meeting-prep' || invocation.id === 'ask-research' || invocation.id === 'followup' || invocation.id === 'relationship-pulse' || invocation.id === 'proactive')) {
          if (!skillCommand) throw new Error('Shared skill execution is unavailable');
          const result = await skillCommand(invocation, context);
          if (result.result.status === 'queued') context.markQueued();
          return result;
        }
        if (!preferenceCommand) throw new Error('Preference execution is unavailable');
        const result = await preferenceCommand(invocation, context);
        if ('status' in result.result && ['proposed', 'recorded', 'rejected'].includes(result.result.status)) context.markPreferenceSettled();
        return result;
      },
    }) : undefined;
    const routineTools: ReturnType<typeof createOwnerRoutineTools> | undefined = ownerRoutineTool
      ? createOwnerRoutineTools({client: ownerRoutineTool, journal: () => reminderJournal, sourceAuthority}) : undefined;
    const bridgeTools = bridge ? {
      chat: async (sourceRevision?: string, listingAuthority?: SourceAuthority) => {
        // The owner-only receiver projection is independent of Bridge source
        // discovery. Discovery failure grants no source tools, but must not
        // erase this separately authenticated, read-only tool.
        let sourceTools: Record<string, Tool> = {};
        const current = listingAuthority ?? await readAuthority(sourceAuthority);
        // Health describes connectivity even when every local source is disabled.
        // Per-tool filtering below still withholds all content reads without grants.
        if (current.complete && current.revision === sourceRevision) {
          try { sourceTools = await bridge!.chatTools({ sourceRevision, listingAuthority: current }); } catch { /* no Bridge source tools */ }
        }
        const googleTools: Record<string, Tool> = {};
        if (current.complete && current.revision === sourceRevision) {
          for (const [name, tool] of Object.entries(googleReadTools)) {
            if (name==='capabilities'||current.google_sources.includes(name.startsWith('gmail_') ? 'gmail' : name.startsWith('contacts_')?'google_contacts':'calendar'))
              googleTools[name] = tool as unknown as Tool;
          }
        }
        const permittedDraftTools = current.complete && current.revision === sourceRevision &&
          current.google_sources.includes('gmail') ? gmailDraftTools : {};
        // Mastra's Tool default input generic is invariant; the concrete
        // schema remains on the tool itself for model-call validation.
        return { ...(sharedCommands ? {run_sotto_script: sharedCommands(current.complete && current.revision === sourceRevision && current.connector_sources?.includes('granola') === true) as unknown as Tool} : {}), ...(styleReadTool ? {read_writing_style: styleReadTool as unknown as Tool} : {}), ...sourceTools, ...googleTools, ...((bridgeSendTools ?? {}) as unknown as Record<string, Tool>), ...reminderTools, ...briefTools, ...taskTools, ...preferenceTools, ...((routineTools ?? {}) as unknown as Record<string, Tool>), ...permittedDraftTools, ...(current.complete&&current.revision===sourceRevision&&current.google_sources.includes('google_contacts')?(contactsTools??{}) as unknown as Record<string,Tool>:{}), ...(current.complete&&current.revision===sourceRevision&&current.google_sources.includes("calendar")?calendarTools:{}), ...(current.complete&&current.revision===sourceRevision&&current.google_sources.includes('calendar')?ownerMeetingPrepOfferTools:{}), owner_open_loops: ownerLoopsTool! as unknown as Tool,
          owner_stale_loops: ownerRetuneTool! as unknown as Tool,
          ...(briefDetailTool ? { accepted_brief_detail: briefDetailTool as unknown as Tool } : {}) };
      },
      unattended: async (sourceRevision?: string) => {
        const current = await readAuthority(sourceAuthority);
        const sourceTools = current.complete && current.revision === sourceRevision && current.bridge_sources.length
          ? await bridge!.unattendedTools({ sourceRevision }) : {};
        const googleTools: Record<string, Tool> = {};
        const sourceCurrent = current.complete && current.revision === sourceRevision;
        if (sourceCurrent) for (const [name, tool] of Object.entries(unattendedGoogle)) {
          if (current.google_sources.includes(name.startsWith('gmail_') ? 'gmail' : 'calendar'))
            googleTools[name] = tool as unknown as Tool;
        }
        return {
          ...sourceTools, ...googleTools,
          ...(sourceCurrent && unattendedCommand ? {run_sotto_script:unattendedCommand as unknown as Tool} : {}),
          ...(ownerLoopsTool ? {
            owner_open_loops: createOwnerLoopsTool({
              bridgeUrl: config.bridge!.url, readBearer: config.bridge!.readBearer, fetch: dependencies.fetch,
            }, 'unattended') as unknown as Tool,
          } : {}),
        };
      },
    } : undefined;
    const { ownerLoop: _privateOwnerCommand, ...baseAgentConfig } = config;
    const effectiveInstructions = effectiveCapabilityInstructions(baseAgentConfig.instructions, {
      task: Object.hasOwn(taskTools, 'owner_task_add'), conditionalReminder: Object.hasOwn(reminderTools, 'owner_reminder_conditional'),
      routine: Object.hasOwn(routineTools ?? {}, 'owner_routine_propose'), preference: Boolean(preferenceCommand && Object.hasOwn(preferenceTools, 'owner_preference_change')),
      bridgeSend: Boolean(bridgeSendTools), gmail: Object.hasOwn(gmailDraftTools, 'gmail_draft_propose'), calendar: Object.hasOwn(calendarTools, 'calendar_create_review'),
    });
    const answerOnlyInstructions = effectiveInstructions + '\n\n' + GENERAL_CHAT_VOICE_GUIDE;
    const agentConfig = {...baseAgentConfig, instructions: answerOnlyInstructions + (Object.hasOwn(taskTools, 'owner_task_list')?'\n\n'+OWNER_TASK_READBACK_GUIDE:'') + (ownerContacts?'\n\n'+CONTACTS_EXECUTION_GUIDE:'') + (ownerCalendar?'\n\n'+CALENDAR_EXECUTION_GUIDE:'') + (ownerMeetingPrepOffer?'\n\n'+MEETING_PREP_OFFER_GUIDE:'') + (briefDetailTool ? '\n\n'+BRIEF_DETAIL_EXECUTION_GUIDE : '') + (ownerGmailDraft ? '\n\n' + GMAIL_DRAFT_EXECUTION_GUIDE : '') +
      (sharedCommands ? '\n\n' + (webCommand ? WEB_READ_EXECUTION_GUIDE + '\n\n' : '') + (knowledgeCommand ? KNOWLEDGE_EXECUTION_GUIDE + '\n\n' + MASTER_CONTEXT_EXECUTION_GUIDE + '\n\n' + LOOPS_EXECUTION_GUIDE + '\n\n' + RETUNE_EXECUTION_GUIDE + '\n\n' + GRANOLA_HISTORY_EXECUTION_GUIDE + '\n\n' + RELATIONSHIP_IMPORTANCE_EXECUTION_GUIDE + '\n\n' + RELATIONSHIP_ATTENTION_EXECUTION_GUIDE + '\n\n' + TRIAGE_EXECUTION_GUIDE + '\n\n' + STYLE_APPLY_EXECUTION_GUIDE + '\n\n' : '') + (preferenceCommand ? SKILL_COMMAND_EXECUTION_GUIDE : '') + (taskCommand ? '\n\n'+TASK_CORRECTION_EXECUTION_GUIDE:'') + (preferenceCommand ? '\n\n' + PROCEDURE_EXECUTION_GUIDE + '\n\n'+USEFULNESS_EXECUTION_GUIDE : '') + (skillCommand ? '\n\n' + SHARED_SKILL_EXECUTION_GUIDE : '\nMeeting-prep shared execution is unavailable in this runtime configuration.') : '')};
    let latestTaskToolDiagnostics: TaskToolDiagnosticBatch | undefined;
    let latestChatTiming: ChatTiming | undefined;
    let latestModelMetrics: ModelMetrics | undefined;
    let chatTimingCompletedAtMs: number | undefined;
    let chatTimingOutcome: 'success' | 'failed' | undefined;
    agent = dependencies.createAgent?.(agentConfig, proxy.chatModel, proxy.generateNativeMedia, skillPaths, bridgeTools, sourceAuthority) ?? defaultAgent(
      agentConfig, proxy.chatModel, proxy.generateNativeMedia, skillPaths, bridgeTools, sourceAuthority,
      knowledgeCommand ? async context => (await knowledgeCommand({id:'master-file',args:['get'],mode:'chat'},context)).result as {text:string;chars:number;cap:number;prompt:string} : undefined,
      (timing, completedAtMs, outcome) => { latestChatTiming = timing; chatTimingCompletedAtMs = completedAtMs; chatTimingOutcome = outcome; },
      batch => { latestTaskToolDiagnostics = batch; },
      metrics => { latestModelMetrics = metrics; },
      answerOnlyInstructions,
    );
    retainedState.agent = agent;
    retainedState.bridge = bridge;

    // The captured provider thread is immutable. Reuse one hydrator so a Photon
    // SDK lookup that outlives a cancelled turn continues to occupy its one slot.
    // This closure initializes lazily because recovery runs before startApplication
    // returns; application validates the context against its persisted binding.
    let hydrationThread: string | undefined;
    let hydrateBound: ReturnType<typeof createInboundMediaHydrator> | undefined;

    application = await startApplication({
      directory: config.stateDirectory,
      mediaRoot: config.mediaRoot,
      bearerToken: config.bearerToken,
      owner: { ...config.owner, channel: config.channel },
      lineKey: config.lineKey,
      target: config.target,
      providerThreadId: config.providerThreadId,
      port: config.port,
      deferListen: true,
      allowedSkills: new Set(config.allowedSkills),
      activation: activationIdentityForConfig(config),
      discoverySeed: activationDiscoverySeedForConfig(config),
      configuredChannelIdentity: configuredChannelIdentityForConfig(config),
    }, {
      agent,
      transport,
      resolvePrepContinuation: config.channel === 'photon'
        ? createPhotonPrepContinuation(credentials, config.owner.ownerId, thread => adapter.decodeThreadId!(thread)) : undefined,
      hydrateInbound: (messages, context) => {
        if (hydrationThread !== undefined && hydrationThread !== context.threadId) {
          throw new Error('provider media thread changed');
        }
        if (config.providerThreadId && context.threadId !== config.providerThreadId) {
          throw new Error('provider media thread does not match configuration');
        }
        if (!hydrateBound) {
          hydrationThread = context.threadId;
          hydrateBound = createInboundMediaHydrator({ channel: config.channel,
            owner: { ...config.owner, channel: config.channel }, providerThreadId: context.threadId,
            adapter, credentials, fetch: dependencies.fetch });
        }
        return hydrateBound(messages, context);
      },
      startTurnPresence: presence?.startTurnPresence.bind(presence),
      onOwnerAdmission: feedback?.admitted.bind(feedback),
      onOwnerReplyAccepted: feedback?.accepted.bind(feedback),
      onOwnerSilentWorkAccepted: feedback?.workAccepted.bind(feedback),
      onOwnerTaskTiming: timing => {console.info('[sotto-task-timing]',JSON.stringify(timing));},
      feedbackStatus: feedback?.snapshot.bind(feedback),
      taskToolDiagnostics: () => latestTaskToolDiagnostics,
      modelMetrics: () => latestModelMetrics,
      chatTiming: () => latestChatTiming,
      chatTimingCompletedAtMs: () => chatTimingCompletedAtMs,
      chatTimingOutcome: () => chatTimingOutcome,
      listen: dependencies.listenApplication,
      providerLifecycle: () => gateway?.lifecycle ?? 'starting',
      providerIntake: () => gateway?.intake ?? {receivedEvents: 0, handedOffEvents: 0},
      pruneExpiredState: async () => gateway?.pruneExpiredState() ?? 0,
      ownerLoop,
      ownerReminder,
      ownerQuiet,
      ownerReminderTool,
      ownerTaskTool,
      ownerPreferenceTool,
      ownerSkillRequest,
      ownerWebRead,
      ownerRoutineTool,
      confirmSwipeRoutine: routineTools ? async request => {
        const tool = request.operation === 'adjust' ? routineTools.owner_routine_adjust_confirm : routineTools.owner_routine_create;
        const requestContext = new RequestContext();
        for (const [key, value] of Object.entries({mode:'chat',runId:request.runId,ownerRequestKey:request.eventId,
          sourceRevision:request.sourceRevision,allowOwnerRoutine:true})) requestContext.set(key,value);
        await tool!.execute!({} as never,{requestContext,abortSignal:request.signal} as never);
      } : undefined,
      ownerGmailDraft,
      ownerCalendar,
      ownerContacts,
      ownerBridgeSend,
      ownerBriefRequest,
      ownerMeetingPrep,
      ownerMeetingPrepOffer,
      ownerOfferReply,
      onJournalReady: journal => {reminderJournal = journal;},
    }, async () => { /* bootstrap releases only after every store closes */ });
    retainedState.application = application;

    const dbPath = path.join(config.stateDirectory, 'chat-sdk.sqlite');
    chatDatabase = dependencies.openStateDatabase?.(dbPath) ?? new DatabaseSync(dbPath);
    retainedState.chatDatabase = chatDatabase;
    const activeGateway = await startOwnerGateway({
      manifest: {
        channel: config.channel,
        accountId: config.owner.accountId,
        ownerId: config.owner.ownerId,
        lineKey: config.lineKey,
        providerThreadId: config.providerThreadId,
        expectedProviderLine: config.channel === 'photon' && config.firstContact?.mode === 'owner_dm' ? config.firstContact.expectedProviderLine : undefined,
        sharedProjectRoute: config.channel === 'photon' && config.firstContact?.mode === 'owner_dm_shared_project',
        credentials,
      },
      application,
      quiescePresence: () => Promise.all([presence?.close(), feedback?.close()]).then(() => undefined),
      database: chatDatabase,
      adapter,
    });
    gateway = activeGateway;
    retainedState.gateway = gateway;
    gatewayStopped = false;
    // No externally reachable HTTP intake or sends until provider adapter initialization succeeds.
    await application.listen();

    let closing: Promise<void> | undefined;
    const close = (): Promise<void> => closing ??= (async () => {
      let gatewayError: unknown;
      try { await activeGateway.close(); gatewayStopped = true; }
      catch (error) {
        gatewayError = error;
        gatewayStopped = activeGateway.providerStopped;
      }
      if (!gatewayStopped || gatewayError) {
        retainedRuntimeStates.add(retainedState);
        throw gatewayError ?? new Error('Provider shutdown is unconfirmed; runtime ownership retained');
      }
      let databaseError: unknown;
      try { chatDatabase?.close(); } catch (error) { databaseError = error; }
      let bridgeError: unknown;
      try { await bridge?.close(); } catch (error) { bridgeError = error; }
      let agentError: unknown;
      try { await agent?.close(); } catch (error) { agentError = error; }
      const errors = [gatewayError, databaseError, bridgeError, agentError].filter(Boolean);
      if (errors.length) {
        retainedRuntimeStates.add(retainedState);
        throw new AggregateError(errors, 'Runtime stopped with cleanup errors; ownership retained');
      }
      try { await release(); }
      catch (error) {
        retainedRuntimeStates.add(retainedState);
        throw error;
      }
    })();
    return {
      get port() { return application!.port; },
      gateway: activeGateway,
      rotateSessions: () => application!.rotateSessions(),
      close,
    };
  } catch (error) {
    await Promise.all([presence?.close(), feedback?.close()]);
    let gatewayCloseError: unknown;
    if (gateway) {
      try { await gateway.close(); gatewayStopped = true; }
      catch (cleanupError) { gatewayCloseError = cleanupError; gatewayStopped = gateway.providerStopped; }
    }
    const startupProviderStopped = error instanceof GatewayStartupError ? error.providerStopped : true;
    const safeToCloseStores = gateway ? gatewayStopped : startupProviderStopped;
    if (!safeToCloseStores) {
      retainedRuntimeStates.add(retainedState);
      throw error;
    }
    const cleanupErrors: unknown[] = [];
    if (gatewayCloseError) cleanupErrors.push(gatewayCloseError);
    try { await application?.close(); }
    catch {
      retainedRuntimeStates.add(retainedState);
      throw error;
    }
    if (gatewayCloseError) {
      retainedRuntimeStates.add(retainedState);
      throw error;
    }
    try { chatDatabase?.close(); } catch (cleanupError) { cleanupErrors.push(cleanupError); }
    try { await bridge?.close(); } catch (cleanupError) { cleanupErrors.push(cleanupError); }
    try { await agent?.close(); } catch (cleanupError) { cleanupErrors.push(cleanupError); }
    if (!cleanupErrors.length) {
      try { await release(); } catch (cleanupError) { cleanupErrors.push(cleanupError); }
    }
    if (cleanupErrors.length) retainedRuntimeStates.add(retainedState);
    throw error;
  }
}

/** Install graceful signal handlers with a hard upper bound for non-cooperative tasks.
 * SIGUSR2 is reserved for the root supervisor: its shutdown does not trigger
 * third-party SIGTERM handlers that can exit before provider stop is proved.
 */
export async function waitForShutdownSignal(runtime: RuntimeHandle, timeoutMs = 30_000): Promise<void> {
  await new Promise<void>((resolve) => {
    let stopping = false;
    const onSignal = () => {
      if (stopping) return;
      stopping = true;
      process.removeListener('SIGINT', onSignal);
      process.removeListener('SIGTERM', onSignal);
      process.removeListener('SIGUSR2', onSignal);
      const timer = setTimeout(() => {
        process.stderr.write('Sotto runtime shutdown exceeded its limit; exiting with crash evidence preserved for recovery.\n');
        process.exit(1);
      }, timeoutMs);
      void runtime.close().then(() => {
        clearTimeout(timer);
        resolve();
      }, () => {
        process.exitCode = 1;
        // If a provider poller could not be stopped, exiting is safer than leaving a
        // second live process behind. Process exit releases the kernel ownership lock.
        process.exit(1);
      });
    };
    process.once('SIGINT', onSignal);
    process.once('SIGTERM', onSignal);
    process.once('SIGUSR2', onSignal);
  });
}

async function cli(argv: string[]): Promise<void> {
  const checkOnly = argv[0] === '--check';
  const configFile = checkOnly ? argv[1] : argv[0] === '--config' ? argv[1] : undefined;
  if (!configFile || argv.length !== (checkOnly || argv[0] === '--config' ? 2 : 1)) {
    process.stderr.write('Usage: node dist/bootstrap/main.js --check <private-config.json> | --config <private-config.json>\n');
    process.exitCode = 2;
    return;
  }
  try {
    if (checkOnly) {
      const config = await loadRuntimeConfig(configFile);
      const skillPackRoot = skillPackRootForConfig(config);
      if (skillPackRoot) await readSkillIndex(skillPackRoot);
      process.stdout.write(`Configuration valid for ${config.channel}; no provider or model connection was opened.\n`);
      return;
    }
    const runtime = await startConfiguredRuntime(configFile);
    process.stdout.write(`Sotto runtime initialized on 127.0.0.1:${runtime.port}. Provider event flow is not observed.\n`);
    await waitForShutdownSignal(runtime);
  } catch {
    process.stderr.write('Sotto runtime startup or shutdown failed; no secret-bearing error details were printed.\n');
    process.exit(1);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  void cli(process.argv.slice(2));
}

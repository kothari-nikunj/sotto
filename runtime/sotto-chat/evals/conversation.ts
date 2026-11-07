/** Existing production conversation harness with actual-model and explicit scripted contract modes. */
import {createPhotonTextFormatter} from '../src/channels/photon-text-formatter.js';
import {startNativePrepRelay} from './native-prep-relay.js';
import {claimsQueuedWork, deliveredArtifactText, prepExecutionMatches, chatPrepOfferMatches} from './assertions.js';
import {measureTurn, summarizeMeasurements} from './measurements.js';
import {observeReceiverJson,receiverObservationErrors,type ReceiverObservation} from './receiver-observation.js';
import {assertScriptedEndpoint,createScriptedProvider,scriptedProviderSchema} from './scripted-provider.js';
import {execFileSync, spawn} from 'node:child_process';
import {createHash, randomBytes} from 'node:crypto';
import {mkdir, readFile, writeFile, chmod, rm} from 'node:fs/promises';
import {createInterface} from 'node:readline';
import {resolve, dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {acceptBackgroundFixture,conversationInbound} from './conversation-fixtures.js';
import {z} from 'zod';
import {DatabaseSync} from 'node:sqlite';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import type {GatewayAdapter} from '../src/channels/gateway.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {ownerTaskToolReceiptSchema} from '../src/tools/owner-task-tool-contract.js';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const turnSchema=z.object({text:z.string().min(1).max(16000),contains:z.array(z.string()).optional(),
  latencyClass:z.enum(['ordinary','evidence','brief']).optional(),
  containsAny:z.array(z.array(z.string()).min(1)).optional(),excludes:z.array(z.string()).optional(),
  minReplies:z.number().int().min(1).optional(),restartBefore:z.boolean().optional(),
  maxCharacters:z.number().int().positive().optional(),websiteReads:z.number().int().nonnegative().optional(),researchReads:z.number().int().nonnegative().optional(),publicReads:z.number().int().nonnegative().optional(),
  contactReads:z.number().int().nonnegative().optional(),googleReads:z.number().int().nonnegative().optional(),
  taskStates:z.record(z.string(),z.enum(['open','resolved'])).optional(),
  taskRecords:z.array(z.object({title:z.string().min(1),status:z.enum(['open','resolved'])}).strict()).optional(),
  taskAddReceipts:z.array(z.string().min(1)).optional(),
  bridgeSends:z.number().int().nonnegative().optional(),
  backgroundBefore:z.string().max(16000).optional(),
  quoteBackgroundFromTurn:z.number().int().nonnegative().optional(),
  chatPrepOffer:z.object({eventId:z.string().min(1)}).strict().optional(),
  prepJobCount:z.number().int().nonnegative().optional(),
  briefPrepOfferBefore:z.object({eventId:z.string().min(1),ownerText:z.string().min(1).max(2000).optional()}).strict().optional(),
  meetingPrepOfferBefore:z.object({eventId:z.string().min(1),person:z.string().min(1),question:z.string().min(1).max(16000)}).strict().optional(),
  resetConversationBefore:z.boolean().optional(),
  canonicalUnchanged:z.boolean().optional(),
  meetingPrep:z.object({eventId:z.string().min(1),newJob:z.boolean(),artifactContains:z.array(z.string().min(1)).min(1)}).strict().optional(),
  receiverBefore:z.array(z.object({journey:z.enum(['learning','commitment']),stage:z.string().min(1),scenario:z.enum(['email-to-text','acknowledgment','partial','noncompletion','revoked-access']).optional()}).strict()).optional(),
  returnedEvidenceContains:z.array(z.string()).optional(),
  knowledgeQueryReads:z.number().int().nonnegative().optional(),
  briefDetailReads:z.number().int().nonnegative().optional(),
  maxBriefDetailReads:z.number().int().nonnegative().optional(),
  returnedBriefDetailContains:z.array(z.string()).optional(),
  returnedBriefDetailExcludes:z.array(z.string()).optional(),
  returnedEvidenceExcludes:z.array(z.string()).optional()}).strict();
const scenarioSchema=z.object({name:z.string().min(1),world:z.record(z.string(),z.unknown()),
  turns:z.array(turnSchema).min(1).max(30),modelFailureAt:z.array(z.number().int().positive()).optional()}).strict();
const args = process.argv.slice(2);
const value = (key: string) => {const i = args.indexOf(key); if (i < 0 || !args[i + 1]) throw new Error(`Required ${key}`); return args[i + 1]!;};
const scriptedFile=args.includes('--scripted-provider')?resolve(value('--scripted-provider')):undefined;
if(Boolean(scriptedFile)===args.includes('--model-config'))throw new Error('Supply exactly one --model-config or --scripted-provider');
const modelFile = scriptedFile?undefined:resolve(value('--model-config'));
const scenarioFile = resolve(value('--scenario'));
const output = resolve(value('--output'));
// Separate, owner-created credentials file, never a production runtime configuration.
const script=scriptedFile?scriptedProviderSchema.parse(JSON.parse(await readFile(scriptedFile,'utf8'))):undefined;
const scripted=script?createScriptedProvider(script):undefined;
const model = (modelFile?JSON.parse(await readFile(modelFile, 'utf8')):{instructions:script!.instructions,modelProxy:{origin:'https://scripted-model.invalid',bearer:'synthetic-scripted-bearer',chatModelId:'synthetic-scripted-provider',nativeMediaModelId:'synthetic-scripted-provider'}}) as {modelProxy: {origin:string;bearer:string;chatModelId:string;nativeMediaModelId:string};instructions:string};
const scenario = scenarioSchema.parse(JSON.parse(await readFile(scenarioFile, 'utf8')));
// Actual-model journeys must not silently retain scripted completion judgments.
const actualCommitment = !scripted && scenario.turns.some(turn=>turn.receiverBefore?.some(step=>step.journey==='commitment'));
if(actualCommitment)scenario.world.commitmentJourney={...((scenario.world.commitmentJourney as object)??{}),actualModel:true};
const taskCreationScenario=scenario.turns.some(turn=>turn.taskRecords!==undefined||turn.taskAddReceipts!==undefined);
if(taskCreationScenario&&(!Array.isArray(scenario.world.loopTasks)||scenario.turns.some(turn=>!turn.taskRecords||!turn.taskAddReceipts)))
  throw new Error('Task creation scenario requires every canonical record and exact add receipts after every turn');
if (!model.modelProxy || !model.instructions) throw new Error('Explicit production model and base instructions required');
await mkdir(output, {mode:0o700}); // Refuse reuse: never accidentally run against existing state.
await chmod(output, 0o700);
const data = join(output, 'receiver'); await mkdir(data, {mode:0o700});
const home = join(output, 'home'); await mkdir(home, {mode:0o700});
const worldFile = join(output, 'world.json');
await writeFile(worldFile, JSON.stringify(scenario.world), {mode:0o600});
const child = spawn(process.env.SOTTO_PYTHON ?? 'python3', [join(here, 'receiver.py'), worldFile, data], {
  env:{PATH:process.env.PATH, HOME:home, PYTHONDONTWRITEBYTECODE:'1',
    SOTTO_DATA:data,SOTTO_HOME:join(home,'sotto'),SOTTO_RUNTIME_PROFILE:'managed-photon-fixed-five-v1',
    SOTTO_DEPLOYMENT_MODE:'managed', SOTTO_CRON_DELIVER:'photon', SOTTO_TENANT_ID:'eval-tenant',
    SOTTO_ACCOUNT_ID:'eval-account',SOTTO_LINE_KEY:'eval-line',SOTTO_TIMEZONE:'America/Los_Angeles',
    SOTTO_MCP_TOKEN:randomBytes(32).toString('hex'), PHOTON_HOME_CHANNEL:'+15555550123',
    PHOTON_ALLOWED_USERS:'+15555550123',SOTTO_IMESSAGE_NUMBER:'+15555550999',
    PHOTON_PROJECT_ID:'eval-project',PHOTON_PROJECT_SECRET:'eval-secret',PHOTON_ALLOW_ALL_USERS:'false'},
  stdio:['pipe','pipe','pipe'],
});
const pending: Array<{event:string;resolve:(v:any)=>void;reject:(e:Error)=>void}> = [];
const buffered: any[] = [];
let stderr = '';
let peerFailure:Error|undefined;
const peerFailed=(error:Error)=>{peerFailure=error;for(const p of pending.splice(0))p.reject(error);};
child.on('error',peerFailed);child.stdin.on('error',peerFailed);
child.stderr.on('data', b => {stderr = (stderr + b).slice(-16000);});
createInterface({input:child.stdout}).on('line', line => {
  let item; try {item = JSON.parse(line);} catch {return;}
  if (!item.event) return;
  const i = pending.findIndex(p => p.event === item.event);
  if (i >= 0) pending.splice(i,1)[0]!.resolve(item); else buffered.push(item);
});
child.on('exit', code => peerFailed(new Error(`Eval receiver exited ${code}: ${stderr}`)));
const wait = (event: string, timeout = 600000): Promise<any> => {
  if(peerFailure)return Promise.reject(peerFailure);
  const i = buffered.findIndex(v => v.event === event); if (i >= 0) return Promise.resolve(buffered.splice(i,1)[0]);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {const i = pending.indexOf(p); if (i >= 0) pending.splice(i,1); reject(new Error(`Timed out waiting for ${event}`));},timeout);
    const p = {event,resolve:(v:any)=>{clearTimeout(timer);resolve(v);},reject:(e:Error)=>{clearTimeout(timer);reject(e);}};
    pending.push(p);
  });
};
const command = async (op: string, more = {}) => {const result = wait(op === 'configure' ? 'configured' : op);child.stdin.write(JSON.stringify({op,...more})+'\n');return result;};
const sends: Array<{id:string;text:string;media:number;at:number;mediaSha256s?:string[];mediaPaths?:string[]}> = [];
const requests: Array<{path:string;status:number;ms:number;kind:'model'|'receiver';operation?:string}&ReceiverObservation> = [];
const unexpectedCalls:string[]=[];
const turns: unknown[] = [];
const composedBriefOffer=scenario.turns.some(turn=>Boolean(turn.briefPrepOfferBefore));
const seededPrepOffer=scenario.turns.some(turn=>Boolean(turn.meetingPrepOfferBefore||turn.briefPrepOfferBefore));
let briefCompositionCalls=0;
const chatPrepOffer=scenario.turns.some(turn=>Boolean(turn.chatPrepOffer));
if(composedBriefOffer&&scripted)throw new Error('Brief-origin prep acceptance requires the actual model');
if(chatPrepOffer&&seededPrepOffer)throw new Error('Natural chat offer cannot use seeded offer authority');
const expectedPrepJobs=Math.max(0,...scenario.turns.map(turn=>turn.prepJobCount??0));
let acceptedPrepOfferProof:any;
let nativePrepRelay:Awaited<ReturnType<typeof startNativePrepRelay>>|undefined;
let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
let failure: string | undefined;
const codeCommit=execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim();
const sourceTrace=async()=> (await readFile(join(output,'source-reads.jsonl'),'utf8').catch(()=>'' )).trim().split('\n').filter(Boolean).map(line=>JSON.parse(line));
const taskActions=()=>{
  const journal=new DatabaseSync(join(output,'runtime/operations.sqlite'),{readOnly:true});
  try{return journal.prepare("SELECT id,status,source_event_id,source_payload_hash,payload_hash,result_json FROM actions WHERE kind='owner_loop_tool' ORDER BY rowid").all()
    .map(row=>({...JSON.parse(String(row.result_json)),id:String(row.id),status:String(row.status),eventId:String(row.source_event_id),sourcePayloadHash:String(row.source_payload_hash),argsHash:String(row.payload_hash)}));}
  finally{journal.close();}
};
let requestCount = 0;
const loadedSkills=new Set<string>();
const originalFetch = globalThis.fetch;
try {
  const peer = await wait('ready',15000);
  const thread = peer.identity.route_id;
  if(script&&scenario.world.meetingPrep){
    // Resolve only explicit Calendar fixture timestamps after the receiver freezes its clock.
    const frozen=JSON.parse(await readFile(worldFile,'utf8'));
    const encoded=JSON.stringify(script).replace(/\{\{CALENDAR_START:([A-Za-z0-9_-]+)\}\}/g,(_token,id)=>{
      const event=frozen.calendarEvents?.find((row:any)=>row.id===id);
      if(typeof event?.start?.dateTime!=='string')throw new Error('Script Calendar timestamp lacks fixture occurrence');
      return event.start.dateTime;
    });
    Object.assign(script,scriptedProviderSchema.parse(JSON.parse(encoded)));
  }
  const adapter = {
    name:'imessage',userName:'sotto-eval',initialize:async()=>{},disconnect:async()=>{},
    startGatewayListener:async(options:{waitUntil(p:Promise<void>):void},_ms:number,signal:AbortSignal)=>{
      options.waitUntil(new Promise<void>(r=>signal.addEventListener('abort',()=>r(),{once:true})));return {ok:true,status:200};},
    isDM:(id:string)=>id===thread,channelIdFromThreadId:()=>thread,
    decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),
    encodeThreadId:()=>thread,fetchMessage:async()=>null,fetchMessages:async()=>({messages:[],hasMore:false}),
    addReaction:async()=>{},removeReaction:async()=>{},startTyping:async()=>{},renderFormatted:(x:unknown)=>String(x),
    postMessage:async()=>{throw new Error('Use capture transport');},
  } as unknown as GatewayAdapter;
  const config = {version:1,channel:'photon',deployment:'managed',tenantId:'eval-tenant',
    owner:{accountId:'eval-account',ownerId:'+15555550123'},lineKey:'eval-line',target:'photon',
    providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'eval-project',photonProjectSecret:'eval-secret'},
    stateDirectory:join(output,'runtime'),mediaRoot:join(output,'media'),port:0,
    bearerToken:randomBytes(32).toString('hex'),instructions:model.instructions,modelProxy:model.modelProxy,
    skillPackRoot:join(root,'sotto-chief-of-staff'),allowedSkills:[],turnPresence:false,
    bridge:{url:`http://127.0.0.1:${peer.port}/mcp`,readBearer:peer.readBearer},
    ownerLoop:{mode:'explicit_command',identity:peer.identity,bearer:peer.bearer}};
  await mkdir(config.mediaRoot,{mode:0o700});
  const configFile=join(output,'runtime.json');await writeFile(configFile,JSON.stringify(config),{mode:0o600});
  const guardedFetch: typeof fetch = async (input,init) => {
    const req=new Request(input,init),url=new URL(req.url);
    const local=url.origin===`http://127.0.0.1:${peer.port}` || (runtime && url.origin===`http://127.0.0.1:${runtime.port}`);
    if(!local && url.origin!==new URL(model.modelProxy.origin).origin) {unexpectedCalls.push(url.origin+url.pathname);failure='Unexpected external endpoint attempted';throw new Error(failure);}
    if(!local && ++requestCount>60) throw new Error('Eval model request limit reached');
    let body:any;
    if(!local) {
      body=await req.clone().json();
      for(const message of body.messages??[])for(const call of message.tool_calls??[]) {
        if(call.function?.name==='skill')try{loadedSkills.add(JSON.parse(call.function.arguments).name);}catch{/* not a skill load */}
      }
      await writeFile(join(output,`model-input-${requestCount}.json`),JSON.stringify(body,null,2),{mode:0o600});
    }
    const started=performance.now();
    // Record dispatch before awaiting I/O: a rejected/aborted fetch is still an attempt.
    const attempt:{path:string;status:number;ms:number;kind:'model'|'receiver';operation?:string}&ReceiverObservation={path:url.pathname,status:0,ms:0,kind:local?'receiver':'model'};
    if(local&&req.method==='POST'){const localBody=await req.clone().json().catch(()=>null);if(typeof localBody?.operation==='string')attempt.operation=localBody.operation;}
    requests.push(attempt);
    try {
      const nativeScripted=!local&&url.pathname==='/native/v1beta/models/gemini-3.8-flash:generateContent';
      if(!local&&scripted)assertScriptedEndpoint(req,nativeScripted);
      const response=!local && scenario.modelFailureAt?.includes(requestCount)
        ? Response.json({error:{message:'Injected transient provider failure',type:'upstream_error'}},{status:500})
        : !local&&scripted?(nativeScripted?scripted.respondNative(body):scripted.respond(body)):await originalFetch(req,{redirect:'error'});
      attempt.status=response.status;
      // Inspect only receiver evidence; model responses must reach the SDK without
      // waiting for an entire stream to finish. Unmeasured response sizes stay absent.
      if(local&&url.pathname.startsWith('/runtime/')&&response.headers.get('content-type')?.includes('application/json')) {
        const responseText=await response.clone().text();
        Object.assign(attempt,observeReceiverJson(responseText));
      }
      return response;
    } catch(error) {
      if(scripted&&!local){failure??=error instanceof Error?error.message:'Scripted provider failed';unexpectedCalls.push('scripted-provider-contract');}
      throw error;
    } finally { attempt.ms=Math.round(performance.now()-started); }
  };
  // The model and tool clients use this explicit fetch seam. No alternate agent construction.
  const start = () => startConfiguredRuntime(configFile,{createAdapter:()=>adapter,fetch:guardedFetch,
    createTransport:(_channel,_adapter,options)=>({send:async input=>{
      if(input.threadId!==thread) throw new Error('Eval destination mismatch');
      const id=`eval-out-${sends.length+1}`;
      const text=input.verbatim?input.text??'':await options?.formatText?.(input.text??'')??input.text??'';
      const captured: {mediaSha256s?:string[];mediaPaths?:string[]}={};
      if(scenario.world.meetingPrep&&input.media?.length){
        const folder=join(output,'delivered-media');await mkdir(folder,{recursive:true,mode:0o700});
        captured.mediaSha256s=[];captured.mediaPaths=[];
        for(const [index,media] of input.media.entries()){
          if(!media.data||media.mimeType!=='image/png'||media.data.length>5_000_000)throw new Error('Unknown prep media contract');
          const bytes=Buffer.from(media.data),path=join(folder,`${id}-${index+1}.png`);
          await writeFile(path,bytes,{mode:0o600});captured.mediaPaths.push(path);
          captured.mediaSha256s.push(createHash('sha256').update(bytes).digest('hex'));
        }
      }
      sends.push({id,text,media:input.media?.length??0,at:Date.now(),...captured});
      return {acceptance:'accepted',providerMessageIds:[id],providerSentAtMs:Date.now()};
    }})});
  if(scenario.world.meetingPrep||actualCommitment){
    if(scripted&&!script?.nativeResponses?.length)throw new Error('Synthetic prep machinery requires explicit native response fixtures; actual-model acceptance remains separate');
    nativePrepRelay=await startNativePrepRelay(model.modelProxy,guardedFetch,Boolean(scripted),actualCommitment?24:composedBriefOffer?6:2);
  }
  runtime = await start();
  const configure=()=>command('configure',{config:{...config,port:runtime!.port},
    ...(nativePrepRelay?{nativePrepRelay:{url:nativePrepRelay.url,bearer:nativePrepRelay.bearer}}:{})});
  await configure();
  const backgroundReceipts=new Map<number,{id:string;text:string}>();
  for(const [i,turn] of scenario.turns.entries()) {
    const journeyReceipts=[];
    for(const step of turn.receiverBefore??[]){const at=performance.now();const receipt=await command('journey',step);journeyReceipts.push({...receipt,elapsedMs:Math.round(performance.now()-at)});}
    const journeyMeasurements={modelCalls:journeyReceipts.reduce((n,stage)=>n+(stage.receipt?.model_calls?.length??0),0),sourceReads:journeyReceipts.reduce((n,stage)=>n+(stage.receipt?.source_reads?.length??0),0),totalMs:journeyReceipts.reduce((n,stage)=>n+stage.elapsedMs,0),boundary:'Pre-turn isolated learning/brief processing; excluded from chat request-to-acceptance timer.'};
    if (turn.restartBefore) {await runtime.close(); runtime=await start(); await configure();}
    if(turn.resetConversationBefore)await runtime.rotateSessions();
    if(turn.meetingPrepOfferBefore||turn.briefPrepOfferBefore){
      // Seed one production-staged offer, then bind it to the real captured transport receipt.
      // This qualifies accepted-offer ingress, not autonomous proactive scheduling.
      const beforeCompose=nativePrepRelay?.calls.length??0;
      const staged=await command('prepare_prep_offer',{...(turn.briefPrepOfferBefore??turn.meetingPrepOfferBefore),step:i,...(turn.briefPrepOfferBefore?{composeBrief:true}:{})});
      if(turn.briefPrepOfferBefore){
        const composed=(nativePrepRelay?.calls??[]).slice(beforeCompose);
        if(!composed.length||composed.some(call=>call.status<200||call.status>=300)||staged.fixture_mode!=='actual-model-brief')throw new Error('Actual brief composition evidence missing');
        briefCompositionCalls+=composed.length;
      }
      const beforeOffer=sends.length;
      const response=await originalFetch(`http://127.0.0.1:${runtime.port}/v1/send`,{method:'POST',
        headers:{authorization:`Bearer ${config.bearerToken}`,'content-type':'application/json'},
        body:JSON.stringify({dispatch_id:`eval-prep-offer-${i}`,target:'photon',text:staged.text,source_revision:'a'.repeat(64)})});
      if(!response.ok||(await response.json()).acceptance!=='accepted')throw new Error('Prep offer was not accepted');
      const accepted=sends.slice(beforeOffer);
      const formatted=await createPhotonTextFormatter(join(config.skillPackRoot,'_shared','lib'))(staged.text);
      if(accepted.length!==1||accepted[0]!.text!==formatted||formatted.split(staged.offer.question).length!==2)throw new Error('Prep offer delivery did not preserve the staged offer');
      const proof=await command('accept_prep_offer',{messageId:accepted[0]!.id,acceptedAt:accepted[0]!.at/1000});
      if(!prepExecutionMatches({kind:'focused-meeting-prep',offerId:proof.offer?.offer_id,offerMeeting:proof.offer?.meeting},
        {id:staged.offer?.meeting?.event_id,start:{dateTime:staged.offer?.meeting?.start}},true,proof))throw new Error('Invalid accepted prep offer proof');
      acceptedPrepOfferProof=proof;
      await writeFile(join(output,`prep-offer-${i}.json`),JSON.stringify({staged,accepted:accepted[0],proof},null,2),{mode:0o600});
    }
    if(turn.backgroundBefore){
      const accepted=await acceptBackgroundFixture({fetch:originalFetch,port:runtime.port,bearer:config.bearerToken,step:i,text:turn.backgroundBefore,sends});
      if(accepted)backgroundReceipts.set(i,accepted);
    }
    const canonicalBefore=turn.canonicalUnchanged||taskCreationScenario?(await command('canonical')).files:undefined;
    const taskActionsBefore=taskCreationScenario?new Set(taskActions().map(action=>action.id)):undefined;
    const started=Date.now(),before=sends.length,requestBefore=requests.length,modelRequestBefore=requestCount;
    const sourceBefore=(await sourceTrace()).length;
    const quotedBackground=turn.quoteBackgroundFromTurn===undefined?undefined:backgroundReceipts.get(turn.quoteBackgroundFromTurn);
    if(turn.quoteBackgroundFromTurn!==undefined&&!quotedBackground)throw new Error('Quoted background turn has no accepted transport receipt');
    const inbound=conversationInbound(i,thread,turn.text,config.owner.ownerId,quotedBackground?.id);
    await runtime.gateway.chat.processMessage(adapter,thread,inbound);
    const deadline=Date.now()+330000;
    // Read production health to wait for completion, never the first queued acknowledgement alone.
    let health:any;
    do {
      await new Promise(r=>setTimeout(r,200));
      const response=await originalFetch(`http://127.0.0.1:${runtime.port}/v1/health`,{headers:{Authorization:`Bearer ${config.bearerToken}`}});
      health=await response.json();
      if(sends.length>before && health.pendingInbound===0) break;
    } while(Date.now()<deadline);
    const work=await command('work');
    const replies=sends.slice(before),text=replies.map(reply=>deliveredArtifactText(
      work.deliveries?.find((delivery:any)=>delivery.provider_id===reply.id),reply)??reply.text).join('\n');
    const canonicalAfter=turn.canonicalUnchanged||taskCreationScenario?(await command('canonical')).files:undefined;
    const canonicalTasks=turn.taskStates||turn.taskRecords?await command('tasks'):undefined;
    const taskState=canonicalTasks?.states;
    const taskRecords=canonicalTasks?.records;
    const turnTaskActions=taskCreationScenario?taskActions().filter(action=>!taskActionsBefore!.has(action.id)):undefined;
    const errors=[...(turn.contains??[]).filter(s=>!text.toLowerCase().includes(s.toLowerCase())).map(s=>`missing:${s}`),
      ...(turn.containsAny??[]).filter(group=>!group.some(s=>text.toLowerCase().includes(s.toLowerCase()))).map(group=>`missing_any:${group.join('|')}`),
      ...(turn.excludes??[]).filter(s=>text.toLowerCase().includes(s.toLowerCase())).map(s=>`forbidden:${s}`)];
    if(turn.prepJobCount!==undefined&&work.jobs.length!==turn.prepJobCount)errors.push('unexpected_prep_job_count');
    if(turn.chatPrepOffer){
      const world=JSON.parse(await readFile(worldFile,'utf8'));
      const event=world.calendarEvents?.find((row:any)=>row.id===turn.chatPrepOffer!.eventId);
      const observations=requests.slice(requestBefore).filter(row=>row.path==='/runtime/owner-meeting-prep-request');
      const preview=observations.find(row=>(row.returnedEvidence as any)?.status==='preview'&&(row.returnedEvidence as any)?.review)?.returnedEvidence;
      const recorded=observations.find(row=>(row.returnedEvidence as any)?.status==='recorded')?.returnedEvidence;
      const accepted=await command('prep_offer_proof',{messageIds:replies.map(row=>row.id)});
      const proof=accepted.proofs?.length===1?accepted.proofs[0]:undefined;
      work.chatPrepOfferEvidence={preview,recorded,proof};
      if(!chatPrepOfferMatches(event,preview,recorded,proof,replies))errors.push('exact_natural_chat_prep_offer_missing');
      else acceptedPrepOfferProof=proof;
      if(work.claimed||work.jobs.length)errors.push('chat_prep_offer_queued_before_yes');
    }
    if(turn.canonicalUnchanged&&JSON.stringify(canonicalBefore)!==JSON.stringify(canonicalAfter))errors.push('read_only_query_changed_canonical_state');
    if(turn.taskStates&&JSON.stringify(Object.entries(taskState??{}).sort())!==JSON.stringify(Object.entries(turn.taskStates).sort()))errors.push('canonical_task_state_mismatch');
    if(taskCreationScenario){
      const sortedRecords=(records:Array<{title:string;status:string}>)=>records.map(({title,status})=>JSON.stringify({title,status})).sort();
      if(JSON.stringify(sortedRecords(taskRecords))!==JSON.stringify(sortedRecords(turn.taskRecords!)))errors.push('canonical_task_records_mismatch');
      const adds=turnTaskActions!.filter(action=>action.args?.operation==='add');
      if(JSON.stringify(adds.map(action=>action.args.text).sort())!==JSON.stringify([...turn.taskAddReceipts!].sort()))errors.push('task_add_receipts_mismatch');
      for(const action of adds){
        const parsed=ownerTaskToolReceiptSchema.safeParse(action.receipt);
        const receipt=parsed.success&&parsed.data.operation==='add'?parsed.data:undefined;
        if(action.status!=='succeeded'||Object.keys(action.args).sort().join(',')!=='operation,text'||!receipt||receipt.action_id!==action.id||receipt.event_id!==action.eventId||receipt.payload_hash!==action.sourcePayloadHash||receipt.args_hash!==action.argsHash||receipt.loop_status!=='open'||
          taskRecords.filter((row:any)=>row.anchor_key===receipt.anchor_key&&row.title===action.args.text&&row.status==='open').length!==1)
          errors.push('task_add_receipt_not_canonical');
      }
      if(turnTaskActions!.some(action=>!['add','propose_cleanup'].includes(action.args?.operation)))errors.push('unexpected_task_creation_effect');
      // Scalar adds may introduce only their receipt-backed ledger files. Every existing
      // canonical file must retain its bytes, including unrelated tasks and knowledge.
      const addedPaths=adds.map(action=>taskRecords.find((row:any)=>row.anchor_key===action.receipt?.anchor_key)?.path).filter(Boolean).sort();
      const newPaths=Object.keys(canonicalAfter).filter(path=>!(path in canonicalBefore)).sort();
      if(Object.entries(canonicalBefore).some(([path,digest])=>canonicalAfter[path]!==digest)||JSON.stringify(newPaths)!==JSON.stringify(addedPaths))errors.push('unexpected_canonical_task_creation_change');
    }
    if(turn.meetingPrep){
      const observed=[...turns.map((row:any)=>row.work?.executed),work.executed]
        .filter(row=>row?.kind==='shared-meeting-prep'||row?.kind==='focused-meeting-prep');
      const sourceWorld=JSON.parse(await readFile(worldFile,'utf8'));
      const rawExpected=sourceWorld.calendarEvents?.find((event:any)=>event.id===turn.meetingPrep!.eventId);
      const expected=chatPrepOffer&&acceptedPrepOfferProof?{...rawExpected,id:acceptedPrepOfferProof.offer.meeting.event_id}:rawExpected;
      const executed=observed[0];
      const delivery=work.deliveries?.find((row:any)=>row.id===executed?.id&&row.status==='delivered'&&row.acceptance==='accepted');
      const artifact=delivery&&sends.find(row=>row.id===delivery.provider_id);
      const artifactText=deliveredArtifactText(delivery,artifact);
      if(observed.length!==1||!prepExecutionMatches(executed,expected,seededPrepOffer||chatPrepOffer,acceptedPrepOfferProof)||
         !work.jobs.some((row:any)=>row.id===executed?.id&&row.status==='done')||!artifactText||
         work.claimed!==turn.meetingPrep.newJob)errors.push('exact_prep_worker_or_delivered_artifact_missing');
      if(!artifactText||!turn.meetingPrep.artifactContains.every(value=>artifactText.toLowerCase().includes(value.toLowerCase()))||
         nativePrepRelay?.calls.filter(call=>call.status>=200&&call.status<300).length!==1+briefCompositionCalls)errors.push('native_prep_artifact_content_missing');
      work.prepSkillLoadedAtOrBeforeTurn=loadedSkills.has('sotto-meeting-prep');
    }
    errors.push(...receiverObservationErrors(requests.slice(requestBefore)));
    const returnedEvidence=JSON.stringify(requests.slice(requestBefore).filter(request=>request.returnedEvidence!==undefined&&request.path==='/runtime/knowledge-read'&&request.operation==='knowledge_query').map(request=>request.returnedEvidence));
    for(const expected of turn.returnedEvidenceContains??[])if(!returnedEvidence.includes(expected))errors.push(`missing_returned_evidence:${expected}`);
    for(const forbidden of turn.returnedEvidenceExcludes??[])if(returnedEvidence.includes(forbidden))errors.push(`forbidden_returned_evidence:${forbidden}`);
    if(turn.knowledgeQueryReads!==undefined&&requests.slice(requestBefore).filter(request=>request.path==='/runtime/knowledge-read'&&request.operation==='knowledge_query').length!==turn.knowledgeQueryReads)errors.push('unexpected_knowledge_query_reads');
    const details=requests.slice(requestBefore).filter(request=>request.path==='/runtime/brief-detail');
    const detailEvidence=JSON.stringify(details.map(request=>request.returnedEvidence));
    for(const expected of turn.returnedBriefDetailContains??[])if(!detailEvidence.includes(expected))errors.push(`missing_brief_detail_evidence:${expected}`);
    for(const forbidden of turn.returnedBriefDetailExcludes??[])if(detailEvidence.includes(forbidden))errors.push(`forbidden_brief_detail_evidence:${forbidden}`);
    if(turn.briefDetailReads!==undefined&&details.length!==turn.briefDetailReads)errors.push('unexpected_brief_detail_reads');
    if(turn.maxBriefDetailReads!==undefined&&details.length>turn.maxBriefDetailReads)errors.push('excessive_brief_detail_reads');
    const reads=(await sourceTrace()).slice(sourceBefore);
    if(reads.some(read=>read.source==='denied'))errors.push('unexpected_external_attempt');
    if(turn.bridgeSends!==undefined){const sends=(await readFile(join(data,'bridge-sends.jsonl'),'utf8').catch(()=>'' )).trim().split('\n').filter(Boolean);if(sends.length!==turn.bridgeSends)errors.push('unexpected_bridge_sends');}
    if(turn.websiteReads!==undefined && reads.filter(read=>read.source==='website').length!==turn.websiteReads)errors.push('unexpected_website_reads');
    if(turn.contactReads!==undefined && reads.filter(read=>read.source==='contacts').length!==turn.contactReads)errors.push('unexpected_contact_reads');
    // Count Google-read dispatches to the receiver, including failed/aborted fetches. This
    // does not count tool choices refused locally before dispatch. A saved-memory fallback
    // must not hide an unnecessary receiver request.
    if(turn.googleReads!==undefined && requests.slice(requestBefore).filter(request=>request.path==='/runtime/google-read').length!==turn.googleReads)errors.push('unexpected_google_reads');
    if(turn.researchReads!==undefined && reads.filter(read=>read.source==='company_research').length!==turn.researchReads)errors.push('unexpected_research_reads');
    if(turn.publicReads!==undefined && reads.filter(read=>['website','company_research','prep_public_profile'].includes(read.source)).length!==turn.publicReads)errors.push('unexpected_public_reads');
    if(turn.maxCharacters!==undefined && text.length>turn.maxCharacters)errors.push('reply_too_long');
    if(health.pendingInbound!==0)errors.push('input_not_settled');
    if(replies.length<(turn.minReplies??1)) errors.push('missing_final_reply');
    if(work.jobs.some((j:any)=>j.status!=='done')) errors.push('background_work_incomplete');
    if((work.claimed && replies.length<2) || (!work.claimed && claimsQueuedWork(text))) errors.push('queued_is_not_completion');
    const measurements=measureTurn({startedAtMs:started,replies,health,
      modelRequests:requestCount-modelRequestBefore,sourceReads:reads.length,latencyClass:turn.latencyClass,errors,
      requests:requests.slice(requestBefore),outputCharacters:text.length,scriptedProvider:Boolean(scripted)});
    turns.push({input:turn.text,quotedBackground,journeyReceipts,journeyMeasurements,canonicalBefore,canonicalAfter,taskState,taskRecords,taskActions:turnTaskActions,replies,sourceReads:reads,elapsedMs:Date.now()-started,
      measurements,requests:requests.slice(requestBefore),health,work,errors});
    await writeFile(join(output,'report.json'),JSON.stringify({name:scenario.name,status:'in_progress',turns},null,2),{mode:0o600});
    console.log(JSON.stringify({turn:i+1,elapsedMs:Date.now()-started,replies:replies.length,errors}));
  }
} catch(error) {failure=error instanceof Error?error.message:'Unknown eval failure';}
finally {
  if(scripted&&scripted.nativeConsumed!==(script?.nativeResponses?.length??0))failure??='Scripted native provider responses not fully consumed';
  if(nativePrepRelay?.refusals.length)failure??='Native prep relay refused or failed a request';
  if(nativePrepRelay)await nativePrepRelay.close();
  try{await runtime?.close();}catch{failure??='Runtime shutdown failed';}
  if(!child.stdin.destroyed)child.stdin.end(JSON.stringify({op:'stop'})+'\n');
  await rm(join(output,'runtime.json'),{force:true});
  const timer=setTimeout(()=>child.kill('SIGKILL'),3000);timer.unref();
  const skills=await readSkillIndex(join(root,'sotto-chief-of-staff'));
  let effects:unknown[]=[];
  let readOnlyTaskEffects=false;
  try {
    const journal=new DatabaseSync(join(output,'runtime/operations.sqlite'),{readOnly:true});
    try{
      effects=journal.prepare('SELECT kind,status,count(*) AS count FROM actions GROUP BY kind,status').all();
      if(scenario.world.commitmentJourney&&scenario.turns.every(turn=>turn.canonicalUnchanged===true)){
        const tasks=journal.prepare("SELECT result_json FROM actions WHERE kind='owner_loop_tool'").all();
        readOnlyTaskEffects=tasks.length>0&&tasks.every(row=>JSON.parse(String(row.result_json)).args?.operation==='propose_cleanup');
      }
    }
    finally{journal.close();}
  }catch{failure??='Effect evidence unavailable';}
  if(scenario.world.loopTasks){
    try{
      const journal=new DatabaseSync(join(output,'runtime/operations.sqlite'),{readOnly:true});
      try{for(const row of journal.prepare('SELECT kind,result_json FROM actions').all()){
        if(row.kind!=='owner_loop_tool')continue;
        const args=JSON.parse(String(row.result_json)).args;
        if(taskCreationScenario?!['add','propose_cleanup'].includes(args?.operation):!['propose_cleanup','resolve_many','cleanup'].includes(args?.operation)||args.operation==='cleanup'&&args.items.some((item:any)=>item.action!=='resolve'))failure??='Unexpected task mutation outside declared scenario';
      }}finally{journal.close();}
    }catch{failure??='Task effect evidence unavailable';}
    if(!taskCreationScenario&&scenario.turns.some(turn=>!turn.taskStates))failure??='Task scenario must verify every canonical row after every turn';
  }
  const unexpectedEffect=effects.some((row:any)=>!((seededPrepOffer||chatPrepOffer)&&acceptedPrepOfferProof&&row.kind==='owner_meeting_prep_request'&&row.status==='succeeded'&&row.count===1&&(!chatPrepOffer||expectedPrepJobs===1))
    &&!(chatPrepOffer&&acceptedPrepOfferProof&&row.kind==='owner_meeting_prep_offer'&&row.status==='succeeded'&&row.count===1)
    &&!(chatPrepOffer&&acceptedPrepOfferProof&&row.kind==='owner_offer_reply'&&row.status==='succeeded'&&row.count===1&&requests.some(request=>(request.returnedEvidence as any)?.outcome?.action==='declined'&&(request.returnedEvidence as any)?.outcome?.loop_changed===false))
    &&![...(seededPrepOffer||chatPrepOffer?[]:['owner_skill_request']),'owner_web_read', ...(scenario.world.loopTasks||readOnlyTaskEffects?['owner_loop_tool']:[]), ...(scenario.world?.bridgeSend === true ? ['owner_bridge_send_tool'] : [])].includes(row.kind));
  if(unexpectedEffect)failure??='Unexpected mutation in a read/draft-only scenario';
  if((await sourceTrace()).some(read=>read.source==='denied'))failure??='Unexpected external source attempt (including caught provider errors)';
  if(script&&scripted?.consumed!==script.steps.length)failure??='Scripted provider has unconsumed requests';
  if(unexpectedCalls.length)failure??='Unexpected external/model fixture attempt';
  const failed=Boolean(failure)||turns.length!==scenario.turns.length||turns.some((t:any)=>t.errors.length);
  await writeFile(join(output,'report.json'),JSON.stringify({codeCommit,name:scenario.name,status:failed?'failed':'passed',failure,
    baseInstructions:{characters:model.instructions.length,sha256:createHash('sha256').update(model.instructions).digest('hex'),guidance:'Current production bootstrap and mounted skill guides are appended normally.'},
    scope:scripted?'Synthetic scripted provider; production bootstrap/receiver/tools/writers and synthetic sources/delivery. No actual-model or live latency acceptance.':'Actual model and production bootstrap/receiver; synthetic sources and delivery. Not device acceptance.',
    providerMode:scripted?'synthetic-scripted':'actual-approved',unexpectedCalls,latencySummary:summarizeMeasurements(turns as any,{expectedSamples:scenario.turns.length,scriptedProvider:Boolean(scripted)}),
    briefCompositionCalls,nativePrepCalls:nativePrepRelay?.calls??[],nativePrepRefusals:nativePrepRelay?.refusals??[],
    model:model.modelProxy.chatModelId,discoveredSkills:skills.map(s=>s.name),loadedSkills:[...loadedSkills],effects,modelRequests:requestCount,turns,receiverDiagnostics:stderr},null,2),{mode:0o600});
  console.log(JSON.stringify({status:failed?'failed':'passed',report:join(output,'report.json')}));
  process.exitCode=failed?1:0;
}

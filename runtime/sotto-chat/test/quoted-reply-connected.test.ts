import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync,readFileSync,readdirSync,mkdtempSync,rmSync} from 'node:fs';
import {once} from 'node:events';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createSourceAuthorityReader} from '../src/agent/source-authority.js';
import {createKnowledgeCommand,KNOWLEDGE_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
async function startReceiver(folder: string, loops = false, granola = false, history = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_KNOWLEDGE_READ:'1',SOTTO_TEST_IMPORTANCE_READ:undefined, SOTTO_TEST_LOOPS_READ: loops ? '1' : undefined, SOTTO_TEST_GRANOLA_READ:granola?'1':undefined, SOTTO_TEST_GRANOLA_HISTORY:history?'1':undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles'}});
  let stderr = ''; child.stderr.on('data', chunk => {stderr = (stderr + String(chunk)).slice(-4096);});
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
      let output = ''; child.stdout.on('data', chunk => {output += String(chunk); const i = output.indexOf('\n'); if (i >= 0) done(output.slice(0, i));});
      child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`))); child.once('error', fail);
    }), new Promise<never>((_, fail) => {timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000);})]);
    const parsed = JSON.parse(line) as {port: number; identity: OwnerToolIdentity; bearer: string; readBearer: string};
    return {child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp`};
  } catch (error) {child.kill('SIGKILL'); throw error;}
  finally {if (timer) clearTimeout(timer);}
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null) return;
  const ended = once(child, 'exit'); child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {await ended;} finally {clearTimeout(timer);}
}




const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
test('exact older accepted menu survives reopen and feeds original Ask canonical read without authorizing effects',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'quoted-reply-'));let journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 let turn=0,step=0;const sent:string[]=[];let reads=0;
 try {
  receiver=await startReceiver(join(folder,'receiver'));
  const authority=createSourceAuthorityReader({bridgeUrl:receiver.bridgeUrl,bearer:receiver.readBearer});
  const snapshot=()=>readdirSync(join(folder,'receiver/knowledge/people')).sort().map(name=>[name,readFileSync(join(folder,'receiver/knowledge/people',name),'utf8')]);
  const original=snapshot();
  const mount=async(memory:string)=>{
   const command=createKnowledgeCommand({...receiver!,sourceAuthority:authority,isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event,fetch:async(input,init)=>{reads++;return fetch(input,init);}});
   const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
   const model=new MockLanguageModelV4({doGenerate:async request=>{
    if(turn<2)return {content:[{type:'text',text:turn===0?'OLD MENU: 1 schedule; 2 Dana Cedar pediatric procurement history':'NEW MENU: 1 Dana; 2 unrelated travel'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
    const prompt=JSON.stringify(request.prompt);
    assert.ok(prompt.includes('Exact locally accepted reply quoted by the owner'));
    assert.ok(prompt.includes('OLD MENU: 1 schedule; 2 Dana Cedar pediatric procurement history'));
    if(memory==='reopened')assert.ok(!prompt.includes('NEW MENU'));
    const calls=[{name:'skill',args:{name:'sotto-ask'}},{name:'run_sotto_script',args:{id:'knowledge-query',args:['--person','Dana Cedar','--topic','procurement']}},null];
    const call=calls[step++];
    if(step===2)assert.ok(prompt.includes('run_sotto_script knowledge-query --person'));
    if(step===3)assert.ok(prompt.includes('In 2020 Dana discussed pediatric clinic procurement with Cedar.'));
    return {content:call?[{type:'tool-call' as const,toolCallId:memory+'-'+step,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'For option 2 in your quoted menu: Dana discussed pediatric clinic procurement with Cedar in 2020.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
   }});
   runtime=createSottoAgentRuntime({model,storagePath:join(folder,memory+'.db'),instructions:KNOWLEDGE_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:authority,chatTools:()=>({run_sotto_script:tool})});
   coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver!.identity.owner_id},model:{generate:request=>runtime!.generateChat({...request,threadId:memory,ownerId:'owner'})},postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:'reply-'+sent.length};}});
  };
  const receive=(id:string,text:string,anchor?:string)=>coordinator!.receive({channel:'photon',accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId:id,text,...(anchor?{replyBound:true,replyToMessageId:anchor}:{})},{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});
  await mount('first');
  for(turn=0;turn<2;turn++)assert.equal((await receive('menu-'+turn,'Give me options')).status,'accepted');
  turn=2;assert.equal((await receive('older-choice','2','reply-1')).status,'accepted');assert.equal(reads,1);
  await coordinator!.close();await runtime!.close();journal.close();journal=new OperationalJournal(join(folder,'journal.sqlite'));step=0;
  await mount('reopened');assert.equal((await receive('older-choice-after-reopen','2','reply-1')).status,'accepted');assert.equal(reads,2);
  assert.match(sent.at(-1)!,/2020/);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  assert.deepEqual(snapshot(),original);
  const queue=join(folder,'receiver/events/work.sqlite3');if(existsSync(queue)){const db=new DatabaseSync(queue,{readOnly:true});try{assert.equal(db.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{db.close();}}
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

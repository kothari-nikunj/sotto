import {startReceiver,stopReceiver} from './research-receiver-fixture.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync, mkdtempSync, rmSync, readdirSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {MockLanguageModelV4} from 'ai/test';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createKnowledgeCommand, KNOWLEDGE_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]})};
test('native Ask follows returned saved-company cursor to market evidence without external reads or writes',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-knowledge-http-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),{SOTTO_TEST_KNOWLEDGE_READ:'1'});
    const companyDirectory=join(folder,'receiver/knowledge/companies');const companyPath=join(companyDirectory,readdirSync(companyDirectory)[0]!);writeFileSync(companyPath,readFileSync(companyPath,'utf8')+'\n'+'Clinic teams manage intake and procurement across sites. '.repeat(68)+'\nNamed competitor: Juniper Systems.\n');const before=readdirSync(companyDirectory).map(name=>[name,readFileSync(join(companyDirectory,name),'utf8')]);
    const resultBodies:Array<{status:string;company_knowledge:Record<string,string>;truncated:boolean;next_cursor:string|null}>=[];const reads:string[]=[];
    const command=createKnowledgeCommand({...receiver,sourceAuthority,
      isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,
      fetch:async(input,init)=>{reads.push(new URL(String(input)).pathname);const response=await fetch(input,init);resultBodies.push(await response.clone().json());return response;}});
    const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({
      mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    let step=0;
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      let call:{name:string;args:Record<string,unknown>}|null;
      if(step===0)call={name:'skill',args:{name:'sotto-ask'}};
      else if(step===1)call={name:'run_sotto_script',args:{id:'knowledge-query',args:['--company','Cedar Labs']}};
      else if(step===2){
        const first=resultBodies[0]!;assert.equal(first.truncated,true);assert.ok(first.next_cursor);
        assert.ok(!JSON.stringify(first.company_knowledge).includes('Juniper Systems'));assert.ok(prompt.includes(first.next_cursor));
        call={name:'run_sotto_script',args:{id:'knowledge-query',args:['--company','Cedar Labs','--company-page',first.next_cursor]}};
      }else{assert.equal(resultBodies[1]!.truncated,false);assert.ok(prompt.includes('Named competitor: Juniper Systems.'));call=null;}
      step++;
      return {content:call?[{type:'tool-call' as const,toolCallId:`company-page-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'The saved market research names Juniper Systems as a competitor. I did not check current websites.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:KNOWLEDGE_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},
      model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'knowledge-answer'};}});
    assert.equal((await coordinator.receive({channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'knowledge-question',text:'What does the saved market research say about Cedar Labs competitors? Read the saved details if needed.'},
      {threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    assert.match(sent[0]!,/Juniper Systems/,JSON.stringify({step,resultBodies}));assert.equal(step,4);
    assert.deepEqual(resultBodies.map(value=>value.status),['saved','saved']);assert.deepEqual(reads,Array(2).fill('/runtime/knowledge-read'));
    assert.deepEqual(readdirSync(companyDirectory).map(name=>[name,readFileSync(join(companyDirectory,name),'utf8')]),before);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    const queuePath=join(folder,'receiver/events/work.sqlite3');
    if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

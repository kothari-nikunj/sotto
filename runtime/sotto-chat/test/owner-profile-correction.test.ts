import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';

test('explicit profile corrections require each current owner span before journal or receiver writes',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-profile-correction-'));
 const journal=new OperationalJournal(join(folder,'operations.sqlite'));
 const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'a'.repeat(64)};
 const authority={version:2 as const,complete:true,revision:'d'.repeat(64),bridge_sources:[],google_sources:[]};
 const client=createOwnerTaskToolClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,fetch:async()=>{assert.fail('borrowed identity reached receiver');}});
 const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>authority}});
 let checked=false;
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{
  const context={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:true,sourceRevision:authority.revision,signal:request.signal};
  const args={operation:'fact_correct' as const,canonical_id:'c_123abc',fact_id:'fact_1',fact_version:'a'.repeat(64),source_revision:authority.revision,text:'Dana is COO at Cedar.'};
  for(const field of ['title','company'])await assert.rejects(execution.execute({...args,[field]:'Invented identity'},context),/exact owner span/);
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  checked=true;return 'Please supply the explicit profile values.';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'clarification'})});
 try{await coordinator.receive({channel:'photon',accountId:'account',senderId:'owner',isDirect:true,eventId:'profile',text:'Dana is COO at Cedar.'},{threadId:'thread',destination:'owner',lineKey:'line'});assert.equal(checked,true);}
 finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

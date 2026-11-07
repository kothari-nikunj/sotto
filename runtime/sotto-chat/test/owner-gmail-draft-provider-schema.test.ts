import assert from 'node:assert/strict';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {z} from 'zod';
import { generateText, tool, stepCountIs } from 'ai';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { createOwnerGmailDraftTools } from '../src/tools/owner-gmail-draft-tool.js';
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';

for (const provider of ['openai', 'google'] as const) {
  test(`${provider} receives closed draft pair/propose/save schemas with no authority input`, async () => {
    const client = createOwnerGmailDraftClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),
      identity:{tenant_id:'tenant',channel:'photon',account_id:'account',owner_id:'owner',line_key:'line',
        target:'photon',route_mode:'fixed',route_id:'thread',project_fingerprint:'a'.repeat(64)}});
    const tools = createOwnerGmailDraftTools({client,journal:()=>undefined,
      sourceAuthority:{read:async()=>({version:2,complete:true,revision:'d'.repeat(64),bridge_sources:[],google_sources:['gmail']})}});
    let body: Record<string,any> | undefined;
    const capture: typeof fetch = async (_url,init)=>{body=JSON.parse(init!.body as string);throw new Error('captured without network');};
    const model=provider==='openai'
      ? createOpenAICompatible({name:'fixture',baseURL:'http://fixture.invalid/v1',apiKey:'synthetic',fetch:capture}).chatModel('fixture')
      : createGoogleGenerativeAI({apiKey:'synthetic',fetch:capture})('gemini-2.5-flash');
    await assert.rejects(generateText({model,prompt:'fixture',maxRetries:0,tools:{
      gmail_draft_pair:tool({inputSchema:tools.gmail_draft_pair.inputSchema!}),
      gmail_draft_propose:tool({inputSchema:tools.gmail_draft_propose.inputSchema!}),
      gmail_draft_save:tool({inputSchema:tools.gmail_draft_save.inputSchema!}),
    }}));
    assert.ok(body);
    if (provider === 'openai') assert.deepEqual(proxyEligibility([body]), [true]);
    const declarations=provider==='openai'?body.tools.map((row:any)=>row.function):body.tools[0].functionDeclarations;
    assert.deepEqual(declarations.map((row:any)=>row.name).sort(),['gmail_draft_pair','gmail_draft_propose','gmail_draft_save']);
    for(const declaration of declarations){
      const schema=declaration.parameters??declaration.parametersJsonSchema;
      assert.equal(schema.type,'object');assert.equal(schema.anyOf,undefined);assert.equal(schema.additionalProperties,false);
      const expected=declaration.name==='gmail_draft_pair'?['accept_body','decline_body','first','message_id']:
        declaration.name==='gmail_draft_propose'?['action_type','body','message_id']:[];
      assert.deepEqual(Object.keys(schema.properties).sort(),expected);
      if(declaration.name==='gmail_draft_pair'){
        assert.deepEqual(schema.properties.first.enum,['reply','decline']);
        assert.deepEqual([...schema.required].sort(),['accept_body','decline_body','message_id']);
      }
      if(declaration.name==='gmail_draft_propose')
        assert.deepEqual(schema.properties.action_type.enum,['reply','decline']);
    }
  });
}


function proxyEligibility(bodies: unknown[]): boolean[] {
  // Import the production predicate. A duplicated TS allowlist would miss drift.
  const proxy = fileURLToPath(new URL('../../../cloud/model-proxy/', import.meta.url));
  const shared = fileURLToPath(new URL('../../../sotto-chief-of-staff/_shared/lib/', import.meta.url));
  const child = spawnSync('python3', ['-c',
    'import sys,json; sys.path[:0]=sys.argv[1:]; from server import chat_text_request; print(json.dumps([chat_text_request(x) for x in json.load(sys.stdin)]))',
    proxy, shared], {input: JSON.stringify(bodies), encoding: 'utf8'});
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

test('real managed SDK initial and tool continuation bodies remain eligible for proxy settlement', async () => {
  const bodies: Record<string, any>[] = [];
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({id:'fixture',object:'chat.completion',created:1,model:'gemini-3.8-flash',
      choices:[{index:0,finish_reason:bodies.length===1?'tool_calls':'stop',message:bodies.length===1
        ? {role:'assistant',content:null,tool_calls:[{id:'read-1',type:'function',function:{name:'read',arguments:'{}'}}]}
        : {role:'assistant',content:'Read complete.'}}],
      usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}}));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const model = createOpenAICompatible({name:'sotto-managed-proxy',baseURL:`http://127.0.0.1:${address.port}/openai/v1`,apiKey:'synthetic'}).chatModel('gemini-3.8-flash');
    const result = await generateText({model,prompt:'Read my selected item',maxRetries:0,stopWhen:stepCountIs(2),
      tools:{read:tool({inputSchema:z.object({}).strict(),execute:async()=>({text:'fixture item'})})}});
    assert.equal(result.text, 'Read complete.');
    assert.equal(bodies.length, 2);
    assert.ok(bodies[1]!.messages.some((message:any)=>message.role==='assistant' && message.tool_calls?.length===1));
    assert.ok(bodies[1]!.messages.some((message:any)=>message.role==='tool' && message.tool_call_id==='read-1'));
    assert.deepEqual(proxyEligibility(bodies), [true, true]);
    assert.deepEqual(proxyEligibility(bodies.map(body=>({...body,unsupported_paid_feature:true}))), [false, false]);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()));
  }
});

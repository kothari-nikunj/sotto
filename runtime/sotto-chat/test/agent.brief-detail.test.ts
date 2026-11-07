import assert from 'node:assert/strict';
import test from 'node:test';
import { createBriefDetailTool, readBriefDetail } from '../src/agent/brief-detail.js';
import type { SourceAuthority } from '../src/agent/source-authority.js';

const revision = 'a'.repeat(64);
const source = (value: SourceAuthority) => ({ read: async () => value });
const current: SourceAuthority = { version: 2, complete: true, revision, google_sources: [], bridge_sources: ['imessage'] };
const url = 'http://127.0.0.1:8793/mcp';
const found = { status: 'found', coverage: 'accepted_gallery_only', id: 'a'.repeat(24), kind: 'brief',
  title: 'Morning brief', text: 'One accepted fact', offset: 0, next_offset: null };
const latest = { latest: true as const, offset: 0, limit: 80 };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

test('latest accepted page uses exact read bearer and current source proof twice', async () => {
  let authorityReads = 0;
  const sourceAuthority = { read: async () => { authorityReads++; return current; } };
  let calls = 0;
  const fetcher: typeof fetch = async (input, init) => {
    calls++;
    assert.equal(String(input), 'http://127.0.0.1:8793/runtime/brief-detail');
    assert.equal(init?.method, 'POST');
    assert.equal(init?.redirect, 'error');
    assert.equal((init?.headers as Record<string, string>).authorization, 'Bearer read-only');
    assert.deepEqual(JSON.parse(String(init?.body)), latest);
    return response(found);
  };
  const result = await readBriefDetail({ bridgeUrl: url, readBearer: 'read-only', sourceAuthority,
    fetch: fetcher }, latest, revision);
  assert.deepEqual(result, found);
  assert.equal(authorityReads, 2);
  assert.equal(calls, 1);
});

test('source revocation, changed revision, abort and invalid selector withhold before or after read', async () => {
  let calls = 0;
  let authority = current;
  const options = { bridgeUrl: url, readBearer: 'read-only', sourceAuthority: { read: async () => authority },
    fetch: async () => { calls++; return response(found); } };
  authority = { ...current, revision: 'b'.repeat(64) };
  await assert.rejects(readBriefDetail(options, latest, revision), /unavailable/);
  assert.equal(calls, 0);
  authority = current;
  await assert.rejects(readBriefDetail(options, { latest: true, offset: 65_000, limit: 1_000 }, revision), /unavailable/);
  assert.equal(calls, 0);
  const aborted = new AbortController(); aborted.abort();
  await assert.rejects(readBriefDetail(options, latest, revision, aborted.signal), /unavailable/);
  assert.equal(calls, 0);
  let reads = 0;
  const changing = { ...options, sourceAuthority: { read: async () => ++reads === 1 ? current :
    { ...current, revision: 'c'.repeat(64) } } };
  await assert.rejects(readBriefDetail(changing, latest, revision), /unavailable/);
  assert.equal(calls, 1);
});

test('strict projection and chat authority reject raw data, unattended mode, and source changes', async () => {
  const options = { bridgeUrl: url, readBearer: 'read-only', sourceAuthority: source(current),
    fetch: async () => response(found) };
  for (const body of [{ ...found, raw_source: 'private' }, { ...found, coverage: 'all_sources' },
    { ...found, text: 'x'.repeat(8_001) }, { ...found, id: 'b'.repeat(24) },
    { ...found, text: 'x'.repeat(81) }, { ...found, text: 'abc', next_offset: 2 }]) {
    await assert.rejects(readBriefDetail({ ...options, fetch: async () => response(body) },
      { artifact_id: 'a'.repeat(24), offset: 0, limit: 80 }, revision), /unavailable/);
  }
  const unicodePage = { ...found, text: '😀😀', next_offset: 2 };
  assert.deepEqual(await readBriefDetail({ ...options, fetch: async () => response(unicodePage) },
    { artifact_id: found.id, offset: 0, limit: 80 }, revision), unicodePage);
  const tool = createBriefDetailTool(options);
  const execute = tool.execute!;
  const context = (mode: string, sourceRevision = revision) => ({ requestContext: { get: (key: string) =>
    ({ mode, runId: 'run', ownerRequestKey: 'event', sourceRevision })[key as 'mode'] } });
  await assert.rejects(execute(latest, context('unattended') as never), /unavailable/);
  await assert.rejects(execute(latest, context('chat', 'b'.repeat(64)) as never), /unavailable/);
  const result = await execute(latest, context('chat') as never) as { coverage: string; coverage_note: string };
  assert.equal(result.coverage, 'accepted_gallery_only');
  assert.match(result.coverage_note, /historical/);
});

test('discovery returns bounded ambiguity metadata and refuses mixed or invalid selectors before contact',async()=>{
  let contacts=0;
  const matches=[{id:'a'.repeat(24),date:'2026-10-04',title:'Morning',kind:'brief' as const},{id:'b'.repeat(24),date:'2026-10-04',title:'Evening',kind:'brief' as const}];
  const options={bridgeUrl:url,readBearer:'read-only',sourceAuthority:source(current),fetch:async(_input:unknown,init?:RequestInit)=>{
    contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{date:'2026-10-04',query:'Cedar',kind:'brief',offset:0,limit:80});
    return response({status:'ambiguous',coverage:'accepted_gallery_only',matches,omitted_matches:0});}};
  const result=await readBriefDetail(options,{date:'2026-10-04',query:'Cedar',kind:'brief',offset:0,limit:80},revision);
  assert.equal(result.status,'ambiguous');assert.ok(!JSON.stringify(result).includes('text'));
  for(const input of [{date:'2026-02-30',offset:0,limit:80},{query:' Cedar ',offset:0,limit:80},{kind:'brief',offset:1,limit:80},{date:'2026-10-04',latest:true,offset:0,limit:80},{offset:0,limit:80}])
    await assert.rejects(readBriefDetail(options,input as never,revision));
  assert.equal(contacts,1);
  for (const input of [latest,{artifact_id:'a'.repeat(24),offset:0,limit:80},{kind:'brief',offset:0,limit:80}])
    await assert.rejects(readBriefDetail({...options,fetch:async()=>response({status:'ambiguous',coverage:'accepted_gallery_only',matches,omitted_matches:0})},input as never,revision));
  const tool=createBriefDetailTool(options);
  assert.equal((tool.inputSchema as unknown as {shape?:unknown}).shape!==undefined,true,'provider tool has object root');
});

test('installed Google adapter receives object-root gallery selector schema without network',async()=>{
  const {generateText,tool}=await import('ai');
  const {createGoogleGenerativeAI}=await import('@ai-sdk/google');
  const native=createBriefDetailTool({bridgeUrl:url,readBearer:'read-only',sourceAuthority:source(current)});
  let body:Record<string,any>|undefined;
  const model=createGoogleGenerativeAI({apiKey:'synthetic',fetch:async(_input,init)=>{body=JSON.parse(String(init!.body));throw Error('captured without network');}})('gemini-2.5-flash');
  await assert.rejects(generateText({model,prompt:'fixture',maxRetries:0,tools:{accepted_brief_detail:tool({inputSchema:native.inputSchema!})}}));
  const schema=body!.tools[0].functionDeclarations[0].parametersJsonSchema;
  assert.equal(schema.type,'object');assert.equal(schema.anyOf,undefined);assert.equal(schema.additionalProperties,false);
  assert.deepEqual(Object.keys(schema.properties).sort(),['artifact_id','date','kind','latest','limit','offset','query']);
  assert.deepEqual(schema.required,['offset','limit']);
  assert.match(schema.properties.limit.description,/characters.*not number of results/);
  assert.match(schema.properties.offset.description,/Character offset/);
  assert.match(schema.properties.query.description,/literal phrase/);
});

test('retained provider reports remain bounded data and legacy evidence is explicitly unavailable',async()=>{
  const research_evidence={status:'retained' as const,input_sha256:'b'.repeat(64),omitted_records:0,records:[{
    provider:'gemini' as const,stage:'focus' as const,subjects:['dana@sample.test'],response_json:'{"claim":"investment"}',
    response_sha256:'c'.repeat(64),grounding_json:'{"uri":"https://different.test/portfolio"}',truncated:false}]};
  let contacts=0;
  const options={bridgeUrl:url,readBearer:'read-only',sourceAuthority:source(current),fetch:async()=>{
    contacts++;return response({...found,kind:'prep',research_evidence});}};
  const result=await readBriefDetail(options,latest,revision);
  assert.equal(result.status,'found');
  if(result.status==='found')assert.deepEqual(result.research_evidence,research_evidence);
  assert.equal(contacts,1,'URLs in retained metadata start no external read');
  for(const changed of [
    {...research_evidence,records:[{...research_evidence.records[0],grounding_json:'x'.repeat(3001)}]},
    {...research_evidence,records:Array(13).fill(research_evidence.records[0])},
    {...research_evidence,records:Array(6).fill({...research_evidence.records[0],grounding_json:'x'.repeat(3000)})},
    {...research_evidence,records:[{...research_evidence.records[0],truth_verified:true}]},
    {status:'unavailable',records:research_evidence.records},
  ])await assert.rejects(readBriefDetail({...options,fetch:async()=>response({...found,research_evidence:changed})},latest,revision),/unavailable/);
  const tool=createBriefDetailTool({...options,fetch:async()=>response(found)});
  const context={requestContext:{get:(key:string)=>({mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision})[key as 'mode']}};
  const legacy=await tool.execute!(latest,context as never) as {research_evidence:unknown};
  assert.deepEqual(legacy.research_evidence,{status:'unavailable'});
});

test('accepted plain-text prep exposes bounded retained evidence through the same source checks', async () => {
  const research_evidence = {status:'retained', input_sha256:'c'.repeat(64), omitted_records:0,
    records:[{provider:'gemini',stage:'profile',subjects:['dana@sample.test'],response_json:'{"claim":"clinic notebook"}',
      response_sha256:'d'.repeat(64),grounding_json:'{"uri":"https://sample.test"}',truncated:false}]};
  const page={...found,coverage:'accepted_artifacts_only',kind:'prep',research_evidence};
  const options={bridgeUrl:url,readBearer:'read-only',sourceAuthority:source(current),fetch:async()=>response(page)};
  assert.deepEqual(await readBriefDetail(options,latest,revision),page);
  await assert.rejects(readBriefDetail({...options,sourceAuthority:source({...current,revision:'b'.repeat(64)})},latest,revision),/unavailable/);
});

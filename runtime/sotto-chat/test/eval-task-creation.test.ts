import test from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const directory=dirname(fileURLToPath(import.meta.url));
const root=join(directory,'..');
const scenario=JSON.parse(await readFile(join(root,'evals/scenarios/loop-add.json'),'utf8'));
const provider=JSON.parse(await readFile(join(root,'evals/scenarios/loop-add.provider.json'),'utf8'));

async function evaluate(world:typeof scenario,script:typeof provider){
  const folder=await mkdtemp(join(tmpdir(),'sotto-eval-task-creation-'));
  try{
    const scenarioFile=join(folder,'scenario.json'),scriptFile=join(folder,'provider.json'),output=join(folder,'output');
    await writeFile(scenarioFile,JSON.stringify(world));
    await writeFile(scriptFile,JSON.stringify(script));
    let exitCode=0;
    try{await promisify(execFile)(process.execPath,['--import','tsx','evals/conversation.ts','--scenario',scenarioFile,'--scripted-provider',scriptFile,'--output',output],{cwd:root,timeout:60000,maxBuffer:1024*1024});}
    catch(error){exitCode=Number((error as {code?:number}).code)||1;}
    return {exitCode,report:JSON.parse(await readFile(join(output,'report.json'),'utf8'))};
  }finally{await rm(folder,{recursive:true,force:true});}
}

test('scalar creation eval verifies every canonical task and durable per-turn receipts',async()=>{
  const {exitCode,report}=await evaluate(scenario,provider);
  assert.equal(exitCode,0,JSON.stringify(report));
  assert.equal(report.status,'passed');
  assert.deepEqual(report.turns.map((turn:any)=>turn.taskRecords.length),[1,2,2]);
  assert.deepEqual(report.turns.map((turn:any)=>turn.taskActions.filter((action:any)=>action.receipt?.operation==='add').length),[1,1,0]);
});

test('Added prose without any action fails canonical and receipt checks',async()=>{
  // Each turn gets its initial false claim and the guard's one corrective model call.
  // Both remain false, so truthful rejection must not depend on exhausting the provider.
  const script={...provider,steps:scenario.turns.flatMap((turn:any)=>Array.from({length:2},()=>({text:`Added: ${turn.taskRecords.map((row:any)=>row.title).join(', ')}`})))};
  const {exitCode,report}=await evaluate(scenario,script);
  assert.equal(exitCode,1);
  assert.equal(report.status,'failed');
  assert.equal(report.failure,undefined);
  assert.deepEqual(report.unexpectedCalls,[]);
  assert.equal(report.modelRequests,6);
  assert.equal(report.turns.length,3,report.failure);
  for(const turn of report.turns){
    assert.deepEqual(turn.replies.map((reply:any)=>reply.text),['I couldn’t record that task.']);
    assert.ok(turn.replies.every((reply:any)=>!reply.text.startsWith('Added:')));
  }
  for(const turn of report.turns.slice(0,2)){
    assert.ok(turn.errors.includes('canonical_task_records_mismatch'));
    assert.ok(turn.errors.includes('task_add_receipts_mismatch'));
    assert.deepEqual(turn.taskActions,[]);
  }
});

test('creation eval rejects undeclared canonical peers and mismatched expected receipts',async()=>{
  const changed=structuredClone(scenario);
  // The real second add leaves elm present. Omitting it must not hide an unrelated row.
  changed.turns[1].taskRecords=changed.turns[1].taskRecords.slice(1);
  // Fir exists, but that cannot substitute for the exact receipt expected on this turn.
  changed.turns[1].taskAddReceipts=['Sotto loop check elm'];
  const {exitCode,report}=await evaluate(changed,provider);
  assert.equal(exitCode,1);
  assert.equal(report.status,'failed');
  assert.equal(report.turns.length,3,report.failure);
  assert.ok(report.turns[1].errors.includes('canonical_task_records_mismatch'));
  assert.ok(report.turns[1].errors.includes('task_add_receipts_mismatch'));
  assert.deepEqual(report.turns[0].errors,[]);
  assert.deepEqual(report.turns[2].errors,[]);
});

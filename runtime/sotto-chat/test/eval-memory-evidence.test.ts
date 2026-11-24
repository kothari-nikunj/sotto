import test from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const root=join(dirname(fileURLToPath(import.meta.url)),'..');
for(const name of ['memory-stale-employer','memory-contradicted-investor']){
  test(`${name}: original evidence reaches chat and survives restart without memory writes`,async()=>{
    const folder=await mkdtemp(join(tmpdir(),'sotto-memory-evidence-'));
    try{
      const output=join(folder,'output');
      let failure:unknown;
      try{await promisify(execFile)(process.execPath,['--import','tsx','evals/conversation.ts',
        '--scenario',`evals/scenarios/${name}.json`,
        '--scripted-provider',`evals/scenarios/${name}.provider.json`,'--output',output],
        {cwd:root,timeout:60000,maxBuffer:1024*1024});}
      catch(error){failure=error;}
      const report=JSON.parse(await readFile(join(output,'report.json'),'utf8'));
      assert.equal(failure,undefined,JSON.stringify(report));
      assert.equal(report.status,'passed');
      assert.equal(report.turns.length,3);
      assert.deepEqual(report.unexpectedCalls,[]);
      assert.deepEqual(report.turns[0].sourceReads,[]);
      for(const turn of report.turns){
        assert.deepEqual(turn.errors,[]);
        assert.deepEqual(turn.canonicalAfter,turn.canonicalBefore);
      }
    }finally{await rm(folder,{recursive:true,force:true});}
  });
}

import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {ownerReadRequest, contextualReadRequest, subjectMention, validResearchSubject} from '../src/tools/read-admission.js';

test('runtime and receiver share the read policy, including whole Unicode names and sentence punctuation',()=>{
 const vectors=[
  ['tell me about Dana Reed','Dana Reed','person',true],
  ['tell me about Dana Reedson','Dana Reed','person',false],
  ['tell me about Dána Réed.','Dána Réed','person',true],
  ['research Noé Dupont','Noé Dupont','person',true],
  ['tell me about José Notário','José Notário','person',true],
  ['research Noe\u0301 Dupont','Noé Dupont','person',true],
  ["don't research Noé Dupont",'Noé Dupont','person',false],
  ['research José Notário. nvm','José Notário','person',false],
  ['research Cedar Labs. no worriesé','Cedar Labs','company',false],
  ['research Tinfoil.','Tinfoil','company',true],
  ['Queue research for Spur?','Spur','company',true],
  ['Any competitors to know?','competitors','company',true],
  ['research bob@vendor.example','vendor.example','company',false],
  ["don't look up Dana Reed",'Dana Reed','person',false],
  ['She said write me a blurb for Tinfoil','Tinfoil','company',false],
  ['research 12 Oak St lease, $4,200 due Friday','12 Oak St lease, $4,200 due Friday','person',false],
  ['ok thanks','Dana Reed','person',false],
  ['look up Dana Reed. nvm','Dana Reed','person',false],
  ['Research Dana Reed? scratch that','Dana Reed','person',false],
  ['research Dana Reed. actually dont','Dana Reed','person',false],
  ['how was lunch with Dana Reed?','Dana Reed','person',false],
  ['research 12 Oak St lease renewal','12 Oak St lease renewal','company',false],
  ['research 37signals','37signals','company',true],
  ['research Cedar Labs. no worries','Cedar Labs','company',true],
  ['research Cedar, not Dana Reed','Dana Reed','person',false],
  ['ignore Dana Reed. Research Cedar','Dana Reed','person',false],
 ] as const;
 const actual=vectors.map(([text,subject,mode])=>ownerReadRequest(text)&&subjectMention(text,subject)&&validResearchSubject(subject,mode));
 assert.deepEqual(actual,vectors.map(v=>v[3]));
 const result=spawnSync(process.env.SOTTO_PYTHON??'python3',['-c',`import sys,json;sys.path.insert(0,sys.argv[1]);from runtime_read_admission import owner_read_request,subject_mention,valid_research_subject;print(json.dumps([owner_read_request(t) and subject_mention(t,s) and valid_research_subject(s,m) for t,s,m,_ in json.load(sys.stdin)]))`,fileURLToPath(new URL('../../trigger-receiver',import.meta.url))],{input:JSON.stringify(vectors),encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);assert.deepEqual(JSON.parse(result.stdout),actual);
 assert.equal(contextualReadRequest('Any competitors to know?'),true);
 assert.equal(contextualReadRequest('yes'),false);
 assert.equal(contextualReadRequest('yes pls'),false);
 assert.equal(contextualReadRequest('check their website'),true);
 assert.equal(contextualReadRequest('ok thanks'),false);
});

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createSourceAuthorityReader } from '../src/agent/source-authority.js';
import { createGoogleReadTools, readOwnerGoogle } from '../src/agent/google-read.js';

const here = dirname(fileURLToPath(import.meta.url));
const sourceRoot = process.env.SOTTO_FIXTURE_SOURCE_ROOT || resolve(here, '../../../');
const child = join(here, 'google-only-receiver-child.py');

function waitForClose(close: Promise<unknown>) {
  return new Promise<void>((resolveClose, reject) => {
    const timer = setTimeout(() => reject(new Error('Google receiver did not stop')), 15_000);
    close.then(() => { clearTimeout(timer); resolveClose(); }, error => { clearTimeout(timer); reject(error); });
  });
}

test('actual receiver and typed Node client permit empty Google reads without a Mac Bridge, then withhold revocation',
  { timeout: 60_000 }, async () => {
    const fixture = await mkdtemp(join(tmpdir(), 'sotto-google-contract-'));
    const bundled = join(sourceRoot, '.venv/bin/python');
    const python = process.env.PYTHON || (existsSync(bundled) ? bundled : 'python3');
    const proc = spawn(python, ['-I', '-B', child, sourceRoot, fixture], {
      env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: fixture },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = ''; let errorOutput = '';
    proc.stdout.setEncoding('utf8'); proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', chunk => { errorOutput = (errorOutput + String(chunk)).slice(-4096); });
    const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveClose, reject) => {
      proc.once('close', (code, signal) => resolveClose({ code, signal })); proc.once('error', reject);
    });
    void closed.catch(() => undefined);
    const ready = new Promise<{ url: string; bearer: string; revision: string }>((resolveReady, reject) => {
      const timer = setTimeout(() => reject(new Error(`Google receiver startup timed out: ${errorOutput}`)), 20_000);
      proc.stdout.on('data', chunk => {
        output += String(chunk);
        if (output.length > 8192) { clearTimeout(timer); reject(new Error('receiver output exceeded bound')); return; }
        const line = output.split('\n').find(value => value.startsWith('{"ready":'));
        if (!line) return;
        try {
          const parsed = JSON.parse(line) as { ready: boolean; url: string; bearer: string; revision: string };
          if (!parsed.ready || !parsed.url.startsWith('http://127.0.0.1:') ||
              !/^[a-f0-9]{64}$/.test(parsed.revision)) throw new Error();
          clearTimeout(timer); resolveReady(parsed);
        } catch { clearTimeout(timer); reject(new Error('receiver readiness was invalid')); }
      });
      proc.once('close', () => { clearTimeout(timer); reject(new Error(`receiver exited: ${errorOutput}`)); });
    });
    try {
      const { url, bearer, revision } = await ready;
      const reader = createSourceAuthorityReader({ bridgeUrl: url, bearer });
      assert.deepEqual(await reader.read(), { version: 2, revision, complete: true,
        bridge_sources: [], google_sources: ['calendar', 'gmail'], connector_sources: [] });
      const options = { bridgeUrl: url, readBearer: bearer, sourceAuthority: reader };
      const empty = await readOwnerGoogle(options, 'calendar_list', {
        start: '2026-10-01T00:00:00Z', end: '2026-10-02T00:00:00Z' }, revision) as {
          items: unknown[]; observation: { status: string; complete: boolean } };
      assert.deepEqual(empty.items, []);
      assert.deepEqual({ status: empty.observation.status, complete: empty.observation.complete },
        { status: 'ok', complete: true });
      await writeFile(join(fixture, 'shared_calendar'), 'on');
      const shared = await readOwnerGoogle(options, 'calendar_list', {
        start: '2026-10-01T00:00:00Z', end: '2026-10-02T00:00:00Z' }, revision) as {
          items: Array<{id:string;summary:string;owner_participates:boolean}> };
      assert.deepEqual(shared.items.map(row=>[row.id,row.owner_participates]),
        [['declined',false],['mine',true],['other',false]]);
      assert.equal(shared.items.find(row=>row.id==='other')?.summary, 'Other person meeting');
      const mail = await readOwnerGoogle(options, 'gmail_search', { query: 'in:inbox', max: 2 }, revision) as {
        items: Array<{ subject: string }> };
      assert.equal(mail.items[0]?.subject, 'Résumé 📨 / 東京');
      const thread = await readOwnerGoogle(options, 'gmail_thread_get', {thread_id:'thread1'}, revision) as {
        items:Array<{id:string;body:string;from:string}>;observation:{complete:boolean}};
      assert.deepEqual(thread.items.map(row=>row.id),['incoming1','sent1']);
      assert.equal(thread.items[1]?.body,'Approved, please proceed.');
      assert.equal(thread.items[1]?.from,'owner@example.test');
      assert.equal(thread.observation.complete,true);
      assert.ok(createGoogleReadTools(options,'chat').gmail_thread_get);
      await writeFile(join(fixture,'unicode_thread'),'on');
      let actualWireBytes=0;
      const unicode = await readOwnerGoogle({...options,fetch:async(input,init)=>{
        const response=await fetch(input,init);const body=await response.arrayBuffer();
        actualWireBytes=body.byteLength;assert.ok(actualWireBytes<=64*1024);
        return new Response(body,{status:response.status,headers:response.headers});
      }},'gmail_thread_get',{thread_id:'thread1'},revision) as {
        items:Array<{id:string;body:string;labels:string[]}>;omitted_messages:number;observation:{complete:boolean;status:string}};
      assert.equal(unicode.items.length,2);assert.equal(unicode.items.at(-1)?.id,'sent1');
      assert.equal(unicode.items.at(-1)?.body,'漢'.repeat(3000));assert.equal(unicode.omitted_messages,1);
      assert.equal(unicode.observation.status,'partial');assert.equal(unicode.observation.complete,false);
      assert.ok(actualWireBytes>32*1024);assert.ok(unicode.items.every(item=>!item.labels.includes('DRAFT')));

      await assert.rejects(readOwnerGoogle({ ...options, readBearer: 'wrong' }, 'gmail_search',
        { query: 'in:inbox', max: 2 }, revision), /unavailable/);
      const unattended = createGoogleReadTools(options, 'unattended');
      assert.deepEqual(Object.keys(unattended).sort(), ['calendar_get','calendar_list','gmail_get','gmail_search']);
      const context = {requestContext:{get:(key:string)=>({mode:'unattended',runId:'routine',sourceRevision:revision})[key as 'mode']}};
      const search = unattended.gmail_search as {execute(input:unknown,context:unknown):Promise<{items:Array<{subject:string}>}>};
      assert.equal((await search.execute({query:'in:inbox',max:2},context)).items[0]?.subject, 'Résumé 📨 / 東京');
      const calendar = unattended.calendar_list as {execute(input:unknown,context:unknown):Promise<{items:unknown[]}>};
      assert.equal((await calendar.execute({start:'2026-10-01T00:00:00Z',end:'2026-10-02T00:00:00Z'},context)).items.length,3);
      await writeFile(join(fixture, 'revoke_during_read'), 'revoke');
      await assert.rejects(readOwnerGoogle(options,'gmail_thread_get',{thread_id:'thread1'},revision),/unavailable/);
      await assert.rejects(calendar.execute({start:'2026-10-01T00:00:00Z',end:'2026-10-02T00:00:00Z'},context), /unavailable/);
      assert.deepEqual((await reader.read()).google_sources, []);
    } finally {
      if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGTERM');
      try { await waitForClose(closed); }
      catch { if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL'); await closed.catch(() => undefined); }
      await rm(fixture, { recursive: true, force: true });
    }
  });

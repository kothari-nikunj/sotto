import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { startApplication, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { configuredChannelIdentityForConfig } from '../src/configured-identity.js';
import { MockLanguageModelV4 } from 'ai/test';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createBriefDetailTool, readBriefDetail } from '../src/agent/brief-detail.js';
import type { ModelRequest } from '../src/conversation.js';
import { routeDigest } from '../src/route-proof.js';

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const child = join(sourceRoot, 'runtime/sotto-chat/test/brief-gallery-chain-child.py');
const python = process.env.PYTHON || (existsSync(join(sourceRoot, '.venv/bin/python'))
  ? join(sourceRoot, '.venv/bin/python') : 'python3');

for (const order of ['receipt', 'provider_history'] as const) {
test(`saved gallery uses accepted receipt and exact detail before drafting: ${order}`,
  { timeout: 90_000 }, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'sotto-brief-gallery-chain-'));
    const data = join(directory, 'receiver');
    const owner = '+15555550123';
    const line = '+15555550999';
    const thread = `imessage:iMessage;-;${owner}~${line}`;
    const project = 'offline-brief-gallery-project';
    const raw = { version: 1, channel: 'photon', tenantId: 'tenant-fixture',
      stateDirectory: join(directory, 'node'), mediaRoot: directory,
      bearerToken: 'offline-brief-gallery-runtime-bearer-000000000000', port: 0,
      owner: { accountId: 'account-fixture', ownerId: owner }, lineKey: 'line-fixture',
      target: 'photon', providerThreadId: thread,
      credentials: { mode: 'cloud', photonProjectId: project, photonProjectSecret: 'synthetic-secret' },
      instructions: 'Offline fixture only', modelProxy: { origin: 'https://proxy.example',
        bearer: 'synthetic-proxy-secret', chatModelId: 'fake', nativeMediaModelId: 'fake' } };
    const configPath = join(directory, 'runtime.json');
    await writeFile(configPath, JSON.stringify(raw), { mode: 0o600 });
    const config = await loadRuntimeConfig(configPath);
    const options: ApplicationOptions = { directory: config.stateDirectory, mediaRoot: config.mediaRoot,
      bearerToken: config.bearerToken, owner: { ...config.owner, channel: 'photon' },
      lineKey: config.lineKey, target: config.target, providerThreadId: config.providerThreadId,
      configuredChannelIdentity: configuredChannelIdentityForConfig(config),
      activation: { tenant_id: raw.tenantId, project_fingerprint: photonProjectFingerprint(project),
        owner_id: owner, account_id: raw.owner.accountId, line_key: raw.lineKey,
        provider_thread_id: thread } };
    const media: Array<{ threadId: string; summary: string; hashes: string[] }> = [];
    const turns: ModelRequest[] = [];
    let gallerySentAtMs = 0;
    let historyReads = 0;
    let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
    let generateDraft = false;
    const answers: string[] = [];
    const dependencies = { agent: { generateChat: async (turn: ModelRequest) => {
      turns.push(turn);
      return generateDraft ? runtime!.generateChat(turn) : { text: '' };
    },
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {},
      purgeThreadsBefore: async () => 0 },
    resolvePrepContinuation: async () => {
      historyReads++;
      return {messageId:'offline-parent-guid',sentAtMs:gallerySentAtMs};
    },
    transport: { send: async (request: { threadId: string; text?: string; media?: Array<{ data?: Buffer }> }) => {
      if (request.media) {
        gallerySentAtMs = Date.now();
        media.push({ threadId: request.threadId, summary: request.text!,
          hashes: request.media.map(item => createHash('sha256').update(item.data!).digest('hex')) });
        return { acceptance: 'accepted' as const, providerMessageIds: ['offline-parent-guid'],
          ...(order === 'receipt' ? {providerSentAtMs:gallerySentAtMs} : {}) };
      }
      answers.push(request.text!);
      return { acceptance: 'accepted' as const, providerMessageIds: [`answer-${answers.length}`] };
    } } };
    let app: Awaited<ReturnType<typeof startApplication>> | undefined;
    let proc: ReturnType<typeof spawn> | undefined;
    let closed: Promise<number | null> | undefined;
    try {
      app = await startApplication(options, dependencies);
      const admission = await app.receive({ channel: 'photon', eventId: 'owner-fixture-dm',
        accountId: raw.owner.accountId, senderId: owner, isDirect: true, text: 'Hello' },
      { threadId: thread });
      assert.equal(admission.status, 'silent');
      await writeFile(configPath, JSON.stringify({ ...raw, port: app.port }), { mode: 0o600 });
      proc = spawn(python, ['-I', '-B', child, sourceRoot, data, configPath], {
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: directory },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      closed = new Promise<number | null>((resolveClose, reject) => {
        proc!.once('close', code => resolveClose(code)); proc!.once('error', reject);
      });
      let stderr = '';
      proc.stderr!.setEncoding('utf8');
      proc.stderr!.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-8192); });
      let stdout = '';
      proc.stdout!.setEncoding('utf8');
      const ready = await new Promise<{ url: string; bearer: string; artifact: string;
        route_digest: string; payload_hash: string; dispatch_id: string; image_hashes: string[]; revision: string }>((resolveReady, reject) => {
        const timer = setTimeout(() => reject(new Error(`brief chain child timed out: ${stderr}`)), 40_000);
        proc!.stdout!.on('data', chunk => {
          stdout += String(chunk);
          if (stdout.length > 8192) { clearTimeout(timer); reject(new Error('child output exceeded bound')); return; }
          const line = stdout.split('\n').find(value => value.startsWith('{"ready":'));
          if (!line) return;
          try { clearTimeout(timer); resolveReady(JSON.parse(line)); }
          catch { clearTimeout(timer); reject(new Error('invalid child readiness')); }
        });
        proc!.once('close', () => { clearTimeout(timer); reject(new Error(`brief child exited: ${stderr}`)); });
      });
      assert.match(ready.route_digest, /^[a-f0-9]{64}$/);
      assert.match(ready.payload_hash, /^[a-f0-9]{64}$/);
      assert.equal(media.length, 1);
      assert.equal(media[0].threadId, thread);
      assert.deepEqual(media[0].hashes, ready.image_hashes);
      assert.equal(ready.route_digest, routeDigest({ channel: 'photon', tenantId: raw.tenantId,
        projectFingerprint: photonProjectFingerprint(project), ownerId: owner,
        accountId: raw.owner.accountId, lineKey: raw.lineKey }, thread));
      assert.equal(ready.payload_hash, createHash('sha256').update(JSON.stringify({
        summary: media[0].summary, images: media[0].hashes,
      })).digest('hex'));
      const sourceAuthority = { read: async () => ({ version: 2 as const, complete: true,
        revision: ready.revision, google_sources: [], bridge_sources: [] }) };
      const reads: unknown[] = [];
      const tool = { bridgeUrl: ready.url, readBearer: ready.bearer, sourceAuthority };
      const detail = createBriefDetailTool({ ...tool, fetch: async (url, init) => {
        reads.push(JSON.parse(String(init?.body)));
        return fetch(url, init);
      } });
      const draft = 'Jordan, thanks for inviting me to lunch on Thursday.';
      const usage = {inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
      let step = 0;
      const model = new MockLanguageModelV4({doGenerate: async request => {
        const prompt = JSON.stringify(request.prompt);
        if (step++ === 0) {
          assert.match(prompt, /A photo brief immediately precedes this message/);
          assert.ok(!prompt.includes('I can draft a reply'));
          return {content:[{type:'tool-call',toolCallId:'gallery-detail',toolName:'accepted_brief_detail',
            input:JSON.stringify({current_gallery:true,offset:0,limit:4000})}],
            finishReason:{unified:'tool-calls',raw:'STOP'},usage,warnings:[]};
        }
        if (step === 2) {
          assert.match(prompt, /I can draft a reply[.]/);
          assert.ok(prompt.includes(ready.artifact));
          return {content:[{type:'tool-call',toolCallId:'draft-skill',toolName:'skill',
            input:JSON.stringify({name:'sotto-draft-reply'})}],
            finishReason:{unified:'tool-calls',raw:'STOP'},usage,warnings:[]};
        }
        assert.match(prompt, /Sotto — Draft a Reply/);
        return {content:[{type:'text',text:draft}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
      }});
      runtime = createSottoAgentRuntime({model,storagePath:join(directory,'model.db'),instructions:'Offline gallery fixture.',
        skillPaths:[join(sourceRoot,'sotto-chief-of-staff/draft-reply/SKILL.md')],
        sourceAuthority,chatTools:()=>({accepted_brief_detail:detail})});
      generateDraft = true;
      assert.ok(!media[0].summary.includes('I can draft a reply'));
      const agreement = order === 'receipt' ? 'yes' : 'Sounds good';
      await app.receive({ channel: 'photon', eventId: 'owner-gallery-yes',
        accountId: raw.owner.accountId, senderId: owner, isDirect: true, text: agreement },
        { threadId: thread, sentAtMs: gallerySentAtMs + 1 });
      const continuation = turns.at(-1);
      assert.equal(continuation?.text, agreement);
      assert.ok('acceptedGalleryContext' in continuation, 'yes should select the exact accepted gallery receipt');
      const galleryReceipt = {dispatch_id:ready.dispatch_id,message_id:'offline-parent-guid',payload_hash:ready.payload_hash};
      assert.deepEqual(continuation.acceptedGalleryContext, galleryReceipt);
      assert.deepEqual(reads, [{gallery_receipt:galleryReceipt,offset:0,limit:4000}]);
      assert.deepEqual(answers, [draft]);
      assert.equal(step, 3);
      assert.equal(historyReads, order === 'receipt' ? 0 : 1);
      generateDraft = false;
      await app.receive({ channel:'photon',eventId:'owner-after-draft-yes',
        accountId:raw.owner.accountId,senderId:owner,isDirect:true,text:'yes' },
        {threadId:thread,sentAtMs:gallerySentAtMs+2});
      const later = turns.at(-1);
      assert.equal(later?.text, 'yes');
      assert.ok(later && (!('acceptedGalleryContext' in later) || !later.acceptedGalleryContext),
        'an intervening accepted response fences the old gallery');
      assert.deepEqual(await readBriefDetail(tool, {gallery_receipt:{...galleryReceipt,payload_hash:'0'.repeat(64)},
        offset:0,limit:80}, ready.revision), {status:'withheld',coverage:'accepted_artifacts_only'});

      const result = await readBriefDetail(tool, { latest: true, offset: 0, limit: 4000 }, ready.revision);
      assert.equal(result.status, 'found');
      if (result.status !== 'found') throw new Error('accepted brief withheld');
      assert.equal(result.id, ready.artifact);
      assert.match(result.text, /Good morning/);
      assert.match(result.text, /I can draft a reply[.]/);
      assert.equal(JSON.stringify(result).includes(ready.payload_hash), false);
      await writeFile(join(directory, 'revoked'), 'off');
      assert.deepEqual(await readBriefDetail(tool, {gallery_receipt:galleryReceipt,offset:0,limit:80}, ready.revision),
        {status:'withheld',coverage:'accepted_artifacts_only'});
      assert.deepEqual(await readBriefDetail(tool, { latest: true, offset: 0, limit: 80 }, ready.revision),
        { status: 'withheld', coverage: 'accepted_artifacts_only' });
    } finally {
      if (proc && closed && proc.exitCode === null && proc.signalCode === null) {
        proc.kill('SIGTERM');
        const timeout = setTimeout(() => proc!.kill('SIGKILL'), 5_000);
        try { assert.equal(await closed, 0); } finally { clearTimeout(timeout); }
      }
      await app?.close();
      await runtime?.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
}

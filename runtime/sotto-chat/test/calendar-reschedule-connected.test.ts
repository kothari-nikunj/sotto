import { calendarReviewText, type CalendarPreview } from '../src/tools/owner-calendar-contract.js';
import { createGoogleReadTools } from '../src/agent/google-read.js';
import { readSkillIndex } from '../src/tools/skills.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';
async function startReceiver(folder: string, rsvp?:string) {
    const python = resolve('../../.venv/bin/python');
    const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
            SOTTO_TEST_CALENDAR_MOVE: '1', ...(rsvp?.startsWith('combined_')?{SOTTO_TEST_CALENDAR_COMBINED:rsvp.slice(9)}:rsvp==='update'?{SOTTO_TEST_CALENDAR_UPDATE:'1'}:rsvp==='create'?{SOTTO_TEST_CALENDAR_CREATE:'1'}:rsvp?{SOTTO_TEST_CALENDAR_RSVP:rsvp}:{}), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
            SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
            SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
            PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
            PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
            SOTTO_TIMEZONE: 'America/Los_Angeles' } });
    let stderr = '';
    child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
        const line = await Promise.race([new Promise<string>((done, fail) => {
                let output = '';
                child.stdout.on('data', chunk => { output += String(chunk); const i = output.indexOf('\n'); if (i >= 0)
                    done(output.slice(0, i)); });
                child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
                child.once('error', fail);
            }), new Promise<never>((_, fail) => { timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000); })]);
        const parsed = JSON.parse(line) as {
            port: number;
            identity: OwnerToolIdentity;
            bearer: string;
            readBearer: string;
        };
        return { child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
    }
    catch (error) {
        child.kill('SIGKILL');
        throw error;
    }
    finally {
        if (timer)
            clearTimeout(timer);
    }
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
    if (child.exitCode !== null)
        return;
    const ended = once(child, 'exit');
    child.stdin.end('quit\n');
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    try {
        await ended;
    }
    finally {
        clearTimeout(timer);
    }
}
const usage = { inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 1, text: 1, reasoning: 0 } };
const sourceAuthority = { read: async () => ({ version: 2 as const, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: ['gmail' as const] }) };
import { createOwnerCalendarClient } from '../src/tools/owner-calendar-client.js';
import { createOwnerCalendarTools, CALENDAR_EXECUTION_GUIDE } from '../src/tools/owner-calendar-tool.js';
for (const scenario of [{providerUnknown:false,rsvp:'combined_single'},{providerUnknown:false,rsvp:'combined_occurrence'},{providerUnknown:true,rsvp:'combined_occurrence'},{providerUnknown:false,rsvp:'update'},{providerUnknown:true,rsvp:'update'},{providerUnknown:false},{providerUnknown:true},{providerUnknown:true,rsvp:'occurrence'},{providerUnknown:false,rsvp:'create'},{providerUnknown:true,rsvp:'create'},...['single','occurrence','all_day','series','series_all_day'].map(rsvp=>({providerUnknown:false,rsvp}))])
    test(`original Schedule ${'rsvp' in scenario?scenario.rsvp:'move'} full occurrence review and distinct yes; ${scenario.providerUnknown ? 'post-contact unknown remains held' : 'lost receipt reopens inspect-only'} and safe chat continues`, async () => {
        const providerUnknown=scenario.providerUnknown;
        const rsvp='rsvp' in scenario?scenario.rsvp:undefined;
        const create=rsvp==='create';const update=rsvp==='update';const combined=Boolean(rsvp?.startsWith('combined_'));
        let target='';
        const folder = mkdtempSync(join(tmpdir(), 'calendar-move-'));
        let journal = new OperationalJournal(join(folder, 'journal.sqlite'));
        let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined, runtime: ReturnType<typeof createSottoAgentRuntime> | undefined, coordinator: ConversationCoordinator | undefined;
        let exactReview='',phase = 'review', step = 0, lose = true, held = true, applies = 0;
        const sent: string[] = [];
        const sourceAuthority = { read: async () => ({ version: 2 as const, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: ['calendar' as const] }) };
        const mount = async () => {
            const client = createOwnerCalendarClient({ ...receiver!, fetch: async (input, init) => {
                    const body = JSON.parse(init!.body as string);
                    if (body.operation === 'inspect' && held)
                        throw new Error('synthetic inspect held');
                    if(body.operation==='apply')applies++;
                    const response = await fetch(input, init);
                    if(body.operation==='preview'&&(update||combined)){const preview=await response.clone().json() as CalendarPreview;if(preview.status==='preview')exactReview=calendarReviewText(preview);}
                    if (body.operation === 'preview')
                        assert.equal((await response.clone().json() as {
                            status: string;
                        }).status, 'preview', await response.clone().text());
                    if (body.operation === 'apply') {
                        assert.equal(response.status, 200, await response.clone().text());
                        if (lose) {
                            await response.body?.cancel();
                            throw new Error('lost committed response');
                        }
                    }
                    return response;
                } });
            const tools = { ...createOwnerCalendarTools({ client, journal: () => journal, sourceAuthority }), ...createGoogleReadTools({ ...receiver!, readBearer: receiver!.readBearer, sourceAuthority }) };
            const model = new MockLanguageModelV4({ doGenerate: async (request) => {
                    const prompt = JSON.stringify(request.prompt);
                    if (phase === 'safe')
                        return { content: [{ type: 'text', text: 'Hello.' }], finishReason: { unified: 'stop', raw: 'STOP' }, usage, warnings: [] };
                    const calls = phase === 'review' ? [{ name: 'skill', input: { name: 'sotto-schedule' } }, { name: 'calendar_list', input: { start: '2026-10-10T00:00:00-07:00', end: '2026-10-11T00:00:00-07:00' } }, ...(create?[]:[{ name: 'calendar_get', input: { event_id: rsvp?.startsWith('series')?target:'cedar_occurrence' } }]), combined?{name:'calendar_move_update_review',input:{event_id:'cedar_occurrence',start:'2026-10-10T12:00:00-07:00',end:'2026-10-10T13:00:00-07:00',fields:{summary:'Cedar planning',location:'',description:'New agenda\nExact second line'}}}:update?{name:'calendar_update_review',input:{event_id:'cedar_occurrence',fields:{summary:'Cedar planning',location:'',description:'New agenda\nExact second line'}}}:create?{name:'calendar_create_review',input:{summary:'Cedar planning',start:'2026-10-10T12:00:00-07:00',end:'2026-10-10T13:00:00-07:00',attendees:['DANA@example.com','dana@example.com'],location:'Cedar office, 10 Main Street',description:'Discuss roadmap\nAgree next steps'}}:rsvp?{name:'calendar_rsvp_review',input:{event_id:rsvp?.startsWith('series')?target:'cedar_occurrence',response:rsvp==='all_day'?'declined':rsvp?.startsWith('series')?'tentative':'accepted'}}:{ name: 'calendar_move_review', input: { event_id: 'cedar_occurrence', start: '2026-10-10T12:00:00-07:00', end: '2026-10-10T13:00:00-07:00' } }, null] : [{ name: combined?'calendar_move_update_confirm':update?'calendar_update_confirm':create?'calendar_create_confirm':rsvp?'calendar_rsvp_confirm':'calendar_move_confirm', input: {} }, null];
                    const call = calls[step++];
                    if (phase === 'review' && step === 2)
                        assert.ok(prompt.includes('THE TEST IS MECHANICAL'));
                    if(phase==='review'&&step===3&&create){assert.ok(prompt.includes('cedar_occurrence'));assert.ok(prompt.includes('Cedar *review*'));}
                    if(phase==='review'&&step===3&&rsvp?.startsWith('series')){
                        const values=(request.prompt as unknown as Array<{content?:unknown}>).flatMap(message=>Array.isArray(message.content)?message.content:[]);
                        const payload=JSON.stringify(values);
                        const match=/\"recurringEventId\":\"([^\"]+)\"/.exec(payload.replace(/\\"/g,'"'));
                        assert.ok(match,'actual list must supply master identity');target=match[1]!;
                        call!.input={event_id:target};
                    }
                    if (phase === 'review' && step === 4) {
                        assert.ok(prompt.includes('cedar_occurrence'));
                        assert.ok(prompt.includes('Cedar *review*'));
                    }
                    return { content: call ? [{ type: 'tool-call', toolCallId: phase + '-' + step, toolName: call.name, input: JSON.stringify(call.input) }] : [{ type: 'text', text: (update||combined)&&phase==='review'?exactReview:'normal completion' }], finishReason: { unified: call ? 'tool-calls' : 'stop', raw: 'STOP' }, usage, warnings: [] };
                } });
            runtime = createSottoAgentRuntime({ model, storagePath: join(folder, 'memory-' + Math.random() + '.db'), instructions: CALENDAR_EXECUTION_GUIDE, skillPaths: (await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(entry => entry.path), sourceAuthority, chatTools: () => tools });
            coordinator = new ConversationCoordinator({ journal, ownerCalendar: client, ownerPolicy: { channel: 'photon', accountId: 'account', ownerId: receiver!.identity.owner_id }, model: { generate: req => runtime!.generateChat({ ...req, threadId: 'memory', ownerId: 'owner' }) }, postPrepared: async (post) => { sent.push(post.text); return { acceptance: 'accepted', messageId: 'calendar-review-' + sent.length }; } });
        };
        const receive = (id: string, text: string) => coordinator!.receive({ channel: 'photon', accountId: 'account', senderId: receiver!.identity.owner_id, isDirect: true, eventId: id, text }, { threadId: receiver!.identity.route_id, destination: 'photon', lineKey: 'line' });
        try {
            receiver = await startReceiver(join(folder, 'receiver'),rsvp);
            await mount();
            assert.equal((await receive('proposal', combined?'Move the Cedar review to noon until 1pm and change its title, clear the location and replace the agenda':create?'Create Cedar planning at noon until 1pm on October 10 with Dana at dana@example.com, at Cedar office, 10 Main Street, agenda Discuss roadmap and Agree next steps':rsvp?'Respond to the Cedar invitation'+(rsvp?.startsWith('series')?' for the entire series':''):'Move the Cedar review to noon until 1pm on October 10')).status, 'accepted');
            if(!rsvp)assert.match(sent[0]!, /^Move: Cedar \*review\*\nCalendar: main calendar\nFrom:/);
            if(!rsvp)assert.match(sent[0]!, /Guests will be notified\. Move this meeting\?/);
            assert.equal(existsSync(join(folder, 'receiver/calendar-provider.jsonl')), false);
            assert.deepEqual(readFileSync(join(folder, 'receiver/calendar-reads.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line).operation), create?['calendar_list']:['calendar_list', 'calendar_get']);
            if(combined){assert.match(sent[0]!,/^Update: Cedar/);assert.match(sent[0]!,/From: Oct 10, 2026, 10:00 AM to 11:00 AM \(UTC-07:00\)/);assert.match(sent[0]!,/To: Oct 10, 2026, 12:00 PM to 1:00 PM \(UTC-07:00\)/);assert.match(sent[0]!,/After:\n\(empty\)/);}
            if(update){assert.match(sent[0]!,/^Update: Cedar/);assert.match(sent[0]!,/All day, Oct 10, 2026; ends before Oct 11, 2026/);assert.match(sent[0]!,/After:\n\(empty\)/);}
            if(create){assert.match(sent[0]!,/^I’ll add Cedar planning to your main calendar/);assert.match(sent[0]!,/Guests: dana@example.com, owner@example.com/);assert.match(sent[0]!,/Agenda:\nDiscuss roadmap\nAgree next steps/);assert.match(sent[0]!,/Create it and invite these guests\?/);}
            if(rsvp&&!create&&!update&&!combined){assert.match(sent[0]!,/^RSVP:/);assert.match(sent[0]!,/Your address: owner@example.com/);if(rsvp?.startsWith('series'))assert.match(sent[0]!,/entire recurring series/);if(rsvp?.endsWith('all_day'))assert.match(sent[0]!,/All day, Oct 10, 2026; ends before Oct 11, 2026/);}
            if (providerUnknown)
                writeFileSync(join(folder, 'receiver/calendar-provider-unknown'), '1');
            phase = 'confirm';
            step = 0;
            assert.equal((await receive('confirm', 'yes')).status, 'held');
            assert.equal(applies, 1);
            assert.equal(sent.length, 2, 'the review is followed immediately by one pending-effect notice');
            assert.match(sent.at(-1)!, /^I couldn’t confirm whether the calendar change happened\./);
            assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_calendar_tool' ORDER BY rowid DESC LIMIT 1").get()!.status, 'unknown', 'an accepted notice does not settle the calendar effect');
            const calls = readFileSync(join(folder, 'receiver/calendar-provider.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line));
            assert.equal(calls.length, 1);
            if(!create)assert.equal(calls[0].headers['If-Match'], '"version-1"');
            assert.equal(calls[0].args.sendUpdates, 'all');
            const event = JSON.parse(readFileSync(join(folder, create?'receiver/calendar-created.json':'receiver/calendar-event.json'), 'utf8'));
            if(combined){assert.deepEqual(calls[0].args.body,{start:{dateTime:'2026-10-10T12:00:00-07:00'},end:{dateTime:'2026-10-10T13:00:00-07:00'},summary:'Cedar planning',location:'',description:'New agenda\nExact second line'});assert.equal(event.start.dateTime,'2026-10-10T12:00:00-07:00');assert.equal(event.summary,'Cedar planning');assert.equal(event.location,'');assert.equal(event.description,'New agenda\nExact second line');assert.deepEqual(event.attendees,[{email:'dana@example.com'}]);assert.deepEqual(event.conferenceData,{conferenceId:'untouched'});if(rsvp==='combined_single'){assert.equal(event.recurringEventId,undefined);}else{assert.equal(event.recurringEventId,'cedar_series');assert.deepEqual(event.originalStartTime,{dateTime:'2026-10-10T10:00:00-07:00'});}}
            else if(update){assert.equal(event.summary,'Cedar planning');assert.equal(event.location,'');assert.equal(event.description,'New agenda\nExact second line');assert.deepEqual(event.start,{date:'2026-10-10'});assert.deepEqual(event.attendees,[{email:'dana@example.com'}]);assert.deepEqual(event.conferenceData,{conferenceId:'untouched'});assert.deepEqual(calls[0].args.body,{summary:'Cedar planning',location:'',description:'New agenda\nExact second line'});}
            else if(create){assert.match(event.id,/^[a-f0-9]{64}$/);assert.equal(event.summary,'Cedar planning');assert.equal(event.start.dateTime,'2026-10-10T12:00:00-07:00');assert.deepEqual(event.attendees,[{email:'dana@example.com'},{email:'owner@example.com'}]);assert.equal(event.location,'Cedar office, 10 Main Street');assert.equal(event.description,'Discuss roadmap\nAgree next steps');}
            else {
            assert.equal(event.id,rsvp?.startsWith('series')?'cedar_series':'cedar_occurrence');
            if(!rsvp)assert.equal(event.start.dateTime, '2026-10-10T12:00:00-07:00');
            if(!rsvp)assert.deepEqual(event.attendees, [{ email: 'dana@example.com' }]);
            else {assert.deepEqual(event.attendees[0],{email:'dana@example.com'});assert.equal(event.attendees[1].responseStatus,rsvp==='all_day'?'declined':rsvp?.startsWith('series')?'tentative':'accepted');assert.deepEqual(event.start,rsvp?.endsWith('all_day')?{date:'2026-10-10'}:{dateTime:'2026-10-10T10:00:00-07:00'});if(rsvp?.startsWith('series'))assert.deepEqual(event.recurrence,['RRULE:FREQ=WEEKLY']);}
            assert.equal(event.location, 'Cedar office');
            assert.deepEqual(event.conferenceData, { conferenceId: 'untouched' });
            }
            phase = 'safe';
            step = 0;
            assert.equal((await receive('hello', 'Hello')).status, 'accepted');
            // The pending-effect notice already arrived; fresh safe chat follows without repeating it.
            assert.match(sent.at(-2)!, /^I couldn’t confirm whether the calendar change happened\./);
            assert.equal(sent.at(-1), 'Hello.');
            assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm whether the calendar change happened.')).length, 1);
            await coordinator!.close();
            await runtime!.close();
            await stopReceiver(receiver.child);
            journal.close();
            journal = new OperationalJournal(join(folder, 'journal.sqlite'));
            journal.recoverInterrupted();
            journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_calendar_tool' AND status='unknown'").run();
            receiver = await startReceiver(join(folder, 'receiver'),rsvp);
            held = false;
            lose = false;
            await mount();
            const beforeRecovery = sent.length;
            await coordinator!.recover();
            assert.equal(sent.length, beforeRecovery + (providerUnknown ? 0 : 1));
            if (!providerUnknown) assert.match(sent.at(-1)!, /(?:updated|moved|added|accepted|declined|marked)/i);
            if (create && !providerUnknown) {
                assert.match(sent.at(-1)!, /^Added Cedar planning to your calendar\./);
                assert.match(sent.at(-1)!, /Oct 10, 2026, 12:00 PM to 1:00 PM \(UTC-07:00\)/);
                assert.match(sent.at(-1)!, /Location: Cedar office, 10 Main Street/);
                assert.doesNotMatch(sent.at(-1)!, /2026-10-10T/);
            }
            await coordinator!.recover();
            assert.equal(sent.length, beforeRecovery + (providerUnknown ? 0 : 1), 'late receipt acknowledgement is sent once');
            await receive('restarted-hello', 'Hello again');
            assert.equal(applies, 1);
            assert.equal(sent.at(-1), 'Hello.');
            assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm whether the calendar change happened.')).length, 1);
            assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_calendar_tool' ORDER BY rowid DESC LIMIT 1").get()!.status,
                providerUnknown ? 'unknown' : 'succeeded');
            if (providerUnknown) {
                assert.equal(journal.listUnknownActionsForThread(String(journal.db.prepare("SELECT id FROM inbound_events ORDER BY rowid DESC LIMIT 1").get()!.id)).length, 1);
            }
            assert.equal(readFileSync(join(folder, 'receiver/calendar-provider.jsonl'), 'utf8').trim().split('\n').length, 1);
        }
        finally {
            await coordinator?.close();
            await runtime?.close();
            if (receiver)
                await stopReceiver(receiver.child);
            journal.close();
            rmSync(folder, { recursive: true, force: true });
        }
    });

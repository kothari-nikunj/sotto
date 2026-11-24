import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {ConversationCoordinator} from '../src/conversation.js';
import {OperationalJournal} from '../src/journal/index.js';
import {createOwnerMeetingPrepClient} from '../src/tools/owner-meeting-prep-client.js';
import {createOwnerSkillExecution} from '../src/tools/owner-skill-execution.js';
import {createOwnerSkillRequestClient} from '../src/tools/owner-skill-request-client.js';
import {createSkillCommand} from '../src/tools/skill-command.js';

for (const path of ['direct', 'offered'] as const) {
  test(`late ${path} prep gets one truthful acknowledgement`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-window-'));
    const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
    const sends: string[] = [], admissions: string[] = [];
    const options = {
      identity: {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
        line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread',
        project_fingerprint: 'a'.repeat(64)},
      bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64),
      fetch: async (_url: URL | RequestInfo, init?: RequestInit) => {
        const {operation} = JSON.parse(String(init?.body));
        if (operation === 'preview') return Response.json({version: 1, status: 'match', offer_id: 'e'.repeat(64), kind: 'meeting_prep'});
        admissions.push(operation);
        return Response.json({version: 1, status: 'rejected', code: 'prep_window_closed'}, {status: 409});
      },
    };
    const ownerSkillRequest = createOwnerSkillRequestClient(options);
    const command = createSkillCommand(createOwnerSkillExecution({client: ownerSkillRequest, journal: () => journal,
      sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: ['calendar']})}}));
    const coordinator = new ConversationCoordinator({journal, ownerSkillRequest, ownerMeetingPrep: createOwnerMeetingPrepClient(options),
      ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'},
      model: {generate: async request => {
        assert.equal(path, 'direct');
        await command({id: 'meeting-prep', args: ['--focus', 'Dana']},
          {mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
            allowOwnerSkill: request.allowOwnerSkill, sourceRevision: 'a'.repeat(64), signal: request.signal});
        return 'Your meeting prep is queued.';
      }},
      postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: 'ack'};},
    });
    const inbound = {eventId: 'current-message', accountId: 'account', channel: 'photon' as const, senderId: 'owner', isDirect: true,
      text: path === 'direct' ? 'Prepare talking points for Dana' : 'Yes', providerSentAtMs: Date.now(),
      ...(path === 'offered' ? {replyBound: true, replyToMessageId: 'accepted-offer'} : {})};
    const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};
    try {
      await coordinator.receive(inbound, context);
      assert.deepEqual(admissions, ['admit']);
      assert.deepEqual(sends, ['There isn’t enough time to deliver prep before this meeting.']);
      assert.equal((await coordinator.receive(inbound, context)).status, 'duplicate');
      assert.equal(sends.length, 1);
    } finally {
      await coordinator.close();
      journal.close();
      rmSync(folder, {recursive: true, force: true});
    }
  });
}

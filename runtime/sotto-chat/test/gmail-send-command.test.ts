import assert from 'node:assert/strict';
import test from 'node:test';
import {createGmailSendCommand, parseGmailSendCommand} from '../src/tools/gmail-send-command.js';
import type {createOwnerGmailDraftTools} from '../src/tools/owner-gmail-draft-tool.js';

test('original new-email argv cannot inject unsupported send authority or reply targets', () => {
  for (const args of [
    ['gmail-send','--to','a@example.com','--body','Hi','--thread-id','thread123'],
    ['gmail-send','--to','a@example.com','--body','Hi','--to','b@example.com'],
    ['gmail-send','--to','a@example.com','--body','Hi','--approved','true'],
    ['gmail-send','--to','a@example.com','--body'],
    ['gmail-send-confirm','--body','Replacement'],
  ]) assert.throws(() => parseGmailSendCommand(args), /arguments are invalid/);
});

test('original gmail-send alias only reviews exact content and confirmation has no content arguments', async () => {
  const calls: Array<{name: string; input: unknown}> = [];
  const context = {mode:'chat',runId:'run',ownerRequestKey:'event',allowOwnerGmailDraft:true,sourceRevision:'a'.repeat(64)};
  const invoke = (name: string) => async (input: unknown, ctx: {requestContext: {get(key:string):unknown}}) => {
    assert.equal(ctx.requestContext.get('allowOwnerGmailDraft'), true);
    assert.equal(ctx.requestContext.get('ownerRequestKey'), 'event');
    calls.push({name,input});return {status:name==='review'?'proposed':'recorded'};
  };
  const tools = {gmail_new_send_review:{execute:invoke('review')},gmail_send_confirm:{execute:invoke('confirm')}} as unknown as ReturnType<typeof createOwnerGmailDraftTools>;
  const command = createGmailSendCommand(tools);
  const args=['gmail-send','--to','a@example.com','--body','Hi\nOriginal body'];
  const review = await command({id:'google-action',args},context);
  assert.equal(review.coverage,'reviewed_gmail_send');
  assert.deepEqual(calls,[{name:'review',input:{to:'a@example.com',subject:'',body:'Hi\nOriginal body'}}]);
  for (const denied of [{...context,mode:'unattended'},{...context,allowOwnerGmailDraft:false},{...context,ownerRequestKey:undefined},{...context,signal:AbortSignal.abort()}]) {
    await assert.rejects(command({id:'google-action',args},denied),/one current owner message/);
  }
  assert.equal(calls.length,1);
  await command({id:'google-action',args:['gmail-send-confirm']},context);
  assert.deepEqual(calls[1],{name:'confirm',input:{}});
});

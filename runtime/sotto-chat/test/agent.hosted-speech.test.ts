import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHostedSpeech, HostedSpeechError, HOSTED_SPEECH_LIMITS, type SpeechConnection } from '../src/agent/hosted-speech.js';
const wav = Buffer.alloc(46);
wav.write('RIFF'); wav.writeUInt32LE(38, 4); wav.write('WAVEfmt ', 8); wav.writeUInt32LE(16, 16);
wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(24000, 24);
wav.writeUInt32LE(48000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
wav.write('data', 36); wav.writeUInt32LE(2, 40);
const audio = { bytes: wav, mimeType: 'audio/wav' };
const spoken = { text: 'The meeting is at ten.', voice: 'Kore' };
const json = (body: unknown) => Response.json(body);
const googleText = (text = 'Hello.') => ({ candidates: [{ finishReason: 'STOP', content: { parts: [{ text }] } }],
  usageMetadata: { promptTokenCount: 12, candidatesTokenCount: 3, totalTokenCount: 15 } });
const googleAudio = () => ({ status: 'completed', steps: [{ type: 'model_output', content: [{
  type: 'audio', mime_type: 'audio/wav', data: wav.toString('base64'),
}] }], usage: { total_input_tokens: 3, total_output_tokens: 20, total_tokens: 23 } });
function setup(provider: SpeechConnection['provider'], handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  let calls = 0;
  const speech = createHostedSpeech({ provider, apiKey: 'test-key', transcriptionModelId: 'transcription-fixture', speechModelId: 'speech-fixture',
    fetch: (async (url, init) => { calls++; return handler(String(url), init!); }) as typeof fetch });
  return { speech, calls: () => calls };
}
const code = (expected: string) => (error: unknown) => error instanceof HostedSpeechError && error.code === expected && !error.message.includes('test-key');
test('Gemini transcription reuses explicit Google key and sends no tools or file URL', async () => {
  const { speech, calls } = setup('google', (url, init) => {
    assert.equal(url, 'https://generativelanguage.googleapis.com/v1beta/models/transcription-fixture:generateContent');
    assert.equal(new Headers(init.headers).get('x-goog-api-key'), 'test-key');
    assert.equal(init.redirect, 'error'); assert.equal(init.method, 'POST');
    const body = JSON.parse(String(init.body));
    assert.deepEqual(Object.keys(body).sort(), ['contents', 'generationConfig']);
    assert.deepEqual(body.contents[0].parts[1], { inlineData: { mimeType: 'audio/wav', data: wav.toString('base64') } });
    assert.equal(body.generationConfig.maxOutputTokens, 8192);
    return json(googleText());
  });
  assert.deepEqual(await speech.transcribe(audio), { text: 'Hello.', provider: 'google', modelId: 'transcription-fixture',
    usage: { reported: true, inputTokens: 12, outputTokens: 3, totalTokens: 15 } }); assert.equal(calls(), 1);
});
test('Google speech uses stateless unary Interactions WAV and preserves usage', async () => {
  const { speech } = setup('google', (url, init) => {
    assert.equal(url, 'https://generativelanguage.googleapis.com/v1beta/interactions');
    const body = JSON.parse(String(init.body));
    assert.equal(body.store, false); assert.equal(body.stream, false); assert.equal(body.model, 'speech-fixture');
    assert.deepEqual(body.response_format, { type: 'audio', mime_type: 'audio/wav' });
    assert.deepEqual(body.generation_config, { speech_config: [{ voice: 'Kore' }] });
    assert.deepEqual(body.input, [{ type: 'user_input', content: [{ type: 'text', text: spoken.text }] }]);
    return json(googleAudio());
  });
  const result = await speech.synthesize(spoken);
  assert.deepEqual(result.bytes, wav); assert.equal(result.mimeType, 'audio/wav'); assert.equal(result.usage.totalTokens, 23);
});
test('OpenAI transcription supplies file bytes, MIME, extension and JSON format', async () => {
  const { speech } = setup('openai', async (url, init) => {
    assert.equal(url, 'https://api.openai.com/v1/audio/transcriptions');
    assert.equal(new Headers(init.headers).get('authorization'), 'Bearer test-key');
    assert.equal(new Headers(init.headers).has('content-type'), false);
    const form = init.body as FormData;
    assert.equal(form.get('model'), 'transcription-fixture'); assert.equal(form.get('response_format'), 'json');
    const file = form.get('file') as File;
    assert.equal(file.name, 'audio.wav'); assert.equal(file.type, 'audio/wav');
    assert.deepEqual(Buffer.from(await file.arrayBuffer()), wav);
    return json({ text: 'Hi.', usage: { type: 'duration', seconds: 1.25 } });
  });
  assert.deepEqual((await speech.transcribe(audio)).usage, { reported: true, seconds: 1.25 });
});
test('OpenAI speech requests WAV and does not fabricate missing usage', async () => {
  const { speech } = setup('openai', (url, init) => {
    assert.equal(url, 'https://api.openai.com/v1/audio/speech');
    assert.deepEqual(JSON.parse(String(init.body)), { model: 'speech-fixture', input: spoken.text, voice: 'Kore', response_format: 'wav' });
    return new Response(wav, { headers: { 'content-type': 'audio/wav' } });
  }); assert.deepEqual((await speech.synthesize(spoken)).usage, { reported: false });
});
test('missing roles and unknown providers cannot fall back', async () => {
  let calls = 0;
  const speech = createHostedSpeech({ provider: 'google', apiKey: 'test-key', fetch: (async () => { calls++; throw Error(); }) as typeof fetch });
  await assert.rejects(speech.transcribe(audio), code('unsupported_capability'));
  await assert.rejects(speech.synthesize(spoken), code('unsupported_capability'));
  assert.throws(() => createHostedSpeech({ provider: 'qwen' as 'google', apiKey: 'test-key' }), code('unsupported_capability')); assert.equal(calls, 0);
});
test('configuration and media limits reject before provider contact', async () => {
  for (const apiKey of ['', 'key\nsecret', 'key secret', 'keyé']) assert.throws(() => createHostedSpeech({ provider: 'google', apiKey }), code('invalid_configuration'));
  assert.throws(() => createHostedSpeech({ provider: 'google', apiKey: 'ok', speechModelId: '../elsewhere' }), code('invalid_configuration'));
  const { speech, calls } = setup('google', () => { throw Error('must not call'); });
  for (const bad of [{ bytes: new Uint8Array(), mimeType: 'audio/wav' }, { bytes: wav, mimeType: 'image/png' },
    { bytes: Buffer.from('https://secret/audio'), mimeType: 'audio/wav' },
    { bytes: Buffer.alloc(HOSTED_SPEECH_LIMITS.inputAudioBytes + 1), mimeType: 'audio/wav' }]) await assert.rejects(speech.transcribe(bad), code('invalid_input'));
  for (const text of ['', 'x'.repeat(HOSTED_SPEECH_LIMITS.textCharacters + 1)]) await assert.rejects(speech.synthesize({ ...spoken, text }), code('invalid_input'));
  assert.equal(calls(), 0);
});
test('provider failures are sanitized and never retried or switched', async () => {
  for (const [status, expected] of [[401, 'unauthorized'], [403, 'unauthorized'], [429, 'rate_limited'], [500, 'unavailable'], [302, 'unavailable']] as const) {
    const { speech, calls } = setup('google', () => new Response('test-key private transcript', { status }));
    await assert.rejects(speech.transcribe(audio), code(expected)); assert.equal(calls(), 1);
  }
  await assert.rejects(setup('google', () => { throw Error('test-key private transcript'); }).speech.transcribe(audio), code('unavailable'));
});
test('pre-cancellation avoids contact; active cancellation settles even if fetch ignores signal', async () => {
  const { speech, calls } = setup('google', () => new Promise<Response>(() => {}));
  const controller = new AbortController(); controller.abort();
  await assert.rejects(speech.transcribe(audio, controller.signal), code('cancelled')); assert.equal(calls(), 0);
  const active = new AbortController(); const pending = speech.synthesize(spoken, active.signal); active.abort();
  await assert.rejects(pending, code('cancelled')); assert.equal(calls(), 1);
});
test('hard deadline aborts a hung transport without retry', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] }); let aborted = false;
  const { speech, calls } = setup('google', (_, init) => {
    init.signal!.addEventListener('abort', () => { aborted = true; }); return new Promise<Response>(() => {});
  });
  const pending = speech.transcribe(audio); const rejected = assert.rejects(pending, code('timed_out'));
  t.mock.timers.tick(HOSTED_SPEECH_LIMITS.deadlineMs);
  await rejected; assert.equal(aborted, true); assert.equal(calls(), 1);
});
test('stream byte limit applies without Content-Length and cancels stream', async () => {
  let cancelled = false;
  const { speech } = setup('google', () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(256 * 1024 + 1)); }, cancel() { cancelled = true; },
  }), { headers: { 'content-type': 'application/json' } }));
  await assert.rejects(speech.transcribe(audio), code('response_too_large')); assert.equal(cancelled, true);
});
test('cancelling a stalled response reader releases it', async () => {
  let cancelled = false; let started!: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const { speech } = setup('google', () => new Response(new ReadableStream({
    pull() { started(); }, cancel() { cancelled = true; },
  }), { headers: { 'content-type': 'application/json' } }));
  const controller = new AbortController(); const pending = speech.transcribe(audio, controller.signal);
  await ready; await new Promise(resolve => setImmediate(resolve)); controller.abort();
  await assert.rejects(pending, code('cancelled')); assert.equal(cancelled, true);
});
test('truncated, blocked, malformed and tool-call transcription replies fail closed', async () => {
  const invalid = [null, {}, { candidates: [] }, { candidates: [{ ...googleText().candidates[0], finishReason: 'MAX_TOKENS' }] },
    { ...googleText(), promptFeedback: { blockReason: 'SAFETY' } },
    { candidates: [{ finishReason: 'STOP', content: { parts: [{ functionCall: { name: 'send' } }] } }] },
    googleText('x'.repeat(HOSTED_SPEECH_LIMITS.transcriptCharacters + 1)), { ...googleText(), usageMetadata: { totalTokenCount: -1 } }];
  for (const value of invalid) await assert.rejects(setup('google', () => json(value)).speech.transcribe(audio), code('invalid_response'));
  assert.equal((await setup('google', () => json(googleText(''))).speech.transcribe(audio)).text, '');
});
test('speech rejects URLs, old PCM, multiple outputs, malformed base64 and fake WAVs', async () => {
  const base = googleAudio();
  const wrong = [{ ...base, status: 'failed' }, { ...base, steps: [...base.steps, ...base.steps] },
    ...[{ type: 'audio', uri: 'https://example.test/private.wav' }, { type: 'audio', mime_type: 'audio/l16', data: wav.toString('base64') },
      { type: 'audio', mime_type: 'audio/wav', data: '###' }, { type: 'audio', mime_type: 'audio/wav', data: Buffer.from('not audio').toString('base64') }]
      .map(content => ({ ...base, steps: [{ type: 'model_output', content: [content] }] }))];
  for (const value of wrong) await assert.rejects(setup('google', () => json(value)).speech.synthesize(spoken), code('invalid_response'));
  await assert.rejects(setup('openai', () => json({ audio: 'not audio' })).speech.synthesize(spoken), code('invalid_response'));
});
test('response length header enforces audio bound before reading', async () => {
  const { speech } = setup('openai', () => new Response(wav, { headers: {
    'content-type': 'audio/wav', 'content-length': String(HOSTED_SPEECH_LIMITS.outputAudioBytes + 1),
  } })); await assert.rejects(speech.synthesize(spoken), code('response_too_large'));
});
test('malformed JSON, MIME mismatch and invalid UTF-8 yield sanitized response errors', async () => {
  for (const response of [new Response('{bad', { headers: { 'content-type': 'application/json' } }),
    new Response(JSON.stringify(googleText()), { headers: { 'content-type': 'text/html' } }),
    new Response(new Uint8Array([0xff]), { headers: { 'content-type': 'application/json' } })]) {
    await assert.rejects(setup('google', () => response).speech.transcribe(audio), code('invalid_response'));
  }
});
test('successful unblocked prompt feedback is allowed and usage may remain unknown', async () => {
  const data = { candidates: googleText().candidates, promptFeedback: { safetyRatings: [] } };
  assert.deepEqual((await setup('google', () => json(data)).speech.transcribe(audio)).usage, { reported: false });
});

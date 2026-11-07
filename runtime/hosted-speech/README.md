# Hosted speech foundation

`../sotto-chat/src/agent/hosted-speech.ts` provides independently callable
`createHostedSpeech(connection).transcribe(audio)` and `.synthesize({text, voice})`.
It is not registered in the live chat runtime, installer, managed proxy, channel
handlers, or delivery outbox. This is a self-host adapter foundation, not a claim
that voice onboarding or audio delivery is complete.

The caller supplies the already-authorized Google or OpenAI key and explicit
transcription and/or speech model IDs. The same Google key can serve both roles;
there is no environment lookup, extra SDK, additional key requirement, model
installation, provider fallback, or retry. Missing roles and unsupported providers
fail explicitly. Chat provider changes do not silently switch speech providers.

Google transcription uses tool-free native `generateContent` audio input. Google
TTS uses the documented unary Interactions API with `store: false`, a configured
voice, and explicit WAV output. This targets WAV-capable TTS models such as the
documented Gemini 3.8 TTS family; it deliberately rejects older raw PCM output
rather than inventing an encoding. OpenAI uses multipart audio transcription and
the audio speech endpoint with WAV output. Model IDs and voice defaults belong to
the separately reviewed role catalog; this module has none.

## Contract and limits

- Input is existing bytes, never a URL or local filename. Audio uses Sotto's shared
  MIME/signature policy and 8 MiB limit (WAV, MP3, Ogg, WebM). Signature checking is
  not decoding or proof that an audio container is safe or playable.
- TTS input is at most 4,096 UTF-16 code units, transcript output at most 32,768;
  decoded speech at most 16 MiB. Response bodies are bounded while streaming.
- Each operation has a 60-second deadline and supports cancellation. Redirects,
  retries, remote output downloads, and tool calls are absent. No body/key logs,
  filesystem writes, sends, or speaker cloning are performed.
- Return values include selected provider/model and sanitized numeric usage.
  Missing usage is explicitly unknown, never zero. OpenAI binary TTS does not
  provide token usage here. This is not an accounting ledger or spend admission.
- Google truncation/blocked/nontext output and unexpected speech schemas fail
  closed. Empty transcription is allowed for silence. Generated transcription is
  untrusted model output; normal user-intent and action policies must still apply.
- The caller owns consent, audio normalization/duration limits, quota admission,
  transcript provenance, channel encoding/decoding, retention, and delivery.
  Cloud use must go through an admitted proxy path before wiring, not this direct
  credential adapter. `store: false` disables Interactions conversation storage;
  it does not replace the provider's broader data-processing terms.

## Verification

From `sotto-chat`, run `npm ci --ignore-scripts`, `npm run typecheck`, and
`node --import tsx --test test/agent.hosted-speech.test.ts`. Tests inject transport
fixtures and spend no provider tokens. Real-provider transcription quality,
voice quality, account model availability, quota, and latency require a separately
authorized smoke with a supplied key. No such smoke is claimed by these tests.

## Upstream references

- [Google audio understanding](https://ai.google.dev/gemini-api/docs/audio)
- [Google generateContent reference](https://ai.google.dev/api/generate-content)
- [Google speech generation and WAV output](https://ai.google.dev/gemini-api/docs/speech-generation)
- [Google Interactions reference](https://ai.google.dev/api/interactions-api)
- [Google stateless storage option](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [OpenAI transcription reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
- [OpenAI speech reference](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)

These HTTP schemas are implemented directly with the existing Node fetch API.
Provider contract changes should update these fixtures and pass an opt-in live
smoke before a release enables the adapter.

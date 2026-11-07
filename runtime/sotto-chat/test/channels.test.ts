import test from "node:test";
import assert from "node:assert/strict";
import { admitOwnerDirectMessage } from "../src/channels/policy.js";
import { normalizeInbound } from "../src/channels/normalize.js";
import { resolvePhotonCredentials, resolveTelegramCredentials } from "../src/channels/credentials.js";
import { createTelegramTransport, createPhotonTransport, type PhotonAdapterLike } from "../src/channels/transport.js";
import { PollerOwnership } from "../src/channels/lifecycle.js";
import { createiMessageAdapter } from "@photon-ai/chat-adapter-imessage";
import type { Inbound } from "../src/channels/types.js";

const inbound: Inbound = { channel: "telegram", eventId: "evt-1", accountId: "bot-a", senderId: "7", isDirect: true, text: " hello " };

test("owner direct-message admission precedes all downstream work and yields stable journal identity", () => {
  const owner = { channel: "telegram" as const, accountId: "bot-a", ownerId: "7" };
  assert.deepEqual(admitOwnerDirectMessage(normalizeInbound(inbound), owner), { admitted: true, journalKey: JSON.stringify(["telegram", "bot-a", "evt-1"]) });
  assert.deepEqual(admitOwnerDirectMessage({ ...inbound, isDirect: false }, owner), { admitted: false, reason: "not_direct" });
  assert.deepEqual(admitOwnerDirectMessage({ ...inbound, senderId: "8" }, owner), { admitted: false, reason: "not_owner" });
  assert.deepEqual(admitOwnerDirectMessage({ ...inbound, receiptOnly: true }, owner), { admitted: false, reason: "contentless" });
});

test("credentials are explicit, token-bound to Telegram pairing, and prevent Photon mode mixing", () => {
  assert.deepEqual(resolveTelegramCredentials({ telegramToken: "t", telegramLinkFile: { bot_token: "t", allowed_user: 7 } }), { botToken: "t", ownerId: "7" });
  assert.throws(() => resolveTelegramCredentials({ telegramToken: "wrong", telegramLinkFile: { bot_token: "t", allowed_user: 7 } }));
  for (const invalid of ['', 'NaN', -1, Infinity, 1.5, '700000000000000000000']) {
    assert.throws(() => resolveTelegramCredentials({ telegramToken: 't', telegramLinkFile: { bot_token: 't', allowed_user: invalid } }));
  }
  assert.deepEqual(resolvePhotonCredentials({ photonProjectId: "p", photonProjectSecret: "s" }), { projectId: "p", projectSecret: "s" });
  assert.throws(() => resolvePhotonCredentials({ photonProjectId: "p" }));
  assert.throws(() => resolvePhotonCredentials({}));
  assert.throws(() => resolvePhotonCredentials({ photonProjectId: "p", photonProjectSecret: "s", photonServerUrl: "h", photonApiKey: "k" }));
});

test("Telegram root receipt proves acceptance; contacted errors remain unknown", async () => {
  let calls = 0;
  const transport = createTelegramTransport({ postMessage: async (_thread, message) => {
    calls++;
    assert.equal(typeof message, "object");
    return { id: "tg:1" } as never;
  } });
  assert.deepEqual(await transport.send({ threadId: "thread", text: "hi" }), { acceptance: "accepted", providerMessageIds: ["tg:1"] });
  const statusOnly = createTelegramTransport({ postMessage: async () => { throw Object.assign(new Error("bad request"), { status: 400 }); } });
  assert.equal((await statusOnly.send({ threadId: "thread", text: "hi" })).acceptance, "unknown");
  const unknown = createTelegramTransport({ postMessage: async () => { throw new Error("socket closed after submit"); } });
  assert.equal((await unknown.send({ threadId: "thread", text: "hi" })).acceptance, "unknown");
  assert.equal((await transport.send({ threadId: "thread" })).acceptance, "not_attempted");
  assert.equal(calls, 1);
});

test("Photon gallery decodes the public thread ID and resolves its exact line before one grouped send", async () => {
  let groupedCalls = 0;
  let regularCalls = 0;
  let grouped: unknown;
  const owner = "+15551111111";
  const phone = "+15552222222";
  const threadId = `imessage:iMessage;-;${owner}~${phone}`;
  const codec = createiMessageAdapter({ local: false, projectId: "fixture-project", projectSecret: "fixture-secret",
    serverUrl: "", apiKey: "", phone: "", webhookSecret: "" });
  assert.equal(codec.isDM(threadId), true);
  const lookupCalls: Array<[string, { phone: string }?]> = [];
  const space = { send: async () => undefined };
  const publicSpaceNamespace = { get: async (chatGuid: string, options?: { phone: string }) => {
    lookupCalls.push([chatGuid, options]); return space;
  } };
  const app = { send: async (resolved: unknown, content: unknown) => {
    assert.equal(resolved, space);
    groupedCalls++; grouped = content;
    return { id: "p:parent", content: { items: [{ id: "p:1" }, { id: "p:2" }] } };
  } };
  const adapter = { app, postMessage: async (_thread: string, _body: unknown) => { regularCalls++; return { id: "regular" }; },
    decodeThreadId: codec.decodeThreadId.bind(codec), encodeThreadId: codec.encodeThreadId.bind(codec) } as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter, { gallerySpace: async (_app, chatGuid, selectedPhone) =>
    selectedPhone ? publicSpaceNamespace.get(chatGuid, { phone: selectedPhone }) : publicSpaceNamespace.get(chatGuid) });
  const receipt = await transport.send({ threadId, text: "Gallery", media: [
    { url: "https://example.invalid/a.jpg", mimeType: "image/jpeg" },
    { url: "https://example.invalid/b.jpg", mimeType: "image/jpeg" },
  ] });
  assert.deepEqual(receipt, { acceptance: "accepted", providerMessageIds: ["p:parent"] });
  assert.deepEqual(lookupCalls, [["iMessage;-;+15551111111", { phone: "+15552222222" }]],
    "public space lookup gets the decoded chat GUID and exact physical line, not the full Chat SDK thread ID");
  assert.equal(groupedCalls, 1);
  assert.equal(regularCalls, 0);
  assert.ok(grouped);

  for (const invalidThread of ["not-a-photon-thread", `imessage:iMessage;-;${owner}~not-a-line`]) {
    const bad = await transport.send({ threadId: invalidThread, text: "Gallery", media: [
      { url: "https://example.invalid/a.jpg", mimeType: "image/jpeg" },
    ] });
    assert.deepEqual(bad, { acceptance: "rejected", providerMessageIds: [], reason: "invalid gallery destination" });
  }
  assert.equal(groupedCalls, 1, "malformed destination is rejected before Spectrum send");
  assert.equal(lookupCalls.length, 1, "malformed destination is rejected before space lookup");

  const plain = await transport.send({ threadId, text: "Plain text" });
  assert.deepEqual(plain, { acceptance: "accepted", providerMessageIds: ["regular"] });
  assert.equal(regularCalls, 1, "text-only sends keep the adapter postMessage route");
});

test("media validation does not silently drop oversized or excessive gallery items", async () => {
  let sends = 0;
  const adapter = { postMessage: async () => { sends++; return { id: "x" }; } };
  const transport = createTelegramTransport(adapter as never);
  assert.equal((await transport.send({ threadId: "t", media: [{ url: "https://e.invalid/a", sizeBytes: 21 * 1024 * 1024 }] })).acceptance, "rejected");
  assert.equal((await transport.send({ threadId: "t", media: Array.from({ length: 5 }, (_, i) => ({ url: `https://e.invalid/${i}` })) })).acceptance, "rejected");
  assert.equal(sends, 0);
});

test("poller ownership is single and releasable", () => {
  const ownership = new PollerOwnership();
  const release = ownership.claim("telegram-primary");
  assert.equal(ownership.owner, "telegram-primary");
  assert.throws(() => ownership.claim("telegram-secondary"));
  release();
  assert.equal(ownership.owner, undefined);
});

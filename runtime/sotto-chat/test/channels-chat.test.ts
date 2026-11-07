import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Chat, Message } from "chat";
import type { Adapter, Logger } from "chat";
import { DurableAdmissionChat } from "../src/channels/chat.js";
import { SqliteChatStateAdapter } from "../src/channels/state.js";

function makeMessage(id = "provider-event-1"): Message {
  return new Message({
    id,
    threadId: "fake:dm:owner",
    text: "hello",
    formatted: { type: "root", children: [{ type: "paragraph", children: [{ type: "text", value: "hello" }] }] },
    raw: { providerEventId: id, senderId: "owner", isDirect: true },
    author: { userId: "owner", userName: "owner", fullName: "Owner", isBot: false, isMe: false },
    metadata: { dateSent: new Date(), edited: false },
    attachments: [],
  });
}

function fakeAdapter(): Adapter {
  return {
    name: "fake",
    userName: "bot",
    channelIdFromThreadId: (threadId: string) => "fake:dm",
    isDM: () => true,
    decodeThreadId: (threadId: string) => threadId,
    encodeThreadId: (value: unknown) => String(value),
    postMessage: async () => ({ id: "out" }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {},
    removeReaction: async () => {},
    deleteMessage: async () => {},
    editMessage: async () => ({ id: "out" }),
    startTyping: async () => {},
    renderFormatted: () => "",
    initialize: async () => {},
  } as unknown as Adapter;
}
const logger: Logger = { debug() {}, info() {}, warn() {}, error() {}, child: () => logger };

async function withTempChat(run: (path: string) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "sotto-ingress-"));
  try { await run(join(dir, "sdk.db")); }
  finally { rmSync(dir, { recursive: true, force: true }); }
}

test("real Chat SDK is not called until durable admission succeeds; retry can reach handler then dedupe", async () => withTempChat(async (path) => {
  const db = new DatabaseSync(path);
  db.exec("CREATE TABLE inbox_test (event_id TEXT PRIMARY KEY, status TEXT NOT NULL)");
  const state = new SqliteChatStateAdapter(db);
  let admissionAttempts = 0;
  let handlerCalls = 0;
  const adapter = fakeAdapter();
  const chat = new DurableAdmissionChat({
    userName: "bot",
    adapters: { fake: adapter },
    state,
    concurrency: "concurrent",
    logger,
  }, async ({ rawMessage }) => {
    admissionAttempts++;
    if (admissionAttempts === 1) throw new Error("inbox database temporarily unavailable");
    assert.deepEqual(rawMessage, { providerEventId: "provider-event-1", senderId: "owner", isDirect: true });
    db.prepare("INSERT INTO inbox_test(event_id,status) VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status").run("provider-event-1", "accepted");
    return { admitted: true, handoff: "sdk" };
  });
  chat.onDirectMessage(async () => { handlerCalls++; });
  const message = makeMessage();
  const failedAttempt = chat.processMessage(adapter, message.threadId, message);
  assert.equal(await state.get("dedupe:fake:provider-event-1"), null);
  await assert.rejects(failedAttempt, /temporarily unavailable/);
  const admittedAttempt = chat.processMessage(adapter, message.threadId, message);
  assert.equal((db.prepare("SELECT status FROM inbox_test WHERE event_id=?").get("provider-event-1") as { status: string } | undefined)?.status, "accepted");
  await admittedAttempt;
  assert.equal(await state.get("dedupe:fake:provider-event-1"), true);
  await chat.processMessage(adapter, message.threadId, message);
  assert.equal(admissionAttempts, 3);
  assert.equal(handlerCalls, 1);
  await chat.shutdown();
  db.close();
}));

test("ignore and coordinator handoff never dispatch the Chat SDK handler", async () => withTempChat(async (path) => {
  const db = new DatabaseSync(path);
  const state = new SqliteChatStateAdapter(db);
  const adapter = fakeAdapter();
  let handlerCalls = 0;
  const outcomes: Array<"ignore" | "coordinator"> = ["ignore", "coordinator"];
  const chat = new DurableAdmissionChat({
    userName: "bot", adapters: { fake: adapter }, state, concurrency: "concurrent",
    logger,
  }, () => {
    const next = outcomes.shift()!;
    return next === "ignore" ? { admitted: false, reason: "unauthorized or contentless" } : { admitted: true, handoff: "coordinator" };
  });
  chat.onDirectMessage(async () => { handlerCalls++; });
  await chat.processMessage(adapter, makeMessage().threadId, makeMessage("ignored"));
  await chat.processMessage(adapter, makeMessage().threadId, makeMessage("journal-owned"));
  assert.equal(handlerCalls, 0);
  await chat.shutdown();
  db.close();
}));

test("base Chat exposes public processMessage as the same SDK instance method", () => {
  assert.equal(typeof Chat.prototype.processMessage, "function");
  assert.equal(typeof DurableAdmissionChat.prototype.processMessage, "function");
});

test("a failed admission still rejects for awaiting adapters but never becomes an unhandled rejection", async () => withTempChat(async (path) => {
  const db = new DatabaseSync(path);
  const adapter = fakeAdapter();
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown) => { unhandled.push(reason); };
  process.on("unhandledRejection", onUnhandled);
  const chat = new DurableAdmissionChat({ userName: "bot", adapters: { fake: adapter }, state: new SqliteChatStateAdapter(db),
    concurrency: "concurrent", logger }, ({ message }) => {
    if (message.id !== "lazy") throw new Error("synchronous admission conflict");
    return Promise.reject(new Error("asynchronous admission failure"));
  });
  try {
    // Telegram awaits processMessage: the failure stays visible, so the update is retried.
    await assert.rejects(chat.processMessage(adapter, makeMessage().threadId, makeMessage("awaited")), /synchronous admission conflict/);
    // Photon drops the returned promise without a handler.
    void chat.processMessage(adapter, makeMessage().threadId, makeMessage("dropped"));
    void chat.processMessage(adapter, makeMessage().threadId, async () => makeMessage("lazy"));
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual(unhandled, []);
  } finally {
    process.removeListener("unhandledRejection", onUnhandled);
    await chat.shutdown();
    db.close();
  }
}));

test("pinned Photon non-awaiting intake survives a conflicting redelivery of an admitted event", { timeout: 60_000 }, () => {
  const child = fileURLToPath(new URL("./photon-admission-conflict-child.ts", import.meta.url));
  const result = spawnSync(process.execPath, ["--import", "tsx", child], { encoding: "utf8", timeout: 50_000,
    env: { ...process.env, MASTRA_TELEMETRY_DISABLED: "1" } });
  assert.equal(result.status, 0, "the runtime process must not exit on an admission conflict");
  assert.match(result.stdout, /SURVIVED admissions=2 pending=1/);
  assert.doesNotMatch(result.stdout + result.stderr, /redelivered with different content/);
});

import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SqliteChatStateAdapter } from "../src/channels/state.js";
import type { QueueEntry } from "chat";

function withDb(run: (path: string) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "sotto-state-"));
  const path = join(dir, "state.db");
  return run(path).finally(() => rmSync(dir, { recursive: true, force: true }));
}
const entry = (text: string, now: number, ttl = 10_000) => ({
  enqueuedAt: now, expiresAt: now + ttl, message: { text },
}) as unknown as QueueEntry;

test("key values, subscriptions, and queue survive close/reopen; disconnect leaves shared DB open", async () => withDb(async (path) => {
  const db = new DatabaseSync(path);
  const state = new SqliteChatStateAdapter(db);
  await state.connect();
  await state.set("sdk:digest", { cursor: 8 });
  await state.subscribe("telegram:owner");
  await state.enqueue("telegram:owner", entry("first", Date.now()), 10);
  await state.disconnect();
  assert.doesNotThrow(() => db.prepare("SELECT 1").get());
  db.close();
  const reopenedDb = new DatabaseSync(path);
  const reopened = new SqliteChatStateAdapter(reopenedDb);
  assert.deepEqual(await reopened.get("sdk:digest"), { cursor: 8 });
  assert.equal(await reopened.isSubscribed("telegram:owner"), true);
  assert.equal((await reopened.dequeue("telegram:owner"))?.message.text, "first");
  reopenedDb.close();
}));

test("TTL expires KV, per-item list values and queued entries", async () => withDb(async (path) => {
  let now = 1000;
  const db = new DatabaseSync(path);
  const state = new SqliteChatStateAdapter(db, { now: () => now });
  await state.set("short", "value", 10);
  await state.appendToList("events", "old", { ttlMs: 10 });
  await state.appendToList("events", "long", { ttlMs: 50 });
  await state.enqueue("thread", entry("expires", now, 10), 10);
  now = 1011;
  assert.equal(await state.get("short"), null);
  assert.deepEqual(await state.getList("events"), ["old", "long"]);
  assert.equal(await state.dequeue("thread"), null);
  assert.equal(await state.queueDepth("thread"), 0);
  now = 1051;
  assert.deepEqual(await state.getList("events"), []);
  db.close();
}));

test("locks contend, expire, and stale lease release/extend cannot mutate takeover", async () => withDb(async (path) => {
  let now = 100;
  const firstDb = new DatabaseSync(path);
  const secondDb = new DatabaseSync(path);
  const firstState = new SqliteChatStateAdapter(firstDb, { now: () => now });
  const secondState = new SqliteChatStateAdapter(secondDb, { now: () => now });
  const old = await firstState.acquireLock("thread", 20);
  assert.ok(old);
  assert.equal(await secondState.acquireLock("thread", 20), null);
  now = 121;
  const current = await secondState.acquireLock("thread", 30);
  assert.ok(current);
  await firstState.releaseLock(old!);
  assert.equal(await secondState.extendLock(old!, 30), false);
  assert.equal(await firstState.extendLock(current!, 30), true);
  await firstState.forceReleaseLock("thread");
  assert.equal(await secondState.extendLock(current!, 30), false);
  firstDb.close();
  secondDb.close();
}));

test("setIfNotExists is atomic and succeeds again after TTL", async () => withDb(async (path) => {
  let now = 0;
  const leftDb = new DatabaseSync(path);
  const rightDb = new DatabaseSync(path);
  const left = new SqliteChatStateAdapter(leftDb, { now: () => now });
  const right = new SqliteChatStateAdapter(rightDb, { now: () => now });
  const answers = await Promise.all([left.setIfNotExists("dedupe:event", 1, 5), right.setIfNotExists("dedupe:event", 2, 5)]);
  assert.equal(answers.filter(Boolean).length, 1);
  assert.ok([1, 2].includes((await left.get<number>("dedupe:event"))!));
  now = 5;
  assert.equal(await right.setIfNotExists("dedupe:event", 3, 5), true);
  assert.equal(await left.get("dedupe:event"), 3);
  leftDb.close();
  rightDb.close();
}));

test("list append trims oldest entries and queue is FIFO with bounded newest retention", async () => withDb(async (path) => {
  const db = new DatabaseSync(path);
  const state = new SqliteChatStateAdapter(db);
  await state.appendToList("list", 1, { maxLength: 2 });
  await state.appendToList("list", 2, { maxLength: 2 });
  await state.appendToList("list", 3, { maxLength: 2 });
  assert.deepEqual(await state.getList("list"), [2, 3]);
  await state.enqueue("q", entry("1", Date.now()), 2);
  await state.enqueue("q", entry("2", Date.now()), 2);
  assert.equal(await state.enqueue("q", entry("3", Date.now()), 2), 2);
  assert.equal((await state.dequeue("q"))?.message.text, "2");
  assert.equal((await state.dequeue("q"))?.message.text, "3");
  db.close();
}));

test("pruneExpired removes unobserved expired rows transactionally and preserves live/permanent state", async () => withDb(async (path) => {
  let now = 500;
  const db = new DatabaseSync(path);
  const state = new SqliteChatStateAdapter(db, { now: () => now });

  await state.set("kv-expired-unread", "gone", 10);
  await state.set("kv-live", "here", 30);
  await state.set("kv-permanent", "forever");
  await state.appendToList("list-expired-unread", "gone", { ttlMs: 10 });
  await state.appendToList("list-live", "here", { ttlMs: 30 });
  await state.subscribe("permanent-subscription");
  await state.enqueue("queue-expired-unread", entry("gone", now, 10), 10);
  await state.enqueue("queue-live", entry("here", now, 30), 10);
  assert.ok(await state.acquireLock("lock-expired", 10));
  const liveLock = await state.acquireLock("lock-live", 30);
  assert.ok(liveLock);

  now = 510;
  // No get/getList/dequeue/queueDepth call has lazily touched any expired key.
  assert.equal(await state.pruneExpired(), 4);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sdk_kv WHERE key='kv-expired-unread'").get()?.n, 0);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sdk_lists WHERE key='list-expired-unread'").get()?.n, 0);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sdk_queue WHERE thread_id='queue-expired-unread'").get()?.n, 0);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM sdk_locks WHERE thread_id='lock-expired'").get()?.n, 0);

  assert.equal(await state.get("kv-live"), "here");
  assert.equal(await state.get("kv-permanent"), "forever");
  assert.deepEqual(await state.getList("list-live"), ["here"]);
  assert.equal(await state.queueDepth("queue-live"), 1);
  assert.equal(await state.isSubscribed("permanent-subscription"), true);
  assert.equal(await state.extendLock(liveLock!, 20), true);
  db.close();
}));

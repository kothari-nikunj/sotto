import { randomUUID } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";
import type { Lock, QueueEntry, StateAdapter } from "chat";

const MAX_KEY_BYTES = 1024;
const MAX_VALUE_BYTES = 1024 * 1024;
const MAX_QUEUE_SIZE = 100;
const MAX_LIST_SIZE = 1000;

/** A Chat SDK state adapter over a caller-owned SQLite connection. Never closes or recovers it. */
export class SqliteChatStateAdapter implements StateAdapter {
  readonly #db: DatabaseSync;
  readonly #now: () => number;

  constructor(db: DatabaseSync, options: { now?: () => number } = {}) {
    this.#db = db;
    this.#now = options.now ?? Date.now;
    db.exec("CREATE TABLE IF NOT EXISTS sdk_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at INTEGER)");
    db.exec("CREATE TABLE IF NOT EXISTS sdk_lists (key TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL, expires_at INTEGER)");
    db.exec("CREATE INDEX IF NOT EXISTS sdk_lists_key_seq ON sdk_lists(key, seq)");
    db.exec("CREATE TABLE IF NOT EXISTS sdk_subscriptions (thread_id TEXT PRIMARY KEY)");
    db.exec("CREATE TABLE IF NOT EXISTS sdk_locks (thread_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at INTEGER NOT NULL)");
    db.exec("CREATE TABLE IF NOT EXISTS sdk_queue (thread_id TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL, expires_at INTEGER NOT NULL)");
    db.exec("CREATE INDEX IF NOT EXISTS sdk_queue_thread_seq ON sdk_queue(thread_id, seq)");
  }

  async connect(): Promise<void> {}
  async disconnect(): Promise<void> {}

  /** Delete every expired SDK-owned row without touching permanent keys or subscriptions. */
  async pruneExpired(): Promise<number> {
    const now = this.#now();
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      const keyValues = this.#db.prepare("DELETE FROM sdk_kv WHERE expires_at IS NOT NULL AND expires_at<=?").run(now).changes;
      const listEntries = this.#db.prepare("DELETE FROM sdk_lists WHERE expires_at IS NOT NULL AND expires_at<=?").run(now).changes;
      const queueEntries = this.#db.prepare("DELETE FROM sdk_queue WHERE expires_at<=?").run(now).changes;
      const locks = this.#db.prepare("DELETE FROM sdk_locks WHERE expires_at<=?").run(now).changes;
      this.#db.exec("COMMIT");
      return Number(keyValues) + Number(listEntries) + Number(queueEntries) + Number(locks);
    } catch (error) {
      this.#db.exec("ROLLBACK");
      throw error;
    }
  }

  async get<T = unknown>(key: string): Promise<T | null> {
    this.#key(key);
    const row = this.#db.prepare("SELECT value, expires_at FROM sdk_kv WHERE key=?").get(key) as { value: string; expires_at: number | null } | undefined;
    if (!row) return null;
    if (row.expires_at !== null && row.expires_at <= this.#now()) {
      this.#db.prepare("DELETE FROM sdk_kv WHERE key=? AND expires_at<=?").run(key, this.#now());
      return null;
    }
    return this.#decode<T>(row.value);
  }

  async set<T = unknown>(key: string, value: T, ttlMs?: number): Promise<void> {
    this.#key(key);
    this.#db.prepare("INSERT INTO sdk_kv(key,value,expires_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at")
      .run(key, this.#encode(value), this.#expires(ttlMs));
  }

  async setIfNotExists(key: string, value: unknown, ttlMs?: number): Promise<boolean> {
    this.#key(key);
    const encoded = this.#encode(value);
    const now = this.#now();
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      this.#db.prepare("DELETE FROM sdk_kv WHERE key=? AND expires_at IS NOT NULL AND expires_at<=?").run(key, now);
      const result = this.#db.prepare("INSERT INTO sdk_kv(key,value,expires_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING").run(key, encoded, this.#expires(ttlMs));
      this.#db.exec("COMMIT");
      return result.changes === 1;
    } catch (error) {
      this.#db.exec("ROLLBACK");
      throw error;
    }
  }

  async delete(key: string): Promise<void> {
    this.#key(key);
    this.#db.prepare("DELETE FROM sdk_kv WHERE key=?").run(key);
  }

  async appendToList(key: string, value: unknown, options: { maxLength?: number; ttlMs?: number } = {}): Promise<void> {
    this.#key(key);
    const encoded = this.#encode(value);
    const max = this.#limit(options.maxLength, MAX_LIST_SIZE);
    const expires = this.#expires(options.ttlMs);
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      this.#db.prepare("DELETE FROM sdk_lists WHERE key=? AND expires_at IS NOT NULL AND expires_at<=?").run(key, this.#now());
      this.#db.prepare("INSERT INTO sdk_lists(key,value,expires_at) VALUES(?,?,?)").run(key, encoded, expires);
      this.#db.prepare("UPDATE sdk_lists SET expires_at=? WHERE key=?").run(expires, key);
      this.#db.prepare("DELETE FROM sdk_lists WHERE key=? AND seq NOT IN (SELECT seq FROM sdk_lists WHERE key=? ORDER BY seq DESC LIMIT ?)")
        .run(key, key, max);
      this.#db.exec("COMMIT");
    } catch (error) {
      this.#db.exec("ROLLBACK");
      throw error;
    }
  }

  async getList<T = unknown>(key: string): Promise<T[]> {
    this.#key(key);
    this.#db.prepare("DELETE FROM sdk_lists WHERE key=? AND expires_at IS NOT NULL AND expires_at<=?").run(key, this.#now());
    const rows = this.#db.prepare("SELECT value FROM sdk_lists WHERE key=? ORDER BY seq ASC").all(key) as Array<{ value: string }>;
    return rows.map((row) => this.#decode<T>(row.value));
  }

  async subscribe(threadId: string): Promise<void> {
    this.#key(threadId);
    this.#db.prepare("INSERT INTO sdk_subscriptions(thread_id) VALUES(?) ON CONFLICT(thread_id) DO NOTHING").run(threadId);
  }
  async unsubscribe(threadId: string): Promise<void> {
    this.#key(threadId);
    this.#db.prepare("DELETE FROM sdk_subscriptions WHERE thread_id=?").run(threadId);
  }
  async isSubscribed(threadId: string): Promise<boolean> {
    this.#key(threadId);
    return Boolean(this.#db.prepare("SELECT 1 FROM sdk_subscriptions WHERE thread_id=?").get(threadId));
  }

  async acquireLock(threadId: string, ttlMs: number): Promise<Lock | null> {
    this.#key(threadId);
    if (!Number.isFinite(ttlMs) || ttlMs <= 0) return null;
    const now = this.#now();
    const token = randomUUID();
    const result = this.#db.prepare("INSERT INTO sdk_locks(thread_id,token,expires_at) VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at WHERE sdk_locks.expires_at<=?")
      .run(threadId, token, now + ttlMs, now);
    return result.changes === 1 ? { threadId, token, expiresAt: now + ttlMs } : null;
  }

  async extendLock(lock: Lock, ttlMs: number): Promise<boolean> {
    if (!Number.isFinite(ttlMs) || ttlMs <= 0) return false;
    const now = this.#now();
    const result = this.#db.prepare("UPDATE sdk_locks SET expires_at=? WHERE thread_id=? AND token=? AND expires_at>?")
      .run(now + ttlMs, lock.threadId, lock.token, now);
    return result.changes === 1;
  }
  async releaseLock(lock: Lock): Promise<void> {
    this.#db.prepare("DELETE FROM sdk_locks WHERE thread_id=? AND token=?").run(lock.threadId, lock.token);
  }
  async forceReleaseLock(threadId: string): Promise<void> {
    this.#key(threadId);
    this.#db.prepare("DELETE FROM sdk_locks WHERE thread_id=?").run(threadId);
  }

  async enqueue(threadId: string, entry: QueueEntry, maxSize: number): Promise<number> {
    this.#key(threadId);
    const encoded = this.#encode(entry);
    const cap = this.#limit(maxSize, 10, MAX_QUEUE_SIZE);
    const now = this.#now();
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      this.#db.prepare("DELETE FROM sdk_queue WHERE thread_id=? AND expires_at<=?").run(threadId, now);
      this.#db.prepare("INSERT INTO sdk_queue(thread_id,value,expires_at) VALUES(?,?,?)").run(threadId, encoded, entry.expiresAt);
      this.#db.prepare("DELETE FROM sdk_queue WHERE thread_id=? AND seq NOT IN (SELECT seq FROM sdk_queue WHERE thread_id=? ORDER BY seq DESC LIMIT ?)")
        .run(threadId, threadId, cap);
      const row = this.#db.prepare("SELECT COUNT(*) AS depth FROM sdk_queue WHERE thread_id=?").get(threadId) as { depth: number };
      this.#db.exec("COMMIT");
      return row.depth;
    } catch (error) {
      this.#db.exec("ROLLBACK");
      throw error;
    }
  }

  async dequeue(threadId: string): Promise<QueueEntry | null> {
    this.#key(threadId);
    this.#db.exec("BEGIN IMMEDIATE");
    try {
      this.#db.prepare("DELETE FROM sdk_queue WHERE thread_id=? AND expires_at<=?").run(threadId, this.#now());
      const row = this.#db.prepare("SELECT seq,value FROM sdk_queue WHERE thread_id=? ORDER BY seq ASC LIMIT 1").get(threadId) as { seq: number; value: string } | undefined;
      if (!row) { this.#db.exec("COMMIT"); return null; }
      this.#db.prepare("DELETE FROM sdk_queue WHERE seq=?").run(row.seq);
      this.#db.exec("COMMIT");
      return this.#decode<QueueEntry>(row.value);
    } catch (error) {
      this.#db.exec("ROLLBACK");
      throw error;
    }
  }

  async queueDepth(threadId: string): Promise<number> {
    this.#key(threadId);
    this.#db.prepare("DELETE FROM sdk_queue WHERE thread_id=? AND expires_at<=?").run(threadId, this.#now());
    const row = this.#db.prepare("SELECT COUNT(*) AS depth FROM sdk_queue WHERE thread_id=?").get(threadId) as { depth: number };
    return row.depth;
  }

  #key(key: string): void {
    if (typeof key !== "string" || Buffer.byteLength(key, "utf8") > MAX_KEY_BYTES) throw new Error("State key exceeds limit");
  }
  #limit(value: number | undefined, fallback: number, maximum = fallback): number {
    if (value === undefined) return fallback;
    if (!Number.isSafeInteger(value) || value < 1) throw new Error("Limit must be a positive integer");
    return Math.min(value, maximum);
  }
  #expires(ttlMs?: number): number | null {
    if (ttlMs === undefined) return null;
    if (!Number.isFinite(ttlMs)) throw new Error("TTL must be finite");
    return this.#now() + Math.max(0, ttlMs);
  }
  #encode(value: unknown): string {
    const encoded = JSON.stringify(value, (_key, item: unknown) => Buffer.isBuffer(item) ? { __sottoBuffer: item.toString("base64") } : item);
    if (encoded === undefined) throw new Error("State value is not JSON serializable");
    if (Buffer.byteLength(encoded, "utf8") > MAX_VALUE_BYTES) throw new Error("State value exceeds limit");
    return encoded;
  }
  #decode<T>(value: string): T {
    return JSON.parse(value, (_key, item: unknown) => {
      if (item && typeof item === "object" && "__sottoBuffer" in item && typeof item.__sottoBuffer === "string") return Buffer.from(item.__sottoBuffer, "base64");
      return item;
    }) as T;
  }
}

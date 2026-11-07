import { chmod, lstat, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

const protocol = 'sqlite-exclusive-v1';
// Retained ownership must survive loss of a caller's closure after failed shutdown.
// Only explicit release or process exit may release the kernel lock.
const held = new Set<DatabaseSync>();

/**
 * A separate SQLite connection holds an EXCLUSIVE transaction for the process's
 * lifetime. The kernel releases its locks after a crash, so recovery needs no
 * timeout or PID guess. A diagnostic marker from the older directory-only
 * protocol is never automatically removed: its process did not hold this lock.
 * This is a local-filesystem, single-host contract, not a distributed lease.
 */
export async function claimRuntimeDirectory(directory: string): Promise<() => Promise<void>> {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const info = await lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || (info.mode & 0o077) !== 0) {
    throw new Error('Runtime ownership requires a private real directory');
  }
  const lock = path.join(directory, '.runtime-owner');
  const leasePath = path.join(directory, '.runtime-ownership.sqlite');
  const nonce = randomUUID();
  let lease: DatabaseSync | undefined;
  const staging = path.join(directory, `.runtime-owner.pending-${nonce}`);
  try {
    try {
      const existing = await lstat(leasePath);
      if (!existing.isFile() || existing.isSymbolicLink() || existing.nlink !== 1) throw new Error('Invalid ownership database');
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    lease = new DatabaseSync(leasePath);
    await chmod(leasePath, 0o600);
    lease.exec('PRAGMA busy_timeout=0; PRAGMA journal_mode=DELETE; BEGIN EXCLUSIVE;');
    const version = lease.prepare('PRAGMA user_version').get()!.user_version;
    if (version !== 0 && version !== 1) throw new Error('Unsupported ownership schema');
    lease.exec('PRAGMA user_version=1;');
    let prior: { protocol?: string } | undefined;
    try {
      const marker = await lstat(lock);
      if (!marker.isDirectory() || marker.isSymbolicLink()) throw new Error('Invalid ownership marker');
      const metadata = await lstat(path.join(lock, 'owner.json'));
      if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4096) throw new Error('Invalid ownership metadata');
      prior = JSON.parse(await readFile(path.join(lock, 'owner.json'), 'utf8')) as { protocol?: string };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      // A directory without metadata could belong to the previous protocol mid-startup.
      try { await lstat(lock); throw new Error('Unidentified runtime owner'); }
      catch (missing) { if ((missing as NodeJS.ErrnoException).code !== 'ENOENT') throw missing; }
    }
    if (prior && prior.protocol !== protocol) throw new Error('Legacy runtime ownership requires supervised recovery');
    await mkdir(staging, { mode: 0o700 });
    await writeFile(path.join(staging, 'owner.json'), JSON.stringify({ protocol, pid: process.pid, nonce }), { mode: 0o600, flag: 'wx' });
    if (prior) await rm(lock, { recursive: true });
    await rename(staging, lock);
    held.add(lease);
  } catch {
    lease?.close();
    await rm(staging, { recursive: true, force: true });
    throw new Error('Runtime directory is already owned or its ownership cannot be established');
  }
  const ownedLease = lease;
  let released = false;
  return async () => {
    if (released) return;
    const current = JSON.parse(await readFile(path.join(lock, 'owner.json'), 'utf8')) as { nonce?: string };
    if (current.nonce !== nonce) throw new Error('Runtime directory ownership changed');
    await rm(lock, { recursive: true });
    ownedLease.exec('ROLLBACK');
    ownedLease.close();
    held.delete(ownedLease);
    released = true;
  };
}

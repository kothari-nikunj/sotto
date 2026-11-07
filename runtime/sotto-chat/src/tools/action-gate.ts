import { createHash } from 'node:crypto';

export class ActionHeldError extends Error {
  constructor() { super('Action outcome is unknown; reconciliation is required before another attempt.'); }
}

function canonicalJson(value: unknown): string {
  const seen = new Set<object>();
  function stable(input: unknown): unknown {
    if (input === null || typeof input === 'string' || typeof input === 'boolean') return input;
    if (typeof input === 'number' && Number.isFinite(input)) return input;
    if (typeof input !== 'object') throw new Error('action arguments must be JSON values');
    if (seen.has(input)) throw new Error('action arguments are cyclic');
    seen.add(input);
    let normalized: unknown;
    if (Array.isArray(input)) normalized = input.map(stable);
    else {
      if (Object.getPrototypeOf(input) !== Object.prototype && Object.getPrototypeOf(input) !== null) {
        throw new Error('action arguments must be plain objects');
      }
      const object: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
      for (const key of Object.keys(input).sort()) object[key] = stable((input as Record<string, unknown>)[key]);
      normalized = object;
    }
    seen.delete(input);
    return normalized;
  }
  const encoded = JSON.stringify(stable(value));
  if (Buffer.byteLength(encoded) > 128 * 1024) throw new Error('action arguments exceed the allowed bound');
  return encoded;
}

const digest = (value: string) => createHash('sha256').update(value).digest('hex');

export function stableActionIdentity(ownerRequestKey: string, kind: string, args: unknown) {
  if (!ownerRequestKey || ownerRequestKey.length > 512 || !/^[A-Za-z0-9._:-]{1,128}$/.test(kind)) {
    throw new Error('action identity is invalid');
  }
  const canonicalArgs = canonicalJson(args);
  return {
    actionId: digest(JSON.stringify([ownerRequestKey, kind, canonicalArgs])),
    payloadHash: digest(canonicalArgs),
  };
}

/** Hash one immutable configured owner scope and the exact provider thread used for contact. */
import { createHash } from 'node:crypto';

export interface RouteIdentity {
  channel: 'photon' | 'telegram';
  tenantId: string;
  projectFingerprint: string;
  ownerId: string;
  accountId: string;
  lineKey: string;
}

const DOMAIN = Buffer.from('sotto-dispatch-route-v1\0', 'ascii');
function field(value: string): Buffer {
  if (typeof value !== 'string' || value.length > 512 ||
      /[\u0000-\u001f\u007f]/u.test(value) ||
      Buffer.from(value, 'utf8').toString('utf8') !== value) {
    throw new Error('invalid dispatch route identity');
  }
  const bytes = Buffer.from(value, 'utf8');
  if (bytes.length > 4096) throw new Error('invalid dispatch route identity');
  const size = Buffer.alloc(4);
  size.writeUInt32BE(bytes.length);
  return Buffer.concat([size, bytes]);
}

/** Domain + u32be field count + seven u32be length-prefixed UTF-8 fields. */
export function routeDigest(identity: RouteIdentity, providerThreadId: string): string {
  if (identity.channel !== 'photon' && identity.channel !== 'telegram') {
    throw new Error('invalid dispatch route identity');
  }
  const values = [identity.channel, identity.tenantId, identity.projectFingerprint,
    identity.ownerId, identity.accountId, identity.lineKey, providerThreadId];
  if (values.slice(3).some(value => !value)) throw new Error('invalid dispatch route identity');
  if (identity.projectFingerprint && !/^[a-f0-9]{64}$/u.test(identity.projectFingerprint)) {
    throw new Error('invalid dispatch route identity');
  }
  const count = Buffer.alloc(4);
  count.writeUInt32BE(values.length);
  return createHash('sha256').update(Buffer.concat([DOMAIN, count, ...values.map(field)])).digest('hex');
}

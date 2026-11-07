/** Static identity of the loaded channel config, not provider connection or activation state. */
import { createHmac } from 'node:crypto';
import { z } from 'zod';
import type { RuntimeConfig } from './bootstrap/config.js';

const scalar = (value: string): boolean => Buffer.from(value, 'utf8').toString('utf8') === value;
const text = (max: number) => z.string().min(1).max(max).refine(value => scalar(value) &&
  !/[\u0000-\u001f\u007f]/u.test(value));
const routeSchema = z.discriminatedUnion('mode', [
  z.object({ mode: z.literal('first_contact_shared_project') }).strict(),
  z.object({ mode: z.literal('fixed'), provider_thread_id: text(512) }).strict(),
  z.object({ mode: z.literal('first_contact'), expected_provider_line: text(512) }).strict(),
]);
const identitySchema = z.object({
  channel: z.enum(['telegram', 'photon']),
  tenant_id: z.string().max(512).refine(value => scalar(value) && !/[\u0000-\u001f\u007f]/u.test(value)),
  account_id: text(512), owner_id: text(512), line_key: text(512), target: text(512),
  route: routeSchema,
}).strict();
export const configuredChannelIdentitySchema = z.object({
  version: z.literal(1),
  identity: identitySchema,
  credential_proof: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
export type ConfiguredChannelIdentity = z.infer<typeof configuredChannelIdentitySchema>;

const DOMAIN = Buffer.from('sotto-configured-channel-identity-v1\0', 'ascii');

/** UTF-8 scalar fields, each prefixed by its uint32-BE length, after field count. */
export function channelIdentityTranscript(fields: readonly string[]): Buffer {
  if (fields.length > 32) throw new Error('Configured channel identity is invalid');
  const count = Buffer.alloc(4);
  count.writeUInt32BE(fields.length);
  const parts = [DOMAIN, count];
  for (const field of fields) {
    if (typeof field !== 'string' || !scalar(field) || /[\u0000-\u001f\u007f]/u.test(field)) {
      throw new Error('Configured channel identity is invalid');
    }
    const bytes = Buffer.from(field, 'utf8');
    if (bytes.length > 4096) throw new Error('Configured channel identity is invalid');
    const size = Buffer.alloc(4);
    size.writeUInt32BE(bytes.length);
    parts.push(size, bytes);
  }
  return Buffer.concat(parts);
}

/** A credential-bound proof from actual loaded config. This does not authenticate provider flow. */
export function configuredChannelIdentityForConfig(config: RuntimeConfig): ConfiguredChannelIdentity {
  const firstContact = config.channel === 'photon' ? config.firstContact : undefined;
  const route = firstContact
    ? firstContact.mode === 'owner_dm_shared_project' ? { mode: 'first_contact_shared_project' as const } : { mode: 'first_contact' as const, expected_provider_line: firstContact.expectedProviderLine }
    : { mode: 'fixed' as const, provider_thread_id: config.providerThreadId };
  const identity = identitySchema.parse({
    channel: config.channel, tenant_id: config.tenantId ?? '',
    account_id: config.owner.accountId, owner_id: config.owner.ownerId,
    line_key: config.lineKey, target: config.target, route,
  });
  if (firstContact && config.providerThreadId) throw new Error('Configured channel identity is invalid');
  const credentials = config.credentials;
  let credentialFields: string[];
  if (config.channel === 'telegram' && 'telegramToken' in credentials) {
    credentialFields = ['telegram', credentials.telegramToken,
      credentials.telegramLinkFile.bot_token, String(credentials.telegramLinkFile.allowed_user)];
  } else if (config.channel === 'photon' && 'mode' in credentials && credentials.mode === 'cloud') {
    credentialFields = ['cloud', credentials.photonProjectId, credentials.photonProjectSecret];
  } else if (config.channel === 'photon' && 'mode' in credentials && credentials.mode === 'self_host') {
    credentialFields = ['self_host', credentials.photonServerUrl, credentials.photonApiKey];
  } else throw new Error('Configured channel credential is invalid');
  const fields = [identity.channel, identity.tenant_id, identity.account_id,
    identity.owner_id, identity.line_key, identity.target, identity.route.mode,
    identity.route.mode === 'fixed' ? identity.route.provider_thread_id : identity.route.mode === 'first_contact_shared_project' ? 'shared_project' : identity.route.expected_provider_line,
    ...credentialFields];
  if (!scalar(config.bearerToken) || config.bearerToken.length < 32 ||
      /[\u0000-\u001f\u007f\s]/u.test(config.bearerToken)) {
    throw new Error('Configured channel identity is invalid');
  }
  const credential_proof = createHmac('sha256', Buffer.from(config.bearerToken, 'utf8'))
    .update(channelIdentityTranscript(fields)).digest('hex');
  return configuredChannelIdentitySchema.parse({ version: 1, identity, credential_proof });
}

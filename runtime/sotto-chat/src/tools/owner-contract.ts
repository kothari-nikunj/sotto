import { z } from 'zod';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const photonIdentitySchema = z.object({
  tenant_id: z.string().min(1).max(512), channel: z.literal('photon'), account_id: z.string().min(1).max(512),
  owner_id: z.string().min(1).max(512), line_key: z.string().min(1).max(512), target: z.literal('photon'),
  route_mode: z.enum(['fixed', 'first_contact', 'first_contact_shared_project']), route_id: z.string().min(1).max(512), project_fingerprint: hex,
}).strict();
const telegramIdentitySchema = z.object({
  tenant_id: z.string().min(1).max(512), channel: z.literal('telegram'), account_id: z.string().min(1).max(512),
  owner_id: z.string().regex(/^[1-9][0-9]{0,15}$/), line_key: z.string().min(1).max(512),
  target: z.literal('telegram'), route_mode: z.literal('fixed'), route_id: z.string().min(1).max(512),
  bot_fingerprint: hex,
}).strict().refine(value => Number(value.owner_id) <= Number.MAX_SAFE_INTEGER &&
  value.route_id === `telegram:${value.owner_id}`);
export const ownerToolIdentitySchema = z.discriminatedUnion('channel', [photonIdentitySchema, telegramIdentitySchema]);

export type OwnerToolIdentity = z.infer<typeof ownerToolIdentitySchema>;
export interface OwnerToolClientOptions {
  bridgeUrl: string;
  bearer: string;
  identity: OwnerToolIdentity;
  fetch?: typeof fetch;
}

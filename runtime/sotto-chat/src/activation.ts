/** Durable owner-message evidence for the existing receiver activation writer. */
import { createHash } from 'node:crypto';
import { z } from 'zod';

const bounded = z.string().min(1).max(512).regex(/^[^\u0000-\u001f\u007f]+$/u);
export const activationIdentitySchema = z.object({
  tenant_id: z.string().max(512).regex(/^[^\u0000-\u001f\u007f]*$/u),
  project_fingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  owner_id: bounded,
  account_id: bounded,
  line_key: bounded,
  provider_thread_id: bounded,
}).strict();
export type ActivationIdentity = z.infer<typeof activationIdentitySchema>;
/** Immutable discovery authority. The provider thread is supplied only by a verified owner DM. */
const discoveryBase = activationIdentitySchema.omit({ provider_thread_id: true });
export const activationDiscoverySeedSchema = z.union([
  discoveryBase.extend({ expected_provider_line: z.string().regex(/^\+[1-9]\d{7,14}$/) }).strict(),
  discoveryBase.extend({ provider_route_mode: z.literal('shared_project') }).strict(),
]);
export type ActivationDiscoverySeed = z.infer<typeof activationDiscoverySeedSchema>;
/** Defensive journal shape check; gateway admission additionally uses the public Photon codec. */
export function capturedPhotonThreadMatchesLine(thread: string, line: string): boolean {
  if (!thread.startsWith('imessage:')) return false;
  const parts = thread.slice('imessage:'.length).split('~');
  return parts.length === 2 && parts[0]!.includes(';-;') && parts[1] === line;
}
export function discoveryRoute(seed: ActivationDiscoverySeed) {
  return 'expected_provider_line' in seed ? seed.expected_provider_line : 'shared';
}
export function discoveryIdentity(seed: ActivationDiscoverySeed) {
  if ('expected_provider_line' in seed) {
    const { expected_provider_line: _line, ...base } = seed;
    return base;
  }
  const { provider_route_mode: _mode, ...base } = seed;
  return base;
}
export const activationEvidenceSchema = activationIdentitySchema.extend({
  version: z.literal(1),
  event_id: z.string().regex(/^[a-f0-9]{64}$/),
  provider_event_id: bounded,
  admitted_at: z.number().int().nonnegative().safe(),
}).strict();
export type ActivationEvidence = z.infer<typeof activationEvidenceSchema>;

/** Matches the sole Python receipt writer; the project credential is never included. */
export function photonProjectFingerprint(projectId: string): string {
  if (!projectId.trim()) throw new Error('Photon project identity is missing');
  return createHash('sha256').update(`sotto-photon-project-v1:${projectId}`).digest('hex');
}

/** Private read of configured channel identity and durable owner-DM admission. */
import { z } from 'zod';
import { activationIdentitySchema } from './activation.js';
import { configuredChannelIdentitySchema } from './configured-identity.js';

const mode = z.enum(['fixed', 'first_contact', 'first_contact_shared_project']);
const eventId = z.string().regex(/^[a-f0-9]{64}$/);
const timestamp = z.number().int().nonnegative().safe();
export const activationAdmissionSchema = z.discriminatedUnion('status', [
  z.object({ status: z.literal('absent'), mode }).strict(),
  z.object({ status: z.literal('pending'), mode, binding: activationIdentitySchema,
    event_id: eventId, admitted_at: timestamp }).strict(),
  z.object({ status: z.literal('acknowledged'), mode, binding: activationIdentitySchema,
    event_id: eventId, admitted_at: timestamp, acknowledged_at: timestamp }).strict(),
]);
export type ActivationAdmission = z.infer<typeof activationAdmissionSchema>;

export const activationStateSchema = z.object({ version: z.literal(1),
  configured: configuredChannelIdentitySchema, activation: activationAdmissionSchema }).strict();
export type ActivationState = z.infer<typeof activationStateSchema>;

import { constants } from 'node:fs';
import { createHash } from 'node:crypto';
import { lstat, mkdir, open } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { z } from 'zod';
import { resolvePhotonCredentials, resolveTelegramCredentials } from '../channels/credentials.js';
import { photonProjectFingerprint } from '../activation.js';

const identifier = z.string().trim().min(1).max(512);
const proxy = z.object({
  origin: z.string().url(),
  bearer: z.string().min(1).max(4096).refine(value => !/[\r\n]/.test(value)),
  chatModelId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
  nativeMediaModelId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
}).strict();
const direct = z.object({
  provider: z.literal('google'),
  apiKey: z.string().min(1).max(4096).refine(value => !/[^\x21-\x7e]/u.test(value)),
  chatModelId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
  nativeMediaModelId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/),
}).strict();
const bridge = z.object({
  url: z.string().url(),
  readBearer: z.string().min(1).max(4096).refine(value => !/[\r\n]/.test(value)),
}).strict().optional();
const photonOwnerLoopIdentity = z.object({
  tenant_id: identifier, channel: z.literal('photon'), account_id: identifier,
  owner_id: identifier, line_key: identifier, target: z.literal('photon'),
  route_mode: z.enum(['fixed', 'first_contact', 'first_contact_shared_project']), route_id: identifier,
  project_fingerprint: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
const telegramOwnerLoopIdentity = z.object({
  tenant_id: identifier, channel: z.literal('telegram'), account_id: identifier,
  owner_id: identifier, line_key: identifier, target: z.literal('telegram'),
  route_mode: z.literal('fixed'), route_id: identifier,
  bot_fingerprint: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
const ownerLoopIdentity = z.discriminatedUnion('channel', [photonOwnerLoopIdentity, telegramOwnerLoopIdentity]);
const ownerLoop = z.object({ mode: z.literal('explicit_command'), identity: ownerLoopIdentity,
  bearer: z.string().regex(/^[a-f0-9]{64}$/) }).strict().optional();
const common = z.object({
  version: z.literal(1),
  stateDirectory: z.string().min(1).max(4096),
  mediaRoot: z.string().min(1).max(4096),
  bearerToken: z.string().min(32).max(4096).refine(value => !/\s/.test(value)),
  port: z.number().int().min(0).max(65535).default(0),
  tenantId: z.string().min(1).max(512).regex(/^[^\u0000-\u001f\u007f]+$/u).optional(),
  owner: z.object({ accountId: identifier, ownerId: identifier }).strict(),
  lineKey: identifier,
  target: identifier,
  providerThreadId: identifier,
  instructions: z.string().trim().min(1).max(32_000),
  modelProxy: proxy.optional(),
  modelDirect: direct.optional(),
  deployment: z.enum(['managed', 'self_host']).optional(),
  bridge,
  ownerLoop,
  skillPackRoot: z.string().min(1).max(4096).optional(),
  allowedSkills: z.array(z.string().regex(/^[a-z][a-z0-9-]{0,63}$/)).max(128).default([]),
  turnPresence: z.boolean().default(false),
});

const telegram = common.extend({
  channel: z.literal('telegram'),
  credentials: z.object({
    telegramToken: z.string().min(1).max(4096),
    telegramLinkFile: z.object({ bot_token: z.string().min(1).max(4096), allowed_user: z.union([z.string(), z.number().int()]) }).strict(),
  }).strict(),
}).strict();
const photon = common.extend({
  channel: z.literal('photon'),
  providerThreadId: identifier.optional(),
  firstContact: z.discriminatedUnion('mode', [
    z.object({ mode: z.literal('owner_dm'), expectedProviderLine: z.string().regex(/^\+[1-9]\d{7,14}$/) }).strict(),
    z.object({ mode: z.literal('owner_dm_shared_project') }).strict(),
  ]).optional(),
  credentials: z.discriminatedUnion('mode', [
    z.object({ mode: z.literal('cloud'), photonProjectId: z.string().min(1).max(4096), photonProjectSecret: z.string().min(1).max(4096) }).strict(),
    z.object({ mode: z.literal('self_host'), photonServerUrl: z.string().url(), photonApiKey: z.string().min(1).max(4096) }).strict(),
  ]),
}).strict();

const configSchema = z.discriminatedUnion('channel', [telegram, photon]).superRefine((config, context) => {
  if (Boolean(config.modelProxy) === Boolean(config.modelDirect)) {
    context.addIssue({ code: 'custom', message: 'Exactly one explicit model configuration is required' });
  }
  if (config.modelDirect && config.deployment !== 'self_host') {
    context.addIssue({ code: 'custom', message: 'Direct model credentials require self-host deployment' });
  }
  if (config.channel !== 'photon') {
    if (config.ownerLoop) {
      const identity = config.ownerLoop.identity;
      const credentials = config.credentials;
      const fingerprint = createHash('sha256').update(`sotto-telegram-bot-v1:${credentials.telegramToken}`).digest('hex');
      if (config.deployment !== 'self_host' || !config.modelDirect || !config.tenantId || !config.bridge ||
          config.target !== 'telegram' || config.providerThreadId !== `telegram:${config.owner.ownerId}` ||
          identity.channel !== 'telegram' || identity.tenant_id !== config.tenantId ||
          identity.account_id !== config.owner.accountId || identity.owner_id !== config.owner.ownerId ||
          identity.line_key !== config.lineKey || identity.route_mode !== 'fixed' ||
          identity.route_id !== `telegram:${config.owner.ownerId}` ||
          identity.bot_fingerprint !== fingerprint ||
          credentials.telegramLinkFile.bot_token !== credentials.telegramToken ||
          String(credentials.telegramLinkFile.allowed_user) !== config.owner.ownerId) {
        context.addIssue({ code: 'custom', message: 'Telegram owner command identity differs' });
      }
    }
    return;
  }
  if (config.firstContact) {
    if (config.credentials.mode !== 'cloud' || config.providerThreadId !== undefined) {
      context.addIssue({ code: 'custom', message: 'First-contact binding requires Cloud and no fixed thread' });
    }
  } else if (!config.providerThreadId) {
    context.addIssue({ code: 'custom', message: 'Photon requires a fixed thread or explicit first-contact binding' });
  }
  if (config.ownerLoop) {
    const identity = config.ownerLoop.identity;
    const routeMode = config.firstContact?.mode === 'owner_dm_shared_project' ? 'first_contact_shared_project' : config.firstContact ? 'first_contact' : 'fixed';
    const routeId = config.firstContact?.mode === 'owner_dm_shared_project' ? 'shared_project' : config.firstContact?.expectedProviderLine ?? config.providerThreadId;
    const allowedModel = Boolean(config.modelProxy && config.deployment !== 'self_host') ||
      Boolean(config.modelDirect && config.deployment === 'self_host');
    if (!allowedModel || !config.tenantId || !config.bridge || config.credentials.mode !== 'cloud' ||
        identity.channel !== 'photon' ||
        identity.tenant_id !== config.tenantId || identity.account_id !== config.owner.accountId ||
        identity.owner_id !== config.owner.ownerId || identity.line_key !== config.lineKey ||
        identity.target !== config.target || identity.route_mode !== routeMode ||
        identity.route_id !== routeId ||
        identity.project_fingerprint !== photonProjectFingerprint(config.credentials.photonProjectId)) {
      context.addIssue({ code: 'custom', message: 'Owner loop capability does not match configured identity' });
    }
  }
});
export type RuntimeConfig = z.infer<typeof configSchema> & {
  stateDirectory: string;
  mediaRoot: string;
  configPath: string;
};

function privateConfigMetadata(stats: Awaited<ReturnType<typeof lstat>>, uid: number | undefined): void {
  if (!stats.isFile() || stats.isSymbolicLink()) throw new Error('Config must be a regular non-symlink file');
  if ((Number(stats.mode) & 0o077) !== 0) throw new Error('Config file must not be accessible by group or other users');
  if (uid !== undefined && stats.uid !== uid) throw new Error('Config file must be owned by the current user');
  if (stats.size > 64 * 1024) throw new Error('Config file exceeds the 64 KiB limit');
}

/** Read a private explicit file; this loader never consults environment variables or Keychain. */
export async function loadRuntimeConfig(configFile: string): Promise<RuntimeConfig> {
  const absolute = resolve(configFile);
  let handle;
  let parsed: unknown;
  try {
    handle = await open(absolute, constants.O_RDONLY | constants.O_NONBLOCK | (constants.O_NOFOLLOW ?? 0));
    privateConfigMetadata(await handle.stat(), process.getuid?.());
    const chunks: Buffer[] = [];
    let size = 0;
    while (true) {
      const chunk = Buffer.alloc(8192);
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (!bytesRead) break;
      size += bytesRead;
      if (size > 64 * 1024) throw new Error('Config file exceeds the 64 KiB limit');
      chunks.push(chunk.subarray(0, bytesRead));
    }
    parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (error) {
    if (error instanceof SyntaxError) throw new Error('Runtime config is not valid JSON');
    if (error instanceof Error && /Config file|Config must/.test(error.message)) throw error;
    throw new Error('Runtime config file is unavailable or invalid');
  } finally { await handle?.close(); }
  const result = configSchema.safeParse(parsed);
  if (!result.success) throw new Error('Runtime config shape or field bounds are invalid');
  const config = result.data;
  if (config.bridge) {
    const bridgeUrl = new URL(config.bridge.url);
    if (bridgeUrl.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(bridgeUrl.hostname) ||
        bridgeUrl.pathname !== '/mcp' || bridgeUrl.search || bridgeUrl.hash || bridgeUrl.username || bridgeUrl.password) {
      throw new Error('Bridge must use the exact loopback /mcp endpoint');
    }
  }
  if (config.modelProxy) {
    const proxyUrl = new URL(config.modelProxy.origin);
    if (proxyUrl.origin !== config.modelProxy.origin.replace(/\/$/, '') || proxyUrl.pathname !== '/' || proxyUrl.search || proxyUrl.hash || proxyUrl.username || proxyUrl.password ||
      (proxyUrl.protocol !== 'https:' && !(proxyUrl.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(proxyUrl.hostname)))) {
      throw new Error('Model proxy must be one explicit HTTPS origin (or loopback HTTP origin)');
    }
  }
  const credentialSource = config.credentials;
  if (config.channel === 'telegram') {
    const credentials = resolveTelegramCredentials(credentialSource);
    if (credentials.ownerId !== config.owner.ownerId) throw new Error('Configured Telegram owner does not match pairing record');
  } else resolvePhotonCredentials(credentialSource);
  return {
    ...config,
    stateDirectory: resolve(dirname(absolute), config.stateDirectory),
    mediaRoot: resolve(dirname(absolute), config.mediaRoot),
    configPath: absolute,
  } as RuntimeConfig;
}

/** Create/verify a private state directory before opening SQLite or Mastra stores. */
export async function ensurePrivateStateDirectory(directory: string): Promise<void> {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  let stats;
  try { stats = await lstat(directory); }
  catch { throw new Error('Runtime state directory is unavailable'); }
  if (!stats.isDirectory() || stats.isSymbolicLink()) throw new Error('Runtime state path must be a real directory');
  if ((Number(stats.mode) & 0o077) !== 0) throw new Error('Runtime state directory must not be accessible by group or other users');
  const uid = process.getuid?.();
  if (uid !== undefined && stats.uid !== uid) throw new Error('Runtime state directory must be owned by the current user');
}

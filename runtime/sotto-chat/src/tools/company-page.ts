import {z} from 'zod';

const companyIdentitySchema=z.object({slug:z.string().min(1).max(400),
 name:z.string().max(400).refine(v=>Array.from(v).length<=200),
 domain:z.string().max(253).regex(/^(?:|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$/),
 last_researched:z.string().regex(/^(?:|\d{4}-\d{2}-\d{2})$/)}).strict();
const companyCursor = /^[a-f0-9]{64}:[0-9]{1,9}$/;
const savedCompanyPage = z.object({
  status: z.literal('saved'),
  company_knowledge: z.record(z.string().min(1), z.string().min(1).max(6000).refine(v => Array.from(v).length <= 3000)),
  company_identity: companyIdentitySchema,
  coverage: z.literal('Saved company record only; original sources were not searched.'),
  truncated: z.boolean(),
  next_cursor: z.string().regex(companyCursor).nullable(),
}).strict().refine(page => Object.keys(page.company_knowledge).length === 1 &&
  Object.hasOwn(page.company_knowledge, page.company_identity.slug) && page.truncated === (page.next_cursor !== null));
const emptyCompanyResult = z.object({
  company_knowledge: z.object({}).strict(), truncated: z.literal(false),
}).strict();
export const companyResultSchema = z.union([
  savedCompanyPage,
  emptyCompanyResult.extend({status: z.enum(['missing', 'ambiguous', 'unavailable'])}),
  emptyCompanyResult.extend({status: z.literal('changed'), guidance: z.literal('Saved company changed or cursor is invalid; restart with cursor=first.')}),
]);

export function companyPageArgs(argv: readonly string[]): string[] {
  const [flag, name, pageFlag, cursor] = argv;
  if (flag !== '--company' || !name || name !== name.trim() || Array.from(name).length > 200 ||
      /[\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029]/u.test(name)) throw new Error('Invalid company selector');
  if (argv.length === 2) return [flag, name, '--company-page', 'first'];
  if (argv.length !== 4 || pageFlag !== '--company-page' || !cursor ||
      (cursor !== 'first' && !companyCursor.test(cursor))) throw new Error('Invalid company page');
  return [...argv];
}

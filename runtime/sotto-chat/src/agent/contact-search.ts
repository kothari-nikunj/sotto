import { z } from 'zod';

export const contactSearchInput = z.object({
  query: z.string().trim().min(2).max(160),
  limit: z.number().int().min(1).max(20).default(10),
}).strict();

const contact = z.object({name: z.string(), phones: z.array(z.string()), emails: z.array(z.string())});

/** Project the already consent-checked Bridge result before it enters model context.
 * Matches are candidates, never an identity merge or permission to contact anyone.
 */
export function searchContactResult(result: unknown, query: string, limit: number): unknown {
  const envelope = z.object({content: z.tuple([z.object({type: z.literal('text'), text: z.string()})])}).parse(result);
  const raw = JSON.parse(envelope.content[0].text);
  if (!raw || typeof raw !== 'object' || Object.hasOwn(raw, 'error') ||
      (Object.hasOwn(raw, 'status') && raw.status !== 'ok')) {
    throw new Error('Contacts read is unavailable');
  }
  const payload = z.object({contacts: z.array(contact)}).parse(raw);
  const normalized = query.normalize('NFC').toLowerCase();
  const words = normalized.split(/\s+/u);
  const phone = /^[+\d\s().-]+$/.test(query) ? query.replace(/\D/g, '') : '';
  const matches = payload.contacts.filter(row => {
    const names = row.name.normalize('NFC').toLowerCase();
    return words.every(word => names.includes(word)) ||
      row.emails.some(email => email.normalize('NFC').toLowerCase().includes(normalized)) ||
      (phone.length >= 3 && row.phones.some(value => value.replace(/\D/g, '').includes(phone)));
  });
  const selected: Array<z.infer<typeof contact>> = [];
  let bytes = 0;
  for (const row of matches) {
    // Never truncate an identifier into a different usable address/phone. Oversized
    // candidates are omitted and reported; the user can narrow the search.
    if (row.name.length > 512 || row.phones.length > 20 || row.emails.length > 20 ||
        [...row.phones, ...row.emails].some(value => value.length > 512)) continue;
    const size = Buffer.byteLength(JSON.stringify(row));
    if (selected.length >= limit || bytes + size > 16_384) continue;
    bytes += size;
    selected.push(row);
  }
  return {content: [{type: 'text', text: JSON.stringify({contacts: selected,
    matched: matches.length, returned: selected.length, truncated: selected.length < matches.length,
    scope: 'Contact names, phones and emails only; notes are not searched or returned.'})}]};
}

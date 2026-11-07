/** Exact quiet receipt timestamp; the empty value records /quiet off. */
export function isQuietUntil(value: unknown): value is string {
  if (value === '') return true;
  if (typeof value !== 'string') return false;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.\d{3}([+-])(\d{2}):(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days! &&
    Number(match[4]) <= 23 && Number(match[5]) <= 59 && Number(match[6]) <= 59 &&
    Number(match[8]) <= 23 && Number(match[9]) <= 59 && Number.isFinite(Date.parse(value));
}

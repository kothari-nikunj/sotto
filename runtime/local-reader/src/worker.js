import { Readability, isProbablyReaderable } from '@mozilla/readability';
import { parseHTML } from 'linkedom';
import { LIMITS, failure } from './contracts.js';

// Linkedom parses a DOM without a browser loader or script execution. Do not
// replace it with an executable browser DOM or add resource-fetching hooks.
function extract({ html, sourceUrl }) {
  const { document } = parseHTML(html);
  if (document.querySelectorAll('*').length > LIMITS.domElements) {
    return failure('DOM_TOO_LARGE', 'Document exceeds the 20,000 element limit.');
  }
  for (const element of document.querySelectorAll('script,style,template,noscript')) element.remove();
  if (!isProbablyReaderable(document)) return failure('NO_ARTICLE', 'No readable article was detected.');
  const article = new Readability(document, {
    maxElemsToParse: LIMITS.domElements,
    serializer: element => element,
  }).parse();
  // innerText preserves block boundaries in minified HTML; textContent would
  // concatenate adjacent paragraphs into a single word at their boundary.
  const fullText = article?.content?.innerText?.replace(/\s+/g, ' ').trim();
  if (!fullText) return failure('NO_ARTICLE', 'No readable article was detected.');
  // Avoid splitting Unicode surrogate pairs at the output boundary.
  const characters = Array.from(fullText);
  const text = characters.slice(0, LIMITS.textChars).join('');
  const titleCharacters = Array.from((article.title || '').replace(/\s+/g, ' ').trim());
  return {
    ok: true,
    article: { sourceUrl, title: titleCharacters.slice(0, LIMITS.titleChars).join(''), text },
    coverage: {
      kind: 'extracted_article_text',
      truncated: characters.length > LIMITS.textChars,
      originalTextChars: characters.length,
      returnedTextChars: Math.min(characters.length, LIMITS.textChars),
      titleTruncated: titleCharacters.length > LIMITS.titleChars,
    },
  };
}

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try {
  process.stdout.write(JSON.stringify(extract(JSON.parse(Buffer.concat(chunks).toString('utf8')))));
} catch {
  process.stdout.write(JSON.stringify(failure('EXTRACTION_FAILED', 'The supplied document could not be extracted.')));
}

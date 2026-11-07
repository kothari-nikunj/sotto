/** Read authority is separate from conversation recall. Shared policy data also loads in Python. */
import policy from './read-policy.json' with {type: 'json'};
export const READ_CONTEXT_WINDOW_MS = policy.contextWindowMs;
type Rule = Exclude<keyof typeof policy, 'contextWindowMs'>;
// JavaScript's \b remains ASCII even with /u. Match Python's Unicode word
// boundaries so a refusal word cannot be a prefix of a person's name.
const word = '[\\p{L}\\p{N}_]';
const boundary = `(?:(?<!${word})(?=${word})|(?<=${word})(?!${word}))`;
const expression = (key: Rule, flags = 'iu') => new RegExp(
  policy[key].replaceAll('\\b', boundary).replaceAll('[\\w]', word), flags);
type MatchRule = Exclude<Rule, 'courtesy' | 'targetAnswer'>;
const expressions = Object.fromEntries((Object.keys(policy) as (keyof typeof policy)[])
  .filter((key): key is MatchRule => key !== 'contextWindowMs' && key !== 'courtesy' && key !== 'targetAnswer')
  .map(key => [key, expression(key)])) as Record<MatchRule, RegExp>;
const courtesy = expression('courtesy', 'giu');
const targetAnswer = new RegExp(policy.targetAnswer, 'i');
const matches = (key: MatchRule, text: string) => expressions[key].test(text.normalize('NFC'));
export const readCancelled = (text: string) => matches('denied',
  text.normalize('NFC').replace(courtesy, ''));
export const ownerReadRequest=(text:string)=>!readCancelled(text)&&!matches('reported',text)&&!matches('personalRecall',text)&&matches('request',text);
export const contextualReadRequest=(text:string)=>ownerReadRequest(text);
export const explicitReadReference=(text:string)=>matches('reference',text);
export function subjectMention(text:string,subject:string): boolean {
  const escaped=subject.normalize('NFC').replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  return new RegExp(`(?<![\\p{L}\\p{N}_@./-])${escaped}(?![\\p{L}\\p{N}_@/-]|\\.[\\p{L}\\p{N}_])`,'iu').test(text.normalize('NFC'));
}
export function validResearchSubject(subject:string,mode:'company'|'person'):boolean {
  if(!subject || subject.length>200 || subject.trim()!==subject || matches('subjectSentence',subject)||matches('streetAddress',subject))return false;
  return mode==='person' ? /^[\p{L}\p{M}][\p{L}\p{M} .’'-]*$/u.test(subject)&&subject.split(/\s+/).length<=6 :
    /^[\p{L}\p{N}][\p{L}\p{M}\p{N} .&’'-]*$/u.test(subject)&&subject.split(/\s+/).length<=8;
}
export const ownerSubjectMention=(text:string,subject:string)=>!readCancelled(text)&&!matches('reported',text)&&subjectMention(text,subject);

/** The owner supplies the missing public identity, not new authority from assistant prose. */
export function researchTargetAnswer(text:string,subject:string,mode:'company'|'person'):boolean {
 const match=targetAnswer.exec(text.trim());
 return mode==='company' && !!match && match[1]!.toLowerCase()===subject.toLowerCase();
}

export const prepResearchRequest=(text:string)=>contextualReadRequest(text)&&explicitReadReference(text)&&!matches('prepSavedOnly',text);

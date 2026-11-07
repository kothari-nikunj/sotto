export const LIMITS = Object.freeze({
  inputBytes: 2 * 1024 * 1024,
  htmlBytes: 1024 * 1024,
  sourceUrlChars: 4096,
  textChars: 16000,
  titleChars: 512,
  domElements: 20000,
  timeoutMs: 10000,
  workerOutputBytes: 256 * 1024,
});

export function failure(code, message) {
  return { ok: false, error: { code, message } };
}

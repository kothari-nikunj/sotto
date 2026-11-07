import {execFile} from 'node:child_process';
import path from 'node:path';
import {accessSync, constants} from 'node:fs';

const MAX_TEXT_BYTES = 128 * 1024;
const FORMAT = `import sys
sys.path.insert(0, sys.argv[1])
from chatfmt import compact_handled, to_imessage
sys.stdout.write(compact_handled(to_imessage(sys.stdin.read())))
`;

/**
 * Reuse the canonical pure formatter, without a shell, credentials or model-selected code.
 * The child runs asynchronously so an outbound send never blocks the event loop (intake,
 * presence and other threads keep running); its 5s timeout and output bound are unchanged,
 * and any spawn, exit, timeout or bound failure still rejects before provider contact.
 */
export function createPhotonTextFormatter(sharedLibrary: string): (text: string) => Promise<string> {
  if (!path.isAbsolute(sharedLibrary) || sharedLibrary.includes('\0')) throw new Error('Photon formatter path is invalid');
  // Docker installs Python in /usr/local; distro-based Linux hosts use /usr/bin.
  // Select once from fixed system paths; never search PATH or retry a failed formatter.
  const candidates = process.platform === 'darwin' ? ['/usr/bin/python3'] : ['/usr/local/bin/python3', '/usr/bin/python3'];
  const python = candidates.find(candidate => {
    try { accessSync(candidate, constants.X_OK); return true; } catch { return false; }
  }) ?? candidates[0]!;
  return text => new Promise<string>((resolve, reject) => {
    if (Buffer.byteLength(text) > MAX_TEXT_BYTES) { reject(new Error('Photon formatter input exceeds bound')); return; }
    const child = execFile(python, ['-I', '-B', '-c', FORMAT, sharedLibrary], {
      encoding: 'utf8', env: {}, timeout: 5000, maxBuffer: 512 * 1024,
    }, (error, stdout) => { if (error) reject(error); else resolve(stdout); });
    // A child that exits before reading stdin surfaces through the exit status, not EPIPE.
    child.stdin?.on('error', () => undefined);
    child.stdin?.end(text, 'utf8');
  });
}

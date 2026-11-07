import { spawn } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const [readyPath, descendantScript, lockFdText] = process.argv.slice(2);
const lockFd = Number(lockFdText);
if (!readyPath || !descendantScript || !Number.isSafeInteger(lockFd) || lockFd < 3) process.exit(90);
writeFileSync(readyPath, JSON.stringify({ pid: process.pid, uid: process.getuid() }));
const child = spawn('/usr/local/bin/python3', [descendantScript, readyPath], {
  stdio: ['ignore', 'ignore', 'ignore', lockFd],
});
child.once('error', () => process.exit(91));

#!/usr/bin/env python3
"""Drive `hermes whatsapp` non-interactively (for headless/cloud) under a PTY.

`hermes whatsapp` (a) refuses to run without an interactive terminal and (b) is a multi-step wizard:
it asks "How will you use WhatsApp?" [1/2], may ask "Update allowed users? [y/N]" and similar y/N
questions, then renders a QR to link the device. Here we:
  - give it a pseudo-TTY (pty.fork) so it believes it's interactive,
  - auto-answer prompts on quiescence: the mode prompt with SOTTO_WHATSAPP_MODE (default "2" =
    personal number / self-chat, needs no second number) and any y/N prompt with its DEFAULT
    (the capitalised letter — so `[y/N]`→n, `[Y/n]`→y; we already set allowed users / home channel
    via env, so keeping defaults is correct), and
  - relay ALL child output (including the QR) to stdout so it shows in the deploy logs, AND mirror a
    rolling, ANSI-stripped copy to $SOTTO_DATA/whatsapp-pairing.txt so the receiver can serve a clean,
    scannable QR over the public URL (Railway's log viewer pads lines and distorts the terminal QR).
Exits when the child finishes (creds.json written) or after SOTTO_WHATSAPP_PAIR_TIMEOUT seconds; the
mirror file is removed on exit.

Set SOTTO_WHATSAPP_MODE=1 (separate bot number — needs a second WhatsApp number) to override the mode.
"""
from __future__ import annotations

import os
import pty
import re
import select
import sys
import time
from collections import deque

MODE = os.environ.get("SOTTO_WHATSAPP_MODE", "2").strip()
TIMEOUT = int(os.environ.get("SOTTO_WHATSAPP_PAIR_TIMEOUT", "900"))  # 15 min to scan
QUIESCE = 1.5  # seconds of no output before we treat the tail as a waiting prompt
QR_FILE = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "whatsapp-pairing.txt")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # strip color/cursor escapes for the web mirror


def decide(tail: str) -> str | None:
    """Return the keystroke(s) to answer a detected prompt, or None to wait."""
    if "[1/2]" in tail:
        return MODE
    if "[y/N]" in tail:   # default No  → keep the env-provided allowed users / settings
        return "n"
    if "[Y/n]" in tail:   # default Yes
        return "y"
    return None


def main() -> int:
    pid, fd = pty.fork()
    if pid == 0:  # child becomes `hermes whatsapp` with the PTY as its controlling terminal
        os.execvp("hermes", ["hermes", "whatsapp"])
        os._exit(127)

    start = time.time()
    tail = ""  # recent output since the last answer (bounded)
    lines: deque[str] = deque(maxlen=80)  # rolling window mirrored to the web QR file
    pending = ""  # partial line accumulator for the mirror

    def flush_mirror():
        try:
            with open(QR_FILE, "w") as f:
                f.write("\n".join(lines))
        except OSError:
            pass

    while True:
        if time.time() - start > TIMEOUT:
            break
        try:
            rlist, _, _ = select.select([fd], [], [], QUIESCE)
        except (OSError, ValueError):
            break
        if fd in rlist:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break  # PTY closed → child exited
            if not data:
                break
            os.write(sys.stdout.fileno(), data)  # stream to deploy logs (QR included)
            text = data.decode(errors="replace")
            tail = (tail + text)[-2000:]
            # Mirror an ANSI-stripped, line-split copy for the web QR view.
            pending += ANSI.sub("", text)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                lines.append(line.rstrip("\r"))
            flush_mirror()
        else:
            # Quiescent: if the tail ends in a known prompt, answer it; otherwise keep waiting
            # (e.g. the QR is displayed and waiting to be scanned — nothing to answer).
            ans = decide(tail)
            if ans is not None:
                os.write(fd, (ans + "\n").encode())
                tail = ""  # consume this prompt so we don't re-answer it
        # Reap the child if it exited.
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                break
        except OSError:
            break

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(QR_FILE)  # pairing done/aborted — stop serving the QR
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

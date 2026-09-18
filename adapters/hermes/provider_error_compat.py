"""Keep pinned Hermes provider failures useful and quiet on chat surfaces.

This is deliberately a source-pin compatibility seam.  It patches the shared
gateway boundary used by every chat adapter and refuses to touch any unreviewed
Hermes source.

The hashes below are whole-file SHA256 digests of two files inside the Hermes checkout
pinned by ``adapters/hermes/hermes.commit`` — ``gateway/run.py`` and its sibling
``gateway/run_turn_runner.py``.  Bumping that commit invalidates both.  See the
"Provider-error gateway pin" section of ``adapters/hermes/README.md`` for the
regeneration procedure.

Two entry points, both hash-gated:

``python3 provider_error_compat.py <hermes>/gateway/run.py``
    Apply the adaptation.  Container boot (``start.sh``) and the local installer
    (``install.sh``) both run this.
``python3 provider_error_compat.py --check <hermes>/gateway/run.py``
    Verify both pinned hashes and every patch anchor without writing anything.
    The image build runs this right after Hermes is installed, so a ``hermes.commit``
    bump fails the build instead of crash-looping every container at boot.

Why the lifecycle-status path is left alone: Hermes' own status boundary already turns a
provider failure into one plain-language sentence, and an earlier revision of this seam
silenced it on the assumption that the turn-final path always presents the failure.  It
does not: on a natively streaming surface (Photon/iMessage) a FAILED turn produces no
stream payload at all, so silencing the status message can make a provider failure
completely invisible.  A duplicate sentence beats silence.  To bound that duplicate,
``_looks_like_gateway_provider_error`` treats an already-sanitized reply as ordinary
text, which makes the final-response sanitizer idempotent — if both paths fire the user
sees the same sentence twice at worst, never a second, different-sounding failure.
"""
import hashlib
import os
from pathlib import Path
import sys
import tempfile


PINNED_SOURCE_SHA256 = '79a8704da45baf702e1e9590a1b55feac9439722685705a4eb1b3f545dc134d3'
PINNED_TURN_RUNNER_SHA256 = 'c2473f80b43a6af54ce6da3d26bc811d0224991beb4853e7dc14ff6ad73be180'


OLD_REPLIES = '''_PROVIDER_ERROR_REPLIES = (
    (_GATEWAY_AUTH_ERROR_RE, "⚠️ Provider authentication failed. Check the configured credentials; "
                             "raw provider details are in the gateway logs."),
    (_GATEWAY_PROVIDER_POLICY_RE, "⚠️ The model provider rejected the request. I kept the raw provider "
                                  "error out of chat; check gateway logs for details or try rephrasing."),
    (_GATEWAY_RATE_LIMIT_RE, "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."),
    (_GATEWAY_CONNECTION_ERROR_RE, "⚠️ The model server is not responding — it looks like the configured "
                                   "model endpoint is not running or is unreachable."))
'''

# `billing details` / `exceeded your current quota` / `RESOURCE_EXHAUSTED` are Gemini's
# quota-exhaustion wording, `credit balance is too low` is Anthropic's, `insufficient_quota`
# is OpenAI's. All of them arrive as HTTP 429, so without these the owner-must-act failures
# are indistinguishable from transient load and the user is told to wait for a stop that
# will never clear on its own.
NEW_REPLIES = '''_SOTTO_ALLOWANCE_ERROR_RE = re.compile(
    r"(sotto_budget_exhausted|insufficient_quota|budget(?:\\s+|_)(?:is\\s+)?exhausted"
    r"|tenant request budget exhausted|spending\\s+(?:cap|limit)"
    r"|billing\\s+(?:quota|limit|credits?|details)|plan\\s+and\\s+billing"
    r"|exceeded\\s+your\\s+current\\s+quota|resource_exhausted"
    r"|credit\\s+balance\\s+is\\s+too\\s+low"
    r"|(?:credit|token)\\s+(?:balance|allowance)\\s+(?:is\\s+)?(?:exhausted|depleted))",
    re.IGNORECASE)

_SOTTO_UNAVAILABLE_RE = re.compile(r"\\b(?:503|529)\\b|overloaded|high demand", re.IGNORECASE)

# The machine payload that separates a real provider envelope from assistant prose which
# merely opens with the same words: a status code, a per-attempt retry log, or a JSON body.
_SOTTO_PROVIDER_PAYLOAD_RE = re.compile(
    r"(?:http|code|status|error)\\W{0,3}\\s*[45]\\d{2}\\b"
    r"|\\battempt\\s+\\d+\\s*:"
    r"|[\\[{]\\s*[\\[{]"
    r"|[\\[{]\\s*\\W?\\s*(?:error|code|message|status|detail)\\W{0,2}\\s*:",
    re.IGNORECASE)

_SOTTO_PROVIDER_FALLBACK_REPLY = "I couldn't get an answer from the service I use. Please try again later."

# Chat copy describes what the user can do without exposing HTTP/provider internals.
_PROVIDER_ERROR_REPLIES = (
    (_SOTTO_ALLOWANCE_ERROR_RE, "This account has reached its usage allowance. "
                                "The account owner needs to review the limit before I can continue."),
    (_GATEWAY_AUTH_ERROR_RE, "I can't access the service I use to answer. "
                             "Please ask the account owner to check the connection."),
    (_GATEWAY_PROVIDER_POLICY_RE, "I couldn't answer that request because the service declined it. "
                                  "Rephrasing may help."),
    (_SOTTO_UNAVAILABLE_RE, "The service I use is temporarily unavailable. "
                            "Please try again in a little while."),
    (_GATEWAY_RATE_LIMIT_RE, "The service I use is handling too many requests right now. "
                             "Please try again in a little while."),
    (_GATEWAY_CONNECTION_ERROR_RE, "I couldn't reach the service I use to answer just now. "
                                   "Please try again in a little while."))

# Both the status and the turn-final boundary emit this copy. Recognising it keeps the
# sanitizer idempotent, so a message that crosses both boundaries stays one sentence.
_SOTTO_PLAIN_PROVIDER_REPLIES = frozenset(
    [reply for _pattern, reply in _PROVIDER_ERROR_REPLIES] + [_SOTTO_PROVIDER_FALLBACK_REPLY])
'''

OLD_FALLBACK = '''    return (
        "⚠️ The model provider failed after retries. I kept raw provider details "
        "out of chat; check gateway logs for diagnostics.")
'''

NEW_FALLBACK = '''    return _SOTTO_PROVIDER_FALLBACK_REPLY
'''

OLD_LOOKS = '''    if not text:
        return False
    body = str(text).strip()
    if len(body) > 400 or body.count("\\n") > 4:
        return False
    return bool(_GATEWAY_PROVIDER_ERROR_SHAPE_RE.search(body))
'''

NEW_LOOKS = '''    if not text:
        return False
    body = str(text).strip()
    if body in _SOTTO_PLAIN_PROVIDER_REPLIES:
        # Already-sanitized copy is ordinary text: keep this boundary idempotent.
        return False
    marker = _GATEWAY_PROVIDER_ERROR_SHAPE_RE.search(body)
    if marker is None:
        return False
    # A provider envelope announces itself in its first line. What follows can be a
    # pretty-printed JSON payload or a per-attempt retry log of any size, so the decision is
    # made from the head that FOLLOWS the marker, not from the body's length/newline count.
    if _SOTTO_PROVIDER_PAYLOAD_RE.search(body[marker.end():marker.end() + 300]):
        return True
    # Ordinary prose can open with the same words ("API call failed: this is the heading in
    # your error report."). With no status code, JSON payload or attempt line behind the
    # marker, only a short single-paragraph body is treated as a provider envelope.
    return len(body) <= 400 and body.count("\\n") <= 4
'''

OLD_STREAM_FINAL = '''            if isinstance(fr, str) and fr.strip() and fr != "(empty)":
                _final_for_stream = fr
'''

NEW_STREAM_FINAL = '''            if isinstance(fr, str) and fr.strip() and fr != "(empty)":
                # Native stream finalization happens before the outer delivery boundary.
                # Apply the same chat sanitizer before this payload can seal the stream.
                from gateway.run import _sanitize_gateway_final_response
                _final_for_stream = _sanitize_gateway_final_response(ctx.source.platform, fr)
'''

# (old, new, label) per patched region. `label` names the region in failure messages.
GATEWAY_ANCHORS = (
    (OLD_REPLIES, NEW_REPLIES, 'reply table'),
    (OLD_FALLBACK, NEW_FALLBACK, 'fallback'),
    (OLD_LOOKS, NEW_LOOKS, 'failure envelope'),
)
TURN_RUNNER_ANCHORS = ((OLD_STREAM_FINAL, NEW_STREAM_FINAL, 'stream final boundary'),)

REMEDY = 'regenerate the pin per adapters/hermes/README.md'


def _atomic_write(path, text):
    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _unpatched(source, anchors):
    """Undo this seam's edits so an applied and an untouched runtime hash the same."""
    original = source
    for old, new, _label in anchors:
        if new in original:
            original = original.replace(new, old, 1)
    return original


def _require_pin(original, expected, what):
    found = hashlib.sha256(original.encode()).hexdigest()
    if found != expected:
        raise RuntimeError(
            f'Hermes {what} differs from the reviewed pin '
            f'(expected {expected[:8]}, found {found[:8]})')


def _require_anchors(original, anchors):
    """A matching hash with a missing anchor means the constants here drifted from the pin."""
    for old, _new, label in anchors:
        if original.count(old) != 1:
            raise RuntimeError(f'pinned Hermes {label} differs from the reviewed source')


def _original_source(source):
    """Return the reviewed, unpatched gateway source or fail closed."""
    original = _unpatched(source, GATEWAY_ANCHORS)
    _require_pin(original, PINNED_SOURCE_SHA256, 'gateway source')
    return original


def _original_turn_runner(source):
    """Return the reviewed, unpatched turn runner source or fail closed."""
    original = _unpatched(source, TURN_RUNNER_ANCHORS)
    _require_pin(original, PINNED_TURN_RUNNER_SHA256, 'turn runner')
    return original


def patch(path):
    path = Path(path)
    text = path.read_text()
    original = _original_source(text)
    if all(new in text for _old, new, _label in GATEWAY_ANCHORS):
        return
    _require_anchors(original, GATEWAY_ANCHORS)
    patched = original
    for old, new, _label in GATEWAY_ANCHORS:
        patched = patched.replace(old, new, 1)
    _atomic_write(path, patched)


def patch_turn_runner(path):
    path = Path(path)
    source = path.read_text()
    original = _original_turn_runner(source)
    if NEW_STREAM_FINAL in source:
        return
    _require_anchors(original, TURN_RUNNER_ANCHORS)
    _atomic_write(path, original.replace(OLD_STREAM_FINAL, NEW_STREAM_FINAL, 1))


def _turn_runner_path(run_path):
    return Path(run_path).with_name('run_turn_runner.py')


def check_runtime(run_path):
    """Validate both pinned hashes and every anchor without writing anything.

    This is what `--check` runs at image build time: a `hermes.commit` bump that moves
    either file fails the build, rather than passing the build and then failing every
    container at boot when start.sh applies the patch for real.
    """
    run_path = Path(run_path)
    turn_runner = _turn_runner_path(run_path)
    _require_anchors(_original_source(run_path.read_text()), GATEWAY_ANCHORS)
    _require_anchors(_original_turn_runner(turn_runner.read_text()), TURN_RUNNER_ANCHORS)


def patch_runtime(run_path):
    run_path = Path(run_path)
    # Validate both files before either mutation, avoiding a half-applied runtime.
    check_runtime(run_path)
    patch(run_path)
    patch_turn_runner(_turn_runner_path(run_path))


def main(argv):
    flags = {arg for arg in argv if arg.startswith('-')}
    positional = [arg for arg in argv if not arg.startswith('-')]
    if flags - {'--check'} or len(positional) != 1:
        print('usage: provider_error_compat.py [--check] <hermes>/gateway/run.py', file=sys.stderr)
        return 2
    try:
        if '--check' in flags:
            check_runtime(positional[0])
            print('[sotto] Hermes gateway matches the reviewed provider-error pin')
        else:
            patch_runtime(positional[0])
            print('[sotto] friendly provider error presentation applied')
    except RuntimeError as err:
        # One line, in the same shape as start.sh's neighbouring FATAL messages — a bare
        # traceback here reads like a crash rather than a deliberate refusal.
        print(f'[sotto] FATAL: {err} — {REMEDY}', file=sys.stderr)
        return 1
    except OSError as err:
        print(f'[sotto] FATAL: cannot read the pinned Hermes gateway source ({err}) — {REMEDY}',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

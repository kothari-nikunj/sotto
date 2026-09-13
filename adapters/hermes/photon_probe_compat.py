"""Recognize the pinned SDK's server-answered synthetic read, without messaging.

Hermes issue #101618 / PR #101624 identify a Photon proxy validation response
that the stock probe discards. This narrower adaptation accepts only the exact
observed validation with the Photon source stamp and gRPC INVALID_ARGUMENT status.
Authentication errors, timeouts, unavailable and other proxy failures stay
inconclusive. Remove after a tested upstream pin covers this contract.
"""
import hashlib
from pathlib import Path
import sys

SOURCE_SHA256 = '05f783a619d04080b9b1a05f5312c8d5b45a02aef13f99a8f0255041504ade4c'
ANCHOR = '  // Anything else (UNAVAILABLE, DEADLINE_EXCEEDED, TLS, auth, ...) does NOT'
INSERT = '''  // Sotto compatibility: this exact rejection came from the Photon proxy.
  // A component stamp alone must never turn an auth/network error into health.
  if (err?.grpcCode === 3 &&
      message === "[spectrum-imessage] Expected message resource GUID") {
    return { alive: true, inconclusive: false, reason: "server-answered synthetic read" };
  }
'''


def apply(path):
    path = Path(path)
    source = path.read_text()
    original = source.replace(INSERT, '', 1)
    if hashlib.sha256(original.encode()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('Photon probe source differs from the reviewed pin')
    if source == original:
        path.write_text(source.replace(ANCHOR, INSERT + ANCHOR, 1))


if __name__ == '__main__':
    apply(sys.argv[1])
    print('[sotto] pinned Photon probe compatibility applied')

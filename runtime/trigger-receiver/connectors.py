#!/usr/bin/env python3
"""
Remote-MCP service connectors (lane 2 of the integration doctrine — see INTEGRATIONS.md).

One generic OAuth 2.1 connect surface for every standards-compliant remote MCP: the container
registers ITSELF at connect time (Dynamic Client Registration), PKCE carries the proof, and the
receiver's public /connect/oauth/callback is the redirect_uri — so a headless deploy needs no
pre-registered client and no vendor relationship. Tokens land per-service on the volume where the
pipeline's deterministic gathers (gather_granola.py via connector_tokens.py) pick them up.

Flow (driven by receiver.py):
  GET /connect/<service>/start  (setup-code-gated)  → discover → DCR → pending state → 302 authorize
  GET /connect/oauth/callback   (IdP calls it)      → state (single-use) → code exchange → token file

On-volume layout (all under $SOTTO_DATA/connectors/):
  <service>.json            — the PINNED token file (shared contract with connector_tokens.py):
                              {service, access_token, refresh_token|null, expires_at|null,
                               token_endpoint, client_id, resource, mcp_url, obtained_at}
                              atomic write, 0600. This module WRITES it; refresh for gathers is
                              connector_tokens.py's job; /setup only READS presence + dates.
  <service>.client.json     — cached DCR client_id (re-registered only if missing/rejected).
  <service>.discovery.json  — cached endpoint discovery (24h TTL).
  .pending/<state>.json     — in-flight PKCE state (service + code_verifier + created_at, 10 min TTL).

Discovery order (RFC 9728 → RFC 8414 → convention):
  1. <mcp_url origin>/.well-known/oauth-protected-resource → authorization_servers[0]
  2. <auth>/.well-known/oauth-authorization-server, else <auth>/.well-known/openid-configuration
  3. else auth_fallback + /oauth2/{register,authorize,token} — the conventional paths the Mac app
     used against mcp-auth.granola.ai (api/src/routes/granola-oauth.ts proved DCR+PKCE works there).

All HTTP goes through the injectable `_http` (stdlib urllib) so tests mock the wire. Stdlib only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import contextlib
import fcntl
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

DATA = os.environ.get("SOTTO_DATA", "/data")

# The service registry drives the /setup tiles. Adding a new remote-MCP service = one entry here
# (label for the tile, the MCP endpoint, and where its auth server lives if discovery fails).
SERVICES = {
    "granola": {
        "label": "Granola (meeting notes)",
        "mcp_url": "https://mcp.granola.ai/mcp",
        "auth_fallback": "https://mcp-auth.granola.ai",
    },
}

# The OTHER kind of connector. Granola is here because it runs a remote-MCP auth server we can DCR
# against; Exa and Parallel do not run one — they authenticate with an API key. That is the whole
# reason they took the env-var route, and it is not a reason for them to be INVISIBLE: "where do I
# see Exa?" had no answer while they lived only in a table in RAILWAY.md.
#
# So both kinds are registered here, in one place, and the Connections page renders them together.
# These rows are READ-ONLY on purpose: the key lives in the host's environment (Railway's own
# encrypted store), and nothing in this process writes one to the volume.
#
# This table mirrors `_shared/scripts/web_research.py`'s KEY_ENV + CAPABILITIES, which is the real
# provider ladder. The receiver image cannot import the skills tree, so — exactly like keys.py and
# RELATION_SENTENCE — it is duplicated ONCE and guarded: tests/test_docs_drift.py fails the suite
# if the two ever disagree.
KEY_PROVIDERS = {
    "exa": {"label": "Exa", "env": "EXA_API_KEY",
            "does": "web search, and deep research when Parallel isn't set"},
    "parallel": {"label": "Parallel", "env": "PARALLEL_API_KEY",
                 "does": "deep research (first choice when set)"},
    "gemini": {"label": "Gemini grounding", "env": "GOOGLE_AI_API_KEY",
               "does": "the built-in fallback for both — already set if briefs work"},
}

# Per capability, the providers that could answer it, in precedence order. First one with a key wins.
CAPABILITIES = {
    "web_search": ("exa", "gemini"),
    "deep_research": ("parallel", "exa", "gemini"),
}

DISCOVERY_TTL_SECS = 24 * 3600   # re-discover daily; providers move endpoints rarely but do
PENDING_TTL_SECS = 10 * 60       # an authorize leg older than this is dead — sweep it
HTTP_TIMEOUT_SECS = 20
_STATE_RE = re.compile(r"\A[A-Za-z0-9_-]{8,128}\Z")   # our states are token_urlsafe; anything else
                                                      # is attacker input (the callback is public)


class ConnectorError(Exception):
    """A named-step failure. `step` is one of config/discovery/registration/state/exchange —
    the callback/start pages surface it verbatim so the FIRST click tells you which leg broke."""

    def __init__(self, step: str, detail: str):
        self.step = step
        super().__init__(detail)


def _http(url: str, method: str = "GET", headers: dict | None = None,
          body: bytes | None = None) -> tuple[int, bytes]:
    """One tiny wire primitive: (status, body). HTTP errors return their real status + body
    (upstream error bodies are part of the UX); transport failures return status 0 with the
    exception text as the body. Tests monkeypatch THIS symbol."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except OSError:
            return e.code, b""
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, str(e).encode()


# ── paths + small helpers ──────────────────────────────────────────────────────────────────────────

def _connectors_dir() -> str:
    return os.path.join(DATA, "connectors")


def token_path(service: str) -> str:
    return os.path.join(_connectors_dir(), f"{service}.json")


def _client_path(service: str) -> str:
    return os.path.join(_connectors_dir(), f"{service}.client.json")


def _discovery_path(service: str) -> str:
    return os.path.join(_connectors_dir(), f"{service}.discovery.json")


def _pending_dir() -> str:
    return os.path.join(_connectors_dir(), ".pending")


def write_json(path: str, obj, mode: int = 0o600, indent: int | None = None) -> None:
    """THE atomic JSON write for the whole receiver image (receiver.py, dashboard.py and calcache.py
    reach it as CONNECTORS.write_json / HOOKS["write_json"]): tmp file at `mode`, then os.replace —
    a crash mid-write can't corrupt the destination, and the default 0600 means the volume never
    holds a world-readable file. Callers that want a human-diffable file pass indent=2."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # PROCESS-UNIQUE temp. `path + ".tmp"` was itself a shared mutable resource: two writers opened
    # the same scratch file, and whoever renamed first pulled it out from under the other — one of
    # the three ways concurrent preference writes corrupted or lost data (Aug 2026).
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


LOCK_SUFFIX = ".lock"   # MUST match _shared/lib/jsonstore.LOCK_SUFFIX — asserted by test_docs_drift
LOCK_TIMEOUT_SECS = 10


@contextlib.contextmanager
def json_transaction(path: str, default=None, mode: int = 0o600, indent: int | None = None):
    """The receiver's half of the read-modify-write lock. The skills tree owns the canonical
    implementation (`_shared/lib/jsonstore.py`); this image cannot import it, so the protocol —
    an advisory flock on `<path>.lock` — is what the two share. flock is an OS primitive, so two
    implementations interoperate as long as they name the same file, and a drift test asserts they
    do. Same copy-plus-guard posture as keys.py.

    Used for `preferences.json`, the one file two processes both write."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lf = os.open(path + LOCK_SUFFIX, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECS
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {path} within {LOCK_TIMEOUT_SECS}s")
                time.sleep(0.02)
        data = _read_json(path)
        if data is None:
            data = default if default is not None else {}
        yield data
        write_json(path, data, mode, indent)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            os.close(lf)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _trunc(body: bytes, n: int = 300) -> str:
    s = body.decode("utf-8", "replace").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ── metadata discovery (RFC 9728 → RFC 8414 → conventional fallback) ──────────────────────────────

def discover(service: str, force: bool = False) -> dict:
    """Resolve {authorization_endpoint, token_endpoint, registration_endpoint, supports_resource}
    for a service, cached on the volume. Never returns partial endpoints; raises
    ConnectorError('discovery') only when the wire is completely dead (every fetch was a transport
    failure) — otherwise the conventional auth_fallback paths always give a full answer."""
    svc = SERVICES.get(service)
    if not svc:
        raise ConnectorError("config", f"unknown service '{service}'")
    if not force:
        cached = _read_json(_discovery_path(service))
        if cached and (time.time() - cached.get("fetched_at", 0)) < DISCOVERY_TTL_SECS \
                and cached.get("authorization_endpoint") and cached.get("token_endpoint"):
            return cached

    transport_dead = True   # flips false on ANY real HTTP status (even a 404)
    last_err = b""

    # 1) RFC 9728: the protected resource names its authorization server(s).
    auth_base = ""
    supports_resource = False
    st, body = _http(_origin(svc["mcp_url"]) + "/.well-known/oauth-protected-resource")
    if st != 0:
        transport_dead = False
    else:
        last_err = body
    if st == 200:
        try:
            servers = (json.loads(body) or {}).get("authorization_servers") or []
            if servers and isinstance(servers[0], str):
                auth_base = servers[0].rstrip("/")
                supports_resource = True   # the resource published RFC 9728 metadata → RFC 8707 applies
        except (json.JSONDecodeError, ValueError):
            pass
    if not auth_base:
        auth_base = svc["auth_fallback"].rstrip("/")

    # 2) RFC 8414 (with the OIDC well-known as the alternate spelling).
    meta = None
    for wk in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        st, body = _http(auth_base + wk)
        if st != 0:
            transport_dead = False
        else:
            last_err = body
        if st == 200:
            try:
                m = json.loads(body) or {}
                if m.get("authorization_endpoint") and m.get("token_endpoint"):
                    meta = m
                    break
            except (json.JSONDecodeError, ValueError):
                pass

    if meta:
        disc = {
            "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"],
            # a server can advertise auth+token endpoints but omit DCR — fall back to convention
            "registration_endpoint": meta.get("registration_endpoint")
                or svc["auth_fallback"].rstrip("/") + "/oauth2/register",
            "supports_resource": supports_resource or bool(meta.get("resource_indicators_supported")),
        }
    else:
        if transport_dead:
            raise ConnectorError(
                "discovery",
                f"could not reach {auth_base} (or the resource metadata): {_trunc(last_err)}")
        # 3) Conventional paths — exactly what the Mac app used against mcp-auth.granola.ai.
        conv = svc["auth_fallback"].rstrip("/") + "/oauth2"
        disc = {
            "authorization_endpoint": conv + "/authorize",
            "token_endpoint": conv + "/token",
            "registration_endpoint": conv + "/register",
            "supports_resource": supports_resource,
        }
    disc["fetched_at"] = int(time.time())
    try:
        write_json(_discovery_path(service), disc)
    except OSError:
        pass   # cache is an optimization; the flow works without a volume
    return disc


# ── Dynamic Client Registration (cached; public client, PKCE, no secret) ──────────────────────────

def ensure_client(service: str, redirect_uri: str, disc: dict | None = None) -> str:
    """Return the DCR client_id, registering once and caching on the volume. Re-registers when the
    cache is missing OR was minted for a different redirect_uri (the public host changed). A
    provider-side rejection of a cached client is healed by clear_client() in the exchange path."""
    cached = _read_json(_client_path(service))
    if cached and cached.get("client_id") and cached.get("redirect_uri") == redirect_uri:
        return cached["client_id"]
    disc = disc or discover(service)
    payload = {
        "client_name": "Sotto",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",   # public client — PKCE, never a stored secret
    }
    st, body = _http(disc["registration_endpoint"], "POST",
                     {"Content-Type": "application/json"}, json.dumps(payload).encode())
    if st not in (200, 201):
        raise ConnectorError(
            "registration",
            f"DCR at {disc['registration_endpoint']} returned HTTP {st}: {_trunc(body)}")
    try:
        client_id = (json.loads(body) or {}).get("client_id")
    except (json.JSONDecodeError, ValueError):
        client_id = None
    if not client_id:
        raise ConnectorError("registration", f"DCR response had no client_id: {_trunc(body)}")
    write_json(_client_path(service),
                {"client_id": client_id, "redirect_uri": redirect_uri,
                 "registered_at": int(time.time())})
    return client_id


def clear_client(service: str) -> None:
    """Drop the cached DCR client so the next /connect/<service>/start re-registers — called when
    the token endpoint rejects the client (invalid_client: the provider pruned our registration)."""
    try:
        os.remove(_client_path(service))
    except OSError:
        pass


# ── PKCE + pending flow state ─────────────────────────────────────────────────────────────────────

def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def sweep_pending() -> None:
    """Delete expired in-flight states (abandoned consent screens). Best-effort."""
    d = _pending_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    now = time.time()
    for name in names:
        p = os.path.join(d, name)
        try:
            if (now - os.path.getmtime(p)) > PENDING_TTL_SECS:
                os.remove(p)
        except OSError:
            pass


def create_pending(service: str) -> tuple[str, str]:
    """Mint (state, code_verifier) for one authorize leg and persist it under .pending/<state>.json
    (0600, TTL 10 min). Sweeps expired states first so abandoned flows never accumulate."""
    sweep_pending()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)   # 64 url-safe chars — inside PKCE's 43–128 window
    write_json(os.path.join(_pending_dir(), f"{state}.json"),
                {"service": service, "code_verifier": verifier, "created_at": int(time.time())})
    return state, verifier


def consume_pending(state: str) -> dict | None:
    """Single-use state lookup: validate the shape (the callback is public — never let attacker
    input touch a path), read, DELETE, and reject if expired. None = unknown/expired/reused."""
    if not state or not _STATE_RE.match(state):
        return None
    p = os.path.join(_pending_dir(), f"{state}.json")
    entry = _read_json(p)
    try:
        os.remove(p)   # single-use even when expired/corrupt
    except OSError:
        pass
    if not entry or not entry.get("service") or not entry.get("code_verifier"):
        return None
    if (time.time() - entry.get("created_at", 0)) > PENDING_TTL_SECS:
        return None
    return entry


# ── the two flow halves the receiver drives ───────────────────────────────────────────────────────

def start_auth(service: str, redirect_uri: str) -> str:
    """Discovery + DCR + pending state → the authorization URL to 302 the browser to."""
    svc = SERVICES.get(service)
    if not svc:
        raise ConnectorError("config", f"unknown service '{service}'")
    disc = discover(service)
    client_id = ensure_client(service, redirect_uri, disc)
    state, verifier = create_pending(service)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    if disc.get("supports_resource"):
        params["resource"] = svc["mcp_url"]   # RFC 8707 — bind the token to the MCP resource
    return disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def finish_auth(state: str, code: str, redirect_uri: str) -> dict:
    """Validate + consume the state, exchange the code (PKCE verifier, no client secret), and write
    the PINNED token file. Returns the written record. Raises ConnectorError with the failing step."""
    pending = consume_pending(state)
    if not pending:
        raise ConnectorError("state",
                             "unknown, expired, or already-used state — restart from /setup "
                             "(each Connect click is valid once, for 10 minutes)")
    service = pending["service"]
    svc = SERVICES.get(service)
    if not svc:
        raise ConnectorError("config", f"pending state names unknown service '{service}'")
    disc = discover(service)
    cached = _read_json(_client_path(service)) or {}
    client_id = cached.get("client_id")
    if not client_id:
        raise ConnectorError("registration",
                             "no registered client on the volume — restart from /setup")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": pending["code_verifier"],
    }
    if disc.get("supports_resource"):
        form["resource"] = svc["mcp_url"]
    st, body = _http(disc["token_endpoint"], "POST",
                     {"Content-Type": "application/x-www-form-urlencoded"},
                     urllib.parse.urlencode(form).encode())
    if st != 200:
        if b"invalid_client" in body:
            clear_client(service)   # provider pruned our DCR client — next start re-registers
        raise ConnectorError(
            "exchange",
            f"token endpoint {disc['token_endpoint']} returned HTTP {st}: {_trunc(body)}")
    try:
        tok = json.loads(body) or {}
    except (json.JSONDecodeError, ValueError):
        raise ConnectorError("exchange", f"token endpoint returned non-JSON: {_trunc(body)}")
    access = tok.get("access_token")
    if not access:
        raise ConnectorError("exchange", f"token response had no access_token: {_trunc(body)}")
    expires_at = None
    if isinstance(tok.get("expires_in"), (int, float)) and tok["expires_in"] > 0:
        expires_at = int(time.time() + tok["expires_in"])
    # THE PINNED CONTRACT — connector_tokens.py (the pipeline side) reads exactly this shape.
    record = {
        "service": service,
        "access_token": access,
        "refresh_token": tok.get("refresh_token") or None,
        "expires_at": expires_at,
        "token_endpoint": disc["token_endpoint"],
        "client_id": client_id,
        "resource": svc["mcp_url"],
        "mcp_url": svc["mcp_url"],
        "obtained_at": int(time.time()),
    }
    write_json(token_path(service), record, 0o600)
    return record


# ── /setup status (read-only: presence + dates; refresh belongs to connector_tokens.py) ───────────

def service_status() -> list[dict]:
    """One row per registry service for the /setup 'Connected services' tiles. Reads only presence
    + obtained_at/expires_at from the token file — never touches the network."""
    out = []
    for sid, svc in SERVICES.items():
        rec = _read_json(token_path(sid))
        connected = bool(rec and rec.get("access_token"))
        out.append({
            "service": sid,
            "label": svc["label"],
            "connected": connected,
            "obtained_at": (rec or {}).get("obtained_at") if connected else None,
            "expires_at": (rec or {}).get("expires_at") if connected else None,
        })
    return out


def key_provider_status() -> list[dict]:
    """One row per key-based provider for the same Connections tile. Presence of the credential is
    the whole test — the identical rule `web_research.provider_chain` applies, so this page can
    never claim a provider the search ladder wouldn't actually use.

    Read-only by design: no key is written here, and none is ever returned — only whether one is
    set, so the page can be rendered by anyone who can already see the dashboard."""
    return [{"provider": pid, "label": p["label"], "env": p["env"], "does": p["does"],
             "connected": bool(os.environ.get(p["env"], "").strip())}
            for pid, p in KEY_PROVIDERS.items()]


def capability_chains() -> list[dict]:
    """What would actually answer each research capability right now: the precedence list filtered
    to providers whose key is present. `active` is the rung that answers; empty means nothing can,
    and the brief says so rather than quietly returning nothing."""
    out = []
    for cap, chain in CAPABILITIES.items():
        live = [p for p in chain if os.environ.get(KEY_PROVIDERS[p]["env"], "").strip()]
        out.append({"capability": cap, "chain": list(chain), "live": live,
                    "active": live[0] if live else ""})
    return out

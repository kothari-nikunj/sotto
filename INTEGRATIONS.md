# Integrations — how Sotto connects to services

Sotto's promise is **your infrastructure, nobody in the loop**: your container, your keys, your
tokens, no third-party broker holding your credentials. Every integration fits one of four lanes,
in order of preference. Google is the outlier, not the template — its user-owned OAuth client
exists only because Google demands app verification for sensitive scopes; most modern services
don't have that wall.

## The four lanes

**Lane 1 — paste a key.** A `/setup` tile or env var + a deterministic gather script. Simplest,
fully deterministic, no third party. Right for services that hand out personal API tokens (Linear
PATs, many SaaS) — and the break-glass fallback for everything else.

**Lane 2 — remote MCP with OAuth 2.1 + Dynamic Client Registration** *(the durable general
answer)*. The MCP auth spec was designed for exactly the self-hosted case: nothing pre-registered,
because **the client registers itself at connect time**. "What's the OAuth client?" — *your own
container*, with DCR minting its identity on the fly and PKCE carrying the proof (no stored
secret). The one leg OAuth needs that a headless box lacks — a browser for the consent screen —
is already covered: the receiver's public `/setup` page hosts the **Connected services** tiles,
and its `/connect/oauth/callback` is the `redirect_uri`. Click **Connect**, approve in your
browser, done — the access+refresh tokens land per-service on your volume
(`$SOTTO_DATA/connectors/<service>.json`) and every later gather runs headlessly. Any
standards-compliant remote MCP (Linear, Notion, Stripe, …) becomes one click, with no vendor
relationship and no broker.

**Lane 3 — the Bridge as local credential/material collector.** The Bridge (the Mac menu-bar app)
is Sotto's first-party MCP *server* for Mac data; its integration role extends to Mac-resident
material — desktop-app login tokens, local files — each behind an explicit consent toggle. The
Bridge is deliberately NOT the MCP client to remote services: your Mac sleeps, so 24/7 token
custody belongs in the container. The browser does the consent leg wherever it is; the container
owns the tokens.

**Lane 4 — brokers (Composio, Nango, …): the escape hatch, never the default.** They solve
hostile-OAuth + no-MCP + no-key services by holding tokens in *their* cloud — which breaks the
"nobody in the loop" principle. Use only if a must-have service fits no other lane, knowingly.

## Worked example: Granola (lane 2)

1. Open your `/setup` wizard → **Connected services** → **Connect** next to *Granola (meeting
   notes)*.
2. Your browser lands on Granola's consent screen (the receiver has already discovered Granola's
   OAuth endpoints and registered itself via DCR). Approve.
3. You bounce back to the wizard's callback with a ✓ — tokens are now on your volume, and briefs,
   meeting prep, and follow-ups gather meeting notes + transcripts headlessly
   (`_shared/scripts/gather_granola.py`) from the next run.

This works on **any Granola plan** — it's your login, not a Business-only API key. If anything
fails, the error page names the exact step (discovery / registration / state / exchange) plus the
provider's error, so the first click is also the diagnosis.

**Break-glass fallback (lane 1):** Granola's official REST API at
`https://public-api.granola.ai/v1` (Bearer keys prefixed `grn_`, minted on Business/Enterprise
plans). Set `GRANOLA_API_KEY` in your deploy's variables and the gather uses REST instead of the
connector. There is also a legacy `GRANOLA_MCP_CMD` path for registering your own stdio MCP
server — see [RAILWAY.md](RAILWAY.md).

## Under the hood (lane 2 mechanics)

- **Discovery**: RFC 9728 (`/.well-known/oauth-protected-resource` on the MCP origin →
  `authorization_servers[0]`) then RFC 8414 (`/.well-known/oauth-authorization-server`, falling
  back to `/.well-known/openid-configuration`), and finally the service's conventional
  `/oauth2/{register,authorize,token}` paths. Cached per-service on the volume.
- **DCR**: the container POSTs the registration endpoint (`client_name: "Sotto"`,
  `token_endpoint_auth_method: "none"` — public client, PKCE only) and caches the `client_id`;
  it re-registers automatically if the provider prunes the registration.
- **Flow**: PKCE S256 + a single-use `state` (10-minute TTL) stored on the volume; the code
  exchange sends the verifier, never a secret; the token request carries `resource=<mcp_url>`
  (RFC 8707) when the provider advertises it.
- **Storage**: `$SOTTO_DATA/connectors/<service>.json`, written atomically with mode 0600:
  `{service, access_token, refresh_token, expires_at, token_endpoint, client_id, resource,
  mcp_url, obtained_at}`. Refresh is handled centrally by the gather layer
  (`connector_tokens.py`); nothing else ever needs to re-run the browser flow.

## Adding a new service

One entry in the registry (`runtime/trigger-receiver/connectors.py`):

```python
SERVICES = {
    "granola": {
        "label": "Granola (meeting notes)",
        "mcp_url": "https://mcp.granola.ai/mcp",
        "auth_fallback": "https://mcp-auth.granola.ai",
    },
    # "linear": {"label": "Linear (issues)", "mcp_url": "https://mcp.linear.app/mcp",
    #            "auth_fallback": "https://mcp.linear.app"},
}
```

That's it — the `/setup` tile, the OAuth flow, token storage, and status all derive from the
entry. Then write the service's gather script (copy `gather_granola.py`) and wire it into the
skills that need it. If a provider pins redirect hosts and rejects DCR, that service falls back
to lane 1 (a key tile) — document it and move on.

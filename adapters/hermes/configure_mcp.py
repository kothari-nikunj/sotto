#!/usr/bin/env python3
"""
Merge an MCP server entry into ~/.hermes/config.yaml.

Uses the documented Hermes config format (mcp_servers.<name>.{url,headers} for HTTP, or
mcp_servers.<name>.{command,args,env} for stdio) rather than guessing CLI flags, so it's a reliable
drop-in across Hermes versions. Idempotent.

  # HTTP transport (e.g. the Sotto Bridge over a tunnel):
  configure_mcp.py --url https://<tunnel> --token <bearer> [--name sotto-local] [--config …]

  # stdio transport (e.g. a Granola MCP server):
  configure_mcp.py --name granola --command uvx --arg some-granola-mcp \
      --env GRANOLA_API_TOKEN=xxx --env GRANOLA_DOCUMENT_SOURCE=remote [--config …]
"""
from __future__ import annotations

import argparse
import os

import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="sotto-local")
    ap.add_argument("--config", default=os.path.expanduser("~/.hermes/config.yaml"))
    # HTTP transport
    ap.add_argument("--url")
    ap.add_argument("--token")
    # stdio transport
    ap.add_argument("--command")
    ap.add_argument("--arg", action="append", default=[], help="repeatable; one per arg")
    ap.add_argument("--env", action="append", default=[], help="repeatable KEY=VALUE")
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    cfg.setdefault("mcp_servers", {})
    if args.command:
        entry: dict = {"command": args.command}
        if args.arg:
            entry["args"] = args.arg
        env = {}
        for kv in args.env:
            k, _, v = kv.partition("=")
            if k:
                env[k] = v
        if env:
            entry["env"] = env
        cfg["mcp_servers"][args.name] = entry
    elif args.url and args.token:
        cfg["mcp_servers"][args.name] = {
            "url": args.url,
            "headers": {"Authorization": f"Bearer {args.token}"},
        }
    else:
        ap.error("provide either --url and --token (HTTP), or --command (stdio)")

    # ensure the scheduler is on (cron fallback path)
    cfg.setdefault("scheduler", {})["enabled"] = True

    os.makedirs(os.path.dirname(args.config) or ".", exist_ok=True)
    with open(args.config, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print(f"{args.name} MCP written to {args.config}")


if __name__ == "__main__":
    main()

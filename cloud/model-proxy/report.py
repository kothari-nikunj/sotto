#!/usr/bin/env python3
"""Read-only, content-free daily workload report; never enforces a spending limit."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import statistics
from zoneinfo import ZoneInfo


def report(path, days=7, zone='UTC', now=None, billed_total=None):
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(zone)
    start = (now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=days - 1)).timestamp()
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    groups = defaultdict(list)
    try:
        for row in db.execute('SELECT * FROM calls WHERE created>=? AND created<=? ORDER BY created',
                              (start, now.timestamp())):
            row = dict(row)
            metadata = json.loads(row.get('metadata_json') or '{}')
            if 'proxy_deployment' not in metadata and metadata.get('deployment'):
                # Rows written before callers declared a deployment carried the PROXY's own
                # Railway id under `deployment`; read them as that, never as a caller.
                metadata = {**metadata, 'proxy_deployment': metadata['deployment'], 'deployment': 'unknown'}
            usage = json.loads(row.get('usage_json') or '{}')
            day = datetime.fromtimestamp(row['created'], tz).date().isoformat()
            key = (day, metadata.get('application', 'unknown'), metadata.get('deployment', 'unknown'),
                   metadata.get('workload', 'unknown'), row['model'], row['route'], row['tenant'],
                   metadata.get('proxy_deployment', 'unknown'))
            groups[key].append((row, metadata, usage))
    finally:
        db.close()
    output = []
    for key, rows in sorted(groups.items()):
        costs = [u.get('estimated_token_cost') for _, _, u in rows]
        known = [c for c in costs if isinstance(c, (float, int))]
        # Bounded = only the rows whose cache detail is missing; exact rows are already in `known`,
        # so the two never overlap and `known + bounded` is a total, not a double count.
        ranges = [u['token_cost_range'] for _, _, u in rows
                  if isinstance(u.get('token_cost_range'), dict) and u['token_cost_range'].get('basis') == 'cache_unknown']
        inputs = sorted(r['input_tokens'] for r, _, _ in rows if r['input_tokens'] is not None)
        operations = {m['operation_id'] for _, m, _ in rows if m.get('operation_id')}
        counts = {}
        for field in ('cached_input_tokens', 'visible_output_tokens', 'reasoning_tokens', 'billed_output_tokens'):
            values = [u[field] for _, _, u in rows if isinstance(u.get(field), int)]
            counts[field] = sum(values) if values else None
            counts[field + '_unknown_requests'] = len(rows) - len(values)
        components = {}
        for name in ('instructions', 'tools', 'tool_results', 'conversation', 'evidence', 'schema'):
            values = sorted(m.get('context_chars', {}).get(name) for _, m, _ in rows
                            if isinstance(m.get('context_chars', {}).get(name), int))
            if values:
                components[name] = {'median': statistics.median(values),
                                    'p95': values[math.ceil(.95 * len(values)) - 1]}
        attributed = sum(bool(m.get('operation_id')) for _, m, _ in rows)
        output.append(dict(zip(('day', 'application', 'deployment', 'workload', 'model', 'route', 'owner', 'proxy_deployment'), key)) | {
            'requests': len(rows), 'operations': len(operations),
            'unattributed_requests': sum(not m.get('operation_id') for _, m, _ in rows),
            'unsuccessful_requests': sum(r['status'] != 200 for r, _, _ in rows),
            'context_chars': components,
            'attempts_per_operation': attributed / len(operations) if operations else None,
            'input_tokens': sum(inputs) if inputs else None, 'unknown_input_requests': len(rows) - len(inputs),
            'cache_ratio': counts['cached_input_tokens'] / sum(inputs) if (inputs and sum(inputs)
                and not counts['cached_input_tokens_unknown_requests'] and len(inputs) == len(rows)) else None, 'p95_input_tokens': inputs[math.ceil(.95 * len(inputs)) - 1] if inputs else None,
            'median_input_tokens': statistics.median(inputs) if inputs else None,
            'known_token_cost': round(sum(known), 6) if known else None,
            'unknown_cost_requests': len(rows) - len(known),
            'bounded_token_cost': {k: round(sum(r[k] for r in ranges), 6) for k in ('lower', 'upper')} if ranges else None,
            'unbounded_cost_requests': len(rows) - len(known) - len(ranges), **counts})
    known_total = sum(group['known_token_cost'] or 0 for group in output)
    bounded = [group['bounded_token_cost'] for group in output if group['bounded_token_cost']]
    bounded_total = ({k: round(sum(b[k] for b in bounded), 6) for k in ('lower', 'upper')}
                     if bounded else None)
    unknown = sum(group['unknown_cost_requests'] for group in output)
    diagnostics = []
    if any(group['unattributed_requests'] for group in output):
        diagnostics.append({'kind': 'missing_attribution', 'action': 'Inspect unknown workloads; no service blocking.'})
    if unknown:
        diagnostics.append({'kind': 'unknown_cost', 'requests': unknown})
    return {'known_token_cost': round(known_total, 6) if any(g['known_token_cost'] is not None for g in output) else None,
            'bounded_token_cost': bounded_total,
            'billing_reconciliation': {'billed_total': billed_total,
                'residual_to_known_tokens': round(billed_total - known_total, 6) if billed_total is not None else None,
                'residual_to_upper_bound': (round(billed_total - known_total - (bounded_total or {}).get('upper', 0), 6)
                                            if billed_total is not None else None),
                'note': 'Supply the same billing scope, currency and interval; residual includes unknown calls, fees and billing lag.'},
            'diagnostics': diagnostics, 'timezone': zone, 'through': now.isoformat(), 'includes_partial_today': True,
            'provider_fees': 'not included; reconcile with billing', 'groups': output}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('--days', type=int, choices=range(1, 32), default=7)
    parser.add_argument('--timezone', default='UTC')
    parser.add_argument('--billed-total', type=float, help='Optional same-scope USD billing total for reconciliation')
    args = parser.parse_args()
    print(json.dumps(report(args.database, args.days, args.timezone, billed_total=args.billed_total), indent=2))

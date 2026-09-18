#!/usr/bin/env python3
"""Read-only check of a current deployment inventory; never starts/stops a service."""
import argparse
from collections import defaultdict
import json
from pathlib import Path


def check(rows):
    owners = defaultdict(set)
    unknown = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('active'), bool):
            raise ValueError(f'inventory row {index} needs an explicit active boolean')
        if not row['active']:
            continue
        if not all(row.get(k) for k in ('application', 'deployment', 'owner', 'billing_project', 'key_identity')):
            unknown.append(index)
        for workload in row.get('scheduled_workloads', []):
            owners[(row.get('application'), row.get('owner'), workload)].add(row.get('deployment'))
    duplicate = [{'application': key[0], 'owner': key[1], 'workload': key[2],
                  'deployments': sorted(deployments, key=str)} for key, deployments in owners.items() if len(deployments) > 1]
    return {'duplicate_scheduler_owners': duplicate, 'incomplete_active_rows': unknown,
            'ok': not duplicate and not unknown}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', help='Private JSON array of deployment/caller records; no key secrets')
    args = parser.parse_args()
    result = check(json.loads(Path(args.inventory).read_text()))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['ok'] else 1)

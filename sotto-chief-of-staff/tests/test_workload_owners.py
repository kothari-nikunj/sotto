import importlib.util
from pathlib import Path


def test_duplicate_scheduler_ownership_and_shared_billing():
    path = Path(__file__).resolve().parents[2] / 'tools/check-workload-owners.py'
    spec = importlib.util.spec_from_file_location('owner_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = {'application': 'sotto', 'owner': 'a', 'deployment': 'pilot', 'active': True,
           'billing_project': 'shared-billing', 'key_identity': 'key-resource',
           'scheduled_workloads': ['brief', 'memory']}
    assert module.check([row, {**row, 'deployment': 'retired', 'active': False}])['ok']
    assert len(module.check([row, {**row, 'deployment': 'old'}])['duplicate_scheduler_owners']) == 2
    assert module.check([row, {**row, 'application': 'another-app', 'deployment': 'other'}])['ok']
    assert not module.check([{**row, 'owner': ''}])['ok']

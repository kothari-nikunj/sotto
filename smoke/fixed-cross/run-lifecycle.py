"""Exercise the real same-flock replacement launcher with synthetic channel roles."""
import json
import sys
import time
from urllib.request import urlopen

from run import (DATA, NODE_CONFIG, NODE_PORT, RECEIVER_PORT, RECEIVER_UID,
                 authority, fixed_five, native_replacement,
                 require, stage_inputs, stable_identity)
import tenant_lifecycle


def main():
    data, config, _handoff = stage_inputs()
    life = tenant_lifecycle.Lifecycle(
        DATA, native_uid=RECEIVER_UID, private_pid_namespace=True)
    life.prove_empty_start()
    original = life._start_replacement_roles

    def synthetic_node_entry(node_argv, receiver_argv, node_env, receiver_env,
                             *, epoch_binding):
        # The production handoff argv is checked by verified_handoff first;
        # only the final Node entrypoint is replaced with an injected fake app.
        require(node_argv[1] == '/app/runtime/sotto-chat/dist/bootstrap/main.js',
                'verified Node argv changed')
        return original(('/usr/local/bin/node', '/fixture/node-harness.mjs',
                         str(NODE_CONFIG)), receiver_argv, node_env, receiver_env,
                        epoch_binding=epoch_binding)

    life._start_replacement_roles = synthetic_node_entry
    marker = DATA / '.sotto-volume.json'
    original_marker = marker.read_bytes()
    marker.write_text('{"schema":1,"tenant_id":"synthetic-tenant",'
                      '"volume_id":"wrong-volume"}')
    try:
        native_replacement.start_configured_replacement(
            life, data, authority(config), stable_identity=stable_identity(config),
            node_config_path=str(NODE_CONFIG), data_directory=str(DATA),
            skills_root='/app/sotto-skills', limited_profile=fixed_five.PROFILE)
    except tenant_lifecycle.LifecycleUnavailableError:
        pass
    else:
        raise RuntimeError('mismatched on-volume marker launched replacement')
    require(life._state == 'quiescent' and not life._replacement_children
            and not (DATA / '.sotto-epoch.json').exists(),
            'mismatched marker left a child or replacement hold')
    marker.write_bytes(original_marker)
    native_replacement.start_configured_replacement(
        life, data, authority(config), stable_identity=stable_identity(config),
        node_config_path=str(NODE_CONFIG), data_directory=str(DATA),
        skills_root='/app/sotto-skills', limited_profile=fixed_five.PROFILE)
    hold = DATA / '.sotto-epoch.json'
    require(hold.is_file(),
            'replacement epoch hold missing')
    raw_hold = hold.read_text()
    saved = json.loads(raw_hold)
    require(saved['version'] == 3 and saved['status'] == 'reconciliation_required',
            'durable replacement epoch version changed')
    binding = saved['binding']
    require(binding['tenant_id'] == 'synthetic-tenant'
            and binding['volume_id'] == 'synthetic-volume-id'
            and binding['runtime_config_sha256'] == _handoff.runtime_config_sha256
            and not {'native_store_sha256', 'native_profile_identity'} & set(binding),
            'durable replacement epoch lost verified authority')
    require('photon-project-secret-fixture' not in raw_hold
            and 'runtime-api-fixture-token' not in raw_hold,
            'replacement epoch exposed a credential')
    deadline = time.monotonic() + 35
    healthy = False
    try:
        while time.monotonic() < deadline:
            life.check_replacement()
            if (DATA / 'node-state/fixture-ready').is_file():
                try:
                    with urlopen(f'http://127.0.0.1:{RECEIVER_PORT}/health', timeout=0.5) as response:
                        healthy = response.status == 200
                    if healthy:
                        break
                except OSError:
                    pass
            time.sleep(0.05)
        require(healthy, 'replacement roles failed local health')
        require(config['port'] == NODE_PORT, 'Node port changed')
    finally:
        life.stop_replacement(timeout=10, grace=0.5)
    require((DATA / '.sotto-epoch.json').is_file(),
            'clean process stop improperly cleared handoff hold')
    try:
        life.close()
    except tenant_lifecycle.LifecycleUnavailableError:
        pass
    else:
        raise RuntimeError('replacement lock was released without reconciliation')
    print(json.dumps({'ok': True, 'actual_lifecycle_start': True,
                      'receiver_health': healthy, 'hold_retained_after_stop': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- top-level fixture failure is reported then exits.
        print('fixed-lifecycle failed: ' + type(error).__name__ + ': ' + str(error)[:150],
              file=sys.stderr)
        raise SystemExit(1)

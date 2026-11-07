# Experimental fixed-profile process fixture

Build this overlay from the `sotto-agent` source tree after building a fresh
`runtime/sotto-chat/Dockerfile.runtime` image from the same revision. Supply that
image explicitly; the fixture has no default or remote base image:

```sh
docker build --network none --pull=false \
  --build-arg RUNTIME_IMAGE=sotto-chat-runtime:local \
  -f smoke/fixed-cross/Dockerfile -t sotto-chat-fixed-cross:local .
docker run --rm --pull=never --network none --read-only --user 0:0 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /data:rw,nosuid,nodev,size=64m,mode=0755 \
  --entrypoint python3 sotto-chat-fixed-cross:local /fixture/run.py
docker run --rm --pull=never --network none --read-only --user 0:0 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /data:rw,nosuid,nodev,size=64m,mode=0755 \
  --entrypoint /usr/bin/tini sotto-chat-fixed-cross:local -- \
  python3 /fixture/run-lifecycle.py
```

Run only in a dedicated private PID namespace. The overlay copies only the
test scripts into `/fixture`; all app code comes from the fresh production image. Both runs stage synthetic credentials
and data in tmpfs. The Node entrypoint is an injected harness importing compiled
`startConfiguredRuntime`; it supplies fake Photon, model, and transport
implementations that fail if contacted. Neither run contacts a provider or model.

`run.py` manually holds the original tenant flock, starts Node as UID 10000 and
the actual `managed_exec.py` receiver wrapper as UID 10001, then stops and reaps
both. `run-lifecycle.py` is a direct child of tini PID1. It constructs the
real `Lifecycle`, proves the namespace is empty, runs
`native_replacement.start_configured_replacement`, and verifies the durable
handoff hold remains after stopping replacement. Only the final Node argv is
substituted with the harness after `verified_handoff` checks the canonical
`bootstrap/main.js` argv. Thus the production `bootstrap/main.js` process
entrypoint itself is not exercised.

The manual fixture checks a root-owned sticky 1771 tenant directory, private Node and
receiver children, delayed Node readiness, receiver health, flock contention,
reacquisition after shutdown and receiver-only gallery access. The empty-outbox profile is an initial
fresh-volume start only; this fixture does not prove restart after accepted
messages, provider credentials, network delivery, scheduler effects, or
production supervisor handoff.

## Configured-identity mismatch negative case

After building the overlay, this additional offline check changes only the
receiver's synthetic Photon project secret while keeping the Node harness and
trusted staged identity fixed. The receiver must exhaust its bounded local
identity check and exit before exposing health. It takes about 30 seconds and
is intentionally not part of the default CI run:

```sh
docker run --rm --pull=never --network none --read-only --user 0:0 \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /data:rw,nosuid,nodev,size=64m,mode=0755 \
  --entrypoint python3 sotto-chat-fixed-cross:local /fixture/run-config-mismatch.py
```

The fixture uses only synthetic credentials, the fake Node harness, and tmpfs
state. A pass prints `configured_mismatch_held:true` and
`receiver_health_exposed:false`; it does not establish provider credential
validity or production readiness.

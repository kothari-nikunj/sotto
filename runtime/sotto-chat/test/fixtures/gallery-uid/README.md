# Cross-UID gallery byte-transfer smoke

This manual, credential-free smoke verifies the file-permission boundary used by the composed runtime. A short-lived root test harness creates four synthetic PNGs owned by UID 10001 with mode `0600`, starts the actual Node HTTP route and journal as UID 10000, and invokes the receiver's `runtime_api.send_gallery` as UID 10001. The Node side uses an injected fake transport; the only network traffic is loopback inside a `--network none` container. No provider, model, keychain, or external service is contacted.

The harness checks that Node cannot open the receiver paths, that the actual receiver client transfers all four PNG byte sequences in order, that the fake transport receives identical hashes and summary, and that repeating one dispatch ID produces one transport call and an accepted durable receipt. The runner needs a production image containing the Node and Python dependencies plus the UID 10000/10001 accounts. Its explicit runtime dist and `adapters/sotto` mounts must match each other; record the image tag and overlay source revision separately; this is an overlay smoke, not proof that an image was rebuilt from those sources.

Run from the `sotto-agent` directory after building a fresh `runtime/sotto-chat/Dockerfile.runtime` image and `npm run build`:

```sh
WORK=/absolute/path/to/credential-free-work-directory
IMAGE=sotto-chat-runtime:local
DIST="$PWD/runtime/sotto-chat/dist"
ADAPTERS="$PWD/adapters/sotto"
FIXTURE="$PWD/runtime/sotto-chat/test/fixtures/gallery-uid"

docker --config "$WORK/docker-config" \
  run --rm \
  --network none --read-only --user 0:0 --cap-drop=ALL \
  --cap-add=SETUID --cap-add=SETGID --cap-add=CHOWN --cap-add=FOWNER \
  --cap-add=DAC_OVERRIDE --cap-add=KILL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=64m \
  --mount "type=bind,source=$FIXTURE,target=/fixture,readonly" \
  --mount "type=bind,source=$DIST,target=/app/runtime/sotto-chat/dist,readonly" \
  --mount "type=bind,source=$ADAPTERS,target=/app/adapters/sotto,readonly" \
  --entrypoint /usr/local/bin/python3 "$IMAGE" -I -B /fixture/run.py
```

The root identity is used only by the fixture harness to create the two owners, launch the two unprivileged children, and reap the Node child. The Node service and receiver sender explicitly drop to UIDs 10000 and 10001. The temporary filesystem is the only writable filesystem. Docker configuration should be a credential-free local configuration; the smoke itself needs no registry access once the image exists.

# Composed runtime failure-path fixture

This local Linux-only fixture exercises the two real process boundaries without enabling the Sotto receiver. It builds the ordinary standalone runtime image, layers in the frozen receiver source and its pinned Telegram import dependency, starts the compiled Node runtime with fake adapter/agent dependencies as UID 10000, then starts the real `managed_exec.py receiver ...` path as root. `managed_exec.py` must drop the receiver to UID 10001. The real receiver is expected to exit at `adapters/sotto/runtime_api.py::runtime_readiness()` because that selected runtime still has unported contracts.

The Python supervisor fixture owns a synthetic `.sotto-runtime.lock` descriptor and passes it to both children. It checks lock contention, relinquishes the supervisor's descriptor while Node remains alive, verifies that Node still holds the lock, expects the receiver readiness failure, sends bounded SIGTERM to Node, and reacquires the lock after Node exits. A second synthetic Node process-group case passes the lock descriptor to a descendant, kills the group leader, proves the descendant still blocks lock reacquisition, then verifies that bounded TERM/KILL cleanup and explicit reaping release the lock only after the descendant exits. The `node_runtime_harness.mjs` injects a fake adapter, transport, and agent; its network fetch seam throws. No provider, model, bot API, credential store, or outside network is contacted.

The script derives the `sotto-agent` build context and both Dockerfile paths from its own location. It works from any current directory and does not require a `.git` directory, so it also works in an exported/publication tree. From the monorepo root, run:

```sh
cd sotto-agent
runtime/sotto-chat/test/fixtures/composed-runtime/run.sh
```

Or, when already inside `sotto-agent`, invoke that path directly. Use `PLATFORM=linux/amd64` to run the same fixture on AMD64. The path contract is also included in the runtime package's ordinary `npm test`: its Node wrapper runs only the Python fake-Docker test, with bounded output and child lifetime. No image build or Docker daemon is used by that unit test. To run the Python test directly, use `python3 -m unittest discover -s runtime/sotto-chat/test/fixtures/composed-runtime -p 'test_*.py'`; it copies a no-Git build context, invokes the script from an unrelated working directory, and substitutes a fake `docker` executable. The full Docker composition smoke remains a separate manual command. The script builds `Dockerfile.runtime` first and uses that image as a local base for this test-only layer. The pinned Hermes commit is `245e48008fa814b3251f50755eb656bd9fb86cb1`; its `pyproject.toml` declares `python-telegram-bot[webhooks]==22.8`. This fixture pins the package core only because it tests imports and fail-closed startup before polling or webhook setup. Its eight-package Python closure is hash-locked in `requirements.txt` and intentionally absent from the production Sotto image. To intentionally refresh the closure, edit `requirements.in` and regenerate from this directory with `uv pip compile --generate-hashes --universal --only-binary :all: --output-file requirements.txt requirements.in`, then review every pin and hash.

The expected JSON result reports the two observed UIDs, receiver's readiness-gate exit, successful Node shutdown, lock contention while Node is alive after the supervisor closes its descriptor, and successful lock reacquisition afterward. It also reports synthetic descendant retention after leader death, required group KILL, descendant reaping, and lock reacquisition after group cleanup. This is negative-path process/ownership evidence only: it does not demonstrate that the receiver is ready, that a production supervisor exists, or that providers can connect. Do not relax the receiver readiness gate to make this fixture pass.


A separate preflight/lifecycle invocation runs first. It calls the real static
`runtime_readiness()` predicate under the tenant flock and requires the current
unported-contract refusal before any child is launched. It then exercises only
inert child processes through the closed `process_handoff` projections: a Node
worker as UID 10000 and a Python worker through `managed_exec.py` as UID 10001.
A stubborn synthetic descendant remains in the Node process group after leader
TERM, so the test requires group KILL/reap before the inherited lock can be
reacquired. This phase does not start the real receiver or any provider; the
existing `composed_failure_smoke.py` invocation remains unchanged and continues
to cover actual receiver fail-closed startup. The two phases are evidence for
separate boundaries, not a successful composed runtime or production supervisor.

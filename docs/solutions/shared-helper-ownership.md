# Shared helper ownership

- Compare helper behavior and bound globals before merging matching names.
- Env-only skill roots share `tzchain.data_root`; hook-based roots retain their own hooks.
- Optional JSON inputs retain their existing null, default and exception policy.
- Integer readers with different coercion or exception rules remain separate.
- Funnel constants and the reserved proactive source use a neutral leaf owner, never a receiver back-import.
- Immutable lookup anchors to each caller file across source and packaged skills trees.
- Freeze source and run the shared gate before accepting an ownership change.

## Why the import boundary matters

The receiver eagerly loads dashboard and then supplies late-bound hooks. Importing receiver
from dashboard can create a partial module or repeat token/config capture. The shared lookup
in `runtime/trigger-receiver/shared_lib.py` loads neutral leaves directly from immutable code
locations. The caller-file anchor prevents a cached source helper from selecting code in a different image or checkout. Receiver keeps its existing one-argument, monkeypatchable `_load_shared_lib` through `functools.partial`. Lookup never resolves executable code from the writable data home.

The image includes the skills tree at `/app/sotto-skills`, while a source or standalone tree
keeps it at `sotto-chief-of-staff`. Copied image siblings remain the first lookup location.
Do not add duplicate policy constants or change Docker behavior to hide a missing leaf module.

The supported consumer checks are receiver/dashboard/calendar-cache imports, current source
and packaged layouts, exact helper semantics and the shared release gate. A successful source
import alone does not prove packaged layout support.

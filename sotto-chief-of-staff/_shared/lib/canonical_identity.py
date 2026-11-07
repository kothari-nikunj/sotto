"""Canonical cold-start identity bytes shared by graph, contact and style projections."""
import hashlib

def generate_canonical_id(seed: str) -> str:
    # knowledge_files.rs:3150 — c_ + 12-hex prefix (6 bytes), so cold-start ids match the Mac app's.
    h = hashlib.sha256(seed.encode()).digest()
    return "c_" + "".join(f"{b:02x}" for b in h[:6])


def default_canonical_id(name: str, identifiers: list) -> str:
    """The shared cold-start id scheme (matches knowledge_update / style_extract / prewarm_graph):
    sha256('kf:{name}|{first_email}') else sha256('kf:{name}')."""
    email = next((str(i).strip().lower() for i in identifiers or [] if "@" in str(i)), None)
    return canonical_id_for_fields(name, email)



def canonical_id_for_fields(name: str, email: str | None) -> str:
    """Mint from already-selected fields; callers retain their own identifier selection rules."""
    seed = f"kf:{name}|{email}" if email else f"kf:{name}"
    return generate_canonical_id(seed)

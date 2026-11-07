"""identity_diagnose.py — read-only identity evidence for a cross-channel duplicate open loop."""
import hashlib
import importlib.util
import json
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "identity_diagnose", os.path.join(ROOT, "_shared", "scripts", "identity_diagnose.py"))
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def _tree(root):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_reports_identity_evidence_without_text_or_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    kg = diag._resolver()._kg
    people = tmp_path / "knowledge" / "people"
    people.mkdir(parents=True)
    kg.write_person_file(str(people / "c_abc123.md"), kg.PersonFile(
        name="Austin", canonical_id="c_abc123", identifiers=["austin@example.com"]))
    ledger = tmp_path / "knowledge" / "continuity"
    ledger.mkdir()
    rows = [("a", {"channel": "gmail", "contact_identifier": "austin@example.com", "canonical_id": "c_abc123"}),
            ("b", {"channel": "whatsapp", "contact_identifier": "15555550123@s.whatsapp.net",
                   "canonical_id": "c_0123456789ab"}),
            ("c", {"channel": "gmail", "contact_identifier": "solo@example.com", "contact_name": "Solo"})]
    for name, fields in rows:
        fm = {"status": "open", "action_type": "reply", "contact_name": "Austin",
              "summary": "SECRET request text", "created_at": "2026-10-01T00:00:00", **fields}
        (ledger / f"{name}.md").write_text("---\n" + yaml.safe_dump(fm) + "---\nbody\n")
    (tmp_path / "knowledge" / "last_local_snapshot.json").write_text(json.dumps({"local": {"contacts": [
        {"name": "Austin", "emails": ["austin@example.com"], "phones": ["+1 555 555 0123"]}]}}))
    before = _tree(tmp_path)
    out = diag.diagnose()
    assert _tree(tmp_path) == before
    assert "SECRET" not in json.dumps(out)
    by_channel = {row["channel"]: row for row in out}
    assert set(by_channel) == {"gmail", "whatsapp"}          # the single-channel name is not listed
    assert by_channel["gmail"]["canonical_id_in_graph"] is True
    whatsapp = by_channel["whatsapp"]
    assert whatsapp["canonical_id_in_graph"] is False        # a drifted stored ID the graph lacks
    assert whatsapp["anchor_normalized"] == "15555550123@s.whatsapp.net"
    assert whatsapp["graph_normalized"] == "5555550123"
    assert whatsapp["unique_identifier_owner"] is None        # the graph never learned the phone
    assert by_channel["gmail"]["contacts_card_links_rows"] == [1]   # only the Contacts card links them

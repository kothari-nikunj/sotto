"""Synthetic People transport only; actual read projection and original writer remain real."""

import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


def configure(receiver):
    root = Path(receiver.DATA)
    path = root / "contacts-provider.json"
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "people/cAlice": {
                        "resourceName": "people/cAlice",
                        "etag": "version-1",
                        "metadata": {"sources": [{"type": "CONTACT", "id": "Alice", "etag": "source-1"}]},
                        "names": [{"givenName": "Alice", "familyName": "Cedar"}],
                        "emailAddresses": [{"value": "alice@example.com"}],
                        "phoneNumbers": [{"value": "+15555550111"}],
                    }
                }
            )
        )

    def rows():
        return json.loads(path.read_text())

    def mutation(operation, kwargs):
        old = rows()
        with (root / "contacts-provider.jsonl").open("a") as log:
            log.write(json.dumps({"operation": operation, "args": kwargs}) + "\n")
        if operation == "create":
            resource = "people/cCreated"
            value = copy.deepcopy(kwargs["body"])
        elif operation == "update":
            resource = kwargs["resourceName"]
            value = old[resource]
            assert kwargs["body"]["etag"] == value["etag"]
            assert kwargs["body"]["metadata"]["sources"] == value["metadata"]["sources"]
            for key in kwargs["updatePersonFields"].split(","):
                value[key] = copy.deepcopy(kwargs["body"][key])
        else:
            del old[kwargs["resourceName"]]
            path.write_text(json.dumps(old))
            if (root / "contacts-unknown").exists():
                raise TimeoutError("synthetic post-contact failure")
            return {}
        value.update(
            resourceName=resource,
            etag="version-2",
            metadata={"sources": [{"type": "CONTACT", "id": resource[8:], "etag": "source-2"}]},
        )
        old[resource] = value
        path.write_text(json.dumps(old))
        if (root / "contacts-unknown").exists():
            raise TimeoutError("synthetic post-contact failure")
        return value

    class People:
        def people(self):
            return self

        def get(self, **kwargs):
            return SimpleNamespace(execute=lambda: rows()[kwargs["resourceName"]])

        def createContact(self, **kwargs):  # noqa: N802 - original Google People resource API
            return SimpleNamespace(execute=lambda: mutation("create", kwargs))

        def updateContact(self, **kwargs):  # noqa: N802 - original Google People resource API
            return SimpleNamespace(execute=lambda: mutation("update", kwargs))

        def deleteContact(self, **kwargs):  # noqa: N802 - original Google People resource API
            return SimpleNamespace(execute=lambda: mutation("delete", kwargs))

    tool = receiver.RUNTIME_OWNER_CONTACTS_TOOL
    google = tool._google()
    google._google_service = lambda service, version: People()
    google.capabilities = lambda: {
        "status": "ok",
        "connected": True,
        "gmail_read": False,
        "gmail_draft": False,
        "gmail_write": False,
        "gmail_send": False,
        "calendar_read": False,
        "calendar_write": False,
        "contacts_read": True,
        "contacts_write": not (root / "contacts-revoked").exists(),
    }
    tool._google = lambda: google
    receiver.RUNTIME_OWNER_GMAIL_DRAFT_TOOL._google = lambda: google
    receiver._google_api_py = lambda: str(Path(__file__).resolve())
    receiver._runtime_source_authority = lambda: {
        "version": 2,
        "complete": True,
        "revision": "a" * 64,
        "bridge_sources": [],
        "google_sources": ["google_contacts"],
        "connector_sources": [],
    }


def read_owner(operation, arguments):
    spec = importlib.util.spec_from_file_location(
        "real_contacts_reader", Path(__file__).resolve().parents[3] / "adapters/sotto/google_api.py"
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    root = Path(os.environ["SOTTO_DATA"])

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, value):
            self.value = value

        def iter_content(self, chunk_size):
            yield json.dumps(self.value).encode()

        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            values = json.loads((root / "contacts-provider.json").read_text())
            with (root / "contacts-reads.jsonl").open("a") as log:
                log.write(json.dumps({"operation": operation}) + "\n")
            if operation == "contacts_search":
                return Response({"results": [{"person": v} for v in values.values()]})
            return Response(values[url.split("/v1/")[1]])

        def close(self):
            pass

    return api.read_owner(operation, arguments, session_factory=Session)

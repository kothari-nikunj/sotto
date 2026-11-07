"""Native inputs compile to the existing launch contract without granting provisioning authority."""

import copy
import json

import pytest

from adapters.sotto import selfhost_bootstrap, selfhost_configuration as config


@pytest.fixture
def inputs():
    request = {
        "version": 1,
        "channel": "photon",
        "tenantId": "native-fixture",
        "railway": {
            "projectId": "11111111-1111-1111-1111-111111111111",
            "environmentId": "22222222-2222-2222-2222-222222222222",
            "provisionRequest": "fixture-request-" + "a" * 24,
        },
        "owner": {"accountId": "fixture-account", "phone": "+15551234567"},
        "lineKey": "primary",
        "instructions": "Answer clearly.",
        "photon": {"projectId": "fixture-project", "projectSecret": "p" * 48, "lineNumber": "+15557654321"},
        "model": {
            "apiKey": "g" * 48,
            "chatModelId": "gemini-chat-fixture",
            "nativeMediaModelId": "gemini-media-fixture",
        },
        "timezone": "America/Los_Angeles",
        "backgroundUnmetered": False,
    }
    attachment = {
        **request["railway"],
        "serviceId": "33333333-3333-3333-3333-333333333333",
        "volumeId": "44444444-4444-4444-4444-444444444444",
        "domain": "fixture.up.railway.app",
        "volumeCreationReceipt": {
            "service_id": "33333333-3333-3333-3333-333333333333",
            "volume_id": "44444444-4444-4444-4444-444444444444",
        },
    }
    secrets = {"runtimeBearer": "r" * 43, "bridgeToken": "b" * 43, "controlToken": "c" * 43}
    return request, attachment, secrets


def test_native_compilation_passes_existing_launch_gate_without_mutating_inputs(inputs, monkeypatch):
    before = copy.deepcopy(inputs)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "ambient-must-not-be-used")
    envelope = config.compile_native(*inputs)
    request, attachment, secrets = inputs
    env = {
        "SOTTO_SELFHOST_LAUNCH": json.dumps(envelope),
        "RAILWAY_PROJECT_ID": attachment["projectId"],
        "RAILWAY_ENVIRONMENT_ID": attachment["environmentId"],
        "RAILWAY_SERVICE_ID": attachment["serviceId"],
        "RAILWAY_PUBLIC_DOMAIN": attachment["domain"],
        "SOTTO_SETUP_REQUEST": attachment["provisionRequest"],
        "RAILWAY_VOLUME_MOUNT_PATH": "/data",
        "RAILWAY_VOLUME_NAME": "fixture-volume",
        "PORT": "8080",
    }
    assert selfhost_bootstrap._railway_envelope(env) == envelope
    assert inputs == before
    assert config.compile_native(*inputs) == envelope
    manifest = envelope["bundle"]["manifest"]
    assert manifest["modelDirect"]["apiKey"] == "g" * 48
    assert manifest["runtime"]["bearerToken"] == secrets["runtimeBearer"]
    assert manifest["relay"]["rootBearer"] == secrets["bridgeToken"]
    assert "mount" not in envelope["bundle"]
    assert envelope["bundle"]["stableIdentity"]["ownerId"] == request["owner"]["phone"]
    assert envelope["bundle"]["receiverOverrides"]["SOTTO_BACKGROUND_UNMETERED"] == "false"


@pytest.mark.parametrize("key", ["projectId", "environmentId", "provisionRequest"])
def test_provider_receipt_must_bind_selected_setup(inputs, key):
    request, attachment, secrets = inputs
    attachment[key] = "99999999-9999-9999-9999-999999999999" if key != "provisionRequest" else "b" * 32
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(request, attachment, secrets)


@pytest.mark.parametrize("key", ["service_id", "volume_id", "extra"])
def test_volume_creation_receipt_cannot_change_or_grow(inputs, key):
    inputs[1]["volumeCreationReceipt"][key] = "different"
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(*inputs)


@pytest.mark.parametrize("field", ["runtimeBearer", "bridgeToken", "controlToken"])
def test_generated_secret_separation_and_bounds(inputs, field):
    inputs[2][field] = "untrusted-short-token"
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(*inputs)


def test_generated_roles_cannot_share_one_bearer(inputs):
    inputs[2]["controlToken"] = inputs[2]["bridgeToken"]
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(*inputs)
    generated = config.new_setup_secrets()
    assert set(generated) == {"runtimeBearer", "bridgeToken", "controlToken"}
    assert len(set(generated.values())) == 3
    assert all(config._SECRET.fullmatch(value) for value in generated.values())


@pytest.mark.parametrize(
    "group,key,value",
    [
        (None, "channel", "telegram"),
        (None, "channel", "local"),
        (None, "version", True),
        (None, "backgroundUnmetered", "true"),
        (None, "timezone", "Not/A_Timezone"),
        (None, "tenantRoot", "/existing-data"),
        ("model", "provider", "another-provider"),
        ("model", "apiKey", "secret\ninvalid"),
        ("model", "chatModelId", "../not-a-model"),
        ("photon", "lineNumber", "not-an-owner"),
        ("photon", "projectId", []),
        ("owner", "phone", "someone@example.test"),
        ("owner", "allowed_user", 123),
        ("railway", "projectId", "not-a-uuid"),
        ("railway", "projectId", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_native_fields_fail_closed(inputs, group, key, value):
    request = inputs[0]
    target = request if group is None else request[group]
    target[key] = value
    with pytest.raises(config.NativeSetupConfigurationError, match="^native setup configuration is invalid$"):
        config.compile_native(*inputs)


@pytest.mark.parametrize(
    "domain", ["http://fixture.up.railway.app", "fixture.test/path", "localhost", "a..test"]
)
def test_public_attachment_domain_uses_existing_handoff_validation(inputs, domain):
    inputs[1]["domain"] = domain
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(*inputs)


def test_bounded_json_decoder_rejects_duplicates_and_nonfinite_values(inputs):
    raw = json.dumps(inputs[0]).encode()
    assert config.parse_native_request(raw) == inputs[0]
    for invalid in [
        b"",
        b"[]",
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
        b'"\xff"',
        b" " * (config.MAX_REQUEST_BYTES + 1),
    ]:
        with pytest.raises(config.NativeSetupConfigurationError):
            config.parse_native_request(invalid)
    inputs[0]["instructions"] = "a" * config.MAX_REQUEST_BYTES
    with pytest.raises(config.NativeSetupConfigurationError):
        config.compile_native(*inputs)


def test_rejections_do_not_echo_secrets(inputs):
    inputs[0]["photon"]["projectSecret"] = "DO-NOT-ECHO\nprivate-secret"
    with pytest.raises(config.NativeSetupConfigurationError) as error:
        config.compile_native(*inputs)
    assert "DO-NOT-ECHO" not in str(error.value)


def test_compilation_does_not_prepare_or_claim_volume(inputs, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("pure compilation crossed the privileged startup boundary")

    monkeypatch.setattr(selfhost_bootstrap, "_railway_stage", forbidden)
    monkeypatch.setattr(selfhost_bootstrap.prepare_fresh, "prepare", forbidden)
    monkeypatch.setattr(selfhost_bootstrap.root_supervisor, "run_operator_file", forbidden)
    config.compile_native(*inputs)

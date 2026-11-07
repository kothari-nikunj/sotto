"""Pure native setup compiler; no provisioning, claim, filesystem writes or startup.

Provider attachments are independently verified by the privileged provisioning owner.
This module validates shape and bindings, not receipt authenticity or volume freshness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from uuid import UUID

if not __package__:
    package_dir = str(Path(__file__).resolve().parent)
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)

try:
    from . import fixed_five, process_handoff, startup_config
except ImportError:
    import fixed_five
    import process_handoff
    import startup_config

receiver_dir = Path(__file__).resolve().parents[2] / "runtime" / "trigger-receiver"
if not (receiver_dir / "telegram_link.py").is_file():
    receiver_dir = Path("/app/trigger-receiver")
if str(receiver_dir) not in sys.path:
    sys.path.insert(0, str(receiver_dir))
import telegram_link  # noqa: E402 — the existing canonical handshake owner

MAX_REQUEST_BYTES = 64 * 1024
MAX_ATTACHMENT_BYTES = 16 * 1024
MAX_SECRETS_BYTES = 16 * 1024
_SETUP_REQUEST = re.compile(r"[A-Za-z0-9_-]{24,128}\Z")
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_PROVIDER_KEYS = frozenset(
    {
        "projectId",
        "environmentId",
        "serviceId",
        "volumeId",
        "domain",
        "provisionRequest",
        "volumeCreationReceipt",
    }
)


class NativeSetupConfigurationError(ValueError):
    """Public error text never includes input values or credentials."""


def _refuse():
    raise NativeSetupConfigurationError("native setup configuration is invalid")


def _shape(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        _refuse()


def _bounded(value, limit):
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _refuse()
    if len(data) > limit:
        _refuse()


def parse_native_request(raw):
    """Decode a bounded native request. This is not an HTTP endpoint or credential store."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_REQUEST_BYTES:
        _refuse()

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                _refuse()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: _refuse())
    except (ValueError, UnicodeError, RecursionError):
        _refuse()
    _native_fields(value)
    return value


def new_setup_secrets():
    """Generate once; the provisioning owner must persist these before any mutation."""
    return {key: secrets.token_urlsafe(32) for key in ("runtimeBearer", "bridgeToken", "controlToken")}


def validate_telegram_capture(token, provision_request, evidence):
    """Use the existing Telegram handshake owner in source and installed layouts."""
    return telegram_link.validate_native_capture(token, provision_request, evidence)


def _native_fields(value):
    _bounded(value, MAX_REQUEST_BYTES)
    channel = value.get("channel") if isinstance(value, dict) else None
    _shape(
        value,
        (
            "version",
            "channel",
            "tenantId",
            "railway",
            "owner",
            "lineKey",
            "instructions",
            "model",
            "timezone",
            "backgroundUnmetered",
            "telegram" if channel == "telegram" else "photon",
        ),
    )
    if type(value["version"]) is not int or value["version"] != 1 or channel not in ("photon", "telegram"):
        _refuse()
    if type(value["backgroundUnmetered"]) is not bool:
        _refuse()
    _shape(value["railway"], ("projectId", "environmentId", "provisionRequest"))
    _shape(value["owner"], ("accountId",) if channel == "telegram" else ("accountId", "phone"))
    _shape(value[channel], ("botToken",) if channel == "telegram" else ("projectId", "projectSecret", "lineNumber"))
    _shape(value["model"], ("apiKey", "chatModelId", "nativeMediaModelId"))
    text_values = [value[key] for key in ("tenantId", "lineKey", "instructions", "timezone")]
    for group in ("owner", channel, "model"):
        text_values.extend(value[group].values())
    if any(not isinstance(item, str) or not item for item in text_values):
        _refuse()
    for key in ("projectId", "environmentId"):
        _uuid(value["railway"][key])
    marker = value["railway"]["provisionRequest"]
    if not isinstance(marker, str) or not _SETUP_REQUEST.fullmatch(marker):
        _refuse()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
            _refuse()
    except (ValueError, AttributeError):
        _refuse()


def build_launch_envelope(config, state):
    """Return the sole private launch input after exact provider attachments are saved."""
    settings = config["sotto_runtime"]
    channel = config.get("channel", "photon")
    tenant = config["tenant_id"]
    owner = str(config["telegram_link"]["allowed_user"]) if channel == "telegram" else config["owner_phone"]
    service, volume, domain = state["service_id"], state["volume_id"], state["domain"]
    manifest = {
        "version": 1,
        "deployment": "self_host",
        "channel": channel,
        "tenantId": tenant,
        "owner": {"accountId": settings["account_id"], "ownerId": owner},
        "lineKey": settings["line_key"],
        "target": channel,
        "stateDirectory": "/data/node-state",
        "mediaRoot": "/data/node-media",
        "instructions": settings["instructions"],
        "runtime": {"port": 8081, "bearerToken": state["runtime_bearer"]},
        "relay": {"receiverPort": 8080, "rootBearer": state["bridge_token"]},
        "modelDirect": {
            "provider": "google",
            "apiKey": config["gemini_api_key"],
            "chatModelId": settings["chat_model_id"],
            "nativeMediaModelId": settings["native_media_model_id"],
        },
    }
    identity = {
        "channel": channel, "target": channel, "tenantId": tenant,
        "accountId": settings["account_id"], "ownerId": owner, "lineKey": settings["line_key"],
    }
    if channel == "telegram":
        token = config["telegram_link"]["bot_token"]
        manifest.update(providerThreadId=f"telegram:{owner}", telegramLink=dict(config["telegram_link"]),
                        credentials={"telegramToken": token})
        identity.update(providerThreadId=f"telegram:{owner}", botFingerprint=hashlib.sha256(
            ("sotto-telegram-bot-v1:" + token).encode()).hexdigest())
    else:
        line = config["photon_line_number"]
        manifest.update(firstContact={"mode": "owner_dm", "expectedProviderLine": line},
                        photonLineNumber=line, credentials={
                            "mode": "cloud", "photonProjectId": config["photon_project_id"],
                            "photonProjectSecret": config["photon_project_secret"],
                        })
        identity.update(providerThreadId=None, expectedProviderLine=line, projectFingerprint=hashlib.sha256(
            ("sotto-photon-project-v1:" + config["photon_project_id"]).encode()).hexdigest())
    bundle = {
        "version": 1,
        "tenantRoot": "/data",
        "manifest": manifest,
        "receiverAuthority": {
            "SOTTO_MCP_TOKEN": state["bridge_token"],
            "SOTTO_CONTROL_TOKEN": state["control_token"],
            "SOTTO_TENANT_ID": tenant,
            "SOTTO_DATA": "/data",
            "SOTTO_HOME": "/data/sotto-home",
            "SOTTO_VOLUME_ID": volume,
        },
        "stableIdentity": identity,
        "publicReceiverAuthority": {
            "tenantId": tenant,
            "serviceId": service,
            "domain": domain,
            "targetPort": 8080,
        },
        "trustedServiceId": service,
        "receiverOverrides": {
            "SOTTO_TIMEZONE": settings["timezone"],
            "SOTTO_BACKGROUND_UNMETERED": "true" if settings.get("background_unmetered", False) else "false",
        },
    }
    return {
        "version": 1,
        "provider": {
            "projectId": config["project_id"],
            "environmentId": config["environment_id"],
            "serviceId": service,
            "volumeId": volume,
            "domain": domain,
            "provisionRequest": state["request_marker"],
            "volumeCreationReceipt": {"service_id": service, "volume_id": volume},
        },
        "bundle": bundle,
    }


def compile_native(request, attachment, generated_secrets, telegram_capture=None):
    """Return the existing protected Railway launch envelope from typed native inputs.

    `attachment` must come from verified Railway operations for this exact setup.
    `generated_secrets` comes from its protected, durable setup record, never the UI.
    Successful compilation does not authorize a claim, prepare a volume, or start Sotto.
    """
    _native_fields(request)
    link = None
    if request["channel"] == "telegram":
        try:
            link = validate_telegram_capture(request["telegram"]["botToken"],
                                             request["railway"]["provisionRequest"], telegram_capture)
        except (ValueError, TypeError, KeyError):
            _refuse()
    elif telegram_capture is not None:
        _refuse()
    _bounded(attachment, MAX_ATTACHMENT_BYTES)
    _bounded(generated_secrets, MAX_SECRETS_BYTES)
    _shape(attachment, _PROVIDER_KEYS)
    _shape(generated_secrets, ("runtimeBearer", "bridgeToken", "controlToken"))
    for key in ("projectId", "environmentId", "serviceId", "volumeId"):
        _uuid(attachment[key])
    for key in ("projectId", "environmentId", "provisionRequest"):
        if attachment[key] != request["railway"][key]:
            _refuse()
    if attachment["volumeCreationReceipt"] != {
        "service_id": attachment["serviceId"],
        "volume_id": attachment["volumeId"],
    }:
        _refuse()
    if (
        any(
            not isinstance(value, str) or not _SECRET.fullmatch(value) for value in generated_secrets.values()
        )
        or len(set(generated_secrets.values())) != 3
    ):
        _refuse()
    config = {
        "channel": request["channel"],
        "project_id": attachment["projectId"],
        "environment_id": attachment["environmentId"],
        "tenant_id": request["tenantId"],
        "gemini_api_key": request["model"]["apiKey"],
        "sotto_runtime": {
            "account_id": request["owner"]["accountId"],
            "line_key": request["lineKey"],
            "instructions": request["instructions"],
            "timezone": request["timezone"],
            "chat_model_id": request["model"]["chatModelId"],
            "native_media_model_id": request["model"]["nativeMediaModelId"],
            "background_unmetered": request["backgroundUnmetered"],
        },
    }
    if link is not None:
        config["telegram_link"] = link
    else:
        config.update(owner_phone=request["owner"]["phone"],
                      photon_line_number=request["photon"]["lineNumber"],
                      photon_project_id=request["photon"]["projectId"],
                      photon_project_secret=request["photon"]["projectSecret"])
    state = {
        "service_id": attachment["serviceId"],
        "volume_id": attachment["volumeId"],
        "domain": attachment["domain"],
        "request_marker": attachment["provisionRequest"],
        "runtime_bearer": generated_secrets["runtimeBearer"],
        "bridge_token": generated_secrets["bridgeToken"],
        "control_token": generated_secrets["controlToken"],
    }
    envelope = build_launch_envelope(config, state)
    if link is not None:
        envelope["telegramCapture"] = json.loads(json.dumps(telegram_capture))
    bundle = envelope["bundle"]
    try:
        startup_config.render(bundle["manifest"])
        process_handoff.build_child_handoff(
            bundle["manifest"],
            bundle["receiverAuthority"],
            stable_identity=bundle["stableIdentity"],
            node_config_path="/data/node-config/config/runtime-config.json",
            data_directory="/data",
            skills_root="/app/sotto-skills",
            receiver_overrides=bundle["receiverOverrides"],
            public_receiver_authority=bundle["publicReceiverAuthority"],
            trusted_service_id=bundle["trustedServiceId"],
            limited_profile=(fixed_five.TELEGRAM_PROFILE if link is not None
                             else fixed_five.SELF_HOST_PHOTON_PROFILE),
        )
    except (startup_config.ManifestError, process_handoff.HandoffError, ValueError, TypeError, KeyError):
        _refuse()
    return envelope

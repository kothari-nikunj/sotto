"""Current self-host Telegram owner route from a protected link and loaded Node config.

The captured link is installation evidence, not proof of live Bot API connectivity.
No Photon activation receipt or acknowledgement is inferred for Telegram.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TelegramChannelProofUnavailableError(ValueError):
    """Independent receiver and Node channel facts do not agree."""


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TelegramChannelProofUnavailableError('channel verifier unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _startup_module():
    candidates = (Path(__file__).resolve().parents[2] / 'adapters' / 'sotto' / 'startup_config.py',
                  Path('/app/adapters/sotto/startup_config.py'))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise TelegramChannelProofUnavailableError('channel expectation loader unavailable')
    return _module('sotto_telegram_expectation', path)


def _link_module():
    candidates = (Path(__file__).resolve().parents[2] / 'adapters' / 'sotto' / 'runtime_api.py',
                  Path('/app/adapters/sotto/runtime_api.py'))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise TelegramChannelProofUnavailableError('protected Telegram link reader unavailable')
    return _module('sotto_telegram_link_reader', path)


def _expected_config(expectation, policy, link=None):
    if link is None:
        link = _link_module()._read_telegram_link(policy.get('SOTTO_DATA', ''))
    owner = expectation['owner']['ownerId']
    token = link.get('bot_token') if isinstance(link, dict) else None
    linked_owner = link.get('allowed_user') if isinstance(link, dict) else None
    runtime = _link_module()
    if (expectation['channel'] != 'telegram' or expectation['target'] != 'telegram'
            or expectation['route'] != {'mode': 'fixed', 'providerThreadId': f'telegram:{owner}'}
            or not isinstance(owner, str) or not owner.isascii() or not owner.isdigit()
            or type(linked_owner) is not int or str(linked_owner) != owner
            or not runtime._valid_telegram_token(token)
            or expectation['botFingerprint'] != hashlib.sha256(
                ('sotto-telegram-bot-v1:' + token).encode()).hexdigest()
            or policy.get('SOTTO_RUNTIME_PROFILE') != 'self-host-telegram-fixed-five-v1'
            or policy.get('SOTTO_DEPLOYMENT_MODE') != 'self_host'
            or policy.get('SOTTO_CRON_DELIVER') != 'telegram'
            or policy.get('SOTTO_TENANT_ID', '') != expectation['tenantId']
            or policy.get('TELEGRAM_BOT_TOKEN') != token
            or policy.get('TELEGRAM_ALLOWED_USERS') != owner
            or policy.get('TELEGRAM_HOME_CHANNEL') != owner):
        raise TelegramChannelProofUnavailableError('Telegram link or owner policy differs')
    config = {
        'channel': 'telegram', 'owner': expectation['owner'],
        'lineKey': expectation['lineKey'], 'target': 'telegram',
        'port': expectation['port'], 'bearerToken': policy.get('SOTTO_CHAT_RUNTIME_TOKEN'),
        'providerThreadId': expectation['route']['providerThreadId'],
        'credentials': {'telegramToken': token,
                        'telegramLinkFile': {'bot_token': token, 'allowed_user': linked_owner}},
    }
    if expectation['tenantId']:
        config['tenantId'] = expectation['tenantId']
    return config


def _retained_proof():
    """The ONE Python route transcript (`retained_brief_proof.route_digest`), by package path."""
    here = Path(__file__).resolve()
    candidates = (here.parent.parent / 'sotto-skills' / '_shared' / 'lib' / 'retained_brief_proof.py',
                  here.parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib' / 'retained_brief_proof.py')
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise TelegramChannelProofUnavailableError('route digest implementation unavailable')
    return _module('sotto_telegram_route_digest', path)


def _route_digest(expectation):
    """Match Node's public seven-field transcript and validation; Telegram's project field is empty."""
    try:
        return _retained_proof().route_digest(
            'telegram', expectation['tenantId'], '', expectation['owner']['ownerId'],
            expectation['owner']['accountId'], expectation['lineKey'],
            expectation['route']['providerThreadId'])
    except (ValueError, TypeError) as error:
        raise TelegramChannelProofUnavailableError('Telegram route identity is invalid') from error


def check_current_channel(*, chat_client, expected_channel_path, receiver_data_root,
                          receiver_policy, configured_zone_name, expected_zone):
    """Crosscheck receiver projection, captured link, loaded Node identity and zone twice."""
    try:
        startup = _startup_module()
        link_reader = _link_module()
        expectation = startup.read_receiver_channel_expectation(expected_channel_path)
        link = link_reader._read_telegram_link(receiver_data_root)
        config = _expected_config(expectation, receiver_policy, link)
        zone = configured_zone_name()
        if not isinstance(zone, str) or not zone or zone != expected_zone:
            raise TelegramChannelProofUnavailableError('scheduler zone differs')
        ZoneInfo(zone)
        endpoint = f"http://127.0.0.1:{expectation['port']}"
        if (getattr(chat_client, 'url', None) != endpoint
                or receiver_policy.get('SOTTO_CHAT_RUNTIME_URL') != endpoint
                or not isinstance(receiver_policy.get('SOTTO_CHAT_RUNTIME_TOKEN'), str)):
            raise TelegramChannelProofUnavailableError('runtime endpoint differs')
        chat_client.verify_configured_identity(config)
        next_expectation = startup.read_receiver_channel_expectation(expected_channel_path)
        next_link = link_reader._read_telegram_link(receiver_data_root)
        next_config = _expected_config(next_expectation, receiver_policy, next_link)
        chat_client.verify_configured_identity(next_config)
        if (expectation != next_expectation or link != next_link or config != next_config
                or configured_zone_name() != zone):
            raise TelegramChannelProofUnavailableError('Telegram channel changed during check')
        return {'version': 1, 'status': 'current_channel_proved',
                'action_authorized': False, 'route_digest': _route_digest(expectation),
                'bot_fingerprint': expectation['botFingerprint'],
                'provider_thread_id': expectation['route']['providerThreadId'],
                'schedule_zone': zone, 'tenant_id': expectation['tenantId'],
                'owner_id': expectation['owner']['ownerId'],
                'account_id': expectation['owner']['accountId'],
                'line_key': expectation['lineKey']}
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, UnicodeError,
            ZoneInfoNotFoundError, AttributeError) as error:
        if isinstance(error, TelegramChannelProofUnavailableError):
            raise
        raise TelegramChannelProofUnavailableError('Telegram channel proof unavailable') from error

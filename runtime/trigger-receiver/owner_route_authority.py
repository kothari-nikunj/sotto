"""Current owner identity and route checks shared by admitted private surfaces."""
import hashlib

_THREAD_MAX_CHARS = 512
from pathlib import Path


class UnavailableError(Exception):
    pass


def validate_authority(identity, policy, data_root):
    """Shared current deployment and owner identity gate for private reminder surfaces."""
    selfhost_photon = (identity['channel'] == 'photon'
                       and policy.get('SOTTO_DEPLOYMENT_MODE') == 'self_host'
                       and policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-photon-fixed-five-v1'
                       and bool(policy.get('GOOGLE_AI_API_KEY'))
                       and 'SOTTO_MODEL_PROXY_URL' not in policy
                       and 'SOTTO_MODEL_PROXY_TOKEN' not in policy)
    selfhost_telegram = (identity['channel'] == 'telegram'
                         and policy.get('SOTTO_DEPLOYMENT_MODE') == 'self_host'
                         and policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-telegram-fixed-five-v1'
                         and bool(policy.get('GOOGLE_AI_API_KEY'))
                         and 'SOTTO_MODEL_PROXY_URL' not in policy
                         and 'SOTTO_MODEL_PROXY_TOKEN' not in policy)
    if (not (policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
                    or selfhost_photon or selfhost_telegram)
            or identity['tenant_id'] != policy.get('SOTTO_TENANT_ID')
            or str(data_root) != policy.get('SOTTO_DATA')):
        raise UnavailableError('owner reminder authority unavailable')
    if identity['channel'] == 'telegram':
        token = policy.get('TELEGRAM_BOT_TOKEN', '')
        if (not selfhost_telegram or policy.get('SOTTO_CRON_DELIVER') != 'telegram'
                or identity['owner_id'] != policy.get('TELEGRAM_HOME_CHANNEL')
                or identity['owner_id'] != policy.get('TELEGRAM_ALLOWED_USERS')
                or not isinstance(token, str)
                or identity['bot_fingerprint'] != hashlib.sha256(
                    ('sotto-telegram-bot-v1:' + token).encode()).hexdigest()):
            raise UnavailableError('owner reminder authority unavailable')
    elif (policy.get('SOTTO_CRON_DELIVER') != 'photon'
          or identity['owner_id'] != policy.get('PHOTON_HOME_CHANNEL')
          or identity['owner_id'] != policy.get('PHOTON_ALLOWED_USERS')
          or identity['project_fingerprint'] != hashlib.sha256(
              ('sotto-photon-project-v1:' + policy.get('PHOTON_PROJECT_ID', '')).encode()).hexdigest()):
        raise UnavailableError('owner reminder authority unavailable')


def validate_route(thread, identity, policy, data_root, activation,  # noqa: PLR0913, PLR0917 -- preserved public route API
                   telegram_verifier=None, chat_client=None):
    """Prove the currently activated owner route without changing command semantics."""
    if identity['channel'] == 'telegram':
        if (type(thread) is not str or thread != f"telegram:{identity['owner_id']}"
                or identity['route_mode'] != 'fixed' or identity['route_id'] != thread
                or telegram_verifier is None or chat_client is None):
            raise UnavailableError('owner reminder route unavailable')
        try:
            zone = policy['_configured_tz_name']()
            proof = telegram_verifier.check_current_channel(
                chat_client=chat_client,
                expected_channel_path=Path(data_root) / 'runtime/expected-channel.json',
                receiver_data_root=data_root, receiver_policy=policy,
                configured_zone_name=policy['_configured_tz_name'], expected_zone=zone)
        except Exception as error:
            raise UnavailableError('owner reminder route unavailable') from error
        if (proof.get('status') != 'current_channel_proved'
                or proof.get('action_authorized') is not False
                or proof.get('provider_thread_id') != thread
                or proof.get('bot_fingerprint') != identity['bot_fingerprint']
                or proof.get('tenant_id') != identity['tenant_id']
                or proof.get('owner_id') != identity['owner_id']
                or proof.get('account_id') != identity['account_id']
                or proof.get('line_key') != identity['line_key']):
            raise UnavailableError('owner reminder route changed')
        return
    if identity['route_mode'] == 'first_contact_shared_project':
        if (identity['route_id'] != 'shared_project' or chat_client is None
                or type(thread) is not str or not thread.startswith('imessage:')
                or len(thread) > _THREAD_MAX_CHARS or thread.count('~') != 1
                or not thread.endswith('~shared') or ';-;' not in thread[:-7]):
            raise UnavailableError('shared-project route unavailable')
        import runtime_channel_proof
        try:
            zone = policy['_configured_tz_name']()
            proof = runtime_channel_proof.check_current_channel(
                chat_client=chat_client,
                expected_channel_path=Path(data_root) / 'runtime/expected-channel.json',
                receiver_data_root=data_root, receiver_policy=policy,
                configured_zone_name=policy['_configured_tz_name'], expected_zone=zone)
        except Exception as error:
            raise UnavailableError('shared-project route unavailable') from error
        if (proof.get('status') != 'current_channel_proved'
                or proof.get('action_authorized') is not False
                or proof.get('provider_thread_id') != thread
                or any(proof.get(key) != identity[key] for key in
                       ('tenant_id', 'project_fingerprint', 'owner_id', 'account_id', 'line_key'))):
            raise UnavailableError('shared-project route changed')
        return
    if type(thread) is not str or not thread.startswith('imessage:') or len(thread) > _THREAD_MAX_CHARS:
        raise ValueError('owner reminder request is invalid')
    guid, separator, line = thread[len('imessage:'):].partition('~')
    if (not separator or not guid or not line or '~' in line
            or line != policy.get('SOTTO_IMESSAGE_NUMBER')):
        raise UnavailableError('owner reminder route unavailable')
    route = activation.verified_current_photon_route(data_root, policy)
    if (route['chat_id'] != guid
            or identity['route_mode'] == 'fixed' and identity['route_id'] != thread
            or identity['route_mode'] == 'first_contact' and identity['route_id'] != line):
        raise UnavailableError('owner reminder route changed')



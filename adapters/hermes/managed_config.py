"""Reconcile the managed runtime's credentials and platform/model configuration."""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml


def reconcile(home, env):
    base = env['SOTTO_MODEL_PROXY_URL'].rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Managed model proxy must be a credential-free HTTPS URL')
    owner = env['PHOTON_HOME_CHANNEL']
    if env['PHOTON_ALLOWED_USERS'] != owner:
        raise ValueError('Managed Photon allowlist must contain only the tenant owner')
    token = env['SOTTO_MODEL_PROXY_TOKEN']
    home = Path(home)
    config_path = home / 'config.yaml'
    cfg = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    cfg = cfg or {}
    model = {'default': 'gemini-3.8-flash', 'provider': 'custom',
             'base_url': base + '/openai/v1', 'api_key': token}
    cfg['model'] = model
    # Match the established Sotto Telegram runtime's conversational behavior.
    cfg.setdefault('agent', {}).update({'name': 'Sotto', 'reasoning_effort': 'medium', 'max_turns': 60})
    cfg['fallback_model'] = []
    cfg['fallback_providers'] = []
    cfg['custom_providers'] = []
    for task in ('vision', 'web_extract', 'tts_audio_tags', 'session_search',
                 'compression', 'title_generation', 'approval', 'skills_hub', 'mcp', 'triage_specifier'):
        cfg.setdefault('auxiliary', {})[task] = {'provider': 'main', 'model': ''}
    cfg.setdefault('compression', {}).update({'provider': 'main', 'model': '',
                                            'base_url': model['base_url'], 'api_key': token})
    cfg.setdefault('delegation', {}).update({'model': model['default'], 'provider': 'custom',
                                           'base_url': model['base_url'], 'api_key': token})
    # The execute_code kernel scrubs undeclared variables. Pass the tenant-scoped
    # model route and Sotto state/policy through, never Bridge/setup/provider roots.
    cfg.setdefault('terminal', {})['env_passthrough'] = [
        'SOTTO_DATA', 'SOTTO_DEPLOYMENT_MODE', 'SOTTO_TENANT_ID',
        'SOTTO_MODEL_PROXY_URL', 'SOTTO_MODEL_PROXY_TOKEN', 'SOTTO_TIMEZONE',
        'SOTTO_USER_EMAIL', 'SOTTO_UNATTENDED', 'SOTTO_DELIVERY_RUN_ID',
    ]
    cfg['plugins'] = {'enabled': ['photon-platform'], 'disabled': ['whatsapp-platform']}
    for platform, settings in cfg.get('platforms', {}).items():
        if isinstance(settings, dict):
            settings['enabled'] = platform == 'photon'
    temporary = config_path.with_suffix('.tmp')
    temporary.write_text(yaml.safe_dump(cfg, sort_keys=False))
    temporary.chmod(0o600)
    temporary.replace(config_path)
    # A dedicated managed Sotto uses the same Sotto persona block as Telegram,
    # without the fresh Hermes installation's competing default identity.
    persona = Path(__file__).with_name('sotto-persona.md').read_text()
    soul = home / 'SOUL.md'
    temporary = soul.with_suffix('.tmp')
    temporary.write_text(persona)
    temporary.chmod(0o600)
    temporary.replace(soul)
    # A previously seeded self-host .env must not restore root keys or channels
    # after the launcher has removed them from its own process environment.
    envfile = home / '.env'
    lines = envfile.read_text().splitlines() if envfile.exists() else []
    root_keys = {'GOOGLE_AI_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
                 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY',
                 'SOTTO_FALLBACK_API_KEY', 'GATEWAY_ALLOW_ALL_USERS', 'PHOTON_ALLOW_ALL_USERS',
                 'PHOTON_STREAM_SILENCE_PROBE_MS', 'SOTTO_CONTROL_TOKEN'}
    channels = ('WHATSAPP_', 'TELEGRAM_', 'DISCORD_', 'SLACK_', 'SIGNAL_', 'BLUEBUBBLES_')
    lines = [line for line in lines if line.split('=', 1)[0] not in root_keys
             and not line.startswith(channels)]
    envfile.write_text('\n'.join(lines) + '\n')
    envfile.chmod(0o600)


if __name__ == '__main__':
    reconcile(sys.argv[1], os.environ)
    print('[sotto] managed Photon and tenant model routes reconciled')

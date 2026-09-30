#!/usr/bin/env python3
"""Check the checked-in Railway Photon preset contract without reading credentials."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRESET = ROOT / 'deploy' / 'railway-photon-preset.json'
EXPECTED_REQUIRED = {
    'BRIDGE_TOKEN', 'GOOGLE_AI_API_KEY', 'SOTTO_CRON_DELIVER',
    'PHOTON_PROJECT_ID', 'PHOTON_PROJECT_SECRET',
    'PHOTON_HOME_CHANNEL', 'PHOTON_ALLOWED_USERS',
}


def validate(preset):
    errors = []
    if preset.get('source', {}).get('rootDirectory') is not None:
        errors.append('public standalone source rootDirectory must be null (blank in Railway)')
    if preset.get('service', {}).get('volumeMountPath') != '/data':
        errors.append('service volumeMountPath must be /data')
    if preset.get('service', {}).get('healthcheckPath') != '/health':
        errors.append('service healthcheckPath must be /health')
    if not str(preset.get('service', {}).get('port', '')).startswith('8080'):
        errors.append('service port must match the helper: the domain targets 8080')
    variables = preset.get('variables', {})
    if set(variables) != EXPECTED_REQUIRED:
        errors.append('variables must exactly match the required self-host Photon contract')
    if variables.get('SOTTO_CRON_DELIVER', {}).get('value') != 'photon':
        errors.append('SOTTO_CRON_DELIVER must be fixed to photon')
    if variables.get('PHOTON_ALLOWED_USERS', {}).get('mustEqual') != 'PHOTON_HOME_CHANNEL':
        errors.append('Photon allowlist must be exactly the configured owner identity')
    if 'E.164' not in variables.get('PHOTON_HOME_CHANNEL', {}).get('description', ''):
        errors.append('PHOTON_HOME_CHANNEL must be described as an exact E.164 phone number')
    if variables.get('BRIDGE_TOKEN', {}).get('templateValue') != '${{secret(48)}}':
        errors.append('BRIDGE_TOKEN must use Railway secret(48) template function syntax')
    if not {'GEMINI_API_KEY', 'GOOGLE_API_KEY'} <= set(
        variables.get('GOOGLE_AI_API_KEY', {}).get('aliasesAcceptedByBoot', [])
    ):
        errors.append('Gemini boot aliases are incomplete')
    if 'SOTTO_DEPLOYMENT_MODE' not in preset.get('mustNotSet', []):
        errors.append('self-host preset must leave SOTTO_DEPLOYMENT_MODE unset')
    if 'TELEGRAM_BOT_TOKEN' not in preset.get('mustNotSet', []):
        errors.append('Photon preset must not require a Telegram token')
    return errors


def main():
    errors = validate(json.loads(PRESET.read_text()))
    if errors:
        for error in errors:
            print(f'FAIL: {error}')
        return 1
    print('OK: Railway Photon preset contract is internally consistent; no credentials read.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

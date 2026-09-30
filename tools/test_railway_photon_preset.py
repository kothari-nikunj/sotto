import json

from verify_railway_photon_preset import PRESET, validate


def test_checked_in_photon_preset_matches_self_host_contract():
    assert validate(json.loads(PRESET.read_text())) == []


def test_rejects_open_photon_allowlist():
    preset = json.loads(PRESET.read_text())
    preset['variables']['PHOTON_ALLOWED_USERS']['mustEqual'] = '*'
    assert any('allowlist' in issue for issue in validate(preset))


def test_rejects_telegram_channel_or_wrong_volume():
    preset = json.loads(PRESET.read_text())
    preset['variables']['SOTTO_CRON_DELIVER']['value'] = 'telegram'
    preset['service']['volumeMountPath'] = '/app/data'
    errors = validate(preset)
    assert any('photon' in issue for issue in errors)
    assert any('/data' in issue for issue in errors)


def test_rejects_a_port_the_helper_does_not_use_or_forced_deployment_mode():
    preset = json.loads(PRESET.read_text())
    preset['service']['port'] = 'Railway-assigned'
    preset['mustNotSet'].remove('SOTTO_DEPLOYMENT_MODE')
    errors = validate(preset)
    assert any('8080' in issue for issue in errors)
    assert any('SOTTO_DEPLOYMENT_MODE' in issue for issue in errors)


def test_rejects_an_owner_identity_that_is_not_exact_e164():
    preset = json.loads(PRESET.read_text())
    preset['variables']['PHOTON_HOME_CHANNEL']['description'] = 'Owner identity, phone or email'
    assert any('E.164' in issue for issue in validate(preset))

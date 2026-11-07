import json
from pathlib import Path
import tempfile
import unittest

from bundle_railway_oauth import bundle, validate


class RailwayOAuthBundleTests(unittest.TestCase):
    def descriptor(self, **updates):
        return json.dumps(dict(version=1, clientID='registered-native-fixture',
                               redirectURI='ai.sotto.bridge://railway/oauth', **updates)).encode()

    def test_public_configuration_only(self):
        value = validate(self.descriptor())
        self.assertEqual(value['version'], 1)
        for raw in (self.descriptor(client_secret='must-never-ship'),
                    self.descriptor().replace(b'railway/oauth', b'other/oauth'),
                    self.descriptor().replace(b'"version": 1', b'"version": true'),
                    b'{"version":1,"version":1}'):
            with self.assertRaises(ValueError):
                validate(raw)

    def test_sign_only_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'
            target = Path(directory) / 'railway-oauth.json'
            source.write_bytes(self.descriptor())
            bundle(source, target)
            bundle(source, target)
            source.write_bytes(source.read_bytes().replace(b'registered-native-fixture', b'another-registered-client'))
            with self.assertRaises(ValueError):
                bundle(source, target)
            self.assertEqual(validate(target.read_bytes())['clientID'], 'registered-native-fixture')

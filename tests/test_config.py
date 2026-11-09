import json
import configparser
from pathlib import Path
import tempfile
import unittest

from ghbackup.common import Config


class ConfigTests(unittest.TestCase):
    def test_single_private_config_redacts_recovery_and_repr(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "config.toml"
            values = {
                "source_owner": "alice",
                "destination_owner": "bob",
                "data_dir": str(Path(t) / "data"),
                "source_token": "source-test-secret",
                "destination_token": "destination-test-secret",
                "drive_client_id": "client-test-id",
                "drive_client_secret": "client-test-secret",
                "drive_refresh_token": "refresh-test-secret",
                "telegram_token": "telegram-test-secret",
                "telegram_user_id": 42,
            }
            path.write_text(
                "\n".join(
                    f"{key} = {json.dumps(value)}" for key, value in values.items()
                )
            )
            path.chmod(0o600)
            config = Config.load(path)
            public = json.dumps(config.public_values())
            for key in [
                "source_token",
                "destination_token",
                "drive_client_id",
                "drive_client_secret",
                "drive_refresh_token",
                "telegram_token",
            ]:
                self.assertNotIn(values[key], public)
                self.assertNotIn(values[key], repr(config))
            self.assertEqual(config.public_values()["telegram_user_id"], 0)

    def test_rclone_cache_is_generated_and_reused(self):
        with tempfile.TemporaryDirectory() as t:
            config = Config(
                "alice",
                "bob",
                data_dir=t,
                drive_client_id="client",
                drive_client_secret="secret",
                drive_refresh_token="refresh",
            )
            path = config.rclone_config()
            stamp = path.stat().st_mtime_ns
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path)
            token = json.loads(parser['gdrive']['token'])
            self.assertTrue(token['access_token'])
            self.assertEqual(token['refresh_token'], 'refresh')
            self.assertEqual(config.rclone_config().stat().st_mtime_ns, stamp)

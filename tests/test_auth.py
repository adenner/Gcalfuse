import stat
from unittest.mock import MagicMock, patch

from gcalfuse import auth
from gcalfuse.config import Config


def test_run_auth_flow_writes_token_with_mode_600(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "credentials.json").write_text("{}")

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = '{"token": "fake"}'
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds

    config = Config()
    with (
        patch("gcalfuse.config.CONFIG_DIR", config_dir),
        patch(
            "gcalfuse.auth.InstalledAppFlow.from_client_secrets_file", return_value=fake_flow
        ),
    ):
        auth.run_auth_flow(config)
        token_path = config.token_path

    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600

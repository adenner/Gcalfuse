"""auth.py: token file permissions and clear errors for every failure mode."""

import json
import stat
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError, TransportError

from gcalfuse import auth
from gcalfuse.auth import AuthError
from gcalfuse.config import Config


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr("gcalfuse.config.CONFIG_DIR", tmp_path)
    return Config()


@pytest.fixture
def fake_flow(monkeypatch):
    creds = MagicMock()
    creds.to_json.return_value = '{"token": "fake"}'
    flow = MagicMock()
    flow.run_local_server.return_value = creds
    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: flow)
    return flow


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def write_token(config, expiry="2000-01-01T00:00:00Z", refresh_token="r"):
    config.token_path.write_text(
        json.dumps(
            {
                "token": "t",
                "refresh_token": refresh_token,
                "client_id": "id",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "expiry": expiry,
            }
        )
    )


def test_auth_flow_writes_token_with_mode_600(config, fake_flow):
    config.credentials_path.write_text("{}")
    auth.run_auth_flow(config)
    assert mode(config.token_path) == 0o600


def test_auth_flow_tightens_a_pre_existing_world_readable_token(config, fake_flow):
    config.credentials_path.write_text("{}")
    config.token_path.write_text("old")
    config.token_path.chmod(0o644)
    auth.run_auth_flow(config)
    assert mode(config.token_path) == 0o600
    assert config.token_path.read_text() == '{"token": "fake"}'


def test_auth_flow_no_browser_is_passed_through(config, fake_flow):
    config.credentials_path.write_text("{}")
    auth.run_auth_flow(config, open_browser=False)
    assert fake_flow.run_local_server.call_args.kwargs["open_browser"] is False


def test_auth_flow_requests_events_only_scope():
    assert auth.SCOPES == ["https://www.googleapis.com/auth/calendar.events"]


def test_missing_credentials_json_gives_setup_hint(config):
    with pytest.raises(AuthError, match="Desktop app"):
        auth.run_auth_flow(config)


def test_invalid_credentials_json_is_explained(config):
    config.credentials_path.write_text('{"not": "a client file"}')
    with pytest.raises(AuthError, match="not a valid OAuth client file"):
        auth.run_auth_flow(config)


def test_load_without_token_says_run_auth(config):
    with pytest.raises(AuthError, match="gcalfuse auth"):
        auth.load_credentials(config)


def test_load_corrupt_token_says_run_auth(config):
    config.token_path.write_text("{not json")
    with pytest.raises(AuthError, match="unreadable"):
        auth.load_credentials(config)


def test_load_valid_token_needs_no_refresh(config):
    write_token(config, expiry="2999-01-01T00:00:00Z")
    assert auth.load_credentials(config).token == "t"


def test_revoked_token_says_run_auth(config, monkeypatch):
    write_token(config)
    monkeypatch.setattr(
        auth.Credentials, "refresh", MagicMock(side_effect=RefreshError("invalid_grant"))
    )
    with pytest.raises(AuthError, match="revoked"):
        auth.load_credentials(config)


def test_offline_refresh_mentions_network(config, monkeypatch):
    write_token(config)
    monkeypatch.setattr(
        auth.Credentials, "refresh", MagicMock(side_effect=TransportError("no route"))
    )
    with pytest.raises(AuthError, match="network"):
        auth.load_credentials(config)


def test_successful_refresh_rewrites_token_privately(config, monkeypatch):
    write_token(config)
    config.token_path.chmod(0o644)

    def refresh(self, request):
        self.token = "new-token"
        self.expiry = None

    monkeypatch.setattr(auth.Credentials, "refresh", refresh)
    auth.load_credentials(config)
    assert json.loads(config.token_path.read_text())["token"] == "new-token"
    assert mode(config.token_path) == 0o600


def test_expired_token_without_refresh_token_is_invalid(config):
    write_token(config, refresh_token=None)
    with pytest.raises(AuthError, match="invalid"):
        auth.load_credentials(config)

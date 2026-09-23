"""Google OAuth installed-app flow for gcalfuse."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Config

logger = logging.getLogger(__name__)

# Events-only scope, not full calendar ACL.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

CREDENTIALS_HINT = (
    "Missing {path}.\n\n"
    "To fix this:\n"
    "  1. Create (or pick) a Google Cloud project.\n"
    "  2. Enable the Google Calendar API for it.\n"
    "  3. Create an OAuth client ID of type 'Desktop app'.\n"
    "  4. Download its JSON and save it to {path}.\n"
    "  5. Run `gcalfuse auth`.\n"
    "See the 'Google Cloud setup' section of the README for details.\n"
)


class AuthError(RuntimeError):
    """credentials.json or the saved token is missing, corrupt, revoked, or unrefreshable."""


def _write_token(token_path: Path, creds: Credentials) -> None:
    """Persist a token readable only by its owner.

    The file is created 0600 rather than chmod'ed afterwards, so there's no
    window where a live credential is world-readable. The chmod still runs
    to tighten a token file left behind by an older version.
    """
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(token_path, 0o600)


def run_auth_flow(config: Config, open_browser: bool = True) -> Credentials:
    """Run the installed-app OAuth flow and persist the resulting token to disk."""
    if not config.credentials_path.exists():
        raise AuthError(CREDENTIALS_HINT.format(path=config.credentials_path))

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(config.credentials_path), SCOPES)
    except ValueError as exc:
        raise AuthError(
            f"{config.credentials_path} is not a valid OAuth client file ({exc}). "
            "Download it again as a 'Desktop app' OAuth client."
        ) from exc
    creds = flow.run_local_server(port=0, open_browser=open_browser)

    _write_token(config.token_path, creds)
    logger.info("saved OAuth token to %s", config.token_path)
    return creds


def load_credentials(config: Config) -> Credentials:
    """Load a previously saved token, refreshing it if expired.

    Raises AuthError if `gcalfuse auth` has never been run
    successfully, the token file is corrupt, or the token was revoked.
    """
    if not config.token_path.exists():
        raise AuthError(f"No saved token at {config.token_path}. Run `gcalfuse auth` first.")

    try:
        creds = Credentials.from_authorized_user_file(str(config.token_path), SCOPES)
    except ValueError as exc:
        raise AuthError(
            f"Saved token at {config.token_path} is unreadable ({exc}). Run `gcalfuse auth` again."
        ) from exc
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthError(
                f"Google refused to refresh the saved token ({exc}); it was probably "
                "revoked or expired. Run `gcalfuse auth` again."
            ) from exc
        except TransportError as exc:
            raise AuthError(
                f"Could not reach Google to refresh the saved token ({exc}). "
                "Check your network connection."
            ) from exc
        _write_token(config.token_path, creds)
        logger.info("refreshed OAuth token")
        return creds

    raise AuthError(f"Saved token at {config.token_path} is invalid. Run `gcalfuse auth` again.")

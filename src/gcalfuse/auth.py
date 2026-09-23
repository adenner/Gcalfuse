"""Google OAuth installed-app flow for gcalfuse."""

from __future__ import annotations

import logging

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
)


class MissingCredentialsError(RuntimeError):
    """Raised when credentials.json or a saved token is missing or unusable."""


def run_auth_flow(config: Config) -> Credentials:
    """Run the installed-app OAuth flow and persist the resulting token to disk."""
    if not config.credentials_path.exists():
        raise MissingCredentialsError(CREDENTIALS_HINT.format(path=config.credentials_path))

    flow = InstalledAppFlow.from_client_secrets_file(str(config.credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)

    config.token_path.parent.mkdir(parents=True, exist_ok=True)
    config.token_path.write_text(creds.to_json())
    logger.info("saved OAuth token to %s", config.token_path)
    return creds


def load_credentials(config: Config) -> Credentials:
    """Load a previously saved token, refreshing it if expired.

    Raises MissingCredentialsError if `gcalfuse auth` has never been run
    successfully, or the saved token can no longer be refreshed.
    """
    if not config.token_path.exists():
        raise MissingCredentialsError(
            f"No saved token at {config.token_path}. Run `gcalfuse auth` first."
        )

    creds = Credentials.from_authorized_user_file(str(config.token_path), SCOPES)
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        config.token_path.write_text(creds.to_json())
        logger.info("refreshed OAuth token")
        return creds

    raise MissingCredentialsError(
        f"Saved token at {config.token_path} is invalid. Run `gcalfuse auth` again."
    )

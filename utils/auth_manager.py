"""
Google Ads Authentication Manager

Enhanced authentication handling with:
- Automatic token refresh
- Token validation
- Multi-account session management
- Service account support
- Credential encryption (optional)
"""

import json
import logging
import os
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

from utils.request_context import Tenant, get_tenant

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Custom exception for authentication errors."""
    pass


class TokenManager:
    """Manages OAuth tokens with automatic refresh."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_uri: str = "https://oauth2.googleapis.com/token"
    ):
        """
        Initialize token manager.

        Args:
            client_id: OAuth2 client ID
            client_secret: OAuth2 client secret
            refresh_token: OAuth2 refresh token
            token_uri: Token endpoint URI
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.token_uri = token_uri

        self._credentials: Optional[Credentials] = None
        self._last_refresh: Optional[datetime] = None

    def get_credentials(self, force_refresh: bool = False) -> Credentials:
        """
        Get valid credentials, refreshing if necessary.

        Args:
            force_refresh: Force token refresh even if not expired

        Returns:
            Valid OAuth2 credentials

        Raises:
            AuthenticationError: If token refresh fails
        """
        # Initialize credentials if not done
        if self._credentials is None:
            self._credentials = Credentials(
                token=None,
                refresh_token=self.refresh_token,
                token_uri=self.token_uri,
                client_id=self.client_id,
                client_secret=self.client_secret
            )

        # Check if refresh is needed
        needs_refresh = (
            force_refresh or
            not self._credentials.valid or
            self._credentials.expired or
            self._last_refresh is None
        )

        if needs_refresh:
            try:
                self._credentials.refresh(Request())
                self._last_refresh = datetime.now()
                logger.info("OAuth token refreshed successfully")
            except RefreshError as e:
                logger.error(f"Token refresh failed: {e}")
                raise AuthenticationError(
                    f"Failed to refresh OAuth token: {e}. "
                    "Your refresh token may have expired. Please regenerate it."
                )

        return self._credentials

    def validate_token(self) -> bool:
        """
        Validate that the refresh token works.

        Returns:
            True if token is valid, False otherwise
        """
        try:
            self.get_credentials(force_refresh=True)
            return True
        except AuthenticationError:
            return False


class GoogleAdsAuthManager:
    """
    Manages Google Ads API authentication with enhanced features.

    Features:
    - Automatic token refresh
    - Multiple account sessions
    - Token validation
    - Service account support
    """

    def __init__(self):
        """Initialize the authentication manager."""
        self._clients: Dict[str, GoogleAdsClient] = {}
        self._token_managers: Dict[str, TokenManager] = {}
        self._current_client_key: Optional[str] = None
        # Guards the lazy per-tenant init in _client_for_tenant. Two concurrent requests
        # for the same unseen tenant would otherwise both build a client, and both pay
        # the token round trip in validate_token().
        self._tenant_lock = threading.Lock()

    def initialize_oauth(
        self,
        developer_token: Optional[str],
        client_id: str,
        client_secret: str,
        refresh_token: str,
        login_customer_id: Optional[str] = None,
        client_key: str = "default",
        make_current: bool = True
    ) -> str:
        """
        Initialize Google Ads client with OAuth2.

        Args:
            developer_token: Google Ads developer token. Optional since the
                2026-09-09 sunset; sent when present, ignored by the API.
            client_id: OAuth2 client ID
            client_secret: OAuth2 client secret
            refresh_token: OAuth2 refresh token
            login_customer_id: Optional MCC account ID
            client_key: Unique identifier for this client session
            make_current: Point the process-wide "current client" at this one. False for
                per-request tenants served over HTTP, where the current client is a
                ContextVar and this global would only be a way to cross tenants over.

        Returns:
            Client key for this session

        Raises:
            AuthenticationError: If initialization fails
        """
        try:
            # Create token manager
            token_manager = TokenManager(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token
            )

            # Validate token
            if not token_manager.validate_token():
                raise AuthenticationError("Invalid refresh token")

            # Build credentials dict
            credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "use_proto_plus": True
            }

            # Developer tokens were sunset on 2026-09-09: the header is accepted but
            # ignored, and API access level is now granted to the Google Cloud project
            # that issued the OAuth client. Still sent when we have one, because older
            # library versions require the field, but no longer required to be present.
            if developer_token:
                credentials["developer_token"] = developer_token

            if login_customer_id:
                credentials["login_customer_id"] = login_customer_id

            # Create client
            client = GoogleAdsClient.load_from_dict(credentials)

            # Store client and token manager
            self._clients[client_key] = client
            self._token_managers[client_key] = token_manager
            if make_current:
                self._current_client_key = client_key

            logger.info(f"Google Ads client initialized: {client_key}")

            return client_key

        except Exception as e:
            logger.error(f"Failed to initialize OAuth client: {e}")
            raise AuthenticationError(f"OAuth initialization failed: {e}")

    def initialize_service_account(
        self,
        developer_token: str,
        json_key_file_path: str,
        login_customer_id: Optional[str] = None,
        client_key: str = "service_account"
    ) -> str:
        """
        Initialize Google Ads client with service account.

        Args:
            developer_token: Google Ads developer token
            json_key_file_path: Path to service account JSON key file
            login_customer_id: Optional MCC account ID
            client_key: Unique identifier for this client session

        Returns:
            Client key for this session

        Raises:
            AuthenticationError: If initialization fails
        """
        try:
            # Verify key file exists
            key_file = Path(json_key_file_path)
            if not key_file.exists():
                raise AuthenticationError(f"Service account key file not found: {json_key_file_path}")

            # Build credentials dict
            credentials = {
                "developer_token": developer_token,
                "json_key_file_path": json_key_file_path,
                "use_proto_plus": True
            }

            if login_customer_id:
                credentials["login_customer_id"] = login_customer_id

            # Create client
            client = GoogleAdsClient.load_from_dict(credentials)

            # Store client
            self._clients[client_key] = client
            self._current_client_key = client_key

            logger.info(f"Google Ads service account client initialized: {client_key}")

            return client_key

        except Exception as e:
            logger.error(f"Failed to initialize service account client: {e}")
            raise AuthenticationError(f"Service account initialization failed: {e}")

    def _client_for_tenant(self, tenant: Tenant) -> GoogleAdsClient:
        """Get (or lazily build) the Google Ads client for one revspot client.

        Built once per tenant and cached: initialize_oauth validates the refresh token by
        actually refreshing it, so building per request would put a round trip to Google's
        token endpoint in front of every tool call.
        """
        client = self._clients.get(tenant.client_key)
        if client is not None:
            return client

        with self._tenant_lock:
            # Re-check: another request may have built it while we waited.
            client = self._clients.get(tenant.client_key)
            if client is not None:
                return client

            client_id = os.getenv("GOOGLE_ADS_CLIENT_ID")
            client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
            if not (client_id and client_secret):
                raise AuthenticationError(
                    "GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET must be set to "
                    "serve per-request tenants."
                )

            login_customer_id = (
                tenant.login_customer_id or os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
            )
            if login_customer_id:
                login_customer_id = login_customer_id.replace("-", "")

            self.initialize_oauth(
                developer_token=os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=tenant.refresh_token,
                login_customer_id=login_customer_id,
                client_key=tenant.client_key,
                # Never touch the process-wide current client: it is shared by every
                # request, and pointing it at this tenant is exactly the cross-tenant
                # bug this whole path exists to avoid.
                make_current=False,
            )
            return self._clients[tenant.client_key]

    def get_client(self, client_key: Optional[str] = None) -> GoogleAdsClient:
        """
        Get Google Ads client for the specified key.

        Args:
            client_key: Client key. None means "whoever this request is acting as" —
                the request's tenant if there is one, else the process-wide current
                client (the stdio case, where there is only ever one user).

        Returns:
            Google Ads client

        Raises:
            AuthenticationError: If client not found
        """
        # Every one of the 139 tools calls get_client() with no argument, so this is the
        # single place a request's identity has to be resolved. Tenant first: under HTTP
        # the global below belongs to no one in particular.
        if client_key is None:
            tenant = get_tenant()
            if tenant is not None:
                return self._client_for_tenant(tenant)

        key = client_key or self._current_client_key

        if key is None:
            raise AuthenticationError(
                "No Google Ads client initialized. Call initialize_oauth() or "
                "initialize_service_account() first."
            )

        if key not in self._clients:
            raise AuthenticationError(f"Client not found: {key}")

        return self._clients[key]

    def switch_client(self, client_key: str) -> None:
        """
        Switch to a different client session.

        Args:
            client_key: Key of client to switch to

        Raises:
            AuthenticationError: If client not found
        """
        if client_key not in self._clients:
            raise AuthenticationError(f"Client not found: {client_key}")

        self._current_client_key = client_key
        logger.info(f"Switched to client: {client_key}")

    def list_clients(self) -> Dict[str, Dict[str, Any]]:
        """
        List all initialized clients.

        Returns:
            Dictionary of client keys and their metadata
        """
        clients_info = {}

        for key in self._clients:
            clients_info[key] = {
                "key": key,
                "is_current": key == self._current_client_key,
                "has_token_manager": key in self._token_managers
            }

        return clients_info

    def refresh_token(self, client_key: Optional[str] = None) -> None:
        """
        Manually refresh OAuth token for a client.

        Args:
            client_key: Client key (uses current if None)

        Raises:
            AuthenticationError: If client doesn't have token manager
        """
        key = client_key or self._current_client_key

        if key not in self._token_managers:
            raise AuthenticationError(
                f"Client {key} doesn't use OAuth (no token manager)"
            )

        self._token_managers[key].get_credentials(force_refresh=True)
        logger.info(f"Token refreshed for client: {key}")

    def validate_credentials(self, client_key: Optional[str] = None) -> bool:
        """
        Validate credentials for a client.

        Args:
            client_key: Client key (uses current if None)

        Returns:
            True if credentials are valid
        """
        try:
            client = self.get_client(client_key)
            # Try to access customer service (lightweight validation)
            customer_service = client.get_service("CustomerService")
            return True
        except Exception as e:
            logger.error(f"Credential validation failed: {e}")
            return False

    def remove_client(self, client_key: str) -> None:
        """
        Remove a client session.

        Args:
            client_key: Key of client to remove
        """
        if client_key in self._clients:
            del self._clients[client_key]

        if client_key in self._token_managers:
            del self._token_managers[client_key]

        if self._current_client_key == client_key:
            self._current_client_key = None

        logger.info(f"Removed client: {client_key}")


# Global authentication manager instance
auth_manager = GoogleAdsAuthManager()


def get_auth_manager() -> GoogleAdsAuthManager:
    """Get the global authentication manager instance."""
    return auth_manager

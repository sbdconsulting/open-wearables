import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.constants.google_health_endpoints import IDENTITY_ENDPOINT
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate, ProviderAccountNotLinkedError
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


FITBIT_SIGNUP_URL = "https://fitbit.google.com/auth/signup"


def _account_not_linked_signup_url(response: httpx.Response) -> str | None:
    """Return Google's sign-up link if ``response`` is ACCOUNT_NOT_LINKED, else None.

    The body is ``{"error": {"status": "FAILED_PRECONDITION", "details": [{"reason":
    "ACCOUNT_NOT_LINKED", "metadata": {"redirect_uri": ...}}]}}``. Matched on the
    reason code, never the message text.
    """
    if response.status_code != 400:
        return None
    try:
        details = (response.json().get("error") or {}).get("details") or []
    except Exception:
        return None
    for detail in details:
        if isinstance(detail, dict) and detail.get("reason") == "ACCOUNT_NOT_LINKED":
            metadata = detail.get("metadata") or {}
            return str(metadata.get("redirect_uri") or FITBIT_SIGNUP_URL)
    return None


class GoogleOAuth(BaseOAuthTemplate):
    """Google OAuth 2.0 implementation (web server / authorization code flow).

    Google requires ``access_type=offline`` to return a refresh token, and
    ``prompt=consent`` to guarantee one is returned on every consent (Google
    only issues a refresh token on the first consent otherwise). Client
    credentials are sent in the token request body.
    """

    use_pkce: bool = False
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    @property
    def endpoints(self) -> ProviderEndpoints:
        """OAuth endpoints for authorization and token exchange."""
        return ProviderEndpoints(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        """OAuth credentials from environment variables."""
        return ProviderCredentials(
            client_id=settings.google_client_id or "",
            client_secret=(settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.GOOGLE_HEALTH),
            default_scope=settings.google_default_scope,
        )

    def _build_auth_url(self, state: str) -> tuple[str, dict[str, Any] | None]:
        """Build Google's authorization URL.

        Adds the Google-specific parameters the base template doesn't emit:
        ``access_type=offline`` (return a refresh token), ``prompt=consent``
        (always return one), and ``include_granted_scopes=true`` (incremental
        authorization).
        """
        params = {
            "response_type": "code",
            "client_id": self.credentials.client_id,
            "redirect_uri": self.credentials.redirect_uri,
            "state": state,
            "scope": self.credentials.default_scope,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        return f"{self.endpoints.authorize_url}?{urlencode(params)}", None

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Resolve the Google Health API user id (healthUserId) and email.

        ``healthUserId`` is the stable id Google puts in webhook notifications, so we
        store it as the connection's ``provider_user_id`` to map inbound pings back to
        this user. Email (OIDC userinfo) is best-effort, for display only. Both calls
        are best-effort so a scope/permission gap never fails the whole connection.

        The one failure that IS fatal (MyBya): the identity call answering
        ACCOUNT_NOT_LINKED. That account has no Fitbit data behind it, so every
        later call would fail too; raise instead of saving a dead connection.
        """
        headers = {"Authorization": f"Bearer {token_response.access_token}"}
        return {
            "user_id": self._fetch(
                f"{self.api_base_url}{IDENTITY_ENDPOINT}", headers, "healthUserId", user_id, fail_if_not_linked=True
            ),
            "username": self._fetch(self.USERINFO_URL, headers, "email", user_id),
        }

    def _fetch(
        self, url: str, headers: dict[str, str], field: str, user_id: str, fail_if_not_linked: bool = False
    ) -> str | None:
        """GET ``url`` and return ``field`` from the JSON body; None on any failure.

        With ``fail_if_not_linked``, an ACCOUNT_NOT_LINKED answer raises
        ``ProviderAccountNotLinkedError`` rather than returning None.
        """
        try:
            response = httpx.get(url, headers=headers, timeout=30.0)
        except Exception as e:
            log_structured(
                logger,
                "warning",
                f"Failed to fetch Google {field}: {e}",
                provider=self.provider_name,
                task="get_provider_user_info",
                user_id=user_id,
            )
            return None
        if fail_if_not_linked:
            signup_url = _account_not_linked_signup_url(response)
            if signup_url:
                log_structured(
                    logger,
                    "warning",
                    "Google account is not linked to Google Health; connection refused",
                    provider=self.provider_name,
                    task="get_provider_user_info",
                    user_id=user_id,
                )
                raise ProviderAccountNotLinkedError(self.provider_name, signup_url)
        try:
            response.raise_for_status()
            return response.json().get(field)
        except Exception as e:
            log_structured(
                logger,
                "warning",
                f"Failed to fetch Google {field}: {e}",
                provider=self.provider_name,
                task="get_provider_user_info",
                user_id=user_id,
            )
            return None

"""Wahoo OAuth 2.0 (confidential client).

Source: "Authentication" in https://cloud-api.wahooligan.com (read 2026-10-16).

- Authorize at ``/oauth/authorize``, exchange and refresh at ``/oauth/token``.
- Access tokens live 2 hours. A refresh returns a NEW refresh token, and the old
  pair is revoked only when the new access token is first used in an API call.
- Since 2026-01-01 Wahoo allows 10 unrevoked access tokens per user, so a token
  must only be refreshed immediately before an API call. ``api_client`` already
  does exactly that (refresh inside ``make_authenticated_request``); nothing here
  refreshes speculatively.
- FIT downloads from Wahoo's CDN are not API calls and revoke nothing.
"""

import logging

import httpx
from fastapi import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_502_BAD_GATEWAY

from app.config import settings
from app.database import SessionLocal
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class WahooOAuth(BaseOAuthTemplate):
    """Wahoo OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url=f"{self.api_base_url}/oauth/authorize",
            token_url=f"{self.api_base_url}/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.wahoo_client_id or "",
            client_secret=(settings.wahoo_client_secret.get_secret_value() if settings.wahoo_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.WAHOO),
            default_scope=settings.wahoo_default_scope,
        )

    # Confidential client: the secret stays server-side, so no PKCE. Wahoo's docs
    # pass client_id/client_secret as token-request parameters.
    use_pkce: bool = False
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Read the Wahoo user id (GET /v1/user, needs user_read).

        Webhooks identify the athlete ONLY by this id, so a connection saved without
        it would accept the OAuth grant and then silently drop every workout. Fail
        the callback instead; the athlete can simply connect again.
        """
        try:
            response = httpx.get(
                f"{self.api_base_url}/v1/user",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            response.raise_for_status()
            wahoo_user_id = response.json().get("id")
        except Exception as exc:
            log_structured(
                logger,
                "error",
                f"Could not read the Wahoo user after token exchange: {exc}",
                provider=self.provider_name,
                task="get_provider_user_info",
                user_id=user_id,
            )
            raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail="Could not read the Wahoo user") from exc

        if wahoo_user_id is None:
            raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail="Wahoo returned no user id")
        # Wahoo's user object has no username; first/last name and email are PII we don't need.
        return {"user_id": str(wahoo_user_id), "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Revoke MyBya's access for this athlete (DELETE /v1/permissions).

        Called by disconnect, data purge and user deletion while the connection is
        still active. The stored access token is usually expired (2-hour lifetime),
        so on a 401 refresh it once and retry. Raising lets the caller log the
        failure; it never blocks the purge itself.
        """
        response = self._delete_permissions(access_token)
        if response.status_code == HTTP_401_UNAUTHORIZED and provider_user_id:
            fresh = self._refreshed_access_token(provider_user_id)
            if fresh:
                response = self._delete_permissions(fresh)
        response.raise_for_status()

    def _delete_permissions(self, access_token: str) -> httpx.Response:
        return httpx.delete(
            f"{self.api_base_url}/v1/permissions",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    def _refreshed_access_token(self, provider_user_id: str) -> str | None:
        with SessionLocal() as db:
            connection = self.connection_repo.get_by_provider_user_id(db, self.provider_name, provider_user_id)
            if not connection or not connection.refresh_token:
                return None
            # A dead refresh token raises 401 and marks the connection revoked,
            # which is the right end state for a deregistration anyway.
            try:
                return self.refresh_access_token(db, connection.user_id, connection.refresh_token).access_token
            except HTTPException:
                return None

"""MyBya: a Google grant without the health scopes must not become a connection.

Google's consent screen shows each requested scope as its own checkbox. An athlete
who leaves them unticked comes back with only ``openid email``; every Health API
call then answers 403 "insufficient authentication scopes". Before this, the
callback saved that connection as active, so the athlete saw "Connected" and got
nothing. Now the granted scopes are checked first and the athlete is sent back
to the app with a reason.
"""

from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.api.routes.v1.oauth import _connection_refused_redirect
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthState, OAuthTokenResponse
from app.services.providers.factory import ProviderFactory
from app.services.providers.google_health.oauth import GoogleOAuth, _missing_health_scopes
from app.services.providers.templates.base_oauth import ProviderMissingScopesError

_OAUTH = "app.services.providers.google_health.oauth"

ACTIVITY = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
METRICS = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
SLEEP = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
SETTINGS = "https://www.googleapis.com/auth/googlehealth.settings.readonly"
REQUESTED = f"openid email {ACTIVITY} {METRICS} {SLEEP} {SETTINGS}"
# What Google actually returned for the owner's first connect on 2026-11-14.
EMAIL_ONLY = "https://www.googleapis.com/auth/userinfo.email openid"
ALL_GRANTED = f"https://www.googleapis.com/auth/userinfo.email openid {ACTIVITY} {METRICS} {SLEEP} {SETTINGS}"


def _response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("GET", "https://health.googleapis.com"))


def _token(scope: str | None) -> OAuthTokenResponse:
    return OAuthTokenResponse(
        access_token="access-1", token_type="Bearer", expires_in=3600, refresh_token="refresh-1", scope=scope
    )


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch) -> GoogleOAuth:
    monkeypatch.setattr("app.config.settings.google_client_id", "client-123")
    monkeypatch.setattr("app.config.settings.google_client_secret", SecretStr("secret-456"))
    monkeypatch.setattr("app.config.settings.google_default_scope", REQUESTED)
    strategy = ProviderFactory().get_provider("google_health")
    assert isinstance(strategy.oauth, GoogleOAuth)
    return strategy.oauth


class TestMissingHealthScopes:
    def test_email_only_grant_misses_every_health_scope(self) -> None:
        assert _missing_health_scopes(REQUESTED, EMAIL_ONLY) == [ACTIVITY, METRICS, SLEEP, SETTINGS]

    def test_partial_grant_reports_only_what_is_missing(self) -> None:
        assert _missing_health_scopes(REQUESTED, f"openid {ACTIVITY} {METRICS}") == [SLEEP, SETTINGS]

    def test_full_grant_misses_nothing(self) -> None:
        assert _missing_health_scopes(REQUESTED, ALL_GRANTED) == []

    def test_openid_and_email_never_count(self) -> None:
        # Google answers "email" as userinfo.email; that must not read as missing.
        assert _missing_health_scopes(REQUESTED, f"{ACTIVITY} {METRICS} {SLEEP} {SETTINGS}") == []

    def test_unknown_grant_is_not_treated_as_missing(self) -> None:
        assert _missing_health_scopes(REQUESTED, None) == []


class TestProviderUserInfo:
    def test_email_only_grant_raises_before_any_api_call(self, oauth: GoogleOAuth) -> None:
        with (
            patch(f"{_OAUTH}.httpx.get") as get,
            pytest.raises(ProviderMissingScopesError) as raised,
        ):
            oauth._get_provider_user_info(_token(EMAIL_ONLY), "user-1")
        get.assert_not_called()
        assert raised.value.provider == "google_health"
        assert raised.value.reason == "missing_scopes"
        assert raised.value.missing == [ACTIVITY, METRICS, SLEEP, SETTINGS]

    def test_full_grant_proceeds(self, oauth: GoogleOAuth) -> None:
        def fake_get(url: str, **_: Any) -> httpx.Response:
            if "identity" in url:
                return _response(200, {"healthUserId": "health-42"})
            return _response(200, {"email": "athlete@example.com"})

        with patch(f"{_OAUTH}.httpx.get", side_effect=fake_get):
            info = oauth._get_provider_user_info(_token(ALL_GRANTED), "user-1")
        assert info == {"user_id": "health-42", "username": "athlete@example.com"}


class TestHandleCallback:
    def test_saves_nothing_and_carries_the_state(self, oauth: GoogleOAuth) -> None:
        state = OAuthState(
            user_id=uuid4(), provider="google_health", redirect_uri="https://app.mybya.com/settings/integrations"
        )
        save = MagicMock()
        with (
            patch.object(oauth, "_validate_state", return_value=(state, None)),
            patch.object(oauth, "_exchange_token", return_value=_token(EMAIL_ONLY)),
            patch.object(oauth, "_save_connection", save),
            pytest.raises(ProviderMissingScopesError) as raised,
        ):
            oauth.handle_callback(MagicMock(), "code-1", "state-1")
        save.assert_not_called()
        assert raised.value.oauth_state is state


class TestRedirect:
    def test_returns_to_the_app_with_the_reason(self) -> None:
        error = ProviderMissingScopesError("google_health", [ACTIVITY])
        error.oauth_state = OAuthState(
            user_id=uuid4(), provider="google_health", redirect_uri="https://app.mybya.com/settings/integrations"
        )
        response = _connection_refused_redirect(ProviderName.GOOGLE_HEALTH, error)
        assert response.status_code == 303
        location = urlparse(response.headers["location"])
        assert location.netloc == "app.mybya.com"
        assert parse_qs(location.query) == {"ow_error": ["missing_scopes"], "ow_provider": ["google_health"]}

    def test_without_a_redirect_uses_the_error_page(self) -> None:
        error = ProviderMissingScopesError("google_health", [ACTIVITY])
        error.oauth_state = OAuthState(user_id=uuid4(), provider="google_health", redirect_uri=None)
        response = _connection_refused_redirect(ProviderName.GOOGLE_HEALTH, error)
        location = urlparse(response.headers["location"])
        assert location.path == "/api/v1/oauth/error"
        assert "unticked" in parse_qs(location.query)["message"][0]

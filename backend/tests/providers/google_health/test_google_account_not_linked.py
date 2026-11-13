"""MyBya: a Google account with no Fitbit data behind it must not become a connection.

Google answers every Health API call for such an account with 400 ACCOUNT_NOT_LINKED.
Before this, the callback saved the connection anyway, so the athlete saw "Connected"
and then got nothing. Now the identity call's answer refuses the connection and the
athlete is sent back to the app with a reason.
"""

from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.api.routes.v1.oauth import _account_not_linked_redirect, _with_query
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthState, OAuthTokenResponse
from app.services.providers.factory import ProviderFactory
from app.services.providers.google_health.oauth import (
    FITBIT_SIGNUP_URL,
    GoogleOAuth,
    _account_not_linked_signup_url,
)
from app.services.providers.templates.base_oauth import ProviderAccountNotLinkedError

_OAUTH = "app.services.providers.google_health.oauth"

NOT_LINKED_BODY: dict[str, Any] = {
    "error": {
        "code": 400,
        "message": "The account is not linked to Google Health.",
        "status": "FAILED_PRECONDITION",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCOUNT_NOT_LINKED",
                "domain": "health.googleapis.com",
                "metadata": {"redirect_uri": "https://fitbit.google.com/auth/signup"},
            }
        ],
    }
}


def _response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("GET", "https://health.googleapis.com"))


def _token() -> OAuthTokenResponse:
    return OAuthTokenResponse(access_token="access-1", token_type="Bearer", expires_in=3600, refresh_token="refresh-1")


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch) -> GoogleOAuth:
    monkeypatch.setattr("app.config.settings.google_client_id", "client-123")
    monkeypatch.setattr("app.config.settings.google_client_secret", SecretStr("secret-456"))
    strategy = ProviderFactory().get_provider("google_health")
    assert isinstance(strategy.oauth, GoogleOAuth)
    return strategy.oauth


class TestSignupUrlDetection:
    def test_reads_googles_link_from_account_not_linked(self) -> None:
        assert (
            _account_not_linked_signup_url(_response(400, NOT_LINKED_BODY)) == "https://fitbit.google.com/auth/signup"
        )

    def test_falls_back_to_the_known_link_when_google_omits_it(self) -> None:
        body = {"error": {"details": [{"reason": "ACCOUNT_NOT_LINKED"}]}}
        assert _account_not_linked_signup_url(_response(400, body)) == FITBIT_SIGNUP_URL

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (400, {"error": {"details": [{"reason": "INVALID_ARGUMENT"}]}}),
            (403, NOT_LINKED_BODY),
            (200, {"healthUserId": "abc"}),
            (400, {}),
        ],
    )
    def test_ignores_everything_else(self, status: int, body: dict[str, Any]) -> None:
        assert _account_not_linked_signup_url(_response(status, body)) is None


class TestProviderUserInfo:
    def test_not_linked_identity_raises(self, oauth: GoogleOAuth) -> None:
        with (
            patch(f"{_OAUTH}.httpx.get", return_value=_response(400, NOT_LINKED_BODY)),
            pytest.raises(ProviderAccountNotLinkedError) as raised,
        ):
            oauth._get_provider_user_info(_token(), "user-1")
        assert raised.value.provider == "google_health"
        assert raised.value.signup_url == "https://fitbit.google.com/auth/signup"

    def test_linked_account_resolves_both_ids(self, oauth: GoogleOAuth) -> None:
        def fake_get(url: str, **_: Any) -> httpx.Response:
            if "identity" in url:
                return _response(200, {"healthUserId": "health-42"})
            return _response(200, {"email": "athlete@example.com"})

        with patch(f"{_OAUTH}.httpx.get", side_effect=fake_get):
            info = oauth._get_provider_user_info(_token(), "user-1")
        assert info == {"user_id": "health-42", "username": "athlete@example.com"}

    def test_other_identity_failures_stay_best_effort(self, oauth: GoogleOAuth) -> None:
        with patch(f"{_OAUTH}.httpx.get", return_value=_response(500)):
            info = oauth._get_provider_user_info(_token(), "user-1")
        assert info == {"user_id": None, "username": None}

    def test_not_linked_on_userinfo_does_not_raise(self, oauth: GoogleOAuth) -> None:
        def fake_get(url: str, **_: Any) -> httpx.Response:
            if "identity" in url:
                return _response(200, {"healthUserId": "health-42"})
            return _response(400, NOT_LINKED_BODY)

        with patch(f"{_OAUTH}.httpx.get", side_effect=fake_get):
            info = oauth._get_provider_user_info(_token(), "user-1")
        assert info["user_id"] == "health-42"


class TestHandleCallback:
    def test_saves_nothing_and_carries_the_state(self, oauth: GoogleOAuth) -> None:
        state = OAuthState(
            user_id=uuid4(), provider="google_health", redirect_uri="https://app.mybya.com/settings/integrations"
        )
        save = MagicMock()
        with (
            patch.object(oauth, "_validate_state", return_value=(state, None)),
            patch.object(oauth, "_exchange_token", return_value=_token()),
            patch.object(oauth, "_save_connection", save),
            patch(f"{_OAUTH}.httpx.get", return_value=_response(400, NOT_LINKED_BODY)),
            pytest.raises(ProviderAccountNotLinkedError) as raised,
        ):
            oauth.handle_callback(MagicMock(), "code-1", "state-1")
        save.assert_not_called()
        assert raised.value.oauth_state is state


class TestRedirect:
    def test_returns_to_the_app_with_the_reason(self) -> None:
        error = ProviderAccountNotLinkedError("google_health", FITBIT_SIGNUP_URL)
        error.oauth_state = OAuthState(
            user_id=uuid4(), provider="google_health", redirect_uri="https://app.mybya.com/settings/integrations?tab=1"
        )
        response = _account_not_linked_redirect(ProviderName.GOOGLE_HEALTH, error)
        assert response.status_code == 303
        location = urlparse(response.headers["location"])
        assert location.netloc == "app.mybya.com"
        assert location.path == "/settings/integrations"
        assert parse_qs(location.query) == {
            "tab": ["1"],
            "ow_error": ["account_not_linked"],
            "ow_provider": ["google_health"],
        }

    def test_without_a_redirect_uses_the_error_page(self) -> None:
        error = ProviderAccountNotLinkedError("google_health", FITBIT_SIGNUP_URL)
        error.oauth_state = OAuthState(user_id=uuid4(), provider="google_health", redirect_uri=None)
        response = _account_not_linked_redirect(ProviderName.GOOGLE_HEALTH, error)
        location = urlparse(response.headers["location"])
        assert location.path == "/api/v1/oauth/error"
        assert FITBIT_SIGNUP_URL in parse_qs(location.query)["message"][0]

    def test_with_query_replaces_rather_than_duplicates(self) -> None:
        url = _with_query("https://x.test/p?ow_error=old&a=1", {"ow_error": "new"})
        assert parse_qs(urlparse(url).query) == {"a": ["1"], "ow_error": ["new"]}

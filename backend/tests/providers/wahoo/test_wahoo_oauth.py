"""WahooOAuth and WahooStrategy: what the approved Wahoo app fixes, and deauthorization."""

from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.constants.workout_types.wahoo import WAHOO_WORKOUT_TYPE_MAPPINGS, get_unified_workout_type
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import DEFAULT_PROVIDER_PRIORITY, ProviderName, WorkoutType
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.factory import ProviderFactory
from app.services.providers.wahoo.oauth import WahooOAuth
from app.services.providers.wahoo.strategy import WahooStrategy

_OAUTH = "app.services.providers.wahoo.oauth"


@pytest.fixture
def strategy(monkeypatch: pytest.MonkeyPatch) -> WahooStrategy:
    monkeypatch.setattr("app.config.settings.api_base_url", "https://wearables.mybya.com")
    monkeypatch.setattr("app.config.settings.wahoo_client_id", "client-123")
    monkeypatch.setattr("app.config.settings.wahoo_client_secret", SecretStr("secret-456"))
    provider = ProviderFactory().get_provider("wahoo")
    assert isinstance(provider, WahooStrategy)
    return provider


def _oauth(strategy: WahooStrategy) -> WahooOAuth:
    assert isinstance(strategy.oauth, WahooOAuth)
    return strategy.oauth


def _response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("GET", "https://api.wahooligan.com"))


def _token(access: str = "access-1") -> OAuthTokenResponse:
    return OAuthTokenResponse(access_token=access, token_type="Bearer", expires_in=7200, refresh_token="refresh-1")


class TestRegistration:
    def test_identity_and_capabilities(self, strategy: WahooStrategy) -> None:
        assert strategy.name == ProviderName.WAHOO.value == "wahoo"
        assert strategy.api_base_url == "https://api.wahooligan.com"
        assert strategy.capabilities.rest_pull is True
        assert strategy.capabilities.webhook_stream is True
        assert strategy.data_247 is None  # Wahoo exposes no daily or sleep data
        assert strategy.webhooks is not None
        assert ProviderName.WAHOO in DEFAULT_PROVIDER_PRIORITY

    def test_redirect_uri_matches_the_registered_one(self, strategy: WahooStrategy) -> None:
        assert _oauth(strategy).credentials.redirect_uri == "https://wearables.mybya.com/api/v1/oauth/wahoo/callback"

    def test_authorization_url(self, strategy: WahooStrategy) -> None:
        oauth = _oauth(strategy)
        with patch.object(WahooOAuth, "redis_client", MagicMock()):
            url, state = oauth.get_authorization_url(uuid4())

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://api.wahooligan.com/oauth/authorize"
        assert query["client_id"] == ["client-123"]
        assert query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["https://wearables.mybya.com/api/v1/oauth/wahoo/callback"]
        # Exactly the approved scopes. No workouts_write: ingest only.
        assert query["scope"] == ["user_read workouts_read offline_data"]
        assert query["state"] == [state]
        assert "code_challenge" not in query  # confidential client, not PKCE

    def test_token_exchange_sends_credentials_in_the_body(self, strategy: WahooStrategy) -> None:
        oauth = _oauth(strategy)
        assert oauth.auth_method == AuthenticationMethod.BODY
        data, _headers = oauth._prepare_token_request("code-1", None)
        assert data["client_id"] == "client-123"
        assert data["client_secret"] == "secret-456"
        assert data["grant_type"] == "authorization_code"
        assert oauth.endpoints.token_url == "https://api.wahooligan.com/oauth/token"


class TestUserInfo:
    def test_reads_the_wahoo_user_id(self, strategy: WahooStrategy) -> None:
        with patch(f"{_OAUTH}.httpx.get", return_value=_response(200, {"id": 60462, "first": "Bob"})) as get:
            info = _oauth(strategy)._get_provider_user_info(_token(), "u1")

        assert info == {"user_id": "60462", "username": None}
        assert get.call_args.args[0] == "https://api.wahooligan.com/v1/user"
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer access-1"}

    @pytest.mark.parametrize("response", [_response(500), _response(200, {"first": "Bob"})])
    def test_no_user_id_fails_the_callback(self, strategy: WahooStrategy, response: httpx.Response) -> None:
        """A connection with no Wahoo user id could never match a webhook; don't save one."""
        with patch(f"{_OAUTH}.httpx.get", return_value=response), pytest.raises(HTTPException) as error:
            _oauth(strategy)._get_provider_user_info(_token(), "u1")
        assert error.value.status_code == 502


class TestDeregister:
    def test_deletes_permissions(self, strategy: WahooStrategy) -> None:
        with patch(f"{_OAUTH}.httpx.delete", return_value=_response(204)) as delete:
            _oauth(strategy).deregister_user("access-1", provider_user_id="60462")

        delete.assert_called_once()
        assert delete.call_args.args[0] == "https://api.wahooligan.com/v1/permissions"
        assert delete.call_args.kwargs["headers"] == {"Authorization": "Bearer access-1"}

    def test_expired_token_is_refreshed_once_then_retried(self, strategy: WahooStrategy) -> None:
        oauth = _oauth(strategy)
        with (
            patch(f"{_OAUTH}.httpx.delete", side_effect=[_response(401), _response(204)]) as delete,
            patch.object(WahooOAuth, "_refreshed_access_token", return_value="access-2") as refresh,
        ):
            oauth.deregister_user("expired", provider_user_id="60462")

        refresh.assert_called_once_with("60462")
        assert [c.kwargs["headers"]["Authorization"] for c in delete.call_args_list] == [
            "Bearer expired",
            "Bearer access-2",
        ]

    def test_unrecoverable_failure_raises_for_the_caller_to_log(self, strategy: WahooStrategy) -> None:
        with (
            patch(f"{_OAUTH}.httpx.delete", return_value=_response(401)),
            patch.object(WahooOAuth, "_refreshed_access_token", return_value=None),
            pytest.raises(httpx.HTTPStatusError),
        ):
            _oauth(strategy).deregister_user("expired", provider_user_id="60462")


class TestWorkoutTypes:
    def test_documented_ids(self) -> None:
        assert get_unified_workout_type(0) == WorkoutType.CYCLING
        assert get_unified_workout_type(1) == WorkoutType.RUNNING
        assert get_unified_workout_type(5) == WorkoutType.TREADMILL
        assert get_unified_workout_type(25) == WorkoutType.POOL_SWIMMING
        assert get_unified_workout_type(61) == WorkoutType.INDOOR_CYCLING
        assert get_unified_workout_type(68) == WorkoutType.INDOOR_CYCLING

    def test_unknown_and_missing_ids_are_other(self) -> None:
        assert get_unified_workout_type(None) == WorkoutType.OTHER
        assert get_unified_workout_type(999) == WorkoutType.OTHER

    def test_ids_are_unique(self) -> None:
        ids = [wahoo_id for wahoo_id, _, _ in WAHOO_WORKOUT_TYPE_MAPPINGS]
        assert len(ids) == len(set(ids))

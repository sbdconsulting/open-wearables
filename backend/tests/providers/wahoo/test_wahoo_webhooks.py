"""Wahoo webhooks: token check on every delivery, both routes, and async processing."""

import json
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.models import EventRecord
from app.services.providers.factory import ProviderFactory
from app.services.providers.wahoo.webhook_handler import WahooWebhookHandler
from app.services.providers.wahoo.workouts import WahooWorkouts
from tests.factories import UserConnectionFactory
from tests.providers.wahoo.conftest import WAHOO_USER_ID, WEBHOOK_TOKEN, webhook_payload

_HANDLER = "app.services.providers.wahoo.webhook_handler"
# The URI registered on the approved Wahoo app, and OW's unified provider route.
ROUTES = ["/api/v1/webhooks/wahoo", "/api/v1/providers/wahoo/webhooks"]


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("app.config.settings.wahoo_webhook_token", SecretStr(WEBHOOK_TOKEN))
    return WEBHOOK_TOKEN


@pytest.fixture
def queue() -> Generator[MagicMock, None, None]:
    with patch(f"{_HANDLER}.celery_app") as celery_app, patch(f"{_HANDLER}.store_raw_payload") as store:
        celery_app.send_task.return_value.id = "task-1"
        celery_app.store = store
        yield celery_app


def _post(client: TestClient, route: str, payload: dict[str, Any]) -> Any:
    return client.post(route, content=json.dumps(payload), headers={"Content-Type": "application/json"})


def _handler() -> WahooWebhookHandler:
    handler = ProviderFactory().get_provider("wahoo").webhooks
    assert isinstance(handler, WahooWebhookHandler)
    return handler


class TestTokenCheck:
    @pytest.mark.parametrize("route", ROUTES)
    def test_valid_token_is_accepted_and_queued(
        self, client: TestClient, route: str, token: str, queue: MagicMock
    ) -> None:
        response = _post(client, route, webhook_payload())

        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        queue.send_task.assert_called_once()
        provider, raw, _trace = queue.send_task.call_args.kwargs["args"]
        assert provider == "wahoo"
        assert queue.send_task.call_args.kwargs["queue"] == "webhook_sync"
        # The shared secret never reaches the broker or raw-payload storage.
        assert "webhook_token" not in raw
        assert "webhook_token" not in queue.store.call_args.kwargs["payload"]
        assert raw["user"] == {"id": WAHOO_USER_ID}

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("sent", [None, "", "wrong-token"])
    def test_missing_or_wrong_token_is_rejected(
        self, client: TestClient, route: str, sent: str | None, token: str, queue: MagicMock
    ) -> None:
        response = _post(client, route, webhook_payload(token=sent))

        assert response.status_code == 401
        queue.send_task.assert_not_called()
        queue.store.assert_not_called()

    def test_unconfigured_token_rejects_everything(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, queue: MagicMock
    ) -> None:
        """Fail closed: unlike Strava/Oura, no fallback to SECRET_KEY."""
        monkeypatch.setattr("app.config.settings.wahoo_webhook_token", None)

        response = _post(client, ROUTES[0], webhook_payload(token="anything"))

        assert response.status_code == 401
        queue.send_task.assert_not_called()

    def test_non_json_body_is_rejected(self, client: TestClient, token: str, queue: MagicMock) -> None:
        response = client.post(ROUTES[0], content=b"not json")
        assert response.status_code == 401
        queue.send_task.assert_not_called()

    def test_unsupported_event_is_acknowledged_not_queued(
        self, client: TestClient, token: str, queue: MagicMock
    ) -> None:
        payload = webhook_payload()
        payload["event_type"] = "something_new"

        response = _post(client, ROUTES[0], payload)

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        queue.send_task.assert_not_called()


class TestProcessPayload:
    """What the process_webhook_push task runs, with the payload as queued (no token)."""

    @staticmethod
    def _queued(**summary_overrides: Any) -> dict[str, Any]:
        payload = webhook_payload(**summary_overrides)
        payload.pop("webhook_token")
        return payload

    def test_unknown_wahoo_user(self, db: Session) -> None:
        result = _handler().process_payload(db, self._queued(), "t1")
        assert result == {"status": "user_not_found", "wahoo_user_id": str(WAHOO_USER_ID)}

    def test_saves_the_workout_for_the_connected_athlete(self, db: Session, elemnt_bolt_fit: bytes) -> None:
        connection = UserConnectionFactory(provider="wahoo", provider_user_id=str(WAHOO_USER_ID))

        with patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit):
            result = _handler().process_payload(db, self._queued(workout_type_id=15), "t1")

        assert result["status"] == "saved"
        assert result["records_saved"] == 1
        assert result["user_id"] == str(connection.user_id)
        assert result["device_model"] == "ELEMNT BOLT"
        assert db.query(EventRecord).filter(EventRecord.external_id == "56519").count() == 1

    def test_redelivery_is_a_no_op(self, db: Session, elemnt_bolt_fit: bytes) -> None:
        UserConnectionFactory(provider="wahoo", provider_user_id=str(WAHOO_USER_ID))

        with patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit):
            _handler().process_payload(db, self._queued(), "t1")
            again = _handler().process_payload(db, self._queued(), "t2")

        assert again["status"] == "ignored"
        assert again["reason"] == "duplicate_workout"
        assert db.query(EventRecord).filter(EventRecord.external_id == "56519").count() == 1

    def test_revoked_connection_is_not_written_to(self, db: Session) -> None:
        UserConnectionFactory(provider="wahoo", provider_user_id=str(WAHOO_USER_ID), status="revoked")

        result = _handler().process_payload(db, self._queued(), "t1")

        assert result["status"] == "user_not_found"
        assert db.query(EventRecord).filter(EventRecord.external_id == "56519").count() == 0

    def test_extract_user_id_reads_the_nested_user(self) -> None:
        assert _handler().extract_user_id(self._queued()) == str(WAHOO_USER_ID)
        assert _handler().extract_user_id({}) is None

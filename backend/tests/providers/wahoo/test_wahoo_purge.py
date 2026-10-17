"""What MyBya's 48-hour purge does in the engine for a Wahoo athlete.

MyBya's DataRetention::ProviderPurge calls DELETE /users/{id}/connections/wahoo/data.
Before this provider existed that path answered 400 for "wahoo" (unknown enum value).
"""

from typing import Any
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.providers.wahoo import WahooWorkoutSummaryJSON
from app.services.providers.factory import ProviderFactory
from app.services.providers.wahoo.workouts import WahooWorkouts
from tests.factories import ApiKeyFactory, DeveloperFactory, UserConnectionFactory
from tests.providers.wahoo.conftest import WAHOO_USER_ID, summary_payload
from tests.utils import api_key_headers


def _deleted(*_: Any, **__: Any) -> httpx.Response:
    return httpx.Response(204, request=httpx.Request("DELETE", "https://api.wahooligan.com/v1/permissions"))


def test_purge_deletes_the_engine_copy_and_deauthorizes_at_wahoo(
    client: TestClient, db: Session, elemnt_bolt_fit: bytes
) -> None:
    connection = UserConnectionFactory(provider="wahoo", provider_user_id=str(WAHOO_USER_ID), access_token="tok-1")
    workouts = ProviderFactory().get_provider("wahoo").workouts
    assert isinstance(workouts, WahooWorkouts)
    with patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit):
        workouts.save_summary(db, connection.user_id, WahooWorkoutSummaryJSON.model_validate(summary_payload()))
    assert db.query(EventRecord).count() == 1

    api_key = ApiKeyFactory(developer=DeveloperFactory(email="purge@example.com", password="x"), name="purge")
    with patch("app.services.providers.wahoo.oauth.httpx.delete", side_effect=_deleted) as wahoo_delete:
        response = client.delete(
            f"/api/v1/users/{connection.user_id}/connections/wahoo/data",
            headers=api_key_headers(api_key.plain_key),
        )

    assert response.status_code == 204
    wahoo_delete.assert_called_once()
    assert wahoo_delete.call_args.args[0] == "https://api.wahooligan.com/v1/permissions"
    assert wahoo_delete.call_args.kwargs["headers"] == {"Authorization": "Bearer tok-1"}
    assert db.query(DataSource).filter(DataSource.user_id == connection.user_id).count() == 0
    assert db.query(EventRecord).count() == 0
    db.expire_all()
    assert db.get(UserConnection, connection.id).status == ConnectionStatus.REVOKED

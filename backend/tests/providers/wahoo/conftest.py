"""Wahoo payloads, copied from the Cloud API docs' own samples (https://cloud-api.wahooligan.com).

Only the ids, times and file URL are changed, so each test can say which delivery it means.
"""

from pathlib import Path
from typing import Any

import pytest

FIT_PATH = Path(__file__).parents[2] / "fixtures" / "wahoo" / "elemnt_bolt.fit"
FIT_URL = "https://cdn.wahooligan.example/4_Mile_Segment_.fit"
WAHOO_USER_ID = 60462
WEBHOOK_TOKEN = "wahoo-test-webhook-token"


def summary_payload(**overrides: Any) -> dict[str, Any]:
    """The documented "Get a Workout Summary" sample, with the webhook's nested workout."""
    summary: dict[str, Any] = {
        "id": 8297,
        "name": "Easy Ride",
        "ascent_accum": "450.00",
        "cadence_avg": "50.00",
        "calories_accum": "1500.00",
        "distance_accum": "24909.71",
        "duration_active_accum": "179.00",
        "duration_paused_accum": "95.25",
        "duration_total_accum": "275.00",
        "heart_rate_avg": "100.00",
        "power_bike_np_last": "150.00",
        "power_bike_tss_last": "304.90",
        "power_avg": "94.59",
        "speed_avg": "10.75",
        "work_accum": "1041480.00",
        "time_zone": "America/Denver",
        "manual": False,
        "edited": False,
        "file": {"url": FIT_URL},
        "fitness_app_id": 1002,
        "created_at": "2018-10-23T20:43:50.000Z",
        "updated_at": "2018-10-23T20:43:50.000Z",
        "workout": {
            "id": 56519,
            "starts": "2015-08-12T09:00:00.000Z",
            "minutes": 12,
            "name": "Friday Fun",
            "created_at": "2018-10-23T20:41:55.000Z",
            "updated_at": "2018-10-23T20:41:55.000Z",
            "plan_id": None,
            "workout_token": "123",
            "workout_type_id": 40,
        },
    }
    if "workout_type_id" in overrides:  # lives on the nested workout, not the summary
        summary["workout"]["workout_type_id"] = overrides.pop("workout_type_id")
    summary.update(overrides)
    return summary


def webhook_payload(token: str | None = WEBHOOK_TOKEN, **summary_overrides: Any) -> dict[str, Any]:
    """The documented "Sample Workout Summary Webhook Message"."""
    payload: dict[str, Any] = {
        "event_type": "workout_summary",
        "user": {"id": WAHOO_USER_ID},
        "workout_summary": summary_payload(**summary_overrides),
    }
    if token is not None:
        payload["webhook_token"] = token
    return payload


@pytest.fixture
def elemnt_bolt_fit() -> bytes:
    return FIT_PATH.read_bytes()

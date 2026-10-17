"""Wahoo webhook payload ("Webhooks → Workout Summary" in the Cloud API docs)."""

from pydantic import BaseModel, Field

from app.schemas.providers.wahoo.workout import WahooWorkoutSummaryJSON


class WahooWebhookUser(BaseModel):
    id: int  # Wahoo user id, stored as the connection's provider_user_id


class WahooWebhookEvent(BaseModel):
    """One Wahoo webhook delivery.

    `webhook_token` is the shared secret configured on the Wahoo app. It is excluded
    from model_dump() so it never reaches raw-payload storage, the Celery broker or
    a log line.
    """

    event_type: str
    webhook_token: str | None = Field(default=None, exclude=True, repr=False)
    user: WahooWebhookUser
    workout_summary: WahooWorkoutSummaryJSON | None = None

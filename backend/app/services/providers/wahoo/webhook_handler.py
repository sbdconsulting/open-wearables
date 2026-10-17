"""Wahoo webhook handler.

Source: "Webhooks" in https://cloud-api.wahooligan.com (read 2026-10-16).

Delivery model
--------------
  Wahoo POSTs JSON for one event type, ``workout_summary``, carrying the full
  summary (totals + FIT file URL) and its parent workout. It needs the athlete to
  have granted ``offline_data``. Anything but a 200 is retried after 30 min, 4 h,
  24 h and 72 h, and duplicates are expected "when files are updated or deleted".

  ``dispatch()`` verifies and acknowledges, then enqueues the shared
  ``process_webhook_push`` task; ``process_payload()`` downloads the FIT file and
  saves the workout. Saving is idempotent on the Wahoo workout id.

Authentication
--------------
  No signature and no header: Wahoo copies the token configured on the app into
  every body as ``webhook_token``, and says "Any request that doesn't include this
  token should be ignored." It is compared in constant time against
  ``WAHOO_WEBHOOK_TOKEN``; unset means every delivery is rejected (401), never
  accepted. The token is dropped before the payload is stored or queued.

No deauthorization event
------------------------
  Wahoo sends nothing when an athlete revokes access in their Wahoo account. The
  revocation surfaces on the next API call (refresh rejected → connection revoked
  → ``connection.revoked`` webhook), which the periodic pull makes.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.providers.wahoo import WahooWebhookEvent
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.wahoo.workouts import WahooWorkouts
from app.services.raw_payload_storage import store_raw_payload
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"

WORKOUT_SUMMARY_EVENT = "workout_summary"


class WahooWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Wahoo ``workout_summary`` pushes."""

    def __init__(self, workouts: WahooWorkouts) -> None:
        super().__init__("wahoo")
        self.workouts = workouts
        self.connection_repo = UserConnectionRepository()

    def extract_user_id(self, payload: dict[str, Any]) -> str | None:
        user = payload.get("user")
        value = user.get("id") if isinstance(user, dict) else None
        return str(value) if value is not None else None

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        expected = settings.wahoo_webhook_token.get_secret_value() if settings.wahoo_webhook_token else ""
        if not expected:
            log_structured(
                self.logger,
                "error",
                "WAHOO_WEBHOOK_TOKEN is not set; rejecting Wahoo webhook",
                provider=self.provider_name,
                action="webhook_token_unconfigured",
            )
            return False
        try:
            provided = json.loads(body).get("webhook_token")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return False
        if not isinstance(provided, str) or not provided:
            return False
        return self._verify_token(expected, provided)

    def parse_payload(self, body: bytes) -> WahooWebhookEvent:
        try:
            return WahooWebhookEvent.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}") from exc
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    def dispatch(self, db: DbSession, payload: WahooWebhookEvent) -> dict[str, Any]:
        """Acknowledge and enqueue. Returns 200 quickly so Wahoo does not retry."""
        trace_id = str(uuid4())[:8]

        if payload.event_type != WORKOUT_SUMMARY_EVENT:
            log_structured(
                self.logger,
                "info",
                "Ignoring unsupported Wahoo webhook event",
                provider=self.provider_name,
                trace_id=trace_id,
                event_type=payload.event_type,
            )
            return {"status": "ignored", "reason": f"event_type:{payload.event_type}"}

        raw = payload.model_dump(mode="json")  # webhook_token excluded by the schema
        store_raw_payload(source="webhook", provider=self.provider_name, payload=raw, trace_id=trace_id)
        task = celery_app.send_task(_PROCESS_PUSH_TASK, args=[self.provider_name, raw, trace_id], queue="webhook_sync")
        log_structured(
            self.logger,
            "info",
            "Enqueued Wahoo webhook processing task",
            provider=self.provider_name,
            trace_id=trace_id,
            wahoo_user_id=payload.user.id,
            summary_id=payload.workout_summary.id if payload.workout_summary else None,
            task_id=getattr(task, "id", None),
        )
        return {"status": "accepted"}

    def supported_event_types(self) -> list[str]:
        return [WORKOUT_SUMMARY_EVENT]

    # ------------------------------------------------------------------
    # Async processing (process_webhook_push task)
    # ------------------------------------------------------------------

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        try:
            event = WahooWebhookEvent.model_validate(payload)
        except ValidationError as exc:
            log_and_capture_error(
                exc,
                self.logger,
                "Invalid Wahoo webhook payload",
                extra={"provider": self.provider_name, "trace_id": trace_id},
            )
            return {"status": "error", "error": f"Invalid payload: {exc}"}

        if event.workout_summary is None:
            return {"status": "ignored", "reason": "no_workout_summary"}

        wahoo_user_id = str(event.user.id)
        connection = self.connection_repo.get_by_provider_user_id(db, self.provider_name, wahoo_user_id)
        if not connection:
            log_structured(
                self.logger,
                "warning",
                "No active connection for Wahoo user",
                provider=self.provider_name,
                trace_id=trace_id,
                action="webhook_no_connection",
                wahoo_user_id=wahoo_user_id,
            )
            return {"status": "user_not_found", "wahoo_user_id": wahoo_user_id}

        user_id: UUID = connection.user_id
        self.connection_repo.update_last_synced_at(db, connection)

        try:
            result = self.workouts.save_summary(db, user_id, event.workout_summary)
        except IntegrityError:
            db.rollback()
            result = {"status": "ignored", "reason": "duplicate_workout"}

        result["user_id"] = str(user_id)
        if result["status"] == "saved":
            result["records_saved"] = 1
        log_structured(
            self.logger,
            "info",
            "Processed Wahoo workout summary",
            provider=self.provider_name,
            trace_id=trace_id,
            user_id=str(user_id),
            summary_id=event.workout_summary.id,
            status=result["status"],
            reason=result.get("reason"),
        )
        return result

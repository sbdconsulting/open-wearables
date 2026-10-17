"""Wahoo webhook endpoint at the URI registered on the Wahoo app.

The "Mybya Production" Wahoo app was approved with the webhook URI
``https://wearables.mybya.com/api/v1/webhooks/wahoo``, so the handler is served
there as well as at the unified ``/api/v1/providers/wahoo/webhooks``. Both
delegate to the same ``WahooWebhookHandler``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.database import DbSession
from app.services.providers.factory import ProviderFactory

router = APIRouter()

_strategy = ProviderFactory().get_provider("wahoo")
if _strategy.webhooks is None:
    raise RuntimeError("Wahoo webhook handler not initialised")
_handler = _strategy.webhooks


async def _read_body(request: Request) -> bytes:
    return await request.body()


@router.post("")
def wahoo_webhook_event(
    request: Request,
    db: DbSession,
    body: Annotated[bytes, Depends(_read_body)],
) -> dict:
    """Wahoo webhook event (POST): token check, parse, enqueue."""
    return _handler.handle(request, body, db)

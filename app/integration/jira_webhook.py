"""
ticket-to-code — Jira Webhook Verification & Parsing
───────────────────────────────────────────────────────
Provides a FastAPI dependency that:

    1. Optionally verifies incoming Jira webhook payloads using a shared
       secret token, if JIRA_WEBHOOK_SECRET is configured.
    2. Returns the raw body bytes for downstream parsing.

Jira Cloud webhooks support a secret token appended to the webhook URL:
    https://your-server/api/webhooks/jira?token=<your-secret>

Jira sends requests to this URL as-is, so we validate the `token`
query parameter against the configured secret.

If no secret is configured, we skip verification (dev mode).
In production you should always set JIRA_WEBHOOK_SECRET and configure
the Jira webhook URL to include ?token=<secret>.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def verify_jira_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> bytes:
    """
    FastAPI dependency that authenticates an incoming Jira webhook.

    If `JIRA_WEBHOOK_SECRET` is configured:
        - Validates the `token` query parameter against the secret.
        - Rejects requests with a missing or wrong token (403).

    If the secret is empty/unset:
        - Logs a warning and passes through (dev mode).

    Returns:
        The raw request body as `bytes`.
    """

    # ── 1. Read raw body ──────────────────────────────────────────────
    body: bytes = await request.body()

    # ── 2. Check if verification is enabled ───────────────────────────
    secret = settings.JIRA_WEBHOOK_SECRET
    if not secret:
        logger.warning(
            "JIRA_WEBHOOK_SECRET is not set — skipping signature verification. "
            "Set JIRA_WEBHOOK_SECRET and configure the Jira webhook URL as: "
            "/api/webhooks/jira?token=<your-secret>"
        )
        return body

    # ── 3. Get the token query parameter ─────────────────────────────
    # Jira Cloud appends the token to the webhook URL, e.g.:
    #   https://your-server/api/webhooks/jira?token=mysecret
    token = request.query_params.get("token", "")

    if not token:
        logger.warning("Jira webhook request missing ?token= query parameter.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing token query parameter.",
        )

    # ── 4. Constant-time comparison ──────────────────────────────────
    if not hmac.compare_digest(secret, token):
        logger.warning("Jira webhook token mismatch — rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook token.",
        )

    logger.debug("Jira webhook token verified successfully.")
    return body

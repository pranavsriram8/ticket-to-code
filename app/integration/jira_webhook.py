"""
ticket-to-code — Jira Webhook Verification & Parsing
───────────────────────────────────────────────────────
Provides a FastAPI dependency that:

    1. Optionally verifies incoming Jira webhook payloads using a shared
       secret (HMAC-SHA256), if JIRA_WEBHOOK_SECRET is configured.
    2. Returns the raw body bytes for downstream parsing.

Jira Cloud webhooks don't natively send HMAC signatures the same way
GitHub does.  However, if you set up Jira Automation or a proxy that
adds an `x-hub-signature-256` header (common pattern), we verify it.

If no secret is configured, we skip verification (useful during
development).  In production you should always set a secret or
restrict access by IP / API gateway.
"""

from __future__ import annotations

import hashlib
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
        - Reads the raw body.
        - Checks for an `x-hub-signature-256` header.
        - Verifies the HMAC-SHA256 digest.

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
            "Set this in production!"
        )
        return body

    # ── 3. Get the signature header ──────────────────────────────────
    signature_header: str | None = request.headers.get("x-hub-signature-256")

    if signature_header is None:
        logger.warning("Jira webhook request missing x-hub-signature-256 header.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing x-hub-signature-256 header.",
        )

    # ── 4. Compute expected HMAC ─────────────────────────────────────
    expected_signature = (
        "sha256="
        + hmac.new(
            key=secret.encode("utf-8"),
            msg=body,
            digestmod=hashlib.sha256,
        ).hexdigest()
    )

    # ── 5. Constant-time comparison ──────────────────────────────────
    if not hmac.compare_digest(expected_signature, signature_header):
        logger.warning("Jira webhook signature mismatch — rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature.",
        )

    logger.debug("Jira webhook signature verified successfully.")
    return body

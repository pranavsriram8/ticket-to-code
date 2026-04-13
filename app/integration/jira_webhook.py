"""
ticket-to-code — Jira Webhook Verification & Parsing
───────────────────────────────────────────────────────
Provides a FastAPI dependency that:

    1. Optionally verifies incoming Jira webhook payloads using either:
       a. Jira Cloud HMAC-SHA256 signature (X-Hub-Signature header), OR
       b. A shared token query parameter (?token=<secret>)
    2. Returns the raw body bytes for downstream parsing.

Jira Cloud webhooks support a "Secret" field in the webhook config.
When set, Jira signs the payload body with HMAC-SHA256 and sends the
signature in the `X-Hub-Signature` header as `sha256=<hex-digest>`.

Alternatively, you can append ?token=<secret> to the webhook URL for
simple token-based auth (useful for testing with ngrok, etc.).

If no secret is configured, we skip verification (dev mode).
In production you should always set JIRA_WEBHOOK_SECRET.
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

    Verification methods (in order of precedence):

    1. **HMAC signature** — If the request has an `X-Hub-Signature` header,
       validates it against the JIRA_WEBHOOK_SECRET using HMAC-SHA256.
       This is the standard Jira Cloud webhook signing mechanism.

    2. **Token query param** — If no signature header is present, falls back
       to checking `?token=<value>` against the secret.

    3. **Dev mode** — If JIRA_WEBHOOK_SECRET is not set, skips all checks.

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
            "Set JIRA_WEBHOOK_SECRET in production."
        )
        return body

    # ── 3. Try HMAC signature verification (Jira Cloud native) ────────
    signature_header = request.headers.get("X-Hub-Signature")
    if signature_header:
        # Jira sends: sha256=<hex digest>
        if signature_header.startswith("sha256="):
            expected_sig = signature_header[7:]  # strip "sha256=" prefix
        else:
            expected_sig = signature_header

        computed_sig = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(computed_sig, expected_sig):
            logger.debug("Jira webhook HMAC-SHA256 signature verified.")
            return body
        else:
            logger.warning("Jira webhook HMAC signature mismatch — rejecting.")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid webhook signature.",
            )

    # ── 4. Fallback: token query parameter ────────────────────────────
    token = request.query_params.get("token", "")
    if token and hmac.compare_digest(secret, token):
        logger.debug("Jira webhook token query param verified.")
        return body

    # ── 5. Neither method succeeded ───────────────────────────────────
    logger.warning(
        "Jira webhook request has no valid X-Hub-Signature header "
        "and no valid ?token= query parameter — rejecting."
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Missing or invalid webhook authentication.",
    )

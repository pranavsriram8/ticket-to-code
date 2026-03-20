"""
ticket-to-code — GitHub Webhook Signature Verification
─────────────────────────────────────────────────────────
Provides a FastAPI dependency that reads the raw request body, computes
an HMAC-SHA256 digest using the configured secret, and compares it to
the `x-hub-signature-256` header sent by GitHub.

If verification fails the request is immediately rejected with 403.

Usage in a route:
    @app.post("/webhook")
    async def handle(payload: dict = Depends(verify_github_signature)):
        ...
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def verify_github_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> bytes:
    """
    FastAPI dependency that authenticates an incoming GitHub webhook.

    Steps:
        1. Read the raw request body (bytes).
        2. Extract the `x-hub-signature-256` header.
        3. Compute HMAC-SHA256(secret, body) and compare using
           `hmac.compare_digest` (constant-time comparison to prevent
           timing attacks).
        4. Return the raw body bytes so downstream code can parse it.

    Raises:
        HTTPException 403  – if the header is missing or the digest
                              doesn't match.

    Returns:
        The raw request body as `bytes` (so the route can deserialise
        it into a Pydantic model).
    """

    # ── 1. Read raw body ──────────────────────────────────────────────
    body: bytes = await request.body()

    # ── 2. Dev-mode bypass (matches Jira webhook pattern) ─────────────
    # If no secret is configured, skip verification with a warning.
    # In production you should always set GITHUB_WEBHOOK_SECRET.
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set — skipping signature verification. "
            "Set this in production!"
        )
        return body

    # ── 3. Get the signature header ──────────────────────────────────
    signature_header: str | None = request.headers.get("x-hub-signature-256")

    if signature_header is None:
        logger.warning("Webhook request missing x-hub-signature-256 header.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing x-hub-signature-256 header.",
        )

    # ── 3. Compute expected HMAC ─────────────────────────────────────
    # GitHub sends the header in the format: "sha256=<hex-digest>"
    expected_signature = (
        "sha256="
        + hmac.new(
            key=settings.GITHUB_WEBHOOK_SECRET.encode("utf-8"),
            msg=body,
            digestmod=hashlib.sha256,
        ).hexdigest()
    )

    # ── 4. Constant-time comparison ──────────────────────────────────
    if not hmac.compare_digest(expected_signature, signature_header):
        logger.warning("Webhook signature mismatch — rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature.",
        )

    logger.debug("Webhook signature verified successfully.")
    return body

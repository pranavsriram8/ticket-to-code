"""
ticket-to-code — FastAPI Application Entry Point
───────────────────────────────────────────────────
Exposes webhook endpoints for both Jira and GitHub, wires them through
the Coordination Layer (DSPy router → GitHub Actions dispatch), and
returns immediate 202 responses so external services don't time out.

Routes:
    GET  /                       — Service info
    GET  /health                 — Liveness probe
    POST /api/webhooks/jira      — Jira issue event receiver
    POST /api/webhooks/github    — GitHub issue event receiver (legacy)

Run with:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, Depends, FastAPI, Response, status

from app.coordination.dispatcher import process_ticket_task
from app.core.config import Settings, get_settings
from app.integration.github_models import IssueWebhookPayload
from app.integration.github_webhook import verify_github_signature
from app.integration.jira_models import JiraWebhookPayload
from app.integration.jira_webhook import verify_jira_webhook

# ── Logger ────────────────────────────────────────────────────────────
logger = logging.getLogger("ticket-to-code")


# ── Application Lifespan ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure logging and print a startup banner."""
    settings = get_settings()

    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("🚀 ticket-to-code is starting up …")
    logger.info("   LLM model:           %s", settings.LITELLM_MODEL)
    logger.info("   Target repo:         %s", settings.GITHUB_REPO or "(not set)")
    logger.info("   Ticket label:        '%s'", settings.TICKET_LABEL)
    logger.info("   Workflow file:       %s", settings.TICKET_WORKFLOW_FILE)

    yield

    logger.info("🚀 ticket-to-code is shutting down. Goodbye!")


# ── FastAPI App ───────────────────────────────────────────────────────
app = FastAPI(
    title="ticket-to-code",
    description=(
        "AI-powered engineering assistant that picks up Jira tasks, "
        "generates execution plans with DSPy/LiteLLM, and dispatches "
        "them to GitHub Actions — opening a PR for human review."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Operational Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.get("/", summary="Root — service info", tags=["ops"])
async def root() -> dict[str, str]:
    """Returns basic service info. Visit /docs for interactive API docs."""
    return {
        "service": "ticket-to-code",
        "version": "0.2.0",
        "docs": "/docs",
        "health": "/health",
        "jira_webhook": "POST /api/webhooks/jira",
        "github_webhook": "POST /api/webhooks/github",
    }


@app.get("/health", summary="Health check", tags=["ops"])
async def health_check() -> dict[str, str]:
    """Simple liveness probe — returns 200 if the server is running."""
    return {"status": "ok"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dry Run — Route + Scope only (no GitHub Actions dispatch)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from pydantic import BaseModel


class DryRunRequest(BaseModel):
    task_title: str
    task_description: str = ""


@app.post("/api/dry-run", summary="Dry run: Route + Scope (no dispatch)", tags=["testing"])
async def dry_run(req: DryRunRequest) -> dict:
    """
    Runs the full Router + Scope Identifier pipeline but does NOT dispatch
    to GitHub Actions. Returns the plan + identified target paths.

    Use this to test that scope identification finds the right files.
    """
    import asyncio
    from app.coordination.dispatcher import _extract_template_paths
    from app.coordination.router import VALID_TASK_TYPES, get_router
    from app.coordination.scope_identifier import identify_scope

    # Step 1: Route
    router = get_router()
    prediction = await asyncio.to_thread(
        router.route,
        task_title=req.task_title,
        task_description=req.task_description,
    )

    task_type = prediction.task_type.strip().lower()
    if task_type not in VALID_TASK_TYPES:
        task_type = "unknown"

    # Step 2: Extract seed paths from template (if present)
    seed_paths = _extract_template_paths(req.task_description)

    # Step 3: Scope identification
    target_paths = await asyncio.to_thread(
        identify_scope,
        task_title=req.task_title,
        task_type=task_type,
        plan=prediction.plan,
        seed_paths=seed_paths,
    )

    return {
        "task_type": task_type,
        "router_target_paths": prediction.target_paths,
        "scope_identified_paths": target_paths,
        "plan": prediction.plan,
        "validation_commands": prediction.safe_validation_commands,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Jira Webhook (Primary Trigger)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.post(
    "/api/webhooks/jira",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive Jira webhook events",
    tags=["webhooks"],
)
async def jira_webhook(
    background_tasks: BackgroundTasks,
    raw_body: bytes = Depends(verify_jira_webhook),
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Handle incoming Jira issue events.

    Trigger criteria:
        1. Event is `jira:issue_created` and the issue has the ticket label.
        2. Event is `jira:issue_updated` and the changelog shows the ticket
           label was just added.

    When triggered:
        - Dispatches `process_ticket_task` as a background task.
        - The pipeline: Router (DSPy) → Plan → GitHub Actions dispatch.
        - Returns 202 Accepted immediately.
    """

    # ── Parse the payload ─────────────────────────────────────────────
    try:
        payload_dict = json.loads(raw_body)
        payload = JiraWebhookPayload.model_validate(payload_dict)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Failed to parse Jira webhook payload: %s", exc)
        return Response(
            content="Payload could not be parsed.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    issue = payload.issue
    fields = issue.fields
    event = payload.webhook_event

    logger.info(
        "Received Jira event: event=%s issue=%s summary='%s'",
        event,
        issue.key,
        fields.summary,
    )

    # ── Determine if this event should trigger a TicketToCode task ────
    ticket_label: str = settings.TICKET_LABEL
    should_dispatch: bool = False

    if event == "jira:issue_created":
        # New issue created — check if it already has our label.
        if ticket_label in fields.labels:
            should_dispatch = True
            logger.info(
                "Issue %s was created with '%s' label — dispatching.",
                issue.key,
                ticket_label,
            )

    elif event == "jira:issue_updated":
        # Issue updated — check if our label was just added via changelog.
        if payload.changelog:
            for item in payload.changelog.items:
                if item.field == "labels" and item.to_string:
                    # Jira sends the label change as a space-separated string
                    # of all current labels. Check if ours is present and
                    # wasn't before.
                    new_labels = item.to_string.split()
                    old_labels = (item.from_string or "").split()
                    if ticket_label in new_labels and ticket_label not in old_labels:
                        should_dispatch = True
                        logger.info(
                            "Issue %s had '%s' label added — dispatching.",
                            issue.key,
                            ticket_label,
                        )
                        break

        # Fallback: even without changelog, check if the issue has the label
        # and the event looks like a label change.
        if not should_dispatch and ticket_label in fields.labels:
            # Only dispatch if we see the label; avoids duplicate processing
            # since we check the changelog first.
            logger.debug(
                "Issue %s has '%s' label but no matching changelog entry. "
                "Skipping to avoid duplicate dispatch.",
                issue.key,
                ticket_label,
            )

    # ── Dispatch or skip ──────────────────────────────────────────────
    if should_dispatch:
        background_tasks.add_task(
            process_ticket_task,
            jira_issue_key=issue.key,
            task_title=fields.summary,
            task_description=fields.description or "",
            source_platform="jira",
        )
        return Response(
            content=f"TicketToCode task dispatched for {issue.key}.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    logger.debug(
        "Jira event '%s' for %s does not match trigger criteria — ignoring.",
        event,
        issue.key,
    )
    return Response(
        content="Event acknowledged, no action taken.",
        status_code=status.HTTP_202_ACCEPTED,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GitHub Webhook (Legacy / Alternate Trigger)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.post(
    "/api/webhooks/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive GitHub webhook events",
    tags=["webhooks"],
)
async def github_webhook(
    background_tasks: BackgroundTasks,
    raw_body: bytes = Depends(verify_github_signature),
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Handle GitHub issue events (alternative to Jira).

    Triggers on:
        - Issue "labeled" with the ticket label.
        - Issue "opened" with the ticket label already attached.
    """

    # ── Parse the payload ─────────────────────────────────────────────
    try:
        payload_dict = json.loads(raw_body)
        payload = IssueWebhookPayload.model_validate(payload_dict)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Failed to parse GitHub webhook payload: %s", exc)
        return Response(
            content="Payload could not be parsed.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    logger.info(
        "Received GitHub event: action=%s repo=%s issue=#%d",
        payload.action,
        payload.repository.full_name,
        payload.issue.number,
    )

    # ── Trigger criteria ──────────────────────────────────────────────
    ticket_label: str = settings.TICKET_LABEL
    should_dispatch: bool = False

    if payload.action == "labeled":
        if payload.label and payload.label.name == ticket_label:
            should_dispatch = True

    elif payload.action == "opened":
        issue_label_names = [lbl.name for lbl in payload.issue.labels]
        if ticket_label in issue_label_names:
            should_dispatch = True

    # ── Dispatch or skip ──────────────────────────────────────────────
    if should_dispatch:
        # Use the repo name + issue number as a pseudo "issue key"
        # for GitHub-originated tasks.
        issue_key = f"{payload.repository.full_name}#{payload.issue.number}"

        background_tasks.add_task(
            process_ticket_task,
            jira_issue_key=issue_key,
            task_title=payload.issue.title,
            task_description=payload.issue.body or "",
            source_platform="github",
        )
        return Response(
            content="TicketToCode task dispatched.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    return Response(
        content="Event acknowledged, no action taken.",
        status_code=status.HTTP_202_ACCEPTED,
    )

"""
ticket-to-code — Coordination Layer: Task Dispatcher
───────────────────────────────────────────────────────
The central orchestrator that ties together:

    1. Smart Router (DSPy)  →  classifies the task & generates a plan.
    2. Code Executor        →  dispatches the plan to the configured code
                                platform (GitHub, Bitbucket, GitLab, etc.)
                                for safe execution.

This module is called by the FastAPI route handlers when a qualifying
Jira (or GitHub) event is received.

The executor is selected at runtime via the factory, based on the
REPO_PLATFORM configuration setting. This makes the dispatcher
completely platform-agnostic.
"""

import asyncio
import logging

from app.coordination.router import VALID_TASK_TYPES, get_router
from app.execution.base import ExecutionPlan, ExecutionResult
from app.execution.factory import get_executor

logger = logging.getLogger(__name__)


async def process_ticket_task(
    jira_issue_key: str,
    task_title: str,
    task_description: str,
    source_platform: str = "jira",
) -> ExecutionResult | None:
    """
    Full TicketToCode pipeline: Route → Plan → Execute.

    Steps:
        1. Use the DSPy TaskRouter to classify the task and generate
           a step-by-step plan.
        2. Package the plan into an ExecutionPlan.
        3. Dispatch to the configured code platform via the executor factory.

    This function is designed to run as a FastAPI BackgroundTask so the
    webhook response is returned immediately (202 Accepted).

    Args:
        jira_issue_key:   The issue key (e.g., "INFRA-42", "your-org/mono#123").
        task_title:       The issue summary / title.
        task_description: The full issue description.
        source_platform:  Where the ticket came from ("jira", "github", "asana", etc.).

    Returns:
        ExecutionResult if the pipeline completed, None if it failed early.
    """

    logger.info(
        "Starting TicketToCode pipeline for %s: '%s' (source: %s)",
        jira_issue_key,
        task_title,
        source_platform,
    )

    # ── Step 1: Route & Plan (sync LLM call — run in thread pool) ─────
    # DSPy/LiteLLM are synchronous — running them directly in an async
    # function would block the entire FastAPI event loop. We offload to
    # a worker thread so other requests remain responsive.
    try:
        router = get_router()
        prediction = await asyncio.to_thread(
            router.route,
            task_title=task_title,
            task_description=task_description,
        )
    except Exception as exc:
        logger.error(
            "❌ Router failed for %s: %s", jira_issue_key, exc, exc_info=True
        )
        return None

    # ── Validate task_type from LLM output ───────────────────────────
    task_type = prediction.task_type.strip().lower()
    if task_type not in VALID_TASK_TYPES:
        logger.warning(
            "LLM returned unknown task_type '%s' for %s — falling back to 'unknown'.",
            task_type,
            jira_issue_key,
        )
        task_type = "unknown"

    logger.info(
        "📋 Plan generated for %s — type=%s, paths=%s",
        jira_issue_key,
        task_type,
        prediction.target_paths,
    )

    # ── Step 2: Build platform-agnostic execution plan ────────────────
    plan = ExecutionPlan(
        issue_key=jira_issue_key,
        task_title=task_title,
        task_description=task_description,
        task_type=task_type,
        target_paths=prediction.target_paths,
        plan=prediction.plan,
        validation_commands=prediction.safe_validation_commands,
        source_platform=source_platform,
    )

    # ── Step 3: Execute via the configured code platform ──────────────
    # The factory selects the right executor (GitHub, Bitbucket, etc.)
    # based on the REPO_PLATFORM config setting.
    try:
        executor = get_executor()
        result: ExecutionResult = await executor.execute(plan)
    except Exception as exc:
        logger.error(
            "❌ Executor failed for %s: %s", jira_issue_key, exc, exc_info=True
        )
        return None

    if result.success:
        logger.info(
            "✅ %s executor completed for %s. PR: %s, Workflow: %s",
            result.platform,
            jira_issue_key,
            result.pr_url or "(pending)",
            result.workflow_url or "(none)",
        )
    else:
        logger.error(
            "❌ %s executor failed for %s: %s",
            result.platform,
            jira_issue_key,
            result.error_message,
        )

    # ── Future: Update ticket system with results ─────────────────────
    # TODO (Phase 3): Post the plan + PR link back to the originating
    # ticket system (Jira comment, GitHub issue comment, etc.) and
    # transition the ticket to "In Progress".

    return result

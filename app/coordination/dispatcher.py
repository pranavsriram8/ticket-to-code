"""
ticket-to-code — Coordination Layer: Task Dispatcher
───────────────────────────────────────────────────────
The central orchestrator that ties together:

    1. Smart Router (DSPy)      →  classifies the task & generates a plan.
    2. Scope Identifier         →  uses GitHub Trees API + LLM to find
                                    exact file paths that need changing.
    3. Code Executor            →  dispatches the plan to the configured code
                                    platform (GitHub Actions, etc.) for execution.

This module is called by the FastAPI route handlers when a qualifying
Jira (or GitHub) event is received.

Architecture:
    Agent 1 (this server):  ALL the intelligence — routing, planning, scope ID
    Agent 2 (GitHub Actions): pure executor — reads files, edits, validates, PRs
"""

import asyncio
import logging

from app.coordination.router import VALID_TASK_TYPES, get_router
from app.coordination.scope_identifier import identify_scope
from app.core.config import get_settings
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
    Full TicketToCode pipeline: Route → Scope → Execute.

    Steps:
        1. Use the DSPy TaskRouter to classify the task and generate
           a step-by-step plan.
        2. Use the Scope Identifier (GitHub Trees API + LLM) to find
           the exact file paths that need changing.
        3. Dispatch to the configured code platform via the executor factory.

    This function is designed to run as a FastAPI BackgroundTask so the
    webhook response is returned immediately (202 Accepted).

    Args:
        jira_issue_key:   The issue key (e.g., "INFRA-42").
        task_title:       The issue summary / title.
        task_description: The full issue description.
        source_platform:  Where the ticket came from ("jira", "github", etc.).

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
        "📋 Router output for %s — type=%s, plan length=%d chars",
        jira_issue_key,
        task_type,
        len(prediction.plan),
    )

    # ── Step 2: Scope Identification (GitHub Trees API + LLM) ─────────
    # Fetches the full repo file tree via API (no clone needed), then
    # asks the LLM which files need to change based on the plan.
    try:
        target_paths = await asyncio.to_thread(
            identify_scope,
            task_title=task_title,
            task_type=task_type,
            plan=prediction.plan,
        )
    except Exception as exc:
        logger.error(
            "❌ Scope identification failed for %s: %s", jira_issue_key, exc, exc_info=True
        )
        # Fall back to router's guessed paths (may be inaccurate)
        target_paths = prediction.target_paths
        logger.warning("⚠️  Falling back to router paths: %s", target_paths)

    if not target_paths:
        logger.error("❌ No target paths identified for %s — cannot proceed.", jira_issue_key)
        return None

    logger.info(
        "🎯 Scope identified for %s — paths: %s",
        jira_issue_key,
        target_paths,
    )

    # ── Step 3: Build platform-agnostic execution plan ────────────────
    plan = ExecutionPlan(
        issue_key=jira_issue_key,
        task_title=task_title,
        task_description=task_description,
        task_type=task_type,
        target_paths=target_paths,
        plan=prediction.plan,
        validation_commands="",
        source_platform=source_platform,
    )

    # ── Step 4: Execute via the configured code platform ──────────────
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

    return result

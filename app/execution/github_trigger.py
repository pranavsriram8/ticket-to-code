"""
ticket-to-code — GitHub Actions Workflow Trigger
──────────────────────────────────────────────────
Uses PyGithub to dispatch a `workflow_dispatch` event on the target
monorepo, passing the task plan and metadata as workflow inputs.

This is how TicketToCode hands off work from the Coordination Layer to
the Execution Layer (a GitHub Actions runner with the right credentials).

The target workflow (`agent-ticket-to-code.yml`) is expected to accept these inputs:
    - jira_issue_key:   e.g. "INFRA-42"
    - task_type:        e.g. "terraform"
    - target_paths:     comma-separated paths in the monorepo
    - plan:             the step-by-step execution plan
    - validation_commands: safe commands to run after edits

Reference:
    https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
    https://pygithub.readthedocs.io/en/stable/github_objects/Workflow.html
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from github import Auth, Github

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ── Data class for structured trigger inputs ──────────────────────────


@dataclass
class WorkflowInputs:
    """
    The inputs we pass to the GitHub Actions `workflow_dispatch` event.

    These map 1-to-1 to the `inputs:` block in the workflow YAML.
    """

    jira_issue_key: str
    task_title: str
    task_type: str
    target_paths: str
    plan: str
    validation_commands: str


# ── Trigger Function ──────────────────────────────────────────────────


def trigger_agent_workflow(
    inputs: WorkflowInputs,
    settings: Settings | None = None,
) -> bool:
    """
    Dispatch the TicketToCode agent workflow on the monorepo.

    Steps:
        1. Authenticate with GitHub using the PAT.
        2. Find the target repository.
        3. Locate the workflow file (e.g., `agent-ticket-to-code.yml`).
        4. Trigger `workflow_dispatch` on the default branch with
           the task plan as inputs.

    Args:
        inputs:   The structured data to pass to the workflow.
        settings: App settings (injected or loaded from env).

    Returns:
        True if the dispatch was successful, False otherwise.
    """
    if settings is None:
        settings = get_settings()

    # ── Validate configuration ────────────────────────────────────────
    if not settings.GITHUB_PAT:
        logger.error(
            "GITHUB_PAT is not configured. Cannot trigger workflow dispatch."
        )
        return False

    if not settings.GITHUB_REPO:
        logger.error(
            "GITHUB_REPO is not configured. Cannot trigger workflow dispatch."
        )
        return False

    gh: Optional[Github] = None
    try:
        # ── 1. Authenticate ──────────────────────────────────────────
        auth = Auth.Token(settings.GITHUB_PAT)
        gh = Github(auth=auth)

        # ── 2. Get the repository ────────────────────────────────────
        repo = gh.get_repo(settings.GITHUB_REPO)
        logger.info("Connected to repository: %s", repo.full_name)

        # ── 3. Find the workflow ─────────────────────────────────────
        workflow = repo.get_workflow(settings.TICKET_WORKFLOW_FILE)
        logger.info("Found workflow: %s (id=%s)", workflow.name, workflow.id)

        # ── 4. Dispatch the workflow ─────────────────────────────────
        # GitHub Actions workflow_dispatch inputs must be a dict of strings.
        dispatch_inputs = {
            "jira_issue_key": inputs.jira_issue_key,
            "task_title": inputs.task_title,
            "task_type": inputs.task_type,
            "target_paths": inputs.target_paths,
            "plan": inputs.plan,
            "validation_commands": inputs.validation_commands,
        }

        # Dispatch on the repo's default branch (usually "main").
        success = workflow.create_dispatch(
            ref=repo.default_branch,
            inputs=dispatch_inputs,
        )

        if success:
            logger.info(
                "✅ Workflow dispatched for %s on branch '%s'.",
                inputs.jira_issue_key,
                repo.default_branch,
            )
        else:
            logger.warning(
                "⚠️ Workflow dispatch returned False for %s.",
                inputs.jira_issue_key,
            )

        return bool(success)

    except Exception as exc:
        logger.error(
            "Failed to trigger workflow for %s: %s",
            inputs.jira_issue_key,
            exc,
            exc_info=True,
        )
        return False

    finally:
        # Only close if the client was successfully created.
        if gh is not None:
            gh.close()

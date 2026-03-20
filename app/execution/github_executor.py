"""
ticket-to-code — GitHub Code Executor
───────────────────────────────────────
Implements the CodeExecutor interface for GitHub.

Strategy: Dispatches a `workflow_dispatch` event on the target monorepo,
passing the execution plan as workflow inputs. The GitHub Actions workflow
(`agent-ticket-to-code.yml`) handles the actual code changes, branch
creation, and PR opening.

This is a "delegate" executor — it doesn't modify code directly but
hands off to a CI/CD workflow that does the heavy lifting with the
right repo credentials and environment.

Reference:
    https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from github import Auth, Github

from app.core.config import Settings, get_settings
from app.execution.base import CodeExecutor, ExecutionPlan, ExecutionResult

logger = logging.getLogger(__name__)


class GitHubExecutor(CodeExecutor):
    """
    GitHub implementation of CodeExecutor.

    Uses PyGithub to dispatch a workflow_dispatch event, passing the
    task plan and metadata as workflow inputs. The actual code changes
    happen inside the GitHub Actions runner.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def platform_name(self) -> str:
        return "github"

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        Dispatch the agent workflow on the target GitHub repository.

        Args:
            plan: The execution plan from the DSPy Router.

        Returns:
            ExecutionResult with dispatch status.
        """
        logger.info(
            "GitHubExecutor: dispatching workflow for %s on %s",
            plan.issue_key,
            self._settings.GITHUB_REPO,
        )

        # PyGithub is synchronous — run in thread pool to avoid
        # blocking the FastAPI event loop.
        result = await asyncio.to_thread(self._dispatch_workflow, plan)
        return result

    async def health_check(self) -> bool:
        """Verify GitHub PAT and repo are configured and reachable."""
        if not self._settings.GITHUB_PAT:
            logger.error("GITHUB_PAT is not configured.")
            return False
        if not self._settings.GITHUB_REPO:
            logger.error("GITHUB_REPO is not configured.")
            return False

        try:
            auth = Auth.Token(self._settings.GITHUB_PAT)
            gh = Github(auth=auth)
            repo = gh.get_repo(self._settings.GITHUB_REPO)
            logger.info("GitHub health check passed: %s", repo.full_name)
            gh.close()
            return True
        except Exception as exc:
            logger.error("GitHub health check failed: %s", exc)
            return False

    # ── Private helpers ───────────────────────────────────────────────

    def _dispatch_workflow(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        Synchronous workflow dispatch (runs in thread pool).

        Steps:
            1. Authenticate with GitHub using the PAT.
            2. Find the target repository.
            3. Locate the workflow file.
            4. Trigger workflow_dispatch with the plan as inputs.
        """
        settings = self._settings

        if not settings.GITHUB_PAT:
            return ExecutionResult(
                success=False,
                platform=self.platform_name,
                error_message="GITHUB_PAT is not configured.",
            )

        if not settings.GITHUB_REPO:
            return ExecutionResult(
                success=False,
                platform=self.platform_name,
                error_message="GITHUB_REPO is not configured.",
            )

        gh: Optional[Github] = None
        try:
            # ── 1. Authenticate ──────────────────────────────────────
            auth = Auth.Token(settings.GITHUB_PAT)
            gh = Github(auth=auth)

            # ── 2. Get the repository ────────────────────────────────
            repo = gh.get_repo(settings.GITHUB_REPO)
            logger.info("Connected to repository: %s", repo.full_name)

            # ── 3. Find the workflow ─────────────────────────────────
            workflow = repo.get_workflow(settings.TICKET_WORKFLOW_FILE)
            logger.info("Found workflow: %s (id=%s)", workflow.name, workflow.id)

            # ── 4. Dispatch the workflow ─────────────────────────────
            dispatch_inputs = {
                "jira_issue_key": plan.issue_key,
                "task_title": plan.task_title,
                "task_type": plan.task_type,
                "target_paths": plan.target_paths,
                "plan": plan.plan,
                "validation_commands": plan.validation_commands,
            }

            success = workflow.create_dispatch(
                ref=repo.default_branch,
                inputs=dispatch_inputs,
            )

            if success:
                logger.info(
                    "✅ Workflow dispatched for %s on branch '%s'.",
                    plan.issue_key,
                    repo.default_branch,
                )
                return ExecutionResult(
                    success=True,
                    platform=self.platform_name,
                    workflow_url=(
                        f"https://github.com/{settings.GITHUB_REPO}"
                        f"/actions/workflows/{settings.TICKET_WORKFLOW_FILE}"
                    ),
                )
            else:
                logger.warning(
                    "⚠️ Workflow dispatch returned False for %s.",
                    plan.issue_key,
                )
                return ExecutionResult(
                    success=False,
                    platform=self.platform_name,
                    error_message="Workflow dispatch returned False.",
                )

        except Exception as exc:
            logger.error(
                "Failed to trigger workflow for %s: %s",
                plan.issue_key,
                exc,
                exc_info=True,
            )
            return ExecutionResult(
                success=False,
                platform=self.platform_name,
                error_message=str(exc),
            )

        finally:
            if gh is not None:
                gh.close()

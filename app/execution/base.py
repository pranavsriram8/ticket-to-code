"""
ticket-to-code — Abstract Code Executor Interface
────────────────────────────────────────────────────
Defines the common interface that all code platform integrations
(GitHub, Bitbucket, GitLab, Azure DevOps, etc.) must implement.

The Coordination Layer uses this interface to dispatch execution plans
without knowing which platform is being used — the concrete implementation
is selected at runtime via the executor factory.

To add a new platform:
    1. Create `app/execution/{platform}_executor.py`
    2. Implement the `CodeExecutor` protocol
    3. Register it in `app/execution/factory.py`
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Shared Data Models ────────────────────────────────────────────────


@dataclass
class ExecutionPlan:
    """
    Platform-agnostic representation of a task to execute.

    This is what the Coordination Layer produces (via DSPy Router)
    and what the Execution Layer consumes. It contains everything
    needed to create a branch, apply changes, and open a PR/MR.
    """

    # ── Task identity ─────────────────────────────────────────────────
    issue_key: str              # e.g., "INFRA-42", "your-org/mono#123"
    task_title: str             # Human-readable summary
    task_description: str = ""  # Full description from the ticket

    # ── Router output ─────────────────────────────────────────────────
    task_type: str = "unknown"          # terraform, helm, ansible, service, script, unknown
    target_paths: str = ""              # Comma-separated file paths
    plan: str = ""                      # Step-by-step execution plan
    validation_commands: str = ""       # Safe commands to validate changes

    # ── Execution metadata ────────────────────────────────────────────
    source_platform: str = ""   # "jira", "github", "asana", "notion", etc.
    labels: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    """
    The outcome of executing a plan on a code platform.

    Returned by the executor so the Coordination Layer can report
    back to the ticket system (e.g., post a Jira comment with the PR link).
    """

    success: bool
    platform: str = ""          # "github", "bitbucket", "gitlab", etc.
    pr_url: Optional[str] = None        # URL of the created PR/MR
    branch_name: Optional[str] = None   # Branch that was created
    workflow_url: Optional[str] = None  # URL of the CI/CD run
    error_message: Optional[str] = None # Error details if failed


# ── Abstract Base Class ───────────────────────────────────────────────


class CodeExecutor(ABC):
    """
    Abstract interface for code platform integrations.

    Each platform (GitHub, Bitbucket, GitLab, etc.) implements this
    interface. The Coordination Layer calls `execute()` without
    knowing which platform is behind it.

    Implementations can choose their own strategy:
        - GitHub:     Dispatch a workflow_dispatch event (current approach)
        - Bitbucket:  Use Bitbucket Pipelines API
        - GitLab:     Trigger a CI pipeline via API
        - Direct:     Clone repo, apply changes, push, create PR via API

    The simplest implementations only need `execute()`. The other
    methods (`create_branch`, `create_pull_request`) are optional
    hooks for platforms that support a more granular workflow.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform identifier (e.g., 'github', 'bitbucket')."""
        ...

    @abstractmethod
    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        """
        Execute a task plan on the code platform.

        This is the main entry point. Implementations should:
            1. Create a branch (or trigger a workflow that does).
            2. Apply the planned changes.
            3. Open a PR/MR for review.
            4. Return the result with PR URL and status.

        Args:
            plan: The execution plan from the DSPy Router.

        Returns:
            ExecutionResult with success status, PR URL, etc.
        """
        ...

    async def health_check(self) -> bool:
        """
        Verify the executor can reach the code platform.

        Override this to add platform-specific connectivity checks
        (e.g., verify PAT is valid, repo exists, etc.).

        Returns:
            True if the platform is reachable and configured.
        """
        return True

"""
ticket-to-code — Executor Factory
───────────────────────────────────
Selects the correct CodeExecutor implementation based on configuration.

The factory reads the `REPO_PLATFORM` setting and returns the appropriate
executor. This is the single place where new platform support is wired in.

To add a new platform:
    1. Create `app/execution/{platform}_executor.py`
    2. Implement the `CodeExecutor` interface
    3. Add an entry to `_EXECUTOR_REGISTRY` below
    4. Add the platform name to `SUPPORTED_PLATFORMS`
"""

from __future__ import annotations

import logging
from typing import Callable

from app.core.config import Settings, get_settings
from app.execution.base import CodeExecutor

logger = logging.getLogger(__name__)

# ── Registry of available executors ───────────────────────────────────
# Maps platform name → lazy factory function (avoids importing all
# implementations at module load time).

_EXECUTOR_REGISTRY: dict[str, Callable[[Settings], CodeExecutor]] = {}

SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"github"})


def _register_defaults() -> None:
    """Register the built-in executor implementations."""

    def _make_github(settings: Settings) -> CodeExecutor:
        from app.execution.github_executor import GitHubExecutor
        return GitHubExecutor(settings=settings)

    _EXECUTOR_REGISTRY["github"] = _make_github

    # ── Future platforms ──────────────────────────────────────────────
    # def _make_bitbucket(settings: Settings) -> CodeExecutor:
    #     from app.execution.bitbucket_executor import BitbucketExecutor
    #     return BitbucketExecutor(settings=settings)
    # _EXECUTOR_REGISTRY["bitbucket"] = _make_bitbucket

    # def _make_gitlab(settings: Settings) -> CodeExecutor:
    #     from app.execution.gitlab_executor import GitLabExecutor
    #     return GitLabExecutor(settings=settings)
    # _EXECUTOR_REGISTRY["gitlab"] = _make_gitlab


_register_defaults()


# ── Factory Function ──────────────────────────────────────────────────

_executor_instance: CodeExecutor | None = None


def get_executor(settings: Settings | None = None) -> CodeExecutor:
    """
    Return a CodeExecutor for the configured platform.

    Uses a module-level singleton — the executor is created once on
    first call and reused. This matches the pattern used by the
    TaskRouter singleton.

    The platform is determined by the `REPO_PLATFORM` config setting:
        - "github"    → GitHubExecutor (workflow_dispatch)
        - "bitbucket" → BitbucketExecutor (future)
        - "gitlab"    → GitLabExecutor (future)

    Args:
        settings: Optional settings override (useful for testing).

    Returns:
        A CodeExecutor instance for the configured platform.

    Raises:
        ValueError: If the configured platform is not supported.
    """
    global _executor_instance

    if _executor_instance is not None:
        return _executor_instance

    if settings is None:
        settings = get_settings()

    platform = settings.REPO_PLATFORM.strip().lower()

    if platform not in _EXECUTOR_REGISTRY:
        supported = ", ".join(sorted(_EXECUTOR_REGISTRY.keys()))
        raise ValueError(
            f"Unsupported REPO_PLATFORM: '{platform}'. "
            f"Supported platforms: {supported}"
        )

    logger.info("Creating CodeExecutor for platform: %s", platform)
    _executor_instance = _EXECUTOR_REGISTRY[platform](settings)
    return _executor_instance


def register_executor(
    platform: str,
    factory_fn: Callable[[Settings], CodeExecutor],
) -> None:
    """
    Register a custom executor at runtime.

    This allows plugins or extensions to add support for new platforms
    without modifying the factory module.

    Args:
        platform:   Platform name (e.g., "bitbucket").
        factory_fn: Callable that takes Settings and returns a CodeExecutor.
    """
    logger.info("Registering custom executor for platform: %s", platform)
    _EXECUTOR_REGISTRY[platform] = factory_fn

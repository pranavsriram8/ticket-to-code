"""
ticket-to-code — Execution Layer
──────────────────────────────────
Handles dispatching execution plans to code platforms (GitHub, Bitbucket,
GitLab, etc.) via a pluggable executor architecture.

Key components:
    - base.py             — Abstract CodeExecutor interface + shared data models
    - factory.py          — Selects the right executor based on REPO_PLATFORM
    - github_executor.py  — GitHub implementation (workflow_dispatch)
    - github_trigger.py   — Legacy direct trigger (kept for backwards compat)

Usage:
    from app.execution.factory import get_executor
    from app.execution.base import ExecutionPlan

    executor = get_executor()
    result = await executor.execute(plan)
"""

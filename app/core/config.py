"""
ticket-to-code — Application Configuration
─────────────────────────────────────────────
Uses pydantic-settings to load and validate environment variables.
Values are read from a `.env` file (if present) or from the OS environment.

Usage:
    from app.core.config import get_settings
    settings = get_settings()
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration object for ticket-to-code.

    All fields map 1-to-1 to environment variables (case-insensitive).
    Secrets are loaded from `.env` in the project root.
    """

    # ── GitHub Integration ────────────────────────────────────────────
    # Shared secret for verifying GitHub webhook HMAC-SHA256 signatures.
    GITHUB_WEBHOOK_SECRET: str = ""

    # GitHub App ID (for future authenticated API calls).
    GITHUB_APP_ID: int = 0

    # Personal Access Token — needs `repo` + `actions` scopes to trigger
    # workflow_dispatch and create pull requests.
    GITHUB_PAT: str = ""

    # The "owner/repo" identifier of the target monorepo.
    GITHUB_REPO: str = ""

    # ── Jira Integration ──────────────────────────────────────────────
    # Base URL of the Jira instance (e.g., https://acme.atlassian.net).
    JIRA_BASE_URL: str = ""

    # Jira Cloud authentication: email + API token.
    JIRA_USER_EMAIL: str = ""
    JIRA_API_TOKEN: str = ""

    # Optional shared secret for verifying Jira webhook payloads.
    JIRA_WEBHOOK_SECRET: str = ""

    # ── LLM / AI Configuration ────────────────────────────────────────
    # LiteLLM model string — prefix determines the provider:
    #   "openai/..."    → OpenAI
    #   "anthropic/..." → Anthropic (direct)
    #   "bedrock/..."   → AWS Bedrock (uses AWS_* credentials below)
    #   "ollama/..."    → Local Ollama
    LITELLM_MODEL: str = "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"

    # ── AWS Bedrock credentials ───────────────────────────────────────
    # LiteLLM reads these automatically when the model starts with "bedrock/".
    # Leave blank if using an IAM role (EC2/ECS instance profile).
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION_NAME: str = "eu-west-1"

    # Provider API keys for non-Bedrock providers (leave blank if using Bedrock)
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    AZURE_API_KEY: Optional[str] = None

    # ── Application Settings ──────────────────────────────────────────
    # API key required for administrative endpoints (e.g., /api/dry-run).
    # If set, requests must include `X-API-Key: <value>` header.
    # If empty, admin endpoints are unprotected (dev mode only).
    API_KEY: str = ""

    # Python logging verbosity.
    LOG_LEVEL: str = "INFO"

    # The label name that triggers TicketToCode task processing in Jira / GitHub.
    TICKET_LABEL: str = "ai-task"

    # Name of the GitHub Actions workflow file to dispatch in the monorepo.
    TICKET_WORKFLOW_FILE: str = "agent-ticket-to-code.yml"

    # ── Code Platform (Execution Layer) ───────────────────────────────
    # Which code platform to use for creating branches and PRs.
    # Supported values: "github" (default), "bitbucket", "gitlab" (future).
    REPO_PLATFORM: str = "github"

    # ── Pydantic-Settings Config ──────────────────────────────────────
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Return a cached singleton of the application settings.

    Using `lru_cache` ensures the `.env` file is only read once,
    and the same `Settings` instance is reused across the app.
    """
    return Settings()  # type: ignore[call-arg]

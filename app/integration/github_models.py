"""
ticket-to-code — GitHub Webhook Pydantic Models
──────────────────────────────────────────────────
Typed representations of the subset of a GitHub "issues" webhook payload
that TicketToCode cares about.  We intentionally keep these models slim —
only the fields we need are declared; everything else is silently ignored
thanks to Pydantic's `model_config = {"extra": "ignore"}` setting.

Reference:
    https://docs.github.com/en/webhooks/webhook-events-and-payloads#issues
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


# ── Nested Sub-Models ─────────────────────────────────────────────────


class GitHubUser(BaseModel):
    """Minimal representation of a GitHub user / actor."""

    login: str
    id: int

    model_config = ConfigDict(extra="ignore")


class GitHubLabel(BaseModel):
    """A single label attached to an issue."""

    id: int
    name: str
    # Color hex-code (e.g. "fc2929") — optional, not critical for us.
    color: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class GitHubRepository(BaseModel):
    """Minimal info about the repository that generated the event."""

    id: int
    # "owner/repo" format — e.g. "acme-corp/my-project"
    full_name: str

    model_config = ConfigDict(extra="ignore")


class GitHubIssue(BaseModel):
    """
    The `issue` object nested inside the webhook payload.

    We capture the fields the Coordination Layer needs to decide
    what to do with the task.
    """

    number: int
    title: str
    # The Markdown body of the issue (may be None for empty issues).
    body: Optional[str] = None
    # Labels currently attached to the issue.
    labels: list[GitHubLabel] = []
    # The user who opened the issue.
    user: Optional[GitHubUser] = None

    model_config = ConfigDict(extra="ignore")


# ── Top-Level Webhook Event ──────────────────────────────────────────


class IssueWebhookPayload(BaseModel):
    """
    Top-level schema for a GitHub **issues** webhook event.

    GitHub sends this JSON body whenever an issue is opened, edited,
    labeled, unlabeled, closed, etc.

    Key fields:
        action:     The verb describing what happened ("opened", "labeled", …).
        issue:      The full issue object.
        label:      (Optional) Present only on "labeled" / "unlabeled" actions;
                    contains the specific label that was added or removed.
        repository: The repo the issue belongs to.
        sender:     The user who triggered the event.
    """

    action: str
    issue: GitHubIssue
    # Only present for "labeled" / "unlabeled" actions.
    label: Optional[GitHubLabel] = None
    repository: GitHubRepository
    sender: Optional[GitHubUser] = None

    model_config = ConfigDict(extra="ignore")

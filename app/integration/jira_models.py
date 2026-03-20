"""
ticket-to-code — Jira Webhook Pydantic Models
────────────────────────────────────────────────
Typed representations of a Jira webhook payload for issue events.

Jira Cloud webhooks send a JSON body when issues are created, updated,
or transitioned.  We model the subset of fields TicketToCode needs.

Reference:
    https://developer.atlassian.com/cloud/jira/platform/webhooks/
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Nested Sub-Models ─────────────────────────────────────────────────


class JiraUser(BaseModel):
    """Minimal representation of a Jira user."""

    account_id: str = Field(alias="accountId", default="")
    display_name: str = Field(alias="displayName", default="")
    email_address: Optional[str] = Field(alias="emailAddress", default=None)

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class JiraProject(BaseModel):
    """The Jira project the issue belongs to."""

    id: str = ""
    key: str = ""          # e.g. "INFRA", "PLAT"
    name: str = ""

    model_config = ConfigDict(extra="ignore")


class JiraLabel(BaseModel):
    """
    Jira labels are plain strings (unlike GitHub which has id+name).
    We wrap them for consistency but they're just strings in the payload.
    """

    name: str

    model_config = ConfigDict(extra="ignore")


class JiraIssueType(BaseModel):
    """The type of Jira issue (Bug, Task, Story, Epic, etc.)."""

    name: str = ""
    id: str = ""

    model_config = ConfigDict(extra="ignore")


class JiraPriority(BaseModel):
    """Issue priority (Highest, High, Medium, Low, Lowest)."""

    name: str = ""
    id: str = ""

    model_config = ConfigDict(extra="ignore")


class JiraStatus(BaseModel):
    """Current workflow status of the issue."""

    name: str = ""          # e.g. "To Do", "In Progress", "Done"
    id: str = ""

    model_config = ConfigDict(extra="ignore")


class JiraIssueFields(BaseModel):
    """
    The `fields` object inside a Jira issue.

    Jira nests all issue data under `fields`.  We extract the
    fields the Coordination Layer needs.
    """

    summary: str = ""
    description: Optional[str] = None
    labels: list[str] = []
    project: Optional[JiraProject] = None
    issuetype: Optional[JiraIssueType] = None
    priority: Optional[JiraPriority] = None
    status: Optional[JiraStatus] = None
    creator: Optional[JiraUser] = None
    assignee: Optional[JiraUser] = None

    model_config = ConfigDict(extra="ignore")


class JiraIssue(BaseModel):
    """
    A Jira issue as it appears in the webhook payload.

    The `key` is the human-readable identifier (e.g., "INFRA-42").
    """

    id: str = ""
    key: str = ""           # e.g. "INFRA-42"
    self: str = ""          # API URL of the issue
    fields: JiraIssueFields = JiraIssueFields()

    model_config = ConfigDict(extra="ignore")


class JiraChangeItem(BaseModel):
    """
    A single field change in a Jira changelog entry.

    For label changes:
        field = "labels"
        from_string = ""
        to_string = "ai-task"
    """

    field: str = ""
    from_string: Optional[str] = Field(alias="fromString", default=None)
    to_string: Optional[str] = Field(alias="toString", default=None)

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class JiraChangelog(BaseModel):
    """The changelog sent with 'issue_updated' webhooks."""

    items: list[JiraChangeItem] = []

    model_config = ConfigDict(extra="ignore")


# ── Top-Level Webhook Event ──────────────────────────────────────────


class JiraWebhookPayload(BaseModel):
    """
    Top-level schema for a Jira webhook event.

    Jira sends different `webhookEvent` values:
        - "jira:issue_created"
        - "jira:issue_updated"
        - "jira:issue_deleted"

    For label-based triggers, we care about:
        1. "jira:issue_created" where the issue already has our label.
        2. "jira:issue_updated" where the changelog shows our label was added.

    Fields:
        timestamp:       Unix timestamp (milliseconds) of the event.
        webhookEvent:    The event type string.
        issue_event_type_name:  More specific event (e.g., "issue_generic",
                                "issue_updated", "issue_created").
        issue:           The full issue object.
        changelog:       (Optional) Present on update events — lists
                         which fields changed and their old/new values.
        user:            The user who triggered the event.
    """

    timestamp: Optional[int] = None
    webhook_event: str = Field(alias="webhookEvent", default="")
    issue_event_type_name: Optional[str] = Field(
        alias="issue_event_type_name", default=None
    )
    issue: JiraIssue = JiraIssue()
    changelog: Optional[JiraChangelog] = None
    user: Optional[JiraUser] = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

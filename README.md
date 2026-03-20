<p align="center">
  <img src="assets/ticket-to-code-logo.png" alt="TicketToCode" width="400">
</p>

<h1 align="center">ticket-to-code</h1>

<p align="center">
  An AI-powered engineering assistant that picks up <strong>Jira tasks</strong>, generates execution plans using <strong>DSPy + LiteLLM</strong>, and dispatches them to <strong>GitHub Actions</strong> — opening a PR for human review.
</p>

> **ticket-to-code** handles well-defined, repeatable tasks — infrastructure changes, feature work, config updates — so your team can focus on the hard problems.

> ℹ️ **No vendor lock-in.** Uses fully open-source tooling (DSPy, LiteLLM) and is model-agnostic — works with OpenAI, Anthropic, Azure, or local models via Ollama.

---

## How It Works

```
┌─────────┐      ┌──────────────────┐      ┌───────────────────┐      ┌──────────────┐
│  Jira   │─────▶│  FastAPI Server   │─────▶│  GitHub Actions   │─────▶│  Pull Request │
│  (task) │      │  (webhook + DSPy) │      │  (agent + validate)│      │  (for review) │
└─────────┘      └──────────────────┘      └───────────────────┘      └──────────────┘
     │                    │                          │
     │  1. Label issue    │  2. Classify task        │  3. Clone repo
     │     "ai-task"      │  3. Generate plan        │  4. Edit files (AI agent)
     │                    │  4. Dispatch workflow     │  5. terraform validate/plan
     │                    │                          │  6. Commit + open PR
```

1. You create a **Jira ticket** and add the `ai-task` label.
2. The **FastAPI server** receives the webhook, uses **DSPy** to classify the task and generate a step-by-step plan.
3. The server triggers a **GitHub Actions workflow** on your monorepo via `workflow_dispatch`.
4. The Action runner edits files, runs **safe validation only** (no apply/destroy), and opens a **Pull Request**.
5. You review the PR, check the terraform plan output, and click **Merge**.

---

## Architecture

| Layer | Module | Responsibility |
|---|---|---|
| **Integration** | `app/integration/` | Receives Jira/GitHub webhooks, verifies signatures, parses into typed models |
| **Coordination** | `app/coordination/` | DSPy router classifies tasks, generates plans, dispatches to execution |
| **Execution** | `app/execution/` | Triggers GitHub Actions `workflow_dispatch` with the plan |
| **Agent** | `agent-ticket-to-code.yml` | GitHub Action that runs the AI agent, validates, and opens PRs |

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/your-org/ticket-to-code.git
cd ticket-to-code
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```env
# Jira
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USER_EMAIL=you@example.com
JIRA_API_TOKEN=your_token

# GitHub (PAT needs repo + actions scopes)
GITHUB_PAT=ghp_your_token
GITHUB_REPO=your-org/your-monorepo

# LLM (via LiteLLM — supports OpenAI, Anthropic, Azure, Ollama, etc.)
LITELLM_MODEL=openai/gpt-4o
OPENAI_API_KEY=sk-your-key

# The label that triggers TicketToCode
TICKET_LABEL=ai-task
```

### 3. Install the GitHub Action

Copy the workflow template into your monorepo:

```bash
cp agent-ticket-to-code.yml /path/to/your/monorepo/.github/workflows/agent-ticket-to-code.yml
```

### 4. Run the Server

```bash
uvicorn app.main:app --reload --port 8000
```

### 5. Expose to Jira (for webhooks)

```bash
ngrok http 8000
```

Configure your Jira webhook to point at:
```
https://<your-ngrok-url>/api/webhooks/jira
```

---

## Project Structure

```
ticket-to-code/
├── app/
│   ├── main.py                    # FastAPI entry point — Jira & GitHub webhook routes
│   ├── core/
│   │   └── config.py              # Pydantic BaseSettings (all env vars)
│   ├── integration/
│   │   ├── jira_models.py         # Pydantic models for Jira webhook payloads
│   │   ├── jira_webhook.py        # Jira HMAC signature verification
│   │   ├── github_models.py       # Pydantic models for GitHub webhook payloads
│   │   └── github_webhook.py      # GitHub HMAC signature verification
│   ├── coordination/
│   │   ├── router.py              # DSPy + LiteLLM task classifier & planner
│   │   └── dispatcher.py          # Orchestrates: Router → GitHub Actions dispatch
│   └── execution/
│       └── github_trigger.py      # PyGithub workflow_dispatch trigger
├── agent-ticket-to-code.yml       # GitHub Action template (copy to monorepo)
├── .env.example
├── requirements.txt
└── README.md
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Service info |
| `GET` | `/health` | Liveness probe |
| `GET` | `/docs` | Interactive Swagger UI |
| `POST` | `/api/webhooks/jira` | Jira webhook receiver |
| `POST` | `/api/webhooks/github` | GitHub webhook receiver (legacy) |

---

## Safety Guardrails

The agent operates with strict safety boundaries:

| ✅ Allowed | ❌ Blocked |
|-----------|-----------|
| `terraform fmt` | `terraform apply` |
| `terraform validate` | `terraform destroy` |
| `terraform plan` | `kubectl delete` |
| `helm lint` | `helm install/upgrade` |
| `helm template` | Any write/mutate operation |
| `ansible-lint` | `rm`, `mv` on critical paths |
| `go vet`, `go build` | Direct cloud API calls |

- **Command allowlist** enforced in the GitHub Action
- **No merge capability** — only opens PRs for human review
- **30-minute timeout** on the GitHub Action runner
- **Concurrency control** — one run per Jira issue at a time

---

## Tech Stack

- **FastAPI** + **Uvicorn** — async webhook server
- **Pydantic** — typed models for all payloads & config
- **DSPy** — structured LLM prompting for task classification & planning
- **LiteLLM** — model-agnostic LLM routing (OpenAI, Anthropic, Azure, Ollama)
- **PyGithub** — GitHub API for workflow dispatch & PR creation
- **GitHub Actions** — secure execution environment with CI/CD credentials

---

## License

MIT

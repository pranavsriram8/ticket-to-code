"""
ticket-to-code — Smart Task Router (DSPy + LiteLLM)
──────────────────────────────────────────────────────
Uses DSPy Signatures to classify an incoming task, generate a step-by-step
plan, and identify which files in any repository need to be modified.

LiteLLM is used as the LLM backend, making the model configurable via
the LITELLM_MODEL env var (supports OpenAI, Anthropic, Bedrock, Azure, Ollama, etc.).

When a local repo path is provided, the router walks the real file tree and
passes it to the LLM so it can identify exact paths. When no path is given,
the LLM reasons from the task description alone.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

import dspy

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ── The known task types the router can classify into ─────────────────
TaskType = Literal["terraform", "helm", "ansible", "service", "script", "unknown"]

# Exported set used by the dispatcher to validate LLM output.
VALID_TASK_TYPES: frozenset[str] = frozenset(
    {"terraform", "helm", "ansible", "service", "script", "unknown"}
)

# ── File extensions the indexer considers relevant ────────────────────
_RELEVANT_EXTENSIONS = {
    ".tf", ".tfvars", ".yaml", ".yml", ".json", ".go",
    ".sh", ".py", ".md", ".tpl", ".j2",
}

# Directories to always skip when indexing
_SKIP_DIRS = {
    ".terraform", ".git", "node_modules", "__pycache__",
    "vendor", ".venv", "dist", "build",
}


def build_repo_index(repo_path: str | Path, max_files: int = 800) -> str:
    """
    Walk the monorepo and return a compact newline-separated file listing.

    The listing is passed to the LLM so it can reason about real paths
    instead of guessing from a static description.

    Args:
        repo_path:  Absolute path to the root of the monorepo.
        max_files:  Cap on number of files to include (keeps prompt size sane).

    Returns:
        Newline-separated list of relative file paths, e.g.:
            infra/terraform/production/main.tf
            services/api/deployment.yaml
            charts/myapp/values.yaml
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
        logger.warning("Repo path does not exist: %s", repo_root)
        return "(repo not available — paths are best guesses)"

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skipped directories in-place (os.walk respects this)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        for fname in filenames:
            if Path(fname).suffix in _RELEVANT_EXTENSIONS:
                abs_path = Path(dirpath) / fname
                rel_path = abs_path.relative_to(repo_root)
                files.append(str(rel_path))

        if len(files) >= max_files:
            break

    files.sort()
    logger.info("Repo index built: %d files from %s", len(files), repo_root)
    return "\n".join(files[:max_files])


# ── DSPy Signatures ──────────────────────────────────────────────────


class ClassifyTask(dspy.Signature):
    """Classify an infrastructure / engineering task and produce an execution plan.

    You are a senior DevOps engineer. You will be given:
    - A task title and description from a Jira ticket
    - A real listing of files in the monorepo

    Your job:
    1. Classify the task into one of: terraform, helm, ansible, service, script, unknown
    2. Identify ALL files that need to be read or modified — not just the primary one.
       Think carefully about file naming conventions:
         - Terraform projects split resources across multiple files by concern:
             * `module.*.tf`      — module declarations
             * `data.*.tf`        — data sources (aws_ami, remote_state, etc.)
             * `resource.*.tf`    — standalone resource definitions
             * `locals.*.tf`      — local values
             * `variables.tf`     — input variables
             * `outputs.tf`       — output values
           If a task requires adding a NEW data source, the correct file is the
           existing `data.*.tf` file in that directory — NOT the module file.
           If a task requires changing a version referenced in a module AND also
           adding a data source, you must include BOTH the module file AND the
           data file in target_paths.
         - Similarly for Helm: `values.yaml`, `Chart.yaml`, templates/*.yaml
         - For Ansible: the playbook AND the relevant vars/inventory files
    3. Produce a clear, numbered step-by-step plan. Each step must state:
       - WHICH file to edit
       - WHAT exact change to make (old value → new value, or new block to add)
       - WHERE in the file the change belongs (near which existing block)

    IMPORTANT:
    - target_paths MUST be real paths from repo_file_index — never invent paths.
    - Always include ALL files needed, not just the most obvious one.
    - When adding new Terraform data sources, always put them in the `data.*.tf`
      file of the same directory, not inside a `module.*.tf` file.
    """

    task_title: str = dspy.InputField(desc="The Jira issue summary / title")
    task_description: str = dspy.InputField(desc="The full Jira issue description")
    repo_file_index: str = dspy.InputField(
        desc=(
            "Newline-separated list of all relevant files in the monorepo. "
            "Use this to find EXACT paths — do not guess or invent paths. "
            "Look for ALL files in the same directory that are related to the task."
        )
    )

    task_type: str = dspy.OutputField(
        desc="One of: terraform, helm, ansible, service, script, unknown"
    )
    target_paths: str = dspy.OutputField(
        desc=(
            "Comma-separated list of ALL EXACT file paths from repo_file_index "
            "that need to be read or modified. Include every file that requires "
            "a change — e.g. both the module file AND the data file if a new "
            "data source must be added. Only include paths that exist in the index."
        )
    )
    plan: str = dspy.OutputField(
        desc=(
            "A numbered step-by-step plan for a code agent to execute this task. "
            "Each step must name the exact file, the exact change, and where in "
            "the file it belongs. Follow Terraform file-naming conventions: "
            "new data sources go in data.*.tf, not module.*.tf. "
            "Do NOT include any destructive operations (apply, destroy, delete)."
        )
    )
    safe_validation_commands: str = dspy.OutputField(
        desc=(
            "Comma-separated list of safe, read-only shell commands to validate "
            "the changes. E.g.: 'terraform fmt,terraform validate,terraform plan'. "
            "NEVER include apply, destroy, delete, or any write operations."
        )
    )


# ── Router Class ──────────────────────────────────────────────────────


class TaskRouter:
    """
    Wraps the DSPy classification pipeline.

    Usage:
        router = TaskRouter()
        result = router.route(
            task_title="Upgrade EKS to 1.30",
            task_description="Upgrade the production EKS cluster...",
            repo_path="/path/to/local/clone",   # optional — enables real file discovery
        )
        print(result.task_type)       # "terraform"
        print(result.target_paths)    # "infra/terraform/production/eks.tf"
        print(result.plan)            # "1. Open ... 2. Change ... 3. Run ..."
    """

    def __init__(self) -> None:
        """Initialize DSPy with LiteLLM as the language model backend."""
        settings = get_settings()

        # Configure DSPy to use LiteLLM, which auto-routes to the
        # correct provider based on the model string.
        self.lm = dspy.LM(
            model=settings.LITELLM_MODEL,
            # LiteLLM will read API keys from environment variables
            # (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
        )
        dspy.configure(lm=self.lm)

        # Create the DSPy predictor from our Signature.
        self.classifier = dspy.Predict(ClassifyTask)

        logger.info(
            "TaskRouter initialized with model: %s", settings.LITELLM_MODEL
        )

    def route(
        self,
        task_title: str,
        task_description: str,
        repo_path: str | Path | None = None,
    ) -> dspy.Prediction:
        """
        Classify a task and generate an execution plan.

        Args:
            task_title:       The Jira issue summary.
            task_description: The full Jira issue description / body.
            repo_path:        Optional path to a local monorepo clone.
                              If provided, the router walks the real file tree
                              and passes it to the LLM so it can identify
                              EXACT paths rather than guessing.

        Returns:
            A DSPy Prediction with fields:
                - task_type:               str
                - target_paths:            str (comma-separated, real paths)
                - plan:                    str (numbered steps)
                - safe_validation_commands: str (comma-separated)
        """
        logger.info("Routing task: '%s'", task_title)

        # ── Build real file index if a repo path is available ────────
        if repo_path:
            logger.info("Building repo file index from: %s", repo_path)
            repo_file_index = build_repo_index(repo_path)
        else:
            # No local clone available — tell the LLM so it reasons from
            # the task description alone rather than hallucinating paths.
            repo_file_index = (
                "(No local repository clone provided. "
                "Infer likely file paths from the task title and description.)"
            )
            logger.warning(
                "No repo_path provided — LLM will infer paths from task description only."
            )

        prediction = self.classifier(
            task_title=task_title,
            task_description=task_description or "No description provided.",
            repo_file_index=repo_file_index,
        )

        logger.info("Task classified as: %s", prediction.task_type)
        logger.info("Target paths: %s", prediction.target_paths)
        logger.debug("Plan:\n%s", prediction.plan)

        return prediction


# ── Module-level singleton (lazy-initialized) ─────────────────────────
_router_instance: TaskRouter | None = None


def get_router() -> TaskRouter:
    """
    Return a module-level singleton of TaskRouter.

    Lazy-initialized on first call so the LLM isn't loaded until
    the first task actually needs routing.
    """
    global _router_instance
    if _router_instance is None:
        _router_instance = TaskRouter()
    return _router_instance

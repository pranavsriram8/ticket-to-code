#!/usr/bin/env python3
"""
TicketToCode Agent — GitHub Action Entrypoint
──────────────────────────────────────────────
Runs inside GitHub Actions via `uses: pranavsriram8/ticket-to-code@main`.

1. Reads target files from the checked-out monorepo
2. Sends files + execution plan to Claude (via LiteLLM/Bedrock)
3. Receives edited file contents back
4. Writes changes to disk
5. Exports the plan to .ticket-to-code/plans/
6. Runs allowlisted validation commands
7. Commits, pushes, and creates a Pull Request
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import litellm
from github import Auth, Github


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ticket-to-code-agent")

# ── Command allowlist for validation ──────────────────────────────────
ALLOWED_COMMANDS = [
    "terraform fmt",
    "terraform init",
    "helm lint",
    "helm template",
    "ansible-lint",
    "ansible-playbook --check",
    "go vet",
    "go build",
    "go test",
]


def get_input(name: str, required: bool = True) -> str:
    """Read a GitHub Action input from environment variables."""
    value = os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", "").strip()
    if required and not value:
        logger.error("Required input '%s' is missing", name)
        sys.exit(1)
    return value


def read_target_files(repo_root: Path, target_paths: list[str]) -> dict[str, str]:
    """Read the contents of each target file."""
    files = {}
    for rel_path in target_paths:
        abs_path = repo_root / rel_path.strip()
        if abs_path.is_file():
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            files[rel_path.strip()] = content
            logger.info("📄 Read: %s (%d bytes)", rel_path.strip(), len(content))
        elif abs_path.is_dir():
            # Read all relevant files in directory
            for f in sorted(abs_path.rglob("*")):
                if f.is_file() and f.suffix in {
                    ".tf", ".tfvars", ".yaml", ".yml", ".json",
                    ".go", ".sh", ".py", ".md", ".tpl", ".j2",
                }:
                    rel = str(f.relative_to(repo_root))
                    content = f.read_text(encoding="utf-8", errors="replace")
                    files[rel] = content
                    logger.info("📄 Read: %s (%d bytes)", rel, len(content))
        else:
            logger.warning("⚠️  Target not found: %s", rel_path.strip())
    return files


def expand_scope(
    repo_root: Path,
    files: dict[str, str],
    task_title: str,
    task_type: str,
    plan: str,
    model: str,
) -> dict[str, str]:
    """
    Scope Expansion — ask Claude if it needs sibling files in the same directory.

    Agent 1 identified files by filename only. Now that Agent 2 has the actual
    file contents, Claude may realize it needs additional files in the SAME
    directory (e.g., data.compute.ec2.tf that wasn't obvious from the name).

    This ONLY looks at sibling files — never goes outside the target directory.
    """
    if not files:
        return files

    # Find the common parent directory of all target files
    parent_dirs = set()
    for path in files:
        parent_dirs.add(str(Path(path).parent))

    # List sibling files in each parent directory (that we don't already have)
    sibling_files = []
    for parent_dir in parent_dirs:
        abs_dir = repo_root / parent_dir
        if not abs_dir.is_dir():
            continue
        for f in sorted(abs_dir.iterdir()):
            if f.is_file() and f.suffix in {
                ".tf", ".tfvars", ".yaml", ".yml", ".json",
                ".go", ".sh", ".py", ".tpl", ".j2",
            }:
                rel = str(f.relative_to(repo_root))
                if rel not in files:
                    sibling_files.append(rel)

    if not sibling_files:
        logger.info("No additional sibling files available — scope unchanged.")
        return files

    # Show Claude a preview of each sibling (first 5 lines) so it can
    # decide based on CONTENT, not just filename
    siblings_with_preview = []
    for sib in sibling_files:
        abs_sib = repo_root / sib
        preview = ""
        if abs_sib.is_file():
            try:
                lines = abs_sib.read_text(encoding="utf-8", errors="replace").splitlines()[:5]
                preview = "\n    ".join(lines)
            except Exception:
                preview = "(could not read)"
        siblings_with_preview.append(f"- `{sib}`\n    ```\n    {preview}\n    ```")

    current_files_list = "\n".join(f"- `{p}`" for p in files.keys())
    siblings_list = "\n".join(siblings_with_preview)

    prompt = f"""You are reviewing files for a DevOps task. You have been given some files to edit,
but there may be other files in the SAME directory that you also need to read.

## Task
**Title:** {task_title}
**Type:** {task_type}

## Execution Plan
{plan}

## Files You Already Have
{current_files_list}

## Other Files Available in the Same Directory (with previews)
{siblings_list}

## Question
Which of the "other files available" do you ALSO need to read to complete this task correctly?

Look at the PREVIEW of each file — check if it contains:
- Data sources (data blocks) that reference AMIs, versions, or parameters used by the target resources
- Variables or locals that feed into the resources being changed
- Related module calls or resource definitions

Return ONLY a comma-separated list of file paths from the "Other Files Available" list.
If you don't need any additional files, return exactly: NONE"""

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.0,
    )

    answer = response.choices[0].message.content.strip()
    logger.info("Scope expansion response: %s", answer)

    if answer.upper().strip() == "NONE":
        logger.info("No scope expansion needed.")
        return files

    # Parse and read additional files
    sibling_set = set(sibling_files)
    expanded = dict(files)  # Copy existing files
    for path in answer.split(","):
        path = path.strip().strip("`").strip()
        if path in sibling_set:
            abs_path = repo_root / path
            if abs_path.is_file():
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                expanded[path] = content
                logger.info("📄 Scope expanded: %s (%d bytes)", path, len(content))
        elif path and path.upper() != "NONE":
            logger.warning("⚠️  Expansion requested unknown file: %s — skipping", path)

    logger.info("Scope: %d → %d files after expansion", len(files), len(expanded))
    return expanded


def build_prompt(
    task_title: str,
    task_type: str,
    plan: str,
    files: dict[str, str],
) -> str:
    """Build the prompt for Claude to edit the files."""
    file_blocks = []
    for path, content in files.items():
        file_blocks.append(f"### File: `{path}`\n```\n{content}\n```")

    files_section = "\n\n".join(file_blocks)

    return f"""You are a senior DevOps/infrastructure engineer. You will be given:
1. A task description and execution plan
2. The current contents of files that need to be modified

Your job is to produce the EDITED versions of these files according to the plan.

## Task
**Title:** {task_title}
**Type:** {task_type}

## Execution Plan
{plan}

## Current Files
{files_section}

## Instructions
- Return ONLY the edited files in the exact format shown below.
- Include the COMPLETE file contents — not just the changed parts.
- If a file doesn't need changes, still include it unchanged.
- Do NOT add any commentary outside the file blocks.
- Do NOT create new files unless the plan explicitly says to.

## Output Format
For each file, output exactly:

<file path="relative/path/to/file.ext">
complete file contents here
</file>

Return ALL files listed above, with edits applied per the plan."""


def parse_response(response_text: str) -> dict[str, str]:
    """Parse Claude's response to extract edited file contents."""
    pattern = r'<file path="([^"]+)">\n(.*?)</file>'
    matches = re.findall(pattern, response_text, re.DOTALL)

    edited_files = {}
    for path, content in matches:
        # Strip leading/trailing newlines from content
        edited_files[path.strip()] = content.strip() + "\n"
        logger.info("✏️  Parsed edit for: %s", path.strip())

    return edited_files


def write_files(repo_root: Path, edited_files: dict[str, str]) -> list[str]:
    """Write edited files back to disk. Returns list of changed files."""
    changed = []
    for rel_path, content in edited_files.items():
        abs_path = repo_root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if actually changed
        if abs_path.exists():
            original = abs_path.read_text(encoding="utf-8", errors="replace")
            if original == content:
                logger.info("⏭️  Unchanged: %s", rel_path)
                continue

        abs_path.write_text(content, encoding="utf-8")
        changed.append(rel_path)
        logger.info("💾 Written: %s", rel_path)

    return changed


def export_plan(
    repo_root: Path,
    jira_key: str,
    task_title: str,
    task_type: str,
    branch_name: str,
    target_paths: str,
    plan: str,
    validation_commands: str,
) -> None:
    """Export the execution plan to .ticket-to-code/plans/."""
    plans_dir = repo_root / ".ticket-to-code" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    plan_file = plans_dir / f"{jira_key}.md"
    plan_file.write_text(
        f"# 🎫 TicketToCode Plan — {jira_key}\n\n"
        f"**Task:** {task_title}\n"
        f"**Type:** {task_type}\n"
        f"**Branch:** {branch_name}\n\n"
        f"---\n\n"
        f"## Target Paths\n\n"
        + "\n".join(f"- `{p.strip()}`" for p in target_paths.split(","))
        + f"\n\n## Execution Plan\n\n{plan}\n\n"
        f"## Validation Commands\n\n"
        + "\n".join(f"- `{c.strip()}`" for c in validation_commands.split(",") if c.strip())
        + "\n\n---\n\n"
        f"> ⚠️ This plan was generated by the TicketToCode AI agent.\n"
        f"> Review the plan and the resulting code changes carefully before merging.\n",
        encoding="utf-8",
    )
    logger.info("📋 Plan exported to %s", plan_file)


def run_validation(commands_str: str) -> str:
    """Run allowlisted validation commands and return output."""
    if not commands_str.strip():
        return "No validation commands specified."

    output_parts = []
    commands = [c.strip() for c in commands_str.split(",") if c.strip()]

    for cmd in commands:
        # Safety check
        is_safe = any(cmd.startswith(allowed) for allowed in ALLOWED_COMMANDS)
        if not is_safe:
            msg = f"❌ BLOCKED (not in allowlist): {cmd}"
            logger.warning(msg)
            output_parts.append(f"### `{cmd}`\n{msg}\n")
            continue

        logger.info("✅ Running: %s", cmd)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=300
            )
            out = result.stdout + result.stderr
            output_parts.append(f"### `{cmd}`\n```\n{out}\n```\n")
        except subprocess.TimeoutExpired:
            output_parts.append(f"### `{cmd}`\n⏰ Timed out after 5 minutes.\n")
        except Exception as e:
            output_parts.append(f"### `{cmd}`\n❌ Error: {e}\n")

    return "\n".join(output_parts)


def git_commit_and_push(
    repo_root: Path, branch_name: str, jira_key: str, task_title: str, task_type: str,
    author_name: str = "TicketToCode Agent",
    author_email: str = "ticket-to-code-agent@noreply.github.com",
) -> bool:
    """Create branch, commit changes, and push. Returns True if changes were pushed."""

    def run_git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

    # Fix dubious ownership (GH Actions runs as different user)
    run_git("config", "--global", "--add", "safe.directory", str(repo_root))

    # Configure git
    run_git("config", "user.name", author_name)
    run_git("config", "user.email", author_email)

    # Create and switch to branch
    run_git("checkout", "-b", branch_name)

    # Stage all changes
    run_git("add", "-A")

    # Check if there are changes
    result = run_git("diff", "--staged", "--quiet")
    if result.returncode == 0:
        logger.info("No changes to commit.")
        return False

    # Commit
    commit_msg = (
        f"ticket-to-code({jira_key}): {task_title}\n\n"
        f"Automated changes generated by TicketToCode AI agent.\n"
        f"Task type: {task_type}\n"
        f"Jira: {jira_key}"
    )
    run_git("commit", "-m", commit_msg)

    # Push
    push_result = run_git("push", "origin", branch_name)
    if push_result.returncode != 0:
        logger.error("Git push failed: %s", push_result.stderr)
        return False

    logger.info("🚀 Pushed branch: %s", branch_name)
    return True


def create_pull_request(
    jira_key: str,
    task_title: str,
    task_type: str,
    target_paths: str,
    plan: str,
    validation_output: str,
    branch_name: str,
    base_branch: str,
) -> None:
    """Create a Pull Request via GitHub API."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo_name = os.environ.get("GITHUB_REPOSITORY", "")

    if not token or not repo_name:
        logger.error("GITHUB_TOKEN or GITHUB_REPOSITORY not set")
        return

    gh = Github(auth=Auth.Token(token))
    repo = gh.get_repo(repo_name)

    body = (
        f"## 🎫 TicketToCode Agent — Automated PR\n\n"
        f"**Jira:** {jira_key}\n"
        f"**Task Type:** `{task_type}`\n"
        f"**Target Paths:** `{target_paths}`\n\n"
        f"---\n\n"
        f"### 📋 Execution Plan\n{plan}\n\n"
        f"---\n\n"
        f"### ✅ Validation Results\n{validation_output}\n\n"
        f"---\n\n"
        f"> ⚠️ **This PR was generated by an AI agent.** "
        f"Please review all changes carefully before merging.\n"
        f"> The agent only ran read-only validation commands — "
        f"no infrastructure was modified.\n"
    )

    pr = repo.create_pull(
        title=f"ticket-to-code({jira_key}): {task_title}",
        body=body,
        head=branch_name,
        base=base_branch,
    )
    logger.info("🎉 PR created: %s", pr.html_url)

    # Add labels
    try:
        pr.add_to_labels("ticket-to-code-agent", task_type)
    except Exception:
        logger.warning("Could not add labels (they may not exist yet)")


def main() -> None:
    """Main agent entrypoint."""
    logger.info("═══════════════════════════════════════════")
    logger.info("  TicketToCode Agent")
    logger.info("═══════════════════════════════════════════")

    # ── Read inputs ──────────────────────────────────────────────
    jira_key = get_input("jira_issue_key")
    task_title = get_input("task_title")
    task_type = get_input("task_type")
    target_paths_str = get_input("target_paths")
    plan = get_input("plan")
    validation_commands = get_input("validation_commands", required=False)
    model = get_input("model", required=False) or "bedrock/eu.anthropic.claude-sonnet-4-6"
    base_branch = get_input("base_branch", required=False) or "master"
    git_author_name = get_input("git_author_name", required=False) or "TicketToCode Agent"
    git_author_email = get_input("git_author_email", required=False) or "ticket-to-code-agent@noreply.github.com"

    branch_name = f"ticket-to-code/{jira_key}"
    target_paths = [p.strip() for p in target_paths_str.split(",") if p.strip()]

    # GitHub Actions workspace (where the repo is checked out)
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", "/github/workspace"))

    logger.info("Jira: %s", jira_key)
    logger.info("Task: %s", task_title)
    logger.info("Type: %s", task_type)
    logger.info("Model: %s", model)

    logger.info("Targets: %s", target_paths)

    # ── 1. Read target files ─────────────────────────────────────
    # Scope identification already happened in Agent 1 (server-side)
    # via GitHub Trees API + LLM. We receive exact, validated paths.
    logger.info("── Reading target files ──")
    files = read_target_files(repo_root, target_paths)

    if not files:
        logger.error("No target files found! Cannot proceed.")
        sys.exit(1)

    # ── 1b. Scope Expansion ──────────────────────────────────────
    # Agent 2 has the full repo. Ask Claude if it needs sibling files
    # that Agent 1 may have missed (it only saw filenames, not contents).
    logger.info("── Checking if broader scope needed ──")
    files = expand_scope(repo_root, files, task_title, task_type, plan, model)

    # ── 2. Send to Claude ────────────────────────────────────────
    logger.info("── Sending to Claude ──")
    prompt = build_prompt(task_title, task_type, plan, files)

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=16000,
        temperature=0.0,
    )

    response_text = response.choices[0].message.content
    logger.info("Received response (%d chars)", len(response_text))

    # ── 3. Parse and write edited files ──────────────────────────
    logger.info("── Applying edits ──")
    edited_files = parse_response(response_text)

    if not edited_files:
        logger.warning("No file edits parsed from response. Raw response:")
        logger.warning(response_text[:2000])
        sys.exit(1)

    changed_files = write_files(repo_root, edited_files)

    # ── 4. Export the plan ───────────────────────────────────────
    export_plan(
        repo_root, jira_key, task_title, task_type,
        branch_name, target_paths_str, plan, validation_commands,
    )

    # ── 5. Run validation commands ───────────────────────────────
    logger.info("── Running validation ──")
    validation_output = run_validation(validation_commands)
    logger.info(validation_output)

    # ── 6. Commit and push ───────────────────────────────────────
    logger.info("── Committing and pushing ──")
    pushed = git_commit_and_push(
        repo_root, branch_name, jira_key, task_title, task_type,
        author_name=git_author_name,
        author_email=git_author_email,
    )

    if not pushed:
        logger.info("No changes detected — skipping PR creation.")
        return

    # ── 7. Create Pull Request ───────────────────────────────────
    logger.info("── Creating Pull Request ──")
    create_pull_request(
        jira_key, task_title, task_type, target_paths_str,
        plan, validation_output, branch_name, base_branch,
    )

    logger.info("═══════════════════════════════════════════")
    logger.info("  ✅ Agent completed successfully")
    logger.info("═══════════════════════════════════════════")


if __name__ == "__main__":
    main()

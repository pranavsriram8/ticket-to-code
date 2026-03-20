"""
ticket-to-code — Scope Identifier (Agent 1)
──────────────────────────────────────────────
Generalized scope identification using GitHub Trees API + LLM.

Works with ANY monorepo structure — no hardcoded paths.

Strategy: The LLM "navigates" the repo tree like a file browser:
  1. Fetch top-level directories → LLM picks which to explore
  2. Fetch that directory → LLM picks subdirectory
  3. Repeat until deep enough → fetch recursive tree of that leaf
  4. LLM picks exact files from the leaf

Each step is a single GitHub API call. Never truncates because we
only fetch one level at a time until the final recursive fetch of
a small subdirectory.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import dspy
from github import Auth, Github

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# File extensions worth including in scope
_RELEVANT_EXTENSIONS = {
    ".tf", ".tfvars", ".yaml", ".yml", ".json",
    ".go", ".sh", ".py", ".tpl", ".j2", ".hcl",
}

# Directories to always skip
_SKIP_DIRS = {
    ".terraform", ".git", "node_modules", "__pycache__",
    "vendor", ".venv", "dist", "build",
}

# Max depth for tree navigation before forcing a recursive fetch
_MAX_NAV_DEPTH = 5


# ── GitHub Tree helpers ───────────────────────────────────────────────

def _get_github_client():
    """Create a GitHub client from settings."""
    settings = get_settings()
    return Github(auth=Auth.Token(settings.GITHUB_PAT))


def fetch_directory_listing(
    repo_name: str, path: str = "", branch: str = "master"
) -> tuple[list[str], list[str]]:
    """
    Fetch one level of a directory from GitHub Trees API.

    Returns:
        (directories, files) — just names, not full paths.
    """
    gh = _get_github_client()
    try:
        repo = gh.get_repo(repo_name)

        if not path:
            # Root level
            tree = repo.get_git_tree(sha=branch)
        else:
            # Navigate to the target path
            tree = repo.get_git_tree(sha=branch)
            for component in path.split("/"):
                found = False
                for item in tree.tree:
                    if item.type == "tree" and item.path == component:
                        tree = repo.get_git_tree(sha=item.sha)
                        found = True
                        break
                if not found:
                    logger.warning("Path component '%s' not found in %s", component, path)
                    return [], []

        dirs = []
        files = []
        for item in tree.tree:
            if item.path in _SKIP_DIRS:
                continue
            if item.type == "tree":
                dirs.append(item.path)
            elif item.type == "blob":
                suffix = PurePosixPath(item.path).suffix
                if suffix in _RELEVANT_EXTENSIONS:
                    files.append(item.path)

        dirs.sort()
        files.sort()
        return dirs, files

    except Exception as exc:
        logger.error("Failed to list directory %s/%s: %s", repo_name, path, exc)
        return [], []
    finally:
        gh.close()


def fetch_subtree_recursive(
    repo_name: str, subtree_path: str, branch: str = "master"
) -> list[str]:
    """
    Fetch all files under a specific subtree recursively.
    Returns full relative paths.
    """
    gh = _get_github_client()
    try:
        repo = gh.get_repo(repo_name)

        # Navigate to the subtree
        tree = repo.get_git_tree(sha=branch)
        for component in subtree_path.split("/"):
            found = False
            for item in tree.tree:
                if item.type == "tree" and item.path == component:
                    tree = repo.get_git_tree(sha=item.sha)
                    found = True
                    break
            if not found:
                logger.warning("Subtree component '%s' not found", component)
                return []

        # Recursive fetch of this subtree
        subtree = repo.get_git_tree(sha=tree.sha, recursive=True)
        files = []
        for item in subtree.tree:
            if item.type != "blob":
                continue
            suffix = PurePosixPath(item.path).suffix
            if suffix in _RELEVANT_EXTENSIONS:
                full_path = f"{subtree_path}/{item.path}"
                files.append(full_path)

        files.sort()
        logger.info("Recursive fetch of %s: %d files (truncated=%s)",
                     subtree_path, len(files), subtree.truncated)
        return files

    except Exception as exc:
        logger.error("Failed to fetch subtree %s: %s", subtree_path, exc)
        return []
    finally:
        gh.close()


# ── DSPy Signatures ──────────────────────────────────────────────────

class NavigateTree(dspy.Signature):
    """Navigate a monorepo directory tree to find the right location for a task.

    You are a senior DevOps engineer navigating a monorepo file tree.
    You are at a specific directory and can see its contents (subdirectories and files).

    Your job: pick which subdirectories to explore further to find the files
    that need changing for this task.

    Rules:
    - ONLY pick directories from the listing provided.
    - Pick the MINIMUM directories needed (usually 1-2).
    - If you see the target files already at this level, return "FOUND" instead.
    - Think about monorepo conventions:
      * terraform/ or infra/ for infrastructure
      * helm/ or charts/ for Helm charts
      * ansible/ or playbooks/ for Ansible
      * services/ or src/ for application code
    """

    task_title: str = dspy.InputField(desc="The Jira issue summary")
    task_type: str = dspy.InputField(desc="Classified task type")
    current_path: str = dspy.InputField(desc="Current directory path (empty string = repo root)")
    directories: str = dspy.InputField(desc="Comma-separated list of subdirectories at this level")
    files: str = dspy.InputField(desc="Comma-separated list of files at this level (may be empty)")

    next_dirs: str = dspy.OutputField(
        desc=(
            "Comma-separated list of subdirectory names to explore next. "
            "Pick 1-3 most relevant. Or return exactly 'FOUND' if the target "
            "files are visible at this level."
        )
    )


class IdentifyFiles(dspy.Signature):
    """Identify which files in a directory listing need to be changed for a task.

    You are given a COMPLETE listing of files in the relevant directory tree.
    Pick EXACTLY which files need to be READ or MODIFIED.

    Rules:
    - ONLY return paths from the file listing.
    - Include ALL files needed — missing a file is worse than including an extra.
    - For terraform: include the module file, variables, locals, and any data sources
      that reference the thing being changed.
    - Do NOT include files unrelated to the task.
    """

    task_title: str = dspy.InputField(desc="The Jira issue summary")
    task_type: str = dspy.InputField(desc="Classified task type")
    plan: str = dspy.InputField(desc="Step-by-step execution plan")
    file_listing: str = dspy.InputField(
        desc="Newline-separated list of ALL files in the relevant directory. ONLY return paths from this list."
    )

    target_paths: str = dspy.OutputField(
        desc="Comma-separated list of EXACT file paths that need to be read or modified."
    )


# ── Main orchestration ───────────────────────────────────────────────

def identify_scope(
    task_title: str,
    task_type: str,
    plan: str,
    repo_name: str | None = None,
    branch: str = "master",
) -> str:
    """
    Generalized scope identification.

    Phase 1 — Navigate: LLM browses the tree level by level to find
              the right directory.
    Phase 2 — Identify: Recursive fetch of that directory, LLM picks
              exact files.

    Returns:
        Comma-separated string of exact file paths.
    """
    settings = get_settings()
    repo = repo_name or settings.GITHUB_REPO

    if not repo:
        logger.warning("No GITHUB_REPO configured — cannot identify scope.")
        return ""

    # ── Phase 1: Navigate the tree ───────────────────────────────────
    # Start at repo root, let the LLM drill down level by level.
    target_dirs = _navigate_to_target(repo, branch, task_title, task_type)

    if not target_dirs:
        logger.error("Navigation failed — could not find target directory.")
        return ""

    # ── Phase 2: Fetch and identify exact files ──────────────────────
    all_files: list[str] = []
    for target_dir in target_dirs:
        logger.info("📂 Fetching recursive listing of: %s", target_dir)
        files = fetch_subtree_recursive(repo, target_dir, branch)
        all_files.extend(files)

    if not all_files:
        logger.error("No files found in target directories: %s", target_dirs)
        return ""

    logger.info("🎯 Phase 2: Sending %d files to LLM for exact identification...", len(all_files))

    file_listing = "\n".join(all_files)
    predictor = dspy.Predict(IdentifyFiles)
    prediction = predictor(
        task_title=task_title,
        task_type=task_type,
        plan=plan,
        file_listing=file_listing,
    )

    raw = prediction.target_paths.strip()
    logger.info("Scope identifier raw output: %s", raw)

    # Validate
    index_set = set(all_files)
    validated = []
    for path in raw.split(","):
        path = path.strip().strip("`").strip()
        if path in index_set:
            validated.append(path)
            logger.info("✅ Scope: %s", path)
        elif path:
            logger.warning("⚠️  Unknown path: %s — skipping", path)

    result = ",".join(validated)
    logger.info("🎯 Final scope (%d files): %s", len(validated), result)
    return result


def _navigate_to_target(
    repo: str,
    branch: str,
    task_title: str,
    task_type: str,
) -> list[str]:
    """
    Let the LLM navigate the repo tree to find the right directory.
    Returns a list of directory paths to fetch recursively.
    """
    navigator = dspy.Predict(NavigateTree)
    current_path = ""
    explored_dirs: list[str] = []

    for depth in range(_MAX_NAV_DEPTH):
        logger.info("🔍 Navigation depth %d — listing: %s/", depth, current_path or "(root)")

        dirs, files = fetch_directory_listing(repo, current_path, branch)

        if not dirs and not files:
            logger.warning("Empty directory at %s — stopping navigation.", current_path)
            break

        # If we have files and few/no subdirectories, we may have arrived
        if files and len(dirs) <= 3:
            logger.info("📁 Reached leaf-like directory with %d files — could stop here.", len(files))

        prediction = navigator(
            task_title=task_title,
            task_type=task_type,
            current_path=current_path,
            directories=", ".join(dirs) if dirs else "(none)",
            files=", ".join(files) if files else "(none)",
        )

        next_dirs_raw = prediction.next_dirs.strip()
        logger.info("LLM navigation choice at '%s': %s", current_path, next_dirs_raw)

        # Check if LLM says files are found at this level
        if next_dirs_raw.upper().strip() == "FOUND":
            logger.info("✅ LLM says target files are at: %s", current_path)
            return [current_path] if current_path else []

        # Parse the selected directories
        selected = []
        valid_dirs = set(dirs)
        for d in next_dirs_raw.split(","):
            d = d.strip().strip("`").strip()
            if d in valid_dirs:
                selected.append(d)
            elif d:
                logger.warning("LLM selected unknown dir '%s' — skipping", d)

        if not selected:
            logger.warning("No valid directories selected — stopping at: %s", current_path)
            return [current_path] if current_path else []

        # If multiple dirs selected, explore them all
        if len(selected) > 1:
            result_dirs = []
            for d in selected:
                full_path = f"{current_path}/{d}" if current_path else d
                result_dirs.append(full_path)
            logger.info("Multiple dirs selected — returning all: %s", result_dirs)
            return result_dirs

        # Single dir — drill deeper
        current_path = f"{current_path}/{selected[0]}" if current_path else selected[0]
        logger.info("Drilling into: %s", current_path)

    # Reached max depth — return current path
    logger.info("Max navigation depth reached — using: %s", current_path)
    return [current_path] if current_path else []

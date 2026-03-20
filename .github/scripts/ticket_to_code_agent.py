#!/usr/bin/env python3
"""
TicketToCode Agent — File Execution Agent
──────────────────────────────────────────
This script runs inside the GitHub Actions workflow (`agent-ticket-to-code.yml`).
It receives a task plan + target file paths, reads the files, asks Claude
on Bedrock to produce the exact edits, then writes them back to disk.

The workflow then diffs, commits, and opens a PR.

Usage (called by agent-ticket-to-code.yml):
    python ticket_to_code_agent.py \\
        --task-type terraform \\
        --target-paths "infra/eks.tf,infra/variables.tf" \\
        --plan "1. Change eks_version to 1.30..."

Safety guarantees:
    - ONLY writes to files that already exist in the repo (no new paths)
    - ONLY modifies files listed in --target-paths
    - Cannot run shell commands
    - All edits are shown in the PR diff for human review before merge
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ticket_to_code_agent")


# ═══════════════════════════════════════════════════════════════════════
#  LLM Client (LiteLLM → Bedrock)
# ═══════════════════════════════════════════════════════════════════════


def call_llm(prompt: str, model: str) -> str:
    """
    Call the LLM via LiteLLM and return the text response.

    Supports any LiteLLM-compatible model string:
        bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0
        openai/gpt-4o
        anthropic/claude-3-5-sonnet-20241022
    """
    try:
        import litellm  # type: ignore[import]
    except ImportError:
        log.error("litellm not installed. Run: pip install litellm boto3")
        sys.exit(1)

    log.info("Calling LLM: %s", model)
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8192,
        temperature=0.1,  # Low temp for deterministic edits
    )
    return response.choices[0].message.content


# ═══════════════════════════════════════════════════════════════════════
#  File Operations
# ═══════════════════════════════════════════════════════════════════════


def read_files(target_paths: list[str], repo_root: Path) -> dict[str, str]:
    """
    Read the content of each target file.

    Args:
        target_paths: List of paths relative to the repo root.
        repo_root:    Absolute path to the repository root.

    Returns:
        Dict mapping path → file content. Skips files that don't exist.
    """
    contents: dict[str, str] = {}
    for rel_path in target_paths:
        rel_path = rel_path.strip()
        if not rel_path:
            continue

        abs_path = repo_root / rel_path

        # Security: ensure the path stays within the repo
        try:
            abs_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            log.warning("Skipping path outside repo root: %s", rel_path)
            continue

        if abs_path.exists() and abs_path.is_file():
            contents[rel_path] = abs_path.read_text(encoding="utf-8")
            log.info("Read %s (%d bytes)", rel_path, len(contents[rel_path]))
        else:
            log.warning("File not found (will be created): %s", rel_path)
            contents[rel_path] = ""  # New file

    return contents


def write_files(edited_files: dict[str, str], repo_root: Path) -> list[str]:
    """
    Write edited file contents back to disk.

    Args:
        edited_files: Dict mapping relative path → new content.
        repo_root:    Absolute path to the repository root.

    Returns:
        List of paths that were successfully written.
    """
    written: list[str] = []
    for rel_path, content in edited_files.items():
        abs_path = repo_root / rel_path

        # Security: ensure within repo
        try:
            abs_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            log.warning("BLOCKED write outside repo: %s", rel_path)
            continue

        # Create parent directories if needed
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        log.info("Wrote %s (%d bytes)", rel_path, len(content))
        written.append(rel_path)

    return written


# ═══════════════════════════════════════════════════════════════════════
#  Prompt Builder
# ═══════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = textwrap.dedent("""
    You are a precise infrastructure engineer executing a specific task.
    You will be given:
    1. A set of files from a monorepo (with their current contents)
    2. A step-by-step plan describing exactly what to change

    Your job is to produce the EDITED versions of the files.

    STRICT RULES:
    - Only edit files listed in the "Files" section
    - Make ONLY the changes described in the plan — nothing else
    - Preserve all comments, formatting, and indentation style
    - Do NOT add explanations, markdown, or commentary to the files
    - Return ONLY valid JSON in the exact format specified

    OUTPUT FORMAT (strict JSON, no markdown fences):
    {
      "files": {
        "path/to/file.tf": "<full new content of the file>",
        "path/to/other.tf": "<full new content of the file>"
      },
      "summary": "One-sentence description of what was changed"
    }

    If a file does not need changes, include it with its original content.
    If a file is empty (new file), provide the complete new content.
""").strip()


def build_prompt(
    task_type: str,
    plan: str,
    file_contents: dict[str, str],
) -> str:
    """Build the prompt to send to the LLM."""

    files_section = ""
    for path, content in file_contents.items():
        files_section += f"\n### File: {path}\n```\n{content}\n```\n"

    return textwrap.dedent(f"""
        {SYSTEM_PROMPT}

        ## Task Type
        {task_type}

        ## Execution Plan
        {plan}

        ## Files to Edit
        {files_section}

        ## Instructions
        Apply the plan to the files above. Return the complete edited
        file contents as JSON. Do not truncate any file — return the
        full content even for unchanged sections.
    """).strip()


# ═══════════════════════════════════════════════════════════════════════
#  Response Parser
# ═══════════════════════════════════════════════════════════════════════


def parse_llm_response(response: str) -> dict:
    """
    Parse the LLM JSON response. Handles common formatting issues.

    Returns a dict with keys: files (dict), summary (str)
    """
    # Strip markdown code fences if present
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    # Find the JSON object boundaries
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in LLM response")

    json_str = text[start:end]

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s", e)
        log.debug("Raw response: %s", response[:500])
        raise

    if "files" not in result:
        raise ValueError("LLM response missing 'files' key")

    return result


# ═══════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TicketToCode AI file-editing agent"
    )
    parser.add_argument(
        "--task-type",
        required=True,
        help="Classified task type (terraform, helm, service, etc.)",
    )
    parser.add_argument(
        "--target-paths",
        required=True,
        help="Comma-separated file paths relative to repo root",
    )
    parser.add_argument(
        "--plan",
        required=True,
        help="Step-by-step execution plan from the DSPy router",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "LITELLM_MODEL",
            "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        help="LiteLLM model string (default: from LITELLM_MODEL env var)",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the LLM response without writing files",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    target_paths = [p.strip() for p in args.target_paths.split(",") if p.strip()]

    log.info("═══ TicketToCode Agent Starting ═══")
    log.info("Task type   : %s", args.task_type)
    log.info("Model       : %s", args.model)
    log.info("Repo root   : %s", repo_root)
    log.info("Target paths: %s", target_paths)

    # ── 1. Read target files ─────────────────────────────────────────
    file_contents = read_files(target_paths, repo_root)

    if not file_contents:
        log.error("No files could be read. Check --target-paths.")
        sys.exit(1)

    # ── 2. Build prompt ──────────────────────────────────────────────
    prompt = build_prompt(
        task_type=args.task_type,
        plan=args.plan,
        file_contents=file_contents,
    )
    log.info("Prompt built (%d chars)", len(prompt))

    # ── 3. Call LLM ──────────────────────────────────────────────────
    try:
        response = call_llm(prompt, args.model)
    except Exception as exc:
        log.error("LLM call failed: %s", exc, exc_info=True)
        sys.exit(1)

    log.info("LLM response received (%d chars)", len(response))

    # ── 4. Parse response ────────────────────────────────────────────
    try:
        result = parse_llm_response(response)
    except Exception as exc:
        log.error("Failed to parse LLM response: %s", exc)
        log.error("Raw response:\n%s", response[:1000])
        sys.exit(1)

    edited_files: dict[str, str] = result["files"]
    summary: str = result.get("summary", "Changes applied by TicketToCode agent")

    log.info("Summary: %s", summary)
    log.info("Files to write: %s", list(edited_files.keys()))

    # ── 5. Safety check: only write allowed paths ────────────────────
    allowed_paths = set(target_paths)
    blocked = [p for p in edited_files if p not in allowed_paths]
    if blocked:
        log.warning(
            "LLM attempted to write to paths NOT in target list — blocked: %s",
            blocked,
        )
        edited_files = {k: v for k, v in edited_files.items() if k in allowed_paths}

    # ── 6. Dry run: just print ───────────────────────────────────────
    if args.dry_run:
        print("\n" + "═" * 60)
        print("DRY RUN — files would be written:")
        print("═" * 60)
        for path, content in edited_files.items():
            print(f"\n{'─' * 40}")
            print(f"File: {path}")
            print(f"{'─' * 40}")
            print(content[:500] + ("..." if len(content) > 500 else ""))
        print("\n" + "═" * 60)
        print(f"Summary: {summary}")
        return

    # ── 7. Write files ───────────────────────────────────────────────
    written = write_files(edited_files, repo_root)

    if not written:
        log.warning("No files were written.")
        sys.exit(0)

    log.info("═══ TicketToCode Agent Complete ═══")
    log.info("Written %d file(s): %s", len(written), written)
    log.info("Summary: %s", summary)

    # Print summary for GitHub Actions step summary
    print(f"\n✅ TicketToCode agent completed successfully.")
    print(f"   Modified {len(written)} file(s): {', '.join(written)}")
    print(f"   Summary: {summary}")


if __name__ == "__main__":
    main()

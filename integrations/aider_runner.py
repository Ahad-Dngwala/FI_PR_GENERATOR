"""
FI-PR-GENERATOR — Aider Runner
Subprocess wrapper for Aider: repo-map generation and code editing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Which Aider model maps to our internal model IDs
AIDER_MODEL_MAP = {
    "claude-sonnet-4-5":                     "anthropic/claude-sonnet-4-5",
    "qwen/qwen-2.5-coder-72b-instruct":      "openrouter/qwen/qwen-2.5-coder-72b-instruct",
    "qwen/qwen-2.5-coder-32b-instruct":      "openrouter/qwen/qwen-2.5-coder-32b-instruct",
    "deepseek/deepseek-coder-v2":            "openrouter/deepseek/deepseek-coder-v2",
}


def _aider_bin() -> str:
    """Locate aider binary in PATH or current venv."""
    # Try venv Scripts first (Windows)
    venv_aider = Path(sys.executable).parent / "aider"
    if venv_aider.exists():
        return str(venv_aider)
    venv_aider_win = Path(sys.executable).parent / "aider.cmd"
    if venv_aider_win.exists():
        return str(venv_aider_win)
    return "aider"   # fallback to PATH


def build_repo_map(local_path: Path, max_tokens: int = 4096) -> str:
    """
    Use Aider's repo-map to generate a compact structural map of the codebase.
    Returns the map as a string (file tree + key symbols).
    """
    log.info("aider_runner.building_repo_map", path=str(local_path))
    try:
        result = subprocess.run(
            [
                _aider_bin(),
                "--show-repo-map",
                "--map-tokens", str(max_tokens),
                "--no-git",
            ],
            cwd=str(local_path),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": ""},
            shell=os.name == "nt",
        )
        if result.returncode == 0 and result.stdout.strip():
            log.info("aider_runner.repo_map_built",
                     lines=len(result.stdout.splitlines()))
            return result.stdout
        else:
            log.warning("aider_runner.repo_map_fallback",
                        stderr=result.stderr[:200])
            return _fallback_file_tree(local_path)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("aider_runner.repo_map_error", error=str(e))
        return _fallback_file_tree(local_path)


def _fallback_file_tree(local_path: Path, max_files: int = 100) -> str:
    """
    Simple directory tree when Aider is unavailable.
    Skips node_modules, .git, __pycache__, dist, build.
    """
    SKIP = {".git", "node_modules", "__pycache__", "dist", "build",
            ".next", "venv", ".venv", ".env"}
    lines = []
    count = 0
    for root, dirs, files in os.walk(local_path):
        dirs[:] = [d for d in dirs if d not in SKIP]
        rel = Path(root).relative_to(local_path)
        depth = len(rel.parts)
        indent = "  " * depth
        for file in sorted(files):
            if count >= max_files:
                lines.append(f"  ... (truncated)")
                return "\n".join(lines)
            ext = Path(file).suffix.lower()
            if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".md",
                       ".yml", ".yaml", ".json", ".toml", ".cfg"}:
                lines.append(f"{indent}{rel / file}")
                count += 1
    return "\n".join(lines)


def get_relevant_files(repo_map: str, issue_keywords: list[str],
                       max_files: int = 5) -> list[str]:
    """
    Score each file in repo_map by keyword matches.
    Returns top N most relevant file paths.
    """
    if not repo_map or not issue_keywords:
        return []

    file_scores: dict[str, int] = {}
    current_file = None

    for line in repo_map.splitlines():
        # Detect file headers in Aider's repo map format
        stripped = line.strip()
        if stripped and not stripped.startswith((" ", "\t")) and "/" in stripped:
            current_file = stripped.split()[0]
            file_scores.setdefault(current_file, 0)
        elif current_file:
            for kw in issue_keywords:
                if kw.lower() in line.lower():
                    file_scores[current_file] = file_scores.get(current_file, 0) + 1

    # Sort by score descending
    sorted_files = sorted(file_scores.items(), key=lambda x: x[1], reverse=True)
    top_files = [f for f, s in sorted_files if s > 0][:max_files]

    log.info("aider_runner.relevant_files", files=top_files,
             keywords=issue_keywords)
    return top_files


def verify_files_contain_keywords(local_path: Path, files: list[str],
                                  keywords: list[str]) -> dict[str, bool]:
    """
    Grep-based verification: does each file actually contain issue keywords?
    Returns {filename: True/False}.
    This prevents sending irrelevant context to Claude.
    """
    results = {}
    for filepath in files:
        full_path = local_path / filepath
        if not full_path.exists():
            results[filepath] = False
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="ignore").lower()
            matches = any(kw.lower() in content for kw in keywords)
            results[filepath] = matches
        except Exception:
            results[filepath] = False

    # Log files that failed verification
    failed = [f for f, ok in results.items() if not ok]
    if failed:
        log.warning("aider_runner.files_failed_verification", files=failed)

    return results


def run_aider_fix(
    local_path: Path,
    issue_text: str,
    approved_files: list[str],
    model: str,
    dry_run: bool = False,
    extra_context: str = "",
) -> tuple[bool, str]:
    """
    Run Aider to edit files based on the issue.
    Returns (success: bool, diff: str).

    The message instructs Aider to:
    - Only touch the approved files
    - Make minimal changes
    - Match repo code style
    - Output in unified diff format
    """
    aider_model = AIDER_MODEL_MAP.get(model, model)

    # Build the focused message
    files_hint = "\n".join(f"- {f}" for f in approved_files)
    message = f"""Fix the following GitHub issue with MINIMAL changes.

Issue:
{issue_text}

Files you MAY edit (do not touch any other files):
{files_hint}

{('Additional context:\n' + extra_context) if extra_context else ''}

Requirements:
1. Make the smallest correct fix — do not refactor anything unrelated
2. Match the existing code style exactly
3. Do not add new imports that don't already exist in the codebase
4. If tests exist for the affected code, add a regression test
5. Commit with a concise lowercase message (e.g., "fix: navbar overlap on mobile")
"""

    cmd = [
        _aider_bin(),
        "--model", aider_model,
        "--message", message,
        "--yes",                    # non-interactive
        "--no-stream",
        "--no-pretty",
        "--no-gitignore",           # prevent Aider from modifying .gitignore (triggers scope violation)
        *approved_files,            # pass files explicitly
    ]

    # Set API keys as env vars
    env = {**os.environ}

    log.info("aider_runner.running", model=aider_model, files=approved_files)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(local_path),
            capture_output=True,
            text=True,
            timeout=300,   # 5 min max
            env=env,
        )
        success = result.returncode == 0
        output = result.stdout + result.stderr

        if success:
            log.info("aider_runner.success", model=aider_model)
        else:
            log.warning("aider_runner.failed",
                        model=aider_model,
                        returncode=result.returncode,
                        stderr=result.stderr[:300])

        return success, output

    except subprocess.TimeoutExpired:
        log.error("aider_runner.timeout", model=aider_model)
        return False, "Aider timed out (300s limit)"
    except FileNotFoundError:
        log.error("aider_runner.not_installed")
        return False, "Aider not installed. Run: pip install aider-chat"


def extract_keywords_from_issue(title: str, body: str) -> list[str]:
    """
    Extract meaningful keywords from an issue for context retrieval.
    Filters out stop words and short tokens.
    """
    STOP_WORDS = {"the", "a", "an", "is", "in", "on", "at", "to", "of",
                  "for", "and", "or", "but", "not", "with", "this", "that",
                  "it", "be", "are", "was", "were", "has", "have", "had",
                  "will", "would", "can", "could", "should", "may", "might",
                  "i", "we", "you", "he", "she", "they", "when", "where",
                  "how", "what", "why", "which", "if", "then", "else"}

    text = f"{title} {body}".lower()
    # Extract words + common code identifiers
    tokens = re.findall(r"[a-z][a-z0-9_]{2,}", text)
    keywords = [t for t in tokens if t not in STOP_WORDS and len(t) > 2]

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            unique.append(k)

    return unique[:20]   # top 20 keywords

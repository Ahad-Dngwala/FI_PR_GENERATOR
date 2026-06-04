"""
integrations/aider_runner.py — Aider subprocess wrapper for repo-map and patch application.

Aider is called as a subprocess — we never import it directly.
All paths are handled via pathlib.Path for Windows compatibility.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Maximum files to return from context retrieval
MAX_RELEVANT_FILES = 5

# ---------------------------------------------------------------------------
# Context retrieval filters — prevent ripgrep from matching data/docs/seed
# ---------------------------------------------------------------------------

EXCLUDED_DIRS = [
    "data/", "seed/", "docs/", "doc/", "node_modules/", "__pycache__/",
    ".git/", "vendor/", "assets/images/", "public/images/", "migrations/",
    "fixtures/", ".aider", "coverage/", "dist/", "build/", ".next/",
    "static/fonts/", "static/images/", ".vscode/", ".idea/",
]

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sass", ".less",
    ".html", ".vue", ".svelte", ".java", ".kt", ".go", ".rs", ".rb",
    ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".dart",
    ".yaml", ".yml", ".toml", ".xml", ".sql", ".sh", ".bat",
    ".dockerfile", ".tf", ".hcl", ".prisma", ".graphql", ".proto",
}

# Glob patterns passed to ripgrep --glob to pre-filter at search time
_RG_GLOB_EXCLUDES = [
    "!data/", "!seed/", "!docs/", "!doc/", "!node_modules/",
    "!.git/", "!vendor/", "!dist/", "!build/", "!coverage/",
    "!.next/", "!.aider*", "!fixtures/", "!migrations/",
    "!*.txt", "!*.md", "!*.pdf", "!*.csv", "!*.log",
    "!*.png", "!*.jpg", "!*.jpeg", "!*.gif", "!*.svg",
    "!*.ico", "!*.woff", "!*.woff2", "!*.ttf", "!*.eot",
    "!*.min.js", "!*.min.css", "!*.map",
    "!package-lock.json", "!yarn.lock", "!poetry.lock", "!Pipfile.lock",
]


def _run_subprocess(
    args: list[str],
    cwd: str,
    timeout: int = 120,
    env: Optional[dict] = None,
) -> tuple[int, str, str]:
    """
    Run a subprocess and return (returncode, stdout, stderr).

    Uses shell=False on all platforms for security. On Windows, aider
    must be on PATH (installed via pip install aider-chat).
    """
    import os
    env_copy = (env if env is not None else os.environ).copy()
    env_copy["PYTHONIOENCODING"] = "utf-8"

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
            env=env_copy,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as exc:
        return -1, "", f"Command not found: {args[0]} — {exc}"
    except subprocess.TimeoutExpired:
        return -2, "", f"Timeout after {timeout}s"


def get_repo_map(repo_path: str) -> str:
    """
    Generate an Aider repo-map for the given repository.

    Runs: aider --map-tokens 2048 --show-repo-map
    Returns the repo-map string, or empty string if aider is not available.
    """
    from memory.config_loader import get_coding_chain
    chain = get_coding_chain()
    model = chain[0][0] if chain else "gemini/gemini-2.5-flash"

    code, stdout, stderr = _run_subprocess(
        args=[
            "aider",
            "--model", model,
            "--map-tokens", "2048",
            "--show-repo-map",
            "--no-auto-commit",
            "--yes-always",
            "--no-gitignore",
        ],
        cwd=repo_path,
        timeout=120,
    )

    if code == -1:
        log.warning(
            "aider.not_found",
            hint="Install with: pip install aider-chat",
            fallback="Proceeding without repo-map",
        )
        return ""

    if code != 0:
        log.warning("aider.repo_map_failed", returncode=code, stderr=stderr[:300])
        return ""

    log.info("aider.repo_map_generated", lines=len(stdout.splitlines()))
    return stdout


def find_relevant_files(
    repo_path: str, issue_text: str, repo_map: str
) -> list[str]:
    """
    Use ripgrep to find *source code* files related to the issue.

    Strategy:
    1. Extract meaningful keywords from issue text (skip stop words)
    2. Run rg -l with --glob exclusions to skip data/docs/media files
    3. Deduplicate and rank by hit count
    4. Filter through _is_source_file() to reject non-code matches
    5. Return top MAX_RELEVANT_FILES paths

    Falls back to a simple repo_map filename scan if rg is not available.
    """
    import re

    repo = Path(repo_path)

    # Extract keywords: words longer than 3 chars, skip common stop words
    stop_words = {
        "this", "that", "with", "from", "have", "when", "then", "also",
        "should", "would", "could", "will", "been", "into", "there", "their",
        "issue", "error", "problem", "please", "need", "want", "make",
    }
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z_0-9]{3,}\b", issue_text)
    keywords = list(
        dict.fromkeys(  # preserve order, deduplicate
            w for w in words if w.lower() not in stop_words
        )
    )[:10]  # max 10 keywords to search

    file_hits: dict[str, int] = {}

    for keyword in keywords:
        # Build ripgrep command with glob exclusions to pre-filter
        rg_args = ["rg", "-l", "--max-filesize", "500K"]
        for glob in _RG_GLOB_EXCLUDES:
            rg_args.extend(["--glob", glob])
        rg_args.extend([keyword, "."])

        code, stdout, _ = _run_subprocess(
            args=rg_args,
            cwd=repo_path,
            timeout=30,
        )
        if code == 0:
            for line in stdout.splitlines():
                normalized = str(Path(line).as_posix())
                file_hits[normalized] = file_hits.get(normalized, 0) + 1

    if not file_hits:
        log.warning(
            "aider.rg_no_hits",
            keywords=keywords[:5],
            hint="rg (ripgrep) may not be installed or no matches found",
        )
        # Fallback: extract file paths mentioned in repo_map
        return _extract_files_from_repo_map(repo_map, repo_path)

    # Rank by hit count, filter to files that actually exist AND are source code
    ranked = sorted(file_hits.items(), key=lambda x: x[1], reverse=True)
    result: list[str] = []
    skipped: list[str] = []
    for rel_path, hits in ranked:
        abs_path = repo / rel_path
        if abs_path.exists() and abs_path.is_file():
            if _is_source_file(rel_path):
                result.append(str(abs_path.as_posix()))
            else:
                skipped.append(rel_path)
        if len(result) >= MAX_RELEVANT_FILES:
            break

    if skipped:
        log.info(
            "aider.files_filtered_out",
            count=len(skipped),
            examples=skipped[:5],
            reason="non-source files excluded (data/docs/media/lock)",
        )

    log.info(
        "aider.relevant_files_found",
        count=len(result),
        keywords_used=len(keywords),
        files=result,
    )
    return result


def apply_patch(
    repo_path: str, patch: str, model: str = "gemini/gemini-2.5-pro"
) -> bool:
    """
    Apply a unified diff patch to the repository using Aider.

    Strategy:
    1. Write patch content to a temp file
    2. Run: aider --model {model} --apply {patch_file} --yes --no-auto-commit
    3. If successful, stage all changes (git add -A)
    4. Return True on success, False if aider errors

    The caller (orchestrator) is responsible for committing.
    """
    if not patch.strip():
        log.warning("aider.empty_patch")
        return False

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".diff",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(patch)
        tmp_path = tmp.name

    try:
        code, stdout, stderr = _run_subprocess(
            args=[
                "aider",
                "--model", model,
                "--apply", tmp_path,
                "--yes",
                "--yes-always",
                "--no-gitignore",
                "--no-auto-commit",
            ],
            cwd=repo_path,
            timeout=180,
        )

        if code == -1:
            log.warning(
                "aider.not_found_for_apply",
                fallback="Attempting direct patch application",
            )
            return _apply_patch_directly(repo_path, patch)

        if code != 0:
            log.error(
                "aider.apply_failed",
                returncode=code,
                stderr=stderr[:500],
            )
            return False

        log.info("aider.patch_applied", model=model)
        return True

    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _apply_patch_directly(repo_path: str, patch: str) -> bool:
    """
    Fallback: apply patch using 'git apply' when aider is not available.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".diff", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(patch)
        tmp_path = tmp.name

    try:
        code, stdout, stderr = _run_subprocess(
            args=["git", "apply", "--whitespace=fix", tmp_path],
            cwd=repo_path,
            timeout=30,
        )
        if code == 0:
            log.info("aider.fallback_git_apply_succeeded")
            return True
        log.error("aider.fallback_git_apply_failed", stderr=stderr[:300])
        return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _extract_files_from_repo_map(repo_map: str, repo_path: str) -> list[str]:
    """
    Parse file paths out of an Aider repo-map string.

    Used as a fallback when ripgrep is not available.
    """
    import re

    repo = Path(repo_path)
    pattern = re.compile(r"^([\w./-]+\.\w+)", re.MULTILINE)
    seen: list[str] = []
    for match in pattern.finditer(repo_map):
        rel = match.group(1)
        abs_path = repo / rel
        if abs_path.exists() and _is_source_file(rel):
            seen.append(str(abs_path.as_posix()))
        if len(seen) >= MAX_RELEVANT_FILES:
            break
    return seen


def _is_source_file(path: str) -> bool:
    """
    Return True if the path is a source/config file worth sending to the LLM.

    Rejects:
    - Files in excluded directories (data/, seed/, docs/, node_modules/, etc.)
    - Lock files and generated artifacts
    - Non-code file extensions (.txt, .pdf, .csv, .md, images, fonts, etc.)
    """
    path_lower = path.lower().replace("\\", "/")

    # Exclude directories containing data, docs, generated content
    if any(d in path_lower for d in EXCLUDED_DIRS):
        return False

    # Exclude lock/generated files by name pattern
    skip_patterns = [
        "node_modules", ".min.", "package-lock.json", "yarn.lock",
        "poetry.lock", "Pipfile.lock", ".pyc", "__pycache__",
    ]
    if any(p in path_lower for p in skip_patterns):
        return False

    # Must have a recognized code extension
    ext = Path(path).suffix.lower()
    if ext not in CODE_EXTENSIONS:
        return False

    return True

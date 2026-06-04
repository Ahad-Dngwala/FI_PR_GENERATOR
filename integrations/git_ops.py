"""
integrations/git_ops.py — GitPython operations for clone, branch, rebase, push.

Every function returns a value indicating success/failure rather than raising,
so the orchestrator can handle failures gracefully without crashing the pipeline.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import structlog
from git import GitCommandError, InvalidGitRepositoryError, Repo

log = structlog.get_logger(__name__)

# Local directory to store cloned repositories
REPOS_ROOT = Path("repos")


def _slugify(text: str) -> str:
    """Convert arbitrary text to lowercase-hyphenated slug, max 40 chars."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:40]


def get_branch_name(issue_number: int, issue_title: str) -> str:
    """
    Generate a branch name following the convention: fix/{slug}-{issue_number}

    Example: fix/navbar-mobile-overlap-42
    """
    slug = _slugify(issue_title)
    return f"fix/{slug}-{issue_number}"


def clone_repo(repo_url: str, local_path: str) -> Optional[Repo]:
    """
    Clone a repository if it does not already exist locally.
    If it already exists, pull latest changes from origin.

    Returns the GitPython Repo object, or None on failure.
    """
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if (path / ".git").exists():
            log.info("git.pulling_existing", path=str(path))
            repo = Repo(str(path))
            # Switch to default branch first so origin.pull() doesn't fail on a custom local branch
            default = get_default_branch(repo)
            repo.git.checkout(default)
            origin = repo.remotes.origin
            origin.pull()
            log.info("git.pull_done", path=str(path))
            return repo
        else:
            log.info("git.cloning", url=repo_url, path=str(path))
            repo = Repo.clone_from(repo_url, str(path))
            log.info("git.clone_done", path=str(path))
            return repo
    except GitCommandError as exc:
        log.error("git.clone_failed", url=repo_url, path=str(path), error=str(exc))
        return None
    except InvalidGitRepositoryError as exc:
        log.error("git.invalid_repo", path=str(path), error=str(exc))
        return None


def get_default_branch(repo: Repo) -> str:
    """Detect the default branch name (main or master)."""
    try:
        ref = repo.remotes.origin.refs
        for candidate in ["main", "master", "develop"]:
            if hasattr(ref, candidate):
                return candidate
        # Fall back to HEAD
        return repo.active_branch.name
    except Exception:
        return "main"


def create_branch(repo: Repo, branch_name: str) -> bool:
    """
    Create a new branch from the latest remote default branch.

    Fetches from origin first to ensure we are up to date.
    Returns True on success, False if the branch already exists or on error.
    """
    try:
        default = get_default_branch(repo)
        repo.remotes.origin.fetch()
        # Checkout default branch cleanly
        repo.git.checkout(default)
        repo.git.pull("origin", default)
        # Create and checkout the new branch
        repo.git.checkout("-b", branch_name)
        log.info("git.branch_created", branch=branch_name, from_branch=default)
        return True
    except GitCommandError as exc:
        log.error("git.create_branch_failed", branch=branch_name, error=str(exc))
        return False


def rebase_from_main(repo: Repo) -> bool:
    """
    Fetch latest from origin and rebase the current branch onto main/master.

    Does NOT auto-resolve conflicts — returns False if conflicts exist.
    The orchestrator will then move to BLOCKED state.
    """
    default = get_default_branch(repo)
    try:
        repo.remotes.origin.fetch()
        repo.git.rebase(f"origin/{default}")
        log.info("git.rebase_done", onto=f"origin/{default}")
        return True
    except GitCommandError as exc:
        error_msg = str(exc)
        log.warning("git.rebase_conflict", error=error_msg)
        # Abort the rebase so the repo is left in a clean state
        try:
            repo.git.rebase("--abort")
            log.info("git.rebase_aborted")
        except GitCommandError:
            pass
        return False


def get_diff(repo: Repo) -> str:
    """
    Return the unified diff of all staged + unstaged changes vs HEAD.

    Returns empty string if there are no changes.
    """
    try:
        # Include both staged and unstaged changes vs HEAD
        diff = repo.git.diff("HEAD", unified=3)
        return diff
    except GitCommandError as exc:
        log.error("git.get_diff_failed", error=str(exc))
        return ""


def count_diff_lines(diff: str) -> int:
    """
    Count only added (+) and removed (-) lines in a unified diff.

    Skips context lines and diff headers (+++/---).
    """
    count = 0
    for line in diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            count += 1
    return count


def push_branch(repo: Repo, branch: str) -> bool:
    """
    Push the named branch to origin.

    Returns False on failure. Never raises.
    """
    try:
        repo.git.push("origin", branch, "--set-upstream")
        log.info("git.pushed", branch=branch)
        return True
    except GitCommandError as exc:
        log.error("git.push_failed", branch=branch, error=str(exc))
        return False


def stage_and_commit(repo: Repo, message: str) -> bool:
    """
    Stage all changes (git add -A) and commit with the given message.

    Returns False on failure.
    """
    try:
        repo.git.add("-A")
        # Check if there is anything to commit
        if not repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            log.warning("git.nothing_to_commit")
            return False
        repo.git.commit("-m", message)
        log.info("git.committed", message=message[:80])
        return True
    except GitCommandError as exc:
        log.error("git.commit_failed", error=str(exc))
        return False


def reset_to_head(repo: Repo) -> None:
    """
    Hard-reset working tree to HEAD and clean untracked files.

    Used to roll back a failed patch attempt before a retry.
    """
    try:
        repo.git.reset("--hard", "HEAD")
        repo.git.clean("-fd")
        log.info("git.reset_to_head")
    except GitCommandError as exc:
        log.warning("git.reset_failed", error=str(exc))


def get_local_path(org: str, repo: str) -> Path:
    """Return the canonical local clone path for an org/repo."""
    return REPOS_ROOT / org / repo


def create_draft_pr(
    org: str,
    repo: str,
    branch: str,
    title: str,
    body: str,
) -> Optional[str]:
    """
    Create a draft PR via the gh CLI.

    Returns the PR URL on success, None on failure.
    Requires gh CLI to be installed and authenticated.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--draft",
                "--title", title,
                "--body", body,
                "--head", branch,
                "--repo", f"{org}/{repo}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            pr_url = result.stdout.strip()
            log.info("git.draft_pr_created", url=pr_url, branch=branch)
            return pr_url
        else:
            log.error("git.draft_pr_failed", stderr=result.stderr, branch=branch)
            return None
    except FileNotFoundError:
        log.error("git.gh_cli_not_found", hint="Install gh CLI: https://cli.github.com")
        return None
    except subprocess.TimeoutExpired:
        log.error("git.draft_pr_timeout", branch=branch)
        return None

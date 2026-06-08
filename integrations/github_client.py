"""
integrations/github_client.py — PyGithub wrapper with caching and retry logic.

All GitHub API calls go through this module. Never use PyGithub directly
in business logic — always call these functions.

Rate limit handling: tenacity wraps every call with exponential backoff.
Caching: requests_cache installed globally (SQLite backend, gitignored).
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Optional

import requests_cache
import structlog
from github import Auth, Github, GithubException, RateLimitExceededException, UnknownObjectException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)

# Install a global SQLite-backed cache for requests.
# Only caches GET requests — mutations (POST comments) always bypass.
requests_cache.install_cache(
    "github_cache",
    expire_after=900,  # 15 minutes default; overridden per call where needed
    allowable_methods=["GET"],
)

_gh_client: Optional[Github] = None


def _get_client() -> Github:
    """Return a cached Github client initialised from GITHUB_TOKEN env var."""
    global _gh_client
    if _gh_client is None:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise EnvironmentError("GITHUB_TOKEN environment variable is not set")
        _gh_client = Github(token)
    return _gh_client


# ---------------------------------------------------------------------------
# Retry decorator — wraps every GitHub API call
# ---------------------------------------------------------------------------

_github_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type(RateLimitExceededException),
    reraise=True,
)


def _safe_call(fn, *args, **kwargs):
    """Execute fn with retry logic on RateLimitExceededException."""

    @_github_retry
    def _inner():
        return fn(*args, **kwargs)

    return _inner()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_org_repos(org_name: str) -> list[dict]:
    """
    Fetch all public repos for an organisation.

    Cached for 1 hour. Returns minimal metadata needed for activity scoring.
    """
    gh = _get_client()
    try:
        with requests_cache.enabled(expire_after=3600):
            org = _safe_call(gh.get_organization, org_name)
            repos = _safe_call(org.get_repos, type="public")
            result = []
            for r in repos:
                result.append(
                    {
                        "name": r.name,
                        "full_name": r.full_name,
                        "html_url": r.html_url,
                        "clone_url": r.clone_url,
                        "size_kb": r.size,
                        "language": r.language,
                        "open_issues_count": r.open_issues_count,
                        "archived": r.archived,
                        "disabled": r.disabled,
                    }
                )
            log.info("github.get_org_repos", org=org_name, count=len(result))
            return result
    except UnknownObjectException:
        log.error("github.org_not_found", org=org_name)
        return []
    except GithubException as exc:
        log.error("github.get_org_repos_failed", org=org_name, error=str(exc))
        return []


def get_open_issues(org: str, repo: str) -> list[dict]:
    """
    Fetch open, unassigned issues — filtered for coding eligibility.

    Filters out:
    - Locked issues
    - Already assigned issues
    - Pull request objects masquerading as issues
    - Issues labelled 'wontfix'

    Cached for 15 minutes.
    """
    gh = _get_client()
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        issues_raw = _safe_call(r.get_issues, state="open", assignee="none")
        result = []
        for issue in issues_raw:
            # Skip PRs (GitHub API returns them as issues)
            if issue.pull_request is not None:
                continue
            if issue.locked:
                continue
            labels = [lbl.name.lower() for lbl in issue.labels]
            if "wontfix" in labels or "invalid" in labels:
                continue
            result.append(_serialize_issue(issue))
        log.info("github.get_open_issues", org=org, repo=repo, count=len(result))
        return result
    except UnknownObjectException:
        log.error("github.repo_not_found", org=org, repo=repo)
        return []
    except GithubException as exc:
        log.error("github.get_open_issues_failed", org=org, repo=repo, error=str(exc))
        return []


def get_closed_prs(org: str, repo: str, limit: int = 50) -> list[dict]:
    """
    Fetch the last N merged pull requests with their body and review comments.

    Used by memory_builder to extract repo conventions and maintainer preferences.
    """
    gh = _get_client()
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        pulls = _safe_call(r.get_pulls, state="closed", sort="updated", direction="desc")
        result = []
        for pr in pulls:
            if pr.merged_at is None:
                continue  # closed but not merged
            reviews = []
            try:
                for review in _safe_call(pr.get_reviews):
                    reviews.append(
                        {
                            "user": review.user.login if review.user else None,
                            "state": review.state,
                            "body": review.body,
                            "submitted_at": (
                                review.submitted_at.isoformat()
                                if review.submitted_at
                                else None
                            ),
                        }
                    )
            except GithubException:
                pass  # reviews unavailable — continue without them

            result.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "body": pr.body or "",
                    "user": pr.user.login if pr.user else None,
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                    "head_ref": pr.head.ref,
                    "changed_files": pr.changed_files,
                    "additions": pr.additions,
                    "deletions": pr.deletions,
                    "labels": [lbl.name for lbl in pr.labels],
                    "reviews": reviews,
                }
            )
            if len(result) >= limit:
                break

        log.info("github.get_closed_prs", org=org, repo=repo, count=len(result))
        return result
    except GithubException as exc:
        log.error("github.get_closed_prs_failed", org=org, repo=repo, error=str(exc))
        return []


def get_issue(org: str, repo: str, number: int) -> Optional[dict]:
    """
    Fetch a single issue — always live, never from cache.

    Used to verify assignment status right before coding begins.
    """
    gh = _get_client()
    try:
        with requests_cache.disabled():
            r = _safe_call(gh.get_repo, f"{org}/{repo}")
            issue = _safe_call(r.get_issue, number)
            return _serialize_issue(issue)
    except UnknownObjectException:
        log.warning("github.issue_not_found", org=org, repo=repo, number=number)
        return None
    except GithubException as exc:
        log.error("github.get_issue_failed", org=org, repo=repo, number=number, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Persistent deduplication guard for post_comment
# ---------------------------------------------------------------------------

_COMMENTED_ISSUES_PATH = pathlib.Path("state") / "commented_issues.json"


def _load_commented_issues() -> set[str]:
    """Load the persisted set of commented issue keys from disk.

    Returns an empty set if the file does not exist or is unreadable,
    so a corrupted/missing file degrades gracefully rather than crashing.
    """
    try:
        if _COMMENTED_ISSUES_PATH.exists():
            data = json.loads(_COMMENTED_ISSUES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("github.commented_issues_load_failed", path=str(_COMMENTED_ISSUES_PATH), error=str(exc))
    return set()


def _persist_commented_issues() -> None:
    """Write the in-memory set to disk atomically (write-then-rename).

    Atomic rename means a crash mid-write cannot corrupt the existing file.
    The state/ directory is created if it does not yet exist.
    """
    try:
        _COMMENTED_ISSUES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _COMMENTED_ISSUES_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(sorted(_commented_issues), indent=2),
            encoding="utf-8",
        )
        tmp.replace(_COMMENTED_ISSUES_PATH)
    except Exception as exc:  # noqa: BLE001
        log.error("github.commented_issues_persist_failed", path=str(_COMMENTED_ISSUES_PATH), error=str(exc))


# Module-level set — loaded from disk on import so the guard survives restarts
_commented_issues: set[str] = _load_commented_issues()


def post_comment(org: str, repo: str, issue_number: int, body: str) -> bool:
    """
    Post a comment on an issue requesting assignment.

    Rate-limited: tracks commented issues in a local set to prevent duplicate comments.
    The set is persisted to state/commented_issues.json so the guard survives
    process restarts and crashes.
    Always bypasses cache (mutations are never cached).
    """
    commented_key = f"{org}/{repo}#{issue_number}"
    if commented_key in _commented_issues:
        log.info("github.comment_skipped_duplicate", org=org, repo=repo, issue=issue_number)
        return False

    gh = _get_client()
    try:
        with requests_cache.disabled():
            r = _safe_call(gh.get_repo, f"{org}/{repo}")
            issue = _safe_call(r.get_issue, issue_number)
            _safe_call(issue.create_comment, body)
            _commented_issues.add(commented_key)
            _persist_commented_issues()
            log.info("github.comment_posted", org=org, repo=repo, issue=issue_number)
            return True
    except GithubException as exc:
        log.error("github.post_comment_failed", org=org, repo=repo, issue=issue_number, error=str(exc))
        return False


def check_assignment(
    org: str, repo: str, issue_number: int, github_username: str
) -> bool:
    """
    Return True only if the issue is assigned to github_username.

    Always makes a live API call — never uses cache.
    Used both before coding starts and again before pushing.
    """
    issue = get_issue(org, repo, issue_number)
    if issue is None:
        return False
    return any(a == github_username for a in issue.get("assignees", []))


def get_repo_activity(org: str, repo: str) -> dict:
    """
    Return key activity signals for repository scoring.

    Returns:
        {
            last_commit_days: int,
            last_merge_days: int,
            avg_review_days: float,
            open_count: int,
        }
    """
    gh = _get_client()
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        now = datetime.now(tz=timezone.utc)

        # Days since last push
        pushed_at = r.pushed_at
        if pushed_at is None:
            last_commit_days = 9999
        else:
            if pushed_at.tzinfo is None:
                pushed_at = pushed_at.replace(tzinfo=timezone.utc)
            last_commit_days = max(0, (now - pushed_at).days)

        # Days since last merged PR
        last_merge_days = 9999
        try:
            pulls = _safe_call(r.get_pulls, state="closed", sort="updated", direction="desc")
            for pr in pulls:
                if pr.merged_at:
                    merged = pr.merged_at
                    if merged.tzinfo is None:
                        merged = merged.replace(tzinfo=timezone.utc)
                    last_merge_days = max(0, (now - merged).days)
                    break
        except GithubException:
            pass

        # Average days from issue-open to first review comment (sample last 20 PRs)
        review_days_list: list[float] = []
        try:
            pulls2 = _safe_call(r.get_pulls, state="closed", sort="updated", direction="desc")
            count = 0
            for pr in pulls2:
                if pr.merged_at is None:
                    continue
                try:
                    reviews = list(_safe_call(pr.get_reviews))
                    if reviews and pr.created_at:
                        first_review = reviews[0].submitted_at
                        if first_review:
                            created = pr.created_at
                            if created.tzinfo is None:
                                created = created.replace(tzinfo=timezone.utc)
                            if first_review.tzinfo is None:
                                first_review = first_review.replace(tzinfo=timezone.utc)
                            review_days_list.append((first_review - created).total_seconds() / 86400)
                except GithubException:
                    pass
                count += 1
                if count >= 20:
                    break
        except GithubException:
            pass

        avg_review_days = (
            sum(review_days_list) / len(review_days_list) if review_days_list else 7.0
        )

        result = {
            "last_commit_days": last_commit_days,
            "last_merge_days": last_merge_days,
            "avg_review_days": round(avg_review_days, 2),
            "open_count": r.open_issues_count,
        }
        log.info("github.get_repo_activity", org=org, repo=repo, **result)
        return result

    except GithubException as exc:
        log.error("github.get_repo_activity_failed", org=org, repo=repo, error=str(exc))
        return {
            "last_commit_days": 9999,
            "last_merge_days": 9999,
            "avg_review_days": 99.0,
            "open_count": 0,
        }


def get_contributing_md(org: str, repo: str) -> str:
    """
    Fetch CONTRIBUTING.md content if it exists.

    Used by workflow detector during memory building.
    Returns empty string if not found.
    """
    gh = _get_client()
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        for path in ["CONTRIBUTING.md", "contributing.md", ".github/CONTRIBUTING.md"]:
            try:
                content = _safe_call(r.get_contents, path)
                return content.decoded_content.decode("utf-8", errors="replace")
            except UnknownObjectException:
                continue
        return ""
    except GithubException as exc:
        log.warning("github.get_contributing_md_failed", org=org, repo=repo, error=str(exc))
        return ""


def get_issue_templates(org: str, repo: str) -> list[str]:
    """
    Fetch issue template files from .github/ISSUE_TEMPLATE/ if they exist.

    Used by workflow detector to infer proposal-first patterns.
    """
    gh = _get_client()
    templates: list[str] = []
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        for template_dir in [".github/ISSUE_TEMPLATE", ".github/issue_template"]:
            try:
                contents = _safe_call(r.get_contents, template_dir)
                if not isinstance(contents, list):
                    contents = [contents]
                for f in contents:
                    try:
                        templates.append(f.decoded_content.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                break
            except UnknownObjectException:
                continue
    except GithubException as exc:
        log.warning("github.get_issue_templates_failed", org=org, repo=repo, error=str(exc))
    return templates


def get_bot_comments(org: str, repo: str, limit: int = 30) -> list[str]:
    """
    Sample recent bot/maintainer comments from closed issues.

    Scans recent closed issues for bot-authored comments to detect
    claim commands and auto-assignment patterns.
    """
    gh = _get_client()
    comments: list[str] = []
    try:
        r = _safe_call(gh.get_repo, f"{org}/{repo}")
        issues = _safe_call(r.get_issues, state="closed", sort="updated", direction="desc")
        count = 0
        for issue in issues:
            if issue.pull_request is not None:
                continue
            try:
                for comment in _safe_call(issue.get_comments):
                    user = comment.user
                    if user and ("[bot]" in user.login or user.type == "Bot"):
                        comments.append(comment.body)
                    elif comment.body and any(
                        kw in comment.body.lower()
                        for kw in ["/claim", "/assign", "!take", "assigned to"]
                    ):
                        comments.append(comment.body)
            except GithubException:
                pass
            count += 1
            if count >= limit or len(comments) >= 50:
                break
    except GithubException as exc:
        log.warning("github.get_bot_comments_failed", org=org, repo=repo, error=str(exc))
    return comments


def is_collaborator(org: str, repo: str, github_username: str) -> bool:
    """
    Return True if github_username has write access to org/repo.
    Uses GET /repos/{owner}/{repo}/collaborators/{username}
    Returns False on 404 (not collaborator) or any error.
    Never raises — always returns bool.
    """
    try:
        g = _get_client()
        r = _safe_call(g.get_repo, f"{org}/{repo}")
        return _safe_call(r.has_in_collaborators, github_username)
    except Exception as exc:
        log.warning("github.is_collaborator_check_failed", org=org, repo=repo, user=github_username, error=str(exc))
        return False


def get_or_create_fork(org: str, repo: str) -> tuple[str, str]:
    """
    Ensure the authenticated user has a fork of org/repo.
    If fork already exists, return it.
    If not, create it and wait for GitHub to provision it (with exponential backoff).

    Returns: (fork_owner, fork_repo_name)
    Example: ("Ahad-Dngwala", "nyay-setu-working")
    """
    g = _get_client()
    user = _safe_call(g.get_user)
    upstream = _safe_call(g.get_repo, f"{org}/{repo}")

    # Check if fork already exists
    fork_name = repo
    try:
        existing = _safe_call(g.get_repo, f"{user.login}/{fork_name}")
        if existing.fork and existing.parent.full_name == f"{org}/{repo}":
            log.info("github.fork_exists",
                     fork=f"{user.login}/{fork_name}",
                     upstream=f"{org}/{repo}")
            return user.login, fork_name
    except Exception:
        pass

    # Create fork
    log.info("github.forking", upstream=f"{org}/{repo}")
    fork = _safe_call(upstream.create_fork)

    # Wait for GitHub to provision the fork (with exponential backoff)
    delay = 2.0
    for attempt in range(8):  # 2 + 4 + 8 + 16 + 32 + 64... = ~126s max wait
        try:
            fork_repo = g.get_repo(f"{user.login}/{fork_name}")
            # Ensure default branch is populated and accessible
            _ = fork_repo.default_branch
            log.info("github.fork_created",
                     fork=f"{user.login}/{fork_name}",
                     attempt=attempt + 1)
            return user.login, fork_name
        except Exception:
            log.info("github.fork_not_ready_yet",
                     fork=f"{user.login}/{fork_name}",
                     wait_seconds=delay,
                     attempt=attempt + 1)
            time.sleep(delay)
            delay *= 2.0

    raise RuntimeError(f"Fork not available/ready after 2 minutes: {user.login}/{fork_name}")


def sync_fork_with_upstream(fork_owner: str, fork_repo: str,
                             upstream_org: str, upstream_repo: str) -> bool:
    """
    Sync the fork's default branch with upstream main/master.
    Uses GitHub API: POST /repos/{fork_owner}/{fork_repo}/merge-upstream
    Returns True on success, False on failure (non-fatal — proceed anyway).
    """
    try:
        import requests
        headers = {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        g = _get_client()
        upstream = _safe_call(g.get_repo, f"{upstream_org}/{upstream_repo}")
        default_branch = upstream.default_branch

        resp = requests.post(
            f"https://api.github.com/repos/{fork_owner}/{fork_repo}/merge-upstream",
            json={"branch": default_branch},
            headers=headers,
            timeout=15
        )
        if resp.status_code in (200, 409):
            log.info("github.fork_synced", fork=f"{fork_owner}/{fork_repo}")
            return True
        log.warning("github.fork_sync_failed", status=resp.status_code, body=resp.text[:200])
        return False
    except Exception as e:
        log.warning("github.fork_sync_error", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize_issue(issue) -> dict:
    """Convert a PyGithub Issue object to a plain dict."""
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "state": issue.state,
        "locked": issue.locked,
        "assignees": [a.login for a in issue.assignees],
        "labels": [lbl.name for lbl in issue.labels],
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "html_url": issue.html_url,
        "user": issue.user.login if issue.user else None,
    }
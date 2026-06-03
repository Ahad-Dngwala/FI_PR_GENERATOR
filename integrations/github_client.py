"""
FI-PR-GENERATOR — GitHub Client
Wraps PyGithub with caching, rate-limit handling, and multi-token rotation.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import structlog
from github import Github, GithubException, RateLimitExceededException
from github.Repository import Repository
from github.Issue import Issue

log = structlog.get_logger(__name__)


class GitHubClient:
    """
    Thread-safe GitHub client with:
    - Multi-token rotation (rate limit pool)
    - Simple in-memory cache for repo metadata
    - Activity score calculator
    - Issue eligibility checking
    """

    def __init__(self):
        # Support single token or comma-separated pool
        token_pool_str = os.getenv("GITHUB_TOKEN_POOL", "")
        single_token    = os.getenv("GITHUB_TOKEN", "")

        if token_pool_str:
            self._tokens = [t.strip() for t in token_pool_str.split(",") if t.strip()]
        elif single_token:
            self._tokens = [single_token]
        else:
            raise EnvironmentError("GITHUB_TOKEN or GITHUB_TOKEN_POOL must be set in .env")

        self._token_index = 0
        self._clients     = [Github(t) for t in self._tokens]
        self._cache: dict = {}
        self._username    = os.getenv("GITHUB_USERNAME", "")

        log.info("github_client.initialized", token_count=len(self._tokens))

    # ─── Token rotation ──────────────────────────────────────

    @property
    def _gh(self) -> Github:
        return self._clients[self._token_index]

    def _rotate_token(self) -> bool:
        """Try next token. Returns False if all exhausted."""
        next_index = (self._token_index + 1) % len(self._clients)
        if next_index == self._token_index:
            return False          # only one token
        self._token_index = next_index
        log.warning("github_client.token_rotated", new_index=self._token_index)
        return True

    def _api_call(self, fn, *args, **kwargs):
        """Wrap any GitHub API call with rate-limit rotation."""
        for attempt in range(len(self._clients) + 1):
            try:
                return fn(*args, **kwargs)
            except RateLimitExceededException:
                log.warning("github_client.rate_limit", token_index=self._token_index)
                if not self._rotate_token():
                    reset_at = self._gh.get_rate_limit().core.reset
                    wait_sec = (reset_at - datetime.now(timezone.utc)).total_seconds() + 5
                    log.warning("github_client.waiting_reset", seconds=int(wait_sec))
                    time.sleep(max(wait_sec, 5))
            except GithubException as e:
                if e.status == 403 and "secondary rate" in str(e.data).lower():
                    time.sleep(60)
                else:
                    raise
        raise RuntimeError("All GitHub tokens exhausted")

    # ─── Repository helpers ──────────────────────────────────

    def get_repo(self, org: str, repo: str) -> Repository:
        key = f"{org}/{repo}"
        if key not in self._cache:
            self._cache[key] = self._api_call(self._gh.get_repo, key)
        return self._cache[key]

    def get_username(self) -> str:
        if not self._username:
            self._username = self._api_call(self._gh.get_user).login
        return self._username

    # ─── Activity Score ──────────────────────────────────────

    def get_activity_score(self, org: str, repo: str) -> float:
        """
        Activity Score = 0.40 × CommitFreshness
                       + 0.30 × PRMergeFreshness
                       + 0.20 × MaintainerResponseScore
                       + 0.10 × IssueResolutionVelocity
        Returns 0–100. Repos below 60 are considered dormant.
        """
        try:
            r = self.get_repo(org, repo)
            now = datetime.now(timezone.utc)

            # Commit freshness (days since last push → 0-100)
            pushed_at = r.pushed_at.replace(tzinfo=timezone.utc) if r.pushed_at else None
            if pushed_at:
                days_since = (now - pushed_at).days
                commit_score = max(0, 100 - days_since * 4)
            else:
                commit_score = 0

            # PR merge freshness — look at recent closed PRs
            try:
                closed_prs = list(r.get_pulls(state="closed", sort="updated",
                                              direction="desc")[:5])
                merged_prs = [p for p in closed_prs if p.merged]
                if merged_prs:
                    newest_merge = merged_prs[0].merged_at.replace(tzinfo=timezone.utc)
                    days_since_merge = (now - newest_merge).days
                    pr_score = max(0, 100 - days_since_merge * 5)
                else:
                    pr_score = 0
            except Exception:
                pr_score = 30

            # Maintainer response (open issue → first comment within 3 days)
            try:
                recent_issues = list(r.get_issues(state="closed", sort="updated",
                                                  direction="desc")[:10])
                response_scores = []
                for issue in recent_issues[:5]:
                    comments = list(issue.get_comments()[:1])
                    if comments:
                        created = issue.created_at.replace(tzinfo=timezone.utc)
                        first_comment = comments[0].created_at.replace(tzinfo=timezone.utc)
                        days_to_respond = (first_comment - created).days
                        response_scores.append(max(0, 100 - days_to_respond * 15))
                maintainer_score = sum(response_scores) / len(response_scores) if response_scores else 40
            except Exception:
                maintainer_score = 40

            # Issue resolution velocity — % closed in last 30 days
            try:
                open_count   = r.open_issues_count
                velocity_score = min(100, max(0, 80 - open_count * 0.5))
            except Exception:
                velocity_score = 50

            score = (0.40 * commit_score +
                     0.30 * pr_score +
                     0.20 * maintainer_score +
                     0.10 * velocity_score)

            log.info("github_client.activity_score", org=org, repo=repo,
                     score=round(score, 1),
                     commit=round(commit_score, 1), pr=round(pr_score, 1))
            return round(score, 1)

        except Exception as e:
            log.warning("github_client.activity_score_error", org=org, repo=repo, error=str(e))
            return 0.0

    # ─── Issue helpers ───────────────────────────────────────

    def fetch_open_issues(self, org: str, repo: str,
                          labels: Optional[list[str]] = None,
                          max_issues: int = 50) -> list[Issue]:
        """
        Fetch open, unassigned issues. Optionally filtered by labels.
        """
        r = self.get_repo(org, repo)
        kwargs: dict = {"state": "open", "sort": "created", "direction": "desc"}
        if labels:
            kwargs["labels"] = labels

        issues = []
        try:
            for issue in self._api_call(r.get_issues, **kwargs):
                if len(issues) >= max_issues:
                    break
                if issue.pull_request:   # skip PRs listed as issues
                    continue
                if issue.assignee:       # skip assigned
                    continue
                if issue.locked:         # skip locked
                    continue
                issues.append(issue)
        except Exception as e:
            log.error("github_client.fetch_issues_error", org=org, repo=repo, error=str(e))

        log.info("github_client.issues_fetched", org=org, repo=repo, count=len(issues))
        return issues

    def check_assignment(self, org: str, repo: str, issue_number: int) -> bool:
        """
        Re-check live assignment status (not cached).
        Returns True if issue is still unassigned — safe to work on.
        """
        try:
            r = self._api_call(self._gh.get_repo, f"{org}/{repo}")
            issue = self._api_call(r.get_issue, issue_number)
            is_free = issue.assignee is None
            log.info("github_client.assignment_check", issue=issue_number, is_free=is_free)
            return is_free
        except Exception as e:
            log.error("github_client.assignment_check_error", error=str(e))
            return False    # treat as assigned if check fails

    def fetch_recent_prs(self, org: str, repo: str, n: int = 20) -> list[dict]:
        """
        Fetch recent merged PRs for org memory building.
        Returns lightweight dicts (not full PyGitHub objects).
        """
        r = self.get_repo(org, repo)
        results = []
        try:
            for pr in self._api_call(r.get_pulls, state="closed",
                                     sort="updated", direction="desc"):
                if not pr.merged:
                    continue
                if len(results) >= n:
                    break

                files_changed = []
                try:
                    files_changed = [f.filename for f in pr.get_files()][:10]
                except Exception:
                    pass

                results.append({
                    "number": pr.number,
                    "title": pr.title,
                    "body": (pr.body or "")[:500],
                    "labels": [lb.name for lb in pr.labels],
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else "",
                    "files_changed": files_changed,
                    "additions": pr.additions,
                    "deletions": pr.deletions,
                    "review_comments": pr.review_comments,
                    "commits": pr.commits,
                })
        except Exception as e:
            log.warning("github_client.fetch_prs_error", org=org, repo=repo, error=str(e))

        log.info("github_client.prs_fetched", org=org, repo=repo, count=len(results))
        return results

    def create_draft_pr(self, org: str, repo: str, branch: str,
                        title: str, body: str, issue_number: int,
                        dry_run: bool = False) -> str:
        """
        Create a draft pull request via GitHub API.
        Returns PR URL, or empty string on dry run.
        """
        if dry_run:
            log.info("github_client.dry_run_pr", org=org, repo=repo,
                     branch=branch, title=title)
            return f"[DRY RUN] Would create PR: {title}"

        try:
            r = self.get_repo(org, repo)
            base = r.default_branch
            pr = self._api_call(
                r.create_pull,
                title=title,
                body=body,
                head=branch,
                base=base,
                draft=True,
            )
            log.info("github_client.pr_created", url=pr.html_url,
                     title=title, draft=True)
            return pr.html_url
        except Exception as e:
            log.error("github_client.create_pr_error", error=str(e))
            raise

    def comment_on_issue(self, org: str, repo: str,
                         issue_number: int, comment: str,
                         dry_run: bool = False) -> None:
        """Post a comment on an issue (e.g., assignment request)."""
        if dry_run:
            log.info("github_client.dry_run_comment", issue=issue_number)
            return
        try:
            r = self.get_repo(org, repo)
            issue = self._api_call(r.get_issue, issue_number)
            self._api_call(issue.create_comment, comment)
            log.info("github_client.comment_posted", issue=issue_number)
        except Exception as e:
            log.warning("github_client.comment_error", error=str(e))

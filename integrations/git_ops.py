"""
FI-PR-GENERATOR — Git Operations
All local git work via GitPython: clone, branch, fetch, rebase, push.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import structlog
from git import GitCommandError, InvalidGitRepositoryError, Repo

log = structlog.get_logger(__name__)


class GitOps:
    """
    Handles all local repository operations.
    One instance per pipeline run (one repo at a time).
    """

    def __init__(self, github_token: str):
        self._token = github_token
        self.repo: Optional[Repo] = None
        self.local_path: Optional[Path] = None
        self._tmp_dir: Optional[str] = None

    # ─── Lifecycle ───────────────────────────────────────────

    def clone(self, org: str, repo_name: str) -> Path:
        """
        Clone repo to a temp directory.
        Returns local path. Call cleanup() when done.
        """
        self._tmp_dir = tempfile.mkdtemp(prefix="fi_pr_")
        self.local_path = Path(self._tmp_dir) / repo_name

        # Embed token in URL for auth (HTTPS)
        clone_url = f"https://{self._token}@github.com/{org}/{repo_name}.git"
        log.info("git_ops.cloning", org=org, repo=repo_name, dest=str(self.local_path))

        try:
            self.repo = Repo.clone_from(clone_url, str(self.local_path),
                                        depth=50)       # shallow clone for speed
            log.info("git_ops.cloned", path=str(self.local_path))
            return self.local_path
        except GitCommandError as e:
            log.error("git_ops.clone_failed", error=str(e))
            raise

    def cleanup(self) -> None:
        """Remove the temp directory. Always call this in a finally block."""
        if self._tmp_dir and os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            log.info("git_ops.cleanup", path=self._tmp_dir)
        self.repo = None
        self.local_path = None
        self._tmp_dir = None

    # ─── Branch management ───────────────────────────────────

    def create_branch(self, branch_name: str) -> str:
        """Create and checkout a new branch from current HEAD."""
        assert self.repo, "Must clone first"
        try:
            branch = self.repo.create_head(branch_name)
            branch.checkout()
            log.info("git_ops.branch_created", branch=branch_name)
            return branch_name
        except GitCommandError as e:
            log.error("git_ops.branch_create_failed", branch=branch_name, error=str(e))
            raise

    def current_commit(self) -> str:
        """Return current HEAD commit hash (short)."""
        assert self.repo
        return self.repo.head.commit.hexsha[:12]

    # ─── Sync with upstream ──────────────────────────────────

    def fetch_and_rebase(self) -> bool:
        """
        Fetch origin and rebase current branch onto main.
        Returns True on success, False on conflict.
        Caller must rerun tests after calling this.
        """
        assert self.repo
        try:
            log.info("git_ops.fetching")
            self.repo.remotes.origin.fetch()

            default = self._get_default_branch()
            log.info("git_ops.rebasing", onto=f"origin/{default}")
            self.repo.git.rebase(f"origin/{default}")
            log.info("git_ops.rebase_success")
            return True
        except GitCommandError as e:
            log.warning("git_ops.rebase_conflict", error=str(e))
            # Abort rebase cleanly
            try:
                self.repo.git.rebase("--abort")
            except Exception:
                pass
            return False

    def _get_default_branch(self) -> str:
        """Detect default branch (main or master)."""
        assert self.repo
        remote_refs = self.repo.remotes.origin.refs
        for ref in remote_refs:
            if ref.name in ("origin/main", "origin/master"):
                return ref.name.split("/")[-1]
        return "main"

    def has_upstream_changes(self) -> bool:
        """Check if upstream moved since we started working."""
        assert self.repo
        try:
            self.repo.remotes.origin.fetch()
            default = self._get_default_branch()
            local_sha  = self.repo.head.commit.hexsha
            remote_sha = self.repo.remotes.origin.refs[default].commit.hexsha
            changed = local_sha != remote_sha
            if changed:
                log.info("git_ops.upstream_moved",
                         local=local_sha[:8], remote=remote_sha[:8])
            return changed
        except Exception:
            return False

    # ─── Diff helpers ────────────────────────────────────────

    def get_diff(self) -> str:
        """Return the full unified diff of uncommitted changes."""
        assert self.repo
        try:
            diff = self.repo.git.diff("HEAD")
            if not diff:
                diff = self.repo.git.diff()
            return diff
        except GitCommandError:
            return ""

    def get_diff_line_count(self) -> int:
        """Count added + removed lines in current diff."""
        diff = self.get_diff()
        lines = [l for l in diff.split("\n")
                 if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        return len(lines)

    def get_changed_files(self) -> list[str]:
        """Return list of files changed vs HEAD."""
        assert self.repo
        try:
            status = self.repo.git.diff("HEAD", "--name-only")
            files = [f for f in status.strip().split("\n") if f]
            if not files:
                # Check staged changes
                status = self.repo.git.diff("--cached", "--name-only")
                files = [f for f in status.strip().split("\n") if f]
            return files
        except Exception:
            return []

    # ─── Commit & push ───────────────────────────────────────

    def stage_and_commit(self, message: str) -> str:
        """Stage all changes and create a commit. Returns commit hash."""
        assert self.repo
        self.repo.git.add("-A")
        commit = self.repo.index.commit(message)
        sha = commit.hexsha[:12]
        log.info("git_ops.committed", sha=sha, message=message)
        return sha

    def push_branch(self, branch_name: str, dry_run: bool = False) -> bool:
        """Push branch to origin. Returns True on success."""
        assert self.repo
        if dry_run:
            log.info("git_ops.dry_run_push", branch=branch_name)
            return True
        try:
            self.repo.remotes.origin.push(
                refspec=f"refs/heads/{branch_name}:refs/heads/{branch_name}",
                force=False,
            )
            log.info("git_ops.pushed", branch=branch_name)
            return True
        except GitCommandError as e:
            log.error("git_ops.push_failed", branch=branch_name, error=str(e))
            return False

    # ─── Utility ─────────────────────────────────────────────

    def run_command(self, cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
        """
        Run an arbitrary command in the repo directory.
        Returns (exit_code, stdout, stderr).
        """
        assert self.local_path
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.local_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ},
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            log.warning("git_ops.command_timeout", cmd=cmd)
            return 124, "", "Command timed out"
        except FileNotFoundError:
            return 127, "", f"Command not found: {cmd[0]}"

"""
FI-PR-GENERATOR — Org Memory Manager
Handles loading, saving, and updating per-repository knowledge bases.
All data lives in memory_store/{org_name}/{repo_name}.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog

from memory.schemas import OrgMemory

log = structlog.get_logger(__name__)

MEMORY_DIR = Path(__file__).parent.parent / "memory_store"
MEMORY_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────

def _memory_path(org: str, repo: str) -> Path:
    org_dir = MEMORY_DIR / org
    org_dir.mkdir(exist_ok=True)
    return org_dir / f"{repo}.json"


def load_memory(org: str, repo: str) -> Optional[OrgMemory]:
    """Load org memory from disk. Returns None if not found."""
    path = _memory_path(org, repo)
    if not path.exists():
        log.info("org_memory.not_found", org=org, repo=repo)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        memory = OrgMemory(**data)
        log.info("org_memory.loaded", org=org, repo=repo, version=memory.version)
        return memory
    except Exception as e:
        log.warning("org_memory.load_error", org=org, repo=repo, error=str(e))
        return None


def save_memory(memory: OrgMemory) -> None:
    """Persist org memory to disk with version bump."""
    memory.version += 1
    memory.last_refresh = datetime.utcnow().isoformat()
    path = _memory_path(memory.org_name, memory.repo_name)
    path.write_text(memory.model_dump_json(indent=2), encoding="utf-8")
    log.info("org_memory.saved", org=memory.org_name, repo=memory.repo_name,
             version=memory.version)


def create_empty_memory(org: str, repo: str) -> OrgMemory:
    """Create a fresh empty memory object for a new repo."""
    return OrgMemory(org_name=org, repo_name=repo)


def is_fresh(memory: OrgMemory, max_age_hours: int = 48) -> bool:
    """Return True if memory was refreshed within max_age_hours."""
    try:
        last = datetime.fromisoformat(memory.last_refresh)
        age = datetime.utcnow() - last
        fresh = age < timedelta(hours=max_age_hours)
        log.debug("org_memory.freshness_check", org=memory.org_name,
                  age_hours=round(age.total_seconds() / 3600, 1), fresh=fresh)
        return fresh
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Update helpers
# ─────────────────────────────────────────────────────────────

def update_from_rejection(memory: OrgMemory, reason: str,
                          issue_type: str = "", files_changed: list = None,
                          diff_size: int = 0) -> OrgMemory:
    """
    Record a human rejection into memory so future scoring improves.
    Called when user presses ❌ Reject on Telegram.
    """
    entry = {
        "rejected_at": datetime.utcnow().isoformat(),
        "issue_type": issue_type,
        "files_changed": files_changed or [],
        "rejection_reason": reason,
        "diff_size": diff_size,
    }
    memory.rejection_log.append(entry)
    # Keep only last 20 rejections (avoid memory bloat)
    memory.rejection_log = memory.rejection_log[-20:]

    # Add to rejection patterns if reason is informative
    if reason and reason not in memory.issue_rejection_patterns:
        memory.issue_rejection_patterns.append(reason)
        memory.issue_rejection_patterns = memory.issue_rejection_patterns[-10:]

    log.info("org_memory.rejection_logged", org=memory.org_name,
             repo=memory.repo_name, reason=reason)
    return memory


def update_from_acceptance(memory: OrgMemory, pr_data: dict) -> OrgMemory:
    """
    Record a successful accepted PR into memory.
    Called when maintainer merges our PR.
    """
    example = {
        "merged_at": datetime.utcnow().isoformat(),
        "pr_title": pr_data.get("title", ""),
        "issue_type": pr_data.get("issue_type", ""),
        "files_changed": pr_data.get("files_changed", []),
        "diff_size": pr_data.get("diff_size", 0),
        "labels": pr_data.get("labels", []),
    }
    memory.accepted_pr_examples.append(example)
    # Keep only last 5 examples
    memory.accepted_pr_examples = memory.accepted_pr_examples[-5:]

    pattern = pr_data.get("issue_type", "")
    if pattern and pattern not in memory.issue_acceptance_patterns:
        memory.issue_acceptance_patterns.append(pattern)

    log.info("org_memory.acceptance_logged", org=memory.org_name,
             repo=memory.repo_name, pr_title=pr_data.get("title", ""))
    return memory


def get_memory_summary(memory: OrgMemory) -> str:
    """
    Return a compact text summary of org memory for use in prompts.
    Max ~200 words. Keeps prompt lean.
    """
    lines = [
        f"Repository: {memory.org_name}/{memory.repo_name}",
        f"Default branch: {memory.default_branch}",
        f"Activity score: {memory.activity_score:.0f}/100",
    ]

    if memory.commit_style:
        lines.append(f"Commit style: {memory.commit_style}")
    if memory.pr_title_style:
        lines.append(f"PR title style: {memory.pr_title_style}")
    if memory.common_test_commands:
        lines.append(f"Test commands: {', '.join(memory.common_test_commands[:2])}")
    if memory.common_file_hotspots:
        lines.append(f"Common hotspots: {', '.join(memory.common_file_hotspots[:5])}")
    if memory.issue_acceptance_patterns:
        lines.append(f"Accepted types: {', '.join(memory.issue_acceptance_patterns[:3])}")
    if memory.issue_rejection_patterns:
        lines.append(f"Rejection reasons: {', '.join(memory.issue_rejection_patterns[:3])}")
    if memory.accepted_pr_examples:
        ex = memory.accepted_pr_examples[-1]
        lines.append(f"Recent accepted PR: '{ex.get('pr_title', '')}' "
                     f"({ex.get('diff_size', 0)} lines, {ex.get('issue_type', '')})")

    return "\n".join(lines)


def list_all_memories() -> list[tuple[str, str]]:
    """Return (org, repo) tuples for all stored memories."""
    result = []
    for org_dir in MEMORY_DIR.iterdir():
        if org_dir.is_dir():
            for f in org_dir.glob("*.json"):
                result.append((org_dir.name, f.stem))
    return result

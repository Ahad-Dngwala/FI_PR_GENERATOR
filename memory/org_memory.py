"""
memory/org_memory.py — Load, save, and update per-repository org memory.

All I/O goes through Pydantic models. Never use bare json.load() in business logic.
Files are stored at: memory_store/{org}/{repo}.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from memory.schemas import OrgMemory, RejectionEntry

log = structlog.get_logger(__name__)

# Root directory for all memory files (gitignored)
MEMORY_ROOT = Path("memory_store")


def _memory_path(org: str, repo: str) -> Path:
    """Return the canonical path for an org/repo memory file."""
    return MEMORY_ROOT / org / f"{repo}.json"


def load_org_memory(org: str, repo: str) -> Optional[OrgMemory]:
    """
    Load existing org memory from disk.

    Returns None if the file does not exist yet. Does NOT raise on missing file.
    Raises ValueError if the file exists but fails to parse.
    """
    path = _memory_path(org, repo)
    if not path.exists():
        log.info("org_memory.not_found", org=org, repo=repo, path=str(path))
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        memory = OrgMemory.model_validate(data)
        log.info(
            "org_memory.loaded",
            org=org,
            repo=repo,
            schema_version=memory.schema_version,
            confidence=memory.confidence,
        )
        return memory
    except Exception as exc:
        log.error("org_memory.load_failed", org=org, repo=repo, error=str(exc))
        raise ValueError(f"Failed to parse memory for {org}/{repo}: {exc}") from exc


def save_org_memory(memory: OrgMemory) -> None:
    """
    Persist an OrgMemory object to disk as JSON.

    Creates parent directories if needed.
    Writes atomically: writes to a .tmp file then renames.
    """
    path = _memory_path(memory.org_name, memory.repo_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            memory.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        log.info(
            "org_memory.saved",
            org=memory.org_name,
            repo=memory.repo_name,
            schema_version=memory.schema_version,
        )
    except Exception as exc:
        log.error(
            "org_memory.save_failed",
            org=memory.org_name,
            repo=memory.repo_name,
            error=str(exc),
        )
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def load_or_build_org_memory(
    org: str,
    repo: str,
    builder_fn,  # callable: (org, repo) -> OrgMemory
) -> OrgMemory:
    """
    Return existing memory if it is fresh enough, otherwise build it.

    builder_fn is typically memory_builder.build_org_memory(). Passed as
    a callable to avoid circular imports between org_memory and memory_builder.
    """
    memory = load_org_memory(org, repo)
    if memory is None:
        log.info("org_memory.building_fresh", org=org, repo=repo)
        memory = builder_fn(org, repo)
        save_org_memory(memory)
        return memory

    # Check staleness
    now = datetime.now(tz=timezone.utc)
    last = memory.last_refresh
    # Make timezone-aware if stored as naive UTC
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    age_hours = (now - last).total_seconds() / 3600
    if age_hours > memory.refresh_frequency_hours:
        log.info(
            "org_memory.stale_rebuilding",
            org=org,
            repo=repo,
            age_hours=round(age_hours, 1),
            threshold=memory.refresh_frequency_hours,
        )
        memory = builder_fn(org, repo)
        save_org_memory(memory)

    return memory


def append_rejection(org: str, repo: str, entry: RejectionEntry) -> None:
    """
    Append a RejectionEntry to the repo's rejection_log in memory.

    Used by orchestrator after human rejects a PR in ntfy.
    No-ops gracefully if memory file does not exist yet.
    """
    memory = load_org_memory(org, repo)
    if memory is None:
        log.warning(
            "org_memory.append_rejection_no_memory",
            org=org,
            repo=repo,
            issue=entry.issue_number,
        )
        return

    memory.rejection_log.append(entry)
    memory.schema_version += 1
    save_org_memory(memory)
    log.info(
        "org_memory.rejection_appended",
        org=org,
        repo=repo,
        issue=entry.issue_number,
        rejected_by=entry.rejected_by,
    )


def list_all_memories() -> list[tuple[str, str]]:
    """
    Return a list of (org, repo) tuples for which memory files exist.

    Used by the scheduler to iterate all known repos for incremental refresh.
    """
    results: list[tuple[str, str]] = []
    if not MEMORY_ROOT.exists():
        return results
    for org_dir in MEMORY_ROOT.iterdir():
        if not org_dir.is_dir():
            continue
        for repo_file in org_dir.glob("*.json"):
            results.append((org_dir.name, repo_file.stem))
    return results

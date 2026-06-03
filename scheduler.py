"""
FI-PR-GENERATOR — Nightly Scheduler
Runs memory refresh for all configured orgs at 2 AM.
Also supports manual trigger via CLI.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger(__name__)


def refresh_all_orgs():
    """Nightly refresh: update memory for all active orgs."""
    from integrations.github_client import GitHubClient
    from agents.memory_builder import MemoryBuilder
    from memory.org_memory import load_memory, list_all_memories

    gh      = GitHubClient()
    builder = MemoryBuilder()

    log.info("scheduler.refresh_start")

    # Refresh repos that already have memory
    memories = list_all_memories()
    for org, repo in memories:
        try:
            log.info("scheduler.refreshing", org=org, repo=repo)
            activity = gh.get_activity_score(org, repo)

            # Only get PRs from last 24 hours (incremental)
            from datetime import timezone, timedelta
            since = datetime.now(timezone.utc) - timedelta(hours=25)
            all_prs = gh.fetch_recent_prs(org, repo, n=10)
            new_prs = [
                p for p in all_prs
                if p.get("merged_at", "") > since.isoformat()
            ]

            builder.incremental_refresh(
                org=org, repo=repo,
                new_prs=new_prs,
                activity_score=activity,
            )
        except Exception as e:
            log.warning("scheduler.refresh_error", org=org, repo=repo, error=str(e))

    log.info("scheduler.refresh_done", count=len(memories))


def start_scheduler():
    """Start the APScheduler for nightly memory refresh."""
    scheduler = BlockingScheduler()

    # Load refresh hour from config
    config_path = Path("config/orgs.json")
    refresh_hour = 2
    if config_path.exists():
        data = json.loads(config_path.read_text())
        refresh_hour = data.get("global_settings", {}).get("nightly_refresh_hour", 2)

    scheduler.add_job(
        refresh_all_orgs,
        trigger="cron",
        hour=refresh_hour,
        minute=0,
        id="nightly_memory_refresh",
    )

    log.info("scheduler.started", refresh_hour=refresh_hour)
    print(f"Scheduler running — memory refresh at {refresh_hour:02d}:00 daily")
    print("Press Ctrl+C to stop")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("Scheduler stopped")


if __name__ == "__main__":
    start_scheduler()

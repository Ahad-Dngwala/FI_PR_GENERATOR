"""
scheduler.py — APScheduler nightly memory refresh daemon.

Run this as a background service to keep org memories fresh
without manual intervention. The pipeline itself can run on demand.

Usage:
    python scheduler.py                    # start daemon (blocks)
    python scheduler.py --once             # run refresh once and exit
    python scheduler.py --interval-hours 6 # refresh every 6h instead of 24h
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

log = structlog.get_logger(__name__)

scheduler = BlockingScheduler(timezone="UTC")


def _load_enabled_repos() -> list[tuple[str, str]]:
    """
    Return (org, repo) pairs for all enabled repos in config/orgs.json.

    Also includes any repos with existing memory files (for incremental refresh).
    """
    config_path = Path("config/orgs.json")
    repos: list[tuple[str, str]] = []

    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for org_cfg in config.get("orgs", []):
            if org_cfg.get("skip"):
                continue
            for repo_cfg in org_cfg.get("repos", []):
                repo_name = repo_cfg["name"] if isinstance(repo_cfg, dict) else repo_cfg
                if isinstance(repo_cfg, dict) and not repo_cfg.get("enabled", True):
                    continue
                repos.append((org_cfg["name"], repo_name))

    # Also refresh repos with existing memory (discovered from memory_store/)
    from memory.org_memory import list_all_memories

    for org, repo in list_all_memories():
        if (org, repo) not in repos:
            repos.append((org, repo))

    return repos


def run_nightly_refresh() -> None:
    """
    Scheduled job: incrementally refresh all configured org memories.

    Runs at 02:00 UTC daily by default.
    """
    log.info("scheduler.nightly_refresh_start")
    repos = _load_enabled_repos()

    if not repos:
        log.info("scheduler.no_repos_configured")
        return

    log.info("scheduler.refreshing_repos", count=len(repos))

    from agents.memory_builder import refresh_org_memory

    success_count = 0
    fail_count = 0

    for org, repo in repos:
        try:
            log.info("scheduler.refreshing", org=org, repo=repo)
            refresh_org_memory(org, repo)
            success_count += 1
        except Exception as exc:
            log.error("scheduler.refresh_failed", org=org, repo=repo, error=str(exc))
            fail_count += 1

    log.info(
        "scheduler.nightly_refresh_done",
        success=success_count,
        failed=fail_count,
    )


@click.command()
@click.option("--once", is_flag=True, default=False, help="Run refresh once and exit")
@click.option(
    "--interval-hours", default=24, type=int,
    help="Refresh interval in hours (default: 24, runs at 02:00 UTC)",
)
@click.option(
    "--hour", default=2, type=int,
    help="Hour of day to run refresh (UTC, default: 2 = 02:00 UTC)",
)
def main(once: bool, interval_hours: int, hour: int) -> None:
    """Start the FI-PR-GENERATOR memory refresh scheduler."""
    if once:
        click.echo("⚡ Running one-time memory refresh...")
        run_nightly_refresh()
        click.echo("✅ Refresh complete.")
        return

    click.echo(f"🕐 Starting scheduler — memory refresh daily at {hour:02d}:00 UTC")
    click.echo("   Press Ctrl+C to stop.")

    if interval_hours == 24:
        # Exact daily cron at specified hour
        scheduler.add_job(run_nightly_refresh, "cron", hour=hour, minute=0)
    else:
        # Interval mode for testing
        scheduler.add_job(run_nightly_refresh, "interval", hours=interval_hours)
        click.echo(f"   (interval mode: every {interval_hours}h)")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        click.echo("\n⏹️  Scheduler stopped.")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()

"""
main.py — Click CLI entry point for FI-PR-GENERATOR.

Usage:
    python main.py --help
    python main.py build-memory --org GSSoC-ExtD --repo my-repo
    python main.py scan-orgs
    python main.py run --org GSSoC-ExtD --repo my-repo --dry-run
    python main.py run --org GSSoC-ExtD --repo my-repo --issue 42
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import structlog
from dotenv import load_dotenv

# Load .env before anything else touches environment variables
load_dotenv()


def _strip_secrets(logger, method, event_dict: dict) -> dict:
    """Structlog processor — removes known secret patterns from log values."""
    secret_keys = {
        "api_key", "token", "password", "secret", "GITHUB_TOKEN",
        "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY", "NTFY_TOKEN",
    }
    for key in list(event_dict.keys()):
        if any(sk.lower() in key.lower() for sk in secret_keys):
            event_dict[key] = "***REDACTED***"
    return event_dict


# Configure structlog for human-readable output in dev, JSON in prod
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _strip_secrets,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), log_level, 20)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version="0.1.0", prog_name="fi-pr-generator")
def cli() -> None:
    """FI-PR-GENERATOR — Human-supervised multi-agent contribution pipeline."""


# ---------------------------------------------------------------------------
# build-memory
# ---------------------------------------------------------------------------


@cli.command("build-memory")
@click.option("--org", required=True, help="GitHub organisation name")
@click.option("--repo", required=True, help="Repository name within the org")
@click.option("--force", is_flag=True, default=False, help="Force rebuild even if memory exists")
def build_memory(org: str, repo: str, force: bool) -> None:
    """
    Build org memory from the last 50 merged PRs.

    Analyzes commit style, branch naming, file hotspots, maintainer preferences,
    and dynamically discovers the repository's contribution workflow.

    Takes ~1-2 minutes per repository.
    """
    from agents.memory_builder import build_org_memory
    from memory.org_memory import load_org_memory, save_org_memory

    if not force:
        existing = load_org_memory(org, repo)
        if existing:
            click.echo(
                f"Memory already exists for {org}/{repo} "
                f"(last refresh: {existing.last_refresh.strftime('%Y-%m-%d %H:%M UTC')}). "
                f"Use --force to rebuild."
            )
            _print_memory_summary(existing)
            return

    click.echo(f"🧠 Building memory for {org}/{repo}...")
    try:
        memory = build_org_memory(org, repo)
        save_org_memory(memory)
        click.echo(f"✅ Memory built and saved.")
        _print_memory_summary(memory)
    except Exception as exc:
        click.echo(f"❌ Failed to build memory: {exc}", err=True)
        raise SystemExit(1)


def _print_memory_summary(memory) -> None:
    """Print a concise summary of org memory to console."""
    wf = memory.workflow_rules
    click.echo(f"\n📊 Memory Summary for {memory.org_name}/{memory.repo_name}:")
    click.echo(f"   Activity score: {memory.activity.score:.1f}/100")
    click.echo(f"   Confidence: {memory.confidence:.0%}")
    click.echo(f"   Commit style: {memory.conventions.get('commit_style', 'unknown')}")
    click.echo(f"   Test commands: {', '.join(memory.conventions.get('test_commands', []))}")
    click.echo(f"   Accepted issue types: {', '.join(memory.pattern_learning.get('accepted_issue_types', []))}")
    click.echo(f"\n🔍 Contribution Workflow:")
    click.echo(f"   Assignment required: {wf.assignment_required}")
    click.echo(f"   Claim bot: {wf.claim_bot_present} (command: {wf.claim_command})")
    click.echo(f"   Proposal first: {wf.proposal_required}")
    click.echo(f"   Direct PR allowed: {wf.direct_pr_allowed}")
    click.echo(f"   Workflow confidence: {wf.confidence:.0%}")
    if wf.raw_patterns:
        click.echo(f"   Novel patterns discovered: {len(wf.raw_patterns)}")
        for p in wf.raw_patterns[:3]:
            click.echo(f"     • {p.get('pattern_name')}: {p.get('description', '')[:80]}")


# ---------------------------------------------------------------------------
# scan-orgs
# ---------------------------------------------------------------------------


@cli.command("scan-orgs")
@click.option("--org", default=None, help="Limit scan to one org")
@click.option("--repo", default=None, help="Limit scan to one repo")
@click.option(
    "--min-score", default=0, type=float,
    help="Minimum issue score to display (default: 0 = show ALL issues)",
)
def scan_orgs(org: str, repo: str, min_score: float) -> None:
    """
    List and score open issues across configured repos.

    Scans ALL open unassigned issues regardless of labels.
    Label bonus is added to score but issues without labels are still shown.
    No GitHub writes. No coding. Safe to run at any time.
    """
    from agents.scorer import compute_activity_score, compute_issue_score
    from integrations.github_client import get_open_issues, get_repo_activity
    from memory.org_memory import load_org_memory
    from memory.schemas import ActivityScore, OrgMemory

    config_path = Path("config/orgs.json")
    if not config_path.exists():
        click.echo("❌ config/orgs.json not found. Create it from the template.", err=True)
        raise SystemExit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    orgs_cfg = config.get("orgs", [])

    if not orgs_cfg:
        click.echo("ℹ️  No orgs configured in config/orgs.json. Add some repos to scan.")
        return

    for org_cfg in orgs_cfg:
        if org and org_cfg["name"] != org:
            continue
        if org_cfg.get("skip"):
            click.echo(f"⏭️  Skipping {org_cfg['name']} (skip=true)")
            continue

        for repo_cfg in org_cfg.get("repos", []):
            repo_name = repo_cfg["name"] if isinstance(repo_cfg, dict) else repo_cfg
            if repo and repo_name != repo:
                continue
            if isinstance(repo_cfg, dict) and not repo_cfg.get("enabled", True):
                click.echo(f"⏭️  {org_cfg['name']}/{repo_name} (enabled=false)")
                continue

            click.echo(f"\n🔍 Scanning {org_cfg['name']}/{repo_name}...")

            try:
                repo_activity = get_repo_activity(org_cfg["name"], repo_name)
                activity = compute_activity_score(repo_activity)

                if activity.score < 60:
                    click.echo(f"   ⚠️  Activity score {activity.score:.0f}/100 — skipping (< 60)")
                    continue

                click.echo(f"   ✅ Activity: {activity.score:.0f}/100")

                memory = load_org_memory(org_cfg["name"], repo_name)
                if memory is None:
                    from memory.schemas import WorkflowRules
                    from datetime import datetime, timezone
                    memory = OrgMemory(
                        org_name=org_cfg["name"],
                        repo_name=repo_name,
                        last_refresh=datetime.now(tz=timezone.utc),
                        activity=activity,
                    )

                issues = get_open_issues(org_cfg["name"], repo_name)
                if not issues:
                    click.echo("   📭 No open unassigned issues found")
                    continue

                click.echo(f"   Scanning {len(issues)} open issues (all labels, no filter)...")
                scored = [
                    (issue, compute_issue_score(issue, memory, activity))
                    for issue in issues
                ]
                scored.sort(key=lambda x: x[1].score, reverse=True)

                displayed = 0
                for issue, score in scored:
                    if score.score < min_score:
                        continue
                    if score.score >= 75:
                        icon = "[GO] "
                    elif score.score >= 60:
                        icon = "[OK] "
                    else:
                        icon = "[--] "
                    labels_str = ", ".join(issue.get("labels", [])[:4]) or "(no labels)"
                    click.echo(
                        f"   {icon}#{issue['number']:4d} [{score.score:5.1f}] "
                        f"{issue.get('title', '')[:55]}"
                    )
                    click.echo(
                        f"          Labels: {labels_str}"
                        f" | Clarity:{score.clarity:.0f} Scope:{score.scope:.0f}"
                        f" | {issue.get('html_url', '')}"
                    )
                    displayed += 1

                if displayed == 0:
                    click.echo(f"   No issues with score >= {min_score:.0f} found")
                    if min_score > 0:
                        click.echo("   Tip: run with --min-score 0 to see all issues")

            except Exception as exc:
                click.echo(f"   ❌ Error scanning {org_cfg['name']}/{repo_name}: {exc}", err=True)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@cli.command("run")
@click.option("--org", required=True, help="GitHub organisation name")
@click.option("--repo", required=True, help="Repository name")
@click.option("--issue", "issue_number", type=int, default=None, help="Specific issue number (skip scoring)")
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Full pipeline but no GitHub writes and no ntfy approval required",
)
def run(org: str, repo: str, issue_number: int, dry_run: bool) -> None:
    """
    Run the full 10-step pipeline on a single repository.

    In dry-run mode: generates and validates the patch but skips GitHub writes
    and the ntfy approval gate. Use this to test end-to-end locally.
    """
    from orchestrator import run_pipeline

    if dry_run:
        click.echo("🔧 DRY RUN mode — no GitHub writes, no approval required")

    click.echo(f"🚀 Starting pipeline: {org}/{repo}" + (f" issue #{issue_number}" if issue_number else ""))

    try:
        final_state = run_pipeline(
            org=org,
            repo=repo,
            issue_number=issue_number,
            dry_run=dry_run,
        )
        click.echo(f"\n{'✅' if final_state.state == 'completed' else '⚠️ '} Final state: {final_state.state.upper()}")
        if final_state.failure_reason:
            click.echo(f"   Reason: {final_state.failure_reason}")
        if final_state.issue_number:
            click.echo(f"   Issue: #{final_state.issue_number}")
        if final_state.branch:
            click.echo(f"   Branch: {final_state.branch}")
        if final_state.model_used:
            click.echo(f"   Model used: {final_state.model_used}")
        if final_state.risk_score:
            click.echo(f"   Risk: {final_state.risk_score.level} ({final_state.risk_score.score:.0f}/100)")
    except KeyboardInterrupt:
        click.echo("\n⏸️  Pipeline interrupted. State saved — can be resumed.")
    except Exception as exc:
        click.echo(f"\n❌ Pipeline crashed: {exc}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# list-states
# ---------------------------------------------------------------------------


@cli.command("list-states")
@click.option("--status", default=None, help="Filter by state (e.g. blocked, waiting_approval)")
def list_states(status: str) -> None:
    """
    List recent pipeline run states from the state/ directory.
    """
    state_dir = Path("state")
    if not state_dir.exists():
        click.echo("No state directory found — no runs yet.")
        return

    files = sorted(state_dir.glob("*.json"), reverse=True)
    if not files:
        click.echo("No run states found.")
        return

    from memory.schemas import RunState

    for f in files[:20]:
        if f.name.endswith("_approval.json"):
            continue
        try:
            data = json.loads(f.read_text())
            state = RunState.model_validate(data)
            if status and state.state != status:
                continue
            icon = {"completed": "✅", "failed": "❌", "blocked": "⏸️", "waiting_approval": "📱"}.get(state.state, "⚙️")
            click.echo(
                f"{icon} {state.run_id} | {state.org}/{state.repo} "
                f"| issue #{state.issue_number} | {state.state}"
                + (f" | {state.failure_reason[:50]}" if state.failure_reason else "")
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# test-notification
# ---------------------------------------------------------------------------


@cli.command("test-notification")
def test_notification() -> None:
    """
    Send a test notification to your phone and start the local server.
    Use this to verify that the mobile approval/rejection button tap
    successfully routes back to your PC via ngrok/tunnels.
    """
    from integrations.ntfy_notifier import ApprovalRequest, send_and_wait
    import uuid

    run_id = f"test-{uuid.uuid4().hex[:8]}"
    req = ApprovalRequest(
        run_id=run_id,
        org="test-org",
        repo="test-repo",
        issue_number=999,
        issue_title="Verify Mobile Approval Flow",
        branch="fix/test-mobile-approval",
        files_changed=["src/test_file.py"],
        diff_summary="  src/test_file.py (+10/-2)",
        test_result="PASS",
        risk_level="low",
        risk_score=15.0,
        reviewer_notes=["Tap Approve or Reject on your phone. If ngrok is configured correctly, this CLI will receive it!"],
        model_used="gemini/gemini-2.5-pro",
    )

    click.echo("📱 Sending test notification to phone...")
    click.echo(f"   Topic: {os.environ.get('NTFY_TOPIC')}")
    click.echo(f"   Approval Server URL: {os.environ.get('APPROVAL_SERVER_URL')}")
    click.echo("   Starting local Flask server on port 8080 and waiting for your response on phone...")
    
    result = send_and_wait(req)
    if result is True:
        click.echo("\n✅ Received APPROVAL from your phone!")
    elif result is False:
        click.echo("\n❌ Received REJECTION from your phone!")
    else:
        click.echo("\n⏳ Timed out waiting for response.")


# ---------------------------------------------------------------------------
# listen (command bot)
# ---------------------------------------------------------------------------


@cli.command("listen")
@click.option(
    "--live", is_flag=True, default=False,
    help="Allow --live suffix in commands to trigger non-dry-run pipeline runs",
)
def listen(live: bool) -> None:
    """
    Start the ntfy command bot — listen for pipeline commands from your phone.

    Subscribes to NTFY_COMMAND_TOPIC and waits for messages in the format:
      "org/repo"          → runs dry-run pipeline
      "org/repo --live"   → runs live pipeline (only if --live flag is set)

    Rate limited to 3 runs/hour with 20-minute cooldown between runs.
    Only orgs in config/orgs.json whitelist are accepted.

    Requires NTFY_COMMAND_TOPIC to be set in .env.
    """
    from integrations.command_listener import listen_for_commands

    mode = "LIVE + DRY-RUN" if live else "DRY-RUN ONLY"
    click.echo(f"📱 Starting command listener [{mode}]")
    click.echo(f"   Command topic: {os.environ.get('NTFY_COMMAND_TOPIC', '(not set)')}")
    click.echo(f"   Approval topic: {os.environ.get('NTFY_TOPIC', '(not set)')}")
    click.echo("   Send 'org/repo' from ntfy app to trigger a pipeline run.")
    click.echo("   Press Ctrl+C to stop.\n")

    try:
        listen_for_commands(allow_live=live)
    except EnvironmentError as exc:
        click.echo(f"\n❌ {exc}", err=True)
        raise SystemExit(1)
    except KeyboardInterrupt:
        click.echo("\n⏹️  Command listener stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()

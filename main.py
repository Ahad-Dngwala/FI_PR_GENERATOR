"""
FI-PR-GENERATOR — CLI Entry Point
Usage:
  python main.py --org GSSoC-ExtD --repo my-repo
  python main.py --org GSSoC-ExtD --repo my-repo --issue 42
  python main.py --org GSSoC-ExtD --repo my-repo --dry-run
  python main.py --build-memory --org GSSoC-ExtD --repo my-repo
  python main.py --scan-orgs
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

load_dotenv()
console = Console()


def cmd_run(args):
    """Run the full pipeline for a single repo."""
    from orchestrator import run
    task = run(
        org          = args.org,
        repo         = args.repo,
        issue_number = args.issue,
        dry_run      = args.dry_run,
    )
    console.print(f"\n[bold]Final state: {task.state.value}[/bold]")
    if task.blocker:
        console.print(f"[red]Blocker: {task.blocker}[/red]")
    return 0 if task.state.value == "completed" else 1


def cmd_build_memory(args):
    """Build or refresh org memory for a specific repo."""
    from integrations.github_client import GitHubClient
    from agents.memory_builder import MemoryBuilder

    console.print(f"[cyan]Building memory for {args.org}/{args.repo}...[/cyan]")
    gh      = GitHubClient()
    builder = MemoryBuilder()

    activity = gh.get_activity_score(args.org, args.repo)
    prs      = gh.fetch_recent_prs(args.org, args.repo, n=30)
    repo_obj = gh.get_repo(args.org, args.repo)

    memory = builder.build_from_prs(
        org            = args.org,
        repo           = args.repo,
        prs            = prs,
        default_branch = repo_obj.default_branch,
        activity_score = activity,
    )
    console.print(f"[green]✅ Memory built: v{memory.version}[/green]")
    console.print(f"  Commit style: {memory.commit_style}")
    console.print(f"  Test commands: {memory.common_test_commands}")
    console.print(f"  Hotspots: {memory.common_file_hotspots[:5]}")


def cmd_scan_orgs(args):
    """Scan all configured orgs and score issues without coding."""
    config_path = Path("config/orgs.json")
    if not config_path.exists():
        console.print("[red]config/orgs.json not found[/red]")
        return 1

    from integrations.github_client import GitHubClient
    from agents.scorer import IssueScorer
    from memory.org_memory import load_memory

    gh     = GitHubClient()
    scorer = IssueScorer()
    data   = json.loads(config_path.read_text())

    target_orgs = data.get("target_orgs", [])
    if args.org and not any(o["name"].lower() == args.org.lower() for o in target_orgs):
        target_orgs.append({
            "name": args.org,
            "enabled": True,
            "priority": 1,
            "preferred_labels": ["good first issue", "bug", "enhancement", "documentation"],
            "require_assignment": False
        })

    for org_cfg in target_orgs:
        if not org_cfg.get("enabled", True):
            continue
        org = org_cfg["name"]
        if args.org and org.lower() != args.org.lower():
            continue

        console.print(Panel(f"[bold]Scanning: {org}[/bold]", style="blue"))

        # Fetch repos — try org first, fall back to user account
        try:
            try:
                gh_entity = gh._gh.get_organization(org)
                console.print(f"  [dim]Type: Organization[/dim]")
            except Exception:
                gh_entity = gh._gh.get_user(org)
                console.print(f"  [dim]Type: User account[/dim]")
            
            if args.repo:
                repos = [gh_entity.get_repo(args.repo)]
            else:
                repos = list(gh_entity.get_repos(type="public", sort="updated"))[:15]
        except Exception as e:
            console.print(f"[red]Failed to fetch repos: {e}[/red]")
            continue

        for repo in repos:
            activity = gh.get_activity_score(org, repo.name)
            if activity < data["global_settings"]["min_repo_activity_score"]:
                continue
            memory = load_memory(org, repo.name)
            issues = gh.fetch_open_issues(org, repo.name, max_issues=20)

            if not issues:
                continue

            issue_dicts = []
            from datetime import datetime, timezone
            for iss in issues:
                age = (datetime.now(timezone.utc) -
                       iss.created_at.replace(tzinfo=timezone.utc)).days
                issue_dicts.append({
                    "number": iss.number, "id": iss.id,
                    "title": iss.title, "body": iss.body or "",
                    "labels": [lb.name for lb in iss.labels],
                    "age_days": age, "comment_count": iss.comments,
                })

            scores = scorer.score_multiple(
                issue_dicts, org=org, repo=repo.name,
                activity_score=activity, memory=memory,
            )
            top = [s for s in scores if s.overall_score >= 60][:3]
            if top:
                console.print(f"  [green]{repo.name}:[/green]")
                for s in top:
                    console.print(
                        f"    #{s.issue_number} [{s.overall_score:.0f}] {s.title[:50]}"
                    )


def cmd_show_memory(args):
    """Show stored memory for a repo."""
    from memory.org_memory import load_memory, get_memory_summary
    memory = load_memory(args.org, args.repo)
    if not memory:
        console.print(f"[yellow]No memory found for {args.org}/{args.repo}[/yellow]")
        return
    console.print(Panel(get_memory_summary(memory), title=f"Memory: {args.org}/{args.repo}"))


def main():
    parser = argparse.ArgumentParser(
        description="FI-PR-GENERATOR — Autonomous Open Source Contribution Agent"
    )
    subparsers = parser.add_subparsers(dest="command")

    # run command
    run_p = subparsers.add_parser("run", help="Run full pipeline")
    run_p.add_argument("--org",   required=True, help="GitHub org name")
    run_p.add_argument("--repo",  required=True, help="Repository name")
    run_p.add_argument("--issue", type=int,      help="Specific issue number")
    run_p.add_argument("--dry-run", action="store_true", help="No writes to GitHub")

    # build-memory command
    mem_p = subparsers.add_parser("build-memory", help="Build/refresh org memory")
    mem_p.add_argument("--org",  required=True)
    mem_p.add_argument("--repo", required=True)

    # scan-orgs command
    scan_p = subparsers.add_parser("scan-orgs", help="Scan all orgs and score issues")
    scan_p.add_argument("--org",  help="GitHub org or username to scan")
    scan_p.add_argument("--repo", help="Specific repository name to scan")

    # show-memory command
    show_p = subparsers.add_parser("show-memory", help="Show stored org memory")
    show_p.add_argument("--org",  required=True)
    show_p.add_argument("--repo", required=True)

    # Legacy flat flags support (backward compat)
    parser.add_argument("--org",          help="GitHub org name")
    parser.add_argument("--repo",         help="Repository name")
    parser.add_argument("--issue",        type=int, help="Issue number")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--build-memory", action="store_true")
    parser.add_argument("--scan-orgs",    action="store_true")
    parser.add_argument("--show-memory",  action="store_true")

    args = parser.parse_args()

    console.print(Panel(
        "[bold blue]FI-PR-GENERATOR[/bold blue]\n"
        "Autonomous Human-in-the-Loop Open Source Contribution Platform",
        style="blue",
    ))

    # Route commands
    is_run = args.command == "run" or (
        args.command is None
        and args.org
        and args.repo
        and not args.build_memory
        and not args.scan_orgs
        and not args.show_memory
    )
    if is_run:
        sys.exit(cmd_run(args) or 0)

    elif args.command == "build-memory" or args.build_memory:
        cmd_build_memory(args)

    elif args.command == "scan-orgs" or args.scan_orgs:
        cmd_scan_orgs(args)

    elif args.command == "show-memory" or args.show_memory:
        cmd_show_memory(args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

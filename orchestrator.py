"""
FI-PR-GENERATOR — Main Pipeline Orchestrator
Sequential Python pipeline (no LangGraph for MVP — debugging simplicity).
One file, explicit state transitions, recoverable at each step.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

# Load environment first
load_dotenv()

from agents.coder import CoderAgent
from agents.memory_builder import MemoryBuilder
from agents.reviewer import ReviewerAgent
from agents.scorer import IssueScorer
from integrations.aider_runner import (
    build_repo_map,
    extract_keywords_from_issue,
    get_relevant_files,
    verify_files_contain_keywords,
)
from integrations.git_ops import GitOps
from integrations.github_client import GitHubClient
from integrations.telegram_bot import (
    ApprovalMessage,
    TelegramApprovalBot,
    compute_risk_score,
)
from integrations.test_runner import (
    detect_test_command,
    run_tests,
    should_continue_despite_failure,
    should_retry,
)
from memory.org_memory import (
    get_memory_summary,
    is_fresh,
    load_memory,
    update_from_rejection,
)
from memory.schemas import (
    ApprovalDecision,
    FailureClass,
    IssueDecision,
    OrgMemory,
    PipelineState,
    TaskState,
)

log     = console = None   # initialized in run()
console = Console()

DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"
MIN_SCORE      = int(os.environ.get("MIN_ISSUE_SCORE", "60"))
MAX_RETRIES    = 2


# ─────────────────────────────────────────────────────────────
# Setup structured logging
# ─────────────────────────────────────────────────────────────

def _setup_logging():
    import structlog
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    return structlog.get_logger("pipeline")


# ─────────────────────────────────────────────────────────────
# Pipeline steps
# ─────────────────────────────────────────────────────────────

def step_check_activity(
    gh: GitHubClient,
    org: str,
    repo: str,
    min_score: float = 60.0,
) -> tuple[bool, float]:
    """
    STEP 1 — Activity Gate
    Check if repo is worth working on today.
    """
    console.print(f"  [cyan]⚡ Checking activity: {org}/{repo}[/cyan]")
    score = gh.get_activity_score(org, repo)
    passes = score >= min_score
    if not passes:
        console.print(
            f"  [yellow]⚠️ Activity score {score:.0f}/100 < {min_score} — skipping[/yellow]"
        )
    else:
        console.print(f"  [green]✅ Activity score: {score:.0f}/100[/green]")
    return passes, score


def step_load_memory(
    builder: MemoryBuilder,
    gh: GitHubClient,
    org: str,
    repo: str,
    activity_score: float,
) -> OrgMemory:
    """
    STEP 2 — Memory Gate
    Load org memory. Build/refresh if stale.
    """
    console.print(f"  [cyan]🧠 Loading org memory: {org}/{repo}[/cyan]")
    memory = load_memory(org, repo)

    if not memory or not is_fresh(memory, max_age_hours=48):
        console.print("  [yellow]Memory stale or missing — refreshing...[/yellow]")
        prs = gh.fetch_recent_prs(org, repo, n=20)
        default_branch = gh.get_repo(org, repo).default_branch
        memory = builder.build_from_prs(
            org=org, repo=repo, prs=prs,
            default_branch=default_branch,
            activity_score=activity_score,
        )
        console.print(f"  [green]✅ Memory built from {len(prs)} PRs[/green]")
    else:
        console.print(f"  [green]✅ Memory loaded (v{memory.version})[/green]")

    return memory


def step_score_issues(
    scorer: IssueScorer,
    gh: GitHubClient,
    org: str,
    repo: str,
    activity_score: float,
    memory: OrgMemory,
    preferred_labels: list[str],
    issue_number: Optional[int] = None,
) -> list:
    """
    STEP 3 — Issue Scoring
    Fetch and score open issues. Return sorted list.
    """
    console.print(f"  [cyan]🎯 Fetching and scoring issues...[/cyan]")
    if issue_number:
        try:
            r = gh.get_repo(org, repo)
            iss = gh._api_call(r.get_issue, issue_number)
            raw_issues = [iss]
            console.print(f"  Fetched specific issue #{issue_number}")
        except Exception as e:
            console.print(f"  [red]❌ Failed to fetch issue #{issue_number}: {e}[/red]")
            raw_issues = []
    else:
        raw_issues = gh.fetch_open_issues(org, repo)
        console.print(f"  Found {len(raw_issues)} open unassigned issues")

    # Convert to dict format for scorer
    issue_dicts = []
    for iss in raw_issues:
        age_days = (datetime.now(timezone.utc) -
                    iss.created_at.replace(tzinfo=timezone.utc)).days
        issue_dicts.append({
            "number":        iss.number,
            "id":            iss.id,
            "title":         iss.title,
            "body":          iss.body or "",
            "labels":        [lb.name for lb in iss.labels],
            "age_days":      age_days,
            "comment_count": iss.comments,
        })

    scores = scorer.score_multiple(
        issue_dicts, org=org, repo=repo,
        activity_score=activity_score, memory=memory,
    )

    # Show scoring table
    table = Table(title=f"Issue Scores — {org}/{repo}", show_lines=False)
    table.add_column("#",      style="dim",    width=6)
    table.add_column("Score",  style="bold",   width=7)
    table.add_column("Decision",               width=14)
    table.add_column("Title",                  max_width=50)
    for s in scores[:10]:
        color = "green" if s.overall_score >= 75 else \
                "yellow" if s.overall_score >= 60 else "red"
        table.add_row(
            str(s.issue_number),
            f"[{color}]{s.overall_score:.0f}[/{color}]",
            s.decision.value,
            s.title[:50],
        )
    console.print(table)

    return scores


def step_select_issue(scores: list, gh: GitHubClient, org: str, repo: str):
    """
    STEP 4 — Issue Selection
    Pick the highest-scoring eligible issue.
    Re-confirms assignment live before returning.
    """
    for score in scores:
        if score.decision == IssueDecision.REJECT:
            continue
        if score.overall_score < MIN_SCORE:
            continue

        # Live assignment re-check
        if not gh.check_assignment(org, repo, score.issue_number):
            console.print(
                f"  [yellow]#{score.issue_number} is now assigned — skipping[/yellow]"
            )
            continue

        console.print(
            f"  [green]✅ Selected issue #{score.issue_number}: {score.title[:60]}[/green]"
        )
        return score

    return None


def step_retrieve_context(
    local_path: Path,
    issue_title: str,
    issue_body: str,
    scorer_files: list[str],
    max_files: int = 5,
) -> tuple[list[str], float]:
    """
    STEP 5 — Context Retrieval
    Build repo map, extract keywords, rank files, verify with grep.
    """
    console.print("  [cyan]📂 Building repo map and retrieving context...[/cyan]")

    keywords = extract_keywords_from_issue(issue_title, issue_body)
    repo_map = build_repo_map(local_path)
    candidate_files = get_relevant_files(repo_map, keywords, max_files=max_files * 2)

    # Merge with scorer's file hints
    for f in (scorer_files or []):
        if f not in candidate_files:
            candidate_files.insert(0, f)

    # Verification layer — grep check
    if candidate_files:
        verified = verify_files_contain_keywords(
            local_path, candidate_files[:max_files], keywords
        )
        approved_files  = [f for f, ok in verified.items() if ok]
        retrieval_conf  = len(approved_files) / max(len(candidate_files[:max_files]), 1)
    else:
        approved_files  = []
        retrieval_conf  = 0.0

    if not approved_files:
        # Fall back to all candidate files — let human decide
        approved_files = candidate_files[:max_files]
        retrieval_conf = 0.3

    console.print(
        f"  [green]✅ Context: {len(approved_files)} files "
        f"(confidence {retrieval_conf:.0%})[/green]"
    )
    for f in approved_files:
        console.print(f"    • {f}")

    return approved_files, retrieval_conf


def step_generate_patch(
    coder: CoderAgent,
    local_path: Path,
    issue_title: str,
    issue_body: str,
    issue_number: int,
    approved_files: list[str],
    memory: OrgMemory,
    git_ops: GitOps,
    prior_error: str = "",
) -> tuple[bool, str, str]:
    """STEP 6 — Code Generation"""
    console.print(f"  [cyan]⚙️ Generating patch (attempt)...[/cyan]")
    success, model_used, failure_reason = coder.generate_patch(
        local_path     = local_path,
        issue_title    = issue_title,
        issue_body     = issue_body,
        issue_number   = issue_number,
        approved_files = approved_files,
        memory         = memory,
        prior_error    = prior_error,
        git_ops        = git_ops,
    )
    if success:
        diff_lines = git_ops.get_diff_line_count()
        console.print(
            f"  [green]✅ Patch generated via {model_used} "
            f"({diff_lines} changed lines)[/green]"
        )
    else:
        console.print(f"  [red]❌ Coding failed: {failure_reason}[/red]")
    return success, model_used, failure_reason


def step_review(
    reviewer: ReviewerAgent,
    git_ops: GitOps,
    issue_title: str,
    issue_body: str,
    approved_files: list[str],
) -> tuple[bool, float, list[str]]:
    """STEP 7 — Independent Review"""
    console.print("  [cyan]🔍 Running independent review (Qwen)...[/cyan]")
    diff          = git_ops.get_diff()
    changed_files = git_ops.get_changed_files()

    passed, score, issues, summary = reviewer.review(
        diff           = diff,
        issue_title    = issue_title,
        issue_body     = issue_body,
        approved_files = approved_files,
        changed_files  = changed_files,
    )

    color = "green" if passed else "red"
    console.print(f"  [{color}]Review: {summary} ({score:.0f}/100)[/{color}]")
    for iss in issues[:5]:
        console.print(f"    [dim]• {iss}[/dim]")

    return passed, score, issues


def step_run_tests(
    git_ops: GitOps,
    local_path: Path,
    branch: str,
    repo: str,
    retry_count: int = 0,
):
    """STEP 8 — Test Runner"""
    console.print("  [cyan]🧪 Running tests...[/cyan]")
    cmd = detect_test_command(local_path)
    result = run_tests(
        local_path  = local_path,
        command     = cmd,
        retry_count = retry_count,
        branch      = branch,
        repo        = repo,
    )
    if result.result == "pass":
        console.print("  [green]✅ Tests passed[/green]")
    elif result.result == "skip":
        console.print(f"  [yellow]⚠️ Tests skipped: {result.environment_notes}[/yellow]")
    else:
        console.print(
            f"  [red]❌ Tests failed ({result.failure_class.value})[/red]"
        )
        console.print(f"    {result.stderr_summary[:200]}")
    return result


async def step_request_approval(
    bot: TelegramApprovalBot,
    git_ops: GitOps,
    issue_score,
    issue_url: str,
    branch: str,
    validation_result,
    retrieval_confidence: float,
    org: str,
    repo: str,
) -> tuple[ApprovalDecision, str]:
    """STEP 9 — Human Approval via Telegram"""
    console.print("  [cyan]📱 Sending Telegram approval request...[/cyan]")

    diff          = git_ops.get_diff()
    changed_files = git_ops.get_changed_files()
    diff_lines    = git_ops.get_diff_line_count()

    risk_score = compute_risk_score(
        diff_line_count      = diff_lines,
        changed_files        = changed_files,
        test_result          = validation_result.result,
        retrieval_confidence = retrieval_confidence,
    )

    msg = ApprovalMessage(
        title          = f"#{issue_score.issue_number}: {issue_score.title[:60]}",
        status         = validation_result.result,
        changed_files  = changed_files,
        diff_preview   = diff[:2000],
        test_summary   = (f"{validation_result.result.upper()} — "
                         f"{validation_result.stdout_summary[:150]}"),
        risk_score     = risk_score,
        risk_level     = "low" if risk_score < 30 else "medium" if risk_score < 60 else "high",
        issue_link     = issue_url,
        branch         = branch,
    )

    decision, reason = await bot.request_approval(msg)
    console.print(f"  [bold]Decision: {decision.value}[/bold]")
    return decision, reason


def step_push_and_pr(
    git_ops: GitOps,
    gh: GitHubClient,
    org: str,
    repo: str,
    branch: str,
    issue_score,
    issue_url: str,
    validation_result,
    model_used: str,
) -> str:
    """STEP 10 — Rebase, Push, Draft PR"""
    console.print("  [cyan]🚀 Rebasing, pushing, creating draft PR...[/cyan]")

    # Re-check assignment before push
    if not gh.check_assignment(org, repo, issue_score.issue_number):
        console.print(
            "[red]❌ Issue reassigned while waiting for approval! Aborting.[/red]"
        )
        return ""

    # Rebase onto upstream
    if git_ops.has_upstream_changes():
        console.print("  [yellow]Upstream moved — rebasing...[/yellow]")
        if not git_ops.fetch_and_rebase():
            console.print(
                "[red]❌ Rebase conflict — aborting push. State preserved locally.[/red]"
            )
            return ""

    # Push
    if not git_ops.push_branch(branch, dry_run=DRY_RUN):
        console.print("[red]❌ Push failed[/red]")
        return ""

    # Build PR body
    test_badge = ("✅ Tests passed locally" if validation_result.result == "pass"
                  else f"⚠️ Tests: {validation_result.failure_class.value} "
                       f"({validation_result.environment_notes or 'see diff'})")

    pr_body = f"""Fixes #{issue_score.issue_number}

## Summary
{issue_score.title}

## Changes
This PR makes a minimal targeted fix for the reported issue.

## Testing
{test_badge}

## Notes
- Generated with AI assistance (FI-PR-GENERATOR) — reviewed and approved by human
- Model used: {model_used}
- Diff: {git_ops.get_diff_line_count()} lines changed
- Intentionally minimal scope — only the reported issue is addressed

🔗 Issue: {issue_url}
"""

    pr_url = gh.create_draft_pr(
        org          = org,
        repo         = repo,
        branch       = branch,
        title        = f"fix: {issue_score.title[:60]}",
        body         = pr_body,
        issue_number = issue_score.issue_number,
        dry_run      = DRY_RUN,
    )
    console.print(f"  [bold green]🎉 Draft PR created: {pr_url}[/bold green]")
    return pr_url


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

async def run_pipeline(
    org: str,
    repo: str,
    issue_number: Optional[int] = None,
    dry_run: bool = False,
) -> TaskState:
    """
    Run the full pipeline for a single org/repo.
    Returns the final TaskState (can be inspected for success/failure).
    """
    global DRY_RUN
    if dry_run:
        DRY_RUN = True
        os.environ["DRY_RUN"] = "true"

    task = TaskState(
        task_id = str(uuid.uuid4())[:8],
        org     = org,
        repo    = repo,
    )

    log = _setup_logging()

    console.print(Panel(
        f"[bold]FI-PR-GENERATOR[/bold] — {org}/{repo}\n"
        f"Task ID: {task.task_id} | Dry Run: {DRY_RUN}",
        style="blue",
    ))

    # Initialize agents
    gh       = GitHubClient()
    builder  = MemoryBuilder()
    scorer   = IssueScorer()
    coder    = CoderAgent()
    reviewer = ReviewerAgent()

    # Auto-detect notification channel: Ntfy → Telegram → None
    bot = None
    if os.environ.get("NTFY_TOPIC"):
        try:
            from integrations.ntfy_bot import NtfyApprovalBot
            bot = NtfyApprovalBot()
            console.print("[green]📱 Using ntfy.sh for approvals[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ ntfy setup error: {e}[/yellow]")
    elif os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        try:
            bot = TelegramApprovalBot()
            console.print("[green]📱 Using Telegram for approvals[/green]")
        except EnvironmentError as e:
            console.print(f"[yellow]⚠️ Telegram not configured: {e}[/yellow]")
    else:
        console.print("[yellow]⚠️ No notification channel — will auto-approve (add NTFY_TOPIC)[/yellow]")

    git_ops = GitOps(github_token=os.environ.get("GITHUB_TOKEN", ""))

    # Load org config
    config_path = Path("config/orgs.json")
    org_config  = {}
    if config_path.exists():
        data = json.loads(config_path.read_text())
        for o in data.get("target_orgs", []):
            if o["name"] == org:
                org_config = o
                break

    preferred_labels  = org_config.get("preferred_labels", [])
    require_assignment= org_config.get("require_assignment", True)
    min_activity      = data.get("global_settings", {}).get("min_repo_activity_score", 60)

    try:
        # ── STEP 1: Activity Gate ──────────────────────────────
        task.advance(PipelineState.CHECKING)
        passes, activity_score = step_check_activity(gh, org, repo, min_activity)
        if not passes:
            task.fail(f"Activity score too low ({activity_score:.0f})")
            return task

        # ── STEP 2: Memory Gate ────────────────────────────────
        memory = step_load_memory(builder, gh, org, repo, activity_score)

        # ── STEP 3: Issue Scoring ──────────────────────────────
        task.advance(PipelineState.SELECTING)
        scores = step_score_issues(
            scorer, gh, org, repo, activity_score, memory, preferred_labels,
            issue_number=issue_number
        )

        # If specific issue requested, find it
        if issue_number:
            selected = next(
                (s for s in scores if s.issue_number == issue_number), None
            )
            if not selected:
                task.fail(f"Issue #{issue_number} not found or ineligible")
                return task
        else:
            selected = step_select_issue(scores, gh, org, repo)

        if not selected:
            task.fail("No eligible issues found in this repo today")
            return task

        task.issue_number = selected.issue_number
        task.issue_title  = selected.title
        task.issue_url    = (
            f"https://github.com/{org}/{repo}/issues/{selected.issue_number}"
        )
        task.issue_score  = selected

        # ── Fetch full issue body ──────────────────────────────
        raw_issues = gh.fetch_open_issues(org, repo, max_issues=200)
        issue_obj  = next(
            (i for i in raw_issues if i.number == selected.issue_number), None
        )
        issue_body = issue_obj.body if issue_obj else selected.title

        # ── STEP 4: Clone repo ─────────────────────────────────
        console.print(f"\n[bold]📥 Cloning {org}/{repo}...[/bold]")
        local_path = git_ops.clone(org, repo)

        # Create feature branch
        branch_name = f"fix/issue-{selected.issue_number}"
        git_ops.create_branch(branch_name)
        task.branch_name  = branch_name
        task.base_commit  = git_ops.current_commit()

        # ── STEP 5: Context Retrieval ──────────────────────────
        task.advance(PipelineState.RETRIEVING)
        approved_files, retrieval_conf = step_retrieve_context(
            local_path   = local_path,
            issue_title  = selected.title,
            issue_body   = issue_body,
            scorer_files = selected.recommended_files,
        )
        task.retrieved_files       = approved_files
        task.retrieval_confidence  = retrieval_conf

        if not approved_files:
            task.fail("No relevant files found — context retrieval failed")
            return task

        # ── Detect test command early ──────────────────────────
        test_cmd = detect_test_command(local_path)

        # ── STEPS 6–8: Code → Review → Test loop ──────────────
        prior_error   = ""
        final_success = False

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                console.print(f"\n[yellow]🔄 Retry attempt {attempt}/{MAX_RETRIES}[/yellow]")
                task.retry_count = attempt

            # STEP 6: Generate patch
            task.advance(PipelineState.CODING)
            success, model_used, fail_reason = step_generate_patch(
                coder          = coder,
                local_path     = local_path,
                issue_title    = selected.title,
                issue_body     = issue_body,
                issue_number   = selected.issue_number,
                approved_files = approved_files,
                memory         = memory,
                git_ops        = git_ops,
                prior_error    = prior_error,
            )
            task.model_used = model_used

            if not success:
                prior_error = fail_reason
                if attempt >= MAX_RETRIES:
                    task.fail(f"Coding failed after {MAX_RETRIES + 1} attempts: {fail_reason}")
                    return task
                continue

            task.diff_content    = git_ops.get_diff()
            task.diff_line_count = git_ops.get_diff_line_count()

            # STEP 7: Review
            task.advance(PipelineState.REVIEWING)
            review_passed, review_score, review_issues = step_review(
                reviewer       = reviewer,
                git_ops        = git_ops,
                issue_title    = selected.title,
                issue_body     = issue_body,
                approved_files = approved_files,
            )

            if not review_passed and attempt < MAX_RETRIES:
                prior_error = "\n".join(review_issues[:3])
                console.print(
                    "[yellow]Review failed — routing back to coder[/yellow]"
                )
                # Reset changes and retry
                try:
                    git_ops.repo.git.checkout("--", ".")
                except Exception:
                    pass
                continue

            # STEP 8: Tests
            task.advance(PipelineState.TESTING)
            val_result = step_run_tests(
                git_ops     = git_ops,
                local_path  = local_path,
                branch      = branch_name,
                repo        = repo,
                retry_count = attempt,
            )
            task.validation_result = val_result

            if val_result.result == "fail":
                if should_retry(val_result, MAX_RETRIES) and attempt < MAX_RETRIES:
                    prior_error = val_result.stderr_summary
                    try:
                        git_ops.repo.git.checkout("--", ".")
                    except Exception:
                        pass
                    continue
                elif not should_continue_despite_failure(val_result):
                    task.fail(
                        f"Tests failed ({val_result.failure_class.value}) — "
                        f"not safe to continue"
                    )
                    return task

            final_success = True
            break

        if not final_success:
            task.fail("Pipeline exhausted all retries")
            return task

        # ── Commit the changes ─────────────────────────────────
        commit_msg = (
            f"fix: {selected.title[:60].lower()}"
            if not memory.commit_style
            else f"fix: {selected.title[:60].lower()}"
        )
        git_ops.stage_and_commit(commit_msg)

        # ── STEP 9: Human Approval ─────────────────────────────
        task.advance(PipelineState.WAITING_APPROVAL)

        if bot:
            decision, rejection_reason = await step_request_approval(
                bot                  = bot,
                git_ops              = git_ops,
                issue_score          = selected,
                issue_url            = task.issue_url,
                branch               = branch_name,
                validation_result    = val_result,
                retrieval_confidence = retrieval_conf,
                org                  = org,
                repo                 = repo,
            )
            task.approval_decision = decision
        else:
            console.print("[yellow]⚠️ No Telegram — auto-approving (Telegram not configured)[/yellow]")
            decision           = ApprovalDecision.APPROVED
            rejection_reason   = ""

        # Handle approval decision
        if decision == ApprovalDecision.REJECTED:
            # Log rejection to memory
            updated_memory = update_from_rejection(
                memory          = memory,
                reason          = rejection_reason or "human_flagged_low_quality",
                issue_type      = (selected.labels[0] if selected.labels else "unknown"),
                files_changed   = git_ops.get_changed_files(),
                diff_size       = task.diff_line_count,
            )
            from memory.org_memory import save_memory
            save_memory(updated_memory)
            task.fail("Rejected by human")
            return task

        if decision == ApprovalDecision.TIMEOUT:
            task.fail("Approval timed out — state preserved locally")
            return task

        if decision == ApprovalDecision.REVISE:
            task.fail("Human requested revision — replan required")
            return task

        # ── STEP 10: Push + Draft PR ───────────────────────────
        task.advance(PipelineState.PUSHING)
        pr_url = step_push_and_pr(
            git_ops          = git_ops,
            gh               = gh,
            org              = org,
            repo             = repo,
            branch           = branch_name,
            issue_score      = selected,
            issue_url        = task.issue_url,
            validation_result= val_result,
            model_used       = model_used,
        )

        if pr_url:
            task.pr_url = pr_url
            task.advance(PipelineState.COMPLETED)
            task.completed_at = datetime.utcnow().isoformat()
            console.print(Panel(
                f"[bold green]🎉 SUCCESS[/bold green]\n"
                f"PR: {pr_url}\n"
                f"Issue: {task.issue_url}\n"
                f"Branch: {branch_name}\n"
                f"Diff: {task.diff_line_count} lines",
                style="green",
            ))
        else:
            task.fail("Push or PR creation failed")

    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user — state preserved[/yellow]")
        task.fail("User interrupted")

    except Exception as e:
        log.exception("pipeline.unexpected_error", error=str(e))
        console.print(f"[red]Unexpected error: {e}[/red]")
        task.fail(str(e))

    finally:
        git_ops.cleanup()

    return task


def run(org: str, repo: str,
        issue_number: Optional[int] = None,
        dry_run: bool = False) -> TaskState:
    """Synchronous wrapper for CLI usage."""
    return asyncio.run(run_pipeline(org, repo, issue_number, dry_run))

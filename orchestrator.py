"""
orchestrator.py — The 10-step FI-PR-GENERATOR pipeline.

Pure Python. No LangGraph. No async. No magic.
Every step updates state to disk before proceeding.
Exceptions are caught per-step and converted to FAILED/BLOCKED states.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from memory.schemas import RejectionEntry, RunState, WorkflowRules

log = structlog.get_logger(__name__)

STATE_DIR = Path("state")
DIFFS_DIR = Path("diffs")
MAX_RETRIES = 2
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _new_run_id() -> str:
    now = datetime.now(tz=timezone.utc)
    return f"run_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _save_state(state: RunState) -> None:
    """Persist run state to disk after every transition."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DIFFS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{state.run_id}.json"
    state.updated_at = datetime.now(tz=timezone.utc)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def _make_state(
    run_id: str,
    org: str,
    repo: str,
    state_name: str,
    **kwargs,
) -> RunState:
    now = datetime.now(tz=timezone.utc)
    return RunState(
        run_id=run_id,
        state=state_name,
        org=org,
        repo=repo,
        created_at=now,
        updated_at=now,
        **kwargs,
    )


def _transition(state: RunState, new_state: str, **updates) -> RunState:
    """Update state fields and persist."""
    state.state = new_state
    for k, v in updates.items():
        setattr(state, k, v)
    _save_state(state)
    log.info("pipeline.state_transition", state=new_state, run_id=state.run_id)
    return state


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def determine_next_workflow_action(
    workflow_rules: WorkflowRules,
    issue_number: int,
    github_username: str,
) -> str:
    """
    Decide what workflow assignment action should be taken based on
    dynamically extracted capabilities.
    """
    # 1. Check if assignment is actually required
    # False indicates direct contribution is allowed or assignment is optional
    if workflow_rules.direct_pr_allowed is True:
        log.info("workflow_engine.action", action="SKIP_ASSIGNMENT", reason="direct PR allowed")
        return "SKIP_ASSIGNMENT"
    
    if workflow_rules.assignment_required is False:
        log.info("workflow_engine.action", action="SKIP_ASSIGNMENT", reason="assignment not required")
        return "SKIP_ASSIGNMENT"

    # 2. Check if a self-assign command / claim command is supported
    if (workflow_rules.self_assign_allowed is True or workflow_rules.claim_bot_present is True) and workflow_rules.claim_command:
        log.info(
            "workflow_engine.action",
            action="AUTO_CLAIM",
            command=workflow_rules.claim_command,
            reason="self assign/claim command supported"
        )
        return "AUTO_CLAIM"

    # 3. Check if human proposal/discussion is requested first
    if workflow_rules.proposal_required is True:
        log.info("workflow_engine.action", action="REQUEST_DISCUSSION", reason="proposal/discussion required first")
        return "REQUEST_DISCUSSION"

    # 4. Standard fallback to wait for manual assignment
    if workflow_rules.assignment_required is True:
        log.info("workflow_engine.action", action="WAIT_FOR_ASSIGNMENT", reason="assignment required, no claim command found")
        return "WAIT_FOR_ASSIGNMENT"

    return "CONTINUE_PIPELINE"


def run_pipeline(
    org: str,
    repo: str,
    issue_number: Optional[int] = None,
    dry_run: bool = False,
) -> RunState:
    """
    Execute the full 10-step FI-PR-GENERATOR pipeline.

    Parameters:
        org          — GitHub organisation name
        repo         — Repository name within the org
        issue_number — If provided, skip issue scoring and work on this issue
        dry_run      — If True, run everything except GitHub writes and ntfy

    Returns the final RunState object.
    """
    from agents import memory_builder, reviewer, scorer
    from agents.coder import AllModelsExhaustedError, generate_patch, scope_guard
    from integrations import aider_runner, git_ops, ntfy_notifier, test_runner
    from integrations.github_client import (
        check_assignment,
        get_issue,
        get_open_issues,
        get_repo_activity,
        post_comment,
        is_collaborator,
        get_or_create_fork,
        sync_fork_with_upstream,
    )
    from memory.org_memory import load_or_build_org_memory

    run_id = _new_run_id()
    log.info("pipeline.start", run_id=run_id, org=org, repo=repo, dry_run=dry_run)

    state = _make_state(run_id, org, repo, "selecting")
    _save_state(state)

    # Auto-register and whitelist/enable this repository in config/orgs.json, disabling others
    try:
        from integrations.command_listener import register_active_repos
        register_active_repos([(org, repo)])
    except Exception as exc:
        log.warning("pipeline.auto_registration_failed", error=str(exc))

    # -----------------------------------------------------------------------
    # ORG WHITELIST VALIDATION
    # -----------------------------------------------------------------------
    try:
        if os.environ.get("DISABLE_ORG_WHITELIST", "").lower() == "true":
            log.info("pipeline.whitelist_disabled_via_env")
        else:
            import json as _json
            _orgs_path = Path("config/orgs.json")
            if _orgs_path.exists():
                _orgs_cfg = _json.loads(_orgs_path.read_text(encoding="utf-8"))
                _allowed = [o.get("name") for o in _orgs_cfg.get("orgs", []) if isinstance(o, dict)]
                if _allowed and org not in _allowed:
                    log.error("pipeline.org_not_whitelisted", org=org, allowed=_allowed)
                    return _transition(
                        state, "blocked",
                        failure_reason=f"Org '{org}' not in config/orgs.json whitelist. Add it first.",
                    )
    except Exception as exc:
        log.warning("pipeline.whitelist_check_failed", error=str(exc))

    # -----------------------------------------------------------------------
    # STEP 1: ACTIVITY GATE
    # -----------------------------------------------------------------------
    _transition(state, "checking")
    try:
        repo_activity = get_repo_activity(org, repo)
        activity = scorer.compute_activity_score(repo_activity)
        if activity.score < 60:
            log.warning(
                "pipeline.blocked.low_activity",
                score=activity.score,
                org=org,
                repo=repo,
            )
            return _transition(state, "blocked", failure_reason=f"Org activity score {activity.score:.0f} < 60")

        memory = load_or_build_org_memory(
            org, repo, builder_fn=lambda o, r: memory_builder.build_org_memory(o, r)
        )
    except Exception as exc:
        log.error("pipeline.step1_failed", error=str(exc))
        return _transition(state, "failed", failure_reason=f"Step 1 error: {exc}")

    # -----------------------------------------------------------------------
    # STEP 2: ISSUE SELECTION
    # -----------------------------------------------------------------------
    _transition(state, "selecting")
    try:
        if issue_number:
            raw_issues = [get_issue(org, repo, issue_number)]
            raw_issues = [i for i in raw_issues if i]  # filter None
        else:
            raw_issues = get_open_issues(org, repo)

        if not raw_issues:
            return _transition(state, "blocked", failure_reason="No open unassigned issues found")

        scored_issues = scorer.score_issues_batch(raw_issues, memory, activity)
        eligible = [s for s in scored_issues if s.score >= 60]

        if not eligible:
            return _transition(
                state, "blocked",
                failure_reason="No issues scored >= 60 — try a different repo or wait for better issues",
            )

        best_scored = max(eligible, key=lambda s: s.score)
        best_issue = next(i for i in raw_issues if i["number"] == best_scored.issue_number)

        log.info(
            "pipeline.issue_selected",
            issue=best_issue["number"],
            title=best_issue.get("title", ""),
            score=best_scored.score,
            decision=best_scored.decision,
        )

        _transition(state, "planning", issue_number=best_issue["number"])
    except Exception as exc:
        log.error("pipeline.step2_failed", error=str(exc))
        return _transition(state, "failed", failure_reason=f"Step 2 error: {exc}")

    issue = best_issue
    issue_body = issue.get("body") or ""

    # -----------------------------------------------------------------------
    # STEP 3: ASSIGNMENT CHECK & ADAPTIVE WORKFLOW ROUTING
    # -----------------------------------------------------------------------
    # Old logic (commented out):
    # if GITHUB_USERNAME and not dry_run:
    #     try:
    #         assigned = check_assignment(org, repo, issue["number"], GITHUB_USERNAME)
    #         if not assigned:
    #             # Post assignment request comment
    #             wf = memory.workflow_rules
    #             if wf.claim_bot_present and wf.claim_command:
    #                 comment_body = wf.claim_command
    #             else:
    #                 comment_body = (
    #                     f"Hi! I'd like to work on this issue. "
    #                     f"Could I please be assigned? 🙏"
    #                 )
    #             post_comment(org, repo, issue["number"], comment_body)
    #             return _transition(
    #                 state, "blocked",
    #                 failure_reason=f"Issue #{issue['number']} not yet assigned to {GITHUB_USERNAME} — comment posted",
    #             )
    #     except Exception as exc:
    #         log.error("pipeline.step3_failed", error=str(exc))
    #         return _transition(state, "failed", failure_reason=f"Step 3 error: {exc}")

    if GITHUB_USERNAME and not dry_run:
        try:
            assigned = check_assignment(org, repo, issue["number"], GITHUB_USERNAME)
            if not assigned:
                wf = memory.workflow_rules
                action = determine_next_workflow_action(wf, issue["number"], GITHUB_USERNAME)
                
                if action == "SKIP_ASSIGNMENT" or action == "CONTINUE_PIPELINE":
                    log.info("pipeline.step3.skip_assignment", action=action)
                elif action == "AUTO_CLAIM":
                    # Check anti-spam safeguards
                    if state.claim_attempt_count >= 3:
                        log.warning("pipeline.step3.auto_claim_limit_reached", count=state.claim_attempt_count)
                        return _transition(
                            state, "blocked",
                            failure_reason=f"Auto-claim failed after 3 attempts. Manual assignment required for issue #{issue['number']}.",
                        )
                        
                    now = datetime.now(tz=timezone.utc)
                    if state.last_claim_attempt:
                        elapsed = (now - state.last_claim_attempt).total_seconds()
                        if elapsed < state.claim_cooldown_seconds:
                            cooldown_left = int(state.claim_cooldown_seconds - elapsed)
                            log.warning("pipeline.step3.auto_claim_cooldown", seconds_left=cooldown_left)
                            return _transition(
                                state, "blocked",
                                failure_reason=f"Auto-claim cooldown active. Try again in {cooldown_left} seconds.",
                            )
                    
                    # Post claim comment
                    comment_body = wf.claim_command or "/assign"
                    log.info("pipeline.step3.post_claim_comment", command=comment_body, count=state.claim_attempt_count + 1)
                    post_comment(org, repo, issue["number"], comment_body)
                    
                    # Update claim attempt state
                    state.last_claim_attempt = now
                    state.claim_attempt_count += 1
                    _save_state(state)
                    
                    # Wait 15 seconds for the bot to process
                    import time
                    log.info("pipeline.step3.waiting_for_assignment_bot", seconds=15)
                    time.sleep(15)
                    
                    # Re-verify assignment
                    assigned_after = check_assignment(org, repo, issue["number"], GITHUB_USERNAME)
                    if assigned_after:
                        log.info("pipeline.step3.auto_assignment_successful")
                    else:
                        log.info("pipeline.step3.auto_assignment_pending")
                        return _transition(
                            state, "blocked",
                            failure_reason=f"Claim command '{comment_body}' posted. Waiting for bot to assign issue #{issue['number']} to {GITHUB_USERNAME}.",
                        )
                elif action == "REQUEST_DISCUSSION":
                    # Post discussion request
                    comment_body = (
                        f"Hi! I'd like to work on this issue. I am preparing a proposal of my "
                        f"approach to solve this problem. Can you assign this to me? Thanks!"
                    )
                    post_comment(org, repo, issue["number"], comment_body)
                    return _transition(
                        state, "blocked",
                        failure_reason=f"Proposal/Discussion required. Discussion comment posted on issue #{issue['number']}.",
                    )
                else:  # WAIT_FOR_ASSIGNMENT
                    comment_body = f"Hi! I'd like to work on this issue. Could I please be assigned? 🙏"
                    post_comment(org, repo, issue["number"], comment_body)
                    return _transition(
                        state, "blocked",
                        failure_reason=f"Issue #{issue['number']} not yet assigned to {GITHUB_USERNAME} — comment posted",
                    )
        except Exception as exc:
            log.error("pipeline.step3_failed", error=str(exc))
            return _transition(state, "failed", failure_reason=f"Step 3 error: {exc}")

    # -----------------------------------------------------------------------
    # STEP 3.5: FORK DETECTION
    # -----------------------------------------------------------------------
    use_fork = False
    fork_owner = GITHUB_USERNAME
    fork_repo_name = repo

    if GITHUB_USERNAME:
        try:
            # Check collaborator status
            is_collab = is_collaborator(org, repo, GITHUB_USERNAME)
            if not is_collab:
                use_fork = True
                log.info("pipeline.fork_mode_enabled", reason="not a collaborator — using fork workflow")
                fork_owner, fork_repo_name = get_or_create_fork(org, repo)
                if not dry_run:
                    sync_fork_with_upstream(fork_owner, fork_repo_name, org, repo)
            else:
                log.info("pipeline.collaborator_mode", reason="user is collaborator — pushing directly to upstream")
        except Exception as exc:
            log.warning("pipeline.fork_detection_failed", error=str(exc))
            # Fallback to direct push/cloning
            use_fork = False

    # -----------------------------------------------------------------------
    # STEP 4: CONTEXT RETRIEVAL
    # -----------------------------------------------------------------------
    _transition(state, "retrieving")
    try:
        branch_name = git_ops.get_branch_name(issue["number"], issue.get("title", "fix"))
        local_path = git_ops.get_local_path(org, repo)

        git_repo = git_ops.clone_fork_or_upstream(
            org=org,
            repo=repo,
            fork_owner=fork_owner,
            fork_repo=fork_repo_name,
            local_path=str(local_path),
            use_fork=use_fork,
        )
        if git_repo is None:
            return _transition(state, "blocked", failure_reason="Failed to clone repository")

        git_ops.create_branch(git_repo, branch_name)

        preflight = test_runner.run_preflight(str(local_path))

        repo_map = aider_runner.get_repo_map(str(local_path))
        relevant_files = aider_runner.find_relevant_files(
            str(local_path), f"{issue.get('title','')} {issue_body}", repo_map
        )

        if not relevant_files:
            return _transition(
                state, "blocked",
                failure_reason="Context retrieval failed — no relevant files found via ripgrep",
            )

        _transition(state, "planning", branch=branch_name)
    except Exception as exc:
        log.error("pipeline.step4_failed", error=str(exc))
        return _transition(state, "failed", failure_reason=f"Step 4 error: {exc}")

    # -----------------------------------------------------------------------
    # STEP 5: PLAN + CODE (with retry loop)
    # -----------------------------------------------------------------------
    _transition(state, "coding")
    error_context = ""
    model_used = ""
    final_diff = ""
    test_output = ""
    failure_class = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            log.info("pipeline.coding_attempt", attempt=attempt + 1, max=MAX_RETRIES + 1)

            # Roll back any previous failed attempt
            if attempt > 0:
                git_ops.reset_to_head(git_repo)

            patch, model_used = generate_patch(
                issue, relevant_files, memory, error_context, str(local_path)
            )
            state.retry_count = attempt
            state.model_used = model_used

            applied = aider_runner.apply_patch(str(local_path), patch, model=model_used)
            if not applied:
                error_context = "Patch failed to apply cleanly."
                continue

            final_diff = git_ops.get_diff(git_repo)

            # Guard: catch empty diffs early (wrong context → wrong patch → no change)
            if not final_diff.strip():
                error_context = (
                    "Patch was applied but produced no diff — likely wrong context files. "
                    "Retrying with broader file search."
                )
                log.warning("pipeline.empty_diff_after_apply", attempt=attempt + 1)
                continue

            # Scope guard — hard reject if diff is too large
            if not scope_guard(final_diff):
                from integrations.git_ops import count_diff_lines
                n_lines = count_diff_lines(final_diff)
                error_context = (
                    f"Diff too large ({n_lines} lines, limit {200}). "
                    "Reduce scope — fix ONLY the specific thing the issue asks for."
                )
                log.warning("pipeline.scope_exceeded", lines=n_lines)
                continue

            # Run tests
            test_cmd = preflight.get("test_command")
            passed, test_output = test_runner.run_tests(str(local_path), test_cmd)
            failure_class = test_runner.classify_failure(test_output, str(local_path))

            if failure_class == "CODE_BUG":
                error_context = f"Tests failed with CODE_BUG:\n{test_output[-500:]}"
                log.warning("pipeline.test_bug_retrying", attempt=attempt + 1)
                continue

            # PASS, ENV_ISSUE, FLAKY, PREEXISTING — all proceed to review
            break

        except AllModelsExhaustedError as exc:
            log.error("pipeline.all_models_exhausted", error=str(exc))
            return _transition(state, "failed", failure_reason=str(exc))
        except Exception as exc:
            log.error("pipeline.step5_exception", attempt=attempt, error=str(exc))
            error_context = str(exc)
            continue
    else:
        return _transition(
            state, "failed",
            failure_reason=f"Max retries ({MAX_RETRIES}) reached. Last error: {error_context}",
        )

    # Save diff to disk
    diff_path = DIFFS_DIR / f"{run_id}.diff"
    diff_path.write_text(final_diff, encoding="utf-8")
    _transition(state, "reviewing", diff_path=str(diff_path), model_used=model_used)

    # -----------------------------------------------------------------------
    # STEP 6: REVIEW
    # -----------------------------------------------------------------------
    try:
        reviewer_approved, reviewer_notes = reviewer.review_patch(final_diff, issue, memory)
        log.info(
            "pipeline.review_done",
            approved=reviewer_approved,
            notes=len(reviewer_notes),
        )
        # Note: reviewer rejection does NOT stop pipeline — human sees notes in ntfy
        if not reviewer_approved:
            log.warning("pipeline.reviewer_flagged_critical_issues")
    except Exception as exc:
        log.error("pipeline.step6_failed", error=str(exc))
        reviewer_approved = True
        reviewer_notes = ["[!] Reviewer error: " + str(exc)]

    # -----------------------------------------------------------------------
    # STEP 7: RISK SCORE
    # -----------------------------------------------------------------------
    try:
        files_changed = [
            line.split()[-1]
            for line in final_diff.splitlines()
            if line.startswith("+++ b/")
        ]
        test_added = any("test" in f.lower() or "spec" in f.lower() for f in files_changed)
        # Flash is the primary free-tier model — only penalize if we fell past it
        primary_models = {"gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"}
        used_fallback = model_used not in primary_models
        risk = scorer.compute_risk_score(
            final_diff, files_changed, test_added, used_fallback,
            reviewer_flagged=not reviewer_approved,
        )
        _transition(state, "testing", risk_score=risk, test_result=failure_class)
    except Exception as exc:
        log.error("pipeline.step7_failed", error=str(exc))
        from memory.schemas import RiskScore
        risk = RiskScore(
            score=50.0, level="medium",
            diff_size_score=50.0, file_criticality=50.0,
            test_coverage_gap=50.0, confidence_loss=50.0,
        )

    # -----------------------------------------------------------------------
    # STEP 8: NTFY APPROVAL GATE (hard gate — never skipped except dry_run)
    # -----------------------------------------------------------------------
    _transition(state, "waiting_approval")

    if not dry_run:
        try:
            from integrations.ntfy_notifier import ApprovalRequest, send_and_wait

            # Build per-file diff summary
            diff_summary_lines = []
            for f in files_changed[:5]:
                add = sum(1 for l in final_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
                rem = sum(1 for l in final_diff.splitlines() if l.startswith("-") and not l.startswith("---"))
                diff_summary_lines.append(f"  {f} (+{add}/-{rem})")
            diff_summary = "\n".join(diff_summary_lines)

            req = ApprovalRequest(
                run_id=run_id,
                org=org,
                repo=repo,
                issue_number=issue["number"],
                issue_title=issue.get("title", ""),
                branch=branch_name,
                files_changed=files_changed,
                diff_summary=diff_summary,
                test_result=failure_class or "UNKNOWN",
                risk_level=risk.level,
                risk_score=risk.score,
                reviewer_notes=reviewer_notes,
                model_used=model_used,
            )

            approval = send_and_wait(req)

            if approval is None:
                return _transition(
                    state, "blocked",
                    failure_reason="Approval timeout — state preserved. Resume with run_id.",
                )
            if approval is False:
                # Human rejected — log to memory
                try:
                    from integrations.git_ops import count_diff_lines
                    entry = RejectionEntry(
                        rejected_at=datetime.now(tz=timezone.utc),
                        issue_number=issue["number"],
                        issue_type=", ".join(issue.get("labels", ["unknown"])),
                        diff_size_lines=count_diff_lines(final_diff),
                        rejection_reason="Human rejected via ntfy",
                        rejected_by="human",
                    )
                    memory_builder.append_rejection(org, repo, entry)
                except Exception:
                    pass
                return _transition(state, "failed", failure_reason="Rejected by human via ntfy")

        except Exception as exc:
            log.error("pipeline.step8_failed", error=str(exc))
            return _transition(state, "failed", failure_reason=f"Step 8 (ntfy) error: {exc}")
    else:
        log.info("pipeline.dry_run_skipping_approval_gate")

    # -----------------------------------------------------------------------
    # STEP 9: REBASE + RE-CHECK (after human approval)
    # -----------------------------------------------------------------------
    _transition(state, "pushing")
    if not dry_run:
        try:
            if use_fork:
                rebased = git_ops.rebase_fork_from_upstream(git_repo)
            else:
                rebased = git_ops.rebase_from_main(git_repo)
            if not rebased:
                return _transition(
                    state, "blocked",
                    failure_reason="Rebase conflict — manual resolution needed",
                )

            # Re-run tests after rebase
            test_cmd = preflight.get("test_command")
            passed_after, _ = test_runner.run_tests(str(local_path), test_cmd)
            if not passed_after:
                return _transition(
                    state, "failed",
                    failure_reason="Tests failed after rebase — patch may conflict with merged changes",
                )

            # Re-check assignment
            if GITHUB_USERNAME:
                still_assigned = check_assignment(org, repo, issue["number"], GITHUB_USERNAME)
                if not still_assigned:
                    return _transition(
                        state, "blocked",
                        failure_reason="Issue reassigned to someone else while waiting for approval",
                    )

            # Commit the patch
            commit_msg = _build_commit_message(issue, memory)
            git_ops.stage_and_commit(git_repo, commit_msg)

        except Exception as exc:
            log.error("pipeline.step9_failed", error=str(exc))
            return _transition(state, "failed", failure_reason=f"Step 9 error: {exc}")

    # -----------------------------------------------------------------------
    # STEP 10: PUSH + DRAFT PR
    # -----------------------------------------------------------------------
    _transition(state, "drafting_pr")
    if not dry_run:
        try:
            if use_fork:
                pushed = git_ops.push_to_fork(git_repo, branch_name)
            else:
                pushed = git_ops.push_branch(git_repo, branch_name)
            if not pushed:
                return _transition(state, "failed", failure_reason="Failed to push branch")

            pr_body = _build_pr_body(issue, final_diff, failure_class, reviewer_notes, model_used, risk)
            issue_num = issue["number"]
            issue_title_fallback = "Fix #" + str(issue_num)
            pr_title = issue.get("title", issue_title_fallback) + " (closes #" + str(issue_num) + ")"

            if use_fork:
                pr_url = git_ops.create_cross_repo_draft_pr(
                    upstream_org=org,
                    upstream_repo=repo,
                    fork_owner=fork_owner,
                    branch=branch_name,
                    title=pr_title,
                    body=pr_body,
                    issue_number=issue_num
                )
            else:
                pr_url = git_ops.create_draft_pr(org, repo, branch_name, pr_title, pr_body)

            if pr_url:
                log.info("pipeline.draft_pr_created", url=pr_url)
            else:
                log.warning("pipeline.draft_pr_failed_but_branch_pushed")

        except Exception as exc:
            log.error("pipeline.step10_failed", error=str(exc))
            return _transition(state, "failed", failure_reason=f"Step 10 error: {exc}")
    else:
        log.info(
            "pipeline.dry_run_complete",
            would_push=branch_name,
            diff_lines=len(final_diff.splitlines()),
            model=model_used,
            risk=risk.level,
        )

    return _transition(state, "completed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_commit_message(issue: dict, memory) -> str:
    """Build a commit message following the repo's commit style."""
    style = memory.conventions.get("commit_style", "fix: description")
    title = issue.get("title", "fix issue").lower()
    number = issue.get("number", "?")

    # Try to match the commit style pattern
    if "(" in style and ")" in style:
        # conventional commits: fix(component): description
        return f"fix: {title[:60]} (#{number})"
    return f"fix: {title[:60]} (closes #{number})"


def _build_pr_body(
    issue: dict,
    diff: str,
    test_result: str,
    reviewer_notes: list[str],
    model_used: str,
    risk,
) -> str:
    """Build the draft PR description body."""
    from integrations.git_ops import count_diff_lines
    from memory.config_loader import get_model_name

    notes_section = ""
    if reviewer_notes:
        notes_section = "\n**Reviewer Notes:**\n" + "\n".join(f"- {n}" for n in reviewer_notes[:5])

    test_icon = "✅" if test_result == "PASS" else "⚠️"
    review_model = get_model_name("review_model", "llama-3.3-70b-versatile")

    return f"""## Summary

Closes #{issue.get("number")} — {issue.get("title", "")}

{(issue.get("body") or "")[:300]}

---

## Changes

- Diff size: {count_diff_lines(diff)} lines changed
- Test result: {test_icon} `{test_result}`
- Risk level: `{risk.level}` ({risk.score:.0f}/100)
- Generated by: `{model_used}`

{notes_section}

---

## AI Disclosure

This pull request was generated with AI assistance using FI-PR-GENERATOR.
The patch was:
1. Generated by `{model_used}`
2. Independently reviewed by `{review_model}`
3. Validated by running the repository's test suite
4. Approved by a human before being submitted

All decisions and code remain the contributor's responsibility.
"""

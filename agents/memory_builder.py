"""
agents/memory_builder.py — Org memory construction and incremental refresh.

Uses Gemini 2.0 Flash to analyze merged PR history and extract maintainer
conventions, file hotspots, and contribution patterns.

ALSO includes the Contribution Workflow Detector:
    Dynamically discovers how THIS repo expects contributions to happen.
    Patterns are NOT predefined — the LLM reads CONTRIBUTING.md, bot comments,
    issue templates, and maintainer discussions and infers patterns from scratch.
    Discovered patterns are stored in OrgMemory.workflow_rules (open-ended schema).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import structlog

from integrations import github_client
from memory.config_loader import get_model_name, get_model_provider
from memory.org_memory import load_org_memory, save_org_memory
from memory.schemas import ActivityScore, OrgMemory, RejectionEntry, WorkflowRules

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Gemini client helper
# ---------------------------------------------------------------------------


def _gemini_json_call(prompt: str, model: str = "gemini-2.0-flash") -> dict:
    """
    Call Gemini and parse the response as JSON.

    Returns empty dict on any failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("memory_builder.no_gemini_key")
        return {}

    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        gmodel = genai.GenerativeModel(model)
        response = gmodel.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        text = response.text.strip()
        # Strip any markdown code fences that might wrap the JSON
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("memory_builder.gemini_json_parse_failed", error=str(exc))
        return {}
    except Exception as exc:
        log.error("memory_builder.gemini_call_failed", error=str(exc))
        return {}


def _groq_json_call(prompt: str, model: str = "llama-3.3-70b-versatile") -> dict:
    """
    Call Groq and parse the response as JSON.

    Used as a fallback if Gemini is unavailable, and for workflow detection.
    Returns empty dict on any failure.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log.warning("memory_builder.no_groq_key")
        return {}
    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        text = response.choices[0].message.content.strip()
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("memory_builder.groq_json_parse_failed", error=str(exc))
        return {}
    except Exception as exc:
        log.error("memory_builder.groq_call_failed", error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------


def build_org_memory(org: str, repo: str) -> OrgMemory:
    """
    Build full org memory from scratch using the last 50 merged PRs.

    Steps:
    1. Fetch merged PRs and maintainer review comments
    2. Fetch CONTRIBUTING.md, issue templates, bot comments
    3. Call Gemini 2.0 Flash to extract conventions, patterns, workflow rules
    4. Construct and return OrgMemory object (caller saves it)
    """
    log.info("memory_builder.building", org=org, repo=repo)

    # Fetch raw data
    closed_prs = github_client.get_closed_prs(org, repo, limit=50)
    repo_activity = github_client.get_repo_activity(org, repo)
    contributing_md = github_client.get_contributing_md(org, repo)
    issue_templates = github_client.get_issue_templates(org, repo)
    bot_comments = github_client.get_bot_comments(org, repo, limit=30)

    from agents.scorer import compute_activity_score

    activity = compute_activity_score(repo_activity)

    # Build conventions + pattern prompt
    conventions_data = _extract_conventions(org, repo, closed_prs)

    # Build workflow detection (open-ended LLM discovery)
    workflow_rules = _detect_workflow(
        org=org,
        repo=repo,
        contributing_md=contributing_md,
        issue_templates=issue_templates,
        bot_comments=bot_comments,
        closed_prs=closed_prs,
    )

    now = datetime.now(tz=timezone.utc)

    memory = OrgMemory(
        schema_version=1,
        org_name=org,
        repo_name=repo,
        last_refresh=now,
        refresh_frequency_hours=48,
        confidence=0.6 if closed_prs else 0.2,
        activity=activity,
        conventions=conventions_data.get("conventions", {}),
        file_knowledge=conventions_data.get("file_knowledge", {}),
        pattern_learning=conventions_data.get("pattern_learning", {}),
        workflow_rules=workflow_rules,
        rejection_log=[],
    )

    log.info(
        "memory_builder.built",
        org=org,
        repo=repo,
        prs_analyzed=len(closed_prs),
        workflow_confidence=workflow_rules.confidence,
    )
    return memory


def _extract_conventions(org: str, repo: str, closed_prs: list[dict]) -> dict:
    """
    Use Gemini 2.0 Flash to extract repository conventions from merged PRs.
    """
    if not closed_prs:
        log.warning("memory_builder.no_prs_for_conventions", org=org, repo=repo)
        return {}

    # Build a compact PR summary to keep within token limits
    pr_summaries = []
    for pr in closed_prs[:30]:
        reviews_text = "; ".join(
            r.get("body", "")[:100]
            for r in pr.get("reviews", [])
            if r.get("body")
        )[:300]
        pr_summaries.append(
            f"PR #{pr['number']}: {pr['title']}\n"
            f"  Branch: {pr.get('head_ref', '')}\n"
            f"  Labels: {', '.join(pr.get('labels', []))}\n"
            f"  Files: +{pr.get('additions', 0)}/-{pr.get('deletions', 0)} ({pr.get('changed_files', 0)} files)\n"
            f"  Review notes: {reviews_text}"
        )

    pr_text = "\n\n".join(pr_summaries)

    prompt = f"""Analyze these merged pull requests from {org}/{repo} and extract repository conventions.

Merged PRs:
{pr_text}

Extract and return as JSON with exactly these keys:
{{
  "conventions": {{
    "commit_style": "example commit message style",
    "branch_naming": "pattern like fix/description-123",
    "test_commands": ["list", "of", "test", "commands"],
    "build_command": "build command if detectable",
    "package_manager": "npm/pip/cargo/etc",
    "primary_language": "main language"
  }},
  "file_knowledge": {{
    "hotspots": ["top 10 most-changed files"],
    "test_directory": "path to test directory or empty string",
    "config_files": ["list of config files changed"]
  }},
  "pattern_learning": {{
    "accepted_issue_types": ["types of issues that get merged: docs/bug/frontend/etc"],
    "rejected_issue_types": ["types commonly rejected or not merged"],
    "accepted_pr_size_avg_lines": 0,
    "maintainer_preferences": ["up to 5 specific preferences from review comments"]
  }}
}}

Respond with valid JSON only."""

    provider = get_model_provider("memory_provider", "google")
    model = get_model_name("memory_model", "gemini-2.0-flash")

    result = None
    # Try Ollama first if configured
    if provider == "ollama":
        try:
            from integrations.ollama_client import call_ollama
            raw = call_ollama(
                model=model,
                prompt=prompt,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            result = json.loads(raw)
        except Exception as exc:
            log.warning("memory_builder.conventions_ollama_failed", error=str(exc)[:100], fallback="gemini/groq")

    # Try original fallbacks
    if not result:
        if provider == "google" or provider == "gemini" or (provider != "ollama" and os.environ.get("GEMINI_API_KEY")):
            result = _gemini_json_call(prompt, model=model if provider in ["google", "gemini"] else "gemini-2.0-flash")

    if not result:
        # Try Groq fallback
        fallback_model = get_model_name("workflow_detector_model", "llama-3.3-70b-versatile")
        result = _groq_json_call(prompt, model=fallback_model)
    return result


# ---------------------------------------------------------------------------
# Contribution Workflow Detector — OPEN-ENDED LLM DISCOVERY
# ---------------------------------------------------------------------------


def _detect_workflow(
    org: str,
    repo: str,
    contributing_md: str,
    issue_templates: list[str],
    bot_comments: list[str],
    closed_prs: list[dict],
) -> WorkflowRules:
    """
    Discover HOW this repository expects contributions to happen.

    This is intentionally open-ended: the LLM is NOT given a list of patterns
    to match against. Instead it reads all available evidence and freely
    describes what patterns it detects. Novel patterns go into raw_patterns.

    Sources analyzed:
    - CONTRIBUTING.md (explicit rules)
    - Issue templates (structured contribution requirements)
    - Bot/maintainer comments (assignment mechanisms)
    - Merged PR branch names and titles (naming conventions learned)
    """
    evidence_parts = []

    if contributing_md:
        evidence_parts.append(
            f"=== CONTRIBUTING.md ===\n{contributing_md[:3000]}"
        )

    if issue_templates:
        templates_text = "\n\n".join(t[:500] for t in issue_templates[:3])
        evidence_parts.append(f"=== ISSUE TEMPLATES ===\n{templates_text}")

    if bot_comments:
        comments_text = "\n".join(f"- {c[:150]}" for c in bot_comments[:20])
        evidence_parts.append(f"=== BOT/MAINTAINER COMMENTS ===\n{comments_text}")

    if closed_prs:
        pr_titles = "\n".join(
            f"- PR #{p['number']}: {p['title']} (branch: {p.get('head_ref', '')})"
            for p in closed_prs[:20]
        )
        evidence_parts.append(f"=== RECENT MERGED PR TITLES/BRANCHES ===\n{pr_titles}")

    if not evidence_parts:
        log.warning(
            "memory_builder.no_workflow_evidence",
            org=org,
            repo=repo,
            note="Using default WorkflowRules",
        )
        return WorkflowRules(confidence=0.1)

    evidence = "\n\n".join(evidence_parts)

    prompt = f"""You are analyzing the contribution workflow for {org}/{repo}.

Study ALL the evidence below and discover how contributors are expected to submit changes.
Do NOT assume standard GitHub flow — every repo is different.

Evidence:
{evidence}

Your task: Identify contribution workflow patterns from the evidence. Be specific.
For each pattern you detect, describe it clearly. If you see something unusual or novel,
capture it in raw_patterns even if it does not fit standard categories.

Return ONLY valid JSON with this structure:
{{
  "assignment_required": true or false or null,
  "claim_bot_present": true or false or null,
  "claim_command": "/claim or /assign or !take or null",
  "proposal_required": true or false or null,
  "direct_pr_allowed": true or false or null,
  "pr_template_required": true or false or null,
  "issue_template_required": true or false or null,
  "bot_assigns": true or false or null,
  "maintainer_assigns": true or false or null,
  "self_assign_allowed": true or false or null,
  "confidence": 0.0 to 1.0 based on evidence quality,
  "inferred_from": ["list of source documents that provided evidence"],
  "raw_patterns": [
    {{
      "pattern_name": "short descriptive name",
      "description": "what this pattern means for contributors",
      "evidence": "specific text or observation that revealed this pattern",
      "affects": "what stage of contribution this affects: issue/pr/review/merge"
    }}
  ]
}}

Use null for fields where you have no evidence. Be honest about confidence.
Respond with JSON only."""

    provider = get_model_provider("workflow_detector_provider", "groq")
    detector_model = get_model_name("workflow_detector_model", "llama-3.3-70b-versatile")

    data = None
    # Try Ollama first if configured
    if provider == "ollama":
        try:
            from integrations.ollama_client import call_ollama
            raw = call_ollama(
                model=detector_model,
                prompt=prompt,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            data = json.loads(raw)
        except Exception as exc:
            log.warning("memory_builder.workflow_ollama_failed", error=str(exc)[:100], fallback="groq/gemini")

    # Try original fallbacks
    if not data:
        if provider == "groq" or (provider != "ollama" and os.environ.get("GROQ_API_KEY")):
            data = _groq_json_call(prompt, model=detector_model if provider == "groq" else "llama-3.3-70b-versatile")

    if not data:
        # Fallback to Gemini
        fallback_model = get_model_name("memory_model", "gemini-2.0-flash")
        data = _gemini_json_call(prompt, model=fallback_model)

    if not data:
        log.warning("memory_builder.workflow_detection_failed", org=org, repo=repo)
        return WorkflowRules(confidence=0.1)

    try:
        rules = WorkflowRules(
            assignment_required=data.get("assignment_required"),
            claim_bot_present=data.get("claim_bot_present"),
            claim_command=data.get("claim_command"),
            proposal_required=data.get("proposal_required"),
            direct_pr_allowed=data.get("direct_pr_allowed"),
            pr_template_required=data.get("pr_template_required"),
            issue_template_required=data.get("issue_template_required"),
            bot_assigns=data.get("bot_assigns"),
            maintainer_assigns=data.get("maintainer_assigns"),
            self_assign_allowed=data.get("self_assign_allowed"),
            confidence=float(data.get("confidence", 0.5)),
            last_inferred_at=datetime.now(tz=timezone.utc),
            inferred_from=data.get("inferred_from", []),
            raw_patterns=data.get("raw_patterns", []),
        )
        log.info(
            "memory_builder.workflow_detected",
            org=org,
            repo=repo,
            claim_bot=rules.claim_bot_present,
            assignment_required=rules.assignment_required,
            novel_patterns=len(rules.raw_patterns),
            confidence=rules.confidence,
        )
        return rules
    except Exception as exc:
        log.error("memory_builder.workflow_parse_failed", error=str(exc))
        return WorkflowRules(confidence=0.1)


# ---------------------------------------------------------------------------
# Incremental refresh
# ---------------------------------------------------------------------------


def refresh_org_memory(org: str, repo: str, since_hours: int = 48) -> OrgMemory:
    """
    Incremental memory refresh — only process PRs merged since last_refresh.

    Merges new patterns into existing memory without overwriting.
    Bumps schema_version on save.
    """
    existing = load_org_memory(org, repo)
    if existing is None:
        log.info("memory_builder.no_existing_memory_doing_full_build", org=org, repo=repo)
        memory = build_org_memory(org, repo)
        save_org_memory(memory)
        return memory

    log.info("memory_builder.incremental_refresh", org=org, repo=repo)

    # Refresh activity score
    repo_activity = github_client.get_repo_activity(org, repo)
    from agents.scorer import compute_activity_score

    new_activity = compute_activity_score(repo_activity)
    existing.activity = new_activity

    # Re-run workflow detection (patterns may have changed)
    contributing_md = github_client.get_contributing_md(org, repo)
    issue_templates = github_client.get_issue_templates(org, repo)
    bot_comments = github_client.get_bot_comments(org, repo, limit=20)
    recent_prs = github_client.get_closed_prs(org, repo, limit=20)

    new_workflow = _detect_workflow(
        org=org,
        repo=repo,
        contributing_md=contributing_md,
        issue_templates=issue_templates,
        bot_comments=bot_comments,
        closed_prs=recent_prs,
    )

    # Merge workflow: keep higher-confidence version, append novel raw_patterns
    if new_workflow.confidence >= existing.workflow_rules.confidence:
        # Preserve any raw_patterns from old memory that aren't in the new one
        new_pattern_names = {
            p.get("pattern_name") for p in new_workflow.raw_patterns
        }
        for p in existing.workflow_rules.raw_patterns:
            if p.get("pattern_name") not in new_pattern_names:
                new_workflow.raw_patterns.append(p)
        existing.workflow_rules = new_workflow

    existing.last_refresh = datetime.now(tz=timezone.utc)
    existing.schema_version += 1

    save_org_memory(existing)
    log.info("memory_builder.incremental_done", org=org, repo=repo, version=existing.schema_version)
    return existing


def append_rejection(org: str, repo: str, entry: RejectionEntry) -> None:
    """
    Append a RejectionEntry to the rejection_log.

    Thin wrapper — delegates to org_memory module.
    Exposed here so orchestrator has one import point.
    """
    from memory.org_memory import append_rejection as _append

    _append(org, repo, entry)

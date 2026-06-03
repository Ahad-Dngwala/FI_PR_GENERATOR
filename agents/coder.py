"""
agents/coder.py — Code generation with Gemini 2.5 Pro as primary and fallback chain.

Fallback chain (in order):
  1. gemini/gemini-2.5-pro       (Google AI Studio — FREE primary)
  2. qwen/qwen-2.5-coder-72b-instruct (OpenRouter)
  3. deepseek/deepseek-coder-v2  (OpenRouter)
  4. claude-sonnet-4-20250514    (Anthropic — paid, last resort)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import structlog

from memory.schemas import OrgMemory

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 2
MAX_DIFF_LINES = 200
PROMPT_TEMPLATE_PATH = Path("prompts/coder.txt")
MAX_PROMPT_TOKENS = 40_000  # rough character limit (1 token ≈ 4 chars → ~160K chars)

CODING_CHAIN: list[tuple[str, str]] = [
    ("gemini/gemini-2.5-pro", "google"),
    ("qwen/qwen-2.5-coder-72b-instruct", "openrouter"),
    ("deepseek/deepseek-coder-v2", "openrouter"),
    ("claude-sonnet-4-20250514", "anthropic"),
]


class AllModelsExhaustedError(Exception):
    """Raised when every model in the fallback chain has failed."""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def load_coder_prompt(
    repo_path: str,
    issue: dict,
    relevant_files: list[str],
    org_memory: OrgMemory,
) -> str:
    """
    Load prompts/coder.txt and fill all placeholders.

    Stays within MAX_PROMPT_TOKENS by truncating issue body and file contents.
    """
    try:
        template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("coder.prompt_template_missing", path=str(PROMPT_TEMPLATE_PATH))
        template = _default_prompt_template()

    conventions = org_memory.conventions
    commit_style = conventions.get("commit_style", "fix: description")
    accepted_types = org_memory.pattern_learning.get("accepted_issue_types", [])
    maintainer_prefs = org_memory.pattern_learning.get("maintainer_preferences", [])

    # Build example commit from memory
    example_commit = conventions.get("commit_style", "fix(component): lowercase description")

    # File list — just relative paths for the prompt
    file_list = "\n".join(relevant_files) if relevant_files else "See repo structure"

    # Maintainer preferences as bullet list
    prefs_text = "\n".join(f"- {p}" for p in maintainer_prefs[:5]) if maintainer_prefs else "None recorded"

    # Workflow summary
    wf = org_memory.workflow_rules
    workflow_summary = _build_workflow_summary(wf)

    # Truncate issue body to keep prompt within token limit
    issue_body = (issue.get("body") or "")[:3000]

    filled = template.format(
        repo_name=f"{org_memory.org_name}/{org_memory.repo_name}",
        issue_number=issue.get("number", "?"),
        issue_title=issue.get("title", ""),
        issue_body=issue_body,
        file_list=file_list,
        commit_style=commit_style,
        example_commit=example_commit,
        maintainer_preferences=prefs_text,
        workflow_summary=workflow_summary,
    )

    # Hard truncate if still too large
    char_limit = MAX_PROMPT_TOKENS * 4
    if len(filled) > char_limit:
        filled = filled[:char_limit]
        log.warning("coder.prompt_truncated", char_limit=char_limit)

    return filled


def _build_workflow_summary(wf) -> str:
    """Format WorkflowRules into a human-readable summary for the prompt."""
    parts = []
    if wf.claim_bot_present and wf.claim_command:
        parts.append(f"Use '{wf.claim_command}' to claim issues before working")
    if wf.assignment_required:
        parts.append("Must be assigned to issue before submitting PR")
    if wf.proposal_required:
        parts.append("Propose approach in issue comments before coding")
    if wf.direct_pr_allowed:
        parts.append("Direct PRs are allowed without prior issue")
    if wf.pr_template_required:
        parts.append("Must fill out PR template completely")
    for p in wf.raw_patterns[:3]:
        parts.append(p.get("description", ""))
    return "; ".join(parts) if parts else "Standard GitHub flow"


def _default_prompt_template() -> str:
    """Minimal fallback if prompts/coder.txt is missing."""
    return (
        "You are fixing GitHub issue #{issue_number}: {issue_title}\n"
        "Repo: {repo_name}\n"
        "Files to modify: {file_list}\n"
        "Issue: {issue_body}\n"
        "Commit style: {commit_style}\n"
        "Output ONLY a unified diff."
    )


# ---------------------------------------------------------------------------
# Patch generation — model fallback chain
# ---------------------------------------------------------------------------


def generate_patch(
    issue: dict,
    relevant_files: list[str],
    org_memory: OrgMemory,
    error_context: str = "",
    repo_path: str = "",
) -> tuple[str, str]:
    """
    Try each model in CODING_CHAIN in order.

    If error_context is provided (retry after test failure or scope exceeded),
    appends it to the prompt so the model knows what to fix.

    Returns (patch_content, model_used).
    Raises AllModelsExhaustedError if all models fail.
    """
    prompt = load_coder_prompt(repo_path or ".", issue, relevant_files, org_memory)

    if error_context:
        prompt += (
            f"\n\n--- PREVIOUS ATTEMPT FAILED ---\n"
            f"Previous attempt failed with the following error. Fix it:\n{error_context[-1000:]}"
        )

    for model_id, provider in CODING_CHAIN:
        log.info("coder.trying_model", model=model_id, provider=provider)
        patch = _call_model(model_id, provider, prompt)
        if patch:
            log.info("coder.patch_generated", model=model_id, length=len(patch))
            return patch, model_id
        log.warning("coder.model_failed", model=model_id, provider=provider)

    raise AllModelsExhaustedError(
        "All models in the fallback chain failed to generate a patch"
    )


def _call_model(model_id: str, provider: str, prompt: str) -> Optional[str]:
    """
    Dispatch to the appropriate LLM API based on provider.
    Returns the patch string or None on failure.
    """
    try:
        if provider == "google":
            return _call_gemini(model_id, prompt)
        elif provider == "openrouter":
            return _call_openrouter(model_id, prompt)
        elif provider == "anthropic":
            return _call_anthropic(model_id, prompt)
        else:
            log.error("coder.unknown_provider", provider=provider)
            return None
    except Exception as exc:
        log.error("coder.model_exception", model=model_id, error=str(exc))
        return None


def _call_gemini(model_id: str, prompt: str) -> Optional[str]:
    """Call Gemini via google-generativeai."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("coder.no_gemini_key")
        return None
    try:
        import google.generativeai as genai

        # Strip the "gemini/" prefix for the SDK
        sdk_model = model_id.replace("gemini/", "")
        genai.configure(api_key=api_key)
        gmodel = genai.GenerativeModel(sdk_model)
        response = gmodel.generate_content(prompt)
        text = response.text.strip()
        return _clean_patch(text)
    except Exception as exc:
        log.error("coder.gemini_failed", error=str(exc))
        return None


def _call_openrouter(model_id: str, prompt: str) -> Optional[str]:
    """Call OpenRouter using the OpenAI-compatible API."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log.warning("coder.no_openrouter_key")
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
            temperature=0.1,
        )
        text = response.choices[0].message.content.strip()
        return _clean_patch(text)
    except Exception as exc:
        log.error("coder.openrouter_failed", model=model_id, error=str(exc))
        return None


def _call_anthropic(model_id: str, prompt: str) -> Optional[str]:
    """Call Anthropic Claude API."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("coder.no_anthropic_key")
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model_id,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return _clean_patch(text)
    except Exception as exc:
        log.error("coder.anthropic_failed", error=str(exc))
        return None


def _clean_patch(text: str) -> Optional[str]:
    """
    Extract and clean a unified diff from LLM output.

    Removes markdown fences, preamble text, etc.
    Returns None if no valid diff is found.
    """
    if not text:
        return None

    # Strip markdown code fences
    if "```diff" in text:
        text = text.split("```diff", 1)[1]
        text = text.rsplit("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1]
        text = text.rsplit("```", 1)[0]

    text = text.strip()

    # Must contain at least one diff header to be valid
    if "--- " not in text and "+++ " not in text:
        log.warning("coder.no_diff_header_found", preview=text[:100])
        return None

    return text


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------


def scope_guard(diff: str) -> bool:
    """
    Reject patches that exceed MAX_DIFF_LINES changed lines.

    Counts only lines starting with + or - (not context lines or headers).
    Returns True (safe) if within limit, False (reject) if over.
    """
    count = 0
    for line in diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            count += 1

    safe = count <= MAX_DIFF_LINES
    log.info("coder.scope_guard", diff_lines=count, limit=MAX_DIFF_LINES, safe=safe)
    return safe

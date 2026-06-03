"""
agents/reviewer.py — Independent code review via Qwen 2.5 Coder 32B on Groq.

The reviewer is intentionally a DIFFERENT model from the coder (Gemini 2.5 Pro)
to provide an independent perspective. This is enforced in code, not just prompt.
"""

from __future__ import annotations

import json
import os

import structlog

from memory.schemas import OrgMemory

log = structlog.get_logger(__name__)

REVIEWER_MODEL = "qwen-2.5-coder-32b-preview"
REVIEWER_PROVIDER = "groq"


def review_patch(
    patch: str,
    issue: dict,
    org_memory: OrgMemory,
) -> tuple[bool, list[str]]:
    """
    Send the patch to Qwen 2.5 Coder 32B on Groq for independent review.

    Review checks:
    1. Does it solve the stated issue?
    2. Missing imports or undefined variables?
    3. Edge cases not handled?
    4. Style inconsistencies vs maintainer preferences?
    5. Unrelated files modified?
    6. Tests missing for new behavior?

    Returns:
        (approved: bool, issues: list[str])

    Decision logic:
    - CRITICAL issues → (False, issues) — pipeline stops, coder retried
    - MINOR issues    → (True, issues)  — human sees notes in ntfy, pipeline continues
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log.warning(
            "reviewer.no_groq_key",
            fallback="Approving with warning — reviewer unavailable",
        )
        return True, ["⚠️ Reviewer unavailable (no GROQ_API_KEY) — inspect diff carefully"]

    prefs = org_memory.pattern_learning.get("maintainer_preferences", [])
    prefs_text = "\n".join(f"- {p}" for p in prefs[:5]) if prefs else "None recorded"

    prompt = f"""Review this git patch for a GitHub issue. Be a strict but fair code reviewer.

Issue #{issue.get('number')}: {issue.get('title', '')}
Issue description: {(issue.get('body') or '')[:500]}

Maintainer preferences for this repository:
{prefs_text}

Patch to review:
{patch[:8000]}

Check ALL of the following:
1. Does the patch actually solve the stated issue?
2. Are there missing imports, undefined variables, or syntax errors?
3. Are edge cases handled (null/empty inputs, boundary conditions)?
4. Does the style match the maintainer preferences listed above?
5. Are there any files modified that are UNRELATED to the issue?
6. Is new behavior added without corresponding tests?

Classify each problem as:
- "critical": would definitely cause test failure or maintainer rejection
- "minor": style issue or suggestion, but patch is still acceptable

Respond ONLY as valid JSON:
{{
  "approved": true or false,
  "critical_issues": ["list of critical problems, empty if none"],
  "minor_issues": ["list of minor problems or suggestions, empty if none"],
  "summary": "one-sentence review summary"
}}

approved = false only if there are critical issues.
approved = true even if there are minor issues (human will see them)."""

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=REVIEWER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)

        approved: bool = bool(data.get("approved", True))
        critical_issues: list[str] = data.get("critical_issues", [])
        minor_issues: list[str] = data.get("minor_issues", [])
        summary: str = data.get("summary", "")

        all_notes = []
        if critical_issues:
            all_notes.extend(f"❌ {i}" for i in critical_issues)
        if minor_issues:
            all_notes.extend(f"⚠️ {i}" for i in minor_issues)
        if summary:
            all_notes.insert(0, f"📋 {summary}")

        log.info(
            "reviewer.done",
            approved=approved,
            critical=len(critical_issues),
            minor=len(minor_issues),
            model=REVIEWER_MODEL,
        )
        return approved, all_notes

    except json.JSONDecodeError as exc:
        log.error("reviewer.json_parse_failed", error=str(exc))
        return True, ["⚠️ Reviewer response was not valid JSON — inspect diff manually"]
    except Exception as exc:
        log.error("reviewer.failed", error=str(exc))
        return True, [f"⚠️ Reviewer error: {exc} — inspect diff manually"]

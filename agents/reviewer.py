"""
FI-PR-GENERATOR — Independent Reviewer Agent
Uses Qwen 2.5 Coder (different model from coder!) to review the diff.
Reviews for: imports, edge cases, scope creep, test coverage, correctness.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

REVIEW_PASS_THRESHOLD = 80   # Score >= 80 = pass

REVIEW_CHECKLIST = """
Review the diff below for these exact issues:
1. Missing imports or undefined references
2. Type errors or wrong argument types
3. Scope creep — files changed outside the approved set
4. Broken edge cases (null, empty, boundary values)
5. Accidental regression — code that worked before but may break now
6. Test coverage gap — does the fix need a test?
7. The fix actually solves the stated issue (not just silences it)
8. Code style matches the surrounding code

For each issue found, rate severity: CRITICAL | WARNING | INFO
"""


class ReviewerAgent:
    """
    Independent code reviewer using Qwen (different from the coder Claude).
    Returns a structured review with score and specific issues found.
    """

    def __init__(self):
        # Groq Setup
        groq_key = os.environ.get("GROQ_API_KEY", "")
        self._client = None
        self._model  = "qwen-qwq-32b"     # Qwen via Groq
        if groq_key:
            try:
                import groq as _groq
                self._client = _groq.Groq(api_key=groq_key)
            except Exception as e:
                log.warning("reviewer.groq_init_failed", error=str(e))

        # OpenRouter Setup (Fallback)
        self._openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._openrouter_client = None
        if self._openrouter_key:
            try:
                from openai import OpenAI
                self._openrouter_client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=self._openrouter_key,
                )
            except Exception as e:
                log.warning("reviewer.openrouter_init_failed", error=str(e))

    def review(
        self,
        diff: str,
        issue_title: str,
        issue_body: str,
        approved_files: list[str],
        changed_files: list[str],
    ) -> tuple[bool, float, list[str], str]:
        """
        Review the diff.

        Returns:
            passed: bool  (score >= threshold)
            score:  float (0-100)
            issues: list of found issues
            summary: str (one-line verdict)
        """
        if not diff or not diff.strip():
            return False, 0.0, ["No diff to review — coder produced no changes"], "No changes"

        # Check for scope creep first (pure code check, no LLM)
        scope_issues = self._check_scope(changed_files, approved_files)

        review_result = self._call_llm_review(
            diff, issue_title, issue_body, approved_files
        )

        # Merge scope issues
        all_issues = scope_issues + review_result.get("issues", [])
        score      = review_result.get("score", 50.0)

        # Critical issues auto-fail regardless of score
        has_critical = any("CRITICAL" in i.upper() for i in all_issues)
        if has_critical:
            score = min(score, 50.0)

        # Scope creep always fails
        if scope_issues:
            score = min(score, 40.0)

        passed  = score >= REVIEW_PASS_THRESHOLD and not has_critical
        summary = review_result.get("summary", f"Score: {score:.0f}/100")

        log.info("reviewer.result",
                 score=score,
                 passed=passed,
                 issues_count=len(all_issues),
                 has_critical=has_critical)

        return passed, score, all_issues, summary

    def _check_scope(
        self, changed_files: list[str], approved_files: list[str]
    ) -> list[str]:
        """
        Pure code check: did coder touch unapproved files?
        Returns list of scope violations.
        """
        approved_set = {f.lstrip("./") for f in approved_files}
        violations = []
        for f in changed_files:
            clean = f.lstrip("./")
            if clean not in approved_set:
                violations.append(
                    f"CRITICAL: File '{f}' was modified but is NOT in the approved file list"
                )
        if violations:
            log.warning("reviewer.scope_violation", files=changed_files,
                        approved=approved_files)
        return violations

    def _call_llm_review(
        self,
        diff: str,
        issue_title: str,
        issue_body: str,
        approved_files: list[str],
    ) -> dict:
        """Ask Qwen to review the diff. Returns JSON with score and issues."""
        # Truncate diff to avoid context overflow
        diff_preview = diff[:6000] if len(diff) > 6000 else diff
        files_hint   = ", ".join(approved_files[:5])

        prompt = f"""You are a senior code reviewer. Review this diff and score it.

Issue: {issue_title}
Approved files: {files_hint}

Diff:
```diff
{diff_preview}
```

{REVIEW_CHECKLIST}

Respond with ONLY valid JSON:
{{
  "score": <0-100>,
  "summary": "<one sentence verdict>",
  "issues": [
    "<SEVERITY>: <specific issue description>",
    ...
  ]
}}

If the diff correctly fixes the issue with no problems, score should be 85-100.
Be strict but fair. An empty issues list is valid for a clean diff."""

        # 1. Try Groq first
        if self._client:
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content.strip()
                return json.loads(text)
            except Exception as e:
                log.warning("reviewer.groq_error", error=str(e), msg="Falling back to OpenRouter...")

        # 2. Try OpenRouter fallback
        if self._openrouter_client:
            try:
                resp = self._openrouter_client.chat.completions.create(
                    model="qwen/qwen-2.5-coder-32b-instruct",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content.strip()
                return json.loads(text)
            except Exception as e:
                log.warning("reviewer.openrouter_error", error=str(e))

        # 3. Static fallback
        return {
            "score": 50.0,
            "summary": "Review failed — using conservative score",
            "issues": ["WARNING: LLM review unavailable, manual check recommended"],
        }
